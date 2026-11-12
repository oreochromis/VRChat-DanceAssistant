import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from dance_ui import dudu_https
from tests.test_core import FakeResponse


class DuduTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location(
            "isolated_dance_cache_dudu", Path(__file__).resolve().parents[1] / "dance_cache.py")
        self.dc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dc)
        self.dc.SCRIPT_DIR = str(self.root)
        self.dc.ROAMING_ROOT = str(self.root / "roaming")
        self.dc._storage_mode_override = "portable"
        self.dc.VRC_LOG_DIR = str(self.root / "missing-logs")
        self.dc._config = self.dc._validated_config({
            **self.dc.DEFAULT_CONFIG,
            "cache_dir": str(self.root / "cache"),
            "dudu_library_dir": str(self.root / "dudu-library"),
            "dudu_permanent": True,
        })
        (self.root / "dudu-library").mkdir()
        self.dc._stop_event.clear()
        self.https_server = None
        self.commands = []

    def tearDown(self):
        self.dc._stop_event.set()
        if self.https_server is not None:
            self.https_server.shutdown()
            self.https_server.server_close()
        self.dc._storage_mode_override = None
        self.temp.cleanup()

    def powershell(self, command):
        self.commands.append(command)
        if command.startswith("Test-Path"):
            return SimpleNamespace(returncode=0, stdout="True\r\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_certificate_lifecycle_and_https_local_song(self):
        with mock.patch.object(dudu_https.core, "data_dir", return_value=str(self.root / "data")), \
                mock.patch.object(dudu_https, "_powershell", side_effect=self.powershell):
            certificate, private_key = dudu_https.ensure_certificate()
            self.assertTrue(dudu_https.certificate_ready())
            ca_path = dudu_https.certificate_paths()[0]
            context = self.dc.ssl.create_default_context(cafile=str(ca_path))
            server = self.dc.ThreadingHTTPServer(("127.0.0.1", 0), self.dc.DanceHandler)
            tls = self.dc.ssl.SSLContext(self.dc.ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(str(certificate), str(private_key))
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            self.https_server = server
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cache = self.root / "cache"
            cache.mkdir()
            (cache / "dudu_2137.mp4").write_bytes(b"local dudu video")
            connection = self.dc._HTTPSConnectionIP("127.0.0.1", self.dc.DUDU_PLAY_HOST,
                                                    server.server_address[1], context=context)
            connection.request("GET", "/videos/2137-1bf61daafc4b.mp4",
                               headers={"Host": self.dc.DUDU_PLAY_HOST})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"local dudu video")
            connection.close()
            dudu_https.remove_certificate()
            self.assertFalse(dudu_https.certificate_directory().exists())
        self.assertTrue(any(command.startswith("Import-Certificate") for command in self.commands))
        self.assertTrue(any(command.startswith("Remove-Item") for command in self.commands))

    def test_queue_and_actual_play_history(self):
        lines = [
            "2026.11.10 20:51:47 Debug - <color=#3EFF00>[VideoQueueHandler.OnPreSerialization] Queue info serialized: "
            "[{\"title\":\"2141. Catch Catch - YENA \",\"playerName\":\"Player\",\"songId\":2141}]</color>",
            "2026.11.10 20:51:59 Debug - <color=#3EFF00>[VideoQueueHandler.DeserializeVideoSongData] "
            "deserialize video data: {\"id\":2137,\"info\":\"2137. Baby One More Time\",\"user\":\"Player\"}</color>",
        ]
        items = self.dc.extract_queue_items(lines)
        self.assertEqual([item["songNum"] for item in items], [2137, 2141])
        self.assertTrue(all(item["dudu"] for item in items))
        self.dc._last_queue_items = items
        current = self.dc.get_queue_snapshot()[0]
        self.assertEqual(current["source"], "dudu")
        self.assertEqual(current["filename"], "dudu_2137.mp4")
        self.dc.record_playing_snapshot(items)
        history = Path(self.dc.played_history_path()).read_text(encoding="utf-8")
        self.assertEqual(json.loads(history)["kind"], "dudu")
        empty = self.dc.extract_queue_items([
            "2026.11.10 20:52:00 Debug - [VideoQueueHandler.OnPreSerialization] Queue info serialized: []"
        ])
        self.assertEqual(empty[0]["songNum"], 2137)

    def test_download_uses_signed_https_path_and_commits_complete_file(self):
        response = FakeResponse([b"du", b"du-video"], total=10)
        connection = FakeConnection = type("FakeConnection", (), {"close": lambda self: None})()
        with mock.patch.object(self.dc, "dudu_video_path", return_value="/videos/2137-hash.mp4?auth_key=temporary"), \
             mock.patch.object(self.dc, "open_upstream", return_value=(connection, response)) as upstream:
            self.assertTrue(self.dc.download_dudu(2137))
        upstream.assert_called_once_with(self.dc.DUDU_PLAY_HOST, "/videos/2137-hash.mp4?auth_key=temporary")
        target = self.root / "dudu-library" / "dudu_2137.mp4"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"dudu-video")
        self.assertFalse(target.with_suffix(".part").exists())

    def test_preload_respects_dudu_enablement(self):
        items = [{"songNum": 2137, "videoName": "Song", "playerName": "Player", "dudu": True}]
        self.dc._config["dudu_enabled"] = False
        self.dc.submit_preload(items)
        self.assertEqual(self.dc._preload_dl_items, [])
        self.dc._config["dudu_enabled"] = True
        self.dc.submit_preload(items)
        self.assertEqual(self.dc._preload_dl_items, [("dudu", None, 2137, None)])

    def test_certificate_corruption_and_expiry_trigger_regeneration(self):
        import datetime
        with mock.patch.object(dudu_https.core, "data_dir", return_value=str(self.root / "data")), \
                mock.patch.object(dudu_https, "_powershell", side_effect=self.powershell):
            first, _ = dudu_https.ensure_certificate()
            first_bytes = first.read_bytes()
            dudu_https.certificate_paths()[2].write_bytes(b"broken private key")
            self.assertFalse(dudu_https.certificate_ready())
            regenerated, _ = dudu_https.ensure_certificate()
            self.assertTrue(dudu_https.certificate_ready())
            self.assertNotEqual(regenerated.read_bytes(), first_bytes)

            real_datetime = datetime.datetime
            class FutureDateTime(real_datetime):
                @classmethod
                def now(cls, tz=None):
                    value = real_datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365 * 3)
                    return cls.fromtimestamp(value.timestamp(), datetime.timezone.utc)

            with mock.patch.object(dudu_https.datetime, "datetime", FutureDateTime):
                self.assertFalse(dudu_https.certificate_ready())
                dudu_https.ensure_certificate()
                self.assertTrue(dudu_https.certificate_ready())

    def test_dudu_directory_change_is_blocked_during_download(self):
        target = self.dc.dudu_target_path(2137)
        key = self.dc._canonical_path(target)
        self.dc._downloads_in_progress[key] = threading.current_thread()
        try:
            with self.assertRaises(ValueError):
                self.dc.update_config({"dudu_library_dir": str(self.root / "new-library")})
            self.assertEqual(self.dc.get_config()["dudu_library_dir"], str(self.root / "dudu-library"))
        finally:
            self.dc._downloads_in_progress.clear()

    def test_dudu_current_song_works_with_cooldown_modes(self):
        item = {"songNum": 2137, "videoName": "Song", "playerName": "Player", "dudu": True}
        self.dc._last_queue_items = [item]
        self.dc._running = True
        self.dc._stop_event.clear()
        try:
            self.dc._config["cooldown_trigger"] = "next"
            self.assertTrue(self.dc.cooldown_arm())
            self.assertIsNone(self.dc.cooldown_check("dudu_2137.mp4", consume=False))
            self.assertIsNotNone(self.dc.cooldown_check("dudu_2141.mp4", consume=False))
            self.dc.cooldown_disarm()
            self.dc._config["cooldown_trigger"] = "reload"
            self.assertTrue(self.dc.cooldown_arm())
            self.assertEqual(self.dc._cooldown_exclude, "dudu_2137.mp4")
        finally:
            self.dc.cooldown_disarm()
            self.dc._running = False

    def test_enable_starts_service_even_when_previously_stopped(self):
        with mock.patch.object(self.dc, "is_running", side_effect=[False, True]), \
             mock.patch.object(self.dc, "update_config"), \
             mock.patch.object(self.dc, "start_services", return_value=None) as start, \
             mock.patch("dance_ui.dudu_https.ensure_certificate"):
            self.dc.configure_dudu(True)
        start.assert_called_once_with()

    def test_redirect_path_and_catalog_offline_fallback(self):
        with mock.patch.object(self.dc, "dudu_api_request", return_value=(
                302, {"location": "https://global-cdn.dudufit.dance/videos/2137-hash.mp4?auth_key=temporary"}, b"")):
            path = self.dc.dudu_video_path(2137)
        self.assertEqual(path, "/videos/2137-hash.mp4?auth_key=temporary")
        payload = {"videos": [{"id": 2137, "name": "Baby One More Time", "artist": "Britney Spears"}]}
        with mock.patch.object(self.dc, "dudu_api_request", return_value=(200, {}, json.dumps(payload).encode())):
            catalog = self.dc._ensure_dudu_catalog()
        self.assertIn(2137, catalog)
        result = self.dc.library_missing("dudu")
        self.assertEqual(result["missing"], [2137])
        self.assertEqual(result["names"][2137], "Baby One More Time - Britney Spears")
        cache = Path(self.dc.data_dir()) / self.dc.DUDU_CATALOG_CACHE_FILENAME
        old = __import__("time").time() - self.dc.DUDU_CATALOG_CACHE_SECONDS - 5
        os.utime(cache, (old, old))
        with mock.patch.object(self.dc, "_fetch_dudu_catalog", side_effect=OSError("offline")):
            self.assertEqual(self.dc.library_missing("dudu")["missing"], [2137])


if __name__ == "__main__":
    unittest.main()
