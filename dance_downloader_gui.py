"""Standalone three-in-one VRChat dance room video downloader."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

import dance_cache as core
from dance_ui.downloader import ROOM_NAMES, UnifiedDownloader, existing_song_ids, parse_song_ids

VERSION = "1.3"


def resource_path(relative):
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / relative


class DownloadEvents(QObject):
    received = Signal(object)


class DownloaderWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Sharkspeare", "DanceVideoDownloader")
        self.main_paths = {"pypy": "", "wd": "", "dudu": ""}
        try:
            config = json.loads((Path(core.data_dir()) / "config.json").read_text(encoding="utf-8-sig"))
            for kind, name in (("pypy", "library_dir"), ("wd", "wd_library_dir"),
                               ("dudu", "dudu_library_dir")):
                self.main_paths[kind] = str(config.get(name) or "")
        except (OSError, ValueError, TypeError):
            pass
        self._path_generation = 0
        self.path_timer = QTimer(self)
        self.path_timer.setSingleShot(True)
        self.path_timer.setInterval(250)
        self.path_timer.timeout.connect(self.scan_current_path)
        self.engine = None
        self.worker = None
        self.running = False
        self.close_after = False
        self.events = DownloadEvents()
        self.events.received.connect(self.on_download_event)
        self.setWindowTitle(f"VRChat Dance Video Downloader {VERSION}")
        icon = resource_path("assets/app-icon.png")
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(820, 680)
        self.setMinimumSize(680, 560)
        body = QWidget()
        self.setCentralWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)
        title = QLabel("跳舞房视频下载器")
        title.setStyleSheet("font-size:24px;font-weight:700;")
        layout.addWidget(title)
        subtitle = QLabel("PyPyDance · WannaDance · DuDu FitDance｜无需管理员权限，不修改 hosts")
        subtitle.setStyleSheet("color:#667085;")
        layout.addWidget(subtitle)

        room_row = QHBoxLayout()
        room_row.addWidget(QLabel("房间"))
        self.room = QComboBox()
        for kind, name in ROOM_NAMES.items():
            self.room.addItem(name, kind)
        self.room.currentIndexChanged.connect(self.room_changed)
        room_row.addWidget(self.room, 1)
        room_row.addWidget(QLabel("并发下载"))
        self.threads = QSpinBox()
        self.threads.setRange(1, 8)
        self.threads.setValue(int(self.settings.value("threads", 2)))
        room_row.addWidget(self.threads)
        layout.addLayout(room_row)

        path_row = QHBoxLayout()
        self.path = QLineEdit()
        path_row.addWidget(self.path, 1)
        self.browse_button = QPushButton("选择目录")
        self.browse_button.clicked.connect(self.browse)
        path_row.addWidget(self.browse_button)
        self.open_button = QPushButton("打开目录")
        self.open_button.clicked.connect(self.open_directory)
        path_row.addWidget(self.open_button)
        self.main_path_button = QPushButton("使用主程序曲库目录")
        self.main_path_button.clicked.connect(self.use_main_library_path)
        path_row.addWidget(self.main_path_button)
        layout.addLayout(path_row)
        self.path_status = QLabel("等待检查目录…")
        self.path_status.setStyleSheet("color:#667085;")
        layout.addWidget(self.path_status)
        self.path.textChanged.connect(self.schedule_path_scan)

        self.ids = QLineEdit()
        self.ids.setPlaceholderText("留空下载全部；编号用空格或逗号分隔，也支持 100-110 范围")
        layout.addWidget(self.ids)
        self.classified = QCheckBox("按官方分类保存到子文件夹（关闭后在根目录平铺保存）")
        self.classified.setChecked(self.settings.value("classified", False, type=bool))
        layout.addWidget(self.classified)
        hint = QLabel("已存在的编号会自动跳过，包含分类目录及 _compressed.mp4；重复运行不会重复下载。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#667085;")
        layout.addWidget(hint)

        actions = QHBoxLayout()
        self.start_button = QPushButton("开始下载")
        self.start_button.setStyleSheet("padding:9px 20px;font-weight:600;")
        self.start_button.clicked.connect(self.start)
        actions.addWidget(self.start_button)
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status = QLabel("选择保存目录后开始下载")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(5000)
        layout.addWidget(self.logs, 1)
        footer = QLabel(f"VRChat Dance Video Downloader · {VERSION}    bilibili @鲨鲨比亚Sharkspeare")
        footer.setStyleSheet("color:#667085;")
        layout.addWidget(footer)
        self.room_changed()

    def current_kind(self):
        return self.room.currentData()

    def room_changed(self):
        kind = self.current_kind()
        default = str(Path.home() / "Downloads" / "DanceVideoCache" / ROOM_NAMES[kind])
        saved = str(self.settings.value(f"path_{kind}", "") or "")
        main_path = self.main_paths.get(kind) or ""
        if (not saved or saved == default) and main_path and Path(main_path).is_dir():
            saved = main_path
        self.path.setText(saved or default)
        self.schedule_path_scan()

    def use_main_library_path(self):
        kind = self.current_kind()
        path = self.main_paths.get(kind) or ""
        if not path or not Path(path).is_dir():
            QMessageBox.information(self, "主程序目录不可用", "主程序尚未设置有效的该房间曲库目录。")
            return
        self.path.setText(path)
        self.schedule_path_scan()

    def schedule_path_scan(self, *args):
        self._path_generation += 1
        self.path_timer.start()

    def scan_current_path(self):
        generation = self._path_generation
        kind, directory = self.current_kind(), self.path.text().strip()
        self.path_status.setText("正在检查目录…")

        def scan():
            try:
                count = len(existing_song_ids(kind, directory))
                error = ""
            except OSError as exc:
                count, error = 0, str(exc)
            self.events.received.emit({"type": "path_status", "generation": generation,
                                       "kind": kind, "path": directory, "count": count, "error": error})

        threading.Thread(target=scan, daemon=True, name="downloader-path-scan").start()

    def browse(self):
        selected = QFileDialog.getExistingDirectory(self, "选择视频保存目录", self.path.text())
        if selected:
            self.path.setText(selected)

    def open_directory(self):
        path = Path(self.path.text().strip())
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def append_log(self, message):
        if not message:
            return
        self.logs.appendPlainText(f"[{datetime.now():%H:%M:%S}] {message}")

    def start(self):
        if self.running:
            return
        directory = self.path.text().strip()
        if not directory:
            QMessageBox.warning(self, "缺少目录", "请先选择视频保存目录。")
            return
        try:
            selected = parse_song_ids(self.ids.text())
        except ValueError as error:
            QMessageBox.warning(self, "编号格式错误", str(error))
            return
        kind = self.current_kind()
        if not selected and not existing_song_ids(kind, directory):
            answer = QMessageBox.question(
                self, "当前目录没有已有视频",
                f"没有在该目录识别到任何已下载歌曲：\n{directory}\n\n"
                "继续将按全量新曲库下载。请确认目录没有选择错误。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        classified = self.classified.isChecked()
        threads = self.threads.value()
        self.settings.setValue(f"path_{kind}", directory)
        self.settings.setValue("classified", classified)
        self.settings.setValue("threads", threads)
        self.engine = UnifiedDownloader(self.events.received.emit)
        self.worker = threading.Thread(
            target=self.engine.run, args=(kind, directory, classified, threads, selected),
            daemon=True, name="unified-dance-downloader")
        self.running = True
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.room.setEnabled(False)
        self.progress.setRange(0, 0)
        self.status.setText("正在获取官方清单并准备下载…")
        self.append_log(f"开始 {ROOM_NAMES[kind]} 下载，保存到 {directory}")
        self.worker.start()

    def cancel(self):
        if self.engine is not None and self.running:
            self.status.setText("正在取消，等待当前连接关闭…")
            self.cancel_button.setEnabled(False)
            self.engine.cancel()

    def on_download_event(self, event):
        event_type = event.get("type")
        if event_type == "path_status":
            if event.get("generation") == self._path_generation:
                if event.get("error"):
                    self.path_status.setText("目录检查失败：" + event["error"])
                else:
                    self.path_status.setText(f"当前目录已识别 {event.get('count', 0)} 首已有视频")
        elif event_type == "log":
            self.append_log(event.get("message", ""))
        elif event_type == "started":
            total = event.get("total", 0)
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(0)
            self.status.setText(f"待下载 {total} 首，已存在 {event.get('existing', 0)} 首")
        elif event_type == "progress":
            self.progress.setValue(event.get("completed", 0))
            summary = event.get("summary") or {}
            self.status.setText(
                f"进度 {event.get('completed', 0)} / {event.get('total', 0)} · "
                f"完成 {summary.get('done', 0)} · 跳过 {summary.get('skipped', 0)} · 失败 {summary.get('failed', 0)}")
        elif event_type == "finished":
            summary = event.get("summary") or {}
            self.running = False
            self.start_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            self.room.setEnabled(True)
            self.progress.setRange(0, 1)
            self.progress.setValue(1)
            if summary.get("error"):
                self.status.setText("任务出错：" + summary["error"])
            elif summary.get("cancelled"):
                self.status.setText("任务已取消；已完成的文件保留")
            else:
                self.status.setText(
                    f"下载结束 · 新完成 {summary.get('done', 0)} · 已存在 {summary.get('existing', 0)} · "
                    f"跳过 {summary.get('skipped', 0)} · 失败 {summary.get('failed', 0)}")
            self.append_log(self.status.text())
            if self.close_after:
                QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if self.running and self.engine is not None:
            self.close_after = True
            self.cancel()
            event.ignore()
            return
        event.accept()


def show_window_centered(window):
    window.showNormal()
    screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        frame = window.frameGeometry()
        x = available.x() + (available.width() - frame.width()) // 2
        y = available.y() + (available.height() - frame.height()) // 2
        x = min(max(x, available.x()), max(available.x(), available.right() - frame.width() + 1))
        y = min(max(y, available.y()), max(available.y(), available.bottom() - frame.height() + 1))
        window.move(x, y)
    window.raise_()
    window.activateWindow()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--smoke-report")
    args, _ = parser.parse_known_args(argv)
    app = QApplication(sys.argv[:1])
    window = DownloaderWindow()
    show_window_centered(window)
    if args.smoke_test:
        def smoke():
            report = {"status": "passed", "rooms": [window.room.itemData(i) for i in range(window.room.count())],
                      "classified_option": window.classified.isChecked(), "version": VERSION,
                      "save_path": window.path.text(), "path_status": window.path_status.text(),
                      "main_library_path": window.main_paths.get(window.current_kind(), ""),
                      "window_title": window.windowTitle()}
            if args.smoke_report:
                Path(args.smoke_report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            window.close()
            app.quit()
        QTimer.singleShot(1200, smoke)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
