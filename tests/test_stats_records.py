"""Software-owned statistics record editing and synchronization tests."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dance_cache as core
from dance_ui import stats_records


class StatsRecordTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.source = self.root / "VRCX.sqlite3"
        connection = sqlite3.connect(self.source)
        connection.executescript("""
            CREATE TABLE gamelog_location (id INTEGER PRIMARY KEY, created_at TEXT, location TEXT, world_id TEXT);
            CREATE TABLE gamelog_join_leave (id INTEGER PRIMARY KEY, created_at TEXT, type TEXT,
                display_name TEXT, location TEXT, user_id TEXT, time INTEGER);
            CREATE TABLE usr_test_friend_log_current (user_id TEXT PRIMARY KEY, display_name TEXT);
            CREATE TABLE usr_test_friend_log_history (id INTEGER PRIMARY KEY, created_at TEXT,
                type TEXT, user_id TEXT, display_name TEXT);
            CREATE TABLE cookies (key TEXT PRIMARY KEY, value TEXT);
        """)
        connection.executemany("INSERT INTO gamelog_location VALUES (?,?,?,?)", [
            (1, "2026-01-01T10:00:00Z", "room-A", "world-old"),
            (2, "2026-01-01T11:00:00Z", "room-B", "world-b"),
        ])
        connection.execute("INSERT INTO usr_test_friend_log_current VALUES ('friend-1', 'Old name')")
        connection.execute("INSERT INTO cookies VALUES ('default', 'sensitive')")
        connection.commit()
        connection.close()
        patcher = patch.object(core, "data_dir", return_value=str(self.data))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_vrcx_copy_crud_and_incremental_sync_preserve_edits(self):
        working = stats_records.ensure_editable_vrcx(self.source)
        self.assertEqual(working, stats_records.editable_vrcx_path())
        connection = sqlite3.connect(working)
        self.assertFalse(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cookies'").fetchone())
        connection.close()
        original = sqlite3.connect(self.source)
        self.assertEqual(original.execute("SELECT value FROM cookies").fetchone()[0], "sensitive")
        original.close()

        values = {"created_at": "2026-01-01T10:00:00Z", "location": "room-A", "world_id": "world-edited"}
        stats_records.save_vrcx_record("location", values, 1)
        stats_records.delete_vrcx_record("location", 2)
        manual = dict(values, created_at="2026-01-01T12:00:00Z", location="manual-room")
        manual_id = stats_records.save_vrcx_record("location", manual)
        self.assertLess(manual_id, 0)
        self.assertEqual(len(stats_records.list_vrcx_records("location", "manual-room")), 1)

        connection = sqlite3.connect(self.source)
        connection.execute("INSERT INTO gamelog_location VALUES (3,'2026-01-01T13:00:00Z','room-C','world-c')")
        connection.execute("UPDATE usr_test_friend_log_current SET display_name='New name' WHERE user_id='friend-1'")
        connection.execute("INSERT INTO usr_test_friend_log_current VALUES ('friend-2', 'Second')")
        connection.commit()
        connection.close()
        self.assertEqual(stats_records.prepare_statistics_source(self.source), working)
        records = {record["id"]: record for record in stats_records.list_vrcx_records("location")}
        self.assertEqual(records[1]["world_id"], "world-edited")
        self.assertNotIn(2, records)
        self.assertEqual(records[3]["location"], "room-C")
        self.assertIn(manual_id, records)
        connection = sqlite3.connect(working)
        friends = dict(connection.execute("SELECT user_id,display_name FROM usr_test_friend_log_current"))
        connection.close()
        self.assertEqual(friends, {"friend-1": "New name", "friend-2": "Second"})

    def test_record_snapshot_restore_and_vrcx_reset(self):
        stats_records.ensure_editable_vrcx(self.source)
        original = {"created_at": "2026-01-01T10:00:00Z", "location": "room-A", "world_id": "world-edited"}
        stats_records.save_vrcx_record("location", original, 1)
        snapshot = stats_records.create_record_snapshot()
        snapshot = stats_records.create_record_snapshot()
        self.assertEqual(len(list((self.data / "record-backups").iterdir())), 2)
        stats_records.save_vrcx_record("location", dict(original, world_id="world-later"), 1)
        stats_records.restore_record_snapshot(snapshot)
        records = {record["id"]: record for record in stats_records.list_vrcx_records("location")}
        self.assertEqual(records[1]["world_id"], "world-edited")
        stats_records.reset_editable_vrcx(self.source)
        records = {record["id"]: record for record in stats_records.list_vrcx_records("location")}
        self.assertEqual(records[1]["world_id"], "world-old")

    def test_history_crud_preserves_unrelated_lines(self):
        self.data.mkdir()
        path = self.data / core.HISTORY_FILENAME
        first = {"ts": 1000, "kind": "pypy", "id": 3528, "name": "Old", "player": "Alice"}
        path.write_text(json.dumps(first) + "\nnot-json\n", encoding="utf-8")
        records = stats_records.list_history_records("queue")
        self.assertEqual(len(records), 1)
        stats_records.save_history_record("queue", dict(first, name="Updated"), records[0]["_line"])
        new = dict(first, ts=2000, id=7152, name="New song", kind="wd")
        line = stats_records.save_history_record("queue", new)
        self.assertEqual(line, 2)
        self.assertEqual(len(stats_records.list_history_records("queue", "New song")), 1)
        stats_records.delete_history_record("queue", 0)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "not-json")
        self.assertEqual(json.loads(lines[1])["name"], "New song")
        stats_records.save_history_record("played", new)
        self.assertEqual(len(stats_records.list_history_records("played")), 1)

    def test_undo_restores_initially_missing_history_files(self):
        stats_records.ensure_editable_vrcx(self.source)
        snapshot = stats_records.create_record_snapshot()
        stats_records.save_history_record("queue", {
            "ts": 1000, "kind": "pypy", "id": 1, "name": "First", "player": "Alice",
        })
        path = self.data / core.HISTORY_FILENAME
        self.assertTrue(path.is_file())
        stats_records.restore_record_snapshot(snapshot)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
