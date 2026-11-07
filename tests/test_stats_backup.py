"""Statistics backup export and incremental import tests."""
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from dance_ui import stats_backup


class StatsBackupTests(unittest.TestCase):
    def test_export_and_incremental_import_preserve_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            first.mkdir()
            entries = [
                {"ts": 1000, "kind": "pypy", "id": 1, "name": "A", "player": "Alice"},
                {"ts": 2000, "kind": "wd", "id": 2, "name": "B", "player": "Bob"},
            ]
            for name in stats_backup.HISTORY_FILES:
                (first / name).write_text(json.dumps(entries[0]) + "\n", encoding="utf-8")
            (first / "vrcx-settings.json").write_text(json.dumps({"self_name": "Current"}), encoding="utf-8")
            (first / "vrcx-stats.json").write_text(json.dumps({"friends": [], "synced_at": 10}), encoding="utf-8")
            database = b"SQLite format 3\x00" + bytes(range(32))
            (first / "vrcx-source.sqlite3").write_bytes(database)
            (first / "profile.json").write_text(json.dumps({"id": "usr_test", "name": "测试昵称"}), encoding="utf-8")
            (first / "profile-avatar.png").write_bytes(b"avatar")
            (first / "avatar-index.json").write_text(json.dumps({
                "usr_existing": {"url": "incoming-existing"}, "usr_new": {"url": "new"},
            }), encoding="utf-8")
            (first / "avatars").mkdir()
            (first / "avatars" / "existing.png").write_bytes(b"incoming avatar")
            (first / "avatars" / "new.png").write_bytes(b"new avatar")
            (first / "room_events.jsonl").write_text(json.dumps({
                "ts": 1000, "kind": "location", "world_id": "wrld_test", "location": "room-A",
            }) + "\n", encoding="utf-8")
            backup = root / "backup.zip"
            with patch.object(stats_backup.core, "data_dir", return_value=str(first)):
                manifest = stats_backup.export_stats_backup(backup)
            self.assertEqual(len(manifest["files"]), 11)
            with zipfile.ZipFile(backup) as archive:
                self.assertEqual(archive.read("vrcx-source.sqlite3"), database)
                self.assertEqual(archive.read("avatars/new.png"), b"new avatar")

            for name in stats_backup.HISTORY_FILES:
                (first / name).write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
            (first / "vrcx-settings.json").write_text(json.dumps({"self_name": "Incoming", "path": "incoming.sqlite3"}), encoding="utf-8")
            (first / "vrcx-stats.json").write_text(json.dumps({"friends": [{"name": "New"}], "synced_at": 30}), encoding="utf-8")
            with patch.object(stats_backup.core, "data_dir", return_value=str(first)):
                stats_backup.export_stats_backup(backup)

            second = root / "second"
            second.mkdir()
            for name in stats_backup.HISTORY_FILES:
                (second / name).write_text(json.dumps(entries[0]) + "\n", encoding="utf-8")
            (second / "vrcx-settings.json").write_text(json.dumps({"self_name": "Keep"}), encoding="utf-8")
            (second / "vrcx-stats.json").write_text(json.dumps({"friends": [], "synced_at": 20}), encoding="utf-8")
            (second / "vrcx-source.sqlite3").write_bytes(b"existing database")
            (second / "avatar-index.json").write_text(json.dumps({
                "usr_existing": {"url": "keep-existing"},
            }), encoding="utf-8")
            (second / "avatars").mkdir()
            (second / "avatars" / "existing.png").write_bytes(b"existing avatar")
            with patch.object(stats_backup.core, "data_dir", return_value=str(second)):
                preview = stats_backup.inspect_stats_backup(backup)
                self.assertEqual(preview["play_history_added"], 1)
                self.assertEqual(preview["played_history_added"], 1)
                self.assertEqual(preview["room_events_added"], 1)
                self.assertEqual(preview["settings_added"], 1)
                self.assertEqual(preview["avatars_added"], 1)
                self.assertTrue(preview["stats_updated"])
                self.assertTrue(preview["profile_imported"])
                self.assertFalse(preview["source_imported"])
                result = stats_backup.import_stats_backup(backup)
                self.assertEqual(result["play_history_added"], 1)
                self.assertEqual(result["played_history_added"], 1)
                self.assertEqual(result["settings_added"], 1)
                self.assertTrue(result["stats_updated"])
                self.assertFalse(result["source_imported"])
                self.assertTrue(result["profile_imported"])
                self.assertEqual(result["avatars_added"], 1)
                self.assertEqual(result["room_events_added"], 1)
                self.assertTrue((second / "room_events.jsonl").is_file())
                self.assertEqual((second / "profile-avatar.png").read_bytes(), b"avatar")
                self.assertEqual((second / "avatars" / "existing.png").read_bytes(), b"existing avatar")
                self.assertEqual((second / "avatars" / "new.png").read_bytes(), b"new avatar")
                merged_index = json.loads((second / "avatar-index.json").read_text(encoding="utf-8"))
                self.assertEqual(merged_index["usr_existing"]["url"], "keep-existing")
                self.assertEqual(merged_index["usr_new"]["url"], "new")
                self.assertEqual(json.loads((second / "vrcx-settings.json").read_text())["self_name"], "Keep")
                self.assertEqual(json.loads((second / "vrcx-settings.json").read_text())["path"], "incoming.sqlite3")
                self.assertEqual(json.loads((second / "vrcx-stats.json").read_text())["synced_at"], 30)
                self.assertEqual((second / "vrcx-source.sqlite3").read_bytes(), b"existing database")
                for name in stats_backup.HISTORY_FILES:
                    lines = (second / name).read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(lines), 2)
                (second / "vrcx-source.sqlite3").unlink()
                restored = stats_backup.import_stats_backup(backup)
                self.assertTrue(restored["source_imported"])
                self.assertEqual(restored["avatars_added"], 0)
                self.assertEqual((second / "vrcx-source.sqlite3").read_bytes(), database)
                again = stats_backup.import_stats_backup(backup)
                self.assertEqual(again["play_history_added"], 0)
                self.assertEqual(again["played_history_added"], 0)
                self.assertEqual(again["room_events_added"], 0)

    def test_edited_vrcx_copy_is_backed_up_without_overwriting_existing_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            first.mkdir()
            edited = first / stats_backup.stats_records.EDITABLE_SOURCE_FILENAME
            connection = sqlite3.connect(edited)
            connection.execute("CREATE TABLE cookies (key TEXT, value TEXT)")
            connection.execute("INSERT INTO cookies VALUES ('default', 'sensitive')")
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('edited')")
            connection.commit()
            connection.close()
            backup = root / "edited.zip"
            with patch.object(stats_backup.core, "data_dir", return_value=str(first)):
                manifest = stats_backup.export_stats_backup(backup)
            self.assertIn(stats_backup.stats_records.EDITABLE_SOURCE_FILENAME, manifest["files"])
            connection = sqlite3.connect(edited)
            self.assertFalse(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cookies'").fetchone())
            connection.close()
            second = root / "second"
            with patch.object(stats_backup.core, "data_dir", return_value=str(second)):
                stats_backup.import_stats_backup(backup)
                imported = second / stats_backup.stats_records.EDITABLE_SOURCE_FILENAME
                self.assertTrue(imported.is_file())
                connection = sqlite3.connect(imported)
                connection.execute("INSERT INTO marker VALUES ('keep')")
                connection.commit()
                connection.close()
                stats_backup.import_stats_backup(backup)
                connection = sqlite3.connect(imported)
                values = connection.execute("SELECT value FROM marker ORDER BY rowid").fetchall()
                connection.close()
                self.assertCountEqual(values, [("keep",), ("edited",)])


if __name__ == "__main__":
    unittest.main()
