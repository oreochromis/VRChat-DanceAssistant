"""VRCX co-room aggregation using synthetic local databases only."""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dance_ui import vrcx_stats


def iso(hour, minute=0):
    return datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def ts(hour, minute=0):
    return datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc).timestamp()


class VRCXStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_patch = patch.object(vrcx_stats.core, "data_dir", return_value=str(self.root / "data"))
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.path = self.root / "VRCX.sqlite3"
        connection = sqlite3.connect(self.path)
        connection.executescript("""
            CREATE TABLE gamelog_location (id INTEGER PRIMARY KEY, created_at TEXT, location TEXT, world_id TEXT);
            CREATE TABLE gamelog_join_leave (id INTEGER PRIMARY KEY, created_at TEXT, type TEXT,
                display_name TEXT, location TEXT, user_id TEXT, time INTEGER);
            CREATE TABLE usr_test_friend_log_current (user_id TEXT PRIMARY KEY, display_name TEXT);
            CREATE TABLE usr_test_friend_log_history (id INTEGER PRIMARY KEY, created_at TEXT,
                type TEXT, user_id TEXT, display_name TEXT);
            CREATE TABLE cookies (key TEXT PRIMARY KEY, value TEXT);
        """)
        connection.execute("INSERT INTO cookies VALUES ('default', 'sensitive-session')")
        pypy = next(key for key, value in vrcx_stats.WORLD_KEYS.items() if value == "pypy")
        wd = next(key for key, value in vrcx_stats.WORLD_KEYS.items() if value == "wd")
        connection.executemany("INSERT INTO gamelog_location(created_at,location,world_id) VALUES(?,?,?)", [
            (iso(9), "room-A", pypy), (iso(12), "room-B", wd), (iso(14), "room-C", pypy),
            (iso(16), "room-other", "wrld_other"), (iso(17), "room-D", pypy),
        ])
        connection.executemany("INSERT INTO usr_test_friend_log_current VALUES(?,?)", [
            ("friend-1", "当前昵称一"), ("friend-2", "当前昵称二"), ("friend-3", "当前昵称三"),
            ("friend-4", "其他地图好友"),
        ])
        friend_events = [
            (iso(10), "Friend", "new-pypy", "舞蹈房新好友一"),
            (iso(10, 5), "Friend", "new-pypy", "重复添加仍是一人"),
            (iso(10, 10), "DisplayName", "friend-1", "改名不是新增"),
            (iso(12, 30), "Friend", "new-wd", "舞蹈房新好友二"),
            (iso(16, 30), "Friend", "new-other", "其他地图好友"),
        ]
        connection.executemany(
            "INSERT INTO usr_test_friend_log_history(created_at,type,user_id,display_name) VALUES(?,?,?,?)",
            friend_events)
        events = [
            ("friend-1", "旧昵称", "OnPlayerJoined", iso(9, 30), "room-A"),
            ("friend-1", "旧昵称", "OnPlayerLeft", iso(10, 15), "room-A"),
            ("friend-1", "旧昵称", "OnPlayerJoined", iso(10, 30), "room-A"),
            ("friend-1", "旧昵称", "OnPlayerLeft", iso(11, 30), "room-A"),
            ("friend-2", "旧昵称二", "OnPlayerJoined", iso(10, 45), "room-A"),
            ("friend-2", "旧昵称二", "OnPlayerJoined", iso(10, 46), "room-A"),
            ("friend-2", "旧昵称二", "OnPlayerLeft", iso(10, 50), "room-A"),
            ("friend-3", "旧昵称三", "OnPlayerJoined", iso(12, 30), "room-B"),
            ("friend-4", "其他地图好友", "OnPlayerJoined", iso(16, 30), "room-other"),
            ("usr_test", "本人旧昵称", "OnPlayerJoined", iso(9), "room-A"),
            ("usr_test", "本人旧昵称", "OnPlayerLeft", iso(11, 50), "room-A"),
            ("not-friend", "路人", "OnPlayerJoined", iso(10, 30), "room-A"),
            ("not-friend", "路人", "OnPlayerLeft", iso(11, 30), "room-A"),
            ("friend-1", "缺房间", "OnPlayerJoined", iso(11, 40), ""),
        ]
        connection.executemany(
            "INSERT INTO gamelog_join_leave(user_id,display_name,type,created_at,location,time) VALUES(?,?,?,?,?,0)",
            events)
        connection.commit()
        connection.close()

    def read(self, start=10, end=12):
        return vrcx_stats.read_co_room_stats(self.path, ts(start), ts(end))

    def test_computes_overlap_by_user_and_room_with_current_names(self):
        result = self.read()
        first, second = result["friends"]
        self.assertEqual(first["user_id"], "friend-1")
        self.assertEqual(first["name"], "当前昵称一")
        self.assertEqual(first["seconds"], 4500)
        self.assertEqual(first["sessions"], 2)
        self.assertEqual(first["last_ts"], ts(11, 30))
        self.assertEqual(second["seconds"], 300)
        self.assertEqual(second["sessions"], 1)
        self.assertEqual(result["matched_friends"], 2)
        self.assertNotIn("not-friend", {item["user_id"] for item in result["friends"]})
        self.assertEqual(vrcx_stats.format_duration(first["seconds"]), "1 小时 15 分钟")

    def test_open_sessions_close_at_last_observed_record(self):
        result = self.read(end=15)
        third = next(item for item in result["friends"] if item["user_id"] == "friend-3")
        self.assertEqual(third["seconds"], 5400)

    def test_non_dance_worlds_are_excluded(self):
        result = self.read(end=18)
        self.assertNotIn("friend-4", {item["user_id"] for item in result["friends"]})

    def test_daily_play_self_names_and_sync_result_persistence(self):
        result = self.read()
        self.assertEqual(result["total_play_seconds"], 7200)
        self.assertEqual(result["daily_play"], [{"day": "2026-01-01", "seconds": 7200}])
        self.assertEqual(result["world_play"], {"pypy": 7200, "wd": 0})
        self.assertEqual(result["self_names"], ["本人旧昵称"])
        with patch.object(vrcx_stats.core, "data_dir", return_value=str(self.root / "cached")):
            vrcx_stats.save_vrcx_stats(result)
            loaded = vrcx_stats.load_vrcx_stats()
            self.assertEqual(loaded["friends"], result["friends"])
            self.assertEqual(loaded["daily_play"], result["daily_play"])
            self.assertEqual(loaded["new_friends"], result["new_friends"])
            self.assertGreater(loaded["synced_at"], 0)
            self.assertTrue((self.root / "cached" / vrcx_stats.STATS_FILENAME).is_file())

    def test_original_database_is_archived_and_used_offline(self):
        original = self.read()
        archive = vrcx_stats.archived_vrcx_path()
        self.assertTrue(archive.is_file())
        connection = sqlite3.connect(archive)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            connection.close()
        self.assertIn("gamelog_location", tables)
        self.assertIn("usr_test_friend_log_history", tables)
        self.assertNotIn("cookies", tables)
        self.path.unlink()
        with patch.object(vrcx_stats, "default_vrcx_path", return_value=self.root / "missing.sqlite3"):
            self.assertEqual(vrcx_stats.resolve_vrcx_path(), archive.resolve())
            offline = vrcx_stats.read_co_room_stats(archive, ts(10), ts(12))
        self.assertEqual(offline["friends"], original["friends"])
        self.assertEqual(offline["daily_play"], original["daily_play"])

    def test_new_friends_require_add_event_inside_dance_room(self):
        result = self.read()
        self.assertEqual([item["user_id"] for item in result["new_friends"]], ["new-pypy"])
        both = self.read(end=13)
        self.assertEqual([item["user_id"] for item in both["new_friends"]], ["new-pypy", "new-wd"])
        other = self.read(end=18)
        self.assertNotIn("new-other", {item["user_id"] for item in other["new_friends"]})
        self.assertEqual(self.read(start=15, end=16)["new_friends"], [])

    def test_date_range_and_missing_records_are_handled(self):
        partial = vrcx_stats.read_co_room_stats(self.path, ts(10), ts(10) + 15 * 60)
        self.assertEqual(partial["friends"][0]["seconds"], 900)
        empty = self.read(start=15, end=16)
        self.assertEqual(empty["friends"], [])
        with self.assertRaises(ValueError):
            self.read(start=12, end=12)

    def test_saved_path_and_standard_location(self):
        with patch.object(vrcx_stats.core, "data_dir", return_value=str(self.root / "settings")):
            vrcx_stats.save_vrcx_path(self.path)
            self.assertEqual(vrcx_stats.saved_vrcx_path(), self.path)
            self.assertEqual(vrcx_stats.resolve_vrcx_path(), self.path)
            vrcx_stats.save_self_name("本人名称")
            self.assertEqual(vrcx_stats.saved_self_name(), "本人名称")
            vrcx_stats.save_vrcx_path(self.path)
            self.assertEqual(vrcx_stats.saved_self_name(), "本人名称")
            saved = json.loads((self.root / "settings" / vrcx_stats.SETTINGS_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(saved["path"], str(self.path))
        with patch.dict(os.environ, {"APPDATA": str(self.root)}):
            self.assertEqual(vrcx_stats.default_vrcx_path(), self.root / "VRCX" / "VRCX.sqlite3")

    def test_rejects_non_vrcx_sqlite_and_missing_file(self):
        invalid = self.root / "other.sqlite3"
        sqlite3.connect(invalid).close()
        with self.assertRaises(ValueError):
            vrcx_stats.read_co_room_stats(invalid, ts(10), ts(12))
        with self.assertRaises(ValueError):
            vrcx_stats.read_co_room_stats(self.root / "missing.sqlite3", ts(10), ts(12))


if __name__ == "__main__":
    unittest.main()
