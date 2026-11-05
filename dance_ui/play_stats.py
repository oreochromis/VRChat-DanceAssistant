"""点歌统计窗口：按日期范围统计点歌排行（可做年度报告），支持导出 CSV。"""
from __future__ import annotations

import csv
from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QDialog, QFileDialog, QHBoxLayout, QMessageBox,
    QSizePolicy, QTableWidgetItem, QVBoxLayout,
)

from .pages import button
from .widgets import label, table

_KINDS = (("", "全部来源"), ("pypy", "PyPyDance"), ("wd", "WannaDance"))
_KIND_LABELS = {"pypy": "PyPyDance", "wd": "WannaDance"}


def _day_start(date: QDate) -> float:
    return datetime(date.year(), date.month(), date.day()).timestamp()


def _fmt_time(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


class PlayStatsDialog(QDialog):
    """独立非模态窗口：日期范围 + 来源筛选的点歌排行。"""

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self.controller = window.controller
        self._songs = []

        self.setWindowTitle("点歌统计")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(880, 600)

        box = QVBoxLayout(self)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.source = QComboBox()
        for value, text in _KINDS:
            self.source.addItem(text, value)
        self.source.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.source)
        today = QDate.currentDate()
        self.start = QDateEdit(QDate(today.year(), 1, 1))
        self.end = QDateEdit(today)
        for edit in (self.start, self.end):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.dateChanged.connect(self.refresh)
        top.addWidget(label("从"))
        top.addWidget(self.start)
        top.addWidget(label("到"))
        top.addWidget(self.end)
        top.addWidget(button("今年", self._preset_year))
        top.addWidget(button("近 30 天", self._preset_month))
        top.addWidget(button("全部", self._preset_all))
        top.addStretch()
        box.addLayout(top)

        self.summary = label("正在读取点歌历史…", muted=True, wrap=True)
        box.addWidget(self.summary)

        self.table = table(["排名", "次数", "歌名", "编号", "来源", "最常点歌人", "最近点歌"], 2)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.setSortingEnabled(True)
        box.addWidget(self.table, 1)

        actions = QHBoxLayout()
        actions.addWidget(label(
            "仅统计本软件运行期间检测到的点歌，从首次使用本功能起累积；"
            "同一首歌离开队列后再次入队会记为新的一次。", muted=True, wrap=True), 1)
        self.export_button = button("导出 CSV…", self._export_csv)
        actions.addWidget(self.export_button)
        box.addLayout(actions)

        self.controller.result.connect(self._on_result)
        self.controller.error.connect(self._on_error)
        self.refresh()

    # ---------------- 日期预设 ----------------

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

    # ---------------- 数据 ----------------

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
            self.summary.setText("该时间范围内没有点歌记录。点歌历史从本功能上线后起记录，"
                                 "需在服务运行时进入跳舞房才会累积。")
        else:
            text = f"期间共 {total} 次点歌 · {len(self._songs)} 首不同歌曲"
            if players:
                top = players[0]
                text += f" · 点歌最多的人：{top['player']}（{top['count']} 次）"
            earliest = result.get("earliest")
            if earliest:
                text += f"。记录最早始于 {_fmt_time(earliest)}"
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

    def closeEvent(self, event):
        for signal, slot in ((self.controller.result, self._on_result),
                             (self.controller.error, self._on_error)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._window._play_stats = None
        super().closeEvent(event)
