"""曲库浏览窗口：独立非模态对话框，列出本地曲库文件，支持搜索/查缺失/删除。"""
from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QUrl, QItemSelectionModel
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QLineEdit, QProgressBar, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from . import i18n
from .i18n import MessageBox as QMessageBox
from .pages import button
from .widgets import bytes_text, label

_KINDS = (("pypy", "PyPyDance 曲库"), ("wd", "WannaDance 曲库"), ("dudu", "DuDu FitDance 曲库"))
_OPS = ("library_scan", "library_missing", "library_delete")
_FLAG_ROLE = Qt.ItemDataRole.UserRole + 1  # 行类型标记：None=本地文件，"missing"/"dead"=缺失行
_FAVORITE_ROLE = Qt.ItemDataRole.UserRole + 2
_CATEGORY_ROLE = Qt.ItemDataRole.UserRole + 3
_SECONDARY_ROLE = Qt.ItemDataRole.UserRole + 4
_PATH_ROLE = Qt.ItemDataRole.UserRole + 5
_COMPRESSED_ROLE = Qt.ItemDataRole.UserRole + 6
_KIND_ROLE = Qt.ItemDataRole.UserRole + 7


class _SortItem(QTableWidgetItem):
    """UserRole 里放数值排序键（编号/大小/时间），文本列走默认字符串比较。"""

    def __lt__(self, other):
        mine = self.data(Qt.ItemDataRole.UserRole)
        theirs = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            return mine < theirs
        return super().__lt__(other)


