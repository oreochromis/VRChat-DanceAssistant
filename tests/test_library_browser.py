"""Library regressions with Qt signals and temporary state; no real core calls."""
import gc
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QCoreApplication, QEvent, Signal
from PySide6.QtWidgets import QApplication, QWidget
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

    def busy(self, operation):
        return operation in self.operations

    def library_scan(self, kind):
        self.scans.append(kind)

    def library_missing(self, kind):
        self.missing.append(kind)


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

    def browser(self, kind="pypy"):
        browser = LibraryBrowser(self.window, kind)
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
        self.assertEqual(browser.table.item(0, 2).text(), "已下架")
        before = self.window.store.reads
        browser._on_result("library_scan", {"kind": "pypy", "dir": "isolated", "songs": []})
        self.assertEqual(self.window.store.reads - before, 1)

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
