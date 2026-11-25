import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dance_cache as core
from dance_downloader_gui import show_window_centered
from PySide6.QtCore import QPoint, QRect
from dance_ui.downloader import (
    UnifiedDownloader, download_destination, existing_song_ids, parse_song_ids,
)


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_id_parser_accepts_lists_ranges_and_rejects_invalid_ranges(self):
        self.assertEqual(parse_song_ids("1, 3，5-7、9"), {1, 3, 5, 6, 7, 9})
        self.assertEqual(parse_song_ids(""), set())
        with self.assertRaises(ValueError):
            parse_song_ids("9-2")
        with self.assertRaises(ValueError):
            parse_song_ids("abc")

    def test_existing_ids_scan_category_folders_and_compressed_names(self):
        samples = {
            "pypy": ("Group/12.mp4", "Other/13_compressed.mp4"),
            "wd": ("Group/wd_12.mp4", "Other/wd_13_compressed.mp4"),
            "dudu": ("Group/dudu_12.mp4", "Other/dudu_13_compressed.mp4"),
        }
        for kind, names in samples.items():
            directory = self.root / kind
            for name in names:
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")
            self.assertEqual(existing_song_ids(kind, str(directory)), {12, 13})

    def test_destination_supports_flat_and_official_category_modes(self):
        for kind, filename in (("pypy", "12.mp4"), ("wd", "wd_12.mp4"), ("dudu", "dudu_12.mp4")):
            with mock.patch.object(core, "library_group_name", return_value="Group / Test"):
                classified = Path(download_destination(kind, str(self.root), 12, True))
            flat = Path(download_destination(kind, str(self.root), 12, False))
            self.assertEqual(classified, self.root / "Group Test" / filename)
            self.assertEqual(flat, self.root / filename)

    def test_one_download_uses_explicit_destination_and_retries(self):
        downloader = UnifiedDownloader()
        with mock.patch.object(core, "download_wd", side_effect=[False, False, True]) as download, \
                mock.patch("dance_ui.downloader.time.sleep"):
            result = downloader._download_one("wd", str(self.root), 12, False)
            self.assertEqual(result, "done")
            self.assertEqual(download.call_count, 3)
            self.assertEqual(download.call_args.kwargs["destination"], str(self.root / "wd_12.mp4"))
    def test_window_is_centered_and_activated_on_the_current_screen(self):
        window = mock.Mock()
        window.frameGeometry.return_value = QRect(0, 0, 820, 680)
        screen = mock.Mock()
        screen.availableGeometry.return_value = QRect(2000, 100, 1920, 1080)
        with mock.patch("dance_downloader_gui.QApplication.screenAt", return_value=screen), \
                mock.patch("dance_downloader_gui.QCursor.pos", return_value=QPoint(2500, 200)):
            show_window_centered(window)
        window.showNormal.assert_called_once_with()
        window.move.assert_called_once_with(2550, 300)
        window.raise_.assert_called_once_with()
        window.activateWindow.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
