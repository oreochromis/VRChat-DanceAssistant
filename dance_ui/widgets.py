"""Shared widgets and the application-wide visual system."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QObject, QPoint, QPointF, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGraphicsEffect, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QTableWidget, QHeaderView, QAbstractItemView, QVBoxLayout, QWidget,
)

from . import resources_rc  # noqa: F401 -- registers the bundled control icons
from .theme import BUILTIN_THEMES, readable_on


def rgba(hex_color: str, alpha: float = 1.0) -> str:
    color = QColor(hex_color)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {round(alpha * 255)})"


def on_color(value: str) -> str:
    return readable_on(value)


def stylesheet(theme: dict[str, Any], preferences: dict[str, Any]) -> str:
    scale = preferences.get("scale", 100) / 100
    px = lambda n: max(1, round(n * scale))
    density = preferences.get("density", "standard")
    padding = {"comfortable": 11, "standard": 8, "compact": 5}.get(density, 8)
    accent = theme["accent"]
    text = theme["text_primary"]
    muted = theme["text_secondary"]
    card = rgba(theme["card_background"], theme["card_opacity"])
    border = rgba(theme["border_color"], theme["border_opacity"])
    radius = round(theme["radius"])
    is_dark = QColor(theme["background_color"]).lightnessF() < .45
    subtle = "rgba(255,255,255,12)" if is_dark else "rgba(24,43,73,7)"
    input_bg = rgba(theme["input_background"], max(.85, theme["card_opacity"]))
    button_bg = rgba(theme["button_background"], max(.85, theme["card_opacity"]))
    gradient_coords = {"diagonal": "0, 0, 1, 1", "horizontal": "0, 0, 1, 0",
                       "vertical": "0, 0, 0, 1"}[theme["gradient_direction"]]
    check_icon = "check-dark.svg" if on_color(accent) != "#FFFFFF" else "check-light.svg"
    arrow_tone = "light" if on_color(theme["input_background"]) == "#FFFFFF" else "dark"
    tones = {
        "success": (theme["success_color"], rgba(theme["success_color"], .10)),
        "warning": (theme["warning_color"], rgba(theme["warning_color"], .11)),
        "error": (theme["error_color"], rgba(theme["error_color"], .10)),
        "info": (theme["info_color"], rgba(theme["info_color"], .10)),
        "muted": (muted, rgba(muted, .08)),
    }
    status_rules = "\n".join(
        f'QLabel[statusTone="{tone}"] {{ color:{colors[0]}; background:{colors[1]}; border:none; border-radius:6px; padding:5px 9px; }}'
        for tone, colors in tones.items()
    )
    return f"""
    QWidget {{ color: {text}; font-size: {px(14)}px; }}
    QMainWindow {{ background: {theme['background_color']}; }}
    QLabel, QCheckBox, QRadioButton {{ background: transparent; border: none; }}
    QLabel[muted="true"] {{ color: {muted}; font-size: {px(12)}px; }}
    QLabel[heading="true"], QLabel[pageTitle="true"] {{ font-size: {px(21)}px; font-weight: 650; }}
    QLabel[subheading="true"] {{ font-size: {px(16)}px; font-weight: 600; }}
    QLabel[strong="true"] {{ font-weight: 600; }}
    QLabel[danger="true"] {{ color: {theme['error_color']}; }}
    QToolButton[sectionHeader="true"] {{ background: transparent; border: none; text-align: left; font-size: {px(16)}px; font-weight: 600; padding: 4px 0; }}
    QLabel[heroTitle="true"] {{ font-size: {px(27)}px; font-weight: 700; }}
    QLabel[number="true"] {{ font-size: {px(27)}px; font-weight: 650; }}
    QFrame[card="true"] {{
        background: {card}; border: {theme['border_width']}px solid {border};
        border-radius: {radius}px;
    }}
    QFrame[hero="true"] {{ background: qlineargradient(x1: {gradient_coords.split(',')[0]}, y1: {gradient_coords.split(',')[1]}, x2: {gradient_coords.split(',')[2]}, y2: {gradient_coords.split(',')[3]}, stop: 0 {rgba(accent, .10)}, stop: 0.55 {rgba(theme['secondary_accent'], .08)}, stop: 1 {rgba(theme['hero_gradient'], theme['hero_gradient_opacity'])}); }}
    QLabel[metricTone] {{ border-radius: 6px; padding: 4px 8px; font-weight: 600; }}
    QLabel[metricTone="success"] {{ color: {theme['success_color']}; background: {rgba(theme['success_color'], .09)}; }}
    QLabel[metricTone="warning"] {{ color: {theme['warning_color']}; background: {rgba(theme['warning_color'], .10)}; }}
    QLabel[metricTone="highlight"] {{ color: {text}; background: {rgba(theme['highlight'], .13)}; border: 1px solid {rgba(theme['highlight'], .35)}; }}
    QFrame[metricCard="true"] {{ min-height: {px(78)}px; }}
    QFrame[metricCard="true"][metricTone="success"] {{ border-top: 3px solid {theme['success_color']}; }}
    QFrame[metricCard="true"][metricTone="warning"] {{ border-top: 3px solid {theme['warning_color']}; }}
    QFrame[metricCard="true"][metricTone="highlight"] {{ border-top: 3px solid {theme['highlight']}; }}
    QFrame[metricCard="true"][metricTone="secondary"] {{ border-top: 3px solid {theme['secondary_accent']}; }}
    QFrame#noticeBanner {{ border-radius: 8px; border: 1px solid {border}; }}
    QFrame#noticeBanner[noticeTone="error"] {{ background: {rgba(theme['error_color'], .10)}; color: {theme['error_color']}; }}
    QFrame#noticeBanner[noticeTone="success"] {{ background: {rgba(theme['success_color'], .08)}; color: {text}; }}
    QFrame#noticeBanner[noticeTone="info"] {{ background: {rgba(accent, .08)}; color: {text}; }}
    QWidget#header {{
        background: {rgba(theme['nav_background'], theme['nav_opacity'])};
        border-bottom: 1px solid {border};
    }}
    QWidget#footer {{ background: {rgba(theme['nav_background'], theme['nav_opacity'])}; border-top: 1px solid {border}; }}
    QPushButton {{
        background: {button_bg}; color: {theme['button_text']}; border: 1px solid {border}; border-radius: {min(radius, 8)}px;
        padding: {px(padding)}px {px(14)}px; min-height: {px(18)}px;
    }}
    QPushButton:hover {{ background: {theme['button_hover_background']}; border-color: {rgba(accent, .50)}; }}
    QPushButton:pressed {{ background: {rgba(accent, .20)}; }}
    QPushButton:focus {{ border-color: {accent}; }}
    QPushButton:disabled {{ color: {muted}; background: {subtle}; border-color: {border}; }}
    QPushButton[primary="true"] {{ background: {accent}; color: {theme['primary_button_text']}; border-color: {accent}; font-weight: 600; }}
    QPushButton[primary="true"]:hover {{ background: {QColor(accent).lighter(110).name()}; }}
    QPushButton[primary="true"]:disabled {{ background: {rgba(accent, .30)}; color: {muted}; border-color: transparent; }}
    QPushButton[nav="true"] {{
        color: {theme['nav_text']}; background: transparent; border: none;
        border-bottom: 3px solid transparent; border-radius: 0;
        padding: {px(17)}px {px(19)}px; font-size: {px(16)}px;
    }}
    QPushButton[nav="true"]:checked {{
        color: {theme['nav_active_text']};
        background: {rgba(theme['nav_active_background'], theme['nav_active_opacity'])};
        border-bottom-color: {accent}; font-weight: 650;
    }}
    QPushButton[nav="true"][compact="true"] {{ padding-left: {px(11)}px; padding-right: {px(11)}px; }}
    QPushButton[link="true"], QPushButton[subtle="true"] {{ color: {muted}; background: transparent; border: none; padding: {px(3)}px; }}
    QPushButton[link="true"]:hover, QPushButton[subtle="true"]:hover {{ color: {text}; background: {rgba(muted, .08)}; border: none; }}
    QPushButton[link="true"]:pressed, QPushButton[subtle="true"]:pressed {{ color: {text}; background: {rgba(muted, .13)}; }}
    QPushButton[danger="true"] {{ color: {theme['error_color']}; }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox {{
        background: {input_bg}; border: 1px solid {border}; border-radius: {min(radius, 7)}px;
        padding: {px(padding)}px {px(10)}px; min-height: {px(19)}px;
        selection-background-color: {accent}; selection-color: {theme['primary_button_text']};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus, QComboBox:focus {{ border-color: {accent}; }}
    QSpinBox, QDoubleSpinBox {{ padding-right: {px(28)}px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: {px(25)}px; border: none; background: transparent; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: {px(25)}px; border: none; background: transparent; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url(:/controls/up-{arrow_tone}.svg); width: {px(10)}px; height: {px(7)}px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url(:/controls/down-{arrow_tone}.svg); width: {px(10)}px; height: {px(7)}px; }}
    QComboBox::drop-down, QDateEdit::drop-down {{ subcontrol-origin: border; subcontrol-position: top right; width: {px(28)}px; border: none; background: transparent; }}
    QComboBox::down-arrow, QDateEdit::down-arrow {{ image: url(:/controls/down-{arrow_tone}.svg); width: {px(10)}px; height: {px(7)}px; }}
    QLineEdit:disabled, QDateEdit:disabled, QComboBox:disabled {{ color: {muted}; background: {subtle}; }}
    QComboBox QAbstractItemView {{ background: {theme['card_background']}; color: {text}; selection-background-color: {accent}; selection-color: {on_color(accent)}; }}
    QCalendarWidget {{ background: {theme['card_background']}; border: 1px solid {border}; }}
    QCalendarWidget QWidget, QCalendarWidget QTableView {{ background: {theme['card_background']}; color: {text}; }}
    QCalendarWidget QWidget#qt_calendar_navigationbar {{
        background: {theme['card_background']}; border: none; border-bottom: 1px solid {border}; padding: {px(4)}px;
    }}
    QCalendarWidget QToolButton {{
        color: {text}; background: transparent; border: none; border-radius: 4px; padding: {px(6)}px; font-weight: 600;
    }}
    QCalendarWidget QToolButton:hover {{ background: {rgba(accent, .12)}; }}
    QCalendarWidget QToolButton:pressed {{ background: {rgba(accent, .20)}; }}
    QCalendarWidget QTableView {{ outline: none; selection-background-color: {accent}; selection-color: {theme['primary_button_text']}; }}
    QCalendarWidget QTableView::item {{ border: none; padding: {px(4)}px; color: {text}; }}
    QCalendarWidget QTableView::item:selected {{ background: {accent}; color: {on_color(accent)}; }}
    QCalendarWidget QHeaderView::section {{ background: {subtle}; color: {muted}; border: none; padding: {px(5)}px; }}
    QCheckBox {{ spacing: {px(9)}px; padding: {px(3)}px 0; }}
    QCheckBox::indicator {{ width: {px(16)}px; height: {px(16)}px; border: 1px solid {rgba(muted, .6)}; border-radius: 4px; background: {input_bg}; }}
    QCheckBox::indicator:checked {{ image: url(:/controls/{check_icon}); border-color: {accent}; background: {accent}; }}
    QCheckBox::indicator:disabled {{ border-color: {border}; background: {subtle}; }}
    QScrollArea, QWidget#page, QWidget#pageViewport, QWidget#appearancePage, QWidget#appearanceContent, QWidget#appearanceScroll {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {rgba(theme['scrollbar_color'], .55)}; min-height: 30px; border-radius: 3px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{ background: transparent; height: 9px; }}
    QScrollBar::handle:horizontal {{ background: {rgba(theme['scrollbar_color'], .55)}; min-width: 30px; border-radius: 3px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QTableWidget {{
        background: transparent; alternate-background-color: {subtle}; border: none;
        gridline-color: {border}; selection-background-color: {theme['table_selection_background']};
        selection-color: {text}; outline: none;
    }}
    QTableWidget::item {{ padding: {px(8)}px; border-bottom: 1px solid {border}; }}
    QHeaderView, QHeaderView::section {{ background: {theme['table_header_background']}; color: {theme['table_header_text']}; border: none; }}
    QHeaderView::section {{ padding: {px(7)}px {px(8)}px; font-weight: 500; }}
    QTableCornerButton::section {{ background: transparent; border: none; }}
    QProgressBar {{
        background: {rgba(theme['text_secondary'], .16)}; border: none; border-radius: 4px;
        min-height: 8px; max-height: 8px;
    }}
    QProgressBar::chunk {{ background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0, stop: 0 {accent}, stop: 1 {theme['secondary_accent']}); border-radius: 4px; }}
    QPlainTextEdit {{ background: {input_bg}; border: 1px solid {border}; border-radius: {min(radius, 8)}px; padding: 8px; selection-background-color: {accent}; selection-color: {theme['primary_button_text']}; }}
    QMenu {{ background: {theme['card_background']}; color: {text}; border: 1px solid {border}; padding: 5px; }}
    QMenu::item:selected {{ background: {theme['table_selection_background']}; color: {text}; }}
    QSlider::groove:horizontal {{ height: 5px; background: {rgba(theme['text_secondary'], .20)}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ width: 15px; margin: -5px 0; background: {accent}; border: 2px solid {theme['card_background']}; border-radius: 8px; }}
    QGroupBox {{ border: 1px solid {border}; border-radius: {radius}px; margin-top: 14px; padding: 14px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 5px; }}
    QToolTip {{ background: {theme['card_background']}; color: {text}; border: 1px solid {border}; padding: 6px; }}
    QMessageBox, QDialog {{ background: {theme['background_color']}; }}
    {status_rules}
    """


class Backdrop(QWidget):
    """Paint only the background; descendants retain their own opacity."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme: dict[str, Any] = {}
        self.image: Image.Image | None = None
        self._image_key = None
        self._render_key = None
        self._render: QPixmap | None = None

    def set_appearance(self, theme: dict, image_path: Path | None):
        self.theme = dict(theme)
        key = (str(image_path), theme.get("image_blur", 0))
        if key != self._image_key:
            self._image_key = key
            self.image = None
            if image_path:
                try:
                    with Image.open(image_path) as source:
                        source = ImageOps.exif_transpose(source).convert("RGBA")
                        source.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                        if theme.get("image_blur", 0):
                            source = source.filter(ImageFilter.GaussianBlur(theme["image_blur"]))
                        self.image = source.copy()
                except (OSError, ValueError):
                    self.image = None
            self._render_key = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        t = self.theme
        foundation = BUILTIN_THEMES.get(t.get("base", "light"), BUILTIN_THEMES["light"])["background_color"]
        painter.fillRect(self.rect(), QColor(foundation))
        base = QColor(t.get("background_color", "#f4f7fb"))
        base.setAlphaF(t.get("background_opacity", 1.0))
        painter.fillRect(self.rect(), base)
        gradient_opacity = t.get("background_gradient_opacity", 0) * t.get("background_opacity", 1.0)
        if gradient_opacity > 0:
            direction = t.get("gradient_direction", "diagonal")
            end = QPointF(self.width(), self.height()) if direction == "diagonal" else (
                QPointF(self.width(), 0) if direction == "horizontal" else QPointF(0, self.height()))
            gradient = QLinearGradient(QPointF(0, 0), end)
            start = QColor(t.get("background_color", "#F3F5F9"))
            start.setAlphaF(t.get("background_opacity", 1.0))
            finish = QColor(t.get("background_gradient", "#E9ECFF"))
            finish.setAlphaF(gradient_opacity)
            gradient.setColorAt(0, start)
            gradient.setColorAt(1, finish)
            painter.fillRect(self.rect(), gradient)
        if self.image is not None and self.width() and self.height():
            fit = t.get("image_fit", "cover")
            key = (self.width(), self.height(), fit, self._image_key)
            if key != self._render_key:
                size = (self.width(), self.height())
                if fit == "cover":
                    prepared = ImageOps.fit(self.image, size, Image.Resampling.LANCZOS)
                elif fit == "contain":
                    prepared = ImageOps.contain(self.image, size, Image.Resampling.LANCZOS)
                elif fit == "stretch":
                    prepared = self.image.resize(size, Image.Resampling.LANCZOS)
                else:
                    prepared = self.image
                self._render = QPixmap.fromImage(ImageQt(prepared))
                self._render_key = key
            if self._render:
                painter.setOpacity(t.get("image_opacity", 1.0))
                if fit == "tile":
                    painter.drawTiledPixmap(self.rect(), self._render)
                else:
                    x = (self.width() - self._render.width()) // 2
                    y = (self.height() - self._render.height()) // 2
                    painter.drawPixmap(x, y, self._render)
                painter.setOpacity(1)
        overlay = QColor(t.get("overlay_color", "#ffffff"))
        overlay.setAlphaF(t.get("overlay_opacity", 0))
        painter.fillRect(self.rect(), overlay)
        painter.end()


class TextHalo(QGraphicsEffect):
    """A four-direction text halo, used only on selected text labels."""

    def __init__(self, color: str, opacity: float, size: float, blur: float, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.opacity = opacity
        self.size = size
        self.blur = blur

    def boundingRectFor(self, rect):
        margin = self.size + self.blur * 2 + 1
        return rect.adjusted(-margin, -margin, margin, margin)

    def draw(self, painter):
        offset = QPoint()
        pixmap = self.sourcePixmap(Qt.CoordinateSystem.LogicalCoordinates, offset, QGraphicsEffect.PixmapPadMode.NoPad)
        if pixmap.isNull():
            return
        dpr = pixmap.devicePixelRatio()
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        raw = bytes(image.constBits())
        source_image = Image.frombytes("RGBA", (image.width(), image.height()), raw, "raw", "RGBA", image.bytesPerLine())
        alpha = source_image.getchannel("A")
        margin = round((self.blur * 2 + self.size + 2) * dpr)
        mask = Image.new("L", (alpha.width + margin * 2, alpha.height + margin * 2))
        size = self.size * dpr
        offsets = [(0, 0)] if size == 0 else [(size, 0), (-size, 0), (0, size), (0, -size)]
        from PIL import ImageChops
        for dx, dy in offsets:
            layer = Image.new("L", mask.size)
            layer.paste(alpha, (margin + round(dx), margin + round(dy)))
            mask = ImageChops.lighter(mask, layer)
        if self.blur:
            mask = mask.filter(ImageFilter.GaussianBlur(self.blur * dpr / 2))
        mask = mask.point(lambda value: round(value * self.opacity))
        shadow = Image.new("RGBA", mask.size, self.color.name())
        shadow.putalpha(mask)
        shadow_pixmap = QPixmap.fromImage(ImageQt(shadow))
        shadow_pixmap.setDevicePixelRatio(dpr)
        painter.drawPixmap(QPointF(offset.x() - margin / dpr, offset.y() - margin / dpr), shadow_pixmap)
        painter.drawPixmap(offset, pixmap)


class ComboWheelFilter(QObject):
    def eventFilter(self, obj, event):
        if isinstance(obj, QComboBox) and event.type() == QEvent.Type.Wheel and not obj.view().isVisible():
            event.ignore()
            return True
        return super().eventFilter(obj, event)


def install_combo_wheel_filter(app):
    if getattr(app, "_dance_combo_wheel_filter", None) is None:
        app._dance_combo_wheel_filter = ComboWheelFilter(app)
        app.installEventFilter(app._dance_combo_wheel_filter)


class NoticeBanner(QFrame):
    close_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("noticeBanner")
        self._raw = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.message = label("", wrap=True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(self.message, 1)
        self.details_button = QPushButton("技术详情")
        self.details_button.setProperty("link", True)
        self.details_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_button.clicked.connect(self._toggle_details)
        top.addWidget(self.details_button)
        self.close_button = QPushButton("×")
        self.close_button.setProperty("link", True)
        self.close_button.setAccessibleName("关闭通知")
        self.close_button.clicked.connect(self.close_requested)
        top.addWidget(self.close_button)
        layout.addLayout(top)
        self.details = label("", muted=True, wrap=True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.hide()
        layout.addWidget(self.details)

    def set_notice(self, message, raw="", *, error=False, busy=False):
        self._raw = str(raw or message)
        self.message.setText(str(message))
        self.details.setText(self._raw)
        self.details.hide()
        self.details_button.setText("技术详情")
        self.details_button.setVisible(bool(self._raw and self._raw != str(message)))
        self.setProperty("noticeTone", "error" if error else "info" if busy else "success")
        self.style().unpolish(self)
        self.style().polish(self)

    def _toggle_details(self):
        visible = not self.details.isVisible()
        self.details.setVisible(visible)
        self.details_button.setText("收起详情" if visible else "技术详情")

    def text(self):
        return self.message.text()

    def rawText(self):
        return self._raw

    def clear(self):
        self.set_notice("")


class Card(QFrame):
    def __init__(self, parent=None, *, hero=False):
        super().__init__(parent)
        self.setProperty("card", True)
        if hero:
            self.setProperty("hero", True)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(20, 14, 20, 14)
        self.box.setSpacing(10)


class HeroCard(Card):
    def __init__(self):
        super().__init__(hero=True)
        self.vivid = False
        self.theme = None

    def set_vivid(self, theme, vivid):
        self.theme = theme
        self.vivid = vivid
        self.update()

    def paintEvent(self, event):
        if not self.vivid or not self.theme:
            super().paintEvent(event)
            return
        theme = self.theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = float(theme["radius"])
        end = (rect.topRight() if theme["gradient_direction"] == "horizontal" else
               rect.bottomLeft() if theme["gradient_direction"] == "vertical" else rect.bottomRight())
        gradient = QLinearGradient(rect.topLeft(), end)
        gradient.setColorAt(0, QColor(theme["accent"]).darker(115))
        gradient.setColorAt(.58, QColor(theme["secondary_accent"]))
        gradient.setColorAt(1, QColor(theme["hero_gradient"]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, radius, radius)
        painter.save()
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)
        decoration = QColor(theme["primary_button_text"])
        decoration.setAlpha(20)
        painter.setBrush(decoration)
        painter.drawEllipse(QRectF(rect.right() - 220, rect.top() - 105, 290, 290))
        painter.drawEllipse(QRectF(rect.right() - 60, rect.bottom() - 90, 160, 160))
        decoration.setAlpha(24)
        painter.setPen(QPen(decoration, 1))
        for offset in range(-60, 240, 42):
            painter.drawLine(QPointF(rect.left() + offset, rect.bottom()),
                             QPointF(rect.left() + offset + 170, rect.top()))
        painter.restore()
        painter.setPen(QPen(QColor(theme["border_color"]), max(1, theme["border_width"])))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)


class MetricCard(QFrame):
    def __init__(self, caption, tone):
        super().__init__()
        self.setProperty("card", True)
        self.setProperty("metricCard", True)
        self.setProperty("metricTone", tone)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 11, 16, 12)
        layout.setSpacing(2)
        layout.addWidget(label(caption, muted=True))
        self.value = label("0")
        self.value.setProperty("number", True)
        layout.addWidget(self.value)

    def set_value(self, value):
        self.value.setText(str(value))


def label(text="", *, muted=False, heading=False, subheading=False, wrap=False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    widget.setProperty("muted", muted)
    widget.setProperty("heading", heading)
    widget.setProperty("subheading", subheading)
    return widget


def page() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.viewport().setObjectName("pageViewport")
    area.viewport().setAutoFillBackground(False)
    body = QWidget()
    body.setObjectName("page")
    body.setAutoFillBackground(False)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(26, 24, 26, 18)
    layout.setSpacing(16)
    area.setWidget(body)
    return area, body, layout


def section_title(title: str, description: str = "") -> QWidget:
    row = QWidget()
    box = QHBoxLayout(row)
    box.setContentsMargins(0, 0, 0, 0)
    box.addWidget(label(title, heading=True))
    box.addStretch()
    if description:
        box.addWidget(label(description, muted=True, wrap=True))
    return row


def table(headers: list[str], stretch_column: int = 0) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().hide()
    widget.setShowGrid(False)
    widget.setAlternatingRowColors(False)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setTextElideMode(Qt.TextElideMode.ElideRight)
    header = widget.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    for index in range(len(headers)):
        header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
    header.setMinimumSectionSize(48)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return widget


def status_label(text: str, state: str = "muted") -> QLabel:
    widget = label(text)
    set_status(widget, text, state)
    return widget


def set_status(widget: QLabel, text: str, state: str = "muted"):
    widget.setText(text)
    widget.setProperty("statusTone", state)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class SegmentedSwitch(QWidget):
    """自绘分段拨杆：滑块随切换滑动，悬停段有文字反馈。颜色由 apply_theme 注入。"""

    modeChanged = Signal(str)
    _PAD = 3

    def __init__(self, modes=(("simple", "简单"), ("advanced", "高级")), parent=None):
        super().__init__(parent)
        self._modes = list(modes)
        self._index = 0
        self._pos = 0.0
        self._hover = -1
        self._colors = {}
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._recalc_size()

    def _recalc_size(self):
        metrics = self.fontMetrics()
        self._seg_w = max(metrics.horizontalAdvance(text) for _, text in self._modes) + 30
        self.setFixedSize(QSize(self._seg_w * len(self._modes) + self._PAD * 2, 30))

    def apply_theme(self, theme: dict[str, Any]):
        is_dark = QColor(theme["background_color"]).lightnessF() < .45
        border = QColor(theme["border_color"])
        border.setAlphaF(min(1.0, theme.get("border_opacity", 1.0) + 0.25))
        self._colors = {
            "track": QColor(255, 255, 255, 18) if is_dark else QColor(24, 43, 73, 10),
            "border": border,
            "thumb": QColor(theme["accent"]),
            "on_thumb": QColor(on_color(theme["accent"])),
            "text": QColor(theme["text_primary"]),
            "muted": QColor(theme["text_secondary"]),
        }
        self.update()

    def mode(self) -> str:
        return self._modes[self._index][0]

    def set_mode(self, mode: str):
        index = next((i for i, (m, _) in enumerate(self._modes) if m == mode), None)
        if index is None:
            return
        self._index = index
        self._pos = float(index)
        self.update()

    def _segment_at(self, x: float) -> int:
        index = int((x - self._PAD) // self._seg_w)
        return index if 0 <= index < len(self._modes) else -1

    def mouseReleaseEvent(self, event):
        index = self._segment_at(event.position().x())
        if index >= 0 and index != self._index:
            self.set_mode(self._modes[index][0])
            self.modeChanged.emit(self.mode())
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        hover = self._segment_at(event.position().x())
        if hover != self._hover:
            self._hover = hover
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _):
        if not self._colors:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        height = self.height()
        track = QRectF(0.5, 0.5, self.width() - 1.0, height - 1.0)
        painter.setPen(QPen(self._colors["border"], 1))
        painter.setBrush(self._colors["track"])
        painter.drawRoundedRect(track, height / 2, height / 2)
        inner_h = height - self._PAD * 2
        thumb = QRectF(self._PAD + self._pos * self._seg_w, self._PAD, self._seg_w, inner_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._colors["thumb"])
        painter.drawRoundedRect(thumb, inner_h / 2, inner_h / 2)
        painter.setFont(self.font())
        active = round(self._pos)
        for index, (_, text) in enumerate(self._modes):
            segment = QRectF(self._PAD + index * self._seg_w, self._PAD, self._seg_w, inner_h)
            if index == active:
                color = self._colors["on_thumb"]
            elif index == self._hover:
                color = self._colors["text"]
            else:
                color = self._colors["muted"]
            painter.setPen(color)
            painter.drawText(segment, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


def app_icon() -> QIcon:
    # PyInstaller preserves this module under _internal/dance_ui and bundles
    # the artwork under _internal/assets, matching the source checkout layout.
    path = Path(__file__).resolve().parents[1] / "assets" / "app-icon.png"
    if not path.is_file():
        raise FileNotFoundError(f"程序图标文件不存在：{path}")
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        raise ValueError(f"无法读取程序图标：{path}")
    return QIcon(pixmap)


def bytes_text(value: float) -> str:
    value = max(0, float(value or 0))
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or suffix == "TB":
            return f"{value:.1f} {suffix}" if suffix != "B" else f"{value:.0f} B"
        value /= 1024
    return ""
