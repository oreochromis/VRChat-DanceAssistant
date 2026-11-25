"""Persistent permanent-library video compression with process-level pause support."""
from __future__ import annotations

import copy
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from fractions import Fraction
from pathlib import Path
from statistics import median
from types import SimpleNamespace

import dance_cache as core

ENCODER_NAMES = {"cpu": "CPU x264", "nvenc": "NVIDIA NVENC", "qsv": "Intel QSV", "amf": "AMD AMF"}
QUALITY_PRESETS = {
    "quality": {"cpu": 28, "nvenc": 35, "qsv": 29, "amf": 33},
    "volume": {"cpu": 30, "nvenc": 37, "qsv": 31, "amf": 35},
}
QUALITY_PARAMETER_NAMES = {"cpu": "CRF", "nvenc": "CQ", "qsv": "ICQ", "amf": "QP"}
MIN_SAVING_RATIO = 0.25
SAVE_INTERVAL = 5.0
DETAIL_LIMIT = 100


def _normalize_quality_mode(value):
    if value in ("quality", "volume", "auto"):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in (24, 26, 28, 30):
        return "volume" if value == 30 else "quality"
    raise ValueError("未知压缩档位")


def _encoder_quality(encoder, value):
    return QUALITY_PRESETS[value][encoder] if value in QUALITY_PRESETS else value


def _quality_text(encoder, value):
    if value in QUALITY_PRESETS:
        return "质量档" if value == "quality" else "体积档"
    return f"{QUALITY_PARAMETER_NAMES[encoder]} {value}"


def _binary(name):
    candidates = []
    import shutil
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    root = Path(__file__).resolve().parents[1]
    candidates.extend(sorted((root / "tools" / "ffmpeg-bin").glob(f"*/bin/{name}.exe"), reverse=True))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "_internal" / "ffmpeg" / f"{name}.exe")
    candidates.append(root / "assets" / "ffmpeg" / f"{name}.exe")
    for path in candidates:
        if path.is_file():
            return str(path)
    raise RuntimeError(f"未找到 {name}，请先配置 FFmpeg 压缩组件。")


def _vrchat_running():
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq VRChat.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        return "vrchat.exe" in (result.stdout or "").lower()
    except (OSError, subprocess.SubprocessError):
        return False


def _suspend_process(pid, pause):
    if os.name != "nt":
        raise RuntimeError("当前系统不支持进程暂停。")
    handle = None
    open_process = ctypes.windll.kernel32.OpenProcess
    open_process.restype = ctypes.c_void_p
    try:
        handle = open_process(0x0800, False, pid)
        if not handle:
            raise RuntimeError("无法打开压缩进程以暂停或恢复。")
        function = ctypes.windll.ntdll.NtSuspendProcess if pause else ctypes.windll.ntdll.NtResumeProcess
        if function(handle) != 0:
            raise RuntimeError("暂停或恢复压缩进程失败。")
    finally:
        if handle:
            close_handle = ctypes.windll.kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle(handle)


