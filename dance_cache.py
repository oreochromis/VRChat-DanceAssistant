#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VRChat跳舞房助手（核心模块 + 命令行模式）
原理：hosts 劫持 cdn.pypy.dance -> 本地 HTTP 服务(80 端口) -> 命中缓存直接返回 / 未命中边下边存
同时监听 VRChat 日志中的 [PyPyDanceQueue] 点歌队列，提前下载即将播放的视频。
曲库模式：设置 library_dir 后，视频按歌曲编号命名（如 3528.mp4）永久保存在曲库目录；
播放时本地有就直接加载，没有就下载后新增进曲库。
YouTube 源：歌单直取开启时优先按 ytid 探测 CDN 同名文件并纳入预载；CDN 无文件时，
播放中从 VRChat 日志捕获解析后的直链录流入库；Unity 的 https 歌单/封面请求由 443 TCP 隧道透传。
需要管理员权限（修改 hosts、绑定 80/443 端口）。
图形界面见 dance_cache_gui.py。
"""

import ctypes
import copy
import http.client
import ipaddress
import json
import math
import os
import random
import re
import select
import shutil
import socket
import ssl
import sys
import threading
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if getattr(sys, "frozen", False):
    # PyInstaller 打包后 __file__ 指向临时解压目录，程序数据要放在 exe 旁边
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Bundled read-only resources live beside this module, including in PyInstaller
# _internal. Keep the selected default implicit so moving an installation works.
DEFAULT_COOLDOWN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "default_cooldown.mp4")
HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
HOSTS_MARK = "# dance-cache"
CDN_HOSTS = ["cdn.pypy.dance", "806bb815.cdn.pypy.dance"]
API_HOST = "api.pypy.dance"
UDON_API_HOST = "api.udon.dance"    # WannaDance：302 跳转服务（不劫持，仅解析真实 IP 供预载用）
UDON_PLAY_HOST = "play.udon.dance"  # WannaDance：视频文件 CDN（劫持）
DUDU_API_HOST = "api.dudufit.dance"
DUDU_PLAY_HOST = "global-cdn.dudufit.dance"
HIJACK_HOSTS = CDN_HOSTS + [API_HOST]  # api 仅在 YouTube 缓存模式启用时劫持
VRC_LOG_DIR = os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "LocalLow", "VRChat", "VRChat")


def _default_roaming_root():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "VRChatDanceAssistant")


ROAMING_ROOT = _default_roaming_root()  # 系统用户目录数据根（测试可改写）
_STORAGE_MODES = ("portable", "roaming")
_storage_mode_override = None           # 测试用：强制数据保存位置


def _pointer_path():
    return os.path.join(ROAMING_ROOT, "storage-mode.json")


def get_storage_mode():
    """数据保存位置：portable=随程序目录；roaming=系统用户目录（默认）。
    无指针文件时一律默认用户目录；exe 旁有旧版便携数据时由
    _migrate_legacy_portable_data 在加载配置前自动迁移，不会读到空配置。"""
    if _storage_mode_override:
        return _storage_mode_override
    if _legacy_fallback:
        return "portable"
    try:
        with open(_pointer_path(), "r", encoding="utf-8") as f:
            mode = json.load(f).get("mode")
        if mode in _STORAGE_MODES:
            return mode
    except (OSError, ValueError, AttributeError):
        pass
    return "roaming"


_legacy_fallback = False  # 旧数据迁移失败时的本次会话兜底：按便携模式读取


def _migrate_legacy_portable_data():
    """exe 旁有旧版数据且尚未记录模式：复制迁移到默认的用户目录模式。
    原文件保留不删；迁移失败时本次会话回退便携模式，绝不读到空配置。"""
    global _legacy_fallback
    if _storage_mode_override or os.path.isfile(_pointer_path()):
        return
    if not os.path.isfile(os.path.join(SCRIPT_DIR, "config.json")):
        return
    try:
        os.makedirs(ROAMING_ROOT, exist_ok=True)
        copied = 0
        for name in _STORAGE_FILES:
            src = os.path.join(SCRIPT_DIR, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(ROAMING_ROOT, name))
                copied += 1
        for name in _STORAGE_DIRS:
            src = os.path.join(SCRIPT_DIR, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(ROAMING_ROOT, name), dirs_exist_ok=True)
        with open(_pointer_path(), "w", encoding="utf-8") as f:
            json.dump({"mode": "roaming"}, f)
        log(f"检测到旧版本的数据文件，已迁移到系统用户目录（{ROAMING_ROOT}，{copied} 个文件，原位置保留）；"
            "可在 设置 → 数据保存位置 改回便携模式")
    except OSError as e:
        _legacy_fallback = True
        log(f"旧数据迁移失败（本次仍按便携模式读取，下次启动重试）: {e}")


def data_dir():
    """当前生效的数据目录：config.json/mapping.json/歌单缓存等用户数据的存放位置"""
    return SCRIPT_DIR if get_storage_mode() == "portable" else ROAMING_ROOT


def get_storage_info():
    return {"mode": get_storage_mode(), "portable_dir": SCRIPT_DIR,
            "roaming_dir": ROAMING_ROOT, "active_dir": data_dir()}


MAPPING_FILENAME = "mapping.json"


def mapping_path():
    return os.path.join(data_dir(), MAPPING_FILENAME)


_STORAGE_FILES = ("config.json", MAPPING_FILENAME, "bundle_songs.json", "dead_songs.json",
                  "play_history.jsonl", "played_history.jsonl", "room_events.jsonl", "vrcx-settings.json", "vrcx-stats.json",
                  "vrcx-source.sqlite3", "vrcx-edited.sqlite3", "play-stats-cache.json", "wanna-favorites.json",
                  "profile.json", "profile-avatar.png", "avatar-index.json",
                  "cdn-favorites.json", "cdn-favorites-migration.json", "cdn-pool.json", "wannadance-catalog.json", "dudu-catalog.json")
_STORAGE_DIRS = ("avatars", "record-backups", "dudu-https")


def set_storage_mode(mode):
    """切换数据保存位置并迁移数据文件（复制，旧位置文件保留不删）。
    需停止服务且无曲库任务；指针文件始终写在用户目录，先落指针再重载。"""
    if mode not in _STORAGE_MODES:
        raise ValueError("未知的数据保存位置: " + str(mode))
    with _lifecycle_lock:
        if _running:
            raise RuntimeError("请先停止服务，再更改数据保存位置")
        if rebuilding() or verifying():
            raise RuntimeError("曲库任务进行中，请等待完成或取消后再更改数据保存位置")
        if mode == get_storage_mode():
            return get_storage_info()
        source = data_dir()
        target = SCRIPT_DIR if mode == "portable" else ROAMING_ROOT
        os.makedirs(target, exist_ok=True)
        copied = 0
        for name in _STORAGE_FILES:
            src = os.path.join(source, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(target, name))
                copied += 1
        for name in _STORAGE_DIRS:
            src = os.path.join(source, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(target, name), dirs_exist_ok=True)
        os.makedirs(ROAMING_ROOT, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="storage-mode-", suffix=".tmp", dir=ROAMING_ROOT)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"mode": mode}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, _pointer_path())
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
        with _mapping_lock:
            _mapping.clear()
            _reverse_mapping.clear()
        with _resolved_lock:
            _resolved_ids.clear()
            _resolve_retry_at.clear()
        load_config()
        load_mapping()
    label = "随程序目录（便携）" if mode == "portable" else "系统用户目录"
    log(f"数据保存位置已切换为{label}：{target}（已迁移 {copied} 个数据文件，原位置文件保留）")
    emit("config", changed=[], restart_required=False)
    return get_storage_info()


QUEUE_RE = re.compile(r"^\[PyPyDanceQueue] (\[.*])")
WD_QUEUE_RE = re.compile(r"queue info serialized: (\[.*])", re.IGNORECASE)  # WannaDance / DuDu 队列
# WannaDance 当前歌曲行（queue info 不含当前歌曲，开播即移出，需从这里解析后插回队首）
WD_STATE_RE = re.compile(r"UpdateState: version = [^,]+, songId = (\d+), player = ([^,]*), isRandom = \w+, infoString = (.*?), volume = ")
FILENAME_RE = re.compile(r"^[\w.\-]+\.mp4$")
YT_FILENAME_RE = re.compile(r"^yt_(\d+)\.mp4$")
WD_FILE_RE = re.compile(r"^(\d+)-[0-9a-z]+\.mp4$", re.IGNORECASE)  # play.udon.dance 的文件名 5283-667d628852a98.mp4
WD_CACHE_RE = re.compile(r"^wd_(\d+)\.mp4$")  # WannaDance 本地缓存命名


def hijack_host_list(include_api):
    return CDN_HOSTS + [UDON_PLAY_HOST] + ([API_HOST] if include_api else []) + ([DUDU_PLAY_HOST] if _config.get("dudu_enabled") else [])

DEFAULT_CONFIG = {
    "cache_dir": "cache",
    "library_dir": "",
    "wd_library_dir": "",
    "max_cache_mb": 2048,
    "preload_count": 3,
    "preload_sequential": True,
    "max_parallel": 2,
    "preferred_cdn_ip": "",
    "auto_prefer_cdn": True,
    "speedtest_threads": 3,
    "speedtest_candidates": 8,
    "speedtest_sample_mb": 8,
    "enable_youtube_cache": True,
    "youtube_permanent": False,
    "cdn_permanent": False,
    "wd_permanent": False,
    "dudu_enabled": False,
    "dudu_library_dir": "",
    "dudu_permanent": False,
    "cooldown_file": "",
    "cooldown_trigger": "next",
    "bundle_direct": True,
    "vr_overlay": True,
    "close_to_tray": False,
    "auto_start_service": True,
}

_config = {}
_cdn_selection_revision = 0
_origin_ips = {}             # host -> [ip, ...]
_downloads_in_progress = {}  # 目标路径 -> thread
_downloads_lock = threading.RLock()
_download_meta = {}          # 规范化目标路径 -> GUI 可读任务元信息
_download_results = {}       # 最近一次下载结果（仅内存，供队列状态使用）
_config_lock = threading.RLock()
_lifecycle_lock = threading.RLock()
_hosts_lock = threading.RLock()
_runtime_lock = threading.RLock()
_library_task_lock = threading.Lock()
_cooldown_lock = threading.RLock()
_run_id = 0
_service_threads = []
_hosts_restore_pending = False
_runtime_state = {
    "running": False, "phase": "stopped", "youtube_available": False,
    "queue_watcher_available": False, "hosts_restore_pending": False,
    "warnings": [], "error": None,
}
_resolved_ids = {}           # songNum -> filename or None（已确认的结果，会话内有效）
_resolve_retry_at = {}       # songNum -> 允许再次解析的时间戳（无结果答复的退避）
_resolved_lock = threading.Lock()
RESOLVE_RETRY_DELAY = 60     # 得到答复但没拿到文件名时，隔多久允许重试一次
_mapping = {}                # song_id -> filename（"" 表示非CDN源）
_reverse_mapping = {}        # filename -> song_id
_mapping_lock = threading.Lock()
_mapping_dirty = False
_stop_event = threading.Event()
_speedtest_stop = threading.Event()
_server = None
_dudu_server = None
_watcher = None
_running = False
_library_stop = threading.Event()
_rate_limit_until = 0.0
_rate_limit_lock = threading.Lock()

RETRYABLE_STATUS = {429, 500, 502, 503, 520, 521, 522, 524}


class TransientUpstream(Exception):
    """源站临时错误（限流/5xx），可退避重试"""

    def __init__(self, status, retry_after=0.0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}")


def note_rate_limited(retry_after):
    global _rate_limit_until
    with _rate_limit_lock:
        _rate_limit_until = max(_rate_limit_until, time.time() + retry_after)


def wait_rate_limit():
    with _rate_limit_lock:
        delay = _rate_limit_until - time.time()
    if delay > 0:
        log(f"触发服务器限流，全局等待 {delay:.0f} 秒...")
        _stop_event.wait(min(delay, 300))


# ---------------- 事件总线 ----------------
# 事件类型: log / queue / progress / download_start / download_done / stats / state / session
#          rebuild_progress / rebuild_done / verify_progress / verify_done

_listeners = []
_listeners_lock = threading.Lock()
_last_queue_items = []  # 最近一次点歌队列（用于下载完成后刷新队列缓存状态）
_queue_lock = threading.RLock()


def add_listener(fn):
    with _listeners_lock:
        if fn not in _listeners:
            _listeners.append(fn)


def remove_listener(fn):
    with _listeners_lock:
        if fn in _listeners:
            _listeners.remove(fn)


def emit(kind, **data):
    with _listeners_lock:
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(kind, data)
        except Exception:
            pass
    if kind == "download_done":
        refresh_queue_state()


def log(msg):
    emit("log", msg=msg)


_default_thread_excepthook = threading.excepthook


def _thread_excepthook(args):
    # 后台线程未捕获异常默认只打到 stderr，GUI 下用户看不到；转进运行日志，
    # “预载线程静默消失”这类故障才能被发现。
    try:
        name = args.thread.name if args.thread is not None else "?"
        tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        log(f"后台线程 {name} 异常退出:\n{tb.strip()}")
    except Exception:
        pass
    try:
        _default_thread_excepthook(args)
    except Exception:
        pass


threading.excepthook = _thread_excepthook


# ---------------- 会话统计（本地命中/回源/节省流量，按文件去重） ----------------

_session_lock = threading.Lock()
_session_hits = 0
_session_misses = 0
_session_saved_bytes = 0
_session_hit_files = set()
_session_miss_files = set()


def get_session_stats():
    with _session_lock:
        return {"hits": _session_hits, "misses": _session_misses,
                "saved_mb": _session_saved_bytes / 1048576}


def _emit_session():
    emit("session", **get_session_stats())


def _reset_session_stats():
    global _session_hits, _session_misses, _session_saved_bytes
    with _session_lock:
        _session_hits = _session_misses = 0
        _session_saved_bytes = 0
        _session_hit_files.clear()
        _session_miss_files.clear()
    _emit_session()


def _note_session_hit(filename, path):
    global _session_hits, _session_saved_bytes
    with _session_lock:
        if filename in _session_hit_files:
            return
        _session_hit_files.add(filename)
        _session_hits += 1
        try:
            _session_saved_bytes += os.path.getsize(path)
        except OSError:
            pass
    _emit_session()


def _note_session_miss(filename):
    global _session_misses
    with _session_lock:
        if filename in _session_miss_files:
            return
        _session_miss_files.add(filename)
        _session_misses += 1
    _emit_session()


# ---------------- 点歌历史（排行/年度报告的原始数据，JSONL 逐行追加） ----------------

HISTORY_FILENAME = "play_history.jsonl"
PLAYED_HISTORY_FILENAME = "played_history.jsonl"

_history_lock = threading.Lock()
_history_last_keys = set()  # 上一帧队列的 (kind, id, player) 集合，用于 diff 出"新点"的歌
_last_playing_key = None


def history_path():
    return os.path.join(data_dir(), HISTORY_FILENAME)


def record_queue_snapshot(items):
    """从队列快照 diff 出新入队的歌曲，逐行追加到 play_history.jsonl。
    同一首歌在队列里停留期间重复出现的快照不会重复记；离开队列后再次入队
    （重听/重点）会再记一条。items 为 None 表示这批日志没有队列行，不处理。"""
    global _history_last_keys
    if items is None:
        return
    keys = set()
    entries = []
    now = time.time()
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("songNum", 0)
        if not (isinstance(sid, int) and not isinstance(sid, bool) and sid > 0):
            continue
        kind = "dudu" if it.get("dudu") else "wd" if it.get("wd") else "pypy"
        player = str(it.get("playerName") or "").strip()
        key = (kind, sid, player)
        if key in keys:
            continue
        keys.add(key)
        entries.append({"ts": now, "kind": kind, "id": sid,
                        "name": str(it.get("videoName") or "").strip(),
                        "player": player})
    with _history_lock:
        fresh = [e for e in entries if (e["kind"], e["id"], e["player"]) not in _history_last_keys]
        if fresh:
            try:
                os.makedirs(data_dir(), exist_ok=True)
                with open(history_path(), "a", encoding="utf-8") as f:
                    for e in fresh:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
            except OSError as e:
                log(f"点歌历史写入失败，将在下一条队列记录时重试: {e}")
                return
        _history_last_keys = keys


def played_history_path():
    return os.path.join(data_dir(), PLAYED_HISTORY_FILENAME)


ROOM_EVENT_FILENAME = "room_events.jsonl"
_room_event_lock = threading.Lock()
_room_event_keys = None
_native_location = None
ROOM_JOIN_RE = re.compile(r"\[Behaviour\] Joining (wrld_[a-f0-9-]+)(?::(.*))?")
ROOM_LEAVE_RE = re.compile(r"\[Behaviour\] OnPlayerLeft (.+?) \((usr_[^)]+)\)")
ROOM_TIME_RE = re.compile(r"^(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})")


def room_events_path():
    return os.path.join(data_dir(), ROOM_EVENT_FILENAME)


def _load_room_event_keys():
    global _room_event_keys
    if _room_event_keys is not None:
        return
    _room_event_keys = set()
    try:
        with open(room_events_path(), "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                    key = (entry.get("kind"), round(float(entry["ts"]), 3),
                           entry.get("location", ""), entry.get("user_id", ""))
                    _room_event_keys.add(key)
                except (ValueError, TypeError, KeyError):
                    continue
    except OSError:
        pass


def record_room_events(lines):
    global _native_location
    discovered = []
    for line in lines:
        time_match = ROOM_TIME_RE.search(line)
        join_match = ROOM_JOIN_RE.search(line)
        leave_match = ROOM_LEAVE_RE.search(line)
        if not time_match or not (join_match or leave_match):
            continue
        try:
            timestamp = datetime.strptime(time_match.group(1), "%Y.%m.%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if join_match:
            world_id = join_match.group(1)
            instance = join_match.group(2)
            location = world_id + ":" + instance if instance else world_id
            _native_location = location
            discovered.append({"ts": timestamp, "kind": "location", "world_id": world_id, "location": location})
        else:
            discovered.append({"ts": timestamp, "kind": "leave", "location": _native_location or "",
                               "user_id": leave_match.group(2)})
    if not discovered:
        return 0
    with _room_event_lock:
        _load_room_event_keys()
        fresh = []
        keys = []
        for entry in discovered:
            key = (entry["kind"], round(float(entry["ts"]), 3), entry.get("location", ""), entry.get("user_id", ""))
            if key not in _room_event_keys:
                fresh.append(entry)
                keys.append(key)
        if fresh:
            os.makedirs(data_dir(), exist_ok=True)
            with open(room_events_path(), "a", encoding="utf-8") as handle:
                for entry in fresh:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _room_event_keys.update(keys)
    return len(fresh)


def scan_recent_room_events():
    path = find_newest_log()
    if path is None:
        return 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 2 * 1024 * 1024))
        lines = handle.read().splitlines()
    return record_room_events(lines)


def recent_activity_timestamp():
    latest = 0.0
    for path in (history_path(), played_history_path(), room_events_path()):
        try:
            with open(path, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 65536))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()[-20:]
        except OSError:
            continue
        for line in lines:
            try:
                latest = max(latest, float(json.loads(line)["ts"]))
            except (ValueError, TypeError, KeyError):
                continue
    return latest


def _compact_jsonl_history(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return 0
    seen = set()
    unique = []
    duplicates = 0
    for line in lines:
        try:
            key = json.dumps(json.loads(line), ensure_ascii=False, sort_keys=True)
        except (ValueError, TypeError):
            unique.append(line)
            continue
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
            unique.append(line)
    if duplicates:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.writelines(unique)
        os.replace(temporary, path)
    return duplicates


def compact_history_files():
    removed = 0
    with _history_lock:
        removed += _compact_jsonl_history(history_path())
        removed += _compact_jsonl_history(played_history_path())
    with _room_event_lock:
        removed += _compact_jsonl_history(room_events_path())
    return removed


def record_playing_snapshot(items):
    """只在队首当前歌曲变化时记录实际播放；空队列允许之后同曲重新计入。"""
    global _last_playing_key
    if not items or not isinstance(items[0], dict):
        _last_playing_key = None
        return
    current = items[0]
    sid = current.get("songNum", 0)
    if isinstance(sid, bool) or not isinstance(sid, int) or sid <= 0:
        _last_playing_key = None
        return
    kind = "dudu" if current.get("dudu") else "wd" if current.get("wd") else "pypy"
    player = str(current.get("playerName") or "").strip()
    key = (kind, sid, player)
    if key == _last_playing_key:
        return
    entry = {"ts": time.time(), "kind": kind, "id": sid,
             "name": str(current.get("videoName") or "").strip(), "player": player}
    try:
        os.makedirs(data_dir(), exist_ok=True)
        with open(played_history_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"实际播放记录写入失败，将在下一条队列记录时重试: {e}")
        return
    _last_playing_key = key


def self_played_count(start_ts, end_ts, self_names):
    """统计自己点播且已经实际开始播放的次数；没有自己身份信息时不猜测。"""
    names = {str(name).strip() for name in (self_names or []) if str(name).strip()}
    if not names:
        return 0
    total = 0
    try:
        with open(played_history_path(), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return 0
    for line in lines:
        try:
            entry = json.loads(line)
            ts = float(entry["ts"])
        except (ValueError, TypeError, KeyError):
            continue
        if start_ts <= ts < end_ts and str(entry.get("player") or "").strip() in names:
            total += 1
    return total


def play_history_stats(start_ts=None, end_ts=None, kind=None, player=None):
    """聚合实际开始播放的点歌；player 为显示名称时只统计该玩家。
    start_ts/end_ts 为本地时间戳（end 不含），kind 限 "pypy"/"wd"。
    available_players 包含当前日期和来源内的全部点歌人，不受 player 筛选影响。"""
    selected_player = str(player or "").strip()
    counts = {}
    players = {}
    available_players = set()
    months = {}
    days = set()
    total = 0
    earliest = None
    try:
        with open(played_history_path(), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            ts = float(entry["ts"])
        except (ValueError, TypeError, KeyError):
            continue
        if earliest is None or ts < earliest:
            earliest = ts
        if start_ts is not None and ts < start_ts:
            continue
        if end_ts is not None and ts >= end_ts:
            continue
        sid = entry.get("id")
        if isinstance(sid, bool) or not isinstance(sid, int):
            continue
        ekind = entry.get("kind") if entry.get("kind") in ("pypy", "wd", "dudu") else "pypy"
        if kind and ekind != kind:
            continue
        entry_player = str(entry.get("player") or "").strip()
        if entry_player:
            available_players.add(entry_player)
        if selected_player and entry_player != selected_player:
            continue
        total += 1
        try:
            local = time.localtime(ts)
            month = time.strftime("%Y-%m", local)
            months[month] = months.get(month, 0) + 1
            days.add(time.strftime("%Y-%m-%d", local))
        except (OverflowError, OSError, ValueError):
            pass
        key = (ekind, sid)
        slot = counts.setdefault(key, {"id": sid, "kind": ekind, "name": "", "count": 0,
                                       "players": {}, "first_ts": ts, "last_ts": ts})
        slot["count"] += 1
        name = str(entry.get("name") or "").strip()
        if name and name != "?":
            slot["name"] = name
        if entry_player:
            slot["players"][entry_player] = slot["players"].get(entry_player, 0) + 1
            players[entry_player] = players.get(entry_player, 0) + 1
        slot["first_ts"] = min(slot["first_ts"], ts)
        slot["last_ts"] = max(slot["last_ts"], ts)
    songs = sorted(counts.values(), key=lambda s: (-s["count"], s["first_ts"]))
    for rank, slot in enumerate(songs, 1):
        slot["rank"] = rank
        slot["top_player"] = max(slot["players"].items(), key=lambda kv: kv[1])[0] if slot["players"] else ""
        del slot["players"]
    top_players = sorted(({"player": p, "count": c} for p, c in players.items()),
                         key=lambda x: (-x["count"], x["player"]))
    return {"total": total, "songs": songs, "players": top_players, "earliest": earliest,
            "available_players": sorted(available_players, key=str.casefold),
            "months": [{"month": month, "count": months[month]} for month in sorted(months)],
            "active_days": len(days)}


# ---------------- 配置 ----------------

def load_config():
    global _config, _cdn_selection_revision
    _migrate_legacy_portable_data()
    path = os.path.join(data_dir(), "config.json")
    missing = not os.path.exists(path)
    warnings = []
    loaded = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("配置文件必须是对象")
            # 历史配置可能共用目录。保留其身份，启动/清理拒绝执行，供设置页修正。
            cfg = _validated_config({**DEFAULT_CONFIG, **loaded}, allow_shared_storage=True)
        except Exception as e:
            warnings.append(f"config.json 参数无效，已恢复默认参数并保留可读取的保存目录: {e}")
            preserved = {}
            if isinstance(loaded, dict):
                for name in ("cache_dir", "library_dir", "wd_library_dir", "dudu_library_dir"):
                    if isinstance(loaded.get(name), str) and loaded[name].strip():
                        preserved[name] = loaded[name]
            cfg = _validated_config({**DEFAULT_CONFIG, **preserved}, allow_shared_storage=True)
    else:
        cfg = _validated_config(dict(DEFAULT_CONFIG))
    os.makedirs(cfg["cache_dir"], exist_ok=True)
    lib = cfg.get("library_dir") or ""
    if lib:
        try:
            os.makedirs(lib, exist_ok=True)
        except Exception as e:
            # 保留开关与目录原值，由运行时暂缓机制处理，修复目录后自动生效
            warnings.append(f"曲库目录不可用，永久保存暂缓生效: {e}")
    wd_lib = cfg.get("wd_library_dir") or ""
    if wd_lib:
        try:
            os.makedirs(wd_lib, exist_ok=True)
        except Exception as e:
            warnings.append(f"WannaDance 曲库目录不可用，其永久保存暂缓生效: {e}")
    dudu_lib = cfg.get("dudu_library_dir") or ""
    if dudu_lib:
        try:
            os.makedirs(dudu_lib, exist_ok=True)
        except Exception as e:
            warnings.append(f"DuDu 曲库目录不可用，其永久保存暂缓生效: {e}")
    with _config_lock:
        _config = cfg
        _cdn_selection_revision += 1
    if _same_directory(cfg["cache_dir"], cfg.get("library_dir")):
        warnings.append("缓存目录与永久曲库相同，已禁用服务启动与缓存清理，请在设置中分开保存目录")
    if _same_directory(cfg["cache_dir"], cfg.get("wd_library_dir")):
        warnings.append("缓存目录与 WannaDance 曲库相同，已禁用服务启动，请在设置中分开保存目录")
    if _same_directory(cfg["cache_dir"], cfg.get("dudu_library_dir")):
        warnings.append("缓存目录与 DuDu 曲库相同，已禁用服务启动，请在设置中分开保存目录")
    for warning in warnings:
        log(warning)
        emit("warning", message=warning)
    if warnings:
        _set_runtime_state(warnings=warnings)
    if missing:
        save_config()


def _canonical_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _same_directory(left, right):
    if not left or not right:
        return False
    try:
        return os.path.samefile(left, right)
    except OSError:
        return _canonical_path(left) == _canonical_path(right)


def _validated_config(config, *, allow_shared_storage=False):
    cfg = copy.deepcopy(config)
    for name, minimum in (("preload_count", 0), ("max_cache_mb", 100), ("max_parallel", 1)):
        value = cfg.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数")
    threads = cfg.get("speedtest_threads")
    if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 8:
        raise ValueError("speedtest_threads 必须是 1 到 8 的整数")
    for name, minimum, maximum, title in (
            ("speedtest_candidates", 2, 64, "快速测速候选数量"),
            ("speedtest_sample_mb", 1, 32, "测速样本大小")):
        value = cfg.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{title}必须是 {minimum} 到 {maximum} 的整数")
    if cfg.get("cooldown_trigger") not in ("next", "reload"):
        raise ValueError("cooldown_trigger 必须是 next 或 reload")
    for name in ("enable_youtube_cache", "youtube_permanent", "cdn_permanent", "wd_permanent", "dudu_enabled", "dudu_permanent", "preload_sequential", "auto_prefer_cdn", "bundle_direct", "vr_overlay", "close_to_tray", "auto_start_service"):
        if not isinstance(cfg.get(name), bool):
            raise ValueError(f"{name} 必须是布尔值")
    for name in ("cache_dir", "library_dir", "wd_library_dir", "dudu_library_dir", "preferred_cdn_ip", "cooldown_file"):
        if not isinstance(cfg.get(name), str):
            raise ValueError(f"{name} 必须是文本")
        cfg[name] = cfg[name].strip()
    if not cfg["cache_dir"]:
        raise ValueError("缓存目录不能为空")
    for name in ("cache_dir", "library_dir", "wd_library_dir", "dudu_library_dir", "cooldown_file"):
        if cfg[name]:
            cfg[name] = os.path.abspath(os.path.join(data_dir(), os.path.expanduser(cfg[name])))
    if not allow_shared_storage and _same_directory(cfg["cache_dir"], cfg["library_dir"]):
        raise ValueError("缓存目录与永久曲库不能是同一个目录")
    if not allow_shared_storage and _same_directory(cfg["cache_dir"], cfg["wd_library_dir"]):
        raise ValueError("缓存目录与 WannaDance 曲库不能是同一个目录")
    if not allow_shared_storage and _same_directory(cfg["cache_dir"], cfg["dudu_library_dir"]):
        raise ValueError("缓存目录与 DuDu 曲库不能是同一个目录")
    if not allow_shared_storage and cfg["library_dir"] and _same_directory(cfg["library_dir"], cfg["wd_library_dir"]):
        raise ValueError("PyPyDance 曲库与 WannaDance 曲库不能是同一个目录")
    if not allow_shared_storage and cfg["library_dir"] and _same_directory(cfg["library_dir"], cfg["dudu_library_dir"]):
        raise ValueError("PyPyDance 曲库与 DuDu 曲库不能是同一个目录")
    if not allow_shared_storage and cfg["wd_library_dir"] and _same_directory(cfg["wd_library_dir"], cfg["dudu_library_dir"]):
        raise ValueError("WannaDance 曲库与 DuDu 曲库不能是同一个目录")
    ip = cfg["preferred_cdn_ip"]
    if ip:
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError as e:
            raise ValueError("优选 CDN IP 必须是有效的公网 IPv4 地址") from e
        if parsed.version != 4 or not parsed.is_global:
            raise ValueError("优选 CDN IP 必须是有效的公网 IPv4 地址")
    # 永久保存未设置曲库目录时不报错，自动暂缓生效（填写目录后即自动启用）
    return cfg


def get_config():
    """返回配置副本。界面通过 update_config 更新，不直接修改全局状态。"""
    with _config_lock:
        return copy.deepcopy(_config or _validated_config(dict(DEFAULT_CONFIG)))


def _write_config(config):
    cfg = copy.deepcopy(config)
    base = data_dir()
    try:
        cfg["cache_dir"] = os.path.relpath(cfg["cache_dir"], base)
    except (ValueError, KeyError):
        pass
    os.makedirs(base, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=base)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, os.path.join(base, "config.json"))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def save_config():
    """兼容旧界面；新界面使用会报告失败的 update_config。"""
    try:
        with _config_lock:
            _write_config(_config)
        return True
    except Exception as e:
        log(f"config.json 保存失败: {e}")
        return False


def update_config(patch, *, expected_cdn_revision=None, require_auto_cdn=False):
    """验证整份配置并原子保存；失败时不修改内存或原配置文件。"""
    global _config, _cdn_selection_revision
    if not isinstance(patch, dict):
        raise ValueError("配置更新必须是对象")
    unknown = set(patch) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError("未知配置项: " + ", ".join(sorted(unknown)))
    with _lifecycle_lock, _config_lock, _downloads_lock, _cooldown_lock:
        if expected_cdn_revision is not None and (
                _cdn_selection_revision != expected_cdn_revision
                or (require_auto_cdn and (not _config.get("auto_prefer_cdn", True)
                                         or _stop_event.is_set()))):
            return {"restart_required": False, "changed": []}
        previous = get_config()
        cfg = _validated_config({**previous, **copy.deepcopy(patch)})
        changed = [name for name in patch if previous.get(name) != cfg[name]]
        if not changed:
            return {"restart_required": False, "changed": []}
        paths_changed = bool({"cache_dir", "library_dir", "wd_library_dir", "dudu_library_dir"} & set(changed))
        busy = bool(_downloads_in_progress) or bool(_preload_dl_active) or rebuilding() or verifying()
        if paths_changed and busy:
            # 目录查找/保存路径每次现读配置，运行中改目录即时生效；
            # 仅在途下载与曲库任务已按旧路径落盘，需等其结束
            raise ValueError("有下载或曲库任务正在进行，请等待结束后再修改保存目录（无需停止服务）")
        if "enable_youtube_cache" in changed and (_running or busy):
            raise ValueError("请先停止服务并等待后台任务结束，再修改 YouTube 缓存开关")
        if "cooldown_file" in changed and cooldown_active():
            raise ValueError("请先取消 Cool Down，再更换放松视频")
        if "cooldown_trigger" in changed and cooldown_active():
            raise ValueError("请先取消 Cool Down，再修改触发时机")
        if cfg["cooldown_file"] and "cooldown_file" in changed:
            if not os.path.isfile(cfg["cooldown_file"]) or os.path.getsize(cfg["cooldown_file"]) == 0:
                raise ValueError("放松视频不存在或为空文件")
        for name in ("cache_dir", "library_dir", "wd_library_dir", "dudu_library_dir"):
            if cfg[name]:
                os.makedirs(cfg[name], exist_ok=True)
        _write_config(cfg)
        _config = cfg
        if {"preferred_cdn_ip", "auto_prefer_cdn"} & set(changed):
            _cdn_selection_revision += 1
    if _running:
        # 运行中改配置后立即重算暂缓类警告，状态灯提示保持准确
        _refresh_deferral_warnings()
    emit("config", changed=changed, restart_required=False)
    emit_stats()
    refresh_queue_state()
    return {"restart_required": False, "changed": changed}


_DEFERRAL_PREFIXES = ("PyPyDance 永久保存已开启", "WannaDance 永久保存已开启", "DuDu 永久保存已开启")


def _deferral_warnings():
    """按当前配置实时计算"永久保存暂缓"类警告"""
    out = []
    if (any(_config.get(k) for k in ("youtube_permanent", "cdn_permanent"))
            and not (_config.get("library_dir") or "").strip()):
        out.append("PyPyDance 永久保存已开启但未设置曲库目录，暂缓生效；在存储页填写曲库目录后自动启用")
    if _config.get("wd_permanent") and not (_config.get("wd_library_dir") or "").strip():
        out.append("WannaDance 永久保存已开启但未设置曲库目录，暂缓生效；在存储页填写曲库目录后自动启用")
    if _config.get("dudu_permanent") and not (_config.get("dudu_library_dir") or "").strip():
        out.append("DuDu 永久保存已开启但未设置曲库目录，暂缓生效；在存储页填写曲库目录后自动启用")
    return out


def _refresh_deferral_warnings():
    with _runtime_lock:
        kept = [w for w in (_runtime_state.get("warnings") or [])
                if not w.startswith(_DEFERRAL_PREFIXES)]
    _set_runtime_state(warnings=kept + _deferral_warnings())


def get_runtime_state():
    with _runtime_lock:
        return copy.deepcopy(_runtime_state)


def _set_runtime_state(**changes):
    with _runtime_lock:
        _runtime_state.update(changes)
        snapshot = copy.deepcopy(_runtime_state)
    emit("state", **snapshot)


def cache_path(filename):
    return os.path.join(_config["cache_dir"], filename)


# ---------------- 编号映射（song_id <-> CDN文件名） ----------------

def load_mapping():
    global _mapping_dirty
    try:
        with open(mapping_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        with _mapping_lock:
            for k, v in data.items():
                sid = int(k)
                _mapping[sid] = v
                if v:
                    _reverse_mapping[v] = sid
        _mapping_dirty = False
        log(f"已加载编号映射 {len(_mapping)} 条")
    except FileNotFoundError:
        pass
    except Exception as e:
        message = f"mapping.json 读取失败: {e}"
        log(message)
        emit("warning", message=message)
    load_bundle_cache()


_mapping_refresh_last = 0.0


def _refresh_queue_for_mapping():
    """学到新映射后刷新队列状态（节流 1s：重建映射会批量调用，逐次刷新会卡 UI）；
    并补投一次预载，让刚解析出的歌立刻开始下载，不必干等下一条队列日志。"""
    global _mapping_refresh_last
    now = time.time()
    if now - _mapping_refresh_last >= 1.0:
        _mapping_refresh_last = now
        refresh_queue_state()
        if _running and not _stop_event.is_set():
            with _queue_lock:
                items = list(_last_queue_items)
            if items:
                submit_preload_async(items)


def mark_mapping(song_id, filename):
    global _mapping_dirty
    with _mapping_lock:
        previous = _mapping.get(song_id)
        if previous == filename:
            return
        if previous and _reverse_mapping.get(previous) == song_id:
            _reverse_mapping.pop(previous, None)
        _mapping[song_id] = filename
        if filename:
            _reverse_mapping[filename] = song_id
        _mapping_dirty = True
    # 锁外刷新：refresh_queue_state -> describe_queue -> song_route 会再次获取 _mapping_lock，
    # 锁内调用会死锁（该锁不可重入）
    _refresh_queue_for_mapping()


def flush_mapping():
    global _mapping_dirty
    with _mapping_lock:
        if not _mapping_dirty:
            return
        data = dict(_mapping)
        _mapping_dirty = False
    temporary = None
    try:
        base = data_dir()
        os.makedirs(base, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="mapping-", suffix=".tmp", dir=base)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, mapping_path())
        temporary = None
    except Exception as e:
        with _mapping_lock:
            _mapping_dirty = True
        log(f"mapping.json 保存失败，将自动重试: {e}")
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except OSError:
                pass


def _mapping_flush_loop():
    while not _stop_event.wait(3):
        flush_mapping()


# ---------------- 歌单直取（bundle：id -> YouTube ID -> CDN 文件名） ----------------

def bundle_path():
    return os.path.join(data_dir(), "bundle_songs.json")


YT_ID_RE = re.compile(r"(?:v=|youtu\.be/)([\w-]{11})")

_bundle_ytid = {}            # song_id -> YouTube ID（仅用于歌单直取）
_bundle_names = {}           # song_id -> 歌名（官方歌单 n 字段，供曲库浏览显示）
_bundle_ids = set()          # 官方歌单全部编号，包括没有 YouTube ID 的歌曲
_bundle_lock = threading.Lock()
_bundle_miss = set()         # 已确认 CDN 上无对应文件的歌曲（持久化到 dead_songs.json）
_bundle_retry_at = {}        # 探测网络错误的退避时间戳


def dead_path():
    return os.path.join(data_dir(), "dead_songs.json")


_dead_loaded = False


def _ensure_dead_loaded():
    """把持久化的"CDN 已下架"记录并入 _bundle_miss（首次使用时读一次）。"""
    global _dead_loaded
    if _dead_loaded:
        return
    _dead_loaded = True
    try:
        with open(dead_path(), "r", encoding="utf-8") as f:
            for sid in json.load(f):
                _bundle_miss.add(int(sid))
    except (OSError, ValueError):
        pass


def _note_dead(song_id):
    """确认某首歌 CDN 上不存在：记入会话集合并持久化，供曲库浏览区分"已下架"。"""
    _ensure_dead_loaded()
    if song_id not in _bundle_miss:
        _bundle_miss.add(song_id)
        try:
            with open(dead_path(), "w", encoding="utf-8") as f:
                json.dump(sorted(_bundle_miss), f)
        except OSError as e:
            log(f"下架记录保存失败: {e}")


def _extract_ytid(urls):
    for u in urls:
        m = YT_ID_RE.search(u or "")
        if m:
            return m.group(1)
    return None


def _load_bundle_data(data):
    """从 bundle JSON 建完整编号表、ytid 直取表和歌名表，返回歌单条数。"""
    ids = set()
    table = {}
    names = {}
    for song in data.get("songs") or []:
        sid = song.get("i")
        urls = song.get("o") or []
        if isinstance(sid, int):
            ids.add(sid)
            ytid = _extract_ytid(urls if isinstance(urls, list) else [urls])
            if ytid:
                table[sid] = ytid
            name = str(song.get("n") or "").strip()
            if name:
                names[sid] = name
    if ids:
        with _bundle_lock:
            _bundle_ids.clear()
            _bundle_ids.update(ids)
            _bundle_ytid.clear()
            _bundle_ytid.update(table)
            _bundle_names.clear()
            _bundle_names.update(names)
    return len(ids)


def bundle_song_names():
    """id -> 歌名表（官方歌单）；为空时先尝试读本地缓存的 bundle。"""
    with _bundle_lock:
        names = dict(_bundle_names)
    if not names:
        load_bundle_cache()
        with _bundle_lock:
            names = dict(_bundle_names)
    return names


def load_bundle_cache():
    """启动时先读本地缓存的歌单（不等网络，歌单变化由 fetch_bundle 在线更新）"""
    try:
        with open(bundle_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        log(f"bundle_songs.json 读取失败: {e}")
        return
    n = _load_bundle_data(data)
    if n:
        log(f"已加载本地歌单直取表 {n} 条")
    _ensure_dead_loaded()


def fetch_bundle():
    """从 api.pypy.dance/bundle 拉取官方歌单，更新 id->ytid 直取表并持久化。
    失败保留旧表（含本地缓存），解析自然回退逐首 API 跳转。"""
    if not _origin_ips.get(API_HOST):
        return
    try:
        status, _, body = api_request("/bundle")
    except Exception as e:
        log(f"歌单直取表拉取失败: {e}")
        return
    if status != 200:
        log(f"歌单直取表拉取失败: HTTP {status}")
        return
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception as e:
        log(f"歌单直取表解析失败: {e}")
        return
    n = _load_bundle_data(data)
    if not n:
        log("歌单直取表为空，保留旧数据")
        return
    log(f"歌单直取表已更新：{n} 首")
    try:
        with open(bundle_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        log(f"bundle_songs.json 保存失败: {e}")


def _bundle_probe(song_num):
    """歌单直取：用歌单里的 ytid 拼 CDN 文件名，Range 探测确认存在后写入映射。
    返回文件名或 None（未开启/歌单没有/不存在/探测失败都回退原解析路径）。"""
    if not _config.get("bundle_direct", True):
        return None
    if song_num in _bundle_miss:
        return None
    with _bundle_lock:
        ytid = _bundle_ytid.get(song_num)
    if not ytid or time.time() < _bundle_retry_at.get(song_num, 0.0):
        return None
    filename = ytid + ".mp4"
    conn = None
    try:
        conn, resp = _open_upstream_once("cdn.pypy.dance", "/" + filename,
                                         extra_headers={"Range": "bytes=0-0"})
        status = resp.status
        resp.read()
    except Exception as e:
        _bundle_retry_at[song_num] = time.time() + 300
        log(f"歌单直取探测失败 id={song_num}: {e}，回退 API 解析")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if status in (200, 206):
        mark_mapping(song_num, filename)
        with _resolved_lock:
            _resolved_ids[song_num] = filename
        log(f"歌单直取命中 id={song_num} -> {filename}")
        return filename
    if status == 404:
        _note_dead(song_num)
        log(f"歌单直取无此文件 id={song_num}，回退 API 解析")
    else:
        log(f"歌单直取探测 id={song_num} 返回 HTTP {status}，回退 API 解析")
    return None


def invalidate_mapping(song_id, filename):
    """下载 404 说明映射已失效：清除映射与会话缓存，并记住这首歌 CDN 无此文件"""
    global _mapping_dirty
    with _mapping_lock:
        if _mapping.get(song_id) != filename:
            return
        del _mapping[song_id]
        _reverse_mapping.pop(filename, None)
        _mapping_dirty = True
    with _resolved_lock:
        _resolved_ids.pop(song_id, None)
    _note_dead(song_id)
    log(f"id={song_id} 的文件 {filename} 在 CDN 已不存在，已清除映射，将回退 API 解析")


# ---------------- 曲库 ----------------

def library_enabled():
    d = _config.get("library_dir") or ""
    return bool(d) and os.path.isdir(d)


def library_path(song_id):
    return os.path.join(_config["library_dir"], f"{song_id}.mp4")


def _nonempty_file(path):
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def in_library(song_id):
    if not library_enabled():
        return False
    p = library_path(song_id)
    return _nonempty_file(p)


def local_path_for(filename):
    """按文件名找本地文件：先缓存目录，再按映射查曲库；yt_{id}.mp4 是 YouTube 源歌曲的虚拟文件名"""
    m = YT_FILENAME_RE.match(filename)
    if m:
        return yt_local_path(int(m.group(1)))
    p = cache_path(filename)
    if _nonempty_file(p):
        return p
    with _mapping_lock:
        sid = _reverse_mapping.get(filename)
    if sid is not None and library_enabled():
        p = library_path(sid)
        if _nonempty_file(p):
            return p
    return None


def is_cached(filename):
    """本地可用（缓存目录或曲库）"""
    return local_path_for(filename) is not None


def target_path(filename, song_id=None):
    """新下载的保存位置：曲库模式且"其他视频永久保存"开启时按编号命名进曲库，
    否则进缓存目录按文件名命名"""
    if song_id is None:
        with _mapping_lock:
            song_id = _reverse_mapping.get(filename)
    if song_id is not None and library_enabled() and _config.get("cdn_permanent", False):
        return library_path(song_id)
    return cache_path(filename)


# ---------------- YouTube 源支持 ----------------

def song_route(song_num):
    """返回 (kind, payload)：('cdn', 文件名) / ('yt', YouTube地址) / (None, None) 未知"""
    with _mapping_lock:
        v = _mapping.get(song_num)
    if not v:
        return None, None
    if v.startswith("yt:"):
        return "yt", v[3:]
    return "cdn", v


_yt_online_logged = set()  # 已提示过"本地无文件走在线"的 YouTube 源歌曲


def yt_local_path(song_id):
    """YouTube 源歌曲的本地文件：曲库 {id}.mp4 / 旧版永久目录 yt/{id}.mp4 / 缓存 yt_{id}.mp4"""
    if in_library(song_id):
        return library_path(song_id)
    p = os.path.join(_config["cache_dir"], "yt", f"{song_id}.mp4")
    if _nonempty_file(p):
        return p
    p = cache_path(f"yt_{song_id}.mp4")
    if _nonempty_file(p):
        return p
    return None


def yt_target_path(song_id):
    """录流保存位置：曲库模式且"YouTube永久保存"开启 -> 曲库 {id}.mp4；否则进普通缓存（会被清理）"""
    if library_enabled() and _config.get("youtube_permanent", False):
        return library_path(song_id)
    return cache_path(f"yt_{song_id}.mp4")


# ---------------- WannaDance 源支持（api.udon.dance 302 -> play.udon.dance 文件） ----------------

def wd_cache_name(song_id):
    return f"wd_{song_id}.mp4"


def wd_library_enabled():
    d = _config.get("wd_library_dir") or ""
    return bool(d) and os.path.isdir(d)


def wd_library_path(song_id):
    """WannaDance 永久曲库独立目录，与 PyPyDance 曲库分开配置"""
    return os.path.join(_config.get("wd_library_dir") or "", wd_cache_name(song_id))


def wd_local_path(song_id):
    """WannaDance 本地文件：独立曲库 wd_{id}.mp4 优先，其次普通缓存目录"""
    if wd_library_enabled():
        p = wd_library_path(song_id)
        if _nonempty_file(p):
            return p
    p = cache_path(wd_cache_name(song_id))
    if _nonempty_file(p):
        return p
    return None


def wd_target_path(song_id):
    """WannaDance 新下载保存位置：独立曲库已设置且"WannaDance永久保存"开启时进曲库，否则进普通缓存"""
    if _config.get("wd_permanent", False) and (_config.get("wd_library_dir") or "").strip():
        target = wd_library_path(song_id)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        except OSError:
            return cache_path(wd_cache_name(song_id))
        return target
    return cache_path(wd_cache_name(song_id))


# ---------------- DuDu FitDance 源支持（HTTPS API -> global-cdn 签名视频） ----------------

DUDU_CACHE_RE = re.compile(r"^dudu_(\d+)\.mp4$")
DUDU_FILE_RE = re.compile(r"^(\d+)-[0-9a-f]+\.mp4$", re.IGNORECASE)


def dudu_cache_name(song_id):
    return f"dudu_{song_id}.mp4"


def dudu_library_enabled():
    directory = _config.get("dudu_library_dir") or ""
    return bool(directory) and os.path.isdir(directory)


def dudu_library_path(song_id):
    return os.path.join(_config.get("dudu_library_dir") or "", dudu_cache_name(song_id))


def dudu_local_path(song_id):
    if dudu_library_enabled():
        path = dudu_library_path(song_id)
        if _nonempty_file(path):
            return path
    path = cache_path(dudu_cache_name(song_id))
    return path if _nonempty_file(path) else None


def dudu_target_path(song_id):
    if _config.get("dudu_permanent") and dudu_library_enabled():
        return dudu_library_path(song_id)
    return cache_path(dudu_cache_name(song_id))


def dudu_api_request(path):
    ip = (_origin_ips.get(DUDU_API_HOST) or [None])[0]
    connection = _HTTPSConnectionIP(ip or DUDU_API_HOST, DUDU_API_HOST, 443, timeout=30,
                                    context=ssl.create_default_context())
    try:
        connection.request("GET", path, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        body = response.read(2 * 1048576 + 1)
        if len(body) > 2 * 1048576:
            raise RuntimeError("DuDu API 响应过大")
        return response.status, {key.lower(): value for key, value in response.getheaders()}, body
    finally:
        connection.close()


def dudu_video_path(song_id):
    status, headers, body = dudu_api_request(f"/api/v1/videos/{song_id}?cdn=jpn")
    if status in (301, 302, 303, 307, 308):
        location = headers.get("location") or ""
        parsed = urllib.parse.urlparse(location)
        if parsed.hostname != DUDU_PLAY_HOST:
            raise RuntimeError("DuDu 视频重定向到未知域名")
        return (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
    if status != 200:
        raise RuntimeError(f"DuDu 视频 API HTTP {status}")
    data = json.loads(body.decode("utf-8"))
    url = data.get("url") if isinstance(data, dict) else None
    if not url:
        raise RuntimeError("DuDu 视频 API 未返回视频地址")
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname != DUDU_PLAY_HOST:
        raise RuntimeError("DuDu 视频地址域名无效")
    return (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")


def download_dudu(song_id):
    target = dudu_target_path(song_id)
    if _nonempty_file(target):
        return True
    name = dudu_cache_name(song_id)
    run_id = _run_id
    meta = _begin_download(target, name, song_id, "dudu")
    if meta is None:
        return False
    part = target + ".part"
    ok = False
    error = None
    done = total = 0
    conn = None
    eof = False
    try:
        conn, response = open_upstream(DUDU_PLAY_HOST, dudu_video_path(song_id))
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        total = int(response.getheader("Content-Length") or 0)
        with open(part, "wb") as handle:
            while not _cancelled(run_id):
                chunk = _read_download_chunk(response)
                if not chunk:
                    eof = True
                    break
                handle.write(chunk)
                done += len(chunk)
                _note_upstream_progress(conn, len(chunk))
                emit_progress(name, done, total, meta["task_id"])
        if not _cancelled(run_id) and eof and done > 0 and (total == 0 or done == total):
            _commit_download(part, target, done)
            ok = True
            log(f"DuDu 缓存完成 {name} ({done / 1048576:.1f} MB)")
        elif not _cancelled(run_id):
            error = f"下载不完整 ({done}/{total})"
            log(f"{name}: {error}")
    except Exception as exc:
        error = str(exc)
        log(f"下载 {name} 出错: {error}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        cancelled = not ok and _cancelled(run_id)
        if not ok:
            _remove_part(part)
        _finish_download(meta, ok, cancelled, None if cancelled else error, done, total)
    return ok


def start_dudu_download(song_id):
    target = dudu_target_path(song_id)
    if _nonempty_file(target):
        return
    with _downloads_lock:
        if _canonical_path(target) in _downloads_in_progress:
            return
    _queue_serial_download("dudu", None, song_id)


# ---------------- 曲库浏览（GUI 曲库管理窗口） ----------------

WD_FAVORITES_FILENAME = "wanna-favorites.json"
WD_FAVORITE_RE = re.compile(r"\[VideoListManager\].*?\bWannaFavorite:(\d*(?:,\d+)*)")
_wd_favorites_lock = threading.Lock()
_wd_favorites = None


def wd_favorites_path():
    return os.path.join(data_dir(), WD_FAVORITES_FILENAME)


def _load_wd_favorites():
    global _wd_favorites
    if _wd_favorites is not None:
        return
    _wd_favorites = set()
    try:
        with open(wd_favorites_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            _wd_favorites = {int(song_id) for song_id in data if isinstance(song_id, int) and song_id > 0}
    except (OSError, ValueError, TypeError):
        pass


def get_wd_favorites():
    with _wd_favorites_lock:
        _load_wd_favorites()
        return set(_wd_favorites)


def _save_wd_favorites(song_ids):
    temporary = wd_favorites_path() + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(sorted(song_ids), handle)
    os.replace(temporary, wd_favorites_path())


def record_wd_favorites(lines):
    global _wd_favorites
    latest = None
    for line in lines:
        match = WD_FAVORITE_RE.search(line)
        if match:
            latest = match.group(1)
    if latest is None:
        return False
    song_ids = {int(value) for value in latest.split(",") if value.isdigit() and int(value) > 0}
    with _wd_favorites_lock:
        _load_wd_favorites()
        if song_ids == _wd_favorites:
            return False
        try:
            os.makedirs(data_dir(), exist_ok=True)
            _save_wd_favorites(song_ids)
        except OSError as error:
            log(f"WannaDance 收藏保存失败，将在读取新日志时重试: {error}")
            return False
        _wd_favorites = set(song_ids)
    emit("wd_favorites", ids=sorted(song_ids))
    return True


def refresh_wd_favorites():
    try:
        candidates = [os.path.join(VRC_LOG_DIR, name) for name in os.listdir(VRC_LOG_DIR)
                      if name.startswith("output_log") and name.endswith(".txt")]
    except OSError:
        return False
    for path in sorted(candidates, key=os.path.getmtime, reverse=True):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 2 * 1024 * 1024))
                lines = handle.read().splitlines()
        except OSError:
            continue
        # An unchanged (including empty) latest snapshot is still authoritative.
        # Do not continue into an older session and restore its stale favorites.
        if any(WD_FAVORITE_RE.search(line) for line in lines):
            return record_wd_favorites(lines)
    return False


NUMERIC_MP4_RE = re.compile(r"^(\d+)\.mp4$")  # PyPyDance 曲库命名（编号即文件名）


WD_CATALOG_CACHE_FILENAME = "wannadance-catalog.json"
WD_CATALOG_CACHE_SECONDS = 86400
_wd_catalog_retry_at = 0.0


def _read_wd_catalog(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    catalog = {}
    for key, item in data.items():
        try:
            sid = int(key)
        except (TypeError, ValueError):
            continue
        if sid > 0 and isinstance(item, dict):
            catalog[sid] = item
    return catalog or None


def _wd_catalog():
    """优先读曲库目录的清单，其次读软件数据目录中的官方清单缓存。"""
    with _config_lock:
        directory = _config.get("wd_library_dir") or ""
    if directory:
        local = _read_wd_catalog(os.path.join(directory, "wannadance_catalog.json"))
        if local is not None:
            return local
    return _read_wd_catalog(os.path.join(data_dir(), WD_CATALOG_CACHE_FILENAME))


def _fetch_wd_catalog():
    request = urllib.request.Request(
        "http://api.udon.dance/Api/Songs/list",
        headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
    with _tracked_urlopen(request, timeout=20) as response:
        if getattr(response, "status", 200) != 200:
            raise RuntimeError(f"WannaDance 清单 HTTP {response.status}")
        raw = response.read(20 * 1048576 + 1)
    if len(raw) > 20 * 1048576:
        raise ValueError("WannaDance 清单过大")
    data = json.loads(raw.decode("utf-8"))
    catalog = {}
    groups = data.get("groups") if isinstance(data, dict) else None
    contents = groups.get("contents") if isinstance(groups, dict) else None
    if not isinstance(contents, list):
        raise ValueError("WannaDance 清单格式无效")
    for group in contents:
        if not isinstance(group, dict):
            continue
        for song in group.get("songInfos") or []:
            if not isinstance(song, dict):
                continue
            sid = song.get("id")
            if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0:
                catalog[sid] = {"group": group.get("groupName"), **song}
    if not catalog:
        raise ValueError("WannaDance 清单为空")
    try:
        base = data_dir()
        os.makedirs(base, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="wd-catalog-", suffix=".tmp", dir=base)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(catalog, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, os.path.join(base, WD_CATALOG_CACHE_FILENAME))
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    except OSError as error:
        log(f"WannaDance 清单缓存保存失败: {error}")
    log(f"WannaDance 官方清单已更新：{len(catalog)} 首")
    return catalog


def _ensure_wd_catalog():
    global _wd_catalog_retry_at
    catalog = _wd_catalog()
    with _config_lock:
        directory = _config.get("wd_library_dir") or ""
    if directory and os.path.isfile(os.path.join(directory, "wannadance_catalog.json")):
        return catalog
    cache_path = os.path.join(data_dir(), WD_CATALOG_CACHE_FILENAME)
    try:
        if catalog is not None and time.time() - os.path.getmtime(cache_path) < WD_CATALOG_CACHE_SECONDS:
            return catalog
    except OSError:
        pass
    if time.time() < _wd_catalog_retry_at:
        return catalog
    try:
        return _fetch_wd_catalog()
    except Exception as error:
        _wd_catalog_retry_at = time.time() + 60
        log(f"WannaDance 官方清单获取失败: {error}" + ("，继续使用旧缓存" if catalog else ""))
        return catalog


DUDU_CATALOG_CACHE_FILENAME = "dudu-catalog.json"
DUDU_CATALOG_CACHE_SECONDS = 86400
_dudu_catalog_retry_at = 0.0


def _read_dudu_catalog():
    try:
        with open(os.path.join(data_dir(), DUDU_CATALOG_CACHE_FILENAME), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    catalog = {}
    for song in data.get("videos", []) if isinstance(data, dict) else []:
        if not isinstance(song, dict):
            continue
        sid = song.get("id")
        if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0:
            catalog[sid] = song
    return catalog or None


def _fetch_dudu_catalog():
    status, _, body = dudu_api_request("/api/v1/videos?cdn=jpn")
    if status != 200:
        raise RuntimeError(f"DuDu 清单 HTTP {status}")
    data = json.loads(body.decode("utf-8"))
    videos = data.get("videos") if isinstance(data, dict) else None
    if not isinstance(videos, list) or not videos:
        raise ValueError("DuDu 清单为空或格式无效")
    catalog = {}
    for song in videos:
        if not isinstance(song, dict):
            continue
        sid = song.get("id")
        if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0:
            catalog[sid] = song
    if not catalog:
        raise ValueError("DuDu 清单没有有效歌曲")
    base = data_dir()
    os.makedirs(base, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="dudu-catalog-", suffix=".tmp", dir=base)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, os.path.join(base, DUDU_CATALOG_CACHE_FILENAME))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    log(f"DuDu 官方清单已更新：{len(catalog)} 首")
    return catalog


def _ensure_dudu_catalog():
    global _dudu_catalog_retry_at
    catalog = _read_dudu_catalog()
    path = os.path.join(data_dir(), DUDU_CATALOG_CACHE_FILENAME)
    try:
        if catalog is not None and time.time() - os.path.getmtime(path) < DUDU_CATALOG_CACHE_SECONDS:
            return catalog
    except OSError:
        pass
    if time.time() < _dudu_catalog_retry_at:
        return catalog
    try:
        return _fetch_dudu_catalog()
    except Exception as error:
        _dudu_catalog_retry_at = time.time() + 60
        log(f"DuDu 官方清单获取失败: {error}" + ("，继续使用旧缓存" if catalog else ""))
        return catalog


def scan_library(kind):
    """扫描曲库目录。kind: "pypy" | "wd" | "dudu"。未配置目录返回 None，否则返回
    {"dir", "kind", "songs": [{id, name, size, mtime}], "parts", "total_bytes"}。
    .part 只计数不列入；歌名分别读取 PyPyDance 直取表或对应官方清单。"""
    with _config_lock:
        if kind == "dudu":
            directory = _config.get("dudu_library_dir") or ""
            pattern = DUDU_CACHE_RE
        elif kind == "wd":
            directory = _config.get("wd_library_dir") or ""
            pattern = WD_CACHE_RE
        else:
            directory = _config.get("library_dir") or ""
            pattern = NUMERIC_MP4_RE
    if not directory or not os.path.isdir(directory):
        return None
    if kind == "wd":
        refresh_wd_favorites()
    favorite_ids = get_wd_favorites() if kind == "wd" else set()
    names = {}
    if kind == "dudu":
        catalog = _ensure_dudu_catalog()
        if catalog:
            for sid, item in catalog.items():
                nm = str(item.get("name") or "").strip()
                ar = str(item.get("artist") or "").strip()
                label = f"{nm} - {ar}" if ar and ar != nm else nm
                if label:
                    names[sid] = label
    elif kind == "wd":
        catalog = _wd_catalog()
        if catalog:
            for sid, item in catalog.items():
                nm = str(item.get("name") or "").strip()
                ar = str(item.get("artist") or "").strip()
                label = f"{nm} - {ar}" if ar and ar != nm else nm
                if label:
                    names[sid] = label
    else:
        names = bundle_song_names()  # PyPyDance 歌名来自官方歌单直取表
    songs = []
    parts = 0
    total = 0
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name.endswith(".part"):
                    parts += 1
                    continue
                m = pattern.match(entry.name)
                if not m:
                    continue
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                sid = int(m.group(1))
                songs.append({"id": sid, "name": names.get(sid),
                              "size": st.st_size, "mtime": st.st_mtime,
                              "favorite": kind == "wd" and sid in favorite_ids})
                total += st.st_size
    except OSError:
        return None
    songs.sort(key=lambda s: s["id"])
    return {"dir": directory, "kind": kind, "songs": songs,
            "parts": parts, "total_bytes": total}


def index_maintenance_status():
    scan = scan_library("pypy")
    if scan is None:
        return {"needed": False, "missing": 0, "total": 0, "dir": "", "mapping": mapping_path()}
    with _mapping_lock:
        missing = sum(not _mapping.get(song["id"]) for song in scan["songs"])
    return {"needed": missing > 0, "missing": missing, "total": len(scan["songs"]),
            "dir": scan["dir"], "mapping": mapping_path()}


def library_missing(kind):
    """对照官方清单找曲库里缺的编号。返回 None（目录未设置）或
    {"source": 清单描述或 None, "expected", "missing": [id], "dead": [id],
     "names": {id: 歌名}, "dir"}。
    pypy 对照完整官方歌单（_bundle_ids），dead 是历史 404 确认的下架歌曲；
    wd 对照 wannadance_catalog.json（下架曲目已被清单排除）。"""
    scan = scan_library(kind)
    if scan is None:
        return None
    have = {s["id"] for s in scan["songs"]}
    favorites = sorted(get_wd_favorites()) if kind == "wd" else []
    names = {}
    dead = []
    if kind == "dudu":
        catalog = _ensure_dudu_catalog()
        if catalog is None:
            return {"source": None, "expected": 0, "missing": [], "dead": [],
                    "names": {}, "dir": scan["dir"], "kind": kind, "favorites": favorites}
        expected = set(catalog)
        source = "DuDu 官方清单"
        for sid in expected - have:
            item = catalog.get(sid) or {}
            nm = str(item.get("name") or "").strip()
            ar = str(item.get("artist") or "").strip()
            label = f"{nm} - {ar}" if ar and ar != nm else nm
            if label:
                names[sid] = label
    elif kind == "wd":
        catalog = _ensure_wd_catalog()
        if catalog is None:
            return {"source": None, "expected": 0, "missing": [], "dead": [],
                    "names": {}, "dir": scan["dir"], "kind": kind, "favorites": favorites}
        expected = {sid for sid, item in catalog.items() if not item.get("disable_public")}
        source = "WannaDance 官方清单"
        for sid in expected - have:
            item = catalog.get(sid) or {}
            nm = str(item.get("name") or "").strip()
            ar = str(item.get("artist") or "").strip()
            label = f"{nm} - {ar}" if ar and ar != nm else nm
            if label:
                names[sid] = label
    else:
        with _bundle_lock:
            ids = set(_bundle_ids)
        if not ids:
            load_bundle_cache()
            with _bundle_lock:
                ids = set(_bundle_ids)
        if not ids:
            return {"source": None, "expected": 0, "missing": [], "dead": [],
                    "names": {}, "dir": scan["dir"], "kind": kind, "favorites": favorites}
        expected = ids
        source = "官方歌单"
        _ensure_dead_loaded()
        with _bundle_lock:
            name_table = dict(_bundle_names)
        lacking = expected - have
        dead = sorted(lacking & _bundle_miss)
        for sid in lacking:
            nm = name_table.get(sid)
            if nm:
                names[sid] = nm
    return {"source": source, "expected": len(expected),
            "missing": sorted(expected - have), "dead": dead,
            "names": names, "dir": scan["dir"], "kind": kind, "favorites": favorites}


def delete_library_songs(kind, ids):
    """从曲库目录永久删除指定编号的 mp4（不进回收站）。
    服务运行中或曲库维护任务（重建/校验）进行中拒绝。返回 {"deleted", "failed", "freed"}。"""
    with _config_lock:
        if kind == "dudu":
            directory = _config.get("dudu_library_dir") or ""
            name_for = dudu_cache_name
        elif kind == "wd":
            directory = _config.get("wd_library_dir") or ""
            name_for = wd_cache_name
        else:
            directory = _config.get("library_dir") or ""
            name_for = lambda sid: f"{sid}.mp4"
    if not directory or not os.path.isdir(directory):
        raise RuntimeError("曲库目录未设置或不存在")
    if is_running():
        raise RuntimeError("服务运行中，请先停止服务再删除曲库文件")
    if rebuilding() or verifying():
        raise RuntimeError("曲库任务进行中，请等待完成或取消后再删除")
    deleted = 0
    freed = 0
    failed = []
    for sid in ids:
        sid = int(sid)
        path = os.path.join(directory, name_for(sid))
        try:
            size = os.path.getsize(path)
            os.remove(path)
            deleted += 1
            freed += size
        except FileNotFoundError:
            failed.append((sid, "文件不存在"))
        except OSError as e:
            failed.append((sid, str(e)))
    log(f"曲库浏览：永久删除 {deleted} 个文件，释放 {freed / 1048576:.1f} MB"
        + (f"，失败 {len(failed)} 个" if failed else ""))
    return {"deleted": deleted, "failed": failed, "freed": freed}


def learn_mapping_from_redirect(song_num, loc):
    """从 /video 的 302 Location 学习映射：CDN 记文件名，YouTube 记 yt:地址"""
    u = urllib.parse.urlparse(loc)
    hn = (u.hostname or "").lower()
    if hn == "cdn.pypy.dance" or hn.endswith(".cdn.pypy.dance"):
        name = os.path.basename(u.path)
        if FILENAME_RE.match(name):
            mark_mapping(song_num, name)
            log(f"学习到映射 id={song_num} -> {name}")
    elif "youtube.com" in hn or "youtu.be" in hn:
        mark_mapping(song_num, "yt:" + loc)
        log(f"学习到 YouTube 源 id={song_num}: {loc}")


def api_request(path):
    """向真实 api.pypy.dance 发起 HTTP 请求（走真实 IP，绕过本地劫持）。
    返回 (status, {小写头名: 值}, body)"""
    ip = _api_ip()
    conn = _HTTPConnectionIP(ip or API_HOST, API_HOST, 80, timeout=30)
    try:
        conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
        resp = conn.getresponse()
        body = resp.read()
        hdrs = {}
        for k, v in resp.getheaders():
            hdrs.setdefault(k.lower(), v)
        return resp.status, hdrs, body
    finally:
        conn.close()


def get_cache_stats():
    count = total = lib_count = lib_total = wd_count = wd_total = dudu_count = dudu_total = 0
    try:
        for name in os.listdir(_config["cache_dir"]):
            p = os.path.join(_config["cache_dir"], name)
            if name.endswith(".mp4") and os.path.isfile(p):
                count += 1
                total += os.path.getsize(p)
    except Exception:
        pass
    if library_enabled():
        try:
            for name in os.listdir(_config["library_dir"]):
                p = os.path.join(_config["library_dir"], name)
                if name.endswith(".mp4") and os.path.isfile(p):
                    lib_count += 1
                    lib_total += os.path.getsize(p)
        except Exception:
            pass
    if wd_library_enabled():
        try:
            wd_dir = _config["wd_library_dir"]
            for name in os.listdir(wd_dir):
                p = os.path.join(wd_dir, name)
                if WD_CACHE_RE.match(name) and os.path.isfile(p):
                    wd_count += 1
                    wd_total += os.path.getsize(p)
        except Exception:
            pass
    if dudu_library_enabled():
        try:
            directory = _config["dudu_library_dir"]
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if DUDU_CACHE_RE.match(name) and os.path.isfile(path):
                    dudu_count += 1
                    dudu_total += os.path.getsize(path)
        except Exception:
            pass
    return count, total, lib_count, lib_total, wd_count, wd_total, dudu_count, dudu_total


def emit_stats():
    count, total, lib_count, lib_total, wd_count, wd_total, dudu_count, dudu_total = get_cache_stats()
    emit("stats", count=count, total_mb=total / 1048576, limit_mb=int(_config["max_cache_mb"]),
         lib_count=lib_count, lib_mb=lib_total / 1048576, lib_enabled=library_enabled(),
         wd_count=wd_count, wd_mb=wd_total / 1048576, dudu_count=dudu_count, dudu_mb=dudu_total / 1048576)


def clear_cache():
    """只清理临时缓存，保护在途目标和 .part，并报告实际结果。"""
    result = {"removed_count": 0, "removed_bytes": 0, "skipped_busy": 0,
              "failed_count": 0, "errors": []}
    with _downloads_lock:
        directory = _config["cache_dir"]
        if (_same_directory(directory, _config.get("library_dir")) or _same_directory(directory, _config.get("wd_library_dir"))
                or _same_directory(directory, _config.get("dudu_library_dir"))):
            raise ValueError("缓存目录与永久曲库相同，已拒绝清理以保护曲库")
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            names = []
        for name in names:
            path = os.path.join(directory, name)
            if not name.endswith((".mp4", ".part")) or not os.path.isfile(path):
                continue
            target = path[:-5] if path.endswith(".part") else path
            if _canonical_path(target) in _downloads_in_progress:
                result["skipped_busy"] += 1
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
                result["removed_count"] += 1
                result["removed_bytes"] += size
            except OSError as e:
                result["failed_count"] += 1
                result["errors"].append({"filename": name, "error": str(e)})
    log(f"缓存清理完成：删除 {result['removed_count']} 个，正在下载跳过 {result['skipped_busy']} 个，"
        f"失败 {result['failed_count']} 个（永久曲库保留）")
    emit_stats()
    refresh_queue_state()
    emit("cache_cleared", **result)
    return result


# ---------------- 管理员权限 ----------------

def ensure_admin():
    if os.environ.get("DANCE_CACHE_NO_ADMIN"):
        return True  # 测试用：跳过提权
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        pass
    log("需要管理员权限，正在请求提权...")
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe = sys.executable
        args = " ".join(f'"{a}"' for a in sys.argv)
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, SCRIPT_DIR, 1)
    if ret <= 32:
        log(f"提权失败(错误码 {ret})，请右键以管理员身份运行。")
        sys.exit(1)
    sys.exit(0)


# ---------------- DNS 解析（劫持前取真实 IP） ----------------

def resolve_real_ips():
    _origin_ips.clear()
    for host in HIJACK_HOSTS + [UDON_API_HOST, UDON_PLAY_HOST, DUDU_API_HOST, DUDU_PLAY_HOST]:
        ips = []
        try:
            _, _, ips = __import__("socket").gethostbyname_ex(host)
        except Exception:
            pass
        ips = [ip for ip in ips if _is_public_ip(ip)]
        if not ips:
            ips = [ip for ip in doh_lookup(host) if _is_public_ip(ip)]
        if ips:
            _origin_ips[host] = ips
            log(f"解析 {host} -> {', '.join(ips)}")
        else:
            log(f"警告: 无法解析 {host}，该域名回源将不可用")


DOH_PROVIDERS = (
    "https://dns.alidns.com/resolve",
    "https://dns.google/resolve",
    "https://cloudflare-dns.com/dns-query",
)


def _doh_query(doh, host, timeout=8):
    conn = None
    try:
        ctx = ssl.create_default_context()
        u = urllib.parse.urlparse(doh)
        conn = _HTTPSConnectionIP(u.hostname, u.hostname, 443, timeout=timeout, context=ctx)
        conn.request("GET", f"{u.path}?name={host}&type=A", headers={"Accept": "application/dns-json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8", "replace"))
        return [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def doh_lookup(host):
    for doh in DOH_PROVIDERS:
        ips = _doh_query(doh, host)
        if ips:
            return ips
    return []


def _is_public_ip(ip):
    """是否公网 IP（过滤 hosts 劫持产生的 127.0.0.1 等非公网结果）"""
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def gather_candidate_ips(host, rounds=2):
    """系统 DNS + 多个 DoH 服务器多轮查询，合并去重候选公网 IP（不同递归出口常被分配到不同节点组）"""
    ips = []
    try:
        _, _, sys_ips = socket.gethostbyname_ex(host)
        ips.extend(sys_ips)
    except Exception:
        pass
    for _ in range(rounds):
        for doh in DOH_PROVIDERS:
            ips.extend(_doh_query(doh, host, timeout=4))
    seen = set()
    out = []
    for ip in ips:
        if ip not in seen and _is_public_ip(ip):
            seen.add(ip)
            out.append(ip)
    return out


# ---------------- hosts 管理 ----------------

def _hosts_error_reason(e):
    """根据身份和错误码给出 hosts 写入失败的具体原因"""
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    if not admin:
        return "没有管理员权限，请以管理员身份运行"
    if getattr(e, "winerror", None) == 32:
        return "hosts 文件正被其他程序占用，请关闭加速器/安全软件后重试"
    return ("hosts 文件被锁定或设为只读（可能是安全软件/加速器的防护功能），"
            "请关闭相关防护后重试，或先启动本软件再开加速器")


def count_stale_hosts():
    """只读统计 hosts 里带本程序标记的条目数（不修改文件）。
    服务未运行时这些条目就是异常退出（强杀、断电）留下的残留。"""
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if HOSTS_MARK in line)
    except OSError:
        return 0


def clean_stale_hosts():
    """删除 hosts 里带本程序标记的条目，返回清理条数。"""
    with _hosts_lock:
        try:
            with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            kept = [l for l in lines if HOSTS_MARK not in l]
            removed = len(lines) - len(kept)
            if removed:
                with open(HOSTS_PATH, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                log("已清理上次残留的 hosts 记录")
            return removed
        except FileNotFoundError:
            return 0
        except PermissionError as e:
            log(f"错误: hosts 修改失败（{_hosts_error_reason(e)}）")
            raise


def hijack_hosts(include_api=False):
    with _hosts_lock:
        clean_stale_hosts()
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        with open(HOSTS_PATH + ".dance-cache.bak", "w", encoding="utf-8") as f:
            f.write(content)
        hosts = hijack_host_list(include_api)
        with open(HOSTS_PATH, "a", encoding="utf-8") as f:
            if not content.endswith("\n"):
                f.write("\n")
            for host in hosts:
                f.write(f"127.0.0.1 {host} {HOSTS_MARK}\n")
    log("hosts 已劫持: " + ", ".join(hosts))


def restore_hosts():
    try:
        clean_stale_hosts()
        log("hosts 已恢复")
        return True
    except OSError as e:
        log(f"hosts 恢复失败: {e}")
        return False


def _hosts_watchdog_loop(include_api):
    """运行期间定期检查 hosts 劫持条目，被外部清除（如加速器恢复备份）时自动重写"""
    expected = hijack_host_list(include_api)
    while not _stop_event.wait(15):
        try:
            # 与最终恢复串行，防止停止期间在恢复后重新写回劫持条目。
            with _hosts_lock:
                if _stop_event.is_set():
                    return
                with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                marked = [l for l in lines if HOSTS_MARK in l]
                missing = [h for h in expected if not any(h in l.split() for l in marked)]
                if not missing:
                    continue
                kept = [l for l in lines if HOSTS_MARK not in l]
                if kept and not kept[-1].endswith("\n"):
                    kept[-1] += "\n"
                for h in expected:
                    kept.append(f"127.0.0.1 {h} {HOSTS_MARK}\n")
                if _stop_event.is_set():
                    return
                with open(HOSTS_PATH, "w", encoding="utf-8") as f:
                    f.writelines(kept)
            log(f"检测到 hosts 条目被清除（可能是加速器恢复备份），已自动重新写入: {', '.join(missing)}")
        except PermissionError as e:
            log(f"hosts 自动重写失败（{_hosts_error_reason(e)}）")
        except Exception as e:
            log(f"hosts 看门狗异常: {e}")


# ---------------- 源站请求（绕过劫持，用真实 IP + Host 头） ----------------

def _parse_retry_after(value, default):
    if value and value.strip().isdigit():
        return float(value)
    return default


def ordered_ips(host):
    """cdn.pypy.dance / play.udon.dance 优先使用用户设置的优选 IP（同为 Cloudflare，一个 IP 两家通用），
    失效时自动回落到默认解析结果。
    注意实际下载会被 301 到带哈希的子域名（如 806bb815.cdn.pypy.dance），优选 IP 必须对子域名同样生效，
    否则真实下载永远走系统 DNS 分配的节点，优选形同虚设。
    子域名（韩国源站直连，通常只有一个 IP）额外附上主域名的 Cloudflare 节点做兜底：
    源站限流(429)/故障时自动改用 CF 节点访问子域名（HTTP 下 CF 可为子域服务）。"""
    ips = list(_origin_ips.get(host, []))
    pref = (_config.get("preferred_cdn_ip") or "").strip()
    is_cdn = host == UDON_PLAY_HOST or host == "cdn.pypy.dance" or host.endswith(".cdn.pypy.dance")
    if pref and _is_public_ip(pref) and is_cdn:
        ips = [pref] + [ip for ip in ips if ip != pref]
    if host.endswith(".cdn.pypy.dance"):
        for ip in _origin_ips.get("cdn.pypy.dance", []):
            if ip not in ips:
                ips.append(ip)
    return ips


def _open_upstream_once(host, path, extra_headers=None, redirects=3):
    """返回 (conn, resp)。手动跟随重定向，目标 host 必须已解析真实 IP。"""
    cur_host, cur_path = host, path
    for _ in range(redirects + 1):
        ips = ordered_ips(cur_host)
        if not ips:
            raise RuntimeError(f"没有 {cur_host} 的真实 IP")
        last_err = None
        last_transient = None
        redirected = False
        for ip in ips:
            conn = None
            try:
                if cur_host in (DUDU_API_HOST, DUDU_PLAY_HOST):
                    conn = _HTTPSConnectionIP(ip, cur_host, 443, timeout=30,
                                              context=ssl.create_default_context())
                else:
                    conn = _HTTPConnectionIP(ip, cur_host, 80, timeout=30)
                headers = {"Host": cur_host, "User-Agent": "stagefright/1.2 (Linux;Android 10)", "Accept-Encoding": "identity"}
                if extra_headers:
                    headers.update(extra_headers)
                conn.request("GET", cur_path, headers=headers)
                resp = conn.getresponse()
                if resp.status in RETRYABLE_STATUS:
                    ra = _parse_retry_after(resp.getheader("Retry-After"), 0.0)
                    resp.read()
                    conn.close()
                    raise TransientUpstream(resp.status, ra)
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.getheader("Location") or ""
                    resp.read()
                    conn.close()
                    u = urllib.parse.urlparse(loc)
                    cur_host = u.hostname
                    cur_path = (u.path or "/") + (("?" + u.query) if u.query else "")
                    if cur_host not in _origin_ips:
                        raise RuntimeError(f"重定向到未支持的域名: {cur_host}")
                    redirected = True
                    break
                return conn, resp
            except TransientUpstream as e:
                last_transient = e  # 该 IP 被限流/临时故障，先换下一个 IP 试试
                continue
            except RuntimeError:
                raise
            except Exception as e:
                last_err = e
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                continue
        if not redirected:
            if last_transient is not None:
                raise last_transient
            raise RuntimeError(f"连接 {cur_host} 失败: {last_err}")
    raise RuntimeError("重定向次数过多")


def open_upstream(host, path, extra_headers=None, redirects=3, retries=5):
    """带限流/临时错误退避重试的源站请求"""
    last_err = None
    for attempt in range(retries):
        wait_rate_limit()
        if _stop_event.is_set():
            raise RuntimeError("已停止")
        try:
            return _open_upstream_once(host, path, extra_headers, redirects)
        except TransientUpstream as e:
            last_err = e
            backoff = max(e.retry_after, min(15 * (attempt + 1), 120))
            if e.status == 429:
                note_rate_limited(backoff)
                log(f"源站限流(429)，{backoff:.0f} 秒后重试 ({attempt + 1}/{retries})")
            else:
                log(f"源站临时错误 HTTP {e.status}，{backoff:.0f} 秒后重试 ({attempt + 1}/{retries})")
                _stop_event.wait(backoff)
        except Exception as e:
            last_err = e
            log(f"源站请求异常: {e}，稍后重试 ({attempt + 1}/{retries})")
            _stop_event.wait(min(3 * (attempt + 1), 15))
    raise RuntimeError(f"重试 {retries} 次后仍失败: {last_err}")


_active_connections = set()
_connections_lock = threading.Lock()


def _register_connection(conn):
    with _connections_lock:
        _active_connections.add(conn)


def _unregister_connection(conn):
    with _connections_lock:
        _active_connections.discard(conn)


def close_active_connections():
    with _connections_lock:
        connections = list(_active_connections)
    for conn in connections:
        try:
            sock = getattr(conn, "sock", None)
            if sock is None:
                raw = getattr(getattr(conn, "fp", None), "raw", None)
                sock = getattr(raw, "_sock", None)
            forced = False
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
                try:
                    descriptor = sock.detach()
                    if not isinstance(descriptor, int):
                        raise TypeError("无效 socket 描述符")
                    forced = True
                    socket.socket(fileno=descriptor).close()
                except (OSError, AttributeError, TypeError):
                    pass
            # Windows 上跨线程关闭 HTTPResponse 会等待读取锁；底层 socket 已脱离时由读取方自行收尾。
            if not forced:
                conn.close()
        except Exception:
            pass
        finally:
            _unregister_connection(conn)


@contextmanager
def _tracked_urlopen(request, timeout):
    response = urllib.request.urlopen(request, timeout=timeout)
    _register_connection(response)
    try:
        yield response
    finally:
        try:
            response.close()
        finally:
            _unregister_connection(response)


class _HTTPConnectionIP(http.client.HTTPConnection):
    """连接真实 IP，但 Host 头仍用域名（api 被 hosts 劫持后必须绕开本地回环）"""

    def __init__(self, ip, host=API_HOST, port=80, **kw):
        self._ip = ip
        self._selected_ip = ip
        super().__init__(host, port, **kw)
        _register_connection(self)

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout, self.source_address)

    def close(self):
        try:
            super().close()
        finally:
            _unregister_connection(self)


class _HTTPSConnectionIP(http.client.HTTPSConnection):
    """连接真实 IP，TLS 握手/SNI 仍用域名，证书校验不受影响"""

    def __init__(self, ip, host=API_HOST, port=443, **kw):
        self._ip = ip
        self._selected_ip = ip
        super().__init__(host, port, **kw)
        _register_connection(self)

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

    def close(self):
        try:
            super().close()
        finally:
            _unregister_connection(self)


def _api_ip():
    ips = _origin_ips.get(API_HOST) or []
    return ips[0] if ips else None


_api_conns = threading.local()


def _api_conn():
    """线程本地复用的 API 连接。api.pypy.dance 支持 HTTP 直连，省去 TLS 握手；
    若 HTTP 失败则自动降级为 HTTPS。一律走真实 IP，避免劫持后回环到自己。"""
    conn = getattr(_api_conns, "conn", None)
    if conn is None:
        ip = _api_ip()
        if getattr(_api_conns, "use_https", False):
            ctx = ssl.create_default_context()
            conn = _HTTPSConnectionIP(ip or API_HOST, API_HOST, 443, timeout=15, context=ctx)
        else:
            conn = _HTTPConnectionIP(ip or API_HOST, API_HOST, 80, timeout=15)
        _api_conns.conn = conn
    _register_connection(conn)
    return conn


def resolve_video_filename(song_num):
    """api.pypy.dance/video?id=N -> 302 -> 提取 CDN 文件名；YouTube 源返回 None。
    429/网络错误会退避重试；失败时不写入映射（避免把限流误判成非CDN源）。
    旧版本标记的 ""（非CDN源）会重新解析一次，以便学习 YouTube 源地址。
    得到答复但没拿到文件名（临时异常/意外状态码）时不永久缓存：记录退避时间，
    过 RESOLVE_RETRY_DELAY 秒允许重试，避免一次故障把这首歌卡死在"待解析"。
    歌单直取开启时，无映射或已知 YouTube 源的歌曲先尝试用歌单 ytid 直取 CDN
    （yt 源在 CDN 常有同名文件，命中即升级为本地缓存，并绕过 API 跳转与限流）。"""
    if _config.get("bundle_direct", True):
        with _mapping_lock:
            v0 = _mapping.get(song_num)
        if not v0 or v0.startswith("yt:"):
            fn = _bundle_probe(song_num)
            if fn:
                return fn
    with _resolved_lock:
        if song_num in _resolved_ids:
            return _resolved_ids[song_num]
    with _mapping_lock:
        v = _mapping.get(song_num)
    if v:
        fn = None if v.startswith("yt:") else v
        with _resolved_lock:
            _resolved_ids[song_num] = fn
        return fn
    with _resolved_lock:
        if time.time() < _resolve_retry_at.get(song_num, 0.0):
            return None
    log(f"解析视频地址 id={song_num}...")
    filename = None
    answered = False
    retries = 4
    for attempt in range(retries):
        wait_rate_limit()
        if _stop_event.is_set():
            return None
        try:
            conn = _api_conn()
            conn.request("GET", f"/video?id={song_num}", headers={"User-Agent": "Mozilla/5.0"})
            resp = conn.getresponse()
            status = resp.status
            loc = resp.getheader("Location") or ""
            ra = _parse_retry_after(resp.getheader("Retry-After"), 0.0)
            resp.read()
        except Exception as e:
            old_conn = getattr(_api_conns, "conn", None)
            _api_conns.conn = None  # 连接可能已失效，丢弃后重建
            if old_conn is not None and not getattr(_api_conns, "use_https", False):
                _api_conns.use_https = True  # HTTP 失败，降级为 HTTPS 重试
            log(f"解析视频地址失败 id={song_num}: {e} ({attempt + 1}/{retries})")
            _stop_event.wait(min(3 * (attempt + 1), 15))
            continue
        if status in RETRYABLE_STATUS:
            backoff = max(ra, min(20 * (attempt + 1), 120))
            note_rate_limited(backoff)
            log(f"API 限流 HTTP {status} id={song_num}，{backoff:.0f} 秒后重试 ({attempt + 1}/{retries})")
            continue
        answered = True
        if status in (301, 302, 303, 307, 308):
            u = urllib.parse.urlparse(loc)
            hn = (u.hostname or "").lower()
            if hn == "cdn.pypy.dance" or hn.endswith(".cdn.pypy.dance"):
                name = os.path.basename(u.path)
                if FILENAME_RE.match(name):
                    filename = name
                mark_mapping(song_num, filename or "")
            elif "youtube.com" in hn or "youtu.be" in hn:
                mark_mapping(song_num, "yt:" + loc)
                log(f"歌曲 id={song_num} 为 YouTube 源: {loc}")
            else:
                mark_mapping(song_num, "")
        else:
            log(f"解析视频地址 id={song_num} 返回 HTTP {status}")
        break
    if answered:
        # 拿到文件名的结果本会话内不再重复探测；没拿到文件名的答复
        # 可能是临时故障，退避一段时间后允许重试
        with _resolved_lock:
            if filename is not None:
                _resolved_ids[song_num] = filename
            else:
                _resolve_retry_at[song_num] = time.time() + RESOLVE_RETRY_DELAY
    return filename


# ---------------- 下载 ----------------

_progress_last = {}


def emit_progress(filename, done, total, task_id=None):
    now = time.time()
    key = task_id or filename
    with _downloads_lock:
        meta = _download_meta.get(task_id)
        if meta is not None:
            meta.update(done=done, total=total)
        data = dict(meta or {"filename": filename, "task_id": task_id})
        should_emit = (total > 0 and done >= total) or now - _progress_last.get(key, 0) >= 0.5
        if should_emit:
            _progress_last[key] = now
    if should_emit:
        data.update(filename=filename, done=done, total=total)
        emit("progress", **data)


def _cancelled(run_id):
    return _stop_event.is_set() or run_id != _run_id


def _storage_label(path):
    """文件落在永久曲库（PyPyDance 或 WannaDance）返回 library，否则 cache"""
    dirname = os.path.dirname(path)
    if (_same_directory(dirname, _config.get("library_dir")) or _same_directory(dirname, _config.get("wd_library_dir"))
            or _same_directory(dirname, _config.get("dudu_library_dir"))):
        return "library"
    return "cache"


def _begin_download(target, filename, song_id=None, source="cdn"):
    key = _canonical_path(target)
    if song_id is None:
        with _mapping_lock:
            song_id = _reverse_mapping.get(filename)
    meta = {
        "task_id": key, "filename": filename, "song_id": song_id,
        "source": source, "target_path": target,
        "storage": _storage_label(target),
        "done": 0, "total": 0,
    }
    with _downloads_lock:
        if _stop_event.is_set() or key in _downloads_in_progress:
            return None
        _downloads_in_progress[key] = threading.current_thread()
        _download_meta[key] = dict(meta)
        _download_results.pop(key, None)
    emit("download_start", **meta)
    refresh_queue_state()
    return meta


def get_downloads_snapshot():
    with _downloads_lock:
        return copy.deepcopy(list(_download_meta.values()))


def _finish_download(meta, ok, cancelled, error, done, total):
    data = {**meta, "ok": bool(ok), "cancelled": bool(cancelled),
            "error": error, "done": done, "total": total}
    key = meta["task_id"]
    with _downloads_lock:
        _downloads_in_progress.pop(key, None)
        _download_meta.pop(key, None)
        _progress_last.pop(key, None)
        _download_results[key] = dict(data)
        while len(_download_results) > 200:
            _download_results.pop(next(iter(_download_results)))
    cleanup_cache()
    emit("download_done", **data)
    emit_stats()


def _commit_download(part, target, size):
    try:
        os.replace(part, target)
    except OSError:
        # Windows 播放器可能持有已有完整文件；不能把不存在的目标当作成功。
        if not os.path.isfile(target) or os.path.getsize(target) != size:
            raise
        log(f"写入 {os.path.basename(target)} 时文件被占用，使用已有同大小文件")
        os.remove(part)


def _remove_part(part):
    try:
        os.remove(part)
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"无法删除临时文件 {os.path.basename(part)}: {e}")


def touch(path):
    try:
        os.utime(path)
    except Exception:
        pass


def download_file(filename, song_id=None, stall_timeout=None):
    """完整下载；只有正常读至 EOF 且长度匹配才发布成功。"""
    target = target_path(filename, song_id)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return True
    run_id = _run_id
    meta = _begin_download(target, filename, song_id)
    if meta is None:
        return False
    part = target + ".part"
    ok = False
    error = None
    done = total = 0
    conn = None
    eof = False
    try:
        conn, resp = open_upstream("cdn.pypy.dance", "/" + filename)
        if resp.status != 200:
            if resp.status == 404 and meta["song_id"] is not None:
                invalidate_mapping(meta["song_id"], filename)
            raise RuntimeError(f"HTTP {resp.status}")
        total = int(resp.getheader("Content-Length") or 0)
        last_progress = time.monotonic()
        with open(part, "wb") as f:
            while not _cancelled(run_id):
                if stall_timeout is not None and time.monotonic() - last_progress >= stall_timeout:
                    error = f"连续 {stall_timeout} 秒没有新数据"
                    log(f"歌单直取下载 {filename} {error}，本次先尝试后续歌曲")
                    break
                chunk = _read_download_chunk(resp)
                if not chunk:
                    eof = True
                    break
                f.write(chunk)
                done += len(chunk)
                _note_upstream_progress(conn, len(chunk))
                last_progress = time.monotonic()
                emit_progress(filename, done, total, meta["task_id"])
        if not _cancelled(run_id) and eof and done > 0 and (total == 0 or done == total):
            _commit_download(part, target, done)
            ok = True
            log(f"缓存完成 {os.path.basename(target)} ({done / 1048576:.1f} MB)")
        elif not _cancelled(run_id):
            error = error or f"下载不完整 ({done}/{total})"
            log(f"{filename}: {error}")
    except Exception as e:
        error = str(e)
        log(f"下载 {filename} 出错: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        cancelled = not ok and _cancelled(run_id)
        if not ok:
            _remove_part(part)
        _finish_download(meta, ok, cancelled, None if cancelled else error, done, total)
    return ok


def start_background_download(filename, song_id=None):
    if not filename:
        return
    target = target_path(filename, song_id)
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return
    with _downloads_lock:
        if _canonical_path(target) in _downloads_in_progress:
            return
    _queue_serial_download("cdn", filename, song_id)


def download_wd(song_id):
    """WannaDance 完整下载：api.udon.dance/Api/Songs/play?id=N 302 到 play.udon.dance 文件。
    文件名本身带编号，无需映射表。"""
    target = wd_target_path(song_id)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return True
    name = wd_cache_name(song_id)
    run_id = _run_id
    meta = _begin_download(target, name, None, "wannadance")
    if meta is None:
        return False
    part = target + ".part"
    ok = False
    error = None
    done = total = 0
    conn = None
    eof = False
    try:
        conn, resp = open_upstream(UDON_API_HOST, f"/Api/Songs/play?id={song_id}")
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        total = int(resp.getheader("Content-Length") or 0)
        with open(part, "wb") as f:
            while not _cancelled(run_id):
                chunk = _read_download_chunk(resp)
                if not chunk:
                    eof = True
                    break
                f.write(chunk)
                done += len(chunk)
                _note_upstream_progress(conn, len(chunk))
                emit_progress(name, done, total, meta["task_id"])
        if not _cancelled(run_id) and eof and done > 0 and (total == 0 or done == total):
            _commit_download(part, target, done)
            ok = True
            log(f"缓存完成 {os.path.basename(target)} ({done / 1048576:.1f} MB)")
        elif not _cancelled(run_id):
            error = f"下载不完整 ({done}/{total})"
            log(f"{name}: {error}")
    except Exception as e:
        error = str(e)
        log(f"下载 {name} 出错: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        cancelled = not ok and _cancelled(run_id)
        if not ok:
            _remove_part(part)
        _finish_download(meta, ok, cancelled, None if cancelled else error, done, total)
    return ok


def start_wd_download(song_id):
    target = wd_target_path(song_id)
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return
    with _downloads_lock:
        if _canonical_path(target) in _downloads_in_progress:
            return
    _queue_serial_download("wd", None, song_id)


# ---------------- YouTube 源录流（从 VRChat 日志捕获播放中的直链） ----------------
# VRChat 播放 YouTube 源时会用自己的 yt-dlp 解析出 googlevideo 直链并写进日志：
#   [Video Playback] URL 'http://api.pypy.dance/video?id=N' resolved to 'https://...googlevideo.com/...'
# 监听到就直接下载该直链保存（不碰 YouTube 解析，无机器人验证问题）

RESOLVED_RE = re.compile(
    r"URL 'http://api\.pypy\.dance/video\?id=(\d+)' resolved to '(https://[^']+)'")


def scan_resolved_youtube(lines):
    """扫描日志行，发现播放中的 YouTube 源直链就开始录流"""
    for line in lines:
        m = RESOLVED_RE.search(line)
        if not m:
            continue
        url = m.group(2)
        if "googlevideo.com" not in url:
            continue
        start_googlevideo_download(int(m.group(1)), url)


def download_googlevideo(song_id, url):
    """保存播放中捕获的直链；取消或不完整时不会冒充可用本地视频。"""
    target = yt_target_path(song_id)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return True
    run_id = _run_id
    name = os.path.basename(target)
    meta = _begin_download(target, name, song_id, "youtube")
    if meta is None:
        return False
    part = target + ".part"
    ok = False
    error = None
    done = total = 0
    eof = False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        log(f"捕获到播放中的 YouTube 源 id={song_id}，开始录制...")
        with _tracked_urlopen(req, timeout=30) as r:
            status = getattr(r, "status", 200)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            total = int(r.headers.get("Content-Length") or 0)
            with open(part, "wb") as f:
                while not _cancelled(run_id):
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        eof = True
                        break
                    f.write(chunk)
                    done += len(chunk)
                    emit_progress(name, done, total, meta["task_id"])
        if not _cancelled(run_id) and eof and done > 0 and (total == 0 or done == total):
            _commit_download(part, target, done)
            ok = True
            log(f"YouTube 源录制完成 {name} ({done / 1048576:.1f} MB)")
        elif not _cancelled(run_id):
            error = f"录制不完整 ({done}/{total})，下次播放时重试"
            log(f"YouTube 源 id={song_id}: {error}")
    except Exception as e:
        error = str(e)
        log(f"YouTube 源录制失败 id={song_id}: {e}")
    finally:
        cancelled = not ok and _cancelled(run_id)
        if not ok:
            _remove_part(part)
        _finish_download(meta, ok, cancelled, None if cancelled else error, done, total)
    return ok


def start_googlevideo_download(song_id, url):
    if yt_local_path(song_id):
        return
    target = yt_target_path(song_id)
    with _downloads_lock:
        if _canonical_path(target) in _downloads_in_progress:
            return
    _queue_serial_download("youtube", url, song_id)


# ---------------- 缓存目录清理 (LRU，曲库目录不清理) ----------------

def cleanup_cache():
    # 与登记下载/手工清理共用锁，避免检查 busy 后又有新任务开始写入。
    with _downloads_lock:
        try:
            directory = _config["cache_dir"]
            if (_same_directory(directory, _config.get("library_dir")) or _same_directory(directory, _config.get("wd_library_dir"))
                    or _same_directory(directory, _config.get("dudu_library_dir"))):
                return
            limit = int(_config["max_cache_mb"]) * 1048576
            files = []
            total = 0
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if name.endswith(".mp4") and os.path.isfile(path):
                    st = os.stat(path)
                    files.append((st.st_mtime, st.st_size, path))
                    total += st.st_size
            for _, size, path in sorted(files):
                if total <= limit:
                    break
                if _canonical_path(path) in _downloads_in_progress:
                    continue
                try:
                    os.remove(path)
                    total -= size
                    log(f"缓存超限，清理 {os.path.basename(path)}")
                except OSError:
                    pass
        except (OSError, KeyError, ValueError):
            pass


# ---------------- 曲库任务取消（重建映射/校验共用） ----------------

def cancel_library_download():
    _library_stop.set()


# ---------------- 映射重建 ----------------

_rebuild_thread = None


def _start_library_task(kind, worker):
    global _rebuild_thread, _verify_thread
    with _lifecycle_lock, _config_lock, _library_task_lock:
        if not library_enabled():
            log("请先设置有效的曲库目录")
            return False
        if rebuilding() or verifying():
            log("已有曲库任务正在进行，请等待完成或取消后再试")
            return False
        if _stop_event.is_set():
            log("服务已停止，请重新启动服务后再运行曲库任务")
            return False
        _library_stop.clear()
        thread = threading.Thread(target=worker, daemon=True)
        if kind == "rebuild":
            _rebuild_thread = thread
        else:
            _verify_thread = thread
        thread.start()
    return True


def rebuild_mapping():
    """只补全本地曲库缺少的映射；与校验任务互斥。"""
    return _start_library_task("rebuild", _rebuild_worker)


def rebuilding():
    return _rebuild_thread is not None and _rebuild_thread.is_alive()


# ---------------- 曲库完整性校验 ----------------

_verify_thread = None


def verify_library():
    """对比远端文件大小，仅报告不一致，不自动删除或重新下载。"""
    return _start_library_task("verify", _verify_worker)


def verifying():
    return _verify_thread is not None and _verify_thread.is_alive()


def _remote_size(filename):
    conn, resp = open_upstream("cdn.pypy.dance", "/" + filename,
                               extra_headers={"Range": "bytes=0-0"}, retries=2)
    try:
        if resp.status == 200:
            value = resp.getheader("Content-Length")
            return int(value) if value else None
        if resp.status != 206:
            return None
        match = re.search(r"/(\d+)\s*$", resp.getheader("Content-Range") or "")
        return int(match.group(1)) if match else None
    finally:
        # 服务器忽略 Range 时不要为校验而读取整个视频。
        conn.close()


def _verify_worker():
    checked = bad = failed = skipped = 0
    bad_ids = []
    total = 0
    error = None
    try:
        directory = _config["library_dir"]
        ids = sorted({int(name[:-4]) for name in os.listdir(directory)
                      if re.fullmatch(r"\d+\.mp4", name)})
        total = len(ids)
        log(f"开始校验曲库文件大小，共 {total} 个文件...")
        emit("verify_progress", checked=0, bad=0, failed=0, skipped=0, total=total, current="")
        lock = threading.Lock()

        def work(sid):
            nonlocal checked, bad, failed, skipped
            if _library_stop.is_set() or _stop_event.is_set():
                return
            filename = resolve_video_filename(sid)
            if _library_stop.is_set() or _stop_event.is_set():
                return
            problem = None
            is_youtube = song_route(sid)[0] == "yt"
            if filename:
                try:
                    local_size = os.path.getsize(os.path.join(directory, f"{sid}.mp4"))
                    remote_size = _remote_size(filename)
                    if remote_size is None:
                        problem = "failed"
                    elif local_size != remote_size:
                        problem = "bad"
                        log(f"校验不一致 {sid}.mp4: 本地 {local_size} / 远端 {remote_size}（仅报告，未修改文件）")
                except Exception:
                    problem = "failed"
            else:
                problem = "skipped" if is_youtube else "failed"
            with lock:
                checked += 1
                if problem == "bad":
                    bad += 1
                    bad_ids.append(sid)
                elif problem == "failed":
                    failed += 1
                elif problem == "skipped":
                    skipped += 1
                progress = dict(checked=checked, bad=bad, failed=failed, skipped=skipped,
                                total=total, current=str(sid))
            emit("verify_progress", **progress)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(work, ids))
        flush_mapping()
    except Exception as e:
        error = str(e)
        log(f"曲库校验出错: {e}")
    cancelled = _library_stop.is_set() or _stop_event.is_set()
    log(("曲库校验已取消。" if cancelled else "曲库校验完成。")
        + f"已检查 {checked}/{total}，不一致 {bad}，YouTube 跳过 {skipped}，失败 {failed}")
    emit("verify_done", total=total, checked=checked, bad=bad, skipped=skipped, failed=failed,
         bad_ids=sorted(bad_ids), cancelled=cancelled, error=error)
    emit_stats()


def _rebuild_worker():
    done = failed = total = 0
    error = None
    try:
        ids = sorted({int(name[:-4]) for name in os.listdir(_config["library_dir"])
                      if re.fullmatch(r"\d+\.mp4", name)})
        with _mapping_lock:
            todo = [sid for sid in ids if not _mapping.get(sid)]
        total = len(todo)
        log(f"曲库共 {len(ids)} 个视频，其中 {total} 个需要补全映射")
        emit("rebuild_progress", done=0, failed=0, total=total, current="")
        lock = threading.Lock()

        def work(sid):
            nonlocal done, failed
            if _library_stop.is_set() or _stop_event.is_set():
                return
            resolve_video_filename(sid)
            if _library_stop.is_set() or _stop_event.is_set():
                return
            known = song_route(sid)[0] in ("cdn", "yt")
            with lock:
                done += int(known)
                failed += int(not known)
                progress = dict(done=done, failed=failed, total=total, current=str(sid))
            emit("rebuild_progress", **progress)

        with ThreadPoolExecutor(max_workers=16) as executor:
            list(executor.map(work, todo))
        flush_mapping()
    except Exception as e:
        error = str(e)
        log(f"映射重建出错: {e}")
    cancelled = _library_stop.is_set() or _stop_event.is_set()
    log(("映射重建已取消。" if cancelled else "映射重建完成。") + f" 成功 {done}，失败 {failed}")
    emit("rebuild_done", done=done, failed=failed, total=total, cancelled=cancelled, error=error)
    refresh_queue_state()


# ---------------- CDN IP 测速 ----------------

SPEEDTEST_BYTES = 8 * 1048576
CDN_POOL_FILENAME = "cdn-pool.json"
CDN_POOL_LIMIT = 128
CDN_POOL_FAILURE_LIMIT = 3
QUICK_CANDIDATE_LIMIT = 8
QUICK_NEW_IPS = 4
QUICK_PROBE_BYTES = 256 * 1024
QUICK_FINALISTS = 8
_cdn_pool_lock = threading.RLock()


def cdn_pool_path():
    return os.path.join(data_dir(), CDN_POOL_FILENAME)


def _load_cdn_pool():
    try:
        with open(cdn_pool_path(), "r", encoding="utf-8") as handle:
            raw = json.load(handle).get("items", {})
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError) as error:
        log(f"CDN 候选池读取失败，本次按空候选池处理: {error}")
        return {}
    if not isinstance(raw, dict):
        return {}
    pool = {}
    for ip, entry in raw.items():
        if not _is_public_ip(ip) or not isinstance(entry, dict):
            continue
        speed = entry.get("mbps")
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
            speed = None
        failures = entry.get("failures", 0)
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
            failures = 0
        timestamps = {}
        for name in ("last_tested", "last_success"):
            value = entry.get(name, 0)
            timestamps[name] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else 0.0
        pool[ip] = {"mbps": speed, "failures": min(failures, CDN_POOL_FAILURE_LIMIT), **timestamps}
    return pool


def _save_cdn_pool(pool):
    base = data_dir()
    os.makedirs(base, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="cdn-pool-", suffix=".tmp", dir=base)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "items": pool}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, cdn_pool_path())
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _record_speedtest_results(results, update_speed=True):
    if not results:
        return
    with _cdn_pool_lock:
        pool = _load_cdn_pool()
        now = time.time()
        for item in results:
            ip = str(item.get("ip") or "")
            if not _is_public_ip(ip):
                continue
            previous = pool.get(ip, {})
            speed = item.get("mbps")
            success = item.get("error") is None and isinstance(speed, (int, float)) and not isinstance(speed, bool) and math.isfinite(speed) and speed > 0
            failures = 0 if success else previous.get("failures", 0) + 1
            if failures >= CDN_POOL_FAILURE_LIMIT:
                pool.pop(ip, None)
                continue
            entry = {
                "mbps": previous.get("mbps"),
                "last_tested": now,
                "last_success": now if success else previous.get("last_success", 0),
                "failures": failures,
            }
            if success and update_speed:
                entry["mbps"] = float(speed)
            pool[ip] = entry
        if len(pool) > CDN_POOL_LIMIT:
            ranked = sorted(pool.items(), key=lambda pair: (
                pair[1].get("failures", 0) == 0,
                pair[1].get("mbps") or 0,
                pair[1].get("last_tested", 0),
            ), reverse=True)
            pool = dict(ranked[:CDN_POOL_LIMIT])
        try:
            _save_cdn_pool(pool)
        except OSError as error:
            log(f"CDN 候选池保存失败: {error}")


def _favorite_cdn_ips():
    try:
        with open(os.path.join(data_dir(), "cdn-favorites.json"), "r", encoding="utf-8") as handle:
            values = json.load(handle)
        return [ip for ip in values if isinstance(ip, str) and _is_public_ip(ip)] if isinstance(values, list) else []
    except (OSError, ValueError):
        return []


def _quick_cdn_candidates():
    candidates = []

    def add(ip):
        if _is_public_ip(ip) and ip not in candidates:
            candidates.append(ip)

    pref = (get_config().get("preferred_cdn_ip") or "").strip()
    add(pref)
    for ip in gather_candidate_ips("cdn.pypy.dance"):
        add(ip)
    for ip in _favorite_cdn_ips():
        add(ip)
    history = sorted((item for item in _load_cdn_pool().items()
                      if item[1].get("failures", 0) == 0 and item[1].get("last_success", 0) > 0),
                     key=lambda pair: (pair[1].get("mbps") is not None,
                                       pair[1].get("mbps") or 0,
                                       pair[1].get("last_success", 0)), reverse=True)
    limit = _speedtest_candidate_count()
    known_limit = max(1, limit - QUICK_NEW_IPS)
    for ip, _ in history:
        if len(candidates) >= known_limit:
            break
        add(ip)
    attempts = 0
    while len(candidates) < limit and attempts < 3:
        for ip in _sample_cf_ips((limit - len(candidates)) * 2):
            if len(candidates) >= limit:
                break
            add(ip)
        attempts += 1
    return candidates[:limit]


def _pick_test_file():
    with _mapping_lock:
        for v in _mapping.values():
            if v and not v.startswith("yt:"):
                return v
    return "KhlUAAndf98.mp4"


def _download_test(ip, filename, nbytes, timeout=20, include_latency=False):
    """从指定 CF IP 下载 nbytes 测速，返回 (MB/s, None) 或 (None, 错误)。
    include_latency=True 时追加响应延迟（含重定向），供初筛筛选，不冒充下载速度。
    节点可能忽略 Range 返回完整文件，读到 nbytes 即主动停止；另有总时限兜底。"""
    deadline = time.monotonic() + timeout
    t0 = time.monotonic()
    conn = None
    got = 0
    responded_at = None

    def finish(speed, error):
        if include_latency:
            return speed, error, max((responded_at or time.monotonic()) - t0, 0.0)
        return speed, error

    try:
        conn = _HTTPConnectionIP(ip, "cdn.pypy.dance", 80, timeout=timeout)

        def apply_remaining_timeout():
            if _speedtest_stop.is_set():
                raise RuntimeError("测速已停止")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("超时（速度过慢）")
            conn.timeout = max(0.1, remaining)

        host, path = "cdn.pypy.dance", "/" + filename
        resp = None
        for _ in range(3):  # 跟随重定向，上限防环
            apply_remaining_timeout()
            conn.request("GET", path, headers={
                "Host": host,
                "Range": f"bytes=0-{nbytes - 1}",
                "Accept-Encoding": "identity",
            })
            apply_remaining_timeout()
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location") or ""
                apply_remaining_timeout()
                resp.read()
                u = urllib.parse.urlparse(loc)
                if not u.hostname:
                    break
                host = u.hostname
                path = (u.path or "/") + (("?" + u.query) if u.query else "")
                continue
            break
        responded_at = time.monotonic()
        if resp is None or resp.status not in (200, 206):
            err = f"HTTP {resp.status if resp else '无响应'}"
            if resp is not None:
                apply_remaining_timeout()
                resp.read()
            return finish(None, err)
        try:
            expected = min(nbytes, int(resp.getheader("Content-Length") or nbytes))
        except (TypeError, ValueError):
            expected = nbytes
        while got < nbytes:
            if time.monotonic() >= deadline:
                result = (None, "超时（速度过慢）") if got < nbytes // 4 else (got / 1048576 / max(time.monotonic() - t0, 0.01), None)
                return finish(*result)
            apply_remaining_timeout()
            chunk = resp.read(min(256 * 1024, nbytes - got))
            if not chunk:
                break
            got += len(chunk)
        if got <= 0:
            return finish(None, "节点未返回测速数据")
        if got < expected:
            return finish(None, f"测速响应不完整 ({got}/{expected})")
        dt = max(time.monotonic() - t0, 0.01)
        return finish(got / 1048576 / dt, None)
    except (TimeoutError, socket.timeout) as e:
        if _speedtest_stop.is_set():
            return finish(None, "测速已停止")
        if got >= nbytes // 4:
            return finish(got / 1048576 / max(time.monotonic() - t0, 0.01), None)
        return finish(None, str(e))
    except Exception as e:
        return finish(None, "测速已停止" if _speedtest_stop.is_set() else str(e))
    finally:
        if conn is not None:
            conn.close()


def _speedtest_thread_count():
    value = get_config().get("speedtest_threads", 3)
    return min(8, max(1, value)) if isinstance(value, int) and not isinstance(value, bool) else 3


def _speedtest_candidate_count():
    value = get_config().get("speedtest_candidates", QUICK_CANDIDATE_LIMIT)
    return min(64, max(2, value)) if isinstance(value, int) and not isinstance(value, bool) else QUICK_CANDIDATE_LIMIT


def _speedtest_sample_bytes():
    value = get_config().get("speedtest_sample_mb", 8)
    value = min(32, max(1, value)) if isinstance(value, int) and not isinstance(value, bool) else 8
    return value * 1048576


def _measure_ips(candidates, filename, nbytes=None, timeout=None, report=True, with_latency=False):
    if not candidates:
        return []
    if nbytes is None:
        nbytes = _speedtest_sample_bytes()
    workers = min(_speedtest_thread_count(), len(candidates))
    sample = f"{nbytes // 1048576}MB" if nbytes >= 1048576 else f"{nbytes // 1024}KB"
    log(f"{'测速' if report else '初筛'}使用 {workers} 个并发线程，每个节点下载 {sample} 取样...")

    def download(ip):
        if with_latency:
            return _download_test(ip, filename, nbytes, 20 if timeout is None else timeout, True)
        if timeout is None:
            return _download_test(ip, filename, nbytes)
        return _download_test(ip, filename, nbytes, timeout)

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download, ip): ip for ip in candidates}
        for future in as_completed(futures):
            ip = futures[future]
            outcome = future.result()
            if with_latency:
                mbps, err, latency = outcome
            else:
                mbps, err = outcome
                latency = None
            item = {"ip": ip, "mbps": mbps, "error": err}
            if with_latency:
                item["latency"] = latency
            results.append(item)
            if report:
                if mbps is not None:
                    log(f"测速 {ip}: {mbps:.2f} MB/s")
                else:
                    log(f"测速 {ip}: 失败 ({err})")
                emit("speedtest_result", ip=ip, mbps=mbps, error=err)
    _record_speedtest_results(results, update_speed=report)
    return results


def _quick_speedtest(candidates, filename):
    probes = _measure_ips(candidates, filename, QUICK_PROBE_BYTES, 6,
                          report=False, with_latency=True)
    usable = [item for item in probes if not item.get("error") and (item.get("mbps") or 0) > 0]
    if len(usable) <= QUICK_FINALISTS:
        finalists = [item["ip"] for item in usable]
    else:
        finalists = [item["ip"] for item in sorted(
            usable, key=lambda item: item.get("latency", float("inf")))[:QUICK_FINALISTS]]
    pref = (get_config().get("preferred_cdn_ip") or "").strip()
    current = next((item for item in usable if item["ip"] == pref), None)
    if current is not None and pref not in finalists:
        finalists.append(pref)
    log(f"快速初筛完成：{len(usable)}/{len(candidates)} 个可用，对 {len(finalists)} 个候选进行完整测速...")
    if not finalists:
        return probes
    return _measure_ips(finalists, filename)


def _apply_fastest_result(results, *, selection_revision=None, require_auto=False):
    valid = [item for item in results if not item.get("error") and (item.get("mbps") or 0) > 0]
    if not valid:
        log("CDN 优选：没有可用的测速结果，保持当前设置")
        return False
    best = max(valid, key=lambda item: item["mbps"])
    pref = (get_config().get("preferred_cdn_ip") or "").strip()
    current = next((item for item in valid if item["ip"] == pref), None)
    if best["ip"] == pref:
        log(f"CDN 优选：当前 {pref} 就是最快（{best['mbps']:.2f} MB/s），保持不变")
        return False
    if current is not None and best["mbps"] <= current["mbps"]:
        log(f"CDN 优选：当前 {pref}（{current['mbps']:.2f} MB/s）不逊于新节点 "
            f"{best['ip']}（{best['mbps']:.2f} MB/s），保持不变")
        return False
    try:
        updated = update_config({"preferred_cdn_ip": best["ip"]},
                                expected_cdn_revision=selection_revision,
                                require_auto_cdn=require_auto)
        if not updated["changed"]:
            return False
    except Exception as error:
        log(f"CDN 优选：最快 {best['ip']}（{best['mbps']:.2f} MB/s）写入配置失败: {error}")
        return False
    log(f"CDN 优选：已自动应用 {best['ip']}（{best['mbps']:.2f} MB/s）"
        + (f"，替换原 {pref}" if pref else ""))
    return True


SLOW_WINDOW_SECONDS = 10.0
SLOW_WINDOW_BYTES = 64 * 1024
SLOW_THRESHOLD_MBPS = 0.75
SLOW_RECHECK_BYTES = 2 * 1048576
SLOW_RECHECK_COOLDOWN = 300.0
SLOW_RECHECK_IMPROVEMENT = 1.3
_slow_check_lock = threading.Lock()
_slow_check_running = False
_slow_check_after = 0.0


def _slow_recheck(current_ip, observed_speed, preferred_at_trigger=None, selection_revision=None):
    global _slow_check_running
    if preferred_at_trigger is None:
        preferred_at_trigger = get_config().get("preferred_cdn_ip") or ""
    try:
        candidates = [current_ip]
        history = sorted((item for item in _load_cdn_pool().items()
                          if item[1].get("failures", 0) == 0 and (item[1].get("mbps") or 0) > 0),
                         key=lambda pair: pair[1]["mbps"], reverse=True)
        for ip, _ in history:
            if ip not in candidates:
                candidates.append(ip)
        for ip in _origin_ips.get("cdn.pypy.dance", []):
            if ip not in candidates:
                candidates.append(ip)
        candidates = candidates[:4]
        log(f"检测到 CDN 持续偏慢（{observed_speed:.2f} MB/s），后台小样本复测 {len(candidates)} 个节点...")
        results = []
        with ThreadPoolExecutor(max_workers=min(2, len(candidates))) as pool:
            futures = {pool.submit(_download_test, ip, _pick_test_file(), SLOW_RECHECK_BYTES, 12): ip
                       for ip in candidates}
            for future in as_completed(futures):
                speed, error = future.result()
                results.append({"ip": futures[future], "mbps": speed, "error": error})
        _record_speedtest_results(results)
        if _stop_event.is_set() or _speedtest_stop.is_set():
            return
        current = next((item for item in results if item["ip"] == current_ip
                        and not item["error"] and (item["mbps"] or 0) > 0), None)
        baseline = max(observed_speed, current["mbps"] if current else 0.0)
        alternatives = [item for item in results if item["ip"] != current_ip
                        and not item["error"] and (item["mbps"] or 0) > 0]
        best = max(alternatives, key=lambda item: item["mbps"]) if alternatives else None
        if best is None or best["mbps"] < baseline * SLOW_RECHECK_IMPROVEMENT:
            log(f"CDN 慢速复测：当前节点已恢复或没有明显更快的替代节点，保持不变（基准 {baseline:.2f} MB/s）")
            return
        config = get_config()
        if not config.get("auto_prefer_cdn", True):
            return
        previous = config.get("preferred_cdn_ip") or ""
        if previous != preferred_at_trigger:
            log("CDN 慢速复测期间用户已更改节点，放弃自动覆盖")
            return
        updated = update_config({"preferred_cdn_ip": best["ip"]},
                                expected_cdn_revision=selection_revision, require_auto_cdn=True)
        if not updated["changed"]:
            return
        log(f"CDN 慢速恢复：后续请求改用 {best['ip']}（{best['mbps']:.2f} MB/s），"
            f"当前连接不中断" + (f"，原节点 {previous}" if previous else ""))
    except Exception as error:
        log(f"CDN 慢速复测失败: {error}")
    finally:
        with _slow_check_lock:
            _slow_check_running = False


def _maybe_recheck_slow_connection(conn, speed):
    global _slow_check_running, _slow_check_after
    now = time.time()
    with _slow_check_lock:
        if _slow_check_running or now < _slow_check_after:
            return
        _slow_check_running = True
        _slow_check_after = now + SLOW_RECHECK_COOLDOWN
    try:
        with _config_lock:
            preferred = _config.get("preferred_cdn_ip") or ""
            revision = _cdn_selection_revision
        _spawn_service_thread(_slow_recheck, (conn._selected_ip, speed, preferred, revision))
    except Exception:
        with _slow_check_lock:
            _slow_check_running = False


def _note_upstream_progress(conn, byte_count):
    if (byte_count <= 0 or not _running or _stop_event.is_set()
            or not _config.get("auto_prefer_cdn", True)):
        return
    host = getattr(conn, "host", "")
    current_ip = getattr(conn, "_selected_ip", "")
    if not (host == UDON_PLAY_HOST or host == "cdn.pypy.dance"
            or host.endswith(".cdn.pypy.dance")) or not _is_public_ip(current_ip):
        return
    if any(thread.name in ("auto_speedtest_apply", "_speed_test_ips", "_full_scan_ips")
           and thread.is_alive() for thread in list(_service_threads)):
        return
    now = time.monotonic()
    started = getattr(conn, "_slow_window_started", None)
    if started is None:
        conn._slow_window_started = now
        conn._slow_window_bytes = byte_count
        return
    conn._slow_window_bytes += byte_count
    elapsed = now - started
    if elapsed < SLOW_WINDOW_SECONDS or conn._slow_window_bytes < SLOW_WINDOW_BYTES:
        return
    speed = conn._slow_window_bytes / 1048576 / elapsed
    conn._slow_window_started = now
    conn._slow_window_bytes = 0
    if speed < SLOW_THRESHOLD_MBPS:
        _maybe_recheck_slow_connection(conn, speed)


def _read_download_chunk(response):
    # read(n) waits for n bytes; read1 returns available data after one socket
    # read, so a slow connection can be observed without collecting 256 KiB.
    reader = getattr(response, "read1", None)
    return reader(64 * 1024) if callable(reader) else response.read(64 * 1024)


def _run_tracked_speedtest(function, *args):
    if get_runtime_state().get("phase") != "stopping":
        _speedtest_stop.clear()
    thread = threading.current_thread()
    added = thread not in _service_threads
    previous_name = thread.name
    if added:
        _service_threads.append(thread)
    thread.name = function.__name__
    try:
        return function(*args)
    finally:
        thread.name = previous_name
        if added and thread in _service_threads:
            _service_threads.remove(thread)


def _speed_test_ips(ips=None):
    """收藏测速直接完整测速；快速扫描合并当前、DNS、收藏、历史与新随机节点。"""
    with _config_lock:
        revision = _cdn_selection_revision
    filename = _pick_test_file()
    pref = (get_config().get("preferred_cdn_ip") or "").strip()
    if ips is not None:
        candidates = list(dict.fromkeys(str(ip).strip() for ip in ips if _is_public_ip(str(ip).strip())))
        if _is_public_ip(pref) and pref not in candidates:
            candidates.append(pref)
        log(f"收藏测速：共 {len(candidates)} 个节点（含当前节点），无需解析 DNS。")
    else:
        log("快速扫描：正在汇总当前、DNS、收藏、历史候选与新节点...")
        candidates = _quick_cdn_candidates()
    if not candidates:
        log("测速失败: 没有可测试的 CDN IP，请检查网络或收藏列表")
        emit("speedtest_done", results=[])
        return
    if ips is None:
        log(f"快速扫描开始：共 {len(candidates)} 个候选，先小样本初筛再完整测速...")
        results = _quick_speedtest(candidates, filename)
    else:
        results = _measure_ips(candidates, filename)
    if not _speedtest_stop.is_set():
        _apply_fastest_result(results, selection_revision=revision)
    emit("speedtest_done", results=results)


def speed_test_ips(ips=None):
    return _run_tracked_speedtest(_speed_test_ips, ips)


def auto_speedtest_apply():
    """服务启动后自动做一次快速测速，只在最快节点优于当前节点时切换。"""
    with _config_lock:
        revision = _cdn_selection_revision
        if not _config.get("auto_prefer_cdn", True):
            return
    if _stop_event.wait(3):  # 让位给启动解析与首批下载
        return
    log("自动优选 CDN：正在汇总当前、DNS、收藏、历史候选与新节点...")
    filename = _pick_test_file()
    candidates = _quick_cdn_candidates()
    if not candidates:
        return
    log(f"自动优选 CDN：共 {len(candidates)} 个候选，先小样本初筛再完整测速...")
    results = _quick_speedtest(candidates, filename)
    if _stop_event.is_set():
        return
    _apply_fastest_result(results, selection_revision=revision, require_auto=True)
    emit("speedtest_done", results=results)


# Cloudflare 官方 IPv4 段（https://www.cloudflare.com/ips-v4）
CF_IP_RANGES = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/12",
    "172.64.0.0/13", "131.0.72.0/22",
]

FULLSCAN_SAMPLES = 300            # 从全段随机抽样探测的 IP 数
FULLSCAN_TOP = 10                 # 初筛后进入完整测速的数量
FULLSCAN_PROBE_BYTES = 64 * 1024  # 初筛每个 IP 的下载量


def _sample_cf_ips(n):
    """按段大小加权，从 Cloudflare 全部 IPv4 段随机抽 n 个不重复 IP"""
    nets = [ipaddress.ip_network(r) for r in CF_IP_RANGES]
    weights = [net.num_addresses for net in nets]
    ips = set()
    while len(ips) < n:
        net = random.choices(nets, weights=weights)[0]
        ip = net.network_address + random.randrange(net.num_addresses)
        ips.add(str(ipaddress.ip_address(ip)))
    return list(ips)


def _full_scan_ips():
    """抽样探测 Cloudflare 节点，再对候选做完整测速。"""
    with _config_lock:
        revision = _cdn_selection_revision
    filename = _pick_test_file()
    samples = _sample_cf_ips(FULLSCAN_SAMPLES)
    pref = (get_config().get("preferred_cdn_ip") or "").strip()
    if _is_public_ip(pref) and pref not in samples:
        samples.append(pref)
    log(f"全部扫描：本次初筛共 {len(samples)} 个 IP（含当前节点，每个试下 {FULLSCAN_PROBE_BYTES // 1024}KB）...")
    probes = []
    probe_results = []
    done = 0
    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(_download_test, ip, filename, FULLSCAN_PROBE_BYTES, 4, True): ip for ip in samples}
        for fut in as_completed(futures):
            mbps, error, latency = fut.result()
            result = {"ip": futures[fut], "mbps": mbps, "error": error, "latency": latency}
            probe_results.append(result)
            done += 1
            if mbps is not None:
                probes.append(result)
            if done % 50 == 0 or done == len(samples):
                log(f"全部扫描初筛进度: {done}/{len(samples)}（可用 {len(probes)}）")
    _record_speedtest_results(probe_results, update_speed=False)
    if _speedtest_stop.is_set():
        emit("speedtest_done", results=probe_results)
        return
    if not probes:
        log("全部扫描失败: 抽样 IP 全部不可用，请检查网络（如开着代理请关闭后重试）")
        emit("speedtest_done")
        return
    top = sorted(probes, key=lambda item: item["latency"])[:FULLSCAN_TOP]
    current = next((item for item in probes if item["ip"] == pref and (item.get("mbps") or 0) > 0), None)
    if current is not None and all(item["ip"] != pref for item in top):
        top.append(current)
    log(f"初筛完成，对 {len(top)} 个候选做 {_speedtest_sample_bytes() // 1048576}MB 完整测速...")
    results = _measure_ips([item["ip"] for item in top], filename)
    if not _speedtest_stop.is_set():
        _apply_fastest_result(results, selection_revision=revision)
    emit("speedtest_done", results=results)


def full_scan_ips():
    return _run_tracked_speedtest(_full_scan_ips)


# ---------------- 本地 HTTP 服务 ----------------

def parse_range(header, size):
    try:
        units, rng = header.split("=", 1)
        if units.strip() != "bytes":
            return None
        start_s, end_s = rng.split("-", 1)
        if start_s.strip() == "":
            length = int(end_s)
            if length <= 0:
                return None
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s.strip() else size - 1
        if start > end or start >= size:
            return None
        return start, min(end, size - 1)
    except Exception:
        return None


class DanceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DanceCache/2.0"

    def log_message(self, fmt, *args):
        pass

    def _host(self):
        return self.headers.get("Host", "").split(":", 1)[0].strip().lower()

    def _filename(self):
        path = urllib.parse.urlparse(self.path).path
        name = os.path.basename(path)
        return name if FILENAME_RE.match(name) else None

    def do_HEAD(self):
        if self._host() == DUDU_PLAY_HOST:
            return self._handle_dudu(head_only=True)
        if self._host() == API_HOST:
            return self._handle_api(head_only=True)
        if self._host() == UDON_PLAY_HOST:
            return self._handle_udon(head_only=True)
        name = self._filename()
        if not name:
            self.send_error(404)
            return
        p = cooldown_check(name, consume=False) or local_path_for(name)
        if p:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(os.path.getsize(p)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
        else:
            self._proxy(head_only=True)

    def do_GET(self):
        if self._host() == DUDU_PLAY_HOST:
            return self._handle_dudu()
        if self._host() == API_HOST:
            return self._handle_api()
        if self._host() == UDON_PLAY_HOST:
            return self._handle_udon()
        name = self._filename()
        if not name:
            self.send_error(404)
            return
        replacement = cooldown_check(name)
        p = replacement or local_path_for(name)
        if p:
            log(f"本地命中 {name} -> {os.path.basename(p)}")
            if not replacement:
                touch(p)
            _note_session_hit(name, p)
            self._serve_file(p, cooldown_name=name if replacement else None)
        else:
            log(f"本地未命中 {name}，回源拉取")
            _note_session_miss(name)
            self._proxy()

    # ---------------- global-cdn.dudufit.dance（DuDu FitDance）处理 ----------------

    def _handle_dudu(self, head_only=False):
        path = urllib.parse.urlparse(self.path).path
        match = DUDU_FILE_RE.match(os.path.basename(path))
        if not match:
            self.send_error(404)
            return
        sid = int(match.group(1))
        name = dudu_cache_name(sid)
        if head_only:
            path = cooldown_check(name, consume=False) or dudu_local_path(sid)
            if path:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(os.path.getsize(path)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
            else:
                self._proxy(head_only=True, upstream_host=DUDU_PLAY_HOST,
                            upstream_path=self.path, name=name, dudu_id=sid)
            return
        replacement = cooldown_check(name)
        path = replacement or dudu_local_path(sid)
        if path:
            log(f"本地命中 {name} (DuDu) -> {os.path.basename(path)}")
            if not replacement:
                touch(path)
            _note_session_hit(name, path)
            self._serve_file(path, cooldown_name=name if replacement else None)
        else:
            log(f"本地未命中 {name} (DuDu)，回源拉取")
            _note_session_miss(name)
            self._proxy(upstream_host=DUDU_PLAY_HOST, upstream_path=self.path,
                        name=name, dudu_id=sid)

    # ---------------- play.udon.dance（WannaDance）处理 ----------------

    def _handle_udon(self, head_only=False):
        """play.udon.dance/files/XXXX/{id}-{hash}.mp4?e=..&s=.. -> wd_{id}.mp4 本地缓存"""
        path = urllib.parse.urlparse(self.path).path
        m = WD_FILE_RE.match(os.path.basename(path))
        if not m:
            self.send_error(404)
            return
        sid = int(m.group(1))
        name = wd_cache_name(sid)
        if head_only:
            p = cooldown_check(name, consume=False) or wd_local_path(sid)
            if p:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(os.path.getsize(p)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
            else:
                self._proxy(head_only=True, upstream_host=UDON_PLAY_HOST,
                            upstream_path=self.path, name=name, wd_id=sid)
            return
        replacement = cooldown_check(name)
        p = replacement or wd_local_path(sid)
        if p:
            log(f"本地命中 {name} (WannaDance) -> {os.path.basename(p)}")
            if not replacement:
                touch(p)
            _note_session_hit(name, p)
            self._serve_file(p, cooldown_name=name if replacement else None)
        else:
            log(f"本地未命中 {name} (WannaDance)，回源拉取")
            _note_session_miss(name)
            self._proxy(upstream_host=UDON_PLAY_HOST, upstream_path=self.path,
                        name=name, wd_id=sid)

    # ---------------- api.pypy.dance 处理 ----------------

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_api(self, head_only=False):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/video":
            sid_s = (urllib.parse.parse_qs(u.query).get("id") or [""])[0]
            if sid_s.isdigit():
                return self._handle_video_redirect(int(sid_s), head_only)
        self._api_passthrough(head_only)

    def _handle_video_redirect(self, sid, head_only=False):
        """/video?id=N 三分支：CDN 源直接给文件名；YouTube 源已入库给本地地址；
        未知/未入库透传真实 API（顺手学习映射）"""
        kind, payload = song_route(sid)
        if kind == "cdn":
            return self._redirect(f"http://cdn.pypy.dance/{payload}")
        if kind == "yt":
            if yt_local_path(sid):
                log(f"API 重定向 id={sid} -> 本地 YouTube 缓存 yt_{sid}.mp4")
                return self._redirect(f"http://cdn.pypy.dance/yt_{sid}.mp4")
            # 未入库：本次走 YouTube 在线播放；VRChat 解析出直链后会被日志监听器捕获录制
            if sid not in _yt_online_logged:
                _yt_online_logged.add(sid)
                log(f"YouTube 源 id={sid} 未入库，本次在线播放，播放时自动录制入库")
            return self._redirect(payload)
        self._api_passthrough(head_only, sid)

    def _api_passthrough(self, head_only=False, sid=None):
        """原样透传到真实 api.pypy.dance；/video 的 302 响应顺手学习映射"""
        try:
            status, hdrs, body = api_request(self.path)
        except Exception as e:
            log(f"API 透传失败 {self.path}: {e}")
            try:
                self.send_error(502, f"upstream error: {e}")
            except Exception:
                pass
            return
        if sid is not None and status in (301, 302, 303, 307, 308):
            loc = hdrs.get("location", "")
            if loc:
                learn_mapping_from_redirect(sid, loc)
        try:
            self.send_response(status)
            skip = {"transfer-encoding", "connection", "keep-alive", "server", "date", "content-length"}
            for k, v in hdrs.items():
                if k not in skip:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _serve_file(self, path, cooldown_name=None):
        size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        rng = parse_range(range_header, size) if range_header else None
        try:
            if rng:
                start, end = rng
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                start, length = 0, size
                self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    if cooldown_name is not None and not cooldown_is_serving(cooldown_name):
                        self.close_connection = True
                        log(f"CoolDown 已退出，停止发送放松视频: {cooldown_name}")
                        return
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _proxy(self, head_only=False, upstream_host="cdn.pypy.dance", upstream_path=None, name=None, wd_id=None, dudu_id=None):
        if name is None:
            name = self._filename()
        if upstream_path is None:
            upstream_path = "/" + name
        run_id = _run_id
        range_header = self.headers.get("Range")
        extra = {"Range": range_header} if range_header else None
        conn = None
        meta = None
        part = None
        ok = False
        saved = total = 0
        error = None
        try:
            conn, resp = open_upstream(upstream_host, upstream_path, extra_headers=extra)
            if dudu_id is not None and resp.status in (401, 403):
                resp.read()
                conn.close()
                upstream_path = dudu_video_path(dudu_id)
                conn, resp = open_upstream(upstream_host, upstream_path, extra_headers=extra)
            if _cancelled(run_id):
                return
            self.send_response(resp.status)
            for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified"):
                value = resp.getheader(header)
                if value is not None:
                    self.send_header(header, value)
            if resp.getheader("Accept-Ranges") is None:
                self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if head_only:
                return

            # 错误正文与服务器未按完整请求返回的片段只透传，绝不登记为缓存。
            if range_header and resp.status in (200, 206):
                if _config.get("preload_sequential", True):
                    kind = "dudu" if dudu_id is not None else "wd" if wd_id is not None else "cdn"
                    song_id = dudu_id if dudu_id is not None else wd_id
                    _queue_serial_download(kind, None if song_id is not None else name, song_id)
                elif dudu_id is not None:
                    start_dudu_download(dudu_id)
                elif wd_id is not None:
                    start_wd_download(wd_id)
                else:
                    start_background_download(name)
            elif not range_header and resp.status == 200:
                target = (dudu_target_path(dudu_id) if dudu_id is not None else
                          wd_target_path(wd_id) if wd_id is not None else target_path(name))
                total = int(resp.getheader("Content-Length") or 0)
                source = "dudu" if dudu_id is not None else "wannadance" if wd_id is not None else "cdn"
                meta = _begin_download(target, name, dudu_id if dudu_id is not None else wd_id, source)
            if meta is None:
                while not _cancelled(run_id):
                    chunk = _read_download_chunk(resp)
                    if not chunk:
                        break
                    _note_upstream_progress(conn, len(chunk))
                    self.wfile.write(chunk)
                return

            part = target + ".part"
            client_alive = True
            eof = False
            with open(part, "wb") as f:
                while not _cancelled(run_id):
                    chunk = _read_download_chunk(resp)
                    if not chunk:
                        eof = True
                        break
                    f.write(chunk)
                    saved += len(chunk)
                    _note_upstream_progress(conn, len(chunk))
                    emit_progress(name, saved, total, meta["task_id"])
                    if client_alive:
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                            client_alive = False
            if not _cancelled(run_id) and eof and saved > 0 and (total == 0 or saved == total):
                _commit_download(part, target, saved)
                ok = True
                log(f"边下边存完成 {os.path.basename(target)} ({saved / 1048576:.1f} MB)")
            elif not _cancelled(run_id):
                error = f"下载不完整 ({saved}/{total})"
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            error = str(e) or "连接已中断"
        except Exception as e:
            error = str(e)
            log(f"代理 {name} 出错: {e}")
            if conn is None:
                try:
                    self.send_error(502, "upstream unavailable")
                except Exception:
                    pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if meta is not None:
                cancelled = not ok and _cancelled(run_id)
                if not ok and part:
                    _remove_part(part)
                _finish_download(meta, ok, cancelled, None if cancelled else error, saved, total)


# ---------------- 443 TCP 隧道（api.pypy.dance 的 https 请求原样透传） ----------------

_tunnel_sock = None


def _tunnel_handle(client, ips):
    up = None
    run_id = _run_id
    try:
        for ip in ips:
            try:
                up = socket.create_connection((ip, 443), timeout=10)
                break
            except OSError:
                continue
        if up is None:
            return
        pair = [client, up]
        last_activity = time.monotonic()
        while not _cancelled(run_id):
            r, _, _ = select.select(pair, [], [], 1)
            if not r:
                if time.monotonic() - last_activity >= 120:
                    return
                continue
            if _cancelled(run_id):
                return
            last_activity = time.monotonic()
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    (up if s is client else client).sendall(data)
                except OSError:
                    return
    finally:
        try:
            client.close()
        except Exception:
            pass
        if up is not None:
            try:
                up.close()
            except Exception:
                pass


def _tunnel_loop(sock, ips):
    while not _stop_event.is_set():
        try:
            client, _ = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_tunnel_handle, args=(client, ips), daemon=True).start()


# ---------------- VRChat 日志监听 ----------------

def find_newest_log():
    try:
        candidates = [
            os.path.join(VRC_LOG_DIR, n)
            for n in os.listdir(VRC_LOG_DIR)
            if n.startswith("output_log") and n.endswith(".txt")
        ]
    except FileNotFoundError:
        return None
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


_wd_current_song = None  # WannaDance 正在播放的歌曲（queue info 不含当前歌曲，从 UpdateState 行解析）
_wd_last_queue = None    # WannaDance 最近一次排队列表
_dudu_current_song = None
_dudu_last_queue = None
DUDU_STATE_RE = re.compile(r"VideoQueueHandler\.DeserializeVideoSongData\] deserialize video data:\s*(\{.*\})")


def _reset_wd_state():
    """服务停止时清空当前歌曲与实际播放去重状态，避免残留到下次启动"""
    global _wd_current_song, _wd_last_queue, _dudu_current_song, _dudu_last_queue, _last_playing_key
    _wd_current_song = None
    _wd_last_queue = None
    _dudu_current_song = None
    _dudu_last_queue = None
    _last_playing_key = None


def extract_queue_items(lines):
    """取最后一条 PyPyDance / WannaDance / DuDu 队列，后出现的来源生效。"""
    global _wd_current_song, _wd_last_queue, _dudu_current_song, _dudu_last_queue
    items = None
    source = None
    for line in lines:
        content = line.split("  -  ", 1)[-1].strip()
        match = QUEUE_RE.match(content)
        if match:
            try:
                items = json.loads(match.group(1))
                source = "pypy"
            except Exception:
                pass
            continue
        match = WD_QUEUE_RE.search(content)
        if match:
            try:
                raw = json.loads(match.group(1))
                is_dudu = (bool(raw) and all("playerNames" not in item for item in raw if isinstance(item, dict)) or (_dudu_current_song is not None and not raw))
                if is_dudu:
                    _dudu_last_queue = [_normalize_dudu_item(item) for item in raw if isinstance(item, dict)]
                    source = "dudu"
                else:
                    _wd_last_queue = [_normalize_wd_item(item) for item in raw if isinstance(item, dict)]
                    source = "wd"
            except Exception:
                pass
            continue
        match = WD_STATE_RE.search(content)
        if match:
            _wd_current_song = _normalize_wd_state(match)
            source = "wd"
            continue
        match = DUDU_STATE_RE.search(content)
        if match:
            try:
                current = json.loads(match.group(1))
                _dudu_current_song = {
                    "songNum": current.get("id"),
                    "videoName": current.get("info") or current.get("title") or "?",
                    "playerName": current.get("user") or "",
                    "dudu": True,
                }
                source = "dudu"
            except Exception:
                pass
    if source == "wd":
        queue = list(_wd_last_queue or [])
        current = _wd_current_song
        if current is None:
            return queue
        queue = [item for item in queue if item.get("songNum") != current.get("songNum")]
        return [current] + queue
    if source == "dudu":
        queue = list(_dudu_last_queue or [])
        current = _dudu_current_song
        if current is None:
            return queue
        queue = [item for item in queue if item.get("songNum") != current.get("songNum")]
        return [current] + queue
    if source == "pypy":
        return items
    return None


def _normalize_dudu_item(item):
    return {
        "songNum": item.get("songId"),
        "videoName": item.get("title") or "?",
        "playerName": item.get("playerName") or "",
        "dudu": True,
    }


def _normalize_wd_item(x):
    """WannaDance 队列项 -> 内部统一结构（wd=True 标记，songNum 即 WannaDance 歌曲编号）"""
    players = x.get("playerNames") or []
    return {
        "songNum": x.get("songId"),
        "videoName": x.get("title") or "?",
        "playerName": ", ".join(str(p) for p in players),
        "wd": True,
    }


def _normalize_wd_state(m):
    """WannaDance UpdateState 行 -> 队首当前歌曲。infoString 末段是点歌人，去掉后与队列 title 格式一致"""
    player = m.group(2).strip()
    info = m.group(3).strip()
    suffix = " | " + player
    if player and info.endswith(suffix):
        info = info[:-len(suffix)]
    return {
        "songNum": int(m.group(1)),
        "videoName": info or "?",
        "playerName": player,
        "wd": True,
    }


def describe_queue(items):
    """纯本地快照；不为显示队列发起解析请求，也不把未缓存误报为下载中。"""
    with _downloads_lock:
        tasks = copy.deepcopy(list(_download_meta.values()))
        outcomes = copy.deepcopy(list(_download_results.values()))
    cooldown = get_cooldown_state()
    result = []
    for index, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        sn = it.get("songNum", 0)
        valid_id = isinstance(sn, int) and not isinstance(sn, bool) and sn > 0
        if it.get("dudu"):
            kind, payload = ("dudu", dudu_cache_name(sn)) if valid_id else (None, None)
            filename = payload
            source = "dudu" if valid_id else "external"
            local = dudu_local_path(sn) if valid_id else None
        elif it.get("wd"):
            # WannaDance：文件名即编号，无需映射表
            kind, payload = ("cdn", wd_cache_name(sn)) if valid_id else (None, None)
            filename = payload
            source = "wannadance" if valid_id else "external"
            local = wd_local_path(sn) if valid_id else None
        else:
            kind, payload = song_route(sn) if valid_id else (None, None)
            filename = payload if kind == "cdn" else (f"yt_{sn}.mp4" if kind == "yt" else None)
            source = {"cdn": "cdn", "yt": "youtube"}.get(kind, "unknown" if valid_id else "external")
            # 不回落 yt_local_path：CDN 源歌曲按 CDN 文件名查找，历史 yt_{id}.mp4
            # 残留不能显示为已缓存（实际播放走 CDN 文件名，会未命中回源）
            local = local_path_for(filename) if filename else None
            if local is None and valid_id and in_library(sn):
                local = library_path(sn)
        expected_sources = ({"dudu"} if it.get("dudu") else {"wannadance"} if it.get("wd") else {"cdn", "youtube"})

        def matches(task, expected_sources=expected_sources, sn=sn, filename=filename, valid_id=valid_id):
            # 不同房间编号同范围时按来源匹配，避免旧房间结果贴错歌曲。
            if task.get("source") not in expected_sources:
                return False
            return (valid_id and task.get("song_id") == sn) or (filename and task.get("filename") == filename)
        task = next((task for task in tasks if matches(task)), None)
        outcome = next((outcome for outcome in reversed(outcomes) if matches(outcome)), None)
        entry = {
            "name": str(it.get("videoName") or "?"), "player": str(it.get("playerName") or ""),
            "song_id": sn if valid_id else None, "filename": filename,
            "source": task["source"] if task else source, "position": index,
            "storage": _storage_label(local) if local else None,
            "task_id": task["task_id"] if task else None,
            "error": None,
        }
        if filename and filename == cooldown["serving_filename"]:
            code, label = "cooldown", "Cool Down"
        elif local:
            code, label = "cached", "已缓存"
        elif task:
            code, label = "downloading", "下载中"
            entry["storage"] = task["storage"]
        elif outcome and not outcome["ok"]:
            code, label = ("cancelled", "已取消") if outcome["cancelled"] else ("failed", "失败")
            entry["error"] = outcome["error"]
        elif source == "youtube":
            code, label = "youtube", "等待播放"
        elif source == "external":
            code, label = "external", "外部源"
        elif source == "unknown":
            code, label = "unresolved", "待解析"
        else:
            code, label = "not_cached", "未缓存"
        entry.update(state_code=code, state=label)
        result.append(entry)
    return result


def get_queue_snapshot():
    with _queue_lock:
        items = copy.deepcopy(_last_queue_items)
    return describe_queue(items)


def refresh_queue_state():
    emit("queue", items=get_queue_snapshot())


YOUTUBE_DIRECT_STALL_SECONDS = 30


def submit_preload(items):
    run_id = _run_id
    count = int(_config["preload_count"]) + 1
    tasks = []
    for it in items[:count]:
        if not isinstance(it, dict) or _stop_event.is_set():
            continue
        sn = it.get("songNum", 0)
        if not (isinstance(sn, int) and not isinstance(sn, bool) and sn > 0):
            continue
        if it.get("dudu"):
            if _config.get("dudu_enabled") and not dudu_local_path(sn):
                tasks.append(("dudu", None, sn, None))
            continue
        if it.get("wd"):
            # WannaDance：文件名即编号，直接走 api.udon.dance 302 下载
            if not wd_local_path(sn):
                tasks.append(("wd", None, sn, None))
            continue
        if in_library(sn):
            continue
        kind, payload = song_route(sn)
        if kind == "yt" and yt_local_path(sn):
            continue
        youtube_direct = kind == "yt" and _config.get("bundle_direct", True)
        if kind is None or youtube_direct:
            resolve_video_filename(sn)
            kind, payload = song_route(sn)
        if kind == "cdn" and not is_cached(payload):
            tasks.append(("cdn", payload, sn, YOUTUBE_DIRECT_STALL_SECONDS if youtube_direct else None))
    # Both modes share a bounded dispatcher. Replace stale queue preloads, while
    # keeping full-cache jobs requested by the player ahead of upcoming songs.
    with _preload_dl_lock:
        if _cancelled(run_id):
            return
        pending = [task for task in _preload_dl_items if _preload_task_id(task) in _preload_dl_priority]
        seen = set(_preload_dl_active)
        _preload_dl_items[:] = []
        for task in pending + tasks:
            identity = _preload_key(task)
            if identity not in seen:
                _preload_dl_items.append(task)
                seen.add(identity)
        _preload_dl_priority.intersection_update(_preload_task_id(task) for task in _preload_dl_items)
    _preload_dl_wake.set()
    refresh_queue_state()


_preload_lock = threading.Lock()
_preload_pending = None
_preload_wake = threading.Event()

_preload_dl_lock = threading.Lock()
_preload_dl_items = []       # Ordered pending jobs; only dispatched jobs own threads.
_preload_dl_wake = threading.Event()
_preload_dl_active = set()   # Canonical targets reserved before starting a worker.
_preload_dl_priority = set() # Pending full-cache jobs requested by actual playback.


def _preload_target(kind, key, song_id):
    if kind == "youtube":
        return yt_target_path(song_id)
    return dudu_target_path(song_id) if kind == "dudu" else wd_target_path(song_id) if kind == "wd" else target_path(key, song_id)


def _preload_key(task):
    return _canonical_path(_preload_target(*task[:3]))


def _preload_task_id(task):
    # Pending jobs follow a new destination when directories change while idle.
    # Their playback priority must not depend on the old absolute target path.
    return task[:3]


def _wait_for_active_download(target):
    key = _canonical_path(target)
    while not _stop_event.is_set():
        with _downloads_lock:
            if key not in _downloads_in_progress:
                return
        _stop_event.wait(.2)


def _queue_serial_download(kind, key, song_id, stall_timeout=None):
    run_id = _run_id
    if _stop_event.is_set():
        return
    target = _preload_target(kind, key, song_id)
    identity = _canonical_path(target)
    with _downloads_lock:
        if identity in _downloads_in_progress:
            return
    task = (kind, key, song_id, stall_timeout)
    with _preload_dl_lock:
        if _cancelled(run_id) or identity in _preload_dl_active:
            return
        for item in _preload_dl_items:
            if _preload_key(item) == identity:
                _preload_dl_priority.discard(_preload_task_id(item))
        _preload_dl_items[:] = [item for item in _preload_dl_items
                                if _preload_key(item) != identity]
        _preload_dl_items.insert(0, task)
        _preload_dl_priority.add(_preload_task_id(task))
    _preload_dl_wake.set()


def _execute_preload_download(task, identity):
    try:
        if _stop_event.is_set():
            return
        kind, key, song_id, stall_timeout = task
        try:
            if kind == "dudu":
                download_dudu(song_id)
            elif kind == "wd":
                download_wd(song_id)
            elif kind == "youtube":
                download_googlevideo(song_id, key)
            else:
                download_file(key, song_id, stall_timeout=stall_timeout)
        except Exception as e:
            log(f"预载下载出错（{key or song_id}）: {e}")
        _wait_for_active_download(_preload_target(kind, key, song_id))
    finally:
        with _preload_dl_lock:
            _preload_dl_active.discard(identity)
        _preload_dl_wake.set()
        thread = threading.current_thread()
        if thread in _service_threads:
            _service_threads.remove(thread)


def _preload_download_loop():
    """Dispatch only as many background jobs as the current concurrency permits.

    Sequential mode waits for the active full-cache job, including one started
    by a playback request. Foreground proxy connections are never gated here.
    """
    while not _stop_event.is_set():
        with _config_lock, _preload_dl_lock:
            limit = 1 if _config.get("preload_sequential", True) else max(1, int(_config["max_parallel"]))
            task = None
            if len(_preload_dl_active) < limit and _preload_dl_items:
                task = _preload_dl_items.pop(0)
                identity = _preload_key(task)
                _preload_dl_priority.discard(_preload_task_id(task))
                if identity in _preload_dl_active:
                    continue
                _preload_dl_active.add(identity)
        if task is not None:
            try:
                _spawn_service_thread(_execute_preload_download, (task, identity))
            except Exception:
                with _preload_dl_lock:
                    _preload_dl_active.discard(identity)
                raise
            continue
        _preload_dl_wake.wait(.2)
        _preload_dl_wake.clear()


def _preload_worker_loop():
    """预载在独立线程执行：resolve_video_filename 要做网络请求并可能等待限流，
    若跑在日志监听线程上，限流期间队列刷新与 YouTube 直链捕获会一起停摆。"""
    global _preload_pending
    while not _stop_event.is_set():
        _preload_wake.wait(1.0)
        _preload_wake.clear()
        with _preload_lock:
            items = _preload_pending
            _preload_pending = None
        if items is None:
            continue
        try:
            submit_preload(items)
        except Exception as e:
            log(f"预载处理出错: {e}")


def submit_preload_async(items):
    """投递最新队列给预载线程；新队列到达时覆盖尚未处理的旧队列。"""
    global _preload_pending
    with _preload_lock:
        _preload_pending = copy.deepcopy(list(items or []))
    _preload_wake.set()


def _preload_retry_loop():
    """队列长时间不变时定期补一枪：前几首里仍有未解析/未缓存的歌曲就重新提交预载。
    解析失败本身有 RESOLVE_RETRY_DELAY 退避，这个循环只提供重试机会，不会轰炸 API。"""
    while not _stop_event.wait(45):
        with _queue_lock:
            items = list(_last_queue_items)
        if not items:
            continue
        count = int(_config["preload_count"]) + 1
        needs_retry = False
        for it in items[:count]:
            if not isinstance(it, dict):
                continue
            sn = it.get("songNum", 0)
            if not (isinstance(sn, int) and not isinstance(sn, bool) and sn > 0):
                continue
            if it.get("dudu"):
                needs_retry = _config.get("dudu_enabled") and not dudu_local_path(sn)
            elif it.get("wd"):
                needs_retry = not wd_local_path(sn)
            elif in_library(sn):
                continue
            else:
                kind, payload = song_route(sn)
                if kind == "yt" and yt_local_path(sn):
                    continue
                needs_retry = (kind is None or (kind == "cdn" and not is_cached(payload))
                               or (kind == "yt" and _config.get("bundle_direct", True)))
            if needs_retry:
                break
        if needs_retry:
            submit_preload_async(items)


# ---------------- Cool Down（下一首或当前歌曲刷新时替换为放松视频，一次性） ----------------

_cooldown_armed = False      # 待命状态
_cooldown_serving = None     # 正在用放松视频服务的文件名（该文件的后续 Range 请求都用放松视频）
_cooldown_exclude = None     # 当前歌曲文件名；下一首模式排除它，刷新模式等待它重新载入
_cooldown_reload_ready = False


def resolve_cooldown_file(configured_path=""):
    """留空使用内置视频；显式选择失效时不替换成其他视频。"""
    path = (configured_path or "").strip() or DEFAULT_COOLDOWN_PATH
    try:
        return path if path and os.path.isfile(path) and os.path.getsize(path) > 0 else None
    except OSError:
        return None


def cooldown_file():
    """返回当前可用的放松视频；不把安装目录写入用户配置。"""
    return resolve_cooldown_file(_config.get("cooldown_file", ""))


def cooldown_available():
    return cooldown_file() is not None


def get_cooldown_state():
    with _cooldown_lock:
        phase = "armed" if _cooldown_armed else ("serving" if _cooldown_serving is not None else "idle")
        return {"phase": phase, "available": cooldown_available(), "active": phase != "idle",
                "serving_filename": _cooldown_serving}


def cooldown_is_serving(filename):
    with _cooldown_lock:
        return _cooldown_serving == filename


def _emit_cooldown():
    emit("cooldown", **get_cooldown_state())
    refresh_queue_state()


def _queue_item_filename(item):
    if not isinstance(item, dict):
        return None
    sn = item.get("songNum", 0)
    if not (isinstance(sn, int) and not isinstance(sn, bool) and sn > 0):
        return None
    if item.get("dudu"):
        return dudu_cache_name(sn)
    if item.get("wd"):
        return wd_cache_name(sn)
    kind, payload = song_route(sn)
    return payload if kind == "cdn" else (f"yt_{sn}.mp4" if kind == "yt" else None)


def _current_song_filename():
    with _queue_lock:
        items = copy.deepcopy(_last_queue_items)
    return _queue_item_filename(items[0]) if items else None


def cooldown_arm():
    global _cooldown_armed, _cooldown_serving, _cooldown_exclude, _cooldown_reload_ready
    with _cooldown_lock:
        if not _running or _stop_event.is_set() or not cooldown_available():
            return False
        _cooldown_exclude = _current_song_filename()
        random_reload = _config.get("cooldown_trigger", "next") == "reload" and not _cooldown_exclude
        _cooldown_armed = True
        _cooldown_serving = None
        _cooldown_reload_ready = random_reload
    if random_reload:
        log("Cool Down 已待命：未识别当前歌曲，将在下一次视频请求时替换（适用于随机播放，可点击取消）")
    else:
        log("Cool Down 已待命：" + ("下次重新载入当前歌曲时" if _config.get("cooldown_trigger") == "reload"
                                  else "下一首在本机") + "替换为放松视频（可点击取消）")
    _emit_cooldown()
    return True


def cooldown_disarm(silent=False):
    global _cooldown_armed, _cooldown_serving, _cooldown_exclude, _cooldown_reload_ready
    with _cooldown_lock:
        was_serving = _cooldown_serving is not None
        _cooldown_armed = False
        _cooldown_serving = None
        _cooldown_exclude = None
        _cooldown_reload_ready = False
    if not silent:
        log("Cool Down 已取消" + ("；在房间里重新加载当前歌曲即可恢复正常播放" if was_serving else ""))
    _emit_cooldown()


def cooldown_active():
    return get_cooldown_state()["active"]


def cooldown_check(filename, consume=True):
    """下一首模式排除当前歌曲；刷新模式在已知当前歌曲重新载入时触发，
    无队列或未识别当前歌曲时允许下一次视频请求触发。"""
    global _cooldown_armed, _cooldown_serving, _cooldown_exclude, _cooldown_reload_ready
    changed = False
    message = None
    path = None
    with _cooldown_lock:
        if _cooldown_serving is not None:
            if filename == _cooldown_serving:
                path = cooldown_file()
                if path:
                    return path
                if not consume:
                    return None
                _cooldown_serving = None
                _cooldown_exclude = None
                _cooldown_reload_ready = False
                changed = True
                message = "Cool Down 视频已不可用，已恢复正常请求"
            else:
                if not consume:
                    return None
                _cooldown_serving = None
                _cooldown_exclude = None
                _cooldown_reload_ready = False
                changed = True
                message = "已切换到其他歌曲，Cool Down 本次替换结束"
        reload_mode = _config.get("cooldown_trigger", "next") == "reload"
        eligible = _cooldown_armed and (
            (_cooldown_reload_ready and (_cooldown_exclude is None or filename == _cooldown_exclude))
            if reload_mode else filename != _cooldown_exclude)
        if not consume:
            return cooldown_file() if eligible else None
        if eligible:
            path = cooldown_file()
            _cooldown_armed = False
            _cooldown_reload_ready = False
            changed = True
            if path:
                _cooldown_serving = filename
                message = f"Cool Down 触发：{filename} -> 放松视频 {os.path.basename(path)}"
            else:
                _cooldown_serving = None
                _cooldown_exclude = None
                message = "Cool Down 视频不可用，已自动取消，请重新选择文件"
    if message:
        log(message)
    if changed:
        _emit_cooldown()
    return path


def _cooldown_queue_reloaded(items):
    global _cooldown_reload_ready, _cooldown_exclude
    if not items:
        return
    with _queue_lock:
        previous = copy.deepcopy(_last_queue_items)
    filename = _queue_item_filename(items[0])
    with _cooldown_lock:
        if not _cooldown_armed or _config.get("cooldown_trigger", "next") != "reload":
            return
        if previous != items:
            _cooldown_exclude = filename
            _cooldown_reload_ready = False
            return
        if _cooldown_exclude is None:
            _cooldown_exclude = filename
            return
        ready = not _cooldown_reload_ready and filename == _cooldown_exclude
        if ready:
            _cooldown_reload_ready = True
    if ready:
        log(f"检测到当前歌曲 {filename} 重新载入，Cool Down 将在本次请求中替换")


class LogWatcher(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="log_watcher")
        self.current_file = None
        self.offset = 0
        self.buf = ""

    def run(self):
        log("日志监听已启动，等待 VRChat 日志...")
        last_check = 0.0
        while not _stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                log(f"日志监听出错: {e}")
            now = time.monotonic()
            if now - last_check > 30:
                last_check = now
                try:
                    _ensure_service_threads()
                except Exception as e:
                    log(f"服务线程巡检出错: {e}")
            _stop_event.wait(1.0)

    def _tick(self):
        newest = find_newest_log()
        if newest is None:
            return
        if newest != self.current_file:
            self.current_file = newest
            self.buf = ""
            log(f"监听日志文件: {os.path.basename(newest)}")
            with open(newest, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2 * 1024 * 1024))
                tail = f.read()
            self.offset = size
            # 上次会话留下的旧日志不做尾部扫描，避免预载已不在队列的歌、拿过期链接录流
            if time.time() - os.path.getmtime(newest) > 600:
                log("日志文件超过 10 分钟未更新，跳过历史内容（等待 VRChat 写入新日志）")
                return
            tail_lines = tail.splitlines()
            record_room_events(tail_lines)
            record_wd_favorites(tail_lines)
            if get_runtime_state()["youtube_available"]:
                scan_resolved_youtube(tail_lines)
            items = extract_queue_items(tail_lines)
            if items is not None:
                self._on_queue(items)
            return
        size = os.path.getsize(newest)
        if size < self.offset:
            self.offset = 0
            self.buf = ""
        if size == self.offset:
            return
        with open(newest, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self.offset)
            data = f.read()
            self.offset = f.tell()
        self.buf += data
        if "\n" not in self.buf:
            return
        lines = self.buf.split("\n")
        self.buf = lines.pop()
        record_room_events(lines)
        record_wd_favorites(lines)
        if get_runtime_state()["youtube_available"]:
            scan_resolved_youtube(lines)
        items = extract_queue_items(lines)
        if items is not None:
            self._on_queue(items)

    def _on_queue(self, items):
        global _last_queue_items
        _cooldown_queue_reloaded(items)
        record_queue_snapshot(items)
        record_playing_snapshot(items)
        with _queue_lock:
            _last_queue_items = copy.deepcopy(items or [])
        if not items:
            emit("queue", items=[])
            return
        emit("queue", items=describe_queue(items))
        submit_preload_async(items)


# ---------------- 服务启停 ----------------

class _StartCancelled(Exception):
    pass


def _spawn_service_thread(target, args=()):
    thread = threading.Thread(target=target, args=args, daemon=True,
                              name=getattr(target, "__name__", "service"))
    _service_threads.append(thread)
    thread.start()
    return thread


def _ensure_service_threads():
    """预载/落盘线程异常退出时自动重启；异常本身已由 threading.excepthook 记入日志。
    由日志监听线程定期调用（它的 run 循环捕获一切异常，是全进程最难死的线程）。"""
    if _stop_event.is_set() or not _running:
        return
    restartable = {
        "_preload_worker_loop": _preload_worker_loop,
        "_preload_download_loop": _preload_download_loop,
        "_preload_retry_loop": _preload_retry_loop,
        "_mapping_flush_loop": _mapping_flush_loop,
    }
    alive = {t.name for t in list(_service_threads) if t.is_alive()}
    for name, target in restartable.items():
        if name not in alive and not _stop_event.is_set():
            log(f"检测到后台线程 {name} 未运行，自动重启")
            _spawn_service_thread(target)


_SERVICE_TASK_LABELS = {
    "auto_speedtest_apply": "CDN 测速",
    "_speed_test_ips": "CDN 测速",
    "_full_scan_ips": "扩大范围 CDN 测速",
    "_slow_recheck": "CDN 慢速复测",
    "fetch_bundle": "歌单直取表更新",
    "_preload_worker_loop": "队列预载",
    "_preload_download_loop": "队列预载下载",
    "_preload_retry_loop": "预载重试",
    "_mapping_flush_loop": "映射保存",
    "_hosts_watchdog_loop": "hosts 恢复检查",
    "_tunnel_loop": "YouTube 隧道关闭",
    "serve_forever": "本地播放服务",
    "LogWatcher": "房间日志监听",
}


def _finishing_tasks():
    tasks = []
    with _downloads_lock:
        for key in _downloads_in_progress:
            meta = _download_meta.get(key) or {}
            filename = meta.get("filename") or os.path.basename(key)
            tasks.append(f"{filename} 下载")
    if rebuilding():
        tasks.append("曲库索引重建")
    if verifying():
        tasks.append("曲库校验")
    for thread in list(_service_threads):
        if thread.is_alive() and thread is not threading.current_thread():
            tasks.append(_SERVICE_TASK_LABELS.get(thread.name, f"后台线程 {thread.name}"))
    return list(dict.fromkeys(tasks))


def _finishing_error(tasks):
    shown = "、".join(tasks[:5])
    if len(tasks) > 5:
        shown += f" 等 {len(tasks)} 项"
    return f"仍有后台任务正在结束：{shown}，请稍后再启动服务"


def start_services():
    """串行启停，返回 None 表示成功；兼容旧 CLI 的错误字符串。"""
    global _server, _dudu_server, _watcher, _running, _tunnel_sock, _run_id, _hosts_restore_pending
    with _lifecycle_lock:
        if _running:
            return None
        if _hosts_restore_pending:
            error = "hosts 尚未成功恢复，请先重试停止服务后再启动"
            _set_runtime_state(phase="failed", running=False, hosts_restore_pending=True, error=error)
            return error
        if _same_directory(_config.get("cache_dir"), _config.get("library_dir")):
            error = "缓存目录与永久曲库相同，请先在设置中分开保存目录再启动服务"
            _set_runtime_state(phase="failed", running=False, error=error)
            return error
        if _same_directory(_config.get("cache_dir"), _config.get("wd_library_dir")):
            error = "缓存目录与 WannaDance 曲库相同，请先在设置中分开保存目录再启动服务"
            _set_runtime_state(phase="failed", running=False, error=error)
            return error
        if _same_directory(_config.get("cache_dir"), _config.get("dudu_library_dir")):
            error = "缓存目录与 DuDu 曲库相同，请先在设置中分开保存目录再启动服务"
            _set_runtime_state(phase="failed", running=False, error=error)
            return error
        finishing = _finishing_tasks()
        if finishing:
            error = _finishing_error(finishing)
            _set_runtime_state(phase="failed", running=False, error=error)
            return error
        _service_threads.clear()
        _run_id += 1
        _stop_event.clear()
        _speedtest_stop.clear()
        _library_stop.clear()
        _set_runtime_state(phase="starting", running=False, youtube_available=False,
                           queue_watcher_available=False, hosts_restore_pending=False,
                           warnings=[], error=None)
        server_started = False
        hosts_touched = False
        warnings = []
        try:
            log("正在解析真实服务器 IP...")
            resolve_real_ips()
            warnings.extend(_deferral_warnings())
            if _stop_event.is_set():
                raise _StartCancelled()
            if not _origin_ips.get("cdn.pypy.dance"):
                raise RuntimeError("无法获取 cdn.pypy.dance 真实 IP，请检查网络后重试")
            try:
                _server = ThreadingHTTPServer(("127.0.0.1", 80), DanceHandler)
            except OSError as e:
                raise RuntimeError(f"无法绑定 80 端口 ({e})，请关闭占用该端口的程序后重试") from e
            _server.daemon_threads = True
            yt_mode = bool(_config.get("enable_youtube_cache"))
            if yt_mode and not _origin_ips.get(API_HOST):
                warnings.append("无法解析 api.pypy.dance，YouTube 源缓存不可用")
                yt_mode = False
            if _config.get("dudu_enabled"):
                from dance_ui.dudu_https import ensure_certificate
                certificate, private_key = ensure_certificate()
                try:
                    _dudu_server = ThreadingHTTPServer(("127.0.0.1", 443), DanceHandler)
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(str(certificate), str(private_key))
                    _dudu_server.socket = context.wrap_socket(_dudu_server.socket, server_side=True)
                    _dudu_server.daemon_threads = True
                except OSError as e:
                    raise RuntimeError(f"无法绑定 DuDu HTTPS 443 端口 ({e})") from e
            elif yt_mode:
                try:
                    _tunnel_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    _tunnel_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    _tunnel_sock.bind(("127.0.0.1", 443))
                    _tunnel_sock.listen(64)
                    _tunnel_sock.settimeout(1)
                except OSError as e:
                    warnings.append(f"无法绑定 443 端口 ({e})，YouTube 源缓存不可用")
                    if _tunnel_sock is not None:
                        _tunnel_sock.close()
                    _tunnel_sock = None
                    yt_mode = False
            if _stop_event.is_set():
                raise _StartCancelled()
            hosts_touched = True
            # 即使 hosts 写入中途失败，也必须保留恢复责任，直到恢复真正成功。
            _hosts_restore_pending = True
            hijack_hosts(yt_mode)
            if _stop_event.is_set():
                raise _StartCancelled()
            _spawn_service_thread(_server.serve_forever)
            server_started = True
            if _dudu_server is not None:
                _spawn_service_thread(_dudu_server.serve_forever)
            _spawn_service_thread(_hosts_watchdog_loop, (yt_mode,))
            if _tunnel_sock is not None:
                _spawn_service_thread(_tunnel_loop, (_tunnel_sock, list(_origin_ips[API_HOST])))
            _spawn_service_thread(_mapping_flush_loop)
            # Passive Range requests also enqueue complete-cache jobs, even if
            # VRChat has not created its log directory yet.
            _spawn_service_thread(_preload_download_loop)
            watcher_available = os.path.isdir(VRC_LOG_DIR)
            _set_runtime_state(youtube_available=yt_mode, queue_watcher_available=watcher_available)
            if watcher_available:
                _watcher = LogWatcher()
                _service_threads.append(_watcher)
                _watcher.start()
                _spawn_service_thread(_preload_worker_loop)
                _spawn_service_thread(_preload_retry_loop)
            else:
                warnings.append("未找到 VRChat 日志目录，当前仅提供被动缓存")
            _running = True
            _reset_session_stats()
            if _config.get("bundle_direct", True):
                _spawn_service_thread(fetch_bundle)  # 一次性任务，不在自愈重启列表里
            if _config.get("auto_prefer_cdn", True):
                _spawn_service_thread(auto_speedtest_apply)
            _set_runtime_state(phase="running", running=True, youtube_available=yt_mode,
                               queue_watcher_available=watcher_available, hosts_restore_pending=True,
                               warnings=warnings, error=None)
            for warning in warnings:
                log("提示: " + warning)
            emit_stats()
            log("本地缓存服务运行中：127.0.0.1:80")
            return None
        except Exception as e:
            cancelled = isinstance(e, _StartCancelled)
            _stop_event.set()
            if _server is not None:
                try:
                    if server_started:
                        _server.shutdown()
                    _server.server_close()
                except Exception:
                    pass
                _server = None
            if _dudu_server is not None:
                try:
                    if server_started:
                        _dudu_server.shutdown()
                    _dudu_server.server_close()
                except Exception:
                    pass
                _dudu_server = None
            if _tunnel_sock is not None:
                try:
                    _tunnel_sock.close()
                except Exception:
                    pass
                _tunnel_sock = None
            _watcher = None
            _running = False
            restored = restore_hosts() if hosts_touched else True
            _hosts_restore_pending = not restored
            error = "启动已取消" if cancelled else str(e)
            if not restored:
                error += "；hosts 恢复失败，请检查日志"
            _set_runtime_state(phase="stopped" if cancelled and restored else "failed", running=False,
                               youtube_available=False, queue_watcher_available=False,
                               hosts_restore_pending=_hosts_restore_pending,
                               warnings=warnings, error=None if cancelled and restored else error)
            return error


def stop_services():
    """停止并恢复 hosts；None 表示成功，错误字符串表示需留窗重试。"""
    global _server, _dudu_server, _watcher, _running, _tunnel_sock, _run_id, _hosts_restore_pending
    global _last_queue_items, _preload_pending
    # 在等待生命周期锁之前通知启动线程取消，防止窗口关闭后又启动成功。
    _stop_event.set()
    _speedtest_stop.set()
    close_active_connections()
    with _lifecycle_lock:
        needs_restore = _hosts_restore_pending or _running
        _set_runtime_state(phase="stopping", error=None)
        _run_id += 1
        cancel_library_download()
        close_active_connections()
        cooldown_disarm(silent=True)
        if _server is not None:
            try:
                _server.shutdown()
                _server.server_close()
            except Exception as e:
                log(f"HTTP 服务关闭异常: {e}")
            _server = None
        if _dudu_server is not None:
            try:
                _dudu_server.shutdown()
                _dudu_server.server_close()
            except Exception as e:
                log(f"DuDu HTTPS 服务关闭异常: {e}")
            _dudu_server = None
        if _tunnel_sock is not None:
            try:
                _tunnel_sock.close()
            except OSError:
                pass
            _tunnel_sock = None
        restored = restore_hosts() if needs_restore else True
        _hosts_restore_pending = not restored
        flush_mapping()
        _running = False
        _watcher = None
        deadline = time.monotonic() + 2
        for thread in list(_service_threads):
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(max(0, deadline - time.monotonic()))
        error = None if restored else "服务已停止，但 hosts 恢复失败，请检查日志并重试停止服务"
        _set_runtime_state(phase="stopped" if restored else "failed", running=False,
                           youtube_available=False, queue_watcher_available=False, warnings=[],
                           hosts_restore_pending=_hosts_restore_pending, error=error)
        log("服务已停止" if restored else "服务已停止，hosts 尚未成功恢复")
        # 清空跨会话状态：残留的预载列表会在下次启动时立刻下载上一个房间的歌；
        # 残留队列会被误显示，学到新映射时还会用它误触发预载（_refresh_queue_for_mapping）
        with _queue_lock:
            _last_queue_items = []
        with _preload_lock:
            _preload_pending = None
        with _preload_dl_lock:
            _preload_dl_items[:] = []
            _preload_dl_priority.clear()
        _reset_wd_state()
        refresh_queue_state()
        return error


def configure_dudu(enabled):
    """按需启停 DuDu，并在用户明确关闭时移除本机 CA。"""
    if not isinstance(enabled, bool):
        raise ValueError("DuDu 开关必须是布尔值")
    was_running = bool(is_running())
    if was_running:
        error = stop_services()
        if error:
            raise RuntimeError(error)
    try:
        if enabled:
            from dance_ui.dudu_https import ensure_certificate
            ensure_certificate()
        update_config({"dudu_enabled": enabled})
        if not enabled:
            from dance_ui.dudu_https import remove_certificate
            remove_certificate()
        if was_running or enabled:
            error = start_services()
            if error:
                raise RuntimeError(error)
    except Exception:
        if was_running and not is_running():
            start_services()
        raise


def is_running():
    return _running


# ---------------- 命令行模式 ----------------

def _cli_listener(kind, data):
    ts = time.strftime("%H:%M:%S")
    if kind == "log":
        print(f"[{ts}] {data['msg']}", flush=True)
    elif kind == "queue":
        items = data["items"]
        if not items:
            return
        print(f"[{ts}] 当前队列:", flush=True)
        for it in items:
            print(f"[{ts}]   [{it['state']}] {it['name']}", flush=True)
    elif kind == "stats":
        line = f"[{ts}] 缓存目录: {data['count']} 个 | {data['total_mb']:.0f} MB / {data['limit_mb']} MB"
        if data.get("lib_enabled"):
            line += f" | 曲库: {data['lib_count']} 首 | {data['lib_mb']:.0f} MB"
        print(line, flush=True)


# ---------------- 诊断信息导出与版本更新检查 ----------------

UPDATE_CHECK_API = "https://api.github.com/repos/oreochromis/pypydance-video-cache/releases/latest"
UPDATE_CHECK_PAGE = "https://github.com/oreochromis/pypydance-video-cache/releases/latest"
PROJECT_PAGE = "https://github.com/oreochromis/pypydance-video-cache"


def fetch_latest_release_tag(timeout=8):
    """查询 GitHub 最新 Release 的 tag；网络失败返回 None（调用方静默忽略）。"""
    req = urllib.request.Request(UPDATE_CHECK_API, headers={
        "User-Agent": "VRChatDanceAssistant", "Accept": "application/vnd.github+json"})
    try:
        with _tracked_urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return str(tag) if tag else None


def collect_diagnostics():
    """汇总排障所需的本地状态为纯文本；路径原样保留（诊断需要），不含任何账号信息。"""
    import platform
    out = []
    add = out.append
    add(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    add(f"系统: {platform.platform()}")
    add(f"Python: {sys.version.split()[0]}")
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin()) if os.name == "nt" else False
    except Exception:
        admin = False
    add(f"管理员权限: {'是' if admin else '否（hosts 劫持与 80 端口需要管理员）'}")

    state = get_runtime_state()
    add("")
    add("== 运行状态 ==")
    for key in ("phase", "running", "youtube_available", "queue_watcher_available",
                "hosts_restore_pending", "error"):
        if key in state:
            add(f"{key}: {state[key]}")
    for warning in state.get("warnings") or []:
        add(f"警告: {warning}")

    add("")
    add("== 服务线程 ==")
    for t in list(_service_threads):
        add(f"{t.name}: {'存活' if t.is_alive() else '已退出'}")

    add("")
    add("== 配置 ==")
    for key, value in sorted(get_config().items()):
        add(f"{key}: {value}")

    add("")
    add("== hosts 劫持条目 ==")
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
            marked = [l.strip() for l in f if HOSTS_MARK in l]
    except OSError as e:
        marked = []
        add(f"hosts 读取失败: {e}")
    expected = hijack_host_list(True)
    for host in expected:
        present = any(host in l.split() for l in marked)
        add(f"{'[已劫持]' if present else '[未劫持]'} {host}")

    add("")
    add("== 本地端口 ==")
    for port in (80, 443):
        sock = socket.socket()
        sock.settimeout(1.5)
        try:
            listening = sock.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            listening = False
        finally:
            sock.close()
        add(f"127.0.0.1:{port}: {'监听中' if listening else '未监听'}")

    add("")
    add("== 缓存与曲库 ==")
    count, total, lib_count, lib_total, wd_count, wd_total, dudu_count, dudu_total = get_cache_stats()
    add(f"临时缓存: {count} 个 | {total / 1048576:.1f} MB / {get_config()['max_cache_mb']} MB")
    add(f"PyPyDance 曲库: {lib_count} 首 | {lib_total / 1048576:.1f} MB")
    add(f"WannaDance 曲库: {wd_count} 首 | {wd_total / 1048576:.1f} MB")
    add(f"DuDu FitDance 曲库: {dudu_count} 首 | {dudu_total / 1048576:.1f} MB")
    session = get_session_stats()
    add(f"本次会话: 本地命中 {session['hits']} 首 | 回源 {session['misses']} 首 | 节省流量 {session['saved_mb']:.1f} MB")

    add("")
    add("== 当前队列快照 ==")
    queue = get_queue_snapshot()
    if queue:
        for item in queue:
            add(f"[{item.get('state', '?')}] {item.get('name', '?')} (来源 {item.get('source', '?')})")
    else:
        add("（空）")

    add("")
    add("== 最近下载结果（最多 15 条） ==")
    with _downloads_lock:
        results = list(_download_results.values())[-15:]
    if results:
        for r in results:
            status = "取消" if r.get("cancelled") else ("成功" if r.get("ok") else "失败")
            line = f"{status} {r.get('filename', '?')} ({r.get('source', '?')}, {r.get('done', 0) / 1048576:.1f} MB)"
            if r.get("error"):
                line += f" 错误: {r['error']}"
            add(line)
    else:
        add("（无）")
    return "\n".join(out) + "\n"


def main():
    add_listener(_cli_listener)
    log("=== VRChat跳舞房助手（命令行模式） ===")
    log("Bilibili @鲨鲨比亚Sharkspeare")
    if os.name != "nt":
        log("本工具仅支持 Windows")
        sys.exit(1)
    ensure_admin()
    load_config()
    load_mapping()
    err = start_services()
    if err:
        log("错误: " + err)
        sys.exit(1)
    log("关闭此窗口或按 Ctrl+C 退出，退出时自动恢复 hosts。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_services()
        log("已退出")


if __name__ == "__main__":
    main()
