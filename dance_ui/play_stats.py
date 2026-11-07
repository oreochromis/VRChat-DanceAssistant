"""点歌统计页面：按日期范围统计点歌排行（可做年度报告），支持导出 CSV。"""
from __future__ import annotations

import csv
import json
import os
import time
import zipfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QSignalBlocker, QTimer, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QComboBox, QDateEdit, QFileDialog, QGridLayout, QHBoxLayout, QMessageBox,
    QProgressBar, QSizePolicy, QTableWidgetItem, QVBoxLayout, QWidget,
)

import dance_cache as core
from . import stats_backup
from .pages import button, field
from .vrcx_profile import friend_avatar_path
from .vrcx_stats import (
    format_duration, is_vrcx_archive, load_vrcx_stats, resolve_vrcx_path, save_self_name, saved_self_name,
)
from .widgets import Card, label, page, section_title, table

_KINDS = (("", "全部来源"), ("pypy", "PyPyDance"), ("wd", "WannaDance"))
_KIND_LABELS = {"pypy": "PyPyDance", "wd": "WannaDance"}
PLAY_STATS_CACHE_FILENAME = "play-stats-cache.json"


def _play_cache_path():
    return Path(core.data_dir()) / PLAY_STATS_CACHE_FILENAME


def _load_play_cache():
    try:
        value = json.loads(_play_cache_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and isinstance(value.get("key"), dict) and isinstance(value.get("result"), dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _save_play_cache(key, result):
    path = _play_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"key": key, "result": result}, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _day_start(date: QDate) -> float:
    return datetime(date.year(), date.month(), date.day()).timestamp()


def _fmt_time(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


class PlayStatsPage(QWidget):
    """日期范围 + 来源筛选的点歌排行，进入页面时加载数据。"""

    def __init__(self, window):
        super().__init__(window)
        self.controller = window.controller
        self._main = window
        self._songs = []
        self._report_data = None
        self._report = None
        self._vrcx_data = None
        self._result_key = None
        self._self_name_value = "小林" if self.controller.demo else saved_self_name()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, _, box = page()
        outer.addWidget(area)
        box.addWidget(section_title("点歌统计"))

        self.filters_panel = Card()
        self.filters_grid = QGridLayout()
        self.filters_grid.setHorizontalSpacing(14)
        self.filters_grid.setVerticalSpacing(12)
        self.source = QComboBox()
        self.source.setMinimumWidth(140)
        for value, text in _KINDS:
            self.source.addItem(text, value)
        self.source.currentIndexChanged.connect(self.refresh)
        source_field = field("来源", self.source)
        self.player_filter = QComboBox()
        self.player_filter.addItem("我的点歌", "__self__")
        self.player_filter.addItem("全部点歌人", "")
        self.player_filter.setMinimumWidth(140)
        self.player_filter.setAccessibleName("点歌人筛选")
        self.player_filter.currentIndexChanged.connect(self.refresh)
        player_field = field("点歌人", self.player_filter)

        today = QDate.currentDate()
        self.start = QDateEdit(QDate(today.year(), 1, 1))
        self.end = QDateEdit(today)
        for edit in (self.start, self.end):
            edit.setCalendarPopup(True)
            edit.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setMinimumWidth(142)
            edit.dateChanged.connect(self.refresh)
            calendar = edit.calendarWidget()
            calendar.setGridVisible(False)
            calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
            calendar.setMinimumSize(320, 240)

        preset_group = QWidget()
        preset_box = QVBoxLayout(preset_group)
        preset_box.setContentsMargins(0, 0, 0, 0)
        preset_box.setSpacing(7)
        preset_box.addWidget(label("快捷范围"))
        presets = QHBoxLayout()
        presets.setSpacing(8)
        presets.addWidget(button("今年", self._preset_year))
        presets.addWidget(button("近 30 天", self._preset_month))
        presets.addWidget(button("全部", self._preset_all))
        preset_box.addLayout(presets)
        report_group = QWidget()
        report_box = QVBoxLayout(report_group)
        report_box.setContentsMargins(0, 0, 0, 0)
        report_box.addStretch()
        self.report_button = button("查看报告", self.open_report, primary=True)
        self.report_button.setEnabled(False)
        report_box.addWidget(self.report_button, 0, Qt.AlignmentFlag.AlignRight)
        self.filter_widgets = [source_field, player_field, field("开始日期", self.start),
                               field("结束日期", self.end), preset_group, report_group]
        self.filters_panel.box.addLayout(self.filters_grid)
        self._layout_filters()
        box.addWidget(self.filters_panel)

        results = Card()
        heading = QHBoxLayout()
        heading.addWidget(label("点歌排行", subheading=True))
        heading.addStretch()
        self.export_button = button("导出表格", self._export_csv)
        self.export_button.setEnabled(False)
        heading.addWidget(self.export_button)
        results.box.addLayout(heading)

        self.summary = label("进入统计页面后自动读取点歌历史。", muted=True, wrap=True)
        results.box.addWidget(self.summary)
        self.loading = QProgressBar()
        self.loading.setRange(0, 0)
        self.loading.setTextVisible(False)
        self.loading.hide()
        results.box.addWidget(self.loading)

        self.empty_state = label("这个时间范围还没有点歌记录。\n试试其他日期，或进入跳舞房点几首歌。", muted=True, wrap=True)
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setMinimumHeight(110)
        results.box.addWidget(self.empty_state)

        self.table = table(["排名", "次数", "歌名", "编号", "来源", "最近播放"], 2)
        self.table.setAccessibleName("点歌排行")
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        self.table.hide()
        results.box.addWidget(self.table)
        box.addWidget(results)

        self.history_note = label(
            "仅统计本软件运行期间检测到并已实际开始播放的点歌，从本功能上线起累积；同一首歌再次播放会记为新的一次。",
            muted=True, wrap=True)
        box.addWidget(self.history_note)
        self.backup_row = QWidget()
        backup_row = QHBoxLayout(self.backup_row)
        backup_row.setContentsMargins(0, 0, 0, 0)
        self.backup_note = label("备份点歌历史与 VRCX 原始数据；增量导入不会覆盖现有记录。", muted=True, wrap=True)
        backup_row.addWidget(self.backup_note, 1)
        backup_row.addStretch()
        self.backup_export_button = button("导出备份", self._export_backup)
        self.backup_import_button = button("增量导入", self._import_backup)
        self.backup_export_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.backup_import_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        backup_row.addWidget(self.backup_export_button)
        backup_row.addWidget(self.backup_import_button)

        vrcx_card = Card()
        heading = QHBoxLayout()
        heading.addWidget(label("好友共同游玩时长", subheading=True))
        heading.addStretch()
        self.vrcx_export_button = button("导出表格", self._export_vrcx_csv)
        self.vrcx_export_button.setEnabled(False)
        heading.addWidget(self.vrcx_export_button)
        self.vrcx_choose_button = button("选择数据库", self.choose_vrcx_database, link=True)
        self.vrcx_read_button = button("从VRCX同步最新数据", self.read_vrcx_stats, primary=True)
        heading.addWidget(self.vrcx_choose_button)
        heading.addWidget(self.vrcx_read_button)
        vrcx_card.box.addLayout(heading)
        self.vrcx_notes = [label(
            "按当前日期范围统计 PyPyDance / WannaDance 共同游玩时间。位置与本人离开记录确定正常区间；异常长记录中，连续 9 分钟没有点歌、播放或房间事件会剔除离线间隙，不额外补时。",
            muted=True, wrap=True), label("“我的点歌”自动使用 VRCX 当前账号昵称，昵称和头像会在同步时更新。", muted=True, wrap=True)]
        for note in self.vrcx_notes:
            vrcx_card.box.addWidget(note)
        self._update_vrcx_path_tooltip()
        self.vrcx_loading = QProgressBar()
        self.vrcx_loading.setRange(0, 0)
        self.vrcx_loading.setTextVisible(False)
        self.vrcx_loading.hide()
        vrcx_card.box.addWidget(self.vrcx_loading)
        self.vrcx_summary = label("尚未读取。VRCX 未运行或没有记录的时段无法统计。", muted=True, wrap=True)
        vrcx_card.box.addWidget(self.vrcx_summary)
        self.vrcx_table = table(["排名", "好友", "共同游玩时长", "共同游玩次数", "最近共同游玩"], 1)
        self.vrcx_table.setAccessibleName("好友共同游玩时长排行")
        self.vrcx_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.vrcx_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.vrcx_table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.vrcx_table.hide()
        vrcx_card.box.addWidget(self.vrcx_table)
        box.addWidget(vrcx_card)
        box.addWidget(self.backup_row)
        box.addStretch()

        self.controller.result.connect(self._on_result)
        self.controller.error.connect(self._on_error)
        if not self.controller.demo:
            cached = load_vrcx_stats()
            if cached:
                self._show_vrcx_stats(cached)
            self._restore_play_cache()
            QTimer.singleShot(0, self.refresh)
            self._vrcx_signature = None
            self._vrcx_timer = QTimer(self)
            self._vrcx_timer.setInterval(30000)
            self._vrcx_timer.timeout.connect(self._auto_sync_vrcx)
            self._vrcx_timer.start()
            QTimer.singleShot(2000, self._auto_sync_vrcx)

    def apply_ui_mode(self):
        advanced = self._main.store.preferences.get("ui_mode", "advanced") == "advanced"
        self.export_button.setVisible(advanced)
        self.vrcx_export_button.setVisible(advanced)
        self.backup_export_button.setVisible(True)
        self.backup_import_button.setVisible(True)
        self.vrcx_choose_button.setVisible(advanced)
        self.vrcx_read_button.setVisible(advanced)
        self.history_note.setVisible(advanced)
        self.backup_row.setVisible(True)
        self.backup_note.setVisible(advanced)
        for note in self.vrcx_notes:
            note.setVisible(advanced)

    def resizeEvent(self, event):
        self._layout_filters()
        super().resizeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._auto_sync_vrcx(force=True)

    def _layout_filters(self):
        while self.filters_grid.count():
            self.filters_grid.takeAt(0)
        columns = 5 if self.width() >= 1250 else 3 if self.width() >= 900 else 2
        self.filter_columns = columns
        for index, widget in enumerate(self.filter_widgets):
            self.filters_grid.addWidget(widget, index // columns, index % columns)
        for column in range(columns):
            self.filters_grid.setColumnStretch(column, 1)

    def _preset_year(self):
        today = QDate.currentDate()
        self.start.setDate(QDate(today.year(), 1, 1))
        self.end.setDate(today)

    def _preset_month(self):
        today = QDate.currentDate()
        self.start.setDate(today.addDays(-30))
        self.end.setDate(today)

    def _preset_all(self):
        self.start.setDate(QDate(2000, 1, 1))
        self.end.setDate(QDate.currentDate())

    def _vrcx_source_signature(self):
        source = resolve_vrcx_path()
        if is_vrcx_archive(source) or not source.is_file():
            return None
        values = []
        for path in (source, Path(str(source) + "-wal")):
            try:
                stat = path.stat()
                values.append((path.name, stat.st_mtime_ns, stat.st_size))
            except OSError:
                values.append(None)
        return tuple(values)

    def _auto_sync_vrcx(self, force=False):
        if self.controller.demo or self._main._closing:
            return
        if self.controller.busy("vrcx_stats") or self.controller.busy("vrcx_stats_auto"):
            return
        signature = self._vrcx_source_signature()
        if signature is None:
            source = resolve_vrcx_path()
            if not is_vrcx_archive(source) and not source.is_file():
                self._main.vrcx_auto_error(
                    "未找到 VRCX 数据库。请先启动并登录 VRCX，检查 %APPDATA%\\VRCX\\VRCX.sqlite3；"
                    "也可切换高级模式，在统计页手动选择数据库。")
            return
        if not force and signature == self._vrcx_signature:
            return
        self._vrcx_signature = signature
        self.read_vrcx_stats(automatic=True)

    def read_vrcx_stats(self, path=None, automatic=False):
        start_date, end_date = self.start.date(), self.end.date()
        if start_date > end_date:
            if not automatic:
                self.vrcx_summary.setText("开始日期晚于结束日期，请调整后再读取。")
            return
        if not automatic:
            self.vrcx_loading.show()
            self.vrcx_read_button.setEnabled(False)
            self.vrcx_choose_button.setEnabled(False)
            self.vrcx_summary.setText("正在只读分析 VRCX 房间记录…")
        self.controller.vrcx_stats(_day_start(start_date), _day_start(end_date) + 86400,
                                   path, automatic=automatic)

    def choose_vrcx_database(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 VRCX 数据库", str(resolve_vrcx_path().parent),
            "VRCX 数据库 (*.sqlite3 *.db);;SQLite 数据库 (*.sqlite *.sqlite3 *.db)")
        if path:
            self.read_vrcx_stats(path)

    def _update_vrcx_path_tooltip(self, current=None):
        source = resolve_vrcx_path()
        if source.is_file():
            lines = ["自动查找到的 VRCX 数据库：\n" + str(source)]
            if current:
                current = Path(str(current))
                if current != source and current.is_file():
                    lines.append("当前统计使用的软件编辑副本：\n" + str(current))
            self.vrcx_choose_button.setToolTip("\n\n".join(lines))
        else:
            self.vrcx_choose_button.setToolTip(
                "未自动找到 VRCX 数据库。请先启动并登录 VRCX，再检查 %APPDATA%\\VRCX\\VRCX.sqlite3；"
                "如果数据库位于其他位置，请点击此按钮手动选择。")

    def _show_vrcx_stats(self, result):
        self.vrcx_loading.hide()
        self.vrcx_read_button.setEnabled(True)
        self.vrcx_choose_button.setEnabled(True)
        self._vrcx_data = result
        self_names = [str(name).strip() for name in (result.get("self_names") or []) if str(name).strip()]
        if self_names:
            self._self_name_value = self_names[0]
            if not self.controller.demo:
                save_self_name(self._self_name_value)
        if result.get("profile"):
            self._main.update_profile(result["profile"])
        elif self_names:
            self._main.update_profile({"id": "", "name": self_names[0], "avatar": ""})
        self._update_vrcx_path_tooltip(result.get("path"))
        friends = list(result.get("friends") or [])
        if friends:
            top = friends[0]
            self.vrcx_summary.setText(
                f"最常与你共同游玩的好友：{top.get('name') or '未知'} · {format_duration(top.get('seconds', 0))}"
                f"；共统计到 {len(friends)} 位好友。")
        else:
            self.vrcx_summary.setText("该日期范围没有可统计的好友共同游玩记录；VRCX 未运行的时段无法补齐。")
        if result.get("profile_error"):
            self.vrcx_summary.setText(self.vrcx_summary.text() + "；账号头像未更新，请在 VRCX 重新登录后同步。")
            self.vrcx_summary.setToolTip(str(result["profile_error"]))
        self.vrcx_export_button.setEnabled(bool(friends))
        self.vrcx_table.setIconSize(QSize(24, 24))
        self.vrcx_table.verticalHeader().setDefaultSectionSize(36)
        self.vrcx_table.setSortingEnabled(False)
        self.vrcx_table.setRowCount(0)
        for friend in friends:
            row_index = self.vrcx_table.rowCount()
            self.vrcx_table.insertRow(row_index)
            values = (row_index + 1, friend.get("name") or "未知",
                      format_duration(friend.get("seconds", 0)), friend.get("sessions", 0),
                      _fmt_time(friend.get("last_ts")))
            for column, value in enumerate(values):
                item = QTableWidgetItem()
                if isinstance(value, int):
                    item.setData(Qt.ItemDataRole.DisplayRole, value)
                else:
                    item.setText(str(value))
                if column == 1:
                    avatar = friend_avatar_path(friend.get("user_id"))
                    if avatar.is_file():
                        item.setIcon(QIcon(str(avatar)))
                self.vrcx_table.setItem(row_index, column, item)
        self.vrcx_table.setVisible(bool(friends))
        if friends:
            row_height = self.vrcx_table.verticalHeader().defaultSectionSize()
            height = self.vrcx_table.horizontalHeader().sizeHint().height() + min(len(friends), 10) * row_height + 4
            self.vrcx_table.setFixedHeight(height)
        self.vrcx_table.setSortingEnabled(True)

    def _update_player_options(self, players):
        selected = self.player_filter.currentData()
        names = sorted({str(name).strip() for name in (players or []) if str(name).strip()},
                       key=str.casefold)
        if selected not in ("", "__self__", None) and selected not in names:
            names.append(selected)
        with QSignalBlocker(self.player_filter):
            self.player_filter.clear()
            self.player_filter.addItem("我的点歌", "__self__")
            self.player_filter.addItem("全部点歌人", "")
            for name in names:
                self.player_filter.addItem(name, name)
            index = self.player_filter.findData(selected)
            self.player_filter.setCurrentIndex(max(0, index))

    def _cache_key(self):
        player = self._self_name_value if self.player_filter.currentData() == "__self__" else self.player_filter.currentData()
        return {"start_ts": _day_start(self.start.date()), "end_ts": _day_start(self.end.date()) + 86400,
                "kind": self.source.currentData() or "", "player": player or ""}

    def _restore_play_cache(self):
        cached = _load_play_cache()
        if not cached or cached["key"] != self._cache_key():
            return
        result = cached["result"]
        if not result.get("updated_at"):
            try:
                result["updated_at"] = _play_cache_path().stat().st_mtime
            except OSError:
                pass
        self._apply_play_stats(result, persist=False)

    def refresh(self):
        start_date, end_date = self.start.date(), self.end.date()
        if start_date > end_date:
            self.loading.hide()
            self.summary.setText("开始日期晚于结束日期，请调整日期范围。")
            return
        player = self._self_name_value if self.player_filter.currentData() == "__self__" else self.player_filter.currentData()
        if self.player_filter.currentData() == "__self__" and not player:
            self.loading.hide()
            self._report_data = None
            self._songs = []
            self._render()
            self.summary.setText("请先从 VRCX 同步当前账号，再查看“我的点歌”。")
            return
        key = self._cache_key()
        if key != self._result_key:
            self.export_button.setEnabled(False)
            self.report_button.setEnabled(False)
        self.summary.setText("正在更新点歌历史；当前显示上次结果…" if self._songs else "正在读取点歌历史…")
        self.loading.show()
        self.controller.play_history_stats(key["start_ts"], key["end_ts"], key["kind"], player or None)

    def _apply_play_stats(self, result, persist=True):
        self.loading.hide()
        self._update_player_options(result.get("available_players"))
        self._result_key = self._cache_key()
        result["updated_at"] = float(result.get("updated_at") or time.time())
        self._report_data = result
        self._songs = list(result.get("songs") or [])
        total = result.get("total", 0)
        if not total:
            self.summary.setText("该时间范围内没有已播放的点歌记录。")
        else:
            scope = "我的点歌" if self.player_filter.currentData() == "__self__" else self.player_filter.currentText()
            text = f"{scope} · 共 {total} 次已播放点歌 · {len(self._songs)} 首不同歌曲"
            earliest = result.get("earliest")
            if earliest:
                text += f" · 最早记录 {_fmt_time(earliest)}"
            self.summary.setText(text)
        self._render()
        self._refresh_open_report()
        if persist:
            _save_play_cache(self._result_key, result)

    def _on_result(self, operation, result):
        if operation in ("vrcx_stats", "vrcx_stats_auto") and isinstance(result, dict):
            self._show_vrcx_stats(result)
            self._refresh_open_report()
            return
        if operation != "play_history_stats" or not isinstance(result, dict):
            return
        self._apply_play_stats(result)

    def _render(self):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for song in self._songs:
            row_index = self.table.rowCount()
            self.table.insertRow(row_index)
            values = (
                song.get("rank"), song.get("count"),
                song.get("name") or "—",
                song.get("id"),
                _KIND_LABELS.get(song.get("kind"), song.get("kind") or "?"),
                _fmt_time(song.get("last_ts")),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem()
                if isinstance(value, int):
                    item.setData(Qt.ItemDataRole.DisplayRole, value)
                else:
                    item.setText(str(value))
                self.table.setItem(row_index, column, item)
        self.table.setSortingEnabled(True)
        has_songs = bool(self._songs)
        self.table.setVisible(has_songs)
        self.empty_state.setVisible(not has_songs)
        self.export_button.setEnabled(has_songs)
        self.report_button.setEnabled(has_songs)
        if has_songs:
            row_height = self.table.verticalHeader().defaultSectionSize()
            header_height = self.table.horizontalHeader().sizeHint().height()
            self.table.setFixedHeight(header_height + min(len(self._songs), 10) * row_height + 4)

    def _report_payload(self):
        report_data = dict(self._report_data)
        timestamps = [float(self._report_data.get("updated_at") or 0)]
        start_ts, end_ts = _day_start(self.start.date()), _day_start(self.end.date()) + 86400
        if self._vrcx_data and self._vrcx_data.get("start_ts") == start_ts and self._vrcx_data.get("end_ts") == end_ts:
            report_data["vrcx"] = self._vrcx_data
            timestamps.append(float(self._vrcx_data.get("synced_at") or 0))
        report_data["updated_at"] = max(timestamps)
        report_data["self_played"] = int(self._report_data.get("total", 0))
        return report_data

    def _refresh_open_report(self):
        if self._report is not None and self._report_data and self._songs:
            self._report.update_data(self._report_payload())

    def open_report(self):
        if not self._report_data or not self._songs:
            return
        self._auto_sync_vrcx(force=True)
        from .play_report import PlayReportDialog
        if self._report is not None:
            self._report.close()
        self._report = PlayReportDialog(
            self, self._report_payload(), self.start.date(), self.end.date(),
            self.source.currentText(), self._main.store.active)
        self._report.show()
        self._report.raise_()
        self._report.activateWindow()

    def _export_csv(self):
        if not self._songs:
            QMessageBox.information(self, "导出表格", "当前范围内没有可导出的数据。")
            return
        start_text = self.start.date().toString("yyyyMMdd")
        end_text = self.end.date().toString("yyyyMMdd")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出点歌统计", f"点歌统计_{start_text}-{end_text}.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["排名", "次数", "歌名", "编号", "来源", "首次播放", "最近播放"])
                for song in self._songs:
                    writer.writerow([
                        song.get("rank"), song.get("count"), song.get("name") or "",
                        song.get("id"), _KIND_LABELS.get(song.get("kind"), ""),
                        _fmt_time(song.get("first_ts")), _fmt_time(song.get("last_ts")),
                    ])
        except OSError as exc:
            QMessageBox.warning(self, "导出表格", f"写入失败: {exc}")
            return
        QMessageBox.information(self, "导出表格", f"已导出 {len(self._songs)} 行到:\n{path}")

    def _export_vrcx_csv(self):
        friends = list((self._vrcx_data or {}).get("friends") or [])
        if not friends:
            QMessageBox.information(self, "导出表格", "当前范围内没有可导出的好友统计。")
            return
        start_text = self.start.date().toString("yyyyMMdd")
        end_text = self.end.date().toString("yyyyMMdd")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出好友共同游玩统计", f"好友共同游玩_{start_text}-{end_text}.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["排名", "好友", "共同游玩时长", "共同游玩次数", "最近共同游玩"])
                for rank, friend in enumerate(friends, 1):
                    writer.writerow([
                        rank, friend.get("name") or "未知", format_duration(friend.get("seconds", 0)),
                        friend.get("sessions", 0), _fmt_time(friend.get("last_ts")),
                    ])
        except OSError as exc:
            QMessageBox.warning(self, "导出表格", f"写入失败: {exc}")
            return
        QMessageBox.information(self, "导出表格", f"已导出 {len(friends)} 行到:\n{path}")

    def _export_backup(self):
        default_name = "舞蹈统计备份_" + QDate.currentDate().toString("yyyyMMdd") + ".zip"
        path, _ = QFileDialog.getSaveFileName(self, "导出统计备份", default_name, "ZIP 备份 (*.zip)")
        if not path:
            return
        try:
            result = stats_backup.export_stats_backup(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导出备份", f"备份失败：{exc}")
            return
        QMessageBox.information(self, "导出备份", f"已保存 {len(result['files'])} 个数据文件到：\n{path}")

    def _import_backup(self):
        path, _ = QFileDialog.getOpenFileName(self, "增量导入统计备份", "", "ZIP 备份 (*.zip)")
        if not path:
            return
        try:
            result = stats_backup.import_stats_backup(path)
        except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "增量导入", f"导入失败：{exc}")
            return
        self.refresh()
        cached = load_vrcx_stats()
        if cached:
            self._show_vrcx_stats(cached)
        QMessageBox.information(
            self, "增量导入",
            f"导入完成：新增入队记录 {result['play_history_added']} 条、播放记录 {result['played_history_added']} 条；"
            f"补充设置 {result['settings_added']} 项。现有记录和原始数据库不会被覆盖。")

    def _on_error(self, operation, message):
        if operation in ("vrcx_stats", "vrcx_stats_auto"):
            self.vrcx_loading.hide()
            self.vrcx_read_button.setEnabled(True)
            self.vrcx_choose_button.setEnabled(True)
            self.vrcx_summary.setText(f"读取 VRCX 统计失败：{message}" if operation == "vrcx_stats"
                                      else "VRCX 自动同步暂不可用，将在数据变化时重试。")
        elif operation == "play_history_stats":
            self.loading.hide()
            self.summary.setText(f"读取点歌历史失败: {message}")
