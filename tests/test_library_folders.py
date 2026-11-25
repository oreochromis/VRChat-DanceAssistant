import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock


class LibraryFolderTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("folder_dance_cache", Path(__file__).resolve().parents[1] / "dance_cache.py")
        self.core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.core)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.dirs = {kind: self.root / kind for kind in ("pypy", "wd", "dudu")}
        for path in self.dirs.values():
            path.mkdir()
        self.core._config = {
            "library_dir": str(self.dirs["pypy"]), "wd_library_dir": str(self.dirs["wd"]),
            "dudu_library_dir": str(self.dirs["dudu"]), "cache_dir": str(self.root / "cache"),
            "max_cache_mb": 2048,
            "cdn_permanent": True, "youtube_permanent": True, "wd_permanent": True, "dudu_permanent": True,
        }
        (self.root / "cache").mkdir()
        self.core._storage_mode_override = "portable"
        self.core.SCRIPT_DIR = str(self.root)
        self.core.VRC_LOG_DIR = str(self.root / "no-logs")
        self.core._bundle_ids = {1, 2}
        self.addCleanup(self.core._stop_event.set)

    def test_flat_legacy_files_remain_available_for_all_rooms(self):
        (self.dirs["pypy"] / "1.mp4").write_bytes(b"pypy")
        (self.dirs["wd"] / "wd_2.mp4").write_bytes(b"wd")
        (self.dirs["dudu"] / "dudu_3.mp4").write_bytes(b"dudu")
        self.core._reverse_mapping["video.mp4"] = 1
        self.assertEqual(self.core.local_path_for("video.mp4"), str(self.dirs["pypy"] / "1.mp4"))
        self.assertEqual(self.core.wd_local_path(2), str(self.dirs["wd"] / "wd_2.mp4"))
        self.assertEqual(self.core.dudu_local_path(3), str(self.dirs["dudu"] / "dudu_3.mp4"))
        for kind in ("pypy", "wd", "dudu"):
            with mock.patch.object(self.core, "refresh_wd_favorites", return_value=False), \
                    mock.patch.object(self.core, "get_wd_favorites", return_value=set()):
                result = self.core.scan_library(kind)
            self.assertEqual(len(result["songs"]), 1)

    def test_new_downloads_default_to_flat_library_paths(self):
        for kind, song_id, expected in (("pypy", 1, "1.mp4"), ("wd", 2, "wd_2.mp4"), ("dudu", 3, "dudu_3.mp4")):
            self.core._config["library_structure"] = "flat"
            path = self.core.classified_library_path(kind, song_id)
            self.assertEqual(Path(path), self.dirs[kind] / expected)

    def test_new_downloads_use_current_official_groups_when_classified(self):
        self.core._config["library_structure"] = "classified"
        with mock.patch.object(self.core, "bundle_song_classification", return_value=({1: "kpop"}, {})), \
                mock.patch.object(self.core, "_wd_catalog", return_value={2: {"group": "Official Group"}}), \
                mock.patch.object(self.core, "dudu_song_groups", return_value={3: "ACG Dance"}):
            self.assertEqual(Path(self.core.library_path(1)), self.dirs["pypy"] / "kpop" / "1.mp4")
            self.assertEqual(Path(self.core.wd_library_path(2)), self.dirs["wd"] / "Official Group" / "wd_2.mp4")
            self.assertEqual(Path(self.core.dudu_library_path(3)), self.dirs["dudu"] / "ACG Dance" / "dudu_3.mp4")

    def test_future_groups_and_windows_names_are_handled_without_code_changes(self):
        self.core._config["library_structure"] = "classified"
        with mock.patch.object(self.core, "bundle_song_classification", return_value=({1: "New: Group / 2027"}, {})):
            path = Path(self.core.library_path(1))
            self.assertEqual(path.parent.name, "New Group 2027")
            self.assertEqual(path.name, "1.mp4")
        self.assertEqual(self.core._safe_group_folder("CON"), "_CON")
        self.assertEqual(self.core._safe_group_folder(""), "_未分类")

    def test_recursive_lookup_scan_and_duplicate_reporting(self):
        first = self.dirs["pypy"] / "Group A" / "1.mp4"
        second = self.dirs["pypy"] / "Group B" / "1.mp4"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        self.assertEqual(self.core.library_path(1), str(first))
        result = self.core.scan_library("pypy")
        self.assertEqual(result["duplicate_ids"], [1])
        self.assertEqual(len(result["songs"]), 2)

    def test_compressed_suffix_remains_playable_in_every_room(self):
        paths = {
            "pypy": self.dirs["pypy"] / "Group" / "1_compressed.mp4",
            "wd": self.dirs["wd"] / "Group" / "wd_2_compressed.mp4",
            "dudu": self.dirs["dudu"] / "Group" / "dudu_3_compressed.mp4",
        }
        for path in paths.values():
            path.parent.mkdir()
            path.write_bytes(b"compressed")
        self.core._reverse_mapping["video.mp4"] = 1
        self.assertEqual(self.core.local_path_for("video.mp4"), str(paths["pypy"]))
        self.assertEqual(self.core.wd_local_path(2), str(paths["wd"]))
        self.assertEqual(self.core.dudu_local_path(3), str(paths["dudu"]))
        for kind, path in paths.items():
            with mock.patch.object(self.core, "refresh_wd_favorites", return_value=False), \
                    mock.patch.object(self.core, "get_wd_favorites", return_value=set()), \
                    mock.patch.object(self.core, "_wd_catalog", return_value={}), \
                    mock.patch.object(self.core, "_ensure_dudu_catalog", return_value={}), \
                    mock.patch.object(self.core, "dudu_song_groups", return_value={}):
                scan = self.core.scan_library(kind)
            self.assertEqual([song["id"] for song in scan["songs"]], [int(path.name.split("_")[1 if kind != "pypy" else 0])])

    def test_organize_moves_flat_files_and_playback_uses_new_paths(self):
        (self.dirs["pypy"] / "1.mp4").write_bytes(b"one")
        (self.dirs["pypy"] / "2.mp4").write_bytes(b"two")
        with mock.patch.object(self.core, "bundle_song_classification", return_value=({1: "kpop", 2: "New Group"}, {})):
            result = self.core.organize_library("pypy")
            self.assertEqual(result["moved"], 2)
            first = self.dirs["pypy"] / "kpop" / "1.mp4"
            second = self.dirs["pypy"] / "New Group" / "2.mp4"
            self.assertEqual(first.read_bytes(), b"one")
            self.assertEqual(second.read_bytes(), b"two")
            self.assertEqual(self.core.library_path(1), str(first))

    def test_existing_destination_conflict_moves_nothing(self):
        source = self.dirs["pypy"] / "1.mp4"
        source.write_bytes(b"source")
        destination = self.dirs["pypy"] / "kpop" / "1.mp4"
        destination.parent.mkdir()
        destination.write_bytes(b"destination")
        with mock.patch.object(self.core, "bundle_song_classification", return_value=({1: "kpop"}, {})):
            with self.assertRaises(RuntimeError):
                self.core.organize_library("pypy")
        self.assertEqual(source.read_bytes(), b"source")
        self.assertEqual(destination.read_bytes(), b"destination")

    def test_duplicate_song_conflict_does_not_move_any_file(self):
        paths = [self.dirs["pypy"] / group / "1.mp4" for group in ("A", "B")]
        for path in paths:
            path.parent.mkdir()
            path.write_bytes(path.parent.name.encode())
        with mock.patch.object(self.core, "bundle_song_classification", return_value=({1: "kpop"}, {})):
            with self.assertRaises(RuntimeError):
                self.core.organize_library("pypy")
        for path in paths:
            self.assertTrue(path.is_file())

    def test_failure_during_second_move_rolls_back_first(self):
        paths = [self.dirs["pypy"] / f"{number}.mp4" for number in (1, 2)]
        for path in paths:
            path.write_bytes(path.name.encode())
        original_replace = self.core.os.replace
        calls = 0

        def replace(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated failure")
            original_replace(source, destination)

        with mock.patch.object(self.core, "bundle_song_classification", return_value=({1: "A", 2: "B"}, {})), \
                mock.patch.object(self.core.os, "replace", side_effect=replace):
            with self.assertRaises(RuntimeError):
                self.core.organize_library("pypy")
        for path in paths:
            self.assertEqual(path.read_bytes(), path.name.encode())
        self.assertFalse((self.dirs["pypy"] / "A" / "1.mp4").exists())
        self.assertFalse((self.dirs["pypy"] / "B" / "2.mp4").exists())

    def test_cancel_after_first_move_rolls_back(self):
        paths = [self.dirs["pypy"] / f"{number}.mp4" for number in (1, 2)]
        for path in paths:
            path.write_bytes(path.name.encode())
        with mock.patch.object(self.core, "bundle_song_classification", return_value=({1: "A", 2: "B"}, {})):
            listener = lambda kind, data: self.core.cancel_library_organization() if kind == "library_organize_progress" else None
            self.core.add_listener(listener)
            result = self.core.organize_library("pypy")
        self.assertTrue(result["cancelled"])
        for path in paths:
            self.assertEqual(path.read_bytes(), path.name.encode())

    def test_directory_migration_preserves_category_tree_and_unrelated_files(self):
        source = self.root / "source"
        target = self.root / "target"
        video = source / "Group" / "1.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"video")
        personal = source / "Group" / "personal.txt"
        personal.write_text("keep", encoding="utf-8")
        result = self.core._apply_directory_action(str(source), str(target), "pypy", "migrate",
                                                   {"library_dir": str(target)}, "library_dir")
        self.assertEqual(result["moved"], 1)
        self.assertEqual((target / "Group" / "1.mp4").read_bytes(), b"video")
        self.assertTrue(personal.is_file())

    def test_recursive_delete_only_removes_owned_videos(self):
        owned = self.dirs["wd"] / "Group" / "wd_7.mp4"
        owned.parent.mkdir()
        owned.write_bytes(b"owned")
        personal = self.dirs["wd"] / "Group" / "personal.mp4"
        personal.write_bytes(b"personal")
        self.core._apply_directory_action(str(self.dirs["wd"]), "", "wd", "delete", {}, "wd_library_dir")
        self.assertFalse(owned.exists())
        self.assertTrue(personal.is_file())

    def test_verify_all_rooms_skips_marked_files_and_reports_every_duplicate(self):
        files = {
            self.dirs["pypy"] / "1.mp4": b"123",
            self.dirs["pypy"] / "4_compressed.mp4": b"1234567",
            self.dirs["wd"] / "A" / "wd_2.mp4": b"1234",
            self.dirs["wd"] / "B" / "wd_2.mp4": b"1234",
            self.dirs["dudu"] / "dudu_3.mp4": b"12345",
        }
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        sizes = {("pypy", 1): 3, ("wd", 2): 4, ("dudu", 3): 5}
        events = []
        self.core.add_listener(lambda kind, data: events.append(data) if kind == "verify_done" else None)
        self.core._library_stop.clear()
        self.core._stop_event.clear()
        with mock.patch.object(self.core, "resolve_video_filename", side_effect=lambda sid: f"video{sid}.mp4"), \
                mock.patch.object(self.core, "_remote_size_for", side_effect=lambda kind, sid, filename=None: sizes[(kind, sid)]) as remote, \
                mock.patch.object(self.core, "emit_stats"):
            self.core._verify_worker("all")
        done = events[-1]
        self.assertEqual((done["total"], done["checked"], done["matched"], done["compressed"], done["bad"], done["failed"]),
                         (5, 5, 4, 1, 0, 0))
        self.assertEqual(remote.call_count, 3)
        self.assertFalse(any(call.args[:2] == ("pypy", 4) for call in remote.call_args_list))
        report = json.loads(Path(done["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(report["results"]), 5)
        self.assertEqual(len([item for item in report["results"] if item["kind"] == "wd" and item["id"] == 2]), 2)

    def test_flatten_moves_classified_files_updates_queue_and_preserves_unrelated_files(self):
        first = self.dirs["pypy"] / "Group" / "1_compressed.mp4"
        second = self.dirs["pypy"] / "Other" / "Nested" / "2.mp4"
        personal = self.dirs["pypy"] / "Group" / "personal.mp4"
        for path, content in ((first, b"one"), (second, b"two"), (personal, b"keep")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        queue = self.root / "compression-queue.json"
        queue.write_text(json.dumps({"items": [{"path": str(first), "status": "done"}]}), encoding="utf-8")
        logs = []
        self.core.add_listener(lambda kind, data: logs.append(data.get("msg", "")) if kind == "log" else None)
        result = self.core.organize_library("pypy", flatten=True)
        self.assertEqual(result["moved"], 2)
        self.assertEqual((self.dirs["pypy"] / "1_compressed.mp4").read_bytes(), b"one")
        self.assertEqual((self.dirs["pypy"] / "2.mp4").read_bytes(), b"two")
        self.assertEqual(personal.read_bytes(), b"keep")
        self.assertTrue(first.parent.is_dir())
        self.assertFalse(second.parent.exists())
        self.assertFalse(second.parent.parent.exists())
        self.assertTrue(any("平铺整理保留非空目录" in message and str(first.parent) in message for message in logs))
        moved = json.loads(queue.read_text(encoding="utf-8"))["items"][0]["path"]
        self.assertEqual(moved, str(self.dirs["pypy"] / "1_compressed.mp4"))

    def test_storage_stats_refresh_picks_up_external_add_move_and_delete(self):
        (self.dirs["pypy"] / "1.mp4").write_bytes(b"pypy")
        (self.dirs["dudu"] / "dudu_3.mp4").write_bytes(b"dudu")
        initial = self.core.get_cache_stats(refresh_library=True)
        self.assertEqual((initial[2], initial[4], initial[6]), (1, 0, 1))
        (self.dirs["pypy"] / "1.mp4").unlink()
        (self.dirs["wd"] / "wd_2.mp4").write_bytes(b"wanna")
        nested = self.dirs["dudu"] / "Group"
        nested.mkdir()
        (nested / "dudu_4.mp4").write_bytes(b"new-dudu")
        cached = self.core.get_cache_stats()
        self.assertEqual((cached[2], cached[4], cached[6]), (0, 0, 1))
        refreshed = self.core.get_cache_stats(refresh_library=True)
        self.assertEqual((refreshed[2], refreshed[4], refreshed[6]), (0, 1, 2))
        self.assertEqual(refreshed[7], len(b"dudu") + len(b"new-dudu"))
        (self.dirs["dudu"] / "dudu_3.mp4").unlink()
        refreshed = self.core.get_cache_stats(refresh_library=True)
        self.assertEqual(refreshed[6], 1)
        self.assertEqual(refreshed[7], len(b"new-dudu"))

    def test_storage_stats_snapshot_is_persisted_and_reloaded(self):
        (self.dirs["pypy"] / "1.mp4").write_bytes(b"pypy")
        with mock.patch.object(self.core, "data_dir", return_value=str(self.root)):
            self.core.emit_stats(refresh_library=True)
            self.assertTrue((self.root / self.core.STORAGE_STATS_FILENAME).is_file())
            self.core._STORAGE_STATS = None
            snapshot = self.core.storage_stats_snapshot()
        self.assertEqual(snapshot["lib_count"], 1)
        self.assertEqual(snapshot["lib_mb"], len(b"pypy") / 1048576)
        self.assertGreater(snapshot["updated_at"], 0)
        self.assertEqual(snapshot["directories"]["pypy"], str(self.dirs["pypy"]))
        self.assertIn("pypy", snapshot["region_times"])
        self.assertFalse(snapshot["no_history"])

    def test_invalid_storage_snapshot_falls_back_to_empty_stats(self):
        with mock.patch.object(self.core, "data_dir", return_value=str(self.root)):
            (self.root / self.core.STORAGE_STATS_FILENAME).write_text(
                json.dumps({"total_mb": "not-a-number"}), encoding="utf-8")
            snapshot = self.core.storage_stats_snapshot()
        self.assertTrue(snapshot["no_history"])
        self.assertEqual(snapshot["total_mb"], 0)

    def test_snapshot_write_failure_does_not_break_stats_events(self):
        events = []
        with mock.patch.object(self.core, "data_dir", return_value=str(self.root)), \
                mock.patch.object(self.core.os, "replace", side_effect=OSError("read-only")), \
                mock.patch.object(self.core, "emit", side_effect=lambda kind, **data: events.append((kind, data))):
            self.core.emit_stats()
        self.assertEqual(events[0][0], "stats")

    def test_cached_stats_do_not_wait_for_an_index_refresh(self):
        (self.dirs["pypy"] / "1.mp4").write_bytes(b"pypy")
        self.core._library_index("pypy", refresh=True)
        started, release, returned = threading.Event(), threading.Event(), threading.Event()
        original_walk = self.core._walk_library_files

        def slow_walk(directory, kind):
            started.set()
            release.wait(5)
            return original_walk(directory, kind)

        refresh_thread = threading.Thread(target=lambda: self.core._library_index("pypy", refresh=True))
        stats_result = []

        def read_stats():
            stats_result.append(self.core.get_cache_stats())
            returned.set()

        with mock.patch.object(self.core, "_walk_library_files", side_effect=slow_walk):
            refresh_thread.start()
            self.assertTrue(started.wait(1))
            stats_thread = threading.Thread(target=read_stats)
            stats_thread.start()
            self.assertTrue(returned.wait(1))
            release.set()
            refresh_thread.join(2)
            stats_thread.join(2)
        self.assertEqual(stats_result[0][2], 1)

    def test_compressed_rename_updates_index_and_stats_tolerate_concurrent_missing_files(self):
        source = self.dirs["pypy"] / "1.mp4"
        marked = self.dirs["pypy"] / "1_compressed.mp4"
        source.write_bytes(b"one")
        self.core._library_index("pypy", refresh=True)
        os.replace(source, marked)
        self.core._library_index_renamed("pypy", str(source), str(marked))
        self.assertEqual(self.core.library_path(1), str(marked))
        stats = self.core.get_cache_stats()
        self.assertEqual(stats[2], 1)
        self.assertEqual(stats[3], marked.stat().st_size)
        self.core._LIBRARY_INDEXES["pypy"][1]["missing.mp4"] = [str(self.dirs["pypy"] / "missing.mp4")]
        stats = self.core.get_cache_stats()
        self.assertEqual(stats[2], 1)
        self.assertEqual(stats[3], marked.stat().st_size)

    def test_rename_during_background_scan_is_not_overwritten_by_stale_index(self):
        source = self.dirs["pypy"] / "1.mp4"
        marked = self.dirs["pypy"] / "1_compressed.mp4"
        source.write_bytes(b"one")
        self.core._library_index("pypy", refresh=True)
        original_walk = self.core._walk_library_files
        calls = 0

        def rename_during_scan(directory, kind):
            nonlocal calls
            calls += 1
            if calls > 1:
                return original_walk(directory, kind)
            stale_paths = original_walk(directory, kind)
            os.replace(source, marked)
            self.core._library_index_renamed(kind, str(source), str(marked))
            return stale_paths

        with mock.patch.object(self.core, "_walk_library_files", side_effect=rename_during_scan):
            files = self.core._library_index("pypy", refresh=True)
        self.assertIn("1_compressed.mp4", files)
        self.assertNotIn("1.mp4", files)
        self.assertEqual(calls, 2)

    def test_organize_updates_cooldown_path(self):
        source = self.dirs["pypy"] / "1.mp4"
        source.write_bytes(b"one")
        self.core._config = {**self.core.DEFAULT_CONFIG, **self.core._config,
                             "cooldown_file": str(source)}
        self.core.organize_library("pypy")
        destination = Path(self.core._config["cooldown_file"])
        self.assertNotEqual(destination, source)
        self.assertTrue(destination.is_file())
        self.assertEqual(self.core.cooldown_file(), str(destination))

    def test_organize_rollback_restores_cooldown_path(self):
        source = self.dirs["pypy"] / "1.mp4"
        source.write_bytes(b"one")
        self.core._config = {**self.core.DEFAULT_CONFIG, **self.core._config,
                             "cooldown_file": str(source)}
        with mock.patch.object(self.core, "_relocate_compression_records",
                               side_effect=RuntimeError("stop after move")):
            with self.assertRaises(RuntimeError):
                self.core.organize_library("pypy")
        self.assertTrue(source.is_file())
        self.assertEqual(self.core._config["cooldown_file"], str(source))
        self.assertEqual(self.core.cooldown_file(), str(source))

    def test_active_cooldown_blocks_library_organization(self):
        source = self.dirs["pypy"] / "1.mp4"
        source.write_bytes(b"one")
        self.core._config = {**self.core.DEFAULT_CONFIG, **self.core._config,
                             "cooldown_file": str(source)}
        self.core._cooldown_armed = True
        self.addCleanup(setattr, self.core, "_cooldown_armed", False)
        with self.assertRaisesRegex(RuntimeError, "CoolDown"):
            self.core.organize_library("pypy")
        self.assertTrue(source.is_file())
        self.assertEqual(self.core._config["cooldown_file"], str(source))

    def test_organize_updates_compression_queue_paths(self):
        source = self.dirs["pypy"] / "1.mp4"
        source.write_bytes(b"one")
        queue = self.root / "compression-queue.json"
        queue.write_text(json.dumps({"items": [{"path": str(source), "status": "done",
                                                "output_mtime_ns": source.stat().st_mtime_ns}]}), encoding="utf-8")
        with mock.patch.object(self.core, "bundle_song_classification", return_value=({1: "kpop"}, {})):
            self.core.organize_library("pypy")
        item = json.loads(queue.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual(item["path"], str(self.dirs["pypy"] / "kpop" / "1.mp4"))
        self.assertIn("compression-queue.json", self.core._STORAGE_FILES)

    def test_directory_migration_updates_compression_queue_paths(self):
        source_dir = self.root / "source"
        source = source_dir / "Group" / "1.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"video")
        target = self.root / "target"
        queue = self.root / "compression-queue.json"
        queue.write_text(json.dumps({"items": [{"path": str(source), "status": "done"}]}), encoding="utf-8")
        self.core._apply_directory_action(str(source_dir), str(target), "pypy", "migrate",
                                          {"library_dir": str(target)}, "library_dir")
        item = json.loads(queue.read_text(encoding="utf-8"))["items"][0]
        self.assertEqual(item["path"], str(target / "Group" / "1.mp4"))

    def test_delete_removes_every_duplicate_path_once(self):
        paths = [self.dirs["pypy"] / group / "1.mp4" for group in ("A", "B")]
        for path in paths:
            path.parent.mkdir()
            path.write_bytes(path.parent.name.encode())
        result = self.core.delete_library_songs("pypy", [1, 1])
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["failed"], [])
        for path in paths:
            self.assertFalse(path.exists())

    def test_first_missing_lookup_does_not_scan_tree_twice(self):
        calls = 0
        original = self.core._library_index

        def counted(kind, refresh=False):
            nonlocal calls
            calls += 1
            return original(kind, refresh)

        with mock.patch.object(self.core, "_library_index", side_effect=counted):
            self.assertIsNone(self.core._existing_library_path("pypy", 999))
        self.assertEqual(calls, 1)

    def test_nested_directory_migration_is_rejected(self):
        source = self.root / "source"
        source.mkdir()
        with self.assertRaises(ValueError):
            self.core._apply_directory_action(str(source), str(source / "nested"), "pypy", "migrate",
                                              {"library_dir": str(source / "nested")}, "library_dir")

    def test_organization_reuses_scan_classification_and_throttles_progress(self):
        for number in range(30):
            (self.dirs["pypy"] / f"{number}.mp4").write_bytes(b"x")
        with mock.patch.object(self.core, "bundle_song_classification", return_value=({}, {})) as classification:
            plan = self.core.library_organization_plan("pypy")
        self.assertEqual(len(plan["moves"]), 30)
        classification.assert_called_once()
        events = []
        self.core.add_listener(lambda kind, data: events.append(data) if kind == "library_organize_progress" else None)
        self.core.organize_library("pypy")
        self.assertEqual([event["phase"] for event in events].count("scanning"), 1)
        self.assertEqual([event["done"] for event in events if event["phase"] == "planning"], [0, 1, 30])
        self.assertEqual([event["done"] for event in events if event["phase"] == "moving"], [0, 1, 25, 30])
        self.assertEqual(events[-1]["phase"], "finalizing")
    def test_legacy_notice_detects_only_flat_owned_files_and_respects_acknowledgement(self):
        for kind, name in (("pypy", "1.mp4"), ("wd", "wd_2.mp4"), ("dudu", "dudu_3.mp4")):
            (self.dirs[kind] / name).write_bytes(b"legacy")
        (self.dirs["pypy"] / "notes.txt").write_text("not a song", encoding="utf-8")
        self.assertEqual(self.core.legacy_library_notice_status()["rooms"], ["pypy", "wd", "dudu"])
        for kind, name in (("pypy", "1.mp4"), ("wd", "wd_2.mp4"), ("dudu", "dudu_3.mp4")):
            target = self.dirs[kind] / "Group" / name
            target.parent.mkdir()
            (self.dirs[kind] / name).replace(target)
        self.assertEqual(self.core.legacy_library_notice_status()["rooms"], [])
        (self.dirs["pypy"] / "4.mp4").write_bytes(b"legacy")
        self.core._config["legacy_library_notice_acknowledged"] = True
        self.assertEqual(self.core.legacy_library_notice_status()["rooms"], [])
        status = self.core.legacy_library_status()
        self.assertEqual(status, {"rooms": ["pypy"], "acknowledged": True})


if __name__ == "__main__":
    unittest.main()
