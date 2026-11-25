import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import dance_cache as core
from dance_ui import compression


class FakeProcess:
    def __init__(self, command, content):
        self.command = command
        self.pid = 4321
        self.returncode = 0
        self.stdout = ["out_time_ms=1000000\n", "progress=end\n"]
        self.terminated = False
        self._returned = False
        if content is not None:
            Path(command[-1]).write_bytes(content)

    def wait(self):
        self._returned = True
        return self.returncode

    def poll(self):
        return self.returncode if self._returned else None

    def terminate(self):
        self.terminated = True
        self.returncode = -1


class FakeCompletedProcess:
    def __init__(self, command):
        self.command = command
        self.pid = 5432
        self.returncode = 0
        self.terminated = False

    def communicate(self, timeout=None):
        return b"", b""

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -1


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.config = {
            **core.DEFAULT_CONFIG,
            "cache_dir": str(self.root / "cache"),
            "library_dir": str(self.library),
            "wd_library_dir": str(self.root / "wd"),
            "dudu_library_dir": str(self.root / "dudu"),
        }
        previous_config = core._config
        core._config = self.config
        self.addCleanup(setattr, core, "_config", previous_config)
        self.config_patch = mock.patch.object(core, "get_config", return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        for patcher in (
                mock.patch.object(core, "data_dir", return_value=str(self.root)),
                mock.patch.object(core, "get_queue_snapshot", return_value=[]),
                mock.patch.object(core, "rebuilding", return_value=False),
                mock.patch.object(core, "verifying", return_value=False),
                mock.patch.object(compression, "_binary", return_value="ffmpeg"),
                mock.patch.object(compression.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout="", stderr="")),
                mock.patch.object(compression.subprocess, "Popen",
                                  side_effect=lambda command, **kwargs: FakeCompletedProcess(command))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, content=b"x" * 70, metadata=None, game_running=False, listener=None):
        metadata = metadata or {"width": 1920, "height": 1080, "rate": "60/1", "frames": 120, "duration": 2.0}
        probe = mock.patch.object(compression.CompressionManager, "_probe",
                                  side_effect=metadata if isinstance(metadata, list) else None,
                                  return_value=None if isinstance(metadata, list) else metadata)
        probe.start()
        self.addCleanup(probe.stop)
        manager = compression.CompressionManager(
            listener=listener, state_path=self.root / "queue.json", game_running=lambda: game_running,
            process_factory=lambda command: FakeProcess(command, content))
        self.addCleanup(manager.shutdown)
        return manager

    def write_song(self, name="123.mp4", content=b"original" * 20):
        path = self.library / name
        path.write_bytes(content)
        return path

    def test_success_replaces_original_and_persists_queue(self):
        source = self.write_song()
        original_size = source.stat().st_size
        manager = self.manager(content=b"x" * int(original_size * 0.5))
        self.assertEqual(manager.enqueue("pypy"), 1)
        self.assertTrue(manager.start("pypy", "cpu", False))
        manager._thread.join(3)
        self.assertFalse(manager._thread.is_alive())
        item = manager._items[0]
        self.assertEqual(item["status"], "done")
        source = Path(item["path"])
        self.assertTrue(source.name.endswith("_compressed.mp4"))
        self.assertEqual(source.stat().st_size, original_size // 2)
        self.assertEqual(item["output_mtime_ns"], source.stat().st_mtime_ns)
        self.assertEqual(item["crf"], 30)
        self.assertEqual(item["encoder"], "cpu")
        self.assertEqual(item["codec"], "h264")
        self.assertGreater(item["compressed_at"], 0)
        reopened = compression.CompressionManager(state_path=self.root / "queue.json")
        self.assertEqual(reopened._items[0]["status"], "done")
        self.assertFalse(reopened.start("pypy", "cpu", False))

    def test_second_scan_skips_done_and_only_adds_new_songs(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.write_song("456.mp4")
        self.assertEqual(manager.enqueue("pypy"), 1)
        self.assertEqual(len(manager._items), 2)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertEqual(manager._items[1]["status"], "queued")
        self.assertEqual(manager.enqueue("pypy"), 0)

    def test_redownloaded_original_is_requeued_and_compressed_again(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        item = manager._items[0]
        identifier = item["id"]
        source = Path(item["path"])
        original_size = source.stat().st_size
        source.write_bytes(b"original" * 20)
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1_000_000_000))
        self.assertNotEqual(source.stat().st_mtime_ns, item["output_mtime_ns"])
        self.assertEqual(manager.enqueue("pypy"), 1)
        source = Path(item["path"])
        self.assertEqual(len(manager._items), 1)
        self.assertEqual(item["id"], identifier)
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["original_size"], source.stat().st_size)
        self.assertNotEqual(item["original_size"], original_size)
        for key in ("output_size", "output_mtime_ns", "crf", "encoder", "codec", "compressed_at"):
            self.assertNotIn(key, item)
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.assertEqual(item["status"], "done")

    def test_permanent_playback_touch_does_not_requeue_compressed_file(self):
        source = self.write_song("123_compressed.mp4")
        manager = self.manager(content=None)
        manager.enqueue("pypy")
        self.assertEqual(manager._items[0]["status"], "done")
        core.touch(str(source))
        manager.enqueue("pypy")
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertTrue(source.name.endswith("_compressed.mp4"))

    def test_compression_updates_cooldown_path(self):
        source = self.write_song()
        self.config["cooldown_file"] = str(source)
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertTrue(core._config["cooldown_file"].endswith("_compressed.mp4"))
        self.assertTrue(Path(core._config["cooldown_file"]).is_file())
        self.assertTrue(core.cooldown_available())

    def test_active_cooldown_file_is_not_replaced(self):
        source = self.write_song()
        self.config["cooldown_file"] = str(source)
        core._cooldown_armed = True
        self.addCleanup(setattr, core, "_cooldown_armed", False)
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "skipped")
        self.assertTrue(source.is_file())
        self.assertFalse(source.with_name("123_compressed.mp4").exists())

    def test_cooldown_falls_back_to_compressed_sibling(self):
        source = self.write_song()
        marked = source.with_name("123_compressed.mp4")
        source.replace(marked)
        self.assertEqual(core.resolve_cooldown_file(str(source)), str(marked))

    def test_failed_skipped_and_cancelled_items_are_requeued_but_done_is_not(self):
        paths = [self.write_song(f"{number}.mp4") for number in (123, 456, 789, 1011)]
        manager = self.manager(content=None)
        for path, status in zip(paths, ("done", "failed", "skipped", "cancelled")):
            item = {"id": uuid.uuid4().hex, "path": str(path), "kind": "pypy", "status": status}
            if status == "done":
                item.update(output_size=path.stat().st_size, output_mtime_ns=path.stat().st_mtime_ns)
            manager._items.append(item)
        self.assertEqual(manager.enqueue("pypy"), 3)
        self.assertEqual([item["status"] for item in manager._items],
                         ["done", "queued", "queued", "queued"])
        self.assertEqual(manager._items[0].get("crf"), None)

    def test_low_saving_skip_is_preserved_on_restart_with_same_parameters(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * int(source.stat().st_size * 0.8))
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "skipped")
        reopened = self.manager(content=None)
        self.assertEqual(reopened.enqueue("pypy"), 0)
        self.assertEqual(reopened._items[0]["status"], "skipped")
        self.assertFalse(reopened.start("pypy", "cpu", False))

    def test_low_saving_skip_retries_after_parameters_change(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * int(source.stat().st_size * 0.8))
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["quality_mode"], "volume")
        manager._crf = "quality"
        self.assertEqual(manager.enqueue("pypy"), 1)
        self.assertEqual(manager._items[0]["status"], "queued")
        self.assertEqual(manager._items[0]["previous_status"], "skipped")

    def test_legacy_skip_parameters_are_checked_before_preserving(self):
        source = self.write_song()
        manager = self.manager(content=None)
        manager._items = [{"id": "legacy", "path": str(source), "kind": "pypy", "status": "skipped",
                           "original_size": source.stat().st_size,
                           "original_mtime_ns": source.stat().st_mtime_ns,
                           "message": "体积档（CRF 30）；未达到 25% 收益，已保留原文件"}]
        manager._crf = "volume"
        self.assertEqual(manager.enqueue("pypy"), 0)
        manager._crf = "quality"
        self.assertEqual(manager.enqueue("pypy"), 1)

    def test_low_saving_skip_retries_when_encoder_changes(self):
        source = self.write_song()
        manager = self.manager(content=None)
        manager._items = [{"id": "gpu-skip", "path": str(source), "kind": "pypy", "status": "skipped",
                           "original_size": source.stat().st_size,
                           "original_mtime_ns": source.stat().st_mtime_ns,
                           "quality_mode": "volume", "crf": 30, "encoder": "cpu",
                           "message": "体积档（CRF 30）；未达到 25% 收益，已保留原文件"}]
        manager._crf = "volume"
        manager._encoder = "nvenc"
        self.assertFalse(manager._should_preserve_skipped(manager._items[0], source.stat()))
        manager._encoder = "cpu"
        self.assertTrue(manager._should_preserve_skipped(manager._items[0], source.stat()))

    def test_retry_order_is_new_then_failed_then_skipped(self):
        items = [
            {"path": "skip.mp4", "kind": "pypy", "status": "queued", "previous_status": "skipped"},
            {"path": "new1.mp4", "kind": "pypy", "status": "queued"},
            {"path": "failed.mp4", "kind": "pypy", "status": "queued", "previous_status": "failed"},
            {"path": "new2.mp4", "kind": "pypy", "status": "queued"},
            {"path": "cancelled.mp4", "kind": "pypy", "status": "queued", "previous_status": "cancelled"},
        ]
        manager = self.eta_manager(items)
        order = []
        while True:
            item = manager._next_queued_item()
            if item is None:
                break
            order.append(item["path"])
            item["status"] = "done"
        self.assertEqual(order, ["new1.mp4", "new2.mp4", "failed.mp4", "cancelled.mp4", "skip.mp4"])

    def test_interrupted_item_resumes_and_only_removes_our_temp_file(self):
        source = self.write_song()
        temporary = source.with_name(f".{source.stem}.compressing-old.mp4")
        temporary.write_bytes(b"partial")
        (self.root / "queue.json").write_text(json.dumps({"items": [{
            "path": str(source), "kind": "pypy", "status": "compressing", "progress": 40,
        }]}), encoding="utf-8")
        manager = self.manager(content=None)
        self.assertEqual(manager._items[0]["status"], "queued")
        self.assertFalse(temporary.exists())
        self.assertTrue(source.is_file())

    def test_less_than_twenty_five_percent_saving_preserves_original(self):
        source = self.write_song()
        original = source.read_bytes()
        manager = self.manager(content=b"x" * int(len(original) * 0.8))
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "skipped")
        self.assertEqual(source.read_bytes(), original)
        self.assertIn("CRF 30", manager._items[0]["message"])
        self.assertIn("未达到 25%", manager._items[0]["message"])
        self.assertIn("原文件", manager._items[0]["message"])

    def test_crf_fallback_uses_original_until_threshold_is_met(self):
        source = self.write_song()
        manager = self.manager(content=None)
        manager.enqueue("pypy")
        original_size = source.stat().st_size
        attempts = []

        def factory(command):
            attempts.append(command)
            content = b"x" * int(original_size * (0.8 if len(attempts) == 1 else 0.5))
            return FakeProcess(command, content)

        manager.process_factory = factory
        manager.start("pypy", "cpu", False, crf="auto")
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertEqual(len(attempts), 2)
        self.assertIn("-crf", attempts[0])
        self.assertIn("24", attempts[0])
        self.assertIn("26", attempts[1])
        self.assertIn("CRF 26", manager._items[0]["message"])

    def test_auto_encoder_falls_back_to_cpu_when_gpu_encoding_fails(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * 10)
        attempts = []

        def factory(command):
            attempts.append(command)
            if "h264_nvenc" in command:
                raise RuntimeError("GPU encode failed")
            return FakeProcess(command, b"x" * 10)

        manager.process_factory = factory
        manager.enqueue("pypy")
        with mock.patch.object(manager, "_encoder_available", return_value=True), \
                mock.patch.object(manager, "_validate_output"):
            manager.start("pypy", "auto", False)
            manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertEqual(manager._items[0]["encoder"], "cpu")
        self.assertEqual(len(attempts), 2)
        self.assertIn("libx264", attempts[1])
        self.assertIn("改用 CPU x264", manager._items[0]["message"])

    def test_hardware_probe_timeout_is_safe_in_auto_and_explicit_modes(self):
        manager = self.manager(content=None)
        manager._encoder = "auto"
        with mock.patch.object(manager, "_run_process", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            self.assertFalse(manager._encoder_available("nvenc"))
        manager._encoder = "nvenc"
        manager._encoder_checks.clear()
        with mock.patch.object(manager, "_run_process", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            with self.assertRaises(RuntimeError):
                manager._encoder_available("nvenc")

    def test_fixed_crf_30_only_encodes_once(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("pypy")
        attempts = []

        def factory(command):
            attempts.append(command)
            return FakeProcess(command, b"x" * 10)

        manager.process_factory = factory
        manager.start("pypy", "cpu", False, crf=30)
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][attempts[0].index("-crf") + 1], "30")
        self.assertIn("CRF 30", manager._items[0]["message"])

    def test_aac_audio_is_copied_without_reencoding(self):
        manager = self.manager(content=None)
        command = manager._command("cpu", "source.mp4", "output.mp4", 30, "aac")
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertNotIn("-b:a", command)

    def test_non_aac_audio_falls_back_to_aac_128k(self):
        manager = self.manager(content=None)
        command = manager._command("cpu", "source.mp4", "output.mp4", 30, "mp3")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-b:a") + 1], "128k")

    def test_validation_failure_preserves_original(self):
        source = self.write_song()
        original = source.read_bytes()
        metadata = [
            {"width": 1920, "height": 1080, "rate": "60/1", "frames": 120, "duration": 2.0},
            {"width": 1280, "height": 720, "rate": "60/1", "frames": 120, "duration": 2.0},
        ]
        manager = self.manager(content=b"x" * 10, metadata=metadata)
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        item = manager._items[0]
        self.assertEqual(item["status"], "failed")
        self.assertTrue(item["encode_attempted"])
        self.assertEqual(item["phase"], "validating")
        self.assertGreater(item["validate_seconds"], 0)
        self.assertGreater(item["terminal_at"], 0)
        self.assertEqual(source.read_bytes(), original)

    def test_source_changed_during_compression_is_not_replaced(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("pypy")
        item = manager._items[0]
        def change_source(command):
            source.write_bytes(b"changed by another process")
            return FakeProcess(command, b"x" * 10)
        manager.process_factory = change_source
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "failed")
        self.assertEqual(source.read_bytes(), b"changed by another process")

    def test_manual_pause_has_priority_over_game_resume(self):
        manager = self.manager(content=None)
        process = FakeProcess(["ffmpeg"], None)
        manager._process = process
        with mock.patch.object(compression, "_suspend_process") as suspend:
            manager._game_pause = True
            manager.resume()
            suspend.assert_not_called()
            manager.pause()
            manager._game_pause = False
            manager.resume()
            suspend.assert_called_with(process.pid, False)
        manager._process = None

    def test_game_pause_blocks_and_resume_after_game_exit(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * 10, game_running=True)
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", True)
        self.assertEqual(manager.snapshot()["phase"], "game_paused")
        self.assertIsNone(manager._current)
        manager.resume()
        self.assertIsNone(manager._current)
        manager._game_pause = False
        manager.resume()
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertTrue(Path(manager._items[0]["path"]).is_file())

    def test_cancel_emits_idle_and_allows_restart(self):
        self.write_song()
        states = []
        manager = self.manager(content=b"x" * 10, game_running=True,
                               listener=lambda kind, data: states.append(data))
        manager.enqueue("pypy")
        self.assertTrue(manager.start("pypy", "cpu", True))
        thread = manager._thread
        manager.cancel()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(manager.snapshot()["phase"], "idle")
        self.assertEqual(states[-1]["phase"], "idle")

        self.assertTrue(manager.start("pypy", "cpu", False))
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertEqual(states[-1]["phase"], "idle")

    def test_cancel_resumes_then_terminates_process_without_touching_source(self):
        source = self.write_song()
        original = source.read_bytes()
        manager = self.manager(content=None)
        manager.enqueue("pypy")
        process = FakeProcess(["ffmpeg"], None)
        manager._process = process
        manager._current = manager._items[0]
        with mock.patch.object(manager, "_resume_process") as resume:
            manager.cancel()
        resume.assert_called_once()
        self.assertTrue(process.terminated)
        self.assertEqual(source.read_bytes(), original)
        manager._process = None

    def test_enqueue_only_includes_owned_permanent_files(self):
        (self.library / "123.mp4").write_bytes(b"owned")
        (self.library / "123.mp4.part").write_bytes(b"owned")
        (self.library / "personal.mp4").write_bytes(b"personal")
        (self.library / "notes.txt").write_text("keep", encoding="utf-8")
        manager = self.manager(content=None)
        self.assertEqual(manager.enqueue("pypy"), 1)
        self.assertEqual({Path(item["path"]).name for item in manager._items}, {"123.mp4"})

    def test_enqueue_includes_songs_in_classification_subfolders(self):
        nested = self.library / "kpop" / "123.mp4"
        nested.parent.mkdir()
        nested.write_bytes(b"nested")
        manager = self.manager(content=None)
        self.assertEqual(manager.enqueue("pypy"), 1)
        self.assertEqual(manager._items[0]["path"], str(nested))

    def test_start_only_processes_the_selected_library(self):
        source = self.write_song()
        (self.root / "wd").mkdir()
        other = self.root / "wd" / "wd_123.mp4"
        other.write_bytes(b"other" * 20)
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("all")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertEqual(manager._items[1]["status"], "queued")
        self.assertTrue(other.is_file())

    def test_selected_library_snapshot_excludes_legacy_queue_items(self):
        manager = self.manager(game_running=True)
        manager._items = [
            {"path": f"pypy/{number}.mp4", "kind": "pypy", "status": "queued"}
            for number in range(4914)
        ] + [
            {"path": f"dudu/dudu_{number}.mp4", "kind": "dudu", "status": "queued"}
            for number in range(5)
        ]
        self.assertTrue(manager.start("dudu", "cpu", True))
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["kind"], "dudu")
        self.assertEqual(snapshot["phase"], "game_paused")
        self.assertEqual(snapshot["counts"], {"queued": 5})
        self.assertEqual(len(snapshot["items"]), 5)
        self.assertTrue(all(item["kind"] == "dudu" for item in snapshot["items"]))
        self.assertTrue(all(item["status"] == "queued" for item in manager._items[:4914]))

    def test_ffmpeg_and_ffprobe_subprocesses_hide_console_windows(self):
        manager = compression.CompressionManager(
            state_path=self.root / "hidden-queue.json", game_running=lambda: False,
            process_factory=lambda command: FakeProcess(command, b"x"))
        self.addCleanup(manager.shutdown)
        metadata = {"width": 1920, "height": 1080, "rate": "60/1", "frames": 120, "duration": 2.0,
                    "audio_codec": None}
        payload = {"streams": [{"codec_type": "video", "width": 1920, "height": 1080,
                                "avg_frame_rate": "60/1", "nb_read_frames": "120", "duration": "2.0"}]}
        popen = compression.subprocess.Popen
        process = mock.Mock(returncode=0, pid=6543)
        process.poll.return_value = 0
        process.communicate.side_effect = [(json.dumps(payload), ""), (b"", b""), (b"", b"")]
        popen.side_effect = lambda *args, **kwargs: process
        manager._probe("song.mp4")
        manager._encoder_available("nvenc")
        with mock.patch.object(manager, "_probe", return_value=metadata):
            manager._validate_output("song.mp4", "output.mp4", metadata)
        self.assertEqual(len(popen.call_args_list), 3)
        for call in popen.call_args_list:
            self.assertEqual(call.kwargs.get("creationflags"),
                             getattr(compression.subprocess, "CREATE_NO_WINDOW", 0))
        with mock.patch.object(compression.subprocess, "Popen") as popen:
            compression._spawn(["ffmpeg"])
            self.assertEqual(popen.call_args.kwargs.get("creationflags"),
                             getattr(compression.subprocess, "CREATE_NO_WINDOW", 0))

    def test_locked_temp_cleanup_retries_without_overwriting_skip_result(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * int(source.stat().st_size * 0.8))
        manager.enqueue("pypy")
        locked = PermissionError(32, "simulated scanner lock")
        with mock.patch.object(compression.os, "remove", side_effect=locked) as remove, \
                mock.patch.object(compression.time, "sleep"):
            manager.start("pypy", "cpu", False)
            manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "skipped")
        self.assertIn("未达到 25%", manager._items[0]["message"])
        self.assertEqual(remove.call_count, 6)

    def test_next_enqueue_cleans_stale_temporary_files(self):
        source = self.write_song()
        stale = self.library / ".123.compressing-old.mp4"
        stale.write_bytes(b"partial")
        manager = self.manager(content=None)
        manager._items.append({"path": str(source), "kind": "pypy", "status": "failed"})
        manager.enqueue("pypy")
        self.assertFalse(stale.exists())
        self.assertEqual(manager._items[0]["status"], "queued")

    def test_blackbox_presets_use_calibrated_encoder_parameters(self):
        expected = {
            "quality": {"cpu": 28, "nvenc": 35, "qsv": 29, "amf": 33},
            "volume": {"cpu": 30, "nvenc": 37, "qsv": 31, "amf": 35},
        }
        for mode, encoders in expected.items():
            for encoder, quality in encoders.items():
                self.assertEqual(compression._encoder_quality(encoder, mode), quality)

    def test_session_scope_excludes_stale_paths_old_directories_and_history_counts(self):
        nested = self.library / "Group" / "1.mp4"
        nested.parent.mkdir()
        nested.write_bytes(b"original" * 20)
        manager = self.manager(content=b"x" * 10)
        manager._items.extend([
            {"path": str(self.library / "1.mp4"), "kind": "pypy", "status": "queued"},
            {"path": str(self.root / "old-library" / "2.mp4"), "kind": "pypy", "status": "queued"},
            {"path": str(self.root / "old-library" / "done.mp4"), "kind": "pypy", "status": "done"},
        ])
        self.assertEqual(manager.enqueue("pypy"), 1)
        self.assertEqual(manager.snapshot()["counts"], {"queued": 1})
        self.assertTrue(manager.start("pypy", "cpu", False))
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "queued")
        self.assertEqual(manager._items[1]["status"], "queued")
        self.assertEqual(manager._items[2]["status"], "done")
        self.assertEqual(manager.snapshot()["counts"], {"done": 1})
        self.assertTrue(Path(manager._items[3]["path"]).name.endswith("_compressed.mp4"))

    def test_external_compressed_marker_is_not_processed_again(self):
        source = self.library / "123_compressed.mp4"
        source.write_bytes(b"already compressed")
        manager = self.manager(content=None)
        self.assertEqual(manager.enqueue("pypy"), 0)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertIn("_compressed", manager._items[0]["message"])
        self.assertFalse(manager.start("pypy", "cpu", False))

    def test_parallel_workers_process_multiple_files_and_publish_details(self):
        for number in range(4):
            self.write_song(f"{number + 1}.mp4")
        active = 0
        maximum = 0
        lock = threading.Lock()

        def factory(command):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.1)
            process = FakeProcess(command, b"x" * 10)
            time.sleep(0.1)
            with lock:
                active -= 1
            return process

        manager = self.manager(content=None)
        manager.process_factory = factory
        manager.enqueue("pypy")
        self.assertTrue(manager.start("pypy", "cpu", False, "volume", parallel=2))
        manager._thread.join(10)
        self.assertGreaterEqual(maximum, 2)
        self.assertTrue(all(item["status"] == "done" for item in manager._items))
        self.assertEqual(len(manager.snapshot()["details"]), 4)
        self.assertEqual(manager.snapshot()["phase"], "idle")

    def test_selected_paths_only_enqueue_chosen_file_inside_library(self):
        paths = [self.write_song(f"{number}.mp4") for number in (1, 2, 3)]
        manager = self.manager(content=b"x" * 10)
        self.assertEqual(manager.enqueue("pypy", [str(paths[1])]), 1)
        self.assertEqual([item["path"] for item in manager._items], [str(paths[1])])
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        self.assertEqual(manager._items[0]["status"], "done")
        self.assertEqual(paths[0].read_bytes(), b"original" * 20)
        self.assertEqual(paths[2].read_bytes(), b"original" * 20)
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        with self.assertRaises(ValueError):
            manager.enqueue("pypy", [str(outside)])

    def test_selected_paths_can_mix_multiple_libraries(self):
        pypy = self.write_song("1.mp4")
        wd_dir = self.root / "wd"
        wd_dir.mkdir()
        wd = wd_dir / "wd_2.mp4"
        wd.write_bytes(b"original" * 20)
        manager = self.manager(content=b"x" * 10)
        selected = {"pypy": [str(pypy)], "wd": [str(wd)], "dudu": []}
        self.assertEqual(manager.enqueue("all", selected), 2)
        self.assertEqual({item["kind"] for item in manager._items}, {"pypy", "wd"})
        manager.start("all", "cpu", False)
        manager._thread.join(3)
        self.assertTrue(all(item["status"] == "done" for item in manager._items))

    def test_full_load_assigns_dedicated_cpu_and_each_available_gpu_worker(self):
        for number in range(6):
            self.write_song(f"{number + 1}.mp4")
        active = 0
        maximum = 0
        used = set()
        lock = threading.Lock()

        def factory(command):
            nonlocal active, maximum
            codec = next(codec for codec in ("libx264", "h264_nvenc", "h264_amf") if codec in command)
            with lock:
                active += 1
                maximum = max(maximum, active)
                used.add(codec)
            time.sleep(0.15)
            process = FakeProcess(command, b"x" * 10)
            time.sleep(0.15)
            with lock:
                active -= 1
            return process

        manager = self.manager(content=None)
        manager.process_factory = factory
        manager.enqueue("pypy")
        with mock.patch.object(manager, "_encoder_available",
                               side_effect=lambda encoder, item=None: encoder in ("cpu", "nvenc", "amf")), \
                mock.patch.object(compression.core, "log") as log:
            self.assertTrue(manager.start("pypy", "full", False, "volume", parallel=3))
            manager._thread.join(10)
        self.assertEqual(maximum, 3)
        self.assertEqual(used, {"libx264", "h264_nvenc", "h264_amf"})
        self.assertEqual(manager.snapshot()["parallel"], 3)
        self.assertTrue(all(item["status"] == "done" for item in manager._items),
                        [(item["status"], item.get("message")) for item in manager._items])
        self.assertIn("CPU x264 + NVIDIA NVENC + AMD AMF", log.call_args.args[0])

    def test_full_load_parallel_count_controls_worker_distribution(self):
        for number in (1, 2):
            self.write_song(f"{number}.mp4")
        codecs = []
        manager = self.manager(content=b"x" * 10)
        manager.process_factory = lambda command: (codecs.append(next(
            codec for codec in ("libx264", "h264_nvenc", "h264_amf") if codec in command)),
            FakeProcess(command, b"x" * 10))[1]
        manager.enqueue("pypy")
        with mock.patch.object(manager, "_encoder_available", return_value=True):
            manager.start("pypy", "full", False, "volume", 2, ["cpu", "nvenc", "amf"])
            manager._thread.join(3)
        self.assertEqual(set(codecs), {"libx264", "h264_nvenc"})
        self.assertNotIn("h264_amf", codecs)
        self.assertEqual(manager.snapshot()["parallel"], 2)

    def test_full_load_assigns_qsv_when_available(self):
        for number in range(4):
            self.write_song(f"{number + 1}.mp4")
        codecs = []
        manager = self.manager(content=b"x" * 10)
        manager.process_factory = lambda command: (codecs.append(next(
            codec for codec in ("libx264", "h264_nvenc", "h264_amf", "h264_qsv") if codec in command)),
            FakeProcess(command, b"x" * 10))[1]
        manager.enqueue("pypy")
        with mock.patch.object(manager, "_encoder_available", return_value=True):
            manager.start("pypy", "full", False, "volume", 4)
            manager._thread.join(5)
        self.assertEqual(set(codecs), {"libx264", "h264_nvenc", "h264_amf", "h264_qsv"})
        self.assertEqual(manager.snapshot()["parallel"], 4)

    def test_full_load_omits_unchecked_available_encoders(self):
        self.write_song()
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("pypy")
        with mock.patch.object(manager, "_encoder_available", return_value=True) as available:
            self.assertTrue(manager.start("pypy", "full", False, "volume", 1, ["cpu"]))
            manager._thread.join(3)
        self.assertEqual(manager._items[0]["encoder"], "cpu")
        self.assertEqual(manager.snapshot()["parallel"], 1)
        self.assertNotIn(mock.call("nvenc"), available.call_args_list)

    def test_parallel_limit_allows_eight_and_rejects_nine(self):
        self.write_song()
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("pypy")
        with mock.patch.object(manager, "_encoder_available", return_value=True):
            self.assertTrue(manager.start("pypy", "full", False, "volume", 8, ["cpu"]))
            manager._thread.join(5)
        self.assertEqual(manager.snapshot()["parallel"], 8)
        self.assertEqual(manager._items[0]["status"], "done")
        with self.assertRaisesRegex(ValueError, "1 到 8"):
            manager.start("pypy", "full", False, "volume", 9, ["cpu"])

    def eta_manager(self, items):
        manager = self.manager(content=None)
        manager._kind = "pypy"
        manager._items = items
        manager._session_ids = {id(item) for item in items}
        manager._active_items = {id(item): item for item in items if item.get("status") == "compressing"}
        manager._thread = mock.Mock()
        manager._thread.is_alive.return_value = True
        manager._active = True
        return manager

    def test_eta_uses_active_progress_completed_and_queued_bytes(self):
        done = {"path": "done.mp4", "kind": "pypy", "status": "done", "original_size": 100}
        active = {"path": "1.mp4", "kind": "pypy", "status": "compressing",
                  "original_size": 100, "progress": 50}
        queued = {"path": "2.mp4", "kind": "pypy", "status": "queued", "original_size": 100}
        manager = self.eta_manager([done, active, queued])
        manager._started_at = 90
        with mock.patch.object(compression.time, "monotonic", return_value=100):
            self.assertEqual(manager.snapshot()["eta_seconds"], 10)

    def test_eta_excludes_manual_pause_time(self):
        manager = self.eta_manager([])
        manager._started_at = 100
        with mock.patch.object(compression.time, "monotonic", return_value=130):
            self.assertEqual(manager._elapsed_seconds(), 30)
            manager._manual_pause = True
            manager._sync_pause_clock()
        with mock.patch.object(compression.time, "monotonic", return_value=170):
            self.assertEqual(manager._elapsed_seconds(), 30)
            manager._manual_pause = False
            manager._sync_pause_clock()
        with mock.patch.object(compression.time, "monotonic", return_value=200):
            self.assertEqual(manager._elapsed_seconds(), 60)

    def test_eta_blends_recent_speed_without_a_hard_switch(self):
        items = [{"path": f"{index}.mp4", "kind": "pypy", "status": status,
                  "original_size": 100 * 1048576, "duration": 100}
                 for index, status in enumerate(("done", "done", "queued"))]
        manager = self.eta_manager(items)
        manager._started_at = 1
        manager._completion_history.extend(((10, 0), (40, 200)))
        with mock.patch.object(compression.time, "monotonic", return_value=51):
            self.assertEqual(manager.snapshot()["eta_seconds"], 25)

    def test_eta_uses_cumulative_recent_speed_when_window_is_confident(self):
        items = [{"path": f"{index}.mp4", "kind": "pypy", "status": status,
                  "original_size": 100 * 1048576, "duration": 100}
                 for index, status in enumerate(("done", "done", "queued"))]
        manager = self.eta_manager(items)
        manager._started_at = 1
        manager._completion_history.extend((50 + index * 30, index * 25) for index in range(9))
        with mock.patch.object(compression.time, "monotonic", return_value=351):
            self.assertEqual(manager.snapshot()["eta_seconds"], 150)

    def test_eta_large_queue_uses_recent_delta_not_whole_session(self):
        items = [{"path": f"{index}.mp4", "kind": "pypy",
                  "status": "done" if index < 1000 else "queued",
                  "original_size": 1048576, "duration": 240}
                 for index in range(2000)]
        manager = self.eta_manager(items)
        manager._started_at = 1
        manager._completion_history.extend(
            (9720 + index * 35, (968 + index * 4) * 240) for index in range(9))
        with mock.patch.object(compression.time, "monotonic", return_value=10001):
            self.assertEqual(manager.snapshot()["eta_seconds"], 8824)

    def test_completion_history_records_cumulative_and_active_work(self):
        items = [
            {"path": "done.mp4", "kind": "pypy", "status": "done", "original_size": 100, "duration": 100},
            {"path": "active.mp4", "kind": "pypy", "status": "compressing",
             "original_size": 100, "duration": 100, "progress": 50},
            {"path": "queued.mp4", "kind": "pypy", "status": "queued", "original_size": 100, "duration": 100},
        ]
        manager = self.eta_manager(items)
        manager._started_at = 90
        with mock.patch.object(compression.time, "monotonic", return_value=100):
            manager._record_completion(items[0])
            self.assertEqual(manager._completion_history[-1], (10, 150))
            manager._record_completion(items[0])
        self.assertEqual(manager._completion_history[-1], (10, 250))

    def test_eta_does_not_count_failed_or_skipped_as_completed(self):
        items = [{"path": f"{index}.mp4", "kind": "pypy", "status": status,
                  "original_size": 100, "duration": 100}
                 for index, status in enumerate(("done", "skipped", "failed", "queued"))]
        manager = self.eta_manager(items)
        manager._started_at = 1
        with mock.patch.object(compression.time, "monotonic", return_value=51):
            self.assertEqual(manager.snapshot()["eta_seconds"], 50)

    def test_eta_counts_only_skips_and_failures_that_actually_encoded(self):
        items = [
            {"path": "done.mp4", "kind": "pypy", "status": "done", "original_size": 100, "duration": 100},
            {"path": "skip-encoded.mp4", "kind": "pypy", "status": "skipped",
             "original_size": 100, "duration": 100, "encode_attempted": True},
            {"path": "skip-direct.mp4", "kind": "pypy", "status": "skipped",
             "original_size": 100, "duration": 100, "encode_attempted": False},
            {"path": "failed-encoded.mp4", "kind": "pypy", "status": "failed",
             "original_size": 100, "duration": 100, "encode_attempted": True},
            {"path": "queued.mp4", "kind": "pypy", "status": "queued", "original_size": 100, "duration": 100},
        ]
        manager = self.eta_manager(items)
        manager._started_at = 1
        with mock.patch.object(compression.time, "monotonic", return_value=51):
            self.assertEqual(manager.snapshot()["eta_seconds"], 17)

    def test_encoded_skip_records_stage_times_and_terminal_history(self):
        source = self.write_song()
        manager = self.manager(content=b"x" * int(source.stat().st_size * 0.8))
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        item = manager._items[0]
        self.assertEqual(item["status"], "skipped")
        self.assertTrue(item["encode_attempted"])
        self.assertEqual(item["phase"], "complete")
        self.assertGreater(item["analysis_seconds"], 0)
        self.assertGreater(item["encode_seconds"], 0)
        self.assertGreater(item["validate_seconds"], 0)
        self.assertGreater(item["terminal_at"], 0)
        self.assertEqual(len(manager._completion_history), 1)
        self.assertEqual(manager._work_totals()[0], item["duration"])

    def test_direct_skip_does_not_enter_eta_history(self):
        source = self.write_song()
        self.config["cooldown_file"] = str(source)
        core._cooldown_armed = True
        self.addCleanup(setattr, core, "_cooldown_armed", False)
        manager = self.manager(content=b"x" * 10)
        manager.enqueue("pypy")
        manager.start("pypy", "cpu", False)
        manager._thread.join(3)
        item = manager._items[0]
        self.assertEqual(item["status"], "skipped")
        self.assertFalse(item["encode_attempted"])
        self.assertGreater(item["terminal_at"], 0)
        self.assertEqual(len(manager._completion_history), 0)
        self.assertEqual(manager._work_totals()[0], 0)

    def test_encoding_progress_leaves_validation_as_a_separate_stage(self):
        item = {"path": "video.mp4", "kind": "pypy", "status": "compressing",
                "phase": "encoding", "duration": 200, "progress": 0}
        manager = self.eta_manager([item])
        manager._process_progress(item, "out_time_ms=100000000")
        self.assertEqual(item["progress"], 50)
        manager._process_progress(item, "progress=end")
        self.assertEqual(item["progress"], 95)
        item["phase"] = "validating"
        self.assertEqual(item["phase"], "validating")

    def test_eta_weights_files_by_duration_instead_of_only_bytes(self):
        items = [{"path": "done.mp4", "kind": "pypy", "status": "done",
                  "original_size": 100, "duration": 100},
                 {"path": "queued.mp4", "kind": "pypy", "status": "queued",
                  "original_size": 100, "duration": 200}]
        manager = self.eta_manager(items)
        manager._started_at = 1
        with mock.patch.object(compression.time, "monotonic", return_value=51):
            self.assertEqual(manager.snapshot()["eta_seconds"], 100)

    def test_eta_estimates_unknown_duration_from_observed_bitrate(self):
        items = [{"path": "done.mp4", "kind": "pypy", "status": "done",
                  "original_size": 100, "duration": 100},
                 {"path": "queued.mp4", "kind": "pypy", "status": "queued", "original_size": 200}]
        manager = self.eta_manager(items)
        manager._started_at = 1
        with mock.patch.object(compression.time, "monotonic", return_value=51):
            self.assertEqual(manager.snapshot()["eta_seconds"], 100)

    def test_progress_saves_are_throttled_and_force_save_bypasses_interval(self):
        manager = self.manager(content=None)
        manager._last_save_time = 0.0
        with mock.patch.object(compression.time, "monotonic", side_effect=[100, 101, 106, 110]), \
                mock.patch.object(compression.os, "replace"), \
                mock.patch.object(compression.Path, "write_text") as write_text:
            manager._save()
            manager._save()
            manager._save()
            manager._save(force=True)
        self.assertEqual(write_text.call_count, 3)

    def test_snapshot_limits_detail_copies_but_preserves_total(self):
        items = [{"kind": "pypy", "status": "done", "path": f"{index}.mp4"} for index in range(150)]
        manager = self.manager(content=None)
        manager._items = items
        manager._session_ids = {id(item) for item in items}
        manager._session_details = items.copy()
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["detail_total"], 150)
        self.assertEqual(len(snapshot["details"]), 100)
        self.assertEqual(snapshot["details"][0]["path"], "50.mp4")
        self.assertEqual(snapshot["details"][-1]["path"], "149.mp4")

    def test_queue_cursor_returns_each_song_once_in_original_order(self):
        items = [{"kind": "pypy", "status": "queued", "path": f"{index}.mp4"} for index in range(4)]
        manager = self.manager(content=None)
        manager._kind = "pypy"
        manager._items = items
        manager._session_ids = {id(item) for item in items}
        selected = []
        while item := manager._next_queued_item():
            selected.append(item["path"])
            item["status"] = "compressing"
        self.assertEqual(selected, ["0.mp4", "1.mp4", "2.mp4", "3.mp4"])
        self.assertIsNone(manager._next_queued_item())
        self.assertEqual(manager._next_item_index, len(items))

    def test_verify_skips_compressed_file_but_checks_redownloaded_original(self):
        source = self.write_song()
        state = Path(core.data_dir()) / "compression-queue.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"items": [{
            "path": str(source), "status": "done", "output_mtime_ns": source.stat().st_mtime_ns,
        }]}), encoding="utf-8")
        core._config["library_dir"] = str(self.library)
        core._library_stop.clear()
        stats_patch = mock.patch.object(core, "emit_stats")
        stats_patch.start()
        self.addCleanup(stats_patch.stop)
        with mock.patch.object(core, "resolve_video_filename", return_value="video.mp4"), \
                mock.patch.object(core, "_remote_size", side_effect=AssertionError("unexpected remote check")):
            core._verify_worker()
        os.utime(source, None)
        with mock.patch.object(core, "resolve_video_filename", return_value="video.mp4"), \
                mock.patch.object(core, "_remote_size", return_value=source.stat().st_size) as remote:
            core._verify_worker()
        remote.assert_called_once()


if __name__ == "__main__":
    unittest.main()
