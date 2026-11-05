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
        self.path = self.root / "VRCX.sqlite3"
        connection = sqlite3.connect(self.path)
        connection.executescript("""
            CREATE TABLE gamelog_location (id INTEGER PRIMARY KEY, created_at TEXT, location TEXT, world_id TEXT);
            CREATE TABLE gamelog_join_leave (id INTEGER PRIMARY KEY, created_at TEXT, type TEXT,
                display_name TEXT, location TEXT, user_id TEXT, time INTEGER);
            CREATE TABLE usr_test_friend_log_current (user_id TEXT PRIMARY KEY, display_name TEXT);
        """)
        pypy, wd = sorted(vrcx_stats.DANCE_WORLD_IDS)
        connection.executemany("INSERT INTO gamelog_location(created_at,location,world_id) VALUES(?,?,?)", [
            (iso(9), "room-A", pypy), (iso(12), "room-B", wd), (iso(14), "room-C", pypy),
            (iso(16), "room-other", "wrld_other"), (iso(17), "room-D", pypy),
        ])
        connection.executemany("INSERT INTO usr_test_friend_log_current VALUES(?,?)", [
            ("friend-1", "当前昵称一"), ("friend-2", "当前昵称二"), ("friend-3", "当前昵称三"),
            ("friend-4", "其他地图好友"),
        ])
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