def _spawn(command):
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class CompressionManager:
    def __init__(self, listener=None, process_factory=None, game_running=None, state_path=None):
        self.listener = listener or (lambda kind, data: None)
        self.process_factory = process_factory or _spawn
        self.game_running = game_running or _vrchat_running
        self.state_path = Path(state_path) if state_path else Path(core.data_dir()) / "compression-queue.json"
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._items = []
        self._current = None
        self._process = None
        self._active_items = {}
        self._processes = {}
        self._thread = None
        self._active = False
        self._monitor_thread = None
        self._monitor_stop = False
        self._stop = False
        self._manual_pause = False
        self._game_pause = False
        self._auto_game = True
        self._encoder = "cpu"
        self._crf = "volume"
        self._parallel = 1
        self._full_encoders = ["cpu", "nvenc", "amf", "qsv"]
        self._kind = "all"
        self._encoder_checks = {}
        self._progress_time = 0.0
        self._session_details = []
        self._session_completed_bytes = 0
        self._session_completed_work = 0.0
        self._started_at = 0.0
        self._paused_seconds = 0.0
        self._pause_began = None
        self._completion_history = deque(maxlen=32)
        self._last_save_time = 0.0
        self._next_item_index = 0
        self._session_candidates = set()
        self._session_ids = set()
        self._load()

    def _load(self):
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            items = value if isinstance(value, list) else value.get("items", [])
            if isinstance(items, list):
                self._items = [item for item in items if isinstance(item, dict) and item.get("path")]
        except (OSError, ValueError, TypeError):
            self._items = []
        for item in self._items:
            if item.get("status") == "compressing":
                item.update(status="queued", progress=0, message="上次中断，等待重新压缩")
                source = Path(item["path"])
                for temporary in source.parent.glob(f".{source.stem}.compressing-*.mp4"):
                    if temporary.is_file():
                        try:
                            temporary.unlink()
                        except OSError:
                            pass
        self._save(force=True)

    def _save(self, force=False):
        with self._lock:
            now = time.monotonic()
            if not force and self._last_save_time and now - self._last_save_time < SAVE_INTERVAL:
                return
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": 1, "items": self._items}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.state_path)
            self._last_save_time = now

    def _emit(self, kind="state", **data):
        payload = self.snapshot()
        payload.update(data)
        self.listener(kind, payload)

    def _elapsed_seconds(self, now=None):
        if not self._started_at:
            return 0.0
        now = time.monotonic() if now is None else now
        paused = self._paused_seconds
        if self._pause_began is not None:
            paused += max(0.0, now - self._pause_began)
        return max(0.0, now - self._started_at - paused)

    def _sync_pause_clock(self):
        if not self._started_at:
            return
        now = time.monotonic()
        paused = self._manual_pause or self._game_pause
        if paused and self._pause_began is None:
            self._pause_began = now
        elif not paused and self._pause_began is not None:
            self._paused_seconds += max(0.0, now - self._pause_began)
            self._pause_began = None

    @staticmethod
    def _work_units(item, bytes_per_second=None):
        duration = float(item.get("duration") or 0)
        if duration > 0:
            return duration
        size = float(item.get("original_size") or 0)
        return size / (bytes_per_second or 1048576)

    def _work_totals(self):
        items = [item for item in self._items
                 if id(item) in self._session_ids
                 and (self._kind == "all" or item.get("kind") == self._kind)]
        active_items = [item for item in self._active_items.values()
                        if self._kind == "all" or item.get("kind") == self._kind]
        ratios = [float(item["original_size"]) / float(item["duration"]) for item in items
                  if float(item.get("original_size") or 0) > 0 and float(item.get("duration") or 0) > 0]
        bytes_per_second = median(ratios) if ratios else None
        work = lambda item: self._work_units(item, bytes_per_second)
        completed = [item for item in items
                     if item.get("status") == "done"
                     or (item.get("status") in ("skipped", "failed") and item.get("encode_attempted"))]
        processed = sum(work(item) for item in completed) + sum(
            work(item) * (item.get("progress") or 0) / 100 for item in active_items)
        remaining = sum(work(item) for item in items if item.get("status") == "queued") + sum(
            work(item) * (1 - (item.get("progress") or 0) / 100) for item in active_items)
        return processed, remaining

    def _record_completion(self, item):
        self._session_completed_work += self._work_units(item)
        active_processed = sum(
            self._work_units(active) * (active.get("progress") or 0) / 100
            for active in self._active_items.values()
            if self._kind == "all" or active.get("kind") == self._kind)
        self._completion_history.append((self._elapsed_seconds(), self._session_completed_work + active_processed))

    def snapshot(self):
        with self._lock:
            items = [item for item in self._items
                     if id(item) in self._session_ids
                     and (self._kind == "all" or item.get("kind") == self._kind)]
            counts = {}
            for item in items:
                status = item.get("status", "queued")
                counts[status] = counts.get(status, 0) + 1
            active = bool(self._active and self._thread and self._thread.is_alive())
            phase = ("manual_paused" if active and self._manual_pause
                     else "game_paused" if active and self._game_pause
                     else "running" if active else "idle")
            active_items = [item for item in self._active_items.values()
                            if self._kind == "all" or item.get("kind") == self._kind]
            current = active_items[0] if active_items else None
            processed, remaining = self._work_totals()
            elapsed = self._elapsed_seconds()
            overall_rate = processed / elapsed if elapsed > 0 and processed > 0 else None
            cutoff = elapsed - 600
            while len(self._completion_history) > 2 and self._completion_history[0][0] < cutoff:
                self._completion_history.popleft()
            recent_rate = None
            recent_elapsed = 0.0
            if len(self._completion_history) >= 2:
                started, completed = self._completion_history[0]
                recent_elapsed = elapsed - started
                recent_processed = processed - completed
                if recent_elapsed >= 15 and recent_processed > 0:
                    recent_rate = recent_processed / recent_elapsed
            if recent_rate and overall_rate:
                recent_weight = min(1.0, recent_elapsed / 300) * min(1.0, (len(self._completion_history) - 1) / 8)
                rate = overall_rate * (1 - recent_weight) + recent_rate * recent_weight
            else:
                rate = recent_rate or overall_rate
            eta = round(remaining / rate) if phase == "running" and rate and remaining > 0 else None
            details = [item for item in self._session_details
                       if self._kind == "all" or item.get("kind") == self._kind]
            return {
                "phase": phase, "auto_game": self._auto_game, "encoder": self._encoder,
                "kind": self._kind, "parallel": self._parallel, "eta_seconds": eta,
                "current": copy.deepcopy(current), "current_items": copy.deepcopy(active_items),
                "counts": counts, "items": copy.deepcopy(items[-20:]),
                "details": copy.deepcopy(details[-DETAIL_LIMIT:]), "detail_total": len(details),
                "queue_path": str(self.state_path),
            }

    def records(self):
        with self._lock:
            return copy.deepcopy(self._items)

    def _should_preserve_skipped(self, item, stat):
        if item is None:
            return False
        if item.get("status") != "skipped" or "未达到 25% 收益" not in str(item.get("message") or ""):
            return False
        if (item.get("original_size") != stat.st_size
                or item.get("original_mtime_ns") != stat.st_mtime_ns):
            return False
        quality_mode = item.get("quality_mode")
        if quality_mode is not None:
            if self._encoder in ENCODER_NAMES and item.get("encoder") != self._encoder:
                return False
            return (quality_mode == self._crf
                    and item.get("crf") == _encoder_quality(item.get("encoder") or "cpu", self._crf))
        encoder = item.get("encoder") or "cpu"
        if self._crf == "auto" and "CRF 24–30" in str(item.get("message") or ""):
            return True
        expected = _encoder_quality(encoder, self._crf)
        parameter = QUALITY_PARAMETER_NAMES[encoder]
        match = re.search(rf"\b{re.escape(parameter)}\s+(\d+(?:\.\d+)?)", str(item.get("message") or ""))
        return bool(match and float(match.group(1)) == float(expected))

    def enqueue(self, kind="all", selected_paths=None):
        if (kind not in ("all", "pypy", "wd", "dudu")
                or (selected_paths is not None and (kind == "all") != isinstance(selected_paths, dict))):
            raise ValueError("指定文件压缩需要按曲库提供文件列表")
        kinds = ("pypy", "wd", "dudu") if kind == "all" else (kind,)
        config = core.get_config()
        config_names = {"pypy": "library_dir", "wd": "wd_library_dir", "dudu": "dudu_library_dir"}
        added = 0
        with self._lock:
            self._kind = kind
            self._session_candidates = set()
            self._session_ids = set()
            known = {os.path.normcase(os.path.normpath(item["path"])): item
                     for item in self._items if item.get("path")}
            for source in kinds:
                directory = config.get(config_names[source]) or ""
                if not directory or not os.path.isdir(directory):
                    continue
                candidates = (core._walk_library_files(directory, source) if selected_paths is None
                              else selected_paths.get(source, []) if isinstance(selected_paths, dict)
                              else selected_paths)
                for file_path in candidates:
                    file_path = os.path.abspath(os.fspath(file_path))
                    if (selected_paths is not None
                            and (not core._path_in_directory(file_path, directory)
                                 or not core._owned_directory_file(os.path.basename(file_path), source))):
                        raise ValueError(f"所选文件不属于当前曲库：{file_path}")
                    if not file_path.lower().endswith(".mp4") or not os.path.isfile(file_path):
                        continue
                    path = os.path.normcase(os.path.normpath(file_path))
                    stat = os.stat(file_path)
                    item = known.get(path)
                    if self._should_preserve_skipped(item, stat):
                        self._session_candidates.add(id(item))
                        continue
                    if item is not None and item.get("status") in ("failed", "skipped", "cancelled"):
                        source_path = Path(file_path)
                        for stale in source_path.parent.glob(f".{source_path.stem}.compressing-*.mp4"):
                            self._cleanup_temporary(str(stale))
                    marked = core.compressed_filename(file_path) == os.path.basename(file_path)
                    if marked:
                        if item is None:
                            item = {"id": uuid.uuid4().hex, "path": file_path, "kind": source}
                            self._items.append(item)
                            known[path] = item
                            item.update(status="done", output_size=stat.st_size,
                                        output_mtime_ns=stat.st_mtime_ns,
                                        message="检测到 _compressed 标记，已跳过重复压缩")
                            self._session_candidates.add(id(item))
                            continue
                        if (item.get("status") == "done"
                                and item.get("output_mtime_ns") == stat.st_mtime_ns
                                and item.get("output_size") == stat.st_size):
                            self._session_candidates.add(id(item))
                            continue
                        stem, extension = os.path.splitext(file_path)
                        unmarked_path = stem[:-len(core.COMPRESSED_SUFFIX)] + extension
                        if os.path.exists(unmarked_path):
                            raise RuntimeError(f"撤销已压缩标记时发生同名冲突：{unmarked_path}")
                        os.replace(file_path, unmarked_path)
                        core._library_index_renamed(source, file_path, unmarked_path)
                        file_path = unmarked_path
                        item["path"] = file_path
                    if item is not None:
                        unchanged = (item.get("status") == "done"
                                     and item.get("output_mtime_ns") == stat.st_mtime_ns
                                     and item.get("output_size") == stat.st_size)
                        if unchanged:
                            marked_path = os.path.join(os.path.dirname(file_path), core.compressed_filename(file_path))
                            if os.path.exists(marked_path):
                                raise RuntimeError(f"已压缩标记改名冲突：{marked_path}")
                            os.replace(file_path, marked_path)
                            core._library_index_renamed(source, file_path, marked_path)
                            item["path"] = marked_path
                            known[os.path.normcase(os.path.normpath(marked_path))] = item
                            self._session_candidates.add(id(item))
                            continue
                        previous = item.get("status")
                        message = ("检测到文件已更新，等待重新压缩" if previous == "done"
                                   else "等待重试" if previous in ("failed", "skipped", "cancelled")
                                   else "等待压缩")
                        retry_status = item.get("previous_status") if previous == "queued" else previous
                        for key in ("output_size", "output_mtime_ns", "duration", "crf",
                                    "encoder", "codec", "compressed_at", "encode_attempted", "phase",
                                    "analysis_seconds", "encode_seconds", "validate_seconds", "terminal_at"):
                            item.pop(key, None)
                        item.update(path=file_path, kind=source,
                                    original_size=stat.st_size, original_mtime_ns=stat.st_mtime_ns,
                                    status="queued", progress=0, previous_status=retry_status, message=message)
                        self._session_candidates.add(id(item))
                        added += int(previous != "queued")
                        continue
                    item = {
                        "id": uuid.uuid4().hex, "path": file_path, "kind": source,
                        "original_size": stat.st_size, "original_mtime_ns": stat.st_mtime_ns,
                        "status": "queued", "progress": 0, "message": "等待压缩",
                    }
                    self._items.append(item)
                    known[path] = item
                    self._session_candidates.add(id(item))
                    added += 1
            self._session_ids = {id(item) for item in self._items
                                 if id(item) in self._session_candidates and item.get("status") == "queued"}
            self._save(force=True)
        self._emit()
        return added

    def start(self, kind="all", encoder="cpu", auto_game=True, crf="volume", parallel=1, full_encoders=None):
        if kind not in ("all", "pypy", "wd", "dudu"):
            raise ValueError("未知曲库来源")
        if encoder not in ENCODER_NAMES and encoder not in ("auto", "full"):
            raise ValueError("未知压缩引擎")
        selected = list(full_encoders) if full_encoders is not None else ["cpu", "nvenc", "amf", "qsv"]
        if encoder == "full" and (not selected or any(value not in ENCODER_NAMES for value in selected)):
            raise ValueError("满负载模式至少需要一个有效的压缩引擎")
        mode = _normalize_quality_mode(crf)
        if isinstance(parallel, bool) or not isinstance(parallel, int) or not 1 <= parallel <= 8:
            raise ValueError("并行任务数必须是 1 到 8")
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._encoder = encoder
            self._full_encoders = selected
            self._crf = mode
            self._parallel = parallel
            self._auto_game = bool(auto_game)
            self._kind = kind
            self._stop = False
            self._manual_pause = False
            self._game_pause = self._auto_game and self.game_running()
            self._session_details = []
            self._session_completed_bytes = 0
            self._session_completed_work = 0.0
            self._started_at = time.monotonic()
            self._paused_seconds = 0.0
            self._pause_began = self._started_at if self._game_pause else None
            self._completion_history.clear()
            self._active_items.clear()
            self._current = None
            eligible = [item for item in self._items
                        if (kind == "all" or item.get("kind") == kind)
                        and (not self._session_candidates or id(item) in self._session_candidates)]
            self._session_ids = {id(item) for item in eligible if item.get("status") == "queued"}
            self._next_item_index = 0
            if not self._session_ids:
                self._save(force=True)
                self._thread = None
                self._active = False
                self._emit()
                return False
            self._thread = threading.Thread(target=self._run, daemon=True, name="library-compression")
            self._active = True
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                self._active = False
                raise
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_stop = False
            self._monitor_thread = threading.Thread(target=self._monitor, daemon=True, name="compression-game-monitor")
            self._monitor_thread.start()
        self._emit()
        return True

    def _monitor(self):
        last = self.game_running()
        while not self._monitor_stop:
            time.sleep(2)
            if self._monitor_stop:
                return
            current = self.game_running()
            if current != last:
                last = current
                with self._lock:
                    if self._auto_game:
                        self._game_pause = current
                        self._sync_pause_clock()
                        if current:
                            self._pause_process()
                        elif not self._manual_pause:
                            self._resume_process()
                        self._condition.notify_all()
                self._emit()

    def pause(self):
        with self._lock:
            self._manual_pause = True
            self._sync_pause_clock()
            self._pause_process()
            self._condition.notify_all()
        self._emit()

    def resume(self):
        with self._lock:
            self._manual_pause = False
            self._sync_pause_clock()
            if not self._game_pause:
                self._resume_process()
            self._condition.notify_all()
        self._emit()

    def _registered_processes(self):
        processes = list(self._processes.values())
        if self._process is not None and all(self._process is not process for process in processes):
            processes.append(self._process)
        return processes

    def _track_process(self, process):
        with self._lock:
            self._processes[id(process)] = process
            self._process = process

    def _untrack_process(self, process):
        with self._lock:
            self._processes.pop(id(process), None)
            if self._process is process:
                self._process = next(iter(self._processes.values()), None)

    def _pause_process(self):
        for process in self._registered_processes():
            if process.poll() is None:
                _suspend_process(process.pid, True)

    def _resume_process(self):
        for process in self._registered_processes():
            if process.poll() is None:
                _suspend_process(process.pid, False)

    def cancel(self):
        with self._lock:
            self._stop = True
            processes = self._registered_processes()
            try:
                self._resume_process()
            except Exception:
                pass
            for process in processes:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
            active = list(self._active_items.values())
            if self._current is not None and all(self._current is not item for item in active):
                active.append(self._current)
            for item in active:
                item.update(status="cancelled", message="已取消，原文件未修改")
            self._save(force=True)
            self._condition.notify_all()
        self._emit()

    def shutdown(self):
        self._monitor_stop = True
        self.cancel()
        with self._lock:
            thread = self._thread
            monitor = self._monitor_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(10)
        if monitor and monitor.is_alive() and monitor is not threading.current_thread():
            monitor.join(3)
        self._save(force=True)

    def _run(self):
        workers = []
        try:
            if self._encoder == "full":
                available = [encoder for encoder in ("cpu", "nvenc", "amf", "qsv")
                             if encoder in self._full_encoders and self._encoder_available(encoder)]
                if not available:
                    core.log("满负载压缩未启动：所选硬件当前不可用。")
                    return
                assignments = [available[index % len(available)] for index in range(self._parallel)]
                core.log("满负载压缩资源：" + " + ".join(ENCODER_NAMES[encoder] for encoder in assignments))
            else:
                assignments = [None] * self._parallel
            for index, assigned_encoder in enumerate(assignments):
                worker = threading.Thread(target=self._run_worker, args=(assigned_encoder,), daemon=True,
                                          name=f"library-compression-{assigned_encoder}-{index + 1}")
                workers.append(worker)
                worker.start()
            for worker in workers:
                worker.join()
        finally:
            with self._lock:
                self._current = None
                self._active_items.clear()
                self._processes.clear()
                self._process = None
                self._active = False
                self._save(force=True)
            self._emit()

    def _next_queued_item(self):
        selected = None
        for index, item in enumerate(self._items):
            if (id(item) not in self._session_ids or item.get("status") != "queued"
                    or (self._kind != "all" and item.get("kind") != self._kind)):
                continue
            previous = item.get("previous_status")
            priority = 2 if previous == "skipped" else 1 if previous in ("failed", "cancelled", "done") else 0
            if selected is None or (priority, index) < selected[:2]:
                selected = (priority, index, item)
        if selected is not None:
            self._next_item_index = selected[1] + 1
            return selected[2]
        self._next_item_index = len(self._items)
        return None

    def _run_worker(self, assigned_encoder=None):
        while True:
            with self._condition:
                while not self._stop and (self._manual_pause or self._game_pause):
                    self._condition.wait(0.5)
                if self._stop:
                    return
                item = self._next_queued_item()
                if item is None:
                    return
                self._active_items[id(item)] = item
                self._current = item
                item.pop("terminal_at", None)
                item.update(status="compressing", progress=0, message="正在分析视频",
                            phase="analyzing", encode_attempted=False,
                            analysis_seconds=0.0, encode_seconds=0.0, validate_seconds=0.0)
                self._save(force=True)
            self._emit()
            try:
                self._compress_item(item, assigned_encoder)
            except Exception as exc:
                with self._lock:
                    item.update(status="cancelled" if self._stop else "failed",
                                message="已取消，原文件未修改" if self._stop else str(exc) or type(exc).__name__)
            with self._lock:
                item["terminal_at"] = time.time()
                item["previous_status"] = item["status"]
                if item.get("status") in ("done", "skipped"):
                    item["phase"] = "complete"
                self._active_items.pop(id(item), None)
                self._current = next(iter(self._active_items.values()), None)
                self._session_details.append(copy.deepcopy(item))
                if item.get("status") == "done":
                    self._session_completed_bytes += item.get("original_size") or 0
                if item.get("status") == "done" or (
                        item.get("status") in ("skipped", "failed") and item.get("encode_attempted")):
                    self._record_completion(item)
                self._save(force=True)
                status = item.get("status")
                if status in ("skipped", "failed"):
                    core.log(f"曲库压缩{'跳过' if status == 'skipped' else '失败'}：{item.get('path')}\n{item.get('message', '')}")
            self._emit()

    def _temporary_path(self, source):
        return str(Path(source).with_name(f".{Path(source).stem}.compressing-{uuid.uuid4().hex}.mp4"))

    @staticmethod
    def _cleanup_temporary(path):
        for attempt in range(6):
            try:
                os.remove(path)
                return True
            except FileNotFoundError:
                return True
            except OSError as error:
                if attempt == 5:
                    core.log(f"临时文件仍被占用，保留待下次清理：{path}（{error}）")
                    return False
                time.sleep(0.25 * (attempt + 1))
        return False

    def _busy_reason(self, source):
        canonical = core._canonical_path(source)
        with core._downloads_lock:
            if canonical in core._downloads_in_progress:
                return "文件正在下载，已跳过"
        if core.rebuilding() or core.verifying():
            return "曲库维护任务进行中，已跳过"
        if core._cooldown_path_in_use(source):
            return "此文件正在用于 CoolDown，已跳过"
        queue = core.get_queue_snapshot()
        if queue and os.path.basename(queue[0].get("filename") or "") == os.path.basename(source):
            return "当前歌曲正在播放，已跳过"
        return None

    def _compress_item(self, item, assigned_encoder=None):
        source = item["path"]
        busy = self._busy_reason(source)
        if busy:
            item.update(status="skipped", message=busy)
            self._save(force=True)
            return
        before_size = os.path.getsize(source)
        before_mtime = os.stat(source).st_mtime_ns
        analysis_started = time.monotonic()
        try:
            metadata = self._probe(source, item)
        finally:
            item["analysis_seconds"] = item.get("analysis_seconds", 0.0) + time.monotonic() - analysis_started
        item["duration"] = metadata["duration"]
        item["phase"] = "encoding"
        encoder = assigned_encoder or self._select_encoder(item)
        encoders = [encoder]
        if self._encoder in ("auto", "full") and encoder != "cpu":
            encoders.append("cpu")
        encoder_index = 0
        for quality in ((24, 26, 28, 30) if self._crf == "auto" else (self._crf,)):
            item["message"] = (f"正在压缩 {ENCODER_NAMES[encoder]} · {_quality_text(encoder, quality)}"
                               if self._encoder == "full" else f"正在压缩 {_quality_text(encoder, quality)}")
            self._save(force=True)
            while True:
                temporary = self._temporary_path(source)
                try:
                    encode_started = time.monotonic()
                    item["encode_attempted"] = True
                    try:
                        encoder_index = self._encode_with_fallback(
                            encoders, encoder_index, source, temporary, quality, item, metadata.get("audio_codec"))
                    finally:
                        item["encode_seconds"] = item.get("encode_seconds", 0.0) + time.monotonic() - encode_started
                    if encoder_index is None:
                        encoder_index = 1
                        continue
                    if self._stop:
                        item.update(status="cancelled", message="已取消，原文件未修改")
                        self._save(force=True)
                        return
                    item.update(phase="validating", progress=95, message="正在校验输出")
                    self._save(force=True)
                    self._emit()
                    validate_started = time.monotonic()
                    try:
                        self._validate_output(source, temporary, metadata, item)
                    finally:
                        item["validate_seconds"] = item.get("validate_seconds", 0.0) + time.monotonic() - validate_started
                    item["output_size"] = os.path.getsize(temporary)
                    item["saving_ratio"] = 1 - item["output_size"] / before_size
                    if item["output_size"] > int(before_size * (1 - MIN_SAVING_RATIO)):
                        break
                    if os.path.getsize(source) != before_size or os.stat(source).st_mtime_ns != before_mtime:
                        raise RuntimeError("原文件在压缩期间发生变化，已取消替换")
                    if core._cooldown_path_in_use(source):
                        item.update(status="skipped", message="此文件正在用于 CoolDown，已跳过，原文件未修改")
                        self._save(force=True)
                        return
                    marked_path = os.path.join(os.path.dirname(source), core.compressed_filename(source))
                    if os.path.normcase(marked_path) != os.path.normcase(source) and os.path.exists(marked_path):
                        raise RuntimeError(f"已压缩标记改名冲突：{marked_path}")
                    os.replace(temporary, source)
                    if os.path.normcase(marked_path) != os.path.normcase(source):
                        os.replace(source, marked_path)
                        core._library_index_renamed(item.get("kind"), source, marked_path)
                    final_size = os.path.getsize(marked_path)
                    actual_encoder = encoders[encoder_index]
                    actual_quality = _encoder_quality(actual_encoder, quality)
                    parameter = f"{QUALITY_PARAMETER_NAMES[actual_encoder]} {actual_quality}"
                    message = f"压缩完成（{parameter}），节省 {before_size - final_size} 字节"
                    if encoder_index > 0:
                        message = "GPU 编码失败，已改用 CPU x264；" + message
                    item.update(path=marked_path, status="done", progress=100, output_size=final_size,
                                output_mtime_ns=os.stat(marked_path).st_mtime_ns,
                                crf=actual_quality, quality_mode=self._crf, encoder=actual_encoder,
                                codec="h264", compressed_at=time.time(), message=message)
                    self._save(force=True)
                    return
                finally:
                    self._cleanup_temporary(temporary)
        actual_encoder = encoders[encoder_index]
        actual_quality = _encoder_quality(actual_encoder, self._crf)
        qualities = "CRF 24–30" if self._crf == "auto" else (
            f"{_quality_text(actual_encoder, self._crf)}（{QUALITY_PARAMETER_NAMES[actual_encoder]} {actual_quality}）")
        output_size = item.get("output_size")
        if output_size:
            change = (1 - output_size / before_size) * 100
            size_text = (f"；原文件 {before_size / 1048576:.1f} MB，输出 {output_size / 1048576:.1f} MB，"
                         f"{'缩小' if change >= 0 else '增大'} {abs(change):.1f}%")
        else:
            size_text = ""
        item.update(status="skipped", progress=100, quality_mode=self._crf,
                    crf=actual_quality, encoder=actual_encoder,
                    message=f"{qualities}{size_text}；未达到 25% 收益，已保留原文件")
        self._save(force=True)

    def _encode_with_fallback(self, encoders, encoder_index, source, temporary, quality, item, audio_codec):
        try:
            actual_quality = _encoder_quality(encoders[encoder_index], quality)
            self._encode_once(encoders[encoder_index], source, temporary, actual_quality, item, audio_codec)
            return encoder_index
        except Exception as error:
            if self._stop or encoder_index + 1 >= len(encoders):
                raise
            failed_name = ENCODER_NAMES[encoders[encoder_index]]
            item["message"] = f"{failed_name} 编码失败，改用 CPU x264：{str(error)[-300:]}"
            self._save(force=True)
            return None

    def _encode_once(self, encoder, source, temporary, quality, item, audio_codec=None):
        command = self._command(encoder, source, temporary, quality, audio_codec)
        process = self.process_factory(command)
        self._track_process(process)
        try:
            with self._lock:
                if self._manual_pause or self._game_pause:
                    self._pause_process()
            for line in process.stdout:
                self._process_progress(item, line)
            return_code = process.wait()
        finally:
            stdout = getattr(process, "stdout", None)
            if hasattr(stdout, "close"):
                stdout.close()
            self._untrack_process(process)
        if self._stop:
            return
        if return_code != 0:
            raise RuntimeError(f"FFmpeg 压缩失败（退出码 {return_code}）")

    def _run_process(self, command, timeout, text=False):
        options = {}
        if text:
            options = {"text": True, "encoding": "utf-8", "errors": "replace"}
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **options)
        self._track_process(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return SimpleNamespace(returncode=process.returncode, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.communicate()
            raise
        finally:
            for stream in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
                if hasattr(stream, "close"):
                    stream.close()
            self._untrack_process(process)

    def _process_progress(self, item, line):
        if "out_time_ms=" in line:
            try:
                elapsed = int(line.split("out_time_ms=", 1)[1].strip()) / 1_000_000
                duration = float(item.get("duration") or 0)
                if duration > 0:
                    item["progress"] = min(95, max(5, round(5 + elapsed / duration * 90, 1)))
            except (ValueError, TypeError):
                pass
        elif line.startswith("progress=end"):
            item["progress"] = 95
        elif "total_size=" in line:
            try:
                item["output_size"] = int(line.split("total_size=", 1)[1].strip())
            except ValueError:
                pass
        now = time.monotonic()
        if now - self._progress_time < 0.75 and not line.startswith("progress=end"):
            return
        self._progress_time = now
        with self._lock:
            self._save()
        self._emit()

    def _select_encoder(self, item):
        if self._encoder not in ("auto", "full"):
            self._encoder_available(self._encoder, item)
            return self._encoder
        for encoder in ("nvenc", "qsv", "amf"):
            if self._encoder_available(encoder, item):
                return encoder
        return "cpu"

    def available_encoders(self):
        return ["cpu"] + [encoder for encoder in ("nvenc", "amf", "qsv")
                          if self._encoder_available(encoder, strict=False)]

    def _encoder_available(self, encoder, item=None, strict=None):
        if strict is None:
            strict = self._encoder not in ("auto", "full")
        if encoder == "cpu":
            return True
        if encoder in self._encoder_checks:
            return self._encoder_checks[encoder]
        command = [_binary("ffmpeg"), "-hide_banner", "-loglevel", "error",
                   "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2"]
        if encoder == "nvenc":
            command += ["-c:v", "h264_nvenc"]
        elif encoder == "qsv":
            command += ["-c:v", "h264_qsv"]
        elif encoder == "amf":
            command += ["-c:v", "h264_amf"]
        else:
            raise ValueError("未知压缩引擎")
        try:
            result = self._run_process(command + ["-f", "null", "-"], 30)
        except (OSError, subprocess.SubprocessError) as error:
            if strict:
                raise RuntimeError(f"{ENCODER_NAMES[encoder]} 硬件探测失败：{error}") from error
            self._encoder_checks[encoder] = False
            return False
        available = result.returncode == 0
        self._encoder_checks[encoder] = available
        if not available and strict:
            raise RuntimeError(f"{ENCODER_NAMES[encoder]} 当前不可用：{result.stderr.decode('utf-8', errors='replace')[-500:]}")
        return available

    def _command(self, encoder, source, temporary, quality=24, audio_codec=None):
        command = [_binary("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-i", source,
                   "-map", "0:v:0", "-map", "0:a?"]
        if encoder == "nvenc":
            command += ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(quality)]
        elif encoder == "qsv":
            command += ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(quality)]
        elif encoder == "amf":
            command += ["-c:v", "h264_amf", "-quality", "2", "-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality)]
        else:
            command += ["-c:v", "libx264", "-preset", "medium", "-crf", str(quality)]
        command += ["-pix_fmt", "yuv420p"]
        if audio_codec == "aac":
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "aac", "-b:a", "128k"]
        command += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", temporary]
        return command

    def _probe(self, path, item=None):
        result = self._run_process([
            _binary("ffprobe"), "-v", "error", "-count_frames", "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,nb_read_frames,duration",
            "-of", "json", path], 600, text=True)
        if result.returncode != 0:
            raise RuntimeError("无法读取视频信息：" + (result.stderr or "")[-500:])
        streams = json.loads(result.stdout).get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if not video:
            raise RuntimeError("文件不包含视频轨道")
        frames = video.get("nb_read_frames") or video.get("nb_frames")
        return {
            "width": int(video["width"]), "height": int(video["height"]),
            "rate": video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0",
            "frames": int(frames) if frames else None,
            "duration": float(video.get("duration") or 0),
            "audio_codec": audio.get("codec_name") if audio else None,
        }

    def _validate_output(self, source, temporary, original, item=None):
        output = self._probe(temporary, item)
        if (output["width"], output["height"]) != (original["width"], original["height"]):
            raise RuntimeError("压缩后分辨率发生变化")
        if original["frames"] is not None and output["frames"] != original["frames"]:
            raise RuntimeError("压缩后实际帧数不一致")
        if original["rate"] not in ("0/0", "0/1") and output["rate"] not in ("0/0", "0/1"):
            if abs(Fraction(original["rate"]) - Fraction(output["rate"])) > Fraction(1, 1000):
                raise RuntimeError("压缩后帧率发生变化")
        if abs(output["duration"] - original["duration"]) > 0.25:
            raise RuntimeError("压缩后视频时长不一致")
        result = self._run_process([_binary("ffmpeg"), "-v", "error", "-i", temporary, "-f", "null", "-"], 3600)
        if result.returncode != 0:
            raise RuntimeError("压缩结果无法完整解码")
