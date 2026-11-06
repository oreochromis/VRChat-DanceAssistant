"""Statistics backup export and incremental import tests."""
import json
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
            backup = root / "backup.zip"
            with patch.object(stats_backup.core, "data_dir", return_value=str(first)):
                manifest = stats_backup.export_stats_backup(backup)
            self.assertEqual(len(manifest["files"]), 5)
            with zipfile.ZipFile(backup) as archive:
                self.assertEqual(archive.read("vrcx-source.sqlite3"), database)

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
            with patch.object(stats_backup.core, "data_dir", return_value=str(second)):
                result = stats_backup.import_stats_backup(backup)
                self.assertEqual(result["play_history_added"], 1)
                self.assertEqual(result["played_history_added"], 1)
                self.assertEqual(result["settings_added"], 1)
                self.assertTrue(result["stats_updated"])
                self.assertFalse(result["source_imported"])
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
                self.assertEqual((second / "vrcx-source.sqlite3").read_bytes(), database)
                again = stats_backup.import_stats_backup(backup)
                self.assertEqual(again["play_history_added"], 0)
                self.assertEqual(again["played_history_added"], 0)


if __name__ == "__main__":
    unittest.main()
