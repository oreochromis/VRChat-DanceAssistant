"""以 Qt 原生绘制的舞蹈报告，支持导出高清 PNG 分享图。"""
from __future__ import annotations

import copy
import math
from datetime import datetime

from PySide6.QtCore import QDate, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLineEdit, QMessageBox, QScrollArea, QVBoxLayout, QWidget

from .pages import button
from .vrcx_profile import friend_avatar_path
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
        vrcx = self.data.get("vrcx") or {}
        self.vrcx_friends = list(vrcx.get("friends") or []) if isinstance(vrcx, dict) else []
        self.friend_avatars = {}
        for friend in self.vrcx_friends[:3]:
            user_id = str(friend.get("user_id") or "")
            path = friend_avatar_path(user_id)
            image = QImage(str(path)) if user_id and path.is_file() else QImage()
            if not image.isNull():
                self.friend_avatars[user_id] = image
        self.daily_play = sorted((item for item in (vrcx.get("daily_play") or [])
                                  if isinstance(item, dict) and item.get("day")),
                                 key=lambda item: item["day"]) if isinstance(vrcx, dict) else []
        self.active_days = sum(int(item.get("seconds", 0)) > 180 for item in self.daily_play)
        self.dance_partners = len(self.vrcx_friends)
        self.new_dance_partners = len(vrcx.get("new_friends") or []) if isinstance(vrcx, dict) else 0
        self.total_play_seconds = int(vrcx.get("total_play_seconds", 0)) if isinstance(vrcx, dict) else 0
        self.play_day_count = int(vrcx.get("play_day_count", 0)) if isinstance(vrcx, dict) else 0
        self.average_play_seconds = int(vrcx.get("average_play_seconds", 0)) if isinstance(vrcx, dict) else 0
        self.favorite_hour = vrcx.get("favorite_hour") if isinstance(vrcx, dict) else None
        self.favorite_hour_seconds = int(vrcx.get("favorite_hour_seconds", 0)) if isinstance(vrcx, dict) else 0
        self.data_status = vrcx.get("data_status", "") if isinstance(vrcx, dict) else ""
        world_play = vrcx.get("world_play") if isinstance(vrcx, dict) else {}
        self.world_play = {key: int((world_play or {}).get(key, 0)) for key in ("pypy", "wd", "dudu")}
        self.self_played = int(self.data.get("self_played", 0))
        self.trend_mode, self.summary_mode = "day", "month"
        self.partner_mode = "co_play"
        self.trend = self._trend_for("day")
        self._trend_bars = []
        self._hover_trend = -1
        self.setMouseTracking(True)
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
            self.title = f"我的 {start.year()} 年度舞蹈报告"
            self.kicker = f"{start.year()} · ANNUAL DANCE REPORT"
        else:
            self.title = "我的舞蹈报告"
            self.kicker = "MY DANCE COLLECTION"
        self.setAccessibleName("舞蹈年度报告")
        self.setMinimumWidth(720)
        self.setMinimumHeight(self._report_height(self.width()))

    def set_title(self, text):
        self.title = str(text).strip() or "我的舞蹈报告"
        self.update()

    def set_trend_mode(self, mode):
        if mode not in ("day", "month", "year"):
            return
        self.trend_mode = mode
        self.trend = self._trend_for(mode)
        self._hover_trend = -1
        self.setMinimumHeight(self._report_height(self.width()))
        self.update()

    def set_summary_mode(self, mode):
        if mode in ("month", "day"):
            self.summary_mode = mode
            self.update()

    def set_partner_mode(self, mode):
        if mode in ("co_play", "new"):
            self.partner_mode = mode
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

    def _set_trend_hover(self, index):
        index = index if 0 <= index < len(self.trend) else -1
        if index != self._hover_trend:
            self._hover_trend = index
            self.update()

    def mouseMoveEvent(self, event):
        position = event.position()
        index = next((index for index, bar in enumerate(self._trend_bars)
                      if bar.contains(position)), -1)
        self._set_trend_hover(index)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._set_trend_hover(-1)
        super().leaveEvent(event)

    def resizeEvent(self, event):
        self.setMinimumHeight(self._report_height(self.width()))
        super().resizeEvent(event)

    def _friend_section_height(self):
        count = min(3, len(self.vrcx_friends))
        return 72 + count * 72 + max(0, count - 1) * 8 if count else 0

    def _report_height(self, width):
        height = 862
        if len(self.songs) > 3:
            height += 100 + min(len(self.songs), 10) * 44 + 24
        height += 310
        height += 254
        height += 24
        if self.vrcx_friends:
            height += self._friend_section_height() + 18
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
        self._draw(painter, width, interactive=False)
        painter.end()
        return image.save(path, "PNG")

    def _draw(self, painter, width, interactive=True):
        painter.fillRect(QRectF(0, 0, width, self._report_height(width)), self.background)
        margin, content = 28, width - 56
        hero = QRectF(margin, 28, content, 270)
        self._draw_hero(painter, hero)
        metrics_y = hero.bottom() + 18
        self._draw_metrics(painter, QRectF(margin, metrics_y, content, 270))
        favorite_title_y = metrics_y + 304
        _text(painter, QRectF(margin, favorite_title_y, content, 34), "本期最爱",
              size=23, color=self.text, weight=QFont.Weight.DemiBold)
        self._draw_favorites(painter, QRectF(margin, favorite_title_y + 34, content, 184))
        charts_y = favorite_title_y + 34 + 184 + 24
        if len(self.songs) > 3:
            top_height = 100 + min(len(self.songs), 10) * 44
            self._draw_top_songs(painter, QRectF(margin, charts_y, content, top_height))
            charts_y += top_height + 24
        gap = 18
        trend_width = round((content - gap) * .62)
        self._draw_trend(painter, QRectF(margin, charts_y, trend_width, 310), interactive)
        self._draw_sources(painter, QRectF(margin + trend_width + gap, charts_y,
                                           content - trend_width - gap, 310))
        charts_y += 310
        self._draw_world_play(painter, QRectF(margin, charts_y, content, 230))
        footer_y = charts_y + 254
        if self.vrcx_friends:
            friend_y = charts_y + 254
            friend_height = self._friend_section_height()
            self._draw_vrcx_friend(painter, QRectF(margin, friend_y, content, friend_height))
            footer_y = friend_y + friend_height + 14
        _text(painter, QRectF(margin, footer_y, content, 30),
              "VRChat 跳舞房助手  ·  本地点歌与 VRCX 记录", size=12, color=self.muted,
              align=Qt.AlignmentFlag.AlignCenter)

    def _card(self, painter, rect, color=None):
        painter.setPen(QPen(self.border, 1))
        painter.setBrush(color or self.card)
        painter.drawRoundedRect(rect, 16, 16)

    def _hero_stat(self, painter, rect, value, caption, value_size):
        value, caption = str(value), str(caption)
        caption_size, gap = 16, 10
        size = value_size
        while True:
            painter.setFont(_font(size, QFont.Weight.Bold))
            value_metrics = painter.fontMetrics()
            painter.setFont(_font(caption_size, QFont.Weight.Medium))
            caption_metrics = painter.fontMetrics()
            group_width = (value_metrics.horizontalAdvance(value) + gap
                           + caption_metrics.horizontalAdvance(caption))
            if group_width <= rect.width() or size <= 16:
                break
            size -= 2
        painter.setFont(_font(size, QFont.Weight.Bold))
        value_width = min(value_metrics.horizontalAdvance(value),
                          rect.width() - gap - caption_metrics.horizontalAdvance(caption))
        value = _elided(painter, value, value_width) if value_width < value_metrics.horizontalAdvance(value) else value
        value_width = value_metrics.horizontalAdvance(value)
        group_width = value_width + gap + caption_metrics.horizontalAdvance(caption)
        x = rect.center().x() - group_width / 2
        baseline = rect.center().y() + (value_metrics.ascent() - value_metrics.descent()) / 2
        painter.setPen(self.hero_text)
        painter.drawText(QPointF(x, baseline), value)
        painter.setFont(_font(caption_size, QFont.Weight.Medium))
        painter.setPen(self.hero_text)
        painter.drawText(QPointF(x + value_width + gap, baseline), caption)

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
        translucent = QColor(self.hero_text)
        translucent.setAlpha(210)
        _text(painter, QRectF(rect.left() + 28, rect.top() + 24, rect.width() * .58, 24),
              self.kicker, size=12, color=translucent, weight=QFont.Weight.Medium)
        stats = QRectF(rect.right() - 324, rect.top() + 76, 296, 116)
        title_rect = QRectF(rect.left() + 28, rect.top() + 67,
                            stats.left() - rect.left() - 52, 104)
        title_flags = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                       | Qt.TextFlag.TextWordWrap)
        title_size = 34
        while True:
            painter.setFont(_font(title_size, QFont.Weight.Bold))
            bounds = painter.fontMetrics().boundingRect(title_rect.toRect(), title_flags, self.title)
            if bounds.height() <= title_rect.height() or title_size <= 16:
                break
            title_size -= 2
        painter.setPen(self.hero_text)
        painter.drawText(title_rect, title_flags, self.title)
        _text(painter, QRectF(rect.left() + 28, rect.bottom() - 65, rect.width() * .62, 28),
              f"{self.start.toString('yyyy.MM.dd')} — {self.end.toString('yyyy.MM.dd')}  ·  {self.source}",
              size=13, color=translucent)
        self._hero_stat(painter, QRectF(stats.left(), stats.top(), stats.width(), 58),
                        int(self.data.get("total", 0)), "次点歌", 46)
        self._hero_stat(painter, QRectF(stats.left(), stats.bottom() - 50, stats.width(), 50),
                        format_duration(self.total_play_seconds), "舞蹈时长", 28)

    def _draw_metrics(self, painter, rect):
        peak_month = max(self._trend_for("month"), key=lambda item: item["seconds"], default=None)
        peak_day = max(self.daily_play, key=lambda item: int(item.get("seconds", 0)), default=None)
        if self.summary_mode == "day":
            summary = ("游玩最长的一天",
                       f"{int(peak_day['day'][5:7])} 月 {int(peak_day['day'][8:10])} 日" if peak_day else "—",
                       "", format_duration(peak_day["seconds"]) if peak_day else "")
        else:
            summary = ("最活跃月份", int(peak_month["period"][5:7]) if peak_month else "—",
                       "月" if peak_month else "", format_duration(peak_month["seconds"]) if peak_month else "")
        partner = (("新增的舞蹈伙伴", self.new_dance_partners) if self.partner_mode == "new"
                   else ("共同游玩的舞蹈伙伴", self.dance_partners))
        favorite_hour = (self.favorite_hour if isinstance(self.favorite_hour, int)
                         and not isinstance(self.favorite_hour, bool) and 0 <= self.favorite_hour < 24 else None)
        favorite_period = (f"{favorite_hour:02d}:00–{(favorite_hour + 1) % 24:02d}:00"
                           if favorite_hour is not None else "—")
        values = [
            ("活跃天数", self.active_days, "天", ""),
            (partner[0], partner[1], "位", ""),
            ("点播歌曲", len(self.songs), "首", ""),
            summary,
            ("最常舞蹈时段", favorite_period, "", ""),
            ("单日平均游玩时长", format_duration(self.average_play_seconds) if self.play_day_count else "—", "",
             f"按 {self.play_day_count} 个游玩日计算" if self.play_day_count else "暂无游玩记录"),
        ]
        gap = 12
        card_width = (rect.width() - gap) / 2
        card_height = (rect.height() - gap * 2) / 3
        self._metric_card_rects = []
        for index, (title, value, unit, caption) in enumerate(values):
            column, row = index % 2, index // 2
            card = QRectF(rect.left() + column * (card_width + gap),
                          rect.top() + row * (card_height + gap), card_width, card_height)
            self._metric_card_rects.append(QRectF(card))
            self._card(painter, card)
            title_width = min(142, card.width() * .38)
            title_rect = QRectF(card.left() + 18, card.top() + 8, title_width, card.height() - 16)
            painter.setFont(_font(12))
            _text(painter, title_rect, _elided(painter, title, title_width), size=12, color=self.muted)
            value_left = title_rect.right() + 12
            value_width = card.right() - 18 - value_left
            value_size = 24
            while True:
                painter.setFont(_font(value_size, QFont.Weight.DemiBold))
                value_metrics = painter.fontMetrics()
                painter.setFont(_font(11))
                unit_width = painter.fontMetrics().horizontalAdvance(unit) if unit else 0
                needed = value_metrics.horizontalAdvance(str(value)) + unit_width + (6 if unit else 0)
                if needed <= value_width or value_size <= 14:
                    break
                value_size -= 1
            unit_gap = 6 if unit else 0
            value_rect = QRectF(value_left, card.top() + (12 if caption else 8),
                                value_width - unit_width - unit_gap, 36 if caption else card.height() - 16)
            painter.setFont(_font(value_size, QFont.Weight.DemiBold))
            display_value = _elided(painter, value, value_rect.width())
            _text(painter, value_rect, display_value, size=value_size, color=self.text,
                  weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if unit:
                _text(painter, QRectF(value_rect.right() + unit_gap, value_rect.top(), unit_width, value_rect.height()),
                      unit, size=11, color=self.muted, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if caption:
                _text(painter, QRectF(value_left, card.bottom() - 29, value_width, 18),
                      caption, size=11, color=self.muted,
                      align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

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
            source = "WannaDance" if song.get("kind") == "wd" else "DuDu FitDance" if song.get("kind") == "dudu" else "PyPyDance"
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

    def _draw_trend(self, painter, rect, interactive=True):
        self._card(painter, rect)
        _text(painter, QRectF(rect.left() + 20, rect.top() + 15, rect.width() - 150, 30),
              "舞蹈时长趋势", size=18, color=self.text, weight=QFont.Weight.DemiBold)
        _text(painter, QRectF(rect.right() - 120, rect.top() + 17, 100, 24),
              {"day": "按日", "month": "按月", "year": "按年"}[self.trend_mode],
              size=11, color=self.muted, align=Qt.AlignmentFlag.AlignRight)
        hover = self._hover_trend if interactive else -1
        counts = [max(0, int(item.get("seconds", 0))) for item in self.trend]
        if 0 <= hover < len(self.trend):
            period = str(self.trend[hover]["period"])
            if self.trend_mode == "day":
                summary_label = QDate.fromString(period, "yyyy-MM-dd").toString("yyyy 年 M 月 d 日")
            elif self.trend_mode == "month":
                summary_label = f"{int(period[:4])} 年 {int(period[5:7])} 月"
            else:
                summary_label = period + " 年"
            summary_value = format_duration(counts[hover])
        else:
            summary_label = "本期合计"
            summary_value = format_duration(sum(counts))
        summary_x = rect.left() + rect.width() * .38
        summary_width = rect.width() * .62 - 20
        _text(painter, QRectF(summary_x, rect.top() + 49, summary_width, 20),
              summary_label, size=11, color=self.muted, align=Qt.AlignmentFlag.AlignRight)
        _text(painter, QRectF(summary_x, rect.top() + 67, summary_width, 38),
              summary_value, size=25, color=self.accent if hover >= 0 else self.text,
              weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignRight)
        plot = QRectF(rect.left() + 62, rect.top() + 122, rect.width() - 84, 120)
        maximum_minutes = max(3, math.ceil(max(counts, default=0) / 60))
        maximum = maximum_minutes * 60
        _text(painter, QRectF(rect.left() + 14, plot.top() - 23, 42, 18), "分钟",
              size=10, color=self.muted, align=Qt.AlignmentFlag.AlignRight)
        for step in range(4):
            y = plot.top() + plot.height() * step / 3
            painter.setPen(QPen(self.border, 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            minutes = round(maximum_minutes * (3 - step) / 3)
            _text(painter, QRectF(rect.left() + 14, y - 10, 38, 20), minutes,
                  size=10, color=self.muted, align=Qt.AlignmentFlag.AlignRight)
        if not counts:
            if interactive:
                self._trend_bars = []
            empty_text = {"no_records": "暂无位置记录", "partial": "记录不完整，仅显示可还原时长"}.get(
                self.data_status, "同步 VRCX 后显示游玩时长")
            _text(painter, plot, empty_text, color=self.muted,
                  align=Qt.AlignmentFlag.AlignCenter)
            return
        slot = plot.width() / len(counts)
        bar_width = min(26, max(5, slot * .46))
        centers = [plot.left() + slot * (index + .5) for index in range(len(counts))]
        if 0 <= hover < len(counts):
            painter.setPen(QPen(self.soft, 1))
            painter.drawLine(QPointF(centers[hover], plot.top()),
                             QPointF(centers[hover], plot.bottom()))
        for index, (count, center) in enumerate(zip(counts, centers)):
            height = plot.height() * count / maximum
            if height > 0:
                bar = QRectF(center - bar_width / 2, plot.bottom() - height, bar_width, height)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(self.secondary if index == hover else self.accent)
                radius = min(5, bar_width / 2)
                painter.drawRoundedRect(bar, radius, radius)
        if interactive:
            self._trend_bars = [QRectF(plot.left() + slot * index, plot.top(), slot, plot.height())
                                for index in range(len(counts))]
        skip = max(1, math.ceil(len(self.trend) / 5))
        for index, item in enumerate(self.trend):
            if index % skip and index != len(self.trend) - 1:
                continue
            period = str(item["period"])
            text = period[5:] if self.trend_mode == "day" else f"{int(period[5:7])} 月" if self.trend_mode == "month" else period + " 年"
            _text(painter, QRectF(centers[index] - 32, plot.bottom() + 10, 64, 20),
                  text, size=10, color=self.accent if index == hover else self.muted,
                  weight=QFont.Weight.DemiBold if index == hover else QFont.Weight.Normal,
                  align=Qt.AlignmentFlag.AlignCenter)

    def _draw_legend(self, painter, rect, items, y, size):
        painter.setFont(_font(size))
        metrics = painter.fontMetrics()
        widths = [16 + metrics.horizontalAdvance(text) for text, _ in items]
        gap = 28
        x = rect.center().x() - (sum(widths) + gap * (len(items) - 1)) / 2
        for (text, color), width in zip(items, widths):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(x, y + 4, 10, 10))
            _text(painter, QRectF(x + 16, y - 4, width - 16, 24),
                  text, size=size, color=self.muted)
            x += width + gap

    def _draw_sources(self, painter, rect):
        self._card(painter, rect)
        _text(painter, QRectF(rect.left() + 20, rect.top() + 15, rect.width() - 40, 30),
              "歌曲来源", size=18, color=self.text, weight=QFont.Weight.DemiBold)
        pypy = sum(int(song.get("count", 0)) for song in self.songs if song.get("kind") == "pypy")
        wd = sum(int(song.get("count", 0)) for song in self.songs if song.get("kind") == "wd")
        dudu = sum(int(song.get("count", 0)) for song in self.songs if song.get("kind") == "dudu")
        total = pypy + wd + dudu
        center = QPointF(rect.center().x(), rect.top() + (142 if rect.height() >= 280 else 105))
        radius = min(62 if rect.height() >= 280 else 48, rect.width() / 5)
        ring = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self.soft, 18))
        painter.drawEllipse(ring)
        if total:
            start = 90 * 16
            for count, color in ((pypy, self.accent), (wd, self.secondary), (dudu, self.highlight)):
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
        self._draw_legend(painter, rect, ((f"PyPyDance {pypy}", self.accent),
                                          (f"WannaDance {wd}", self.secondary),
                                          (f"DuDu {dudu}", self.highlight)),
                          rect.bottom() - 55, 10)

    def _draw_world_play(self, painter, rect):
        self._card(painter, rect)
        _text(painter, QRectF(rect.left() + 20, rect.top() + 15, rect.width() - 40, 30),
              "游玩时长分布", size=18, color=self.text, weight=QFont.Weight.DemiBold)
        pypy, wd, dudu = self.world_play["pypy"], self.world_play["wd"], self.world_play["dudu"]
        total = pypy + wd + dudu
        center = QPointF(rect.center().x(), rect.top() + 105)
        radius = 48
        ring = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self.soft, 18))
        painter.drawEllipse(ring)
        if total:
            start = 90 * 16
            for count, color in ((pypy, self.accent), (wd, self.secondary), (dudu, self.highlight)):
                if not count:
                    continue
                span = round(-5760 * count / total)
                painter.setPen(QPen(color, 18, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                painter.drawArc(ring, start, span)
                start += span
        if total:
            duration_parts = format_duration(total).split(" 小时 ")
            if len(duration_parts) == 2:
                _text(painter, QRectF(center.x() - 55, center.y() - 30, 110, 20),
                      duration_parts[0] + " 小时", size=13, color=self.text,
                      weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignCenter)
                _text(painter, QRectF(center.x() - 55, center.y() - 10, 110, 20),
                      duration_parts[1], size=13, color=self.text,
                      weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignCenter)
            else:
                _text(painter, QRectF(center.x() - 55, center.y() - 23, 110, 26),
                      duration_parts[0], size=15, color=self.text,
                      weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignCenter)
            _text(painter, QRectF(center.x() - 55, center.y() + 13, 110, 17), "总时长",
                  size=10, color=self.muted, align=Qt.AlignmentFlag.AlignCenter)
        else:
            _text(painter, QRectF(center.x() - 60, center.y() - 16, 120, 32), "暂无数据",
                  size=13, color=self.muted, weight=QFont.Weight.Medium,
                  align=Qt.AlignmentFlag.AlignCenter)
        self._draw_legend(painter, rect,
                          ((f"PyPyDance · {format_duration(pypy)}", self.accent),
                           (f"WannaDance · {format_duration(wd)}", self.secondary),
                           (f"DuDu · {format_duration(dudu)}", self.highlight)),
                          rect.bottom() - 46, 11)

    def _draw_vrcx_friend(self, painter, rect):
        self._card(painter, rect)
        friends = self.vrcx_friends[:3]
        _text(painter, QRectF(rect.left() + 24, rect.top() + 12, rect.width() - 48, 28),
              "最常与你共同游玩的好友 · TOP 3", size=16, color=self.text, weight=QFont.Weight.DemiBold)
        self._friend_row_rects = []
        for index, friend in enumerate(friends):
            row = QRectF(rect.left() + 20, rect.top() + 56 + index * 80, rect.width() - 40, 72)
            self._friend_row_rects.append(QRectF(row))
            if index:
                painter.setPen(QPen(self.border, 1))
                painter.drawLine(QPointF(row.left() + 4, row.top() - 4),
                                 QPointF(row.right() - 4, row.top() - 4))
            _text(painter, QRectF(row.left(), row.top(), 32, row.height()), f"{index + 1:02d}",
                  size=18, color=self.accent if index == 0 else self.muted,
                  weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignCenter)
            avatar = QRectF(row.left() + 42, row.center().y() - 28, 56, 56)
            image = self.friend_avatars.get(str(friend.get("user_id") or ""))
            painter.save()
            path = QPainterPath()
            path.addEllipse(avatar)
            painter.setClipPath(path)
            if image is not None:
                size = max(1, round(avatar.width() * 2))
                scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation)
                side = min(scaled.width(), scaled.height())
                source = QRectF((scaled.width() - side) / 2, (scaled.height() - side) / 2, side, side)
                painter.drawImage(avatar, scaled, source)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(self.soft)
                painter.drawEllipse(avatar)
                _text(painter, avatar, str(index + 1), size=20, color=self.accent,
                      weight=QFont.Weight.DemiBold, align=Qt.AlignmentFlag.AlignCenter)
            painter.restore()
            painter.setPen(QPen(self.border, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(avatar)
            stats_width = min(340, row.width() * .47)
            stats = QRectF(row.right() - stats_width, row.top(), stats_width, row.height())
            name_area = QRectF(avatar.right() + 14, row.top(), stats.left() - avatar.right() - 30, row.height())
            painter.setFont(_font(18, QFont.Weight.DemiBold))
            name = _elided(painter, friend.get("name") or "未知好友", name_area.width())
            _text(painter, name_area, name, size=18, color=self.text, weight=QFont.Weight.DemiBold)
            for column, (value, caption) in enumerate((
                    (f"{friend.get('days', friend.get('sessions', 0))} 天", "共同游玩天数"),
                    (format_duration(friend.get("seconds", 0)), "共同游玩时长"))):
                left = stats.left() + (stats.width() * .36 if column else 0)
                width = stats.width() * (.64 if column else .36)
                _text(painter, QRectF(left, stats.top() + 8, width, 30), value,
                      size=17, color=self.text, weight=QFont.Weight.DemiBold,
                      align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                _text(painter, QRectF(left, stats.top() + 42, width, 18), caption,
                      size=10, color=self.muted, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

class PlayReportDialog(QDialog):
    def __init__(self, owner, data, start, end, source, theme):
        super().__init__(owner)
        self._owner = owner
        self.start, self.end = start, end
        self.theme = theme
        self.setWindowTitle("我的舞蹈报告")
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
        controls.addWidget(label("第二项指标", muted=True))
        self.partner_combo = QComboBox()
        for text, value in (("共同游玩的舞蹈伙伴", "co_play"), ("新增的舞蹈伙伴", "new")):
            self.partner_combo.addItem(text, value)
        self.partner_combo.currentIndexChanged.connect(lambda index: self.canvas.set_partner_mode(self.partner_combo.itemData(index)))
        controls.addWidget(self.partner_combo)
        controls.addWidget(label("第四项指标", muted=True))
        self.summary_combo = QComboBox()
        for text, value in (("最活跃月份", "month"), ("游玩最长的一天", "day")):
            self.summary_combo.addItem(text, value)
        self.summary_combo.currentIndexChanged.connect(lambda index: self.canvas.set_summary_mode(self.summary_combo.itemData(index)))
        controls.addWidget(self.summary_combo)
        controls.addStretch()
        box.addLayout(controls)
        self.updated_label = label("", muted=True)
        box.addWidget(self.updated_label)
        self._update_timestamp(data)
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.canvas)
        box.addWidget(self.scroll, 1)

    def update_data(self, data):
        previous = self.canvas
        title = previous.title
        trend_mode = previous.trend_mode
        summary_mode = previous.summary_mode
        partner_mode = previous.partner_mode
        scroll_value = self.scroll.verticalScrollBar().value()
        self.title_edit.textChanged.disconnect(previous.set_title)
        canvas = ReportCanvas(data, self.start, self.end, previous.source, self.theme)
        canvas.set_title(title)
        canvas.set_trend_mode(trend_mode)
        canvas.set_summary_mode(summary_mode)
        canvas.set_partner_mode(partner_mode)
        self.canvas = canvas
        self.title_edit.textChanged.connect(self.canvas.set_title)
        self.scroll.setWidget(self.canvas)
        self.scroll.verticalScrollBar().setValue(scroll_value)
        try:
            previous.deleteLater()
        except RuntimeError:
            pass
        self._update_timestamp(data)

    def _update_timestamp(self, data):
        try:
            timestamp = float(data.get("updated_at") or 0)
        except (TypeError, ValueError):
            timestamp = 0
        text = ("数据最后更新于 " + datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
                if timestamp > 0 else "数据最后更新时间未知")
        status = (data.get("vrcx") or {}).get("data_status")
        if status == "no_records":
            text += " · 暂无位置记录"
        elif status == "partial":
            text += " · 记录不完整"
        self.updated_label.setText(text)

    def _export_png(self):
        name = f"舞蹈报告_{self.start.toString('yyyyMMdd')}-{self.end.toString('yyyyMMdd')}.png"
        path, _ = QFileDialog.getSaveFileName(self, "导出舞蹈报告", name, "PNG 图片 (*.png)")
        if not path:
            return
        if not self.canvas.save_png(path):
            QMessageBox.warning(self, "导出分享图", "图片写入失败，请检查目录权限和剩余空间。")
            return
        QMessageBox.information(self, "导出分享图", f"高清报告已保存到：\n{path}")

    def closeEvent(self, event):
        self._owner._report = None
        super().closeEvent(event)
