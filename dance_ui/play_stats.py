"""点歌统计页面：按日期范围统计点歌排行（可做年度报告），支持导出 CSV。"""
from __future__ import annotations

import csv
from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox, QComboBox, QDateEdit, QFileDialog, QGridLayout, QHBoxLayout, QMessageBox,
    QProgressBar, QSizePolicy, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .pages import button, field
from .vrcx_stats import format_duration, resolve_vrcx_path
from .widgets import Card, label, page, section_title, table

_KINDS = (("", "全部来源"), ("pypy", "PyPyDance"), ("wd", "WannaDance"))
_KIND_LABELS = {"pypy": "PyPyDance", "wd": "WannaDance"}


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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, _, box = page()
        outer.addWidget(area)
        box.addWidget(section_title("点歌统计", "回看点歌记录，导出你的年度总结"))

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
        self.filter_widgets = [source_field, field("开始日期", self.start), field("结束日期", self.end), preset_group]
        self.filters_panel.box.addLayout(self.filters_grid)
        self._layout_filters()
        box.addWidget(self.filters_panel)

        results = Card()
        heading = QHBoxLayout()
        heading.addWidget(label("点歌排行", subheading=True))
        heading.addStretch()
        self.export_button = button("导出 CSV…", self._export_csv)
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

        self.table = table(["排名", "次数", "歌名", "编号", "来源", "最常点歌人", "最近点歌"], 2)
        self.table.setAccessibleName("点歌排行")
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        self.table.hide()
        results.box.addWidget(self.table)
        box.addWidget(results)

        box.addWidget(label(
            "统计本软件运行期间检测到的点歌，从本功能上线起累积；同一首歌离开队列后再次入队会记为新的一次。",
            muted=True, wrap=True))
        report_row = QHBoxLayout()
        report_row.addWidget(label("把当前日期和来源的统计结果变成一份可分享的年度风格报告。", muted=True, wrap=True), 1)
        self.report_button = button("查看报告", self.open_report, primary=True)
        self.report_button.setEnabled(False)
        report_row.addWidget(self.report_button)
        box.addLayout(report_row)

        vrcx_card = Card()
        heading = QHBoxLayout()
        heading.addWidget(label("好友同房时长", subheading=True))
        heading.addStretch()
        self.vrcx_choose_button = button("选择数据库", self.choose_vrcx_database, link=True)
        self.vrcx_read_button = button("读取 VRCX 统计", self.read_vrcx_stats, primary=True)
        heading.addWidget(self.vrcx_choose_button)
        heading.addWidget(self.vrcx_read_button)
        vrcx_card.box.addLayout(heading)
        vrcx_card.box.addWidget(label(
            "按当前日期范围统计你与本机 VRCX 好友在同一房间的时间。只读本地数据，不读取账号凭据。",
            muted=True, wrap=True))
        self.vrcx_path = label("数据库：" + str(resolve_vrcx_path()), muted=True, wrap=True)
        self.vrcx_path.setToolTip(self.vrcx_path.text())
        vrcx_card.box.addWidget(self.vrcx_path)
        self.vrcx_loading = QProgressBar()
        self.vrcx_loading.setRange(0, 0)
        self.vrcx_loading.setTextVisible(False)
        self.vrcx_loading.hide()
        vrcx_card.box.addWidget(self.vrcx_loading)
        self.vrcx_summary = label("尚未读取。VRCX 未运行或没有记录的时段无法统计。", muted=True, wrap=True)
        vrcx_card.box.addWidget(self.vrcx_summary)
        self.vrcx_table = table(["排名", "好友", "同房时长", "同房次数", "最近同房"], 1)
        self.vrcx_table.setAccessibleName("好友同房时长排行")
        self.vrcx_table.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.vrcx_table.hide()
        vrcx_card.box.addWidget(self.vrcx_table)
        box.addWidget(vrcx_card)
        box.addStretch()

        self.controller.result.connect(self._on_result)
        self.controller.error.connect(self._on_error)

    def resizeEvent(self, event):
        self._layout_filters()
        super().resizeEvent(event)

    def _layout_filters(self):
        while self.filters_grid.count():
            self.filters_grid.takeAt(0)
        columns = 4 if self.width() >= 1040 else 2
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

    def read_vrcx_stats(self, path=None):
        start_date, end_date = self.start.date(), self.end.date()
        if start_date > end_date:
            self.vrcx_summary.setText("开始日期晚于结束日期，请调整后再读取。")
            return
        self.vrcx_loading.show()
        self.vrcx_read_button.setEnabled(False)
        self.vrcx_choose_button.setEnabled(False)
        self.vrcx_summary.setText("正在只读分析 VRCX 房间记录…")
        self.controller.vrcx_stats(_day_start(start_date), _day_start(end_date) + 86400, path)

    def choose_vrcx_database(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 VRCX 数据库", str(resolve_vrcx_path().parent),
            "VRCX 数据库 (*.sqlite3 *.db);;SQLite 数据库 (*.sqlite *.sqlite3 *.db)")
        if path:
            self.read_vrcx_stats(path)

    def _show_vrcx_stats(self, result):
        self.vrcx_loading.hide()
        self.vrcx_read_button.setEnabled(True)
        self.vrcx_choose_button.setEnabled(True)
        self._vrcx_data = result
        self.vrcx_path.setText("数据库：" + str(result.get("path") or "未知"))
        self.vrcx_path.setToolTip(str(result.get("path") or ""))
        friends = list(result.get("friends") or [])
        if friends:
            top = friends[0]
            self.vrcx_summary.setText(
                f"最常与你同房的好友：{top.get('name') or '未知'} · {format_duration(top.get('seconds', 0))}"
                f"；共统计到 {len(friends)} 位好友。")
        else:
            self.vrcx_summary.setText("该日期范围没有可统计的好友同房记录；VRCX 未运行的时段无法补齐。")
        self.vrcx_table.setSortingEnabled(False)
        self.vrcx_table.setRowCount(0)
        for friend in friends[:10]:
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
                self.vrcx_table.setItem(row_index, column, item)
        self.vrcx_table.setVisible(bool(friends))
        if friends:
            row_height = max(36, self.vrcx_table.verticalHeader().defaultSectionSize())
            height = self.vrcx_table.horizontalHeader().sizeHint().height() + min(len(friends), 10) * row_height + 4
            self.vrcx_table.setFixedHeight(height)
        self.vrcx_table.setSortingEnabled(True)

    def refresh(self):
        start_date, end_date = self.start.date(), self.end.date()
        if start_date > end_date:
            self.loading.hide()
            self.summary.setText("开始日期晚于结束日期，请调整日期范围。")
            return
        start_ts, end_ts = _day_start(start_date), _day_start(end_date) + 86400
        if self._vrcx_data and (self._vrcx_data.get("start_ts") != start_ts
                                or self._vrcx_data.get("end_ts") != end_ts):
            self._vrcx_data = None
            self.vrcx_table.hide()
            self.vrcx_summary.setText("日期范围已变化，请重新读取 VRCX 统计。")
        self.summary.setText("正在读取点歌历史…")
        self.loading.show()
        self.export_button.setEnabled(False)
        self.report_button.setEnabled(False)
        self.controller.play_history_stats(
            _day_start(start_date), _day_start(end_date) + 86400,
            self.source.currentData())

    def _on_result(self, operation, result):
        if operation == "vrcx_stats" and isinstance(result, dict):
            self._show_vrcx_stats(result)
            return
        if operation != "play_history_stats" or not isinstance(result, dict):
            return
        self.loading.hide()
        self._report_data = result
        self._songs = list(result.get("songs") or [])
        total = result.get("total", 0)
        players = result.get("players") or []
        if not total:
            self.summary.setText("该时间范围内没有点歌记录。")
        else:
            text = f"共 {total} 次点歌 · {len(self._songs)} 首不同歌曲"
            if players:
                top = players[0]
                text += f" · 点歌最多：{top['player']}（{top['count']} 次）"
            earliest = result.get("earliest")
            if earliest:
                text += f" · 最早记录 {_fmt_time(earliest)}"
            self.summary.setText(text)
        self._render()

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
                song.get("top_player") or "—",
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
            row_height = max(36, self.table.verticalHeader().defaultSectionSize())
            header_height = self.table.horizontalHeader().sizeHint().height()
            self.table.setFixedHeight(header_height + min(len(self._songs), 8) * row_height + 4)

    def open_report(self):
        if not self._report_data or not self._songs:
            return
        from .play_report import PlayReportDialog
        if self._report is not None:
            self._report.close()
        report_data = dict(self._report_data)
        if self._vrcx_data:
            report_data["vrcx"] = self._vrcx_data
        self._report = PlayReportDialog(
            self, report_data, self.start.date(), self.end.date(),
            self.source.currentText(), self._main.store.active)
        self._report.show()
        self._report.raise_()
        self._report.activateWindow()

    def _export_csv(self):
        if not self._songs:
            QMessageBox.information(self, "导出 CSV", "当前范围内没有可导出的数据。")
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
                writer.writerow(["排名", "次数", "歌名", "编号", "来源", "最常点歌人", "首次点歌", "最近点歌"])
                for song in self._songs:
                    writer.writerow([
                        song.get("rank"), song.get("count"), song.get("name") or "",
                        song.get("id"), _KIND_LABELS.get(song.get("kind"), ""),
                        song.get("top_player") or "",
                        _fmt_time(song.get("first_ts")), _fmt_time(song.get("last_ts")),
                    ])
        except OSError as exc:
            QMessageBox.warning(self, "导出 CSV", f"写入失败: {exc}")
            return
        QMessageBox.information(self, "导出 CSV", f"已导出 {len(self._songs)} 行到:\n{path}")

    def _on_error(self, operation, message):
        if operation == "vrcx_stats":
            self.vrcx_loading.hide()
            self.vrcx_read_button.setEnabled(True)
            self.vrcx_choose_button.setEnabled(True)
            self.vrcx_summary.setText(f"读取 VRCX 统计失败：{message}")
        elif operation == "play_history_stats":
            self.loading.hide()
            self.summary.setText(f"读取点歌历史失败: {message}")
