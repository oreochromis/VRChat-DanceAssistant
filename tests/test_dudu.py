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

    def test_event_preload_uses_latest_playlist_and_preserves_song_order(self):
        payload = {"playlists": [
            {"id": 1, "name": "older", "event_date": 100, "ordered_items": [1, 2]},
            {"id": 65, "name": "20261124", "event_date": 200,
             "ordered_items": [2398, 1559, 2400, 258, 1559, 0]},
        ]}
        with mock.patch.object(self.dc, "is_running", return_value=True), \
                mock.patch.object(self.dc, "dudu_api_request",
                                  return_value=(200, {}, json.dumps(payload).encode())), \
                mock.patch.object(self.dc, "dudu_local_path",
                                  side_effect=lambda song_id: "cached" if song_id == 1559 else None), \
                mock.patch.object(self.dc, "start_dudu_download") as start:
            result = self.dc.preload_dudu_latest_event()
        self.assertEqual(result["name"], "20261124")
        self.assertEqual(result["song_ids"], [2398, 1559, 2400, 258])
        self.assertEqual(result["already_cached"], 1)
        self.assertEqual(result["queued"], 3)
        self.assertEqual([call.args[0] for call in start.call_args_list], [258, 2400, 2398])

    def test_event_auto_check_requires_certificate_and_skips_same_decision(self):
        payload = {"playlists": [{"id": 65, "name": "20261124", "event_date": 200,
                                  "ordered_items": [2398, 1559, 2400, 0]}]}
        with mock.patch.object(self.dc, "dudu_api_request") as request:
            self.assertEqual(self.dc.check_dudu_latest_event()["action"], "disabled")
        request.assert_not_called()

        self.dc._config["dudu_enabled"] = True
        expiry = self.dc._dudu_event_expiry(payload["playlists"][0])
        with mock.patch.object(self.dc.time, "time", return_value=expiry - 100), \
                mock.patch.object(self.dc, "dudu_api_request",
                                  return_value=(200, {}, json.dumps(payload).encode())) as request, \
                mock.patch.object(self.dc, "dudu_local_path",
                                  side_effect=lambda song_id: "cached" if song_id == 1559 else None):
            first = self.dc.check_dudu_latest_event()
            self.assertEqual(first["action"], "ask")
            self.assertEqual((first["already_cached"], first["queued"]), (1, 2))
            self.dc.dismiss_dudu_latest_event(first["signature"])
            self.assertEqual(self.dc.check_dudu_latest_event()["action"], "skipped")
            payload["playlists"][0]["ordered_items"].append(2401)
            request.return_value = (200, {}, json.dumps(payload).encode())
            self.assertEqual(self.dc.check_dudu_latest_event()["action"], "ask")

    def test_event_protection_expires_and_strict_capacity_reclaims_normal_files(self):
        cache = self.root / "cache"
        cache.mkdir(exist_ok=True)
        protected = cache / "dudu_1.mp4"
        expired = cache / "dudu_2.mp4"
        normal = cache / "123.mp4"
        protected.write_bytes(b"protected")
        expired.write_bytes(b"expired")
        normal.write_bytes(b"normal")
        now = self.dc.time.time()
        self.dc._write_dudu_event_state({
            "handled_signature": "", "decision": "",
            "playlists": {
                "active": {"name": "active", "expires_at": now + 3600, "song_ids": [1]},
                "expired": {"name": "expired", "expires_at": now - 100, "song_ids": [2]},
            },
        })
        self.dc._config["max_cache_mb"] = 0
        self.dc.cleanup_cache()
        self.assertTrue(protected.exists())
        self.assertFalse(expired.exists())
        self.assertFalse(normal.exists())

    def test_event_capacity_reservation_and_allow_over_limit(self):
        cache = self.root / "cache"
        cache.mkdir(exist_ok=True)
        protected = cache / "dudu_1.mp4"
        normal = cache / "123.mp4"
        protected.write_bytes(b"x" * (700 * 1024))
        normal.write_bytes(b"x" * (500 * 1024))
        self.dc._write_dudu_event_state({
            "handled_signature": "", "decision": "",
            "playlists": {"event": {"name": "event", "expires_at": self.dc.time.time() + 3600,
                                    "song_ids": [1]}},
        })
        self.dc._config["max_cache_mb"] = 1
        target = cache / "dudu_2.mp4"
        self.assertTrue(self.dc._reserve_dudu_event_download(str(target), 300 * 1024, "event"))
        self.assertFalse(normal.exists())
        self.assertTrue(protected.exists())
        with self.assertRaises(self.dc.DuduEventCapacityError):
            self.dc._reserve_dudu_event_download(str(cache / "dudu_3.mp4"), 800 * 1024, "event")
        self.dc._config["dudu_event_allow_over_limit"] = True
        self.assertFalse(self.dc._reserve_dudu_event_download(str(target), 0, "event"))

    def test_event_capacity_failure_pauses_only_remaining_event_tasks(self):
        signature = "event"
        first = ("dudu", None, 1, None, signature)
        second = ("dudu", None, 2, None, signature)
        ordinary = ("dudu", None, 3, None)
        self.dc._preload_dl_items[:] = [second, ordinary]

        def full(*args, **kwargs):
            threading.current_thread()._dance_download_error = self.dc.DuduEventCapacityError("full")
            return False

        with mock.patch.object(self.dc, "download_dudu", side_effect=full):
            self.dc._execute_preload_download(first, self.dc._preload_key(first))
        self.assertEqual(self.dc._preload_dl_items, [ordinary])

    def test_event_custom_https_endpoint_and_validation(self):
        for invalid in ("http://example.com/events", "https://user:pass@example.com/events", "not-a-url"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.dc.update_config({"dudu_event_api_url": invalid})
        url = "https://example.com/custom-events?room=ddfd"
        self.dc.update_config({"dudu_event_api_url": url})
        response = SimpleNamespace(
            status=200, geturl=lambda: url, getheaders=lambda: {},
            read=lambda size: b'{"playlists":[]}')
        context = mock.MagicMock()
        context.__enter__.return_value = response
        with mock.patch.object(self.dc, "_tracked_urlopen", return_value=context) as tracked:
            status, _, body = self.dc._dudu_event_api_request()
        self.assertEqual((status, body), (200, b'{"playlists":[]}'))
        self.assertEqual(tracked.call_args.args[0].full_url, url)
        self.assertEqual(tracked.call_args.args[1], 30)

    def test_automatic_event_preload_rechecks_enablement_mode_and_service(self):
        self.dc._config["dudu_enabled"] = False
        with mock.patch.object(self.dc, "is_running", return_value=True), \
                mock.patch.object(self.dc, "dudu_api_request") as request:
            self.assertEqual(self.dc.preload_dudu_latest_event(automatic=True)["action"], "disabled")
        request.assert_not_called()

        self.dc._config.update(dudu_enabled=True, dudu_event_mode="off")
        with mock.patch.object(self.dc, "is_running", return_value=True), \
                mock.patch.object(self.dc, "dudu_api_request") as request:
            self.assertEqual(self.dc.preload_dudu_latest_event(automatic=True)["action"], "off")
        request.assert_not_called()

        self.dc._config["dudu_event_mode"] = "auto"
        with mock.patch.object(self.dc, "is_running", return_value=False), \
                mock.patch.object(self.dc, "dudu_api_request") as request:
            self.assertEqual(self.dc.preload_dudu_latest_event(automatic=True)["action"], "stopped")
        request.assert_not_called()

    def test_event_preload_rejects_empty_song_list_and_stopped_service(self):
        with mock.patch.object(self.dc, "is_running", return_value=True), \
                mock.patch.object(self.dc, "dudu_api_request",
                                  return_value=(200, {}, b'{"playlists":[{"ordered_items":[0]}]}')):
            with self.assertRaisesRegex(RuntimeError, "没有可预载的歌曲"):
                self.dc.preload_dudu_latest_event()
        with mock.patch.object(self.dc, "is_running", return_value=False), \
                mock.patch.object(self.dc, "dudu_api_request") as request:
            with self.assertRaisesRegex(RuntimeError, "请先启动缓存服务"):
                self.dc.preload_dudu_latest_event()
        request.assert_not_called()

    def test_dudu_actual_state_log_drives_reload_cooldown(self):
        clip = self.root / "relax.mp4"
        clip.write_bytes(b"relax")
        self.dc._config["cooldown_file"] = str(clip)
        self.dc._config["cooldown_trigger"] = "reload"
        state = (
            "2026.11.13 22:46:45 Debug      -  <color=#3EFF00>[22:46:45] : "
            "[VideoQueueHandler.DeserializeVideoSongData] deserialize video data:  "
            "{\"id\":2346,\"info\":\"2346. Nicole Kidman - ADÉLA \",\"user\":\"Player\","
            "\"url\":\"https://api.dudufit.dance/api/v1/videos/2346\"}</color>"
        )
        items = self.dc.extract_queue_items([state])
        self.assertEqual(items[0]["songNum"], 2346)
        watcher = self.dc.LogWatcher()
        watcher._on_queue(items)
        self.dc._running = True
        try:
            self.assertTrue(self.dc.cooldown_arm())
            self.assertEqual(self.dc._cooldown_exclude, "dudu_2346.mp4")
            self.assertFalse(self.dc._cooldown_reload_ready)
            watcher._on_queue(self.dc.extract_queue_items([state]))
            self.assertFalse(self.dc._cooldown_reload_ready)
            self.dc.extract_queue_items([
                "[Video Playback] Attempting to resolve URL 'https://api.dudufit.dance/api/v1/videos/2346?cdn=jpn'"
            ])
            self.assertTrue(self.dc._cooldown_reload_ready)
            self.assertEqual(self.dc.cooldown_check("dudu_2346.mp4"), str(clip))
            self.assertEqual(self.dc.get_cooldown_state()["phase"], "serving")
            self.dc.cooldown_disarm()
            self.assertIsNone(self.dc.cooldown_check("dudu_2346.mp4"))
        finally:
            self.dc.cooldown_disarm()
            self.dc._running = False

    def test_room_change_clears_previous_dudu_current_and_empty_queue_source(self):
        state = (
            "[VideoQueueHandler.DeserializeVideoSongData] deserialize video data:  "
            "{\"id\":2346,\"info\":\"Old DuDu song\",\"user\":\"Player\"}"
        )
        current = self.dc.extract_queue_items([state])
        self.assertEqual(current[0]["songNum"], 2346)
        items = self.dc.extract_queue_items([
            "[Behaviour] Entering Room: WannaDance",
            "[VideoQueueHandler.OnPreSerialization] Queue info serialized: []",
        ])
        self.assertEqual(items, [])
        self.assertIsNone(self.dc._dudu_current_song)
        self.assertEqual(self.dc._queue_room, "wd")
        items = self.dc.extract_queue_items([
            "[VideoQueueHandler.OnPreSerialization] Queue info serialized: []"
        ])
        self.assertEqual(items, [])
        items = self.dc.extract_queue_items(["[Behaviour] Entering Room: DuDu FitDance"])
        self.assertEqual(items, [])
        self.assertEqual(self.dc._queue_room, "dudu")

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
