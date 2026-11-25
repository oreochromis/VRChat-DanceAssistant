"""Local UI translations with source-text tracking for dynamic Qt widgets."""
from __future__ import annotations

import os
import re
from functools import wraps, lru_cache

from PySide6.QtCore import QObject, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox, QTabWidget, QTableWidget

from .i18n_en import TRANSLATIONS as EN_TRANSLATIONS, PATTERNS as EN_PATTERNS
from .i18n_ja import TRANSLATIONS as JA_TRANSLATIONS, PATTERNS as JA_PATTERNS
from .i18n_ko import TRANSLATIONS as KO_TRANSLATIONS, PATTERNS as KO_PATTERNS
from .i18n_zh_tw import PATTERNS as ZH_TW_PATTERNS
from .i18n_messages import TRANSLATIONS as MESSAGE_TRANSLATIONS, PATTERNS as MESSAGE_PATTERNS, RawGroups

SUPPORTED_LANGUAGES = {"zh": "简体中文", "zh_TW": "繁體中文", "en": "English", "ja": "日本語", "ko": "한국어"}
TRANSLATIONS = {language: {**catalog, **MESSAGE_TRANSLATIONS.get(language, {})}
                for language, catalog in (("en", EN_TRANSLATIONS), ("ja", JA_TRANSLATIONS), ("ko", KO_TRANSLATIONS))}
PATTERNS = {language: list(catalog) + list(MESSAGE_PATTERNS.get(language, []))
            for language, catalog in (("en", EN_PATTERNS), ("ja", JA_PATTERNS), ("ko", KO_PATTERNS),
                                      ("zh_TW", ZH_TW_PATTERNS))}
_current_language = "zh"


def detect_system_language():
    if os.name == "nt":
        import ctypes
        language_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        if language_id in (0x0404, 0x0C04, 0x1404):
            return "zh_TW"
        primary = language_id & 0x3FF
        return {0x04: "zh", 0x09: "en", 0x11: "ja", 0x12: "ko"}.get(primary, "zh")
    import locale
    value = (locale.getlocale()[0] or os.environ.get("LANG") or "").lower()
    if value.startswith("ja"):
        return "ja"
    if value.startswith("ko"):
        return "ko"
    if any(value.startswith(region) for region in ("zh_tw", "zh_hk", "zh_mo", "zh-hk", "zh-mo")):
        return "zh_TW"
    if value.startswith("zh"):
        return "zh"
    return "en" if value else "zh"


def set_language(language):
    global _current_language
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError("Unsupported interface language")
    _current_language = language


def current_language():
    return _current_language


@lru_cache(maxsize=4096)
def _to_traditional(text):
    if os.name != "nt":
        return text
    import ctypes
    from ctypes import wintypes
    function = ctypes.windll.kernel32.LCMapStringEx
    function.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_int,
                         wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    output = ctypes.create_unicode_buffer(len(text) + 16)
    result = function("zh-TW", 0x04000000, text, len(text), output, len(output), None, None, 0)
    return output.value if result > 0 else text


def tr(text, language=None):
    if not isinstance(text, str) or not text:
        return text
    language = language or _current_language
    if language == "zh":
        return text
    if language == "zh_TW":
        for pattern, template in PATTERNS["zh_TW"]:
            match = pattern.fullmatch(text)
            if match:
                return (_to_traditional(str(template)).format(*match.groups()) if isinstance(template, RawGroups)
                        else _to_traditional(template.format(*match.groups())))
        return _to_traditional(text)
    direct = TRANSLATIONS.get(language, {}).get(text)
    if direct is not None:
        return direct
    for pattern, template in PATTERNS.get(language, []):
        match = pattern.fullmatch(text)
        if match:
            return template(*match.groups()) if callable(template) else template.format(*match.groups())
    return text


def tr_log(text, language=None):
    language = language or _current_language
    if language != "zh_TW":
        return tr(text, language)
    for pattern, template in PATTERNS["zh_TW"]:
        match = pattern.fullmatch(text)
        if match:
            return (_to_traditional(str(template)).format(*match.groups()) if isinstance(template, RawGroups)
                        else _to_traditional(template.format(*match.groups())))
    return text


def _translate_property(widget, name, setter):
    if name == "text" and type(widget).__name__ == "QLineEdit":
        return
    if not hasattr(widget, name) or not hasattr(widget, setter):
        return
    getter = getattr(widget, name)
    setter = getattr(widget, setter)
    try:
        current = getter()
    except (TypeError, RuntimeError):
        return
    if not isinstance(current, str) or not current:
        return
    original_name = "_i18n_original_" + name
    translated_name = "_i18n_translated_" + name
    original = widget.property(original_name)
    previous = widget.property(translated_name)
    if not original or (previous and current != previous and current != original):
        original = current
        widget.setProperty(original_name, original)
    translated = tr(original)
    widget.setProperty(translated_name, translated)
    if current != translated:
        setter(translated)


