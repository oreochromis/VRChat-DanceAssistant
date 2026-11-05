"""点歌统计页面：按日期范围统计点歌排行（可做年度报告），支持导出 CSV。"""
from __future__ import annotations

import csv
from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox, QComboBox, QDateEdit, QFileDialog, QHBoxLayout, QMessageBox,
    QSizePolicy, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .pages import button, field
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
        self._songs = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, _, box = page()
        outer.addWidget(area)
        box.addWidget(section_title("点歌统计", "回看点歌记录，导出你的年度总结"))

        filters = Card()
        filter_row = QHBoxLayout()
        filter_row.setSpacing(14)
        self.source = QComboBox()
        self.source.setMinimumWidth(140)
        for value, text in _KINDS:
            self.source.addItem(text, value)
        self.source.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(field("来源", self.source))

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
        filter_row.addWidget(field("开始日期", self.start))
        filter_row.addWidget(field("结束日期", self.end))

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
        filter_row.addWidget(preset_group)
        filter_row.addStretch()
        filters.box.addLayout(filter_row)
        box.addWidget(filters)

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
        box.addStretch()

        self.controller.result.connect(self._on_result)
        self.controller.error.connect(self._on_error)

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

    def refresh(self):
        start_date, end_date = self.start.date(), self.end.date()
        if start_date > end_date:
            self.summary.setText("开始日期晚于结束日期，请调整日期范围。")
            return
        self.summary.setText("正在读取点歌历史…")
        self.controller.play_history_stats(
            _day_start(start_date), _day_start(end_date) + 86400,
            self.source.currentData())

    def _on_result(self, operation, result):
        if operation != "play_history_stats" or not isinstance(result, dict):
            return
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
        if has_songs:
            row_height = max(36, self.table.verticalHeader().defaultSectionSize())
            header_height = self.table.horizontalHeader().sizeHint().height()
            self.table.setFixedHeight(header_height + min(len(self._songs), 8) * row_height + 4)

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
        if operation == "play_history_stats":
            self.summary.setText(f"读取点歌历史失败: {message}")
