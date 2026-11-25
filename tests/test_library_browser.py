"""Library regressions with Qt signals and temporary state; no real core calls."""
import gc
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QCoreApplication, QEvent, QItemSelectionModel, Signal
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from shiboken6 import isValid

from dance_ui.library_browser import LibraryBrowser
from dance_ui.theme import ThemeStore


class FakeController(QObject):
    result = Signal(str, object)
    error = Signal(str, str)
    busy_changed = Signal(str, bool)
    event = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.operations = set()
        self.scans = []
        self.missing = []
        self.compression_history = []

    def busy(self, operation):
        return operation in self.operations

    def library_scan(self, kind):
        self.scans.append(kind)

    def library_missing(self, kind):
        self.missing.append(kind)

    def compression_records(self):
        return self.compression_history


class CountingStore:
    def __init__(self, root):
        self.store = ThemeStore(root)
        self.reads = 0

    @property
    def active(self):
        self.reads += 1
        return self.store.active


class LibraryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.window = QWidget()
        self.window.controller = FakeController()
        self.window.store = CountingStore(Path(self.directory.name))
        self.window._library_browser = None
        self.addCleanup(self.dispose)

    def dispose(self):
        for browser in self.window.findChildren(LibraryBrowser):
            browser.close()
        self.drain_deletes()
        self.window.deleteLater()
        self.drain_deletes()

    def drain_deletes(self):
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()

    def browser(self, kind="pypy", compression_mode=False, cooldown_mode=False):
        browser = LibraryBrowser(self.window, kind, compression_mode, cooldown_mode)
        self.window._library_browser = browser
        return browser

    def test_dudu_source_and_first_scan_agree(self):
        browser = self.browser("dudu")
        self.assertEqual(browser.source.currentData(), "dudu")
        self.assertEqual(self.window.controller.scans, ["dudu"])

    def test_close_releases_each_table_and_clears_only_its_own_reference(self):
        for _ in range(5):
            browser = self.browser()
            browser.show()
            browser.close()
            self.drain_deletes()
            self.assertFalse(isValid(browser))
            self.assertIsNone(self.window._library_browser)
            self.assertEqual(self.window.findChildren(LibraryBrowser), [])
        old = self.browser("pypy")
        new = self.browser("dudu")
        old.close()
        self.assertIs(self.window._library_browser, new)
        self.drain_deletes()
        # Signals from a worker that outlives the old dialog must stay harmless.
        self.window.controller.result.emit("library_scan", {
            "kind": "pypy", "dir": "old", "songs": [], "total_bytes": 0,
        })
        self.assertEqual(new._dir, "")

    def test_missing_favorite_remains_in_favorites_filter(self):
        browser = self.browser("wd")
        browser._on_result("library_scan", {
            "kind": "wd", "dir": "isolated", "songs": [
                {"id": 10, "name": "Downloaded", "size": 1, "mtime": 0, "favorite": True}],
            "total_bytes": 1,
        })
        browser._on_result("library_missing", {
            "kind": "wd", "source": "WannaDance", "expected": 3,
            "missing": [20, 30], "favorites": [10, 20],
            "names": {20: "Favorite not downloaded", 30: "Other"},
        })
        browser.only_favorites.setChecked(True)
        self.assertEqual(browser.table.rowCount(), 3)
        self.assertFalse(browser.table.isRowHidden(0))
        self.assertFalse(browser.table.isRowHidden(1))
        self.assertTrue(browser.table.isRowHidden(2))
        self.assertIn("💗", browser.table.item(1, 1).text())
        browser.only_missing.setChecked(True)
        self.assertTrue(browser.table.isRowHidden(0))
        self.assertFalse(browser.table.isRowHidden(1))

    def test_escape_rejection_releases_parent_reference_and_table(self):
        browser = self.browser()
        browser.reject()
        self.assertIsNone(self.window._library_browser)
        self.drain_deletes()
        self.assertFalse(isValid(browser))

    def test_missing_rows_read_theme_once_per_batch(self):
        browser = self.browser()
        before = self.window.store.reads
        browser._on_result("library_missing", {
            "kind": "pypy", "source": "test", "expected": 500,
            "missing": list(range(1, 501)), "dead": [1],
        })
        self.assertEqual(self.window.store.reads - before, 1)
        self.assertEqual(browser.table.rowCount(), 500)
        self.assertEqual(browser.table.item(0, 3).text(), "已下架")
        before = self.window.store.reads
        browser._on_result("library_scan", {"kind": "pypy", "dir": "isolated", "songs": []})
        self.assertEqual(self.window.store.reads - before, 1)

    def test_category_and_genre_filters_include_missing_rows(self):
        browser = self.browser("pypy")
        browser._on_result("library_scan", {
            "kind": "pypy", "dir": "isolated", "total_bytes": 2,
            "facets": {"categories": ["jpop", "kpop"], "secondary": ["anime", "pop"]},
            "songs": [
                {"id": 1, "name": "JPOP", "size": 1, "mtime": 0,
                 "category": "jpop", "secondary": ["pop"]},
                {"id": 2, "name": "KPOP", "size": 1, "mtime": 0,
                 "category": "kpop", "secondary": ["pop"]},
            ],
        })
        browser._on_result("library_missing", {
            "kind": "pypy", "source": "official", "expected": 3, "missing": [3],
            "names": {3: "Missing"}, "metadata": {3: {"category": "jpop", "secondary": ["anime"]}},
        })
        self.assertEqual(browser.table.columnCount(), 7)
        self.assertEqual(browser.table.item(0, 2).text(), "jpop")
        browser.category_filter.setCurrentIndex(browser.category_filter.findData("jpop"))
        self.assertFalse(browser.table.isRowHidden(0))
        self.assertTrue(browser.table.isRowHidden(1))
        self.assertFalse(browser.table.isRowHidden(2))
        browser.secondary_filter.setCurrentIndex(browser.secondary_filter.findData("anime"))
        self.assertTrue(browser.table.isRowHidden(0))
        self.assertFalse(browser.table.isRowHidden(2))
        self.assertEqual(browser.category_filter.currentText(), "jpop")

    def test_wd_tag_filter_and_dudu_source_label(self):
        browser = self.browser("wd")
        browser._on_result("library_scan", {
            "kind": "wd", "dir": "isolated", "total_bytes": 2,
            "facets": {"categories": ["Group A", "Group B"], "secondary": ["new", "warm-up"]},
            "songs": [
                {"id": 1, "name": "New", "size": 1, "mtime": 0,
                 "category": "Group A", "secondary": ["new"]},
                {"id": 2, "name": "Warmup", "size": 1, "mtime": 0,
                 "category": "Group B", "secondary": ["warm-up"]},
            ],
        })
        self.assertEqual(browser.category_label.text(), "官方分组")
        self.assertEqual(browser.secondary_label.text(), "标签")
        browser.secondary_filter.setCurrentIndex(browser.secondary_filter.findData("warm-up"))
        self.assertTrue(browser.table.isRowHidden(0))
        self.assertFalse(browser.table.isRowHidden(1))
        browser.source.setCurrentIndex(browser.source.findData("dudu"))
        self.assertEqual(browser.category_label.text(), "歌曲分组")
        self.assertFalse(browser.secondary_filter.isVisible())
        self.assertEqual(browser.category_filter.currentIndex(), 0)

    def test_copy_selected_ids_uses_chinese_separator(self):
        self.window.notify = lambda *args, **kwargs: None
        browser = self.browser("pypy")
        self.assertEqual(browser.cloud_copy_button.text(), "将选中编号复制到剪贴板")
        browser._on_result("library_scan", {
            "kind": "pypy", "dir": "isolated", "total_bytes": 3,
            "songs": [{"id": number, "name": str(number), "size": 1, "mtime": 0}
                      for number in (4861, 4860, 4589)],
        })
        browser.table.selectAll()
        browser.cloud_copy_button.click()
        self.assertEqual(QApplication.clipboard().text(), "4589、4860、4861")

    def test_export_selected_ids_writes_only_selected_available_and_missing_songs(self):
        self.window.notify = lambda *args, **kwargs: None
        browser = self.browser("pypy")
        browser._on_result("library_scan", {
            "kind": "pypy", "dir": "isolated", "total_bytes": 2,
            "songs": [{"id": number, "name": str(number), "size": 1, "mtime": 0}
                      for number in (10, 20)],
        })
        browser._on_result("library_missing", {
            "kind": "pypy", "source": "test", "expected": 3, "missing": [30],
        })
        selection = browser.table.selectionModel()
        for selected_row in (0, 2):
            selection.select(browser.table.model().index(selected_row, 0),
                             QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        destination = Path(self.directory.name) / "selected.txt"
        with mock.patch("dance_ui.library_browser.QFileDialog.getSaveFileName",
                        return_value=(str(destination), "")):
            browser.export_button.click()
        self.assertEqual(destination.read_text(encoding="utf-8"), "10\n30\n")

    def test_compression_status_filter_and_parameter_tooltip(self):
        browser = self.browser("pypy")
        known_path = "D:/library/3.mp4"
        self.window.controller.compression_history = [{
            "path": known_path, "status": "done", "encoder": "qsv", "quality_mode": "quality", "crf": 29,
        }]
        browser._on_result("library_scan", {
            "kind": "pypy", "dir": "D:/library", "total_bytes": 3,
            "songs": [
                {"id": 1, "name": "Plain", "size": 1, "mtime": 0, "path": "D:/library/1.mp4"},
                {"id": 2, "name": "External", "size": 1, "mtime": 0, "path": "D:/library/2_compressed.mp4"},
                {"id": 3, "name": "Known", "size": 1, "mtime": 0, "path": known_path},
            ],
        })
        browser._on_result("library_missing", {
            "kind": "pypy", "source": "test", "expected": 4, "missing": [4],
        })
        self.assertEqual(browser.table.item(0, 5).text(), "未压缩")
        self.assertEqual(browser.table.item(1, 5).text(), "已压缩")
        self.assertEqual(browser.table.item(1, 5).toolTip(), "已压缩，压缩参数未知")
        self.assertEqual(browser.table.item(2, 5).toolTip(), "Intel QSV · 质量档（ICQ 29）")
        browser.only_uncompressed.setChecked(True)
        self.assertFalse(browser.table.isRowHidden(0),
                         [(browser.table.item(row, 0).text(), browser.table.item(row, 0).data(257),
                           browser.table.item(row, 0).data(262), browser.table.isRowHidden(row))
                          for row in range(browser.table.rowCount())])
        self.assertTrue(browser.table.isRowHidden(1))
        self.assertTrue(browser.table.isRowHidden(2))
        self.assertTrue(browser.table.isRowHidden(3))

    def test_compression_browser_hides_missing_and_compresses_only_selected_path(self):
        from dance_ui.i18n import MessageBox
        root = Path(self.directory.name) / "library"
        root.mkdir()
        paths = []
        for number in (1, 2):
            path = root / f"{number}.mp4"
            path.write_bytes(b"original" * 20)
            paths.append(path)
        browser = self.browser("pypy", compression_mode=True)
        hint = "仅显示本地已有视频；支持分类筛选，按住 Ctrl／Shift 多选，使用 Ctrl+A 全选。"
        self.assertIn(hint, [label.text() for label in browser.findChildren(QLabel)])
        browser._on_result("library_scan", {
            "kind": "pypy", "dir": str(root), "total_bytes": 320,
            "songs": [{"id": number, "name": str(number), "size": 160, "mtime": 0, "path": str(path)}
                      for number, path in zip((1, 2), paths)],
        })
        browser._on_result("library_missing", {
            "kind": "pypy", "source": "test", "expected": 3, "missing": [3],
        })
        self.assertTrue(browser.only_missing.isChecked())
        self.assertTrue(browser.only_missing.isHidden())
        self.assertFalse(browser.table.isRowHidden(0))
        self.assertFalse(browser.table.isRowHidden(1))
        self.assertTrue(browser.table.isRowHidden(2))
        self.assertEqual(self.window.controller.missing, [])
        browser.table.selectRow(1)
        self.window.start_compression_paths = mock.Mock()
        with mock.patch("dance_ui.library_browser.QMessageBox.question",
                        return_value=MessageBox.StandardButton.Yes):
            browser.compress_selected_button.click()
        self.window.start_compression_paths.assert_called_once_with("pypy", [str(paths[1])])
        self.assertTrue(browser._closed)

    def test_compression_browser_stays_open_when_start_is_rejected(self):
        from dance_ui.i18n import MessageBox
        root = Path(self.directory.name) / "library"
        root.mkdir()
        path = root / "1.mp4"
        path.write_bytes(b"original")
        browser = self.browser("pypy", compression_mode=True)
        browser._on_result("library_scan", {
            "kind": "pypy", "dir": str(root), "total_bytes": 8,
            "songs": [{"id": 1, "name": "One", "size": 8, "mtime": 0, "path": str(path)}],
        })
        browser.table.selectRow(0)
        self.window.start_compression_paths = mock.Mock(return_value=False)
        with mock.patch("dance_ui.library_browser.QMessageBox.question",
                        return_value=MessageBox.StandardButton.Yes):
            browser.compress_selected_button.click()
        self.assertFalse(browser._closed)

    def test_all_libraries_show_source_and_compress_mixed_selection(self):
        from dance_ui.i18n import MessageBox
        root = Path(self.directory.name)
        pypy = root / "pypy" / "1.mp4"
        wd = root / "wd" / "wd_1.mp4"
        pypy.parent.mkdir(); wd.parent.mkdir()
        pypy.write_bytes(b"pypy"); wd.write_bytes(b"wd")
        browser = self.browser("all", compression_mode=True)
        self.assertEqual(browser.source.currentData(), "all")
        self.assertFalse(browser.table.isColumnHidden(6))
        browser._on_result("library_scan", {
            "kind": "all", "dir": "combined", "songs": [
                {"id": 1, "name": "Same ID PyPy", "size": 4, "mtime": 0, "path": str(pypy), "kind": "pypy"},
                {"id": 1, "name": "Same ID WD", "size": 2, "mtime": 0, "path": str(wd), "kind": "wd"},
            ],
        })
        self.assertEqual(browser.table.rowCount(), 2)
        self.assertEqual(browser.table.item(0, 6).text(), "PyPyDance")
        self.assertEqual(browser.table.item(1, 6).text(), "WannaDance")
        browser.table.selectAll()
        self.window.start_compression_paths = mock.Mock()
        with mock.patch("dance_ui.library_browser.QMessageBox.question",
                        return_value=MessageBox.StandardButton.Yes):
            browser.compress_selected_button.click()
        self.window.start_compression_paths.assert_called_once_with("all", {
            "pypy": [str(pypy)], "wd": [str(wd)], "dudu": [],
        })

    def test_cooldown_search_with_and_without_space_returns_same_songs(self):
        browser = self.browser("pypy")
        names = {
            344: "Modern Loneliness COOL DOWN", 450: "Save Room For Us Cool Down",
            4280: "[Cool Down] DREAM", 4689: "[Cool Down] Good Goodbye",
            1067: "THE WAY I ARE | CoolDown", 3621: "Song | CoolDown", 10: "Unrelated",
        }
        browser._on_result("library_scan", {
            "kind": "pypy", "dir": "isolated", "songs": [
                {"id": sid, "name": name, "size": 1, "mtime": 0} for sid, name in names.items()],
        })
        def visible_ids():
            return [int(browser.table.item(row, 0).text()) for row in range(browser.table.rowCount())
                    if not browser.table.isRowHidden(row)]
        browser.search.setText("cooldown")
        expected = [344, 450, 1067, 3621, 4280, 4689]
        self.assertEqual(visible_ids(), expected)
        self.assertIn("当前筛选 6 首", browser.summary.text())
        browser.search.setText("cool down")
        self.assertEqual(visible_ids(), expected)
        self.assertIn("当前筛选 6 首", browser.summary.text())

    def test_all_library_missing_rows_keep_duplicate_ids_separate(self):
        browser = self.browser("all")
        browser._on_result("library_scan", {"kind": "all", "dir": "combined", "songs": [
            {"id": 1, "name": "Local Same ID", "size": 1, "mtime": 0, "kind": "pypy"},
        ]})
        browser._on_result("library_missing", {
            "kind": "all", "source": "全部曲库", "expected": 2,
            "missing_entries": [
                {"id": 1, "kind": "pypy", "name": "PyPy Missing", "dead": False},
                {"id": 1, "kind": "wd", "name": "WD Missing", "dead": False},
            ],
        })
        self.assertEqual(browser.table.rowCount(), 3)
        self.assertEqual(browser.table.item(0, 6).text(), "PyPyDance")
        self.assertEqual(browser.table.item(1, 6).text(), "PyPyDance")
        self.assertEqual(browser.table.item(2, 6).text(), "WannaDance")
        browser.only_missing.setChecked(True)
        self.assertTrue(browser.table.isRowHidden(0))
        self.assertFalse(browser.table.isRowHidden(1))
        self.assertFalse(browser.table.isRowHidden(2))

    def test_cooldown_picker_searches_all_libraries_and_rejects_missing_file(self):
        root = Path(self.directory.name)
        local = root / "1.mp4"
        local.write_bytes(b"video")
        browser = self.browser("all", cooldown_mode=True)
        self.assertEqual(browser.search.text(), "cool down")
        self.assertFalse(browser.only_missing.isChecked())
        browser._on_result("library_scan", {
            "kind": "all", "dir": str(root), "songs": [
                {"id": 1, "name": "Local CoolDown", "size": 5, "mtime": 0,
                 "path": str(local), "kind": "pypy"},
                {"id": 2, "name": "Unrelated", "size": 5, "mtime": 0, "kind": "wd"},
            ],
        })
        browser._on_result("library_missing", {
            "kind": "all", "source": "全部曲库", "expected": 3,
            "missing_entries": [{"id": 3, "kind": "wd", "name": "[Cool Down] Missing", "dead": False}],
        })
        self.assertFalse(browser.table.isRowHidden(0))
        self.assertTrue(browser.table.isRowHidden(1))
        self.assertFalse(browser.table.isRowHidden(2))
        self.window.notify = mock.Mock()
        self.window.select_cooldown_from_library = mock.Mock()
        browser.table.selectRow(2)
        browser.cooldown_select_button.click()
        self.window.select_cooldown_from_library.assert_not_called()
        browser.table.selectRow(0)
        browser.cooldown_select_button.click()
        self.window.select_cooldown_from_library.assert_called_once_with(str(local))

    def test_quark_action_is_not_present(self):
        browser = self.browser("wd")
        self.assertFalse(hasattr(browser, "quark_button"))
        self.assertFalse(hasattr(browser, "open_quark_download"))

    def test_reopened_browser_waits_for_previous_scan_then_scans_its_source(self):
        controller = self.window.controller
        controller.operations.add("library_scan")
        browser = self.browser("dudu")
        self.assertEqual(controller.scans, [])
        controller.result.emit("library_scan", {
            "kind": "wd", "dir": "old", "songs": [], "total_bytes": 0,
        })
        controller.result.emit("library_missing", {
            "kind": "wd", "source": "old", "missing": [77], "expected": 1,
        })
        self.assertEqual(browser.table.rowCount(), 0)
        controller.operations.remove("library_scan")
        controller.busy_changed.emit("library_scan", False)
        self.app.processEvents()
        self.assertEqual(controller.scans, ["dudu"])
        controller.result.emit("library_scan", {
            "kind": "dudu", "dir": "new", "songs": [], "total_bytes": 0,
        })
        self.assertEqual(browser._dir, "new")

    def test_closing_before_scheduled_refresh_prevents_a_new_request(self):
        controller = self.window.controller
        controller.operations.add("library_scan")
        browser = self.browser()
        controller.operations.remove("library_scan")
        controller.busy_changed.emit("library_scan", False)
        browser.close()
        self.drain_deletes()
        self.assertEqual(controller.scans, [])

    def test_reopened_browser_retries_catalog_after_old_source_finishes(self):
        controller = self.window.controller
        controller.operations.add("library_missing")
        browser = self.browser("dudu")
        controller.result.emit("library_scan", {
            "kind": "dudu", "dir": "new", "songs": [], "total_bytes": 0,
        })
        self.assertEqual(controller.missing, [])
        controller.result.emit("library_missing", {
            "kind": "wd", "source": "old", "missing": [77], "expected": 1,
        })
        controller.operations.remove("library_missing")
        controller.busy_changed.emit("library_missing", False)
        self.app.processEvents()
        self.assertEqual(controller.missing, ["dudu"])
        self.assertEqual(browser.table.rowCount(), 0)