def _translate_combo(combo):
    count = combo.count()
    original = combo.property("_i18n_combo_original")
    previous = combo.property("_i18n_combo_translated")
    current = [combo.itemText(index) for index in range(count)]
    changed = not isinstance(original, list) or len(original) != count or not isinstance(previous, list) or len(previous) != count
    if not changed:
        for index in range(count):
            if current[index] != original[index] and current[index] != previous[index]:
                changed = True
                break
    if changed:
        original = current
        combo.setProperty("_i18n_combo_original", original)
    translated_values = [tr(text) for text in original]
    combo.setProperty("_i18n_combo_translated", translated_values)
    was_blocked = combo.signalsBlocked()
    combo.blockSignals(True)
    try:
        for index, translated in enumerate(translated_values):
            if combo.itemText(index) != translated:
                combo.setItemText(index, translated)
    finally:
        combo.blockSignals(was_blocked)


def _translate_item(item):
    original = item.data(1000)
    previous = item.data(1001)
    current = item.text()
    if not isinstance(original, str) or (current != original and current != previous):
        original = current
    translated = tr(original)
    item.setData(1000, original)
    item.setData(1001, translated)
    if current != translated:
        item.setText(translated)


def _translate_table(table):
    for index in range(table.columnCount()):
        item = table.horizontalHeaderItem(index)
        if item is not None:
            _translate_item(item)
    for row in range(table.rowCount()):
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                _translate_item(item)


def _translate_tabs(widget):
    original = widget.property("_i18n_tabs_original")
    previous = widget.property("_i18n_tabs_translated")
    current = [widget.tabText(index) for index in range(widget.count())]
    if (not isinstance(original, list) or len(original) != len(current)
            or not isinstance(previous, list) or len(previous) != len(current)
            or any(current[index] != original[index] and current[index] != previous[index]
                   for index in range(len(current)))):
        original = current
        widget.setProperty("_i18n_tabs_original", original)
    translated = [tr(text) for text in original]
    widget.setProperty("_i18n_tabs_translated", translated)
    for index, text in enumerate(translated):
        widget.setTabText(index, text)


def translate_widget_tree(root):
    if root is None:
        return
    try:
        widgets = [root] + root.findChildren(QObject)
    except RuntimeError:
        return
    for widget in widgets:
        try:
            for name, setter in (("text", "setText"), ("toolTip", "setToolTip"),
                                 ("windowTitle", "setWindowTitle"), ("placeholderText", "setPlaceholderText"),
                                 ("statusTip", "setStatusTip"), ("whatsThis", "setWhatsThis"),
                                 ("informativeText", "setInformativeText"),
                                 ("suffix", "setSuffix"), ("prefix", "setPrefix")):
                _translate_property(widget, name, setter)
            if isinstance(widget, QComboBox):
                _translate_combo(widget)
            elif isinstance(widget, QTabWidget):
                _translate_tabs(widget)
            elif isinstance(widget, QTableWidget):
                _translate_table(widget)
            if hasattr(widget, "refresh_translation"):
                widget.refresh_translation()
        except (RuntimeError, TypeError):
            continue


def translate_all():
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        translate_widget_tree(widget)


class MessageBox(QMessageBox):
    def exec(self):
        translate_widget_tree(self)
        standard_texts = {
            QMessageBox.StandardButton.Yes: "是", QMessageBox.StandardButton.No: "否",
            QMessageBox.StandardButton.Ok: "确定", QMessageBox.StandardButton.Cancel: "取消",
            QMessageBox.StandardButton.Close: "关闭", QMessageBox.StandardButton.Retry: "重试",
            QMessageBox.StandardButton.Help: "帮助",
        }
        for button in self.buttons():
            source = standard_texts.get(self.standardButton(button))
            if source:
                button.setText(tr(source))
        return super().exec()

    @classmethod
    def _show(cls, icon, parent, title, text, buttons, default_button):
        box = cls(parent)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(buttons)
        if default_button != QMessageBox.StandardButton.NoButton:
            box.setDefaultButton(default_button)
        return box.exec()

    @classmethod
    def question(cls, parent, title, text, buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                 default_button=QMessageBox.StandardButton.NoButton):
        return cls._show(QMessageBox.Icon.Question, parent, title, text, buttons, default_button)

    @classmethod
    def warning(cls, parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                default_button=QMessageBox.StandardButton.NoButton):
        return cls._show(QMessageBox.Icon.Warning, parent, title, text, buttons, default_button)

    @classmethod
    def information(cls, parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                    default_button=QMessageBox.StandardButton.NoButton):
        return cls._show(QMessageBox.Icon.Information, parent, title, text, buttons, default_button)

    @classmethod
    def critical(cls, parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                 default_button=QMessageBox.StandardButton.NoButton):
        return cls._show(QMessageBox.Icon.Critical, parent, title, text, buttons, default_button)


def translate_after(function):
    @wraps(function)
    def wrapper(self, *args, **kwargs):
        result = function(self, *args, **kwargs)
        if _current_language != "zh":
            QTimer.singleShot(0, lambda: translate_widget_tree(self))
        return result
    return wrapper


class _LanguageEventFilter(QObject):
    def eventFilter(self, widget, event):
        if event.type() == QEvent.Type.Show and _current_language != "zh":
            QTimer.singleShot(0, lambda widget=widget: translate_widget_tree(widget))
        return False


def install(app):
    if app is None or app.property("_dance_i18n_installed"):
        return
    event_filter = _LanguageEventFilter(app)
    app.installEventFilter(event_filter)
    app.setProperty("_dance_i18n_installed", True)
    app._dance_i18n_filter = event_filter
