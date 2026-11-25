import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QTableWidget, QTableWidgetItem

from dance_ui import i18n


class I18nTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.addCleanup(i18n.set_language, "zh")

    def test_catalogs_cover_the_same_static_strings(self):
        self.assertGreater(len(i18n.EN_TRANSLATIONS), 300)
        self.assertEqual(set(i18n.EN_TRANSLATIONS), set(i18n.JA_TRANSLATIONS))
        self.assertEqual(set(i18n.EN_TRANSLATIONS), set(i18n.KO_TRANSLATIONS))
        for language in ("en", "ja", "ko"):
            for source, translated in i18n.TRANSLATIONS[language].items():
                self.assertIsInstance(source, str)
                self.assertIsInstance(translated, str)
                self.assertTrue(translated.strip())

    def test_direct_and_dynamic_translations(self):
        cases = {"en": "Run", "ja": "実行", "ko": "실행"}
        for language, expected in cases.items():
            self.assertEqual(i18n.tr("运行", language), expected)
            self.assertEqual(i18n.tr("12 首", language), i18n.tr("12 首", language))
            self.assertNotEqual(i18n.tr("12 首", language), "12 首")
            self.assertEqual(i18n.tr("选择颜色 · #ABCDEF", language), i18n.tr("选择颜色 · #ABCDEF", language))

    def test_system_language_mapping(self):
        if os.name != "nt":
            self.skipTest("Windows language IDs")
        for language_id, expected in ((0x0404, "zh_TW"), (0x0C04, "zh_TW"), (0x1404, "zh_TW"),
                                      (0x0804, "zh"), (0x1004, "zh"), (0x0409, "en"),
                                      (0x0411, "ja"), (0x0412, "ko"), (0x0401, "zh")):
            with mock.patch("ctypes.windll.kernel32.GetUserDefaultUILanguage", return_value=language_id):
                self.assertEqual(i18n.detect_system_language(), expected)

    def test_traditional_chinese_direct_and_dynamic_text(self):
        if os.name != "nt":
            self.skipTest("Windows LCMapStringEx")
        self.assertEqual(i18n.tr("运行", "zh_TW"), "運行")
        self.assertEqual(i18n.tr("曲库目录", "zh_TW"), "曲庫目錄")
        self.assertEqual(i18n.tr("12 首", "zh_TW"), "12 首")
        self.assertIn("暫停", i18n.tr("VRChat 启动时自动暂停，退出后自动继续", "zh_TW"))
        self.assertNotEqual(i18n.tr("共 12 首 · 临时缓存", "zh_TW"), "共 12 首 · 临时缓存")

    def test_widget_switch_and_dynamic_update_keep_source_text(self):
        label = QLabel("运行")
        i18n.set_language("en")
        i18n.translate_widget_tree(label)
        self.assertEqual(label.text(), "Run")
        i18n.set_language("ja")
        i18n.translate_widget_tree(label)
        self.assertEqual(label.text(), "実行")
        label.setText("停止服务")
        i18n.translate_widget_tree(label)
        self.assertEqual(label.text(), "サービスを停止")
        label.deleteLater()

    def test_combo_data_and_table_source_survive_language_switches(self):
        combo = QComboBox()
        combo.addItem("全部分类", "all")
        combo.addItem("今年", "year")
        table = QTableWidget(1, 2)
        table.setHorizontalHeaderLabels(["编号", "歌名"])
        table.setItem(0, 0, QTableWidgetItem("已缓存"))
        table.setItem(0, 1, QTableWidgetItem("Song"))
        for language, expected in (("en", "All Categories"), ("ja", "すべての分類"), ("ko", "모든 분류")):
            i18n.set_language(language)
            i18n.translate_widget_tree(combo)
            self.assertEqual(combo.currentData(), "all")
            self.assertEqual(combo.itemText(0), expected)
        for language in ("en", "ja", "ko"):
            i18n.set_language(language)
            i18n.translate_widget_tree(table)
            self.assertEqual(table.horizontalHeaderItem(0).text(), "ID")
            self.assertEqual(table.item(0, 1).text(), "Song")
            self.assertNotEqual(table.item(0, 0).text(), "已缓存")
        combo.deleteLater()
        table.deleteLater()

    def test_combo_translation_does_not_emit_change_signals(self):
        combo = QComboBox()
        combo.addItem("全部分类", "all")
        changes = []
        combo.currentIndexChanged.connect(lambda index: changes.append(index))
        i18n.set_language("en")
        i18n.translate_widget_tree(combo)
        self.assertEqual(changes, [])
        self.assertEqual(combo.currentData(), "all")
        combo.deleteLater()

    def test_editable_paths_are_not_translated(self):
        from PySide6.QtWidgets import QLineEdit
        field = QLineEdit()
        path = r"D:\软件小玩具开发\跳舞房缓存\曲库\3528.mp4"
        field.setText(path)
        field.setPlaceholderText("选择临时缓存目录")
        changes = []
        field.textChanged.connect(changes.append)
        for language in ("en", "ja", "ko", "zh_TW", "zh"):
            i18n.set_language(language)
            i18n.translate_widget_tree(field)
            self.assertEqual(field.text(), path)
        self.assertEqual(changes, [])
        field.deleteLater()

    def test_message_boxes_translate_text_custom_and_standard_buttons(self):
        from PySide6.QtCore import QTimer
        expected = {"en": "Organize into Category Folders", "ja": "分類フォルダーに整理",
                    "ko": "분류 폴더로 정리", "zh_TW": "整理為分類文件夾"}
        for language, title in expected.items():
            i18n.set_language(language)
            box = i18n.MessageBox()
            box.setWindowTitle("整理为分类文件夹")
            box.setText("确定开始吗？")
            box.setInformativeText("是否前往 GitHub 下载更新？")
            box.setStandardButtons(i18n.MessageBox.StandardButton.Yes | i18n.MessageBox.StandardButton.No)
            custom = box.addButton("取消更改", i18n.MessageBox.ButtonRole.RejectRole)
            QTimer.singleShot(0, box.accept)
            box.exec()
            self.assertEqual(box.windowTitle(), title)
            self.assertEqual(box.informativeText(), i18n.tr("是否前往 GitHub 下载更新？", language))
            self.assertEqual(custom.text(), i18n.tr("取消更改", language))
            standard_texts = {box.standardButton(button): button.text() for button in box.buttons()}
            self.assertEqual(standard_texts[i18n.MessageBox.StandardButton.Yes], i18n.tr("是", language))
            self.assertEqual(standard_texts[i18n.MessageBox.StandardButton.No], i18n.tr("否", language))
            box.deleteLater()

    def test_dynamic_messages_preserve_paths_and_unknown_logs(self):
        old = r"D:\软件小玩具开发\旧曲库"
        new = r"D:\软件小玩具开发\新曲库"
        text = f"WannaDance 永久曲库目录已更改：\n\n原目录：{old}\n新目录：{new}\n\n是否把原目录中的软件视频迁移到新目录？"
        for language in ("en", "ja", "ko", "zh_TW"):
            translated = i18n.tr(text, language)
            self.assertIn(old, translated)
            self.assertIn(new, translated)
            self.assertNotEqual(translated, text)
            self.assertEqual(i18n.tr_log("原始错误 " + old, language), "原始错误 " + old)
        prompt = ("检测到你仍在使用旧版平铺曲库（PyPyDance、WannaDance）。\n\n"
                  "当前版本仍可正常读取，但未来版本可能不再支持这种曲库格式。"
                  "建议尽快变更曲库目录，并使用“整理为分类文件夹”转换为新格式。\n\n"
                  "提示不会自动移动或删除文件；转换前仍会要求你确认，并保留冲突检测、取消和失败回滚。")
        for language in ("en", "ja", "ko", "zh_TW"):
            translated = i18n.tr(prompt, language)
            self.assertNotEqual(translated, prompt)
            self.assertIn("PyPyDance", translated)
            self.assertIn("WannaDance", translated)

    def test_dynamic_statistics_and_logs_translate_without_changing_values(self):
        summary = "最常与你共同游玩的好友：七河みこ · 30 小时 5 分钟；共统计到 59 位好友。"
        logs = [
            "监听日志文件: output_log_2026-11-18_23-43-09.txt",
            "歌单直取表已更新：4914 首",
            "自动优选 CDN：共 8 个候选，先小样本初筛再完整测速...",
            "初筛使用 3 个并发线程，每个节点下载 256KB 取样...",
            "测速 104.20.20.68: 1.53 MB/s",
            "CDN 优选：已自动应用 162.159.224.188（1.76 MB/s），替换原 104.20.20.68",
            "后台下载 wd_11527.mp4 连接已恢复，使用当前优选节点重试（1/1）",
            "NVIDIA NVENC 编码失败，改用 CPU x264：GPU encode failed",
            "GPU 编码失败，已改用 CPU x264；压缩完成（CRF 30），节省 150 字节",
        ]
        for language in ("en", "ja", "ko", "zh_TW"):
            translated = i18n.tr(summary, language)
            self.assertNotEqual(translated, summary)
            self.assertIn("七河みこ", translated)
            self.assertNotIn("小时", translated)
            for log in logs:
                result = i18n.tr_log(log, language)
                self.assertNotEqual(result, log, f"{language}: {log}")
                for value in ("output_log_2026-11-18_23-43-09.txt", "4914", "104.20.20.68",
                              "162.159.224.188", "wd_11527.mp4"):
                    if value in log:
                        self.assertIn(value, result)

    def test_dudu_event_dynamic_messages_translate_and_preserve_values(self):
        prompt = ("活动 20261124 共 17 首，本地已有 14 首，需要下载 3 首。\n\n"
                  "临时活动文件将至少保留到 2026-11-25 00:00。确定后会启动缓存服务（如尚未运行）并只下载缺失歌曲。")
        capacity = ("临时缓存上限不足，活动预载已暂停，还需约 12.5 MB；"
                    "请提高上限、清理普通缓存，或在高级模式允许活动超限")
        for language in ("en", "ja", "ko", "zh_TW"):
            for text in (prompt, capacity):
                translated = i18n.tr(text, language)
                self.assertNotEqual(translated, text)
            for value in ("20261124", "17", "14", "3", "2026-11-25 00:00"):
                self.assertIn(value, i18n.tr(prompt, language))
            self.assertIn("12.5", i18n.tr(capacity, language))

    def test_new_widget_show_event_is_translated(self):
        i18n.install(self.app)
        label = QLabel("运行")
        i18n.set_language("en")
        label.show()
        self.app.processEvents()
        self.assertEqual(label.text(), "Run")
        label.close()
        label.deleteLater()

    def test_vr_panel_and_report_rendering_use_selected_language(self):
        from dance_ui import play_report, vr_overlay
        state = {"running": True, "language": "zh", "current": None, "queue": [],
                 "stats": {"hits": 1, "misses": 2, "saved_mb": 3},
                 "cooldown": {"phase": "idle", "available": True}}
        zh = vr_overlay.render_panel(state)
        state["language"] = "en"
        en = vr_overlay.render_panel(state)
        self.assertEqual(len(zh), len(en))
        self.assertNotEqual(zh, en)
        painter = mock.Mock()
        i18n.set_language("ja")
        play_report._text(painter, None, "本期最爱")
        self.assertIn("この期間", painter.drawText.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
