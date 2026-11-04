"""曲库浏览窗口：独立非模态对话框，列出本地曲库文件，支持搜索/查缺失/删除。"""
from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QLineEdit, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .pages import button
from .widgets import bytes_text, label

_KINDS = (("pypy", "PyPyDance 曲库"), ("wd", "WannaDance 曲库"))
_OPS = ("library_scan", "library_missing", "library_delete")


class _SortItem(QTableWidgetItem):
    """UserRole 里放数值排序键（编号/大小/时间），文本列走默认字符串比较。"""

    def __lt__(self, other):
        mine = self.data(Qt.ItemDataRole.UserRole)
        theirs = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            return mine < theirs
        return super().__lt__(other)


class LibraryBrowser(QDialog):
    def __init__(self, window, kind: str):
        super().__init__(window)
        self._window = window
        self.controller = window.controller
        self._kind = kind if kind in dict(_KINDS) else "pypy"
        self._dir = ""
        self._songs = []
        self._pending = set()

        self.setWindowTitle("曲库浏览")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(760, 560)

        box = QVBoxLayout(self)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        top = QHBoxLayout()
        self.source = QComboBox()
        for value, text in _KINDS:
            self.source.addItem(text, value)
        self.source.setCurrentIndex(0 if self._kind == "pypy" else 1)
        self.source.currentIndexChanged.connect(self._source_changed)
        top.addWidget(self.source)
        self.search = QLineEdit()
        self.search.setPlaceholderText("按编号或歌名过滤…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        self.refresh_button = button("刷新", self.refresh)
        top.addWidget(self.refresh_button)
        box.addLayout(top)

        self.summary = label("正在扫描…", muted=True)
        box.addWidget(self.summary)
        self.missing_label = label("", muted=True, wrap=True)
        self.missing_label.hide()
        box.addWidget(self.missing_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["编号", "歌名", "大小", "修改时间"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(self._play_current)
        box.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.missing_button = button("查缺失", self.find_missing)
        bottom.addWidget(self.missing_button)
        self.open_dir_button = button("打开曲库目录", self.open_directory)
        bottom.addWidget(self.open_dir_button)
        bottom.addStretch(1)
        self.delete_button = button("删除选中…", self.delete_selected)
        self.delete_button.setProperty("danger", True)
        bottom.addWidget(self.delete_button)
        box.addLayout(bottom)

        self.controller.result.connect(self._on_result)
        self.controller.error.connect(self._on_error)
        self.controller.busy_changed.connect(self._on_busy)
        self.refresh()

    # ---------------- 数据流 ----------------

    def refresh(self):
        self.summary.setText("正在扫描…")
        self.table.setRowCount(0)
        self.controller.library_scan(self._kind)

    def find_missing(self):
        self.missing_label.setText("正在对照清单检查缺失…")
        self.missing_label.show()
        self.controller.library_missing(self._kind)

    def _on_result(self, operation, result):
        if operation == "library_scan":
            if result is None or not isinstance(result, dict) or result.get("kind", self._kind) != self._kind:
                if result is None:
                    self._show_no_dir()
                return
            self._populate(result)
        elif operation == "library_missing":
            if result is None:
                self.missing_label.setText("未设置曲库目录，无法检查。")
                return
            self._show_missing(result)
        elif operation == "library_delete":
            self._show_deleted(result or {})

    def _on_error(self, operation, message):
        if operation not in _OPS:
            return
        self.summary.setText("操作失败：" + message)
        if operation in ("library_missing", "library_delete"):
            self.missing_label.setText(message)
            self.missing_label.show()

    def _on_busy(self, operation, busy):
        if operation not in _OPS:
            return
        if busy:
            self._pending.add(operation)
        else:
            self._pending.discard(operation)
        blocked = bool(self._pending)
        for widget in (self.refresh_button, self.missing_button,
                       self.delete_button, self.source):
            widget.setEnabled(not blocked)

    # ---------------- 展示 ----------------

    def _show_no_dir(self):
        self._dir = ""
        self._songs = []
        self.summary.setText("未设置曲库目录或目录不存在，请先在「存储」页配置并应用更改。")

    def _populate(self, result):
        self._dir = result.get("dir") or ""
        self._songs = result.get("songs") or []
        parts = result.get("parts", 0)
        total = result.get("total_bytes", 0)
        text = f"共 {len(self._songs)} 首 · {bytes_text(total)}"
        if parts:
            text += f" · {parts} 个未完成下载（.part）"
        if self._dir:
            text += f" — {self._dir}"
        self.summary.setText(text)

        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(self._songs))
        for row, song in enumerate(self._songs):
            sid = int(song.get("id", 0))
            item_id = _SortItem(str(sid))
            item_id.setData(Qt.ItemDataRole.UserRole, sid)
            item_name = _SortItem(str(song.get("name") or "—"))
            item_size = _SortItem(bytes_text(song.get("size", 0)))
            item_size.setData(Qt.ItemDataRole.UserRole, int(song.get("size", 0)))
            mtime = song.get("mtime") or 0
            item_time = _SortItem(datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "—")
            item_time.setData(Qt.ItemDataRole.UserRole, mtime)
            self.table.setItem(row, 0, item_id)
            self.table.setItem(row, 1, item_name)
            self.table.setItem(row, 2, item_size)
            self.table.setItem(row, 3, item_time)
        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self._apply_filter()

    def _apply_filter(self):
        needle = self.search.text().strip().lower()
        for row in range(self.table.rowCount()):
            if not needle:
                self.table.setRowHidden(row, False)
                continue
            hay = (self.table.item(row, 0).text() + " " + self.table.item(row, 1).text()).lower()
            self.table.setRowHidden(row, needle not in hay)

    def _show_missing(self, result):
        source = result.get("source")
        missing = result.get("missing") or []
        expected = result.get("expected", 0)
        if not source:
            if self._kind == "wd":
                text = "缺少清单文件 wannadance_catalog.json（可由批量下载器生成），无法对比缺失。"
            else:
                text = "官方歌单直取表尚未加载，无法对比缺失；启动一次缓存服务后重试。"
        elif not missing:
            text = f"对照{source}（{expected} 首）：没有缺失，曲库完整。"
        else:
            shown = "、".join(str(i) for i in missing[:20])
            suffix = f" 等共 {len(missing)} 首" if len(missing) > 20 else ""
            text = f"对照{source}（{expected} 首）：缺失 {len(missing)} 首 — {shown}{suffix}"
            if self._kind == "pypy":
                text += "（仅覆盖官方歌单直取表范围）"
        self.missing_label.setText(text)
        self.missing_label.show()

    def _show_deleted(self, result):
        deleted = result.get("deleted", 0)
        freed = result.get("freed", 0)
        failed = result.get("failed") or []
        if result.get("demo"):
            self._window.notify("演示模式：未实际删除文件。")
        else:
            text = f"已永久删除 {deleted} 个文件，释放 {bytes_text(freed)}。"
            if failed:
                text += f" {len(failed)} 个删除失败（详见日志）。"
            self._window.notify(text, error=bool(failed))
        if deleted or failed:
            self.refresh()

    # ---------------- 动作 ----------------

    def _source_changed(self):
        self._kind = self.source.currentData()
        self.missing_label.hide()
        self.refresh()

    def _selected_ids(self):
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        ids = []
        for row in sorted(rows):
            item = self.table.item(row, 0)
            if item is not None:
                ids.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return ids

    def _row_path(self, row):
        item = self.table.item(row, 0)
        if item is None or not self._dir:
            return None
        sid = int(item.data(Qt.ItemDataRole.UserRole))
        name = f"wd_{sid}.mp4" if self._kind == "wd" else f"{sid}.mp4"
        return os.path.join(self._dir, name)

    def _play_current(self):
        row = self.table.currentRow()
        if row < 0:
            return
        path = self._row_path(row)
        if path and os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_directory(self):
        if self._dir:
            self.controller.open_directory(self._dir)

    def delete_selected(self):
        ids = self._selected_ids()
        if not ids:
            self._window.notify("请先在表格中选中要删除的歌曲。")
            return
        if self._window._runtime.get("running"):
            self._window.notify("服务运行中，请先停止服务再删除曲库文件。")
            return
        if self.controller.busy("rebuild") or self.controller.busy("verify"):
            self._window.notify("曲库任务进行中，请等待完成或取消后再删除。")
            return
        total = 0
        sizes = {int(s["id"]): int(s.get("size", 0)) for s in self._songs}
        for sid in ids:
            total += sizes.get(sid, 0)
        answer = QMessageBox.warning(
            self, "删除曲库文件",
            f"将永久删除选中的 {len(ids)} 首歌曲（共 {bytes_text(total)}），"
            "文件直接删除、不进入回收站，无法恢复。\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.library_delete(self._kind, ids)

    def closeEvent(self, event):
        for signal, slot in ((self.controller.result, self._on_result),
                             (self.controller.error, self._on_error),
                             (self.controller.busy_changed, self._on_busy)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._window._library_browser = None
        super().closeEvent(event)
