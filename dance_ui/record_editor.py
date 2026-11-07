"""Advanced editor for software-owned VRCX records and local song history."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLineEdit,
    QMessageBox, QSpinBox, QTableWidgetItem, QVBoxLayout, QDateTimeEdit, QHeaderView,
)

from . import stats_records, vrcx_stats
from .pages import button
from .widgets import label, table

SOURCES = {
    "location": ("VRCX 位置记录", (("id", "ID"), ("created_at", "时间"), ("world_id", "地图 ID"), ("location", "位置 / 实例 ID"))),
    "event": ("VRCX 房间事件", (("id", "ID"), ("created_at", "时间"), ("type", "类型"), ("display_name", "显示名称"), ("user_id", "用户 ID"), ("location", "位置 / 实例 ID"))),
    "queue": ("点歌记录", (("ts", "时间"), ("id", "歌曲编号"), ("name", "歌名"), ("kind", "来源"), ("player", "点歌人"))),
    "played": ("实际播放记录", (("ts", "时间"), ("id", "歌曲编号"), ("name", "歌名"), ("kind", "来源"), ("player", "点歌人"))),
}


def _time_text(value, vrcx=False):
    try:
        timestamp = vrcx_stats._timestamp(value) if vrcx else float(value)
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError, OSError):
        return str(value or "")


class RecordFieldsDialog(QDialog):
    def __init__(self, kind, record=None, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.record = record or {}
        self.setWindowTitle("编辑记录" if record else "新增记录")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.time = QDateTimeEdit()
        self.time.setCalendarPopup(True)
        self.time.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        timestamp = self.record.get("created_at") if kind in stats_records.VRCX_TABLES else self.record.get("ts")
        try:
            seconds = vrcx_stats._timestamp(timestamp) if kind in stats_records.VRCX_TABLES else float(timestamp)
        except (ValueError, TypeError, OverflowError, OSError):
            seconds = datetime.now().timestamp()
        self.time.setDateTime(QDateTime.fromSecsSinceEpoch(round(seconds)))
        form.addRow("时间", self.time)
        self.fields = {}
        if kind == "location":
            self._text_field(form, "world_id", "地图 ID")
            self._text_field(form, "location", "位置 / 实例 ID")
        elif kind == "event":
            self._choice_field(form, "type", "事件类型", ("OnPlayerJoined", "OnPlayerLeft"))
            self._text_field(form, "user_id", "用户 ID")
            self._text_field(form, "display_name", "显示名称")
            self._text_field(form, "location", "位置 / 实例 ID")
            self._text_field(form, "time", "原始 time 值")
        else:
            self._choice_field(form, "kind", "来源", ("pypy", "wd"))
            song_id = QSpinBox()
            song_id.setRange(1, 1000000000)
            song_id.setValue(max(1, int(self.record.get("id") or 1)))
            self.fields["id"] = song_id
            form.addRow("歌曲编号", song_id)
            self._text_field(form, "name", "歌名")
            self._text_field(form, "player", "点歌人")
        layout.addLayout(form)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = button("取消", self.reject)
        save = button("保存", self.accept, primary=True)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def _text_field(self, form, key, title):
        field = QLineEdit(str(self.record.get(key) if self.record.get(key) is not None else ""))
        self.fields[key] = field
        form.addRow(title, field)

    def _choice_field(self, form, key, title, choices):
        field = QComboBox()
        field.addItems(choices)
        value = str(self.record.get(key) or choices[0])
        field.setCurrentIndex(max(0, field.findText(value)))
        self.fields[key] = field
        form.addRow(title, field)

    def values(self):
        result = {}
        for key, field in self.fields.items():
            if isinstance(field, QLineEdit):
                result[key] = field.text()
            elif isinstance(field, QComboBox):
                result[key] = field.currentText()
            else:
                result[key] = field.value()
        if self.kind in stats_records.VRCX_TABLES:
            result["created_at"] = self.time.dateTime().toPython().isoformat()
        else:
            result["ts"] = self.time.dateTime().toSecsSinceEpoch()
        return result


class RecordEditorDialog(QDialog):
    def __init__(self, parent=None, on_changed=None):
        super().__init__(parent)
        self.on_changed = on_changed
        self.setWindowTitle("原始记录 · 软件副本")
        self.resize(1200, 720)
        self.setMinimumSize(850, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(label(
            "这里只修改本软件的数据副本，不修改 VRCX 正在使用的数据库。VRCX 后续同步会补入新记录，保留你的修改和删除。",
            muted=True, wrap=True))
        controls = QHBoxLayout()
        self.source = QComboBox()
        for key, (title, _) in SOURCES.items():
            self.source.addItem(title, key)
        self.source.currentIndexChanged.connect(self._source_changed)
        controls.addWidget(self.source)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索当前类型的原始记录")
        self.search.setClearButtonEnabled(True)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        controls.addWidget(self.search, 1)
        controls.addWidget(button("刷新", self.refresh))
        layout.addLayout(controls)
        self.table = table([], 0)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.doubleClicked.connect(lambda: self.edit_selected())
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.add_button = button("新增", self.add_record, primary=True)
        self.edit_button = button("编辑", self.edit_selected)
        self.delete_button = button("删除", self.delete_selected)
        self.delete_button.setProperty("danger", True)
        actions.addWidget(self.add_button)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        actions.addWidget(button("关闭", self.close))
        layout.addLayout(actions)
        self.status = label("", muted=True, wrap=True)
        layout.addWidget(self.status)
        self._source_changed()

    @property
    def kind(self):
        return self.source.currentData()

    def _source_changed(self):
        columns = SOURCES[self.kind][1]
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels([title for _, title in columns])
        header = self.table.horizontalHeader()
        for index in range(len(columns)):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(columns) - 1, QHeaderView.ResizeMode.Stretch)
        self.refresh()

    def refresh(self):
        try:
            if self.kind in stats_records.VRCX_TABLES:
                records = stats_records.list_vrcx_records(self.kind, self.search.text())
            else:
                records = stats_records.list_history_records(self.kind, self.search.text())
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.setText("读取失败：" + str(error))
            return
        columns = SOURCES[self.kind][1]
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            for column, (key, _) in enumerate(columns):
                value = record.get(key)
                if key == "created_at":
                    value = _time_text(value, vrcx=True)
                elif key == "ts":
                    value = _time_text(value)
                item = QTableWidgetItem(str(value if value is not None else ""))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record)
                self.table.setItem(row, column, item)
        self.status.setText(f"显示 {len(records)} 条最近记录；记录较多时可先用搜索缩小范围。")

    def selected_record(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def add_record(self):
        dialog = RecordFieldsDialog(self.kind, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save(dialog.values())

    def edit_selected(self):
        record = self.selected_record()
        if record is None:
            self.status.setText("请先选择一条记录。")
            return
        dialog = RecordFieldsDialog(self.kind, record, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save(dialog.values(), record)

    def _save(self, values, record=None):
        try:
            if self.kind in stats_records.VRCX_TABLES:
                stats_records.save_vrcx_record(self.kind, values, None if record is None else record["id"])
            else:
                stats_records.save_history_record(self.kind, values, None if record is None else record["_line"])
        except (OSError, ValueError, sqlite3.Error) as error:
            QMessageBox.warning(self, "保存记录", str(error))
            return
        self.refresh()
        if self.on_changed:
            self.on_changed()

    def delete_selected(self):
        record = self.selected_record()
        if record is None:
            self.status.setText("请先选择一条记录。")
            return
        answer = QMessageBox.question(self, "删除记录", "确定从本软件的数据副本中删除这条记录吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if self.kind in stats_records.VRCX_TABLES:
                stats_records.delete_vrcx_record(self.kind, record["id"])
            else:
                stats_records.delete_history_record(self.kind, record["_line"])
        except (OSError, ValueError, sqlite3.Error) as error:
            QMessageBox.warning(self, "删除记录", str(error))
            return
        self.refresh()
        if self.on_changed:
            self.on_changed()
