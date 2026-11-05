"""以 Qt 原生绘制的点歌年度报告，支持导出高清 PNG 分享图。"""
from __future__ import annotations

import copy
import math

from PySide6.QtCore import QDate, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLineEdit, QMessageBox, QScrollArea, QVBoxLayout, QWidget

from .pages import button
from .vrcx_stats import format_duration
from .widgets import label


def _font(size, weight=QFont.Weight.Normal):
    font = QFont()
    font.setPixelSize(size)
    font.setWeight(weight)
    return font


def _text(painter, rect, text, *, size=14, color=None, weight=QFont.Weight.Normal,
          align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter):
    painter.setFont(_font(size, weight))
    if color is not None:
        painter.setPen(color)
    painter.drawText(rect, align, str(text))


def _elided(painter, text, width):
    return painter.fontMetrics().elidedText(str(text), Qt.TextElideMode.ElideRight, max(0, round(width)))


class ReportCanvas(QWidget):
    def __init__(self, data, start: QDate, end: QDate, source: str, theme: dict, parent=None):
        super().__init__(parent)
        self.data = copy.deepcopy(data)
        self.start, self.end, self.source = start, end, source
        self.songs = sorted(self.data.get("songs") or [],
                            key=lambda song: (-int(song.get("count", 0)), int(song.get("rank", 0))))
        self.players = list(self.data.get("players") or [])
        vrcx = self.data.get("vrcx") or {}
        self.vrcx_friends = list(vrcx.get("friends") or []) if isinstance(vrcx, dict) else []
        self.daily_play = sorted((item for item in (vrcx.get("daily_play") or [])
                                  if isinstance(item, dict) and item.get("day")),
                                 key=lambda item: item["day"]) if isinstance(vrcx, dict) else []
        self.total_play_seconds = int(vrcx.get("total_play_seconds", 0)) if isinstance(vrcx, dict) else 0
        self.self_played = int(self.data.get("self_played", 0))
        self.trend_mode, self.summary_mode = "day", "month"
        self.trend = self._trend_for("day")
        self.background = QColor(theme["background_color"])
        self.card = QColor(theme["card_background"])
        self.text = QColor(theme["text_primary"])
        self.muted = QColor(theme["text_secondary"])
        self.accent = QColor(theme["accent"])
        self.secondary = QColor(theme["secondary_accent"])
        self.highlight = QColor(theme["highlight"])
        self.hero_text = QColor(theme["primary_button_text"])
        self.hero_gradient_opacity = theme["hero_gradient_opacity"]
        self.gradient_direction = theme["gradient_direction"]
        self.dark = self.background.lightnessF() < .45
        self.soft = QColor(self.accent)
        self.soft.setAlpha(24 if self.dark else 18)
        self.border = QColor(theme["border_color"])
        self.border.setAlpha(150)
        if start.month() == 1 and start.day() == 1 and start.year() == end.year():
            self.title = f"我的 {start.year()} 年度点歌报告"
            self.kicker = f"{start.year()} · ANNUAL DANCE REPORT"
        else:
            self.title = "我的点歌时光报告"
            self.kicker = "MY DANCE COLLECTION"
        self.setAccessibleName("点歌年度报告")
        self.setMinimumWidth(720)
        self.setMinimumHeight(self._report_height(self.width()))

    def set_title(self, text):
        self.title = str(text).strip() or "我的点歌报告"
        self.update()

    def set_trend_mode(self, mode):
        if mode not in ("day", "month", "year"):
            return
        self.trend_mode = mode
        self.trend = self._trend_for(mode)
        self.setMinimumHeight(self._report_height(self.width()))
        self.update()

    def set_summary_mode(self, mode):
        if mode in ("month", "day"):
            self.summary_mode = mode
            self.update()

    def _trend_for(self, mode):
        totals = {}
        for item in self.daily_play:
            day = str(item.get("day") or "")
            if len(day) < 4:
                continue
            period = day if mode == "day" else day[:7] if mode == "month" else day[:4]
            seconds = max(0, int(item.get("seconds", 0)))
            totals[period] = totals.get(period, 0) + seconds
        return [{"period": period, "seconds": totals[period]} for period in sorted(totals)]

    def resizeEvent(self, event):
        self.setMinimumHeight(self._report_height(self.width()))
        super().resizeEvent(event)

    def _report_height(self, width):
        height = 704
        if len(self.songs) > 3:
            height += 100 + min(len(self.songs), 10) * 44 + 24
        height += 310 if len(self.trend) > 1 else 230
        height += 24
        if self.vrcx_friends:
            height += 130
        return height + 52 + 28

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw(painter, self.width())

    def save_png(self, path):
        width, height = max(720, self.width()), self._report_height(max(720, self.width()))
        image = QImage(width * 2, height * 2, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(2.0)
        image.fill(self.background)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw(painter, width)
        painter.end()
        return image.save(path, "PNG")

    def _draw(self, painter, width):
        painter.fillRect(QRectF(0, 0, width, self._report_height(width)), self.background)
        margin, content = 28, width - 56
        hero = QRectF(margin, 28, content, 270)
        self._draw_hero(painter, hero)
        metrics_y = hero.bottom() + 18
        self._draw_metrics(painter, QRectF(margin, metrics_y, content, 112))
        favorite_title_y = metrics_y + 146
        _text(painter, QRectF(margin, favorite_title_y, content, 34), "本期最爱",
              size=23, color=self.text, weight=QFont.Weight.DemiBold)
        self._draw_favorites(painter, QRectF(margin, favorite_title_y + 34, content, 184))
        charts_y = favorite_title_y + 34 + 184 + 24
        if len(self.songs) > 3:
            top_height = 100 + min(len(self.songs), 10) * 44
            self._draw_top_songs(painter, QRectF(margin, charts_y, content, top_height))
            charts_y += top_height + 24
        if len(self.trend) > 1:
            gap = 18
            monthly_width = round((content - gap) * .62)
            self._draw_trend(painter, QRectF(margin, charts_y, monthly_width, 310))
            self._draw_sources(painter, QRectF(margin + monthly_width + gap, charts_y,
                                               content - monthly_width - gap, 310))
            charts_y += 310
        else:
            self._draw_sources(painter, QRectF(margin, charts_y, content, 230))
            charts_y += 230
        footer_y = charts_y + 36
        if self.vrcx_friends:
            friend_y = charts_y + 24
            self._draw_vrcx_friend(painter, QRectF(margin, friend_y, content, 112))
            footer_y = friend_y + 126
        _text(painter, QRectF(margin, footer_y, content, 30),
              "VRChat 跳舞房助手  ·  本地点歌与 VRCX 记录", size=12, color=self.muted,
              align=Qt.AlignmentFlag.AlignCenter)

    def _card(self, painter, rect, color=None):
        painter.setPen(QPen(self.border, 1))
        painter.setBrush(color or self.card)
        painter.drawRoundedRect(rect, 16, 16)

    def _draw_hero(self, painter, rect):
        end_point = (rect.topRight() if self.gradient_direction == "horizontal" else
                     rect.bottomLeft() if self.gradient_direction == "vertical" else rect.bottomRight())
        gradient = QLinearGradient(rect.topLeft(), end_point)
        gradient.setColorAt(0, self.accent.darker(120))
        gradient.setColorAt(.58, self.accent)
        gradient.setColorAt(1, self.secondary if self.hero_gradient_opacity > 0 else self.accent.lighter(135))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, 22, 22)
        painter.save()
        path = QPainterPath()
        path.addRoundedRect(rect, 22, 22)
        painter.setClipPath(path)
        decoration = QColor(self.hero_text)
        decoration.setAlpha(18)
        painter.setBrush(decoration)
        painter.drawEllipse(QRectF(rect.right() - 240, rect.top() - 90, 310, 310))
        painter.drawEllipse(QRectF(rect.right() - 80, rect.bottom() - 100, 180, 180))
        decoration.setAlpha(22)
        painter.setPen(QPen(decoration, 1))
        for offset in range(-80, 250, 42):
            painter.drawLine(QPointF(rect.left() + offset, rect.bottom()),
                             QPointF(rect.left() + offset + 180, rect.top()))
        painter.restore()
        white = self.hero_text
        translucent = QColor(self.hero_text)
        translucent.setAlpha(210)
        _text(painter, QRectF(rect.left() + 28, rect.top() + 24, rect.width() * .58, 24),
              self.kicker, size=12, color=translucent, weight=QFont.Weight.Medium)
        _text(painter, QRectF(rect.left() + 28, rect.top() + 67, rect.width() * .60, 104),
              self.title, size=34, color=white, weight=QFont.Weight.Bold,
              align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        _text(painter, QRectF(rect.left() + 28, rect.bottom() - 65, rect.width() * .62, 28),
              f"{self.start.toString('yyyy.MM.dd')} — {self.end.toString('yyyy.MM.dd')}  ·  {self.source}",
              size=13, color=translucent)
        total = int(self.data.get("total", 0))
        _text(painter, QRectF(rect.right() - 265, rect.top() + 83, 220, 76), total,
              size=58, color=white, weight=QFont.Weight.Bold, align=Qt.AlignmentFlag.AlignCenter)
        _text(painter, QRectF(rect.right() - 265, rect.top() + 158, 220, 30), "次点歌",
              size=18, color=translucent, align=Qt.AlignmentFlag.AlignCenter)
        _text(painter, QRectF(rect.right() - 285, rect.top() + 198, 260, 26),
              "共同游玩 " + format_duration(self.total_play_seconds),
              size=12, color=translucent, align=Qt.AlignmentFlag.AlignCenter)

    def _draw_metrics(self, painter, rect):
        peak_month = max(self._trend_for("month"), key=lambda item: item["seconds"], default=None)
        peak_day = max(self.daily_play, key=lambda item: int(item.get("seconds", 0)), default=None)
        if self.summary_mode == "day":
            summary = ("游玩最长的一天",
                       f"{int(peak_day['day'][5:7])} 月 {int(peak_day['day'][8:10])} 日" if peak_day else "—",
                       format_duration(peak_day["seconds"]) if peak_day else "")
        else:
            summary = ("最活跃月份",
                       f"{int(peak_month['period'][5:7])} 月" if peak_month else "—",
                       format_duration(peak_month["seconds"]) if peak_month else "")
        values = [
            ("活跃天数", int(self.data.get("active_days", 0)), "天"),
            ("舞蹈伙伴", len(self.players), "位"),
            ("点播歌曲", self.self_played, "自己点播且已播放"),
            summary,
        ]
        gap = 12
        card_width = (rect.width() - gap * 3) / 4
        for index, (title, value, caption) in enumerate(values):
            card = QRectF(rect.left() + index * (card_width + gap), rect.top(), card_width, rect.height())
            self._card(painter, card)
            _text(painter, QRectF(card.left() + 18, card.top() + 13, card.width() - 36, 22),
                  title, size=12, color=self.muted)
            _text(painter, QRectF(card.left() + 18, card.top() + 38, card.width() - 36, 42),
                  value, size=27, color=self.text, weight=QFont.Weight.DemiBold)
            _text(painter, QRectF(card.left() + 18, card.bottom() - 28, card.width() - 36, 18),
                  caption, size=11, color=self.muted)

    def _draw_favorites(self, painter, rect):
        favorites = self.songs[:3]
        if not favorites:
            return
        gap = 14
        width = (rect.width() - gap * 2) / 3
        for index, song in enumerate(favorites):
            card = QRectF(rect.left() + index * (width + gap), rect.top(), width, rect.height())
            fill = self.soft if index == 0 else self.card
            self._card(painter, card, fill)
            badge = QRectF(card.left() + 18, card.top() + 16, 40, 40)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.accent if index == 0 else self.soft)
            painter.drawEllipse(badge)
            _text(painter, badge, f"0{index + 1}", size=14,
                  color=self.hero_text if index == 0 else self.accent,
                  weight=QFont.Weight.Bold, align=Qt.AlignmentFlag.AlignCenter)
            _text(painter, QRectF(card.right() - 95, card.top() + 20, 77, 30),
                  f"{song.get('count', 0)} 次", size=17, color=self.highlight,
                  weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignRight)
            painter.setFont(_font(18, QFont.Weight.DemiBold))
            painter.setPen(self.text)
            name_rect = QRectF(card.left() + 18, card.top() + 70, card.width() - 36, 58)
            painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                             | Qt.TextFlag.TextWordWrap, song.get("name") or f"编号 {song.get('id')}")
            source = "WannaDance" if song.get("kind") == "wd" else "PyPyDance"
            _text(painter, QRectF(card.left() + 18, card.bottom() - 35, card.width() - 36, 22),
                  f"#{song.get('id')}  ·  {source}", size=11, color=self.muted)

    def _draw_top_songs(self, painter, rect):
        self._card(painter, rect)
        _text(painter, QRectF(rect.left() + 22, rect.top() + 16, rect.width() - 44, 30),
              "点歌 TOP 10", size=19, color=self.text, weight=QFont.Weight.DemiBold)
        songs = self.songs[:10]
        maximum = max((int(song.get("count", 0)) for song in songs), default=1)
        for index, song in enumerate(songs):
            row = QRectF(rect.left() + 22, rect.top() + 61 + index * 44, rect.width() - 44, 34)
            _text(painter, QRectF(row.left(), row.top(), 34, row.height()), f"{index + 1:02d}",
                  size=13, color=self.accent if index < 3 else self.muted, weight=QFont.Weight.DemiBold)
            name_width = row.width() * .32
            painter.setFont(_font(13))
            name = _elided(painter, song.get("name") or f"编号 {song.get('id')}", name_width)
            _text(painter, QRectF(row.left() + 42, row.top(), name_width, row.height()), name,
                  size=13, color=self.text)
            count_width = 62
            bar_x = row.left() + 50 + name_width + 18
            bar_width = max(40, row.width() - (50 + name_width + 18 + count_width + 12))
            bar = QRectF(bar_x, row.top() + 11, bar_width, 12)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.soft)
            painter.drawRoundedRect(bar, 6, 6)
            value_width = bar_width * int(song.get("count", 0)) / maximum
            painter.setBrush(self.accent)
            painter.drawRoundedRect(QRectF(bar_x, bar.top(), max(8, value_width), 12), 6, 6)
            _text(painter, QRectF(row.right() - count_width, row.top(), count_width, row.height()),
                  f"{song.get('count', 0)} 次", size=12, color=self.muted,
                  align=Qt.AlignmentFlag.AlignRight)

    def _draw_trend(self, painter, rect):
        self._card(painter, rect)
        _text(painter, QRectF(rect.left() + 20, rect.top() + 15, rect.width() - 150, 30),
              "舞蹈时长趋势", size=18, color=self.text, weight=QFont.Weight.DemiBold)
        _text(painter, QRectF(rect.right() - 120, rect.top() + 17, 100, 24),
              {"day": "按日", "month": "按月", "year": "按年"}[self.trend_mode],
              size=11, color=self.muted, align=Qt.AlignmentFlag.AlignRight)
        plot = QRectF(rect.left() + 32, rect.top() + 66, rect.width() - 58, 174)
        counts = [max(0, int(item.get("seconds", 0))) for item in self.trend]
        maximum = max(counts, default=1) or 1
        painter.setFont(_font(10))
        for step in range(4):
            y = plot.top() + plot.height() * step / 3
            painter.setPen(QPen(self.border, 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        if not counts:
            _text(painter, plot, "同步 VRCX 后显示游玩时长", color=self.muted,
                  align=Qt.AlignmentFlag.AlignCenter)
            return
        points = []
        for index, count in enumerate(counts):
            x = plot.left() + (plot.width() * index / max(1, len(counts) - 1))
            y = plot.bottom() - plot.height() * count / maximum
            points.append(QPointF(x, y))
        area = QPainterPath(points[0])
        for point in points[1:]:
            area.lineTo(point)
        area.lineTo(QPointF(points[-1].x(), plot.bottom()))
        area.lineTo(QPointF(points[0].x(), plot.bottom()))
        area.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.soft)
        painter.drawPath(area)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self.accent, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        for first, second in zip(points, points[1:]):
            painter.drawLine(first, second)
        painter.setBrush(self.accent)
        painter.setPen(QPen(self.card, 2))
        for point in points:
            painter.drawEllipse(point, 4, 4)
        skip = max(1, math.ceil(len(self.trend) / 6))
        for index, item in enumerate(self.trend):
            if index % skip and index != len(self.trend) - 1:
                continue
            period = str(item["period"])
            text = period[5:] if self.trend_mode == "day" else f"{int(period[5:7])}月" if self.trend_mode == "month" else period + "年"
            _text(painter, QRectF(points[index].x() - 28, plot.bottom() + 10, 56, 20),
                  text, size=10, color=self.muted, align=Qt.AlignmentFlag.AlignCenter)

    def _draw_sources(self, painter, rect):
        self._card(painter, rect)
        _text(painter, QRectF(rect.left() + 20, rect.top() + 15, rect.width() - 40, 30),
              "歌曲来源", size=18, color=self.text, weight=QFont.Weight.DemiBold)
        pypy = sum(int(song.get("count", 0)) for song in self.songs if song.get("kind") != "wd")
        wd = sum(int(song.get("count", 0)) for song in self.songs if song.get("kind") == "wd")
        total = pypy + wd
        center = QPointF(rect.center().x(), rect.top() + (142 if rect.height() >= 280 else 105))
        radius = min(62 if rect.height() >= 280 else 48, rect.width() / 5)
        ring = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self.soft, 18))
        painter.drawEllipse(ring)
        if total:
            start = 90 * 16
            for count, color in ((pypy, self.accent), (wd, self.secondary)):
                if not count:
                    continue
                span = round(-5760 * count / total)
                painter.setPen(QPen(color, 18, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                painter.drawArc(ring, start, span)
                start += span
        _text(painter, QRectF(center.x() - 60, center.y() - 22, 120, 32), total,
              size=25, color=self.text, weight=QFont.Weight.Bold, align=Qt.AlignmentFlag.AlignCenter)
        _text(painter, QRectF(center.x() - 60, center.y() + 10, 120, 20), "次点歌",
              size=11, color=self.muted, align=Qt.AlignmentFlag.AlignCenter)
        legend_y = rect.bottom() - 55
        for index, (name, count, color) in enumerate((("PyPyDance", pypy, self.accent),
                                                     ("WannaDance", wd, self.secondary))):
            x = rect.left() + 24 + index * (rect.width() - 48) / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(x, legend_y + 4, 10, 10))
            _text(painter, QRectF(x + 16, legend_y - 4, (rect.width() - 48) / 2 - 16, 24),
                  f"{name} {count}", size=10, color=self.muted)

    def _draw_vrcx_friend(self, painter, rect):
        self._card(painter, rect)
        friend = self.vrcx_friends[0]
        _text(painter, QRectF(rect.left() + 24, rect.top() + 14, rect.width() * .55, 22),
              "最常与你共同游玩的好友", size=12, color=self.muted)
        painter.setFont(_font(24, QFont.Weight.DemiBold))
        name = _elided(painter, friend.get("name") or "未知好友", rect.width() * .52)
        _text(painter, QRectF(rect.left() + 24, rect.top() + 39, rect.width() * .52, 38),
              name, size=24, color=self.text, weight=QFont.Weight.DemiBold)
        _text(painter, QRectF(rect.left() + 24, rect.bottom() - 30, rect.width() * .6, 20),
              f"累计共同游玩 {friend.get('sessions', 0)} 次 · 来自 VRCX 本地记录", size=12, color=self.muted)
        _text(painter, QRectF(rect.right() - 310, rect.top() + 31, 286, 50),
              format_duration(friend.get("seconds", 0)), size=25, color=self.highlight,
              weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignRight)

class PlayReportDialog(QDialog):
    def __init__(self, owner, data, start, end, source, theme):
        super().__init__(owner)
        self._owner = owner
        self.start, self.end = start, end
        self.setWindowTitle("我的点歌报告")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(1000, 800)
        self.setMinimumSize(760, 600)
        box = QVBoxLayout(self)
        box.setContentsMargins(16, 12, 16, 16)
        self.canvas = ReportCanvas(data, start, end, source, theme)
        header = QHBoxLayout()
        header.addWidget(label("报告标题", muted=True))
        self.title_edit = QLineEdit(self.canvas.title)
        self.title_edit.setMaxLength(60)
        self.title_edit.setAccessibleName("编辑报告标题")
        self.title_edit.textChanged.connect(self.canvas.set_title)
        header.addWidget(self.title_edit, 1)
        self.export_button = button("导出分享图", self._export_png, primary=True)
        header.addWidget(self.export_button)
        box.addLayout(header)
        controls = QHBoxLayout()
        controls.addWidget(label("舞蹈时长趋势", muted=True))
        self.trend_combo = QComboBox()
        for text, value in (("按日", "day"), ("按月", "month"), ("按年", "year")):
            self.trend_combo.addItem(text, value)
        self.trend_combo.currentIndexChanged.connect(lambda index: self.canvas.set_trend_mode(self.trend_combo.itemData(index)))
        controls.addWidget(self.trend_combo)
        controls.addWidget(label("第四项指标", muted=True))
        self.summary_combo = QComboBox()
        for text, value in (("最活跃月份", "month"), ("游玩最长的一天", "day")):
            self.summary_combo.addItem(text, value)
        self.summary_combo.currentIndexChanged.connect(lambda index: self.canvas.set_summary_mode(self.summary_combo.itemData(index)))
        controls.addWidget(self.summary_combo)
        controls.addStretch()
        box.addLayout(controls)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.canvas)
        box.addWidget(scroll, 1)

    def _export_png(self):
        name = f"点歌报告_{self.start.toString('yyyyMMdd')}-{self.end.toString('yyyyMMdd')}.png"
        path, _ = QFileDialog.getSaveFileName(self, "导出点歌报告", name, "PNG 图片 (*.png)")
        if not path:
            return
        if not self.canvas.save_png(path):
            QMessageBox.warning(self, "导出分享图", "图片写入失败，请检查目录权限和剩余空间。")
            return
        QMessageBox.information(self, "导出分享图", f"高清报告已保存到：\n{path}")

    def closeEvent(self, event):
        self._owner._report = None
        super().closeEvent(event)
