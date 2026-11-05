"""曲库浏览窗口：独立非模态对话框，列出本地曲库文件，支持搜索/查缺失/删除。"""
from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QProgressBar, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from .pages import button
from .widgets import bytes_text, label

_KINDS = (("pypy", "PyPyDance 曲库"), ("wd", "WannaDance 曲库"))
_OPS = ("library_scan", "library_missing", "library_delete")
_FLAG_ROLE = Qt.ItemDataRole.UserRole + 1  # 行类型标记：None=本地文件，"missing"/"dead"=缺失行


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
        self._parts = 0
        self._missing = {}  # id -> {"name": str|None, "dead": bool}
        self._pending = set()

        self.setWindowTitle("曲库浏览")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(820, 560)

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
        self.only_missing = QCheckBox("只看缺失")
        self.only_missing.toggled.connect(self._apply_filter)
        top.addWidget(self.only_missing)
        self.refresh_button = button("刷新", self.refresh)
        top.addWidget(self.refresh_button)
        box.addLayout(top)

        self.summary = label("正在扫描…", muted=True)
        box.addWidget(self.summary)
        self.loading = QProgressBar()
        self.loading.setRange(0, 0)
        self.loading.setTextVisible(False)
        self.loading.hide()
        box.addWidget(self.loading)
        self.missing_label = label("", muted=True, wrap=True)
        self.missing_label.hide()
        box.addWidget(self.missing_label)
        box.addWidget(label("双击行可用默认播放器打开；灰色行为缺失歌曲（缺失 / 已下架）。", muted=True))

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
        self.copy_button = button("复制选中编号", self.copy_selected)
        bottom.addWidget(self.copy_button)
        self.export_button = button("导出缺失清单…", self.export_missing)
        bottom.addWidget(self.export_button)
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
        self._missing = {}
        self.missing_label.hide()
        self.summary.setText("正在扫描…")
        self.table.setRowCount(0)
        self.controller.library_scan(self._kind)

    def find_missing(self):
        self.missing_label.setText("正在对照清单检查缺失…")
        self.missing_label.show()
        self.controller.library_missing(self._kind)

    def _on_result(self, operation, result):
        if operation == "library_scan":
            if result is None:
                self._show_no_dir()
                return
            if isinstance(result, dict) and result.get("kind", self._kind) == self._kind:
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
        self.loading.setVisible(blocked)
        for widget in (self.refresh_button, self.missing_button,
                       self.delete_button, self.source):
            widget.setEnabled(not blocked)

    # ---------------- 展示 ----------------

    def _show_no_dir(self):
        self._dir = ""
        self._songs = []
        self.summary.setText("未设置曲库目录或目录不存在，请先在「存储」页配置并应用更改。")

    def _row_items(self, sid, name, size_text, size_key, time_text, time_key, flag=None):
        item_id = _SortItem(str(sid))
        item_id.setData(Qt.ItemDataRole.UserRole, sid)
        item_id.setData(_FLAG_ROLE, flag)
        item_name = _SortItem(name or "—")
        item_size = _SortItem(size_text)
        item_size.setData(Qt.ItemDataRole.UserRole, size_key)
        item_time = _SortItem(time_text)
        item_time.setData(Qt.ItemDataRole.UserRole, time_key)
        items = [item_id, item_name, item_size, item_time]
        if flag:
            for item in items:
                item.setForeground(QColor(self._window.store.active["text_secondary"]))
                item.setToolTip("已确认下架（服务器无此文件）" if flag == "dead"
                                else "缺失：本地没有该文件")
        return items

    def _populate(self, result):
        self._dir = result.get("dir") or ""
        self._songs = result.get("songs") or []
        parts = self._parts = result.get("parts", self._parts)
        total = result.get("total_bytes", 0)
        text = f"共 {len(self._songs)} 首 · {bytes_text(total)}"
        if parts:
            text += f" · {parts} 个未完成下载（.part）"
        if self._dir:
            text += f" — {self._dir}"
        self.summary.setText(text)

        rows = []
        for song in self._songs:
            mtime = song.get("mtime") or 0
            rows.append(self._row_items(
                int(song.get("id", 0)), song.get("name"),
                bytes_text(song.get("size", 0)), int(song.get("size", 0)),
                datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "—",
                mtime))
        for sid, info in self._missing.items():
            rows.append(self._row_items(
                sid, info.get("name"),
                "已下架" if info.get("dead") else "缺失", -2 if info.get("dead") else -1,
                "—", 0, "dead" if info.get("dead") else "missing"))
        rows.sort(key=lambda items: items[0].data(Qt.ItemDataRole.UserRole))

        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for row_index, items in enumerate(rows):
            for column, item in enumerate(items):
                self.table.setItem(row_index, column, item)
        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self._apply_filter()

    def _apply_filter(self):
        needle = self.search.text().strip().lower()
        only_missing = self.only_missing.isChecked()
        for row in range(self.table.rowCount()):
            flag = self.table.item(row, 0).data(_FLAG_ROLE)
            hidden = False
            if only_missing and not flag:
                hidden = True
            if not hidden and needle:
                hay = (self.table.item(row, 0).text() + " " + self.table.item(row, 1).text()).lower()
                hidden = needle not in hay
            self.table.setRowHidden(row, hidden)

    def _show_missing(self, result):
        source = result.get("source")
        missing = result.get("missing") or []
        dead = set(result.get("dead") or [])
        names = result.get("names") or {}
        expected = result.get("expected", 0)
        if not source:
            if self._kind == "wd":
                text = "缺少清单文件 wannadance_catalog.json（可由批量下载器生成），无法对比缺失。"
            else:
                text = "官方歌单直取表尚未加载，无法对比缺失；启动一次缓存服务后重试。"
            self.missing_label.setText(text)
            self.missing_label.show()
            return
        self._missing = {int(sid): {"name": names.get(str(sid)) or names.get(sid),
                                    "dead": sid in dead}
                         for sid in missing}
        fillable = len(missing) - len(dead)
        if not missing:
            text = f"对照{source}（{expected} 首）：没有缺失，曲库完整。"
        else:
            text = f"对照{source}（{expected} 首）：缺失 {len(missing)} 首（可补 {fillable}"
            if dead:
                text += f" · 已确认下架 {len(dead)}"
            text += "），已在列表中灰色标出。"
            if self._kind == "pypy":
                text += "仅覆盖官方歌单直取表范围。"
        self.missing_label.setText(text)
        self.missing_label.show()
        self._merge_missing()

    def _merge_missing(self):
        """把缺失行增量合入表格：只删旧的缺失行、只建新的缺失行，
        不重建成千上万的本地文件行（否则大曲库点查缺失会长时间卡死界面）。"""
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        for row in range(self.table.rowCount() - 1, -1, -1):
            item = self.table.item(row, 0)
            if item is not None and item.data(_FLAG_ROLE):
                self.table.removeRow(row)
        start = self.table.rowCount()
        self.table.setRowCount(start + len(self._missing))
        for offset, (sid, info) in enumerate(sorted(self._missing.items())):
            items = self._row_items(
                sid, info.get("name"),
                "已下架" if info.get("dead") else "缺失",
                -2 if info.get("dead") else -1, "—", 0,
                "dead" if info.get("dead") else "missing")
            for column, item in enumerate(items):
                self.table.setItem(start + offset, column, item)
        self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self._apply_filter()

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
        self.refresh()

    def _selected_rows(self):
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _row_info(self, row):
        item = self.table.item(row, 0)
        if item is None:
            return None, None
        return int(item.data(Qt.ItemDataRole.UserRole)), item.data(_FLAG_ROLE)

    def copy_selected(self):
        ids = []
        for row in self._selected_rows():
            sid, _ = self._row_info(row)
            if sid is not None:
                ids.append(str(sid))
        if not ids:
            self._window.notify("请先在表格中选中歌曲。")
            return
        QApplication.clipboard().setText("\n".join(ids))
        self._window.notify(f"已复制 {len(ids)} 个编号到剪贴板。")

    def export_missing(self):
        ids = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            sid, flag = self._row_info(row)
            if flag:
                ids.append(sid)
        if not ids:
            self._window.notify("当前没有可导出的缺失歌曲，请先「查缺失」。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出缺失清单", f"缺失歌曲-{self._kind}.txt", "文本文件 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(str(sid) for sid in ids) + "\n")
        except OSError as e:
            self._window.notify(f"导出失败：{e}", error=True)
            return
        self._window.notify(f"已导出 {len(ids)} 个缺失编号到 {path}")

    def _row_path(self, row):
        sid, flag = self._row_info(row)
        if sid is None or flag or not self._dir:
            return None
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
        rows = self._selected_rows()
        ids = []
        skipped_missing = 0
        for row in rows:
            sid, flag = self._row_info(row)
            if sid is None:
                continue
            if flag:
                skipped_missing += 1
            else:
                ids.append(sid)
        if not ids:
            self._window.notify("选中的是缺失歌曲，没有本地文件可删。" if skipped_missing
                                else "请先在表格中选中要删除的歌曲。")
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
