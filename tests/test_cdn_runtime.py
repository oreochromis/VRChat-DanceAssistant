"""Regression checks for CDN choice races and shared bookmark storage, offline."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_core
from dance_ui.cdn_favorites import CDNFavorites


class CDNSelectionTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown

    def auto_scan(self, change):
        def measure(*_):
            change()
            return [{"ip": "8.8.8.8", "mbps": 4.0, "error": None}]
        with mock.patch.object(self.dc._stop_event, "wait", return_value=False), \
             mock.patch.object(self.dc, "_quick_cdn_candidates", return_value=["8.8.8.8"]), \
             mock.patch.object(self.dc, "_pick_test_file", return_value="test.mp4"), \
             mock.patch.object(self.dc, "_quick_speedtest", side_effect=measure):
            self.dc.auto_speedtest_apply()

    def test_auto_scan_retains_a_later_manual_choice(self):
        self.auto_scan(lambda: self.dc.update_config({"preferred_cdn_ip": "9.9.9.9"}))
        self.assertEqual(self.dc.get_config()["preferred_cdn_ip"], "9.9.9.9")

    def test_auto_scan_respects_disabled_option(self):
        self.auto_scan(lambda: self.dc.update_config({"auto_prefer_cdn": False}))
        self.assertEqual(self.dc.get_config()["preferred_cdn_ip"], "")

    def test_toggle_off_and_back_on_invalidates_old_scan(self):
        def toggle():
            self.dc.update_config({"auto_prefer_cdn": False})
            self.dc.update_config({"auto_prefer_cdn": True})
        self.auto_scan(toggle)
        self.assertEqual(self.dc.get_config()["preferred_cdn_ip"], "")

    def test_unrelated_config_edit_does_not_discard_valid_scan(self):
        self.auto_scan(lambda: self.dc.update_config({"preload_count": 3}))
        self.assertEqual(self.dc.get_config()["preferred_cdn_ip"], "8.8.8.8")
        self.assertEqual(self.dc.get_config()["preload_count"], 3)

    def test_manual_scan_retains_a_later_manual_choice(self):
        def measure(*_):
            self.dc.update_config({"preferred_cdn_ip": "9.9.9.9", "auto_prefer_cdn": False})
            return [{"ip": "8.8.8.8", "mbps": 4.0, "error": None}]
        with mock.patch.object(self.dc, "_measure_ips", side_effect=measure), \
             mock.patch.object(self.dc, "_pick_test_file", return_value="test.mp4"):
            self.dc.speed_test_ips(["8.8.8.8"])
        self.assertEqual(self.dc.get_config()["preferred_cdn_ip"], "9.9.9.9")

    def test_manual_scan_can_apply_while_service_is_stopped_and_auto_disabled(self):
        self.dc._stop_event.set()
        self.dc.update_config({"auto_prefer_cdn": False})
        with mock.patch.object(self.dc, "_measure_ips", return_value=[
                {"ip": "8.8.8.8", "mbps": 4.0, "error": None}]), \
             mock.patch.object(self.dc, "_pick_test_file", return_value="test.mp4"):
            self.dc.speed_test_ips(["8.8.8.8"])
        self.assertEqual(self.dc.get_config()["preferred_cdn_ip"], "8.8.8.8")

    def test_sampled_full_scan_retains_a_later_manual_choice(self):
        def measure(*_):
            self.dc.update_config({"preferred_cdn_ip": "9.9.9.9"})
            return [{"ip": "8.8.8.8", "mbps": 4.0, "error": None}]
        with mock.patch.object(self.dc, "_sample_cf_ips", return_value=["8.8.8.8"]), \
             mock.patch.object(self.dc, "_download_test", return_value=(1.0, None, .1)), \
             mock.patch.object(self.dc, "_record_speedtest_results"), \
             mock.patch.object(self.dc, "_measure_ips", side_effect=measure), \
             mock.patch.object(self.dc, "_pick_test_file", return_value="test.mp4"):
            self.dc.full_scan_ips()
        self.assertEqual(self.dc.get_config()["preferred_cdn_ip"], "9.9.9.9")

    def test_stale_choice_rejected_in_config_transaction(self):
        old = self.dc._cdn_selection_revision
        self.dc.update_config({"preferred_cdn_ip": "9.9.9.9"})
        self.dc.update_config({"preferred_cdn_ip": ""})
        result = self.dc.update_config({"preferred_cdn_ip": "8.8.8.8"}, expected_cdn_revision=old)
        self.assertFalse(result["changed"])
        self.assertEqual(self.dc.get_config()["preferred_cdn_ip"], "")
        self.assertEqual(json.loads((self.root / "config.json").read_text())["preferred_cdn_ip"], "")

    def test_seven_kib_per_second_rechecks_before_two_mib(self):
        conn = SimpleNamespace(host="cdn.pypy.dance", _selected_ip="1.1.1.1")
        self.dc._running = True
        self.dc._stop_event.clear()
        chunk = 64 * 1024
        seconds = chunk / (7 * 1024)
        with mock.patch.object(self.dc.time, "monotonic", side_effect=[seconds, seconds * 2, seconds * 3]), \
             mock.patch.object(self.dc, "_maybe_recheck_slow_connection") as recheck:
            for _ in range(3):
                self.dc._note_upstream_progress(conn, chunk)
        recheck.assert_called_once()
        self.assertLess(seconds * 3, 30)
        self.assertLess(chunk * 3, 2 * 1048576)

    def test_short_or_fast_connections_do_not_recheck(self):
        self.dc._running = True
        self.dc._stop_event.clear()
        with mock.patch.object(self.dc, "_maybe_recheck_slow_connection") as recheck:
            for count, stamps in [(65536, [0, 1]), (8 * 1048576, [0, 11])]:
                conn = SimpleNamespace(host="cdn.pypy.dance", _selected_ip="1.1.1.1")
                with mock.patch.object(self.dc.time, "monotonic", side_effect=stamps):
                    for _ in stamps:
                        self.dc._note_upstream_progress(conn, count)
        recheck.assert_not_called()

    def test_streaming_reader_uses_available_bytes_without_filling_a_large_buffer(self):
        class Response:
            def read1(self, size):
                self.size = size
                return b"available"
            def read(self, size):
                raise AssertionError("read(n) may block until n bytes arrive")
        response = Response()
        self.assertEqual(self.dc._read_download_chunk(response), b"available")
        self.assertEqual(response.size, 65536)

    def test_gui_and_background_share_store_and_demo_stays_isolated(self):
        from dance_ui.app import MainWindow
        import dance_cache as ui_core
        root = self.root / "appearance"
        old = CDNFavorites(root)
        old.save(["8.8.4.4"])
        window = SimpleNamespace(controller=SimpleNamespace(demo=False,
                                 storage_info=lambda: {"active_dir": self.root}),
                                 store=SimpleNamespace(root_dir=root))
        with mock.patch.object(ui_core, "data_dir", return_value=str(self.root)):
            MainWindow._load_cdn_favorites(window)
        self.assertEqual(window.cdn_favorites.items, ["8.8.4.4"])
        self.assertEqual(self.dc._favorite_cdn_ips(), ["8.8.4.4"])
        demo = SimpleNamespace(controller=SimpleNamespace(demo=True), store=SimpleNamespace(root_dir=self.root / "demo"))
        with mock.patch.object(ui_core, "data_dir", side_effect=AssertionError("demo accessed real data")):
            MainWindow._load_cdn_favorites(demo)
            demo.cdn_favorites.save(["1.1.1.1"])
        self.assertEqual(self.dc._favorite_cdn_ips(), ["8.8.4.4"])


class BookmarkMigrationTests(unittest.TestCase):
    def test_merge_once_and_do_not_resurrect_deleted_ip(self):
        with tempfile.TemporaryDirectory() as root:
            old, new = Path(root) / "appearance", Path(root) / "data"
            CDNFavorites(old).save(["1.1.1.1", "8.8.8.8"])
            CDNFavorites(new).save(["9.9.9.9", "1.1.1.1"])
            bookmarks = CDNFavorites(new, legacy_roots=(old,))
            self.assertEqual(bookmarks.items, ["9.9.9.9", "1.1.1.1", "8.8.8.8"])
            bookmarks.save(["9.9.9.9"])
            self.assertEqual(CDNFavorites(new, legacy_roots=(old,)).items, ["9.9.9.9"])
            self.assertEqual(CDNFavorites(old).items, ["1.1.1.1", "8.8.8.8"])

    def test_bad_source_or_target_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            old, new = Path(root) / "appearance", Path(root) / "data"
            old.mkdir()
            (old / "cdn-favorites.json").write_text("broken")
            self.assertTrue(CDNFavorites(new, legacy_roots=(old,)).warning)
            self.assertEqual((old / "cdn-favorites.json").read_text(), "broken")
            CDNFavorites(new).save(["9.9.9.9"])
            (new / "cdn-favorites.json").write_text("also broken")
            self.assertTrue(CDNFavorites(new, legacy_roots=(old,)).warning)
            self.assertEqual((new / "cdn-favorites.json").read_text(), "also broken")

    def test_migration_failure_retains_both_files_and_retries(self):
        with tempfile.TemporaryDirectory() as root:
            old, new = Path(root) / "appearance", Path(root) / "data"
            CDNFavorites(old).save(["1.1.1.1"])
            CDNFavorites(new).save(["9.9.9.9"])
            with mock.patch("dance_ui.cdn_favorites.os.replace", side_effect=OSError("disk full")):
                self.assertTrue(CDNFavorites(new, legacy_roots=(old,)).warning)
            self.assertEqual(CDNFavorites(new).items, ["9.9.9.9"])
            self.assertEqual(CDNFavorites(new, legacy_roots=(old,)).items, ["9.9.9.9", "1.1.1.1"])