class LibraryBrowser(QDialog):
    def __init__(self, window, kind: str, compression_mode=False, cooldown_mode=False):
        super().__init__(window)
        self._window = window
        self.controller = window.controller
        self._compression_mode = bool(compression_mode)
        self._cooldown_mode = bool(cooldown_mode)
        self._kind = kind if kind in dict(_KINDS) or kind == "all" else "pypy"
        self._dir = ""
        self._songs = []
        self._parts = 0
        self._summary_base = "正在扫描…"
        self._missing = {}  # id -> {"name": str|None, "dead": bool}
        self._pending = set()
        self._closed = False
        self._needs_scan = False
        self._needs_missing = False

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.finished.connect(self._detach)
        self.setWindowTitle("选择 CoolDown 放松视频" if cooldown_mode else
                            "选择要压缩的歌曲" if compression_mode else "曲库浏览")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(1100 if compression_mode or cooldown_mode else 900,
                    650 if compression_mode or cooldown_mode else 560)

        box = QVBoxLayout(self)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        top = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItem("全部曲库", "all")
        for value, text in _KINDS:
            self.source.addItem(text, value)
        self.source.setCurrentIndex(self.source.findData(self._kind))
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
        self.only_uncompressed = QCheckBox("只看未压缩")
        self.only_uncompressed.toggled.connect(self._apply_filter)
        top.addWidget(self.only_uncompressed)
        self.only_favorites = QCheckBox("只看收藏")
        self.only_favorites.toggled.connect(self._apply_filter)
        self.only_favorites.setVisible(self._kind in ("wd", "all"))
        top.addWidget(self.only_favorites)
        self.refresh_button = button("刷新", self.refresh)
        top.addWidget(self.refresh_button)
        box.addLayout(top)

        filters = QHBoxLayout()
        self.category_label = label("分类")
        filters.addWidget(self.category_label)
        self.category_filter = QComboBox()
        self.category_filter.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.category_filter, 1)
        self.secondary_label = label("流派")
        filters.addWidget(self.secondary_label)
        self.secondary_filter = QComboBox()
        self.secondary_filter.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.secondary_filter, 1)
        box.addLayout(filters)

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
        box.addWidget(label("缺失歌曲也会显示；请选择本地已有的视频作为 CoolDown。" if cooldown_mode else
                            "仅显示本地已有视频；支持分类筛选，按住 Ctrl／Shift 多选，使用 Ctrl+A 全选。" if compression_mode else
                            "双击行可用默认播放器打开；灰色行为缺失歌曲（缺失 / 已下架）。", muted=True))

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["编号", "歌名", "分类", "大小", "修改时间", "压缩状态", "曲库"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection if cooldown_mode else
                                    QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self._configure_kind()
        self.table.doubleClicked.connect(self._play_current)
        box.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.missing_button = button("查缺失", self.find_missing)
        bottom.addWidget(self.missing_button)
        self.cloud_copy_button = button("将选中编号复制到剪贴板", self.copy_cloud_selected)
        self.cloud_copy_button.setToolTip("将选中歌曲编号复制为 4861、4860、4589 格式。")
        bottom.addWidget(self.cloud_copy_button)
        self.export_button = button("将选中编号导出为txt文件", self.export_selected_ids)
        bottom.addWidget(self.export_button)
        self.select_filtered_button = button("选中当前筛选结果", self.select_filtered)
        self.select_filtered_button.setVisible(compression_mode)
        bottom.addWidget(self.select_filtered_button)
        self.compress_selected_button = button("压缩选中歌曲…", self.compress_selected, primary=True)
        self.compress_selected_button.setVisible(compression_mode)
        bottom.addWidget(self.compress_selected_button)
        self.cooldown_select_button = button("选择这首歌曲", self.select_cooldown_song, primary=True)
        self.cooldown_select_button.setVisible(cooldown_mode)
        bottom.addWidget(self.cooldown_select_button)
        self.open_dir_button = button("打开曲库目录", self.open_directory)
        bottom.addWidget(self.open_dir_button)
        bottom.addStretch(1)
        self.delete_button = button("删除选中…", self.delete_selected)
        self.delete_button.setProperty("danger", True)
        bottom.addWidget(self.delete_button)
        for widget in (self.missing_button, self.cloud_copy_button, self.export_button,
                       self.open_dir_button, self.delete_button):
            widget.setVisible(not compression_mode and not cooldown_mode)
        self.only_uncompressed.setVisible(not cooldown_mode)
        self.only_missing.setChecked(compression_mode)
        self.only_missing.setVisible(not compression_mode)
        box.addLayout(bottom)
        if cooldown_mode:
            self.search.setText("cool down")

        self.controller.result.connect(self._on_result)
        self.controller.error.connect(self._on_error)
        self.controller.busy_changed.connect(self._on_busy)
        self.controller.event.connect(self._on_favorites_changed)
        self.refresh()

    def _configure_kind(self):
        category = "歌曲分组" if self._kind == "dudu" else "官方分组" if self._kind == "wd" else "分类"
        self.category_label.setText(category)
        self.table.horizontalHeaderItem(2).setText(category)
        self.secondary_label.setText("标签" if self._kind == "wd" else "流派")
        visible = self._kind != "dudu"
        self.secondary_label.setVisible(visible)
        self.secondary_filter.setVisible(visible)
        self.table.setColumnHidden(6, self._kind != "all")

    def _update_filter_choices(self, facets):
        facets = facets or {}
        for combo, all_text, key in ((self.category_filter, "全部分类", "categories"),
                                     (self.secondary_filter, "全部", "secondary")):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(all_text, None)
            for value in facets.get(key) or []:
                combo.addItem(str(value), str(value))
            index = combo.findData(current)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)
        self._apply_filter()

    # ---------------- 数据流 ----------------

    def _on_favorites_changed(self, kind, data):
        if kind == "wd_favorites" and self._kind in ("wd", "all"):
            self.refresh()
        elif kind == "compression" and data.get("phase") == "idle":
            self.refresh()

    def refresh(self):
        if self._closed:
            return
        # A previous browser may still have an in-flight scan. The controller
        # serializes scans, so request ours after it finishes instead of losing it.
        if self.controller.busy("library_scan"):
            self._needs_scan = True
            self._on_busy("library_scan", True)
            return
        self._missing = {}
        self.missing_label.hide()
        self._summary_base = "正在扫描…"
        self.summary.setText(self._summary_base)
        self.table.setRowCount(0)
        self.controller.library_scan(self._kind)

    def find_missing(self):
        if self._closed:
            return
        self.missing_label.setText("正在对照清单检查缺失…")
        self.missing_label.show()
        if self.controller.busy("library_missing"):
            self._needs_missing = True
            self._on_busy("library_missing", True)
            return
        self.controller.library_missing(self._kind)

    def _on_result(self, operation, result):
        if self._closed or (operation == "library_scan" and self._needs_scan) or (
                operation == "library_missing" and self._needs_missing):
            return
        if isinstance(result, dict) and result.get("kind", self._kind) != self._kind:
            return
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

    @i18n.translate_after
    def _on_error(self, operation, message):
        if self._closed or operation not in _OPS:
            return
        self.summary.setText("操作失败：" + message)
        if operation in ("library_missing", "library_delete"):
            self.missing_label.setText(message)
            self.missing_label.show()

    def _on_busy(self, operation, busy):
        if self._closed or operation not in _OPS:
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
        if operation == "library_scan" and not busy and self._needs_scan:
            self._needs_scan = False
            QTimer.singleShot(0, self.refresh)
        if operation == "library_missing" and not busy and self._needs_missing:
            self._needs_missing = False
            QTimer.singleShot(0, self.find_missing)

    # ---------------- 展示 ----------------

    def _show_no_dir(self):
        self._dir = ""
        self._songs = []
        self._summary_base = "未设置曲库目录或目录不存在，请先在「存储」页配置并应用更改。"
        self.summary.setText(self._summary_base)

    def _row_items(self, sid, name, size_text, size_key, time_text, time_key, flag=None, favorite=False,
                   missing_color=None, category="", secondary=None, path="", compression=None, song_kind=""):
        compression = compression or {}
        row_kind = song_kind or self._kind
        marked = bool(path) and os.path.splitext(os.path.basename(path))[0].endswith("_compressed")
        compressed = bool(not flag and (marked or compression.get("status") == "done"))
        item_id = _SortItem(str(sid))
        item_id.setData(Qt.ItemDataRole.UserRole, sid)
        item_id.setData(_FLAG_ROLE, flag)
        item_id.setData(_FAVORITE_ROLE, bool(favorite))
        item_id.setData(_CATEGORY_ROLE, category or "")
        item_id.setData(_SECONDARY_ROLE, list(secondary or []))
        item_id.setData(_PATH_ROLE, path or "")
        item_id.setData(_COMPRESSED_ROLE, compressed)
        item_id.setData(_KIND_ROLE, row_kind)
        item_name = _SortItem(("💗 " if favorite else "") + (name or "—"))
        item_category = _SortItem(category or "—")
        item_size = _SortItem(size_text)
        item_size.setData(Qt.ItemDataRole.UserRole, size_key)
        item_time = _SortItem(time_text)
        item_time.setData(Qt.ItemDataRole.UserRole, time_key)
        item_compression = _SortItem("—" if flag else "已压缩" if compressed else "未压缩")
        if compressed:
            encoder = compression.get("encoder")
            mode = compression.get("quality_mode")
            value = compression.get("crf")
            encoder_names = {"cpu": "CPU x264", "nvenc": "NVIDIA NVENC", "qsv": "Intel QSV", "amf": "AMD AMF"}
            parameters = {"cpu": "CRF", "nvenc": "CQ", "qsv": "ICQ", "amf": "QP"}
            if encoder and mode in ("quality", "volume"):
                text = f"{encoder_names.get(encoder, encoder)} · {'质量档' if mode == 'quality' else '体积档'}"
                if value is not None:
                    text += f"（{parameters.get(encoder, '质量')} {value}）"
            elif encoder and value is not None:
                text = f"{encoder_names.get(encoder, encoder)} · {parameters.get(encoder, '质量')} {value}"
            else:
                text = "已压缩，压缩参数未知"
            item_compression.setToolTip(text)
        source_names = {"pypy": "PyPyDance", "wd": "WannaDance", "dudu": "DuDu FitDance"}
        item_source = _SortItem(source_names.get(row_kind, row_kind))
        items = [item_id, item_name, item_category, item_size, item_time, item_compression, item_source]
        if flag:
            color = missing_color if missing_color is not None else QColor(self._window.store.active["text_secondary"])
            for item in items:
                item.setForeground(color)
                item.setToolTip("已确认下架（服务器无此文件）" if flag == "dead"
                                else "缺失：本地没有该文件")
        return items

    @i18n.translate_after
    def _populate(self, result):
        self._dir = result.get("dir") or ""
        self._songs = result.get("songs") or []
        compression_records = self.controller.compression_records()
        compression_by_path = {os.path.normcase(os.path.normpath(item["path"])): item
                               for item in compression_records if item.get("path")}
        self._update_filter_choices(result.get("facets"))
        parts = self._parts = result.get("parts", self._parts)
        total = result.get("total_bytes", 0)
        text = f"共 {len(self._songs)} 首 · {bytes_text(total)}"
        if self._kind == "wd":
            favorites = sum(bool(song.get("favorite")) for song in self._songs)
            text += f" · {favorites} 首收藏"
        if parts:
            text += f" · {parts} 个未完成下载（.part）"
        if self._dir:
            text += f" — {self._dir}"
        self._summary_base = text
        self.summary.setText(text)

        missing_color = QColor(self._window.store.active["text_secondary"])
        rows = []
        for song in self._songs:
            mtime = song.get("mtime") or 0
            song_path = song.get("path") or ""
            compression = compression_by_path.get(os.path.normcase(os.path.normpath(song_path)))
            rows.append(self._row_items(
                int(song.get("id", 0)), song.get("name"),
                bytes_text(song.get("size", 0)), int(song.get("size", 0)),
                datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "—",
                mtime, favorite=song.get("favorite", False),
                category=song.get("category", ""), secondary=song.get("secondary"),
                path=song_path, compression=compression, song_kind=song.get("kind", "")))
        for sid, info in self._missing.items():
            rows.append(self._row_items(
                sid, info.get("name"),
                "已下架" if info.get("dead") else "缺失", -2 if info.get("dead") else -1,
                "—", 0, "dead" if info.get("dead") else "missing",
                favorite=info.get("favorite", False), missing_color=missing_color,
                category=info.get("category", ""), secondary=info.get("secondary")))
        def row_id(items):
            value = items[0].data(Qt.ItemDataRole.UserRole)
            return value[1] if isinstance(value, tuple) else value
        rows.sort(key=row_id)

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
        if not self._compression_mode:
            self.missing_label.setText("正在自动对照完整清单，缺失歌曲会以灰色显示…")
            self.missing_label.show()
            self.find_missing()

    def _apply_filter(self):
        needle = "".join(self.search.text().lower().split())
        only_missing = self.only_missing.isChecked()
        only_uncompressed = self.only_uncompressed.isChecked()
        only_favorites = self.only_favorites.isChecked() and self._kind in ("wd", "all")
        category = self.category_filter.currentData()
        secondary = self.secondary_filter.currentData() if self._kind != "dudu" else None
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            flag = item.data(_FLAG_ROLE)
            favorite = item.data(_FAVORITE_ROLE)
            hidden = False
            if (self._compression_mode and flag) or (not self._compression_mode and only_missing and not flag):
                hidden = True
            if only_uncompressed and (flag or item.data(_COMPRESSED_ROLE)):
                hidden = True
            if only_favorites and not favorite:
                hidden = True
            if category and item.data(_CATEGORY_ROLE) != category:
                hidden = True
            if secondary and secondary not in (item.data(_SECONDARY_ROLE) or []):
                hidden = True
            if not hidden and needle:
                hay = "".join((item.text() + " " + self.table.item(row, 1).text() + " "
                               + self.table.item(row, 2).text()).lower().split())
                hidden = needle not in hay
            self.table.setRowHidden(row, hidden)
        visible = sum(not self.table.isRowHidden(row) for row in range(self.table.rowCount()))
        self.summary.setText(f"当前筛选 {visible} 首 · {self._summary_base}")

    @i18n.translate_after
    def _show_missing(self, result):
        source = result.get("source")
        missing = result.get("missing") or []
        dead = set(result.get("dead") or [])
        favorites = set(result.get("favorites") or []) if self._kind == "wd" else set()
        names = result.get("names") or {}
        metadata = result.get("metadata") or {}
        expected = result.get("expected", 0)
        if not source:
            if self._kind == "dudu":
                text = "暂未获取到 DuDu 官方清单，请检查网络后点击“刷新”重试。"
            elif self._kind == "wd":
                text = "暂未获取到 WannaDance 官方清单，请检查网络后点击“刷新”重试。"
            else:
                text = "官方歌单直取表尚未加载，无法对比缺失；启动一次缓存服务后重试。"
            self.missing_label.setText(text)
            self.missing_label.show()
            return
        if self._kind == "all":
            entries = result.get("missing_entries") or []
            self._missing = {}
            for entry in entries:
                sid = int(entry["id"])
                self._missing[(entry.get("kind"), sid)] = dict(entry)
            dead_count = sum(bool(entry.get("dead")) for entry in entries)
            text = (f"对照全部曲库（{expected} 首）：没有缺失，曲库完整。" if not entries else
                    f"对照全部曲库（{expected} 首）：缺失 {len(entries)} 首"
                    + (f" · 已确认下架 {dead_count}" if dead_count else "") + "，已在列表中灰色标出。")
            self.missing_label.setText(text)
            self.missing_label.show()
            self._merge_missing()
            return
        self._missing = {}
        for sid in missing:
            info = dict(metadata.get(str(sid)) or metadata.get(sid) or {})
            info.update(name=names.get(str(sid)) or names.get(sid),
                        dead=sid in dead, favorite=sid in favorites)
            self._missing[int(sid)] = info
        fillable = len(missing) - len(dead)
        if not missing:
            text = f"对照{source}（{expected} 首）：没有缺失，曲库完整。"
        else:
            text = f"对照{source}（{expected} 首）：缺失 {len(missing)} 首（可补 {fillable}"
            if dead:
                text += f" · 已确认下架 {len(dead)}"
            text += "），已在列表中灰色标出。"
            if self._kind == "pypy":
                text += "对照完整官方歌单，包括没有 YouTube 直取 ID 的歌曲。"
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
        missing_color = QColor(self._window.store.active["text_secondary"])
        for offset, (key, info) in enumerate(sorted(self._missing.items())):
            sid = key[1] if isinstance(key, tuple) else key
            items = self._row_items(
                sid, info.get("name"),
                "已下架" if info.get("dead") else "缺失",
                -2 if info.get("dead") else -1, "—", 0,
                "dead" if info.get("dead") else "missing",
                favorite=info.get("favorite", False), missing_color=missing_color,
                category=info.get("category", ""), secondary=info.get("secondary"),
                song_kind=info.get("kind", ""))
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
        self._configure_kind()
        self.only_favorites.setVisible(self._kind in ("wd", "all"))
        self.only_favorites.setChecked(False)
        self.category_filter.setCurrentIndex(0)
        self.secondary_filter.setCurrentIndex(0)
        self.refresh()

    def _selected_rows(self):
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _row_info(self, row):
        item = self.table.item(row, 0)
        if item is None:
            return None, None
        return int(item.data(Qt.ItemDataRole.UserRole)), item.data(_FLAG_ROLE)

    def _selected_ids(self):
        ids = []
        for row in self._selected_rows():
            sid, _ = self._row_info(row)
            if sid is not None:
                ids.append(str(sid))
        return ids

    def copy_cloud_selected(self):
        ids = self._selected_ids()
        if not ids:
            self._window.notify("请先在表格中选中歌曲。")
            return
        QApplication.clipboard().setText("、".join(ids))
        self._window.notify(f"已将 {len(ids)} 个选中编号复制到剪贴板。")

    def export_selected_ids(self):
        ids = self._selected_ids()
        if not ids:
            self._window.notify("请先在表格中选中歌曲。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "将选中编号导出为txt文件", f"选中编号-{self._kind}.txt", "文本文件 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(ids) + "\n")
        except OSError as e:
            self._window.notify(f"导出失败：{e}", error=True)
            return
        self._window.notify(f"已将 {len(ids)} 个选中编号导出到 {path}")

    def _row_path(self, row):
        sid, flag = self._row_info(row)
        if sid is None or flag or not self._dir:
            return None
        path = self.table.item(row, 0).data(_PATH_ROLE)
        if path:
            return path
        if self._kind == "all":
            return None
        row_kind = self.table.item(row, 0).data(_KIND_ROLE) or self._kind
        name = f"wd_{sid}.mp4" if row_kind == "wd" else f"dudu_{sid}.mp4" if row_kind == "dudu" else f"{sid}.mp4"
        return os.path.join(self._dir, name)

    def select_filtered(self):
        selection = self.table.selectionModel()
        selection.clearSelection()
        for row in range(self.table.rowCount()):
            _, flag = self._row_info(row)
            if not self.table.isRowHidden(row) and not flag:
                selection.select(self.table.model().index(row, 0),
                                 QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)

    def compress_selected(self):
        paths = []
        grouped_paths = {"pypy": [], "wd": [], "dudu": []}
        total = 0
        for row in self._selected_rows():
            path = self._row_path(row)
            if path and os.path.isfile(path):
                paths.append(path)
                grouped_paths[self.table.item(row, 0).data(_KIND_ROLE)].append(path)
                total += os.path.getsize(path)
        if not paths:
            self._window.notify("请先选择本地已有且需要压缩的歌曲。")
            return
        answer = QMessageBox.question(
            self, "压缩选中歌曲",
            f"将压缩选中的 {len(paths)} 首歌曲，共 {bytes_text(total)}。\n"
            "使用当前已保存的压缩档位和硬件设置；已压缩文件会自动跳过。\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            accepted = self._window.start_compression_paths(self._kind, grouped_paths if self._kind == "all" else paths)
            if accepted:
                self.accept()

    def select_cooldown_song(self):
        rows = self._selected_rows()
        row = rows[0] if rows else self.table.currentRow()
        path = self._row_path(row) if row >= 0 else None
        if not path or not os.path.isfile(path):
            self._window.notify("这首歌曲不在本地，请选择已下载的视频。")
            return
        self._window.select_cooldown_from_library(path)
        self.close()

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
        grouped = {"pypy": [], "wd": [], "dudu": []}
        total = 0
        skipped_missing = 0
        for row in rows:
            sid, flag = self._row_info(row)
            if sid is None:
                continue
            if flag:
                skipped_missing += 1
            else:
                ids.append(sid)
                grouped[self.table.item(row, 0).data(_KIND_ROLE)].append(sid)
                total += int(self.table.item(row, 3).data(Qt.ItemDataRole.UserRole) or 0)
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
        answer = QMessageBox.warning(
            self, "删除曲库文件",
            f"将永久删除选中的 {len(ids)} 首歌曲（共 {bytes_text(total)}），"
            "文件直接删除、不进入回收站，无法恢复。\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.library_delete(self._kind, grouped if self._kind == "all" else ids)

    def _detach(self):
        if self._closed:
            return
        self._closed = True
        for signal, slot in ((self.controller.result, self._on_result),
                             (self.controller.error, self._on_error),
                             (self.controller.busy_changed, self._on_busy),
                             (self.controller.event, self._on_favorites_changed)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        if self._window._library_browser is self:
            self._window._library_browser = None

    def closeEvent(self, event):
        self._detach()
        super().closeEvent(event)
