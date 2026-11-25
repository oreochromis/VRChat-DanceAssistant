"""Shared three-room catalog and download engine for CLI and desktop downloaders."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import dance_cache as core

ROOM_NAMES = {"pypy": "PyPyDance", "wd": "WannaDance", "dudu": "DuDu FitDance"}


def parse_song_ids(text):
    values = set()
    for token in re.split(r"[\s,，、;；]+", text.strip()):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)-(\d+)", token)
        if match:
            first, last = map(int, match.groups())
            if last < first or last - first > 20000:
                raise ValueError(f"编号范围无效：{token}")
            values.update(range(first, last + 1))
        elif token.isdigit():
            values.add(int(token))
        else:
            raise ValueError(f"无法识别编号：{token}")
    return values


def existing_song_ids(kind, directory):
    if not directory or not os.path.isdir(directory):
        return set()
    pattern = {"pypy": core.NUMERIC_MP4_RE, "wd": core.WD_CACHE_RE, "dudu": core.DUDU_CACHE_RE}[kind]
    result = set()
    for path in core._walk_library_files(directory, kind):
        if not path.lower().endswith(".mp4") or os.path.getsize(path) <= 0:
            continue
        match = pattern.fullmatch(os.path.basename(path))
        if match:
            result.add(int(match.group(1)))
    return result


def download_destination(kind, directory, song_id, classified=True):
    name = core._library_filename(kind, song_id)
    if not classified:
        return os.path.join(directory, name)
    group = core._safe_group_folder(core.library_group_name(kind, song_id))
    return os.path.join(directory, group, name)


class UnifiedDownloader:
    def __init__(self, callback=None):
        self.callback = callback or (lambda event: None)
        self._cancel_requested = False
        self._listener = None
        self._original_emit_stats = None

    def _emit(self, event_type, **data):
        self.callback({"type": event_type, **data})

    def _receive_core(self, kind, data):
        if kind == "log":
            self._emit("log", message=data.get("msg", ""))
        elif kind == "progress":
            self._emit("file_progress", **data)

    def cancel(self):
        self._cancel_requested = True
        core._stop_event.set()
        core.close_active_connections()

    def prepare(self, kind, directory):
        if kind not in ROOM_NAMES:
            raise ValueError("未知房间")
        os.makedirs(directory, exist_ok=True)
        self._cancel_requested = False
        core._config = copy.deepcopy(core.DEFAULT_CONFIG)
        core._config.update(
            cache_dir=os.path.join(core.data_dir(), "downloader-cache"),
            library_dir=directory if kind == "pypy" else "",
            wd_library_dir=directory if kind == "wd" else "",
            dudu_library_dir=directory if kind == "dudu" else "",
            cdn_permanent=False, wd_permanent=False, dudu_permanent=False,
            max_cache_mb=1024 * 1024,
        )
        os.makedirs(core._config["cache_dir"], exist_ok=True)
        core._stop_event.clear()
        core._speedtest_stop.clear()
        core._run_id += 1
        core.load_mapping()
        core.resolve_real_ips()
        for host in core.HIJACK_HOSTS + [core.UDON_API_HOST, core.UDON_PLAY_HOST,
                                        core.DUDU_API_HOST, core.DUDU_PLAY_HOST]:
            if not core._origin_ips.get(host):
                ips = [ip for ip in core.doh_lookup(host) if core._is_public_ip(ip)]
                if ips:
                    core._origin_ips[host] = ips
        if not core._origin_ips.get(core.DUDU_PLAY_HOST) and core._origin_ips.get(core.DUDU_API_HOST):
            core._origin_ips[core.DUDU_PLAY_HOST] = list(core._origin_ips[core.DUDU_API_HOST])
        if not core._origin_ips.get(core.UDON_PLAY_HOST) and core._origin_ips.get(core.UDON_API_HOST):
            core._origin_ips[core.UDON_PLAY_HOST] = list(core._origin_ips[core.UDON_API_HOST])
        self._listener = self._receive_core
        core.add_listener(self._listener)
        self._original_emit_stats = core.emit_stats
        core.emit_stats = lambda: None

    def catalog(self, kind):
        if kind == "pypy":
            status, _, body = core.api_request("/bundle")
            if status != 200:
                raise RuntimeError(f"PyPyDance 清单 HTTP {status}")
            data = json.loads(body.decode("utf-8"))
            if not core._load_bundle_data(data):
                raise RuntimeError("PyPyDance 清单为空")
            return sorted({song.get("i") for song in data.get("songs") or []
                           if isinstance(song.get("i"), int) and song.get("i") > 0})
        catalog = core._ensure_wd_catalog() if kind == "wd" else core._ensure_dudu_catalog()
        if not catalog:
            raise RuntimeError(f"{ROOM_NAMES[kind]} 清单不可用")
        return sorted(song_id for song_id, item in catalog.items()
                      if kind == "dudu" or not item.get("disable_public"))

    def _download_one(self, kind, directory, song_id, classified):
        if self._cancel_requested:
            return "cancelled"
        destination = download_destination(kind, directory, song_id, classified)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        for attempt in range(1, 4):
            if self._cancel_requested:
                return "cancelled"
            try:
                if kind == "pypy":
                    filename = core.resolve_video_filename(song_id)
                    if not filename:
                        self._emit("log", message=f"{song_id} 没有可下载的 CDN 文件，跳过")
                        return "skipped"
                    ok = core.download_file(filename, song_id, destination=destination)
                elif kind == "wd":
                    ok = core.download_wd(song_id, destination=destination)
                else:
                    ok = core.download_dudu(song_id, destination=destination)
                if ok:
                    return "done"
            except Exception as error:
                self._emit("log", message=f"{song_id} 第 {attempt} 次下载异常：{error}")
            if not self._cancel_requested and attempt < 3:
                time.sleep(attempt * 2)
        return "cancelled" if self._cancel_requested else "failed"

    def run(self, kind, directory, classified=True, threads=2, selected_ids=None):
        summary = {"done": 0, "failed": 0, "skipped": 0, "existing": 0, "cancelled": False}
        try:
            self.prepare(kind, directory)
            catalog = self.catalog(kind)
            wanted = set(selected_ids or [])
            selected = [song_id for song_id in catalog if not wanted or song_id in wanted]
            missing = wanted - set(catalog)
            for song_id in sorted(missing):
                self._emit("log", message=f"编号 {song_id} 不在当前官方清单中，跳过")
            existing = existing_song_ids(kind, directory)
            todo = [song_id for song_id in selected if song_id not in existing]
            summary["existing"] = len(selected) - len(todo)
            self._emit("log", message=f"{ROOM_NAMES[kind]}：选择 {len(selected)} 首，已存在 {summary['existing']} 首，待下载 {len(todo)} 首")
            self._emit("started", total=len(todo), existing=summary["existing"])
            if not todo:
                return summary
            completed = 0
            pool = ThreadPoolExecutor(max_workers=threads)
            futures = {pool.submit(self._download_one, kind, directory, song_id, classified): song_id
                       for song_id in todo}
            try:
                for future in as_completed(futures):
                    song_id = futures[future]
                    try:
                        result = future.result()
                    except Exception as error:
                        self._emit("log", message=f"{song_id} 下载失败：{error}")
                        result = "failed"
                    if result in ("done", "failed", "skipped"):
                        summary[result] += 1
                    completed += 1
                    self._emit("progress", completed=completed, total=len(todo), song_id=song_id,
                               result=result, summary=dict(summary))
                    if self._cancel_requested:
                        summary["cancelled"] = True
                        for pending in futures:
                            pending.cancel()
            finally:
                if self._cancel_requested:
                    core.close_active_connections()
                pool.shutdown(wait=True, cancel_futures=True)
            summary["cancelled"] = summary["cancelled"] or self._cancel_requested
            return summary
        except Exception as error:
            self._emit("log", message=f"下载任务出错：{error}")
            summary["error"] = str(error)
            return summary
        finally:
            if self._listener is not None:
                core.remove_listener(self._listener)
                self._listener = None
            if self._original_emit_stats is not None:
                core.emit_stats = self._original_emit_stats
                self._original_emit_stats = None
            core.close_active_connections()
            self._emit("finished", summary=summary)


def run_cli(kind, argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=f"{ROOM_NAMES[kind]} 曲库下载器（默认平铺）")
    parser.add_argument("--out", required=True, help="视频保存目录")
    parser.add_argument("--threads", type=int, default=2, help="并发下载数，1 到 8")
    parser.add_argument("--ids", type=int, nargs="*", help="只下载指定编号，默认全部")
    parser.add_argument("--classified", action="store_true", help="按官方分类建立子文件夹")
    parser.add_argument("--flat", action="store_true", help="平铺保存（默认，保留用于兼容旧命令）")
    args = parser.parse_args(argv)
    if not 1 <= args.threads <= 8:
        parser.error("--threads 必须在 1 到 8 之间")

    def report(event):
        if event.get("type") == "log":
            print(event.get("message", ""), flush=True)
        elif event.get("type") == "progress" and (event["completed"] % 25 == 0
                                                   or event["completed"] == event["total"]):
            summary = event["summary"]
            print(f"进度 {event['completed']}/{event['total']}，完成 {summary['done']}，失败 {summary['failed']}", flush=True)

    summary = UnifiedDownloader(report).run(kind, args.out, classified=args.classified and not args.flat,
                                            threads=args.threads, selected_ids=set(args.ids or []))
    print(summary, flush=True)
    return 1 if summary.get("error") or summary.get("failed") else 0
