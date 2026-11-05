"""Theme persistence without a GUI dependency.

ThemeStore deliberately owns only appearance data.  It never touches the cache
service's configuration, hosts file, or any source image selected by the user.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import struct
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Mapping


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_EDGE = 16_384
HISTORY_LIMIT = 50

COLOR_FIELDS = frozenset({
    "accent", "secondary_accent", "highlight", "background_color", "background_gradient",
    "hero_gradient", "overlay_color", "nav_background", "nav_text",
    "nav_active_background", "nav_active_text", "text_primary", "text_secondary",
    "card_background", "border_color", "text_shadow_color", "success_color",
    "warning_color", "error_color", "info_color", "button_background", "button_text",
    "button_hover_background", "primary_button_text", "input_background",
    "table_header_background", "table_header_text", "table_selection_background",
    "scrollbar_color",
})
NUMBER_RANGES = {
    "background_opacity": (0.0, 1.0),
    "background_gradient_opacity": (0.0, 1.0),
    "hero_gradient_opacity": (0.0, 1.0),
    "image_opacity": (0.0, 1.0),
    "image_blur": (0.0, 30.0),
    "overlay_opacity": (0.0, 1.0),
    "nav_opacity": (0.0, 1.0),
    "nav_active_opacity": (0.0, 1.0),
    "card_opacity": (0.0, 1.0),
    "border_opacity": (0.0, 1.0),
    "border_width": (0.0, 6.0),
    "radius": (0.0, 24.0),
    "text_shadow_opacity": (0.0, 1.0),
    "text_shadow_size": (0.0, 6.0),
    "text_shadow_blur": (0.0, 20.0),
}
IMAGE_FITS = ("cover", "contain", "stretch", "center", "tile")
GRADIENT_DIRECTIONS = ("diagonal", "horizontal", "vertical")
THEME_FIELDS = COLOR_FIELDS | frozenset(NUMBER_RANGES) | {
    "background_image", "image_fit", "gradient_direction",
}
DEFAULT_PREFERENCES = {"scale": 100, "density": "standard", "logs_expanded": False,
                       "dismissed_update_version": "", "ui_mode": "simple", "home_style": "simple"}

_COMMON = {
    "background_image": "", "background_opacity": 1.0,
    "background_gradient": "#E9ECFF", "background_gradient_opacity": 0.0,
    "hero_gradient": "#39B8C6", "hero_gradient_opacity": 0.0,
    "gradient_direction": "diagonal",
    "image_fit": "cover", "image_opacity": 0.35, "image_blur": 0.0,
    "overlay_opacity": 0.0, "border_width": 1.0, "radius": 12.0,
    "text_shadow_color": "#000000", "text_shadow_opacity": 0.0,
    "text_shadow_size": 1.0, "text_shadow_blur": 4.0,
    "secondary_accent": "#39B8C6", "highlight": "#A56B12",
    "success_color": "#1B6B3F", "warning_color": "#83520F",
    "error_color": "#A93649", "info_color": "#285EBB",
    "button_background": "#FFFFFF", "button_text": "#172133",
    "button_hover_background": "#EDF0FF", "primary_button_text": "#FFFFFF",
    "input_background": "#FFFFFF", "table_header_background": "#F5F6FA",
    "table_header_text": "#526172", "table_selection_background": "#E7E9FA",
    "scrollbar_color": "#9AA6B8",
}
BUILTIN_THEMES = {
    "light": {
        **_COMMON, "id": "light", "name": "浅色", "builtin": True, "base": "light",
        "accent": "#5865D8", "background_color": "#F3F5F9", "overlay_color": "#F3F5F9",
        "nav_background": "#FFFFFF", "nav_opacity": 0.82, "nav_text": "#657189",
        "nav_active_background": "#5865D8", "nav_active_opacity": 0.13,
        "nav_active_text": "#414FB8", "text_primary": "#172133",
        "text_secondary": "#58667D", "card_background": "#FFFFFF",
        "card_opacity": 0.94, "border_color": "#DDE2EB", "border_opacity": 0.8,
    },
    "dark": {
        **_COMMON, "id": "dark", "name": "深色", "builtin": True, "base": "dark",
        "accent": "#95A3FF", "background_color": "#0F1420", "overlay_color": "#0F1420",
        "nav_background": "#141A28", "nav_opacity": 0.9, "nav_text": "#ADB7CB",
        "nav_active_background": "#95A3FF", "nav_active_opacity": 0.18,
        "nav_active_text": "#D6DCFF", "text_primary": "#EDF1F8",
        "text_secondary": "#A6B1C7", "card_background": "#1A2130",
        "card_opacity": 0.96, "border_color": "#344052", "border_opacity": 0.65,
        "secondary_accent": "#6CD5DF", "highlight": "#E7BF78",
        "success_color": "#7CDBA5", "warning_color": "#E7BF78",
        "error_color": "#F3A0AD", "info_color": "#9CBDFF",
        "button_background": "#242D40", "button_text": "#EDF1F8",
        "button_hover_background": "#303B55", "primary_button_text": "#182132",
        "input_background": "#202839", "table_header_background": "#202839",
        "table_header_text": "#A6B1C7", "table_selection_background": "#303B60",
        "scrollbar_color": "#6E7A90", "background_gradient": "#19243A",
        "hero_gradient": "#6CD5DF",
    },
}
THEME_GROUPS = {
    "background": (
        "background_color", "background_opacity", "background_image", "image_fit",
        "image_opacity", "image_blur", "overlay_color", "overlay_opacity",
    ),
    "colors": ("accent", "text_primary", "text_secondary"),
    "semantic": ("secondary_accent", "highlight", "success_color", "warning_color", "error_color", "info_color"),
    "gradients": ("background_gradient", "background_gradient_opacity", "hero_gradient",
                  "hero_gradient_opacity", "gradient_direction"),
    "navigation": (
        "nav_background", "nav_opacity", "nav_text", "nav_active_background",
        "nav_active_opacity", "nav_active_text",
    ),
    "cards": ("card_background", "card_opacity", "border_color", "border_opacity", "border_width", "radius"),
    "buttons": ("button_background", "button_text", "button_hover_background", "primary_button_text",
                "input_background"),
    "tables": ("table_header_background", "table_header_text", "table_selection_background", "scrollbar_color"),
    "text": ("text_shadow_color", "text_shadow_opacity", "text_shadow_size", "text_shadow_blur"),
}


class ThemeError(ValueError):
    """A user-facing invalid appearance value or image."""


def normalize_color(value: Any) -> str:
    """Accept RGB/RRGGBB hex input and return an uppercase #RRGGBB value."""
    if not isinstance(value, str):
        raise ThemeError("颜色需要填写 HEX 色值，例如 #5865D8。")
    result = value.strip().lstrip("#")
    if len(result) == 3:
        result = "".join(c * 2 for c in result)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", result):
        raise ThemeError("颜色需要填写 #RGB 或 #RRGGBB 格式的 HEX 色值。")
    return "#" + result.upper()


def contrast_ratio(first: str, second: str) -> float:
    def luminance(value):
        color = normalize_color(value).lstrip("#")
        channels = [int(color[index:index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
                  for channel in channels]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + .05) / (low + .05)


def readable_on(background: str) -> str:
    return ("#182132" if contrast_ratio("#182132", background) >= contrast_ratio("#FFFFFF", background)
            else "#FFFFFF")


def _blend_colors(foreground: str, background: str, opacity: float) -> str:
    def rgb(value):
        value = normalize_color(value).lstrip("#")
        return [int(value[index:index + 2], 16) for index in (0, 2, 4)]
    first, second = rgb(foreground), rgb(background)
    opacity = min(1.0, max(0.0, float(opacity)))
    return "#" + "".join(f"{round(a * opacity + b * (1 - opacity)):02X}"
                         for a, b in zip(first, second))


def theme_contrast_issues(theme: Mapping[str, Any], preferences: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    background = theme["background_color"]
    card = theme["card_background"]
    gradient = _blend_colors(theme["background_gradient"], background, theme["background_gradient_opacity"])
    nav = _blend_colors(theme["nav_background"], background, theme["nav_opacity"])
    nav_active = _blend_colors(theme["nav_active_background"], nav, theme["nav_active_opacity"])
    dark = readable_on(background) == "#FFFFFF"
    state_color = "#FFFFFF" if dark else "#182B49"
    switch_track = _blend_colors(state_color, nav, 18 / 255 if dark else 10 / 255)
    disabled = _blend_colors(state_color, theme["button_background"], 12 / 255 if dark else 7 / 255)
    disabled_input = _blend_colors(state_color, theme["input_background"], 12 / 255 if dark else 7 / 255)
    primary_disabled = _blend_colors(theme["accent"], theme["button_background"], .30)
    pressed = _blend_colors(theme["accent"], theme["button_background"], .20)
    issues = []

    def check(label, foreground, surface, threshold=4.5):
        color = theme.get(foreground, foreground)
        if contrast_ratio(color, surface) < threshold:
            issues.append({"label": label + "与背景对比度偏低", "key": foreground if foreground in theme else None,
                           "foreground": color, "background": surface, "threshold": threshold})

    for label, foreground in (("主要文字", "text_primary"), ("辅助文字", "text_secondary")):
        check(label + "（卡片）", foreground, card)
        check(label + "（页面）", foreground, background)
        check(label + "（渐变页面）", foreground, gradient)
    check("输入框文字", "text_primary", theme["input_background"])
    check("导航文字", "nav_text", nav)
    check("导航选中项文字", "nav_active_text", nav_active)
    check("模式拨杆未选中文字", "text_secondary", switch_track)
    check("模式拨杆选中文字", readable_on(theme["accent"]), theme["accent"])
    check("页脚工具文字", "text_secondary", nav)
    check("普通按钮文字", "button_text", theme["button_background"])
    check("悬停按钮文字", "button_text", theme["button_hover_background"])
    check("按下按钮文字", "button_text", pressed)
    check("禁用按钮文字", "text_secondary", disabled, 3.0)
    check("禁用输入控件文字", "text_secondary", disabled_input, 3.0)
    check("禁用主按钮文字", "text_secondary", primary_disabled, 3.0)
    check("主要按钮文字", "primary_button_text", theme["accent"])
    check("复选框勾选标记", readable_on(theme["accent"]), theme["accent"], 3.0)
    check("日历选中日期", readable_on(theme["accent"]), theme["accent"])
    check("下拉框箭头", readable_on(theme["input_background"]), theme["input_background"], 3.0)
    check("表头文字", "table_header_text", theme["table_header_background"])
    check("表格选中行文字", "text_primary", theme["table_selection_background"])
    check("排名与高亮数字", "highlight", card, 3.0)
    check("排名编号", "accent", card)
    for label, key in (("成功状态", "success_color"), ("警告状态", "warning_color"),
                       ("错误状态", "error_color"), ("信息状态", "info_color")):
        check(label + "标签", key, _blend_colors(theme[key], card, .10))
        check("运行" + label, key, nav)
    for label, key, opacity in (("错误", "error_color", .10), ("成功", "success_color", .08),
                                ("信息", "accent", .08)):
        surface = _blend_colors(theme[key], background, opacity)
        check(label + "通知文字", "text_primary", surface)
        check(label + "通知详情", "text_secondary", surface)
    hero_backgrounds = [theme["accent"]]
    if theme["hero_gradient_opacity"] > 0 or (preferences or {}).get("home_style") == "vivid":
        hero_backgrounds.extend((theme["secondary_accent"], theme["hero_gradient"]))
    for surface in hero_backgrounds:
        check("主视觉文字", "primary_button_text", surface, 3.0)
    return issues


def theme_contrast_warnings(theme: Mapping[str, Any], preferences: Mapping[str, Any] | None = None) -> list[str]:
    return [issue["label"] for issue in theme_contrast_issues(theme, preferences)]


def _relative_luminance(value: str) -> float:
    color = normalize_color(value).lstrip("#")
    channels = [int(color[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
              for channel in channels]
    return sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))


def _move_to_luminance(color: str, low: float, high: float) -> str:
    luminance = _relative_luminance(color)
    if low <= luminance <= high:
        return color
    target = "#FFFFFF" if luminance < low else "#000000"
    candidate = color
    for step in range(1, 41):
        candidate = _blend_colors(target, color, step / 40)
        if low <= _relative_luminance(candidate) <= high:
            return candidate
    return candidate


def _ensure_foreground(color: str, surfaces: list[str], threshold: float) -> str:
    def minimum(candidate):
        return min(contrast_ratio(candidate, surface) for surface in surfaces)
    if minimum(color) >= threshold:
        return color
    target = "#000000" if minimum("#000000") >= minimum("#FFFFFF") else "#FFFFFF"
    best, best_ratio = color, minimum(color)
    for step in range(1, 101):
        candidate = _blend_colors(target, color, step / 100)
        ratio = minimum(candidate)
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
        if ratio >= threshold:
            return candidate
    return best


def ensure_theme_contrast(theme: Mapping[str, Any], preferences: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = copy.deepcopy(dict(theme))
    light = result.get("base", "light") == "light"
    surface_fields = (
        "background_color", "background_gradient", "nav_background",
        "card_background", "button_background", "button_hover_background", "input_background",
        "table_header_background", "table_selection_background",
    )
    for key in surface_fields:
        result[key] = _move_to_luminance(result[key], .65, 1.0) if light else _move_to_luminance(result[key], 0.0, .10)
    result["accent"] = _ensure_foreground(result["accent"], [result["card_background"]], 4.5)
    hero_active = result["hero_gradient_opacity"] > 0 or (preferences or {}).get("home_style") == "vivid"
    luminance = _relative_luminance(result["accent"])
    if .16 < luminance < .30 and contrast_ratio(readable_on(result["accent"]), result["accent"]) < 4.5:
        result["accent"] = _move_to_luminance(result["accent"], .30, 1.0) if luminance >= .23 else _move_to_luminance(result["accent"], 0.0, .16)
    if hero_active:
        bright_accent = _relative_luminance(result["accent"]) >= .30
        for key in ("secondary_accent", "hero_gradient"):
            result[key] = (_move_to_luminance(result[key], .30, 1.0) if bright_accent
                           else _move_to_luminance(result[key], 0.0, .16))
    for _ in range(8):
        issues = theme_contrast_issues(result, preferences)
        if not issues:
            break
        groups = {}
        for issue in issues:
            if issue["key"]:
                groups.setdefault(issue["key"], []).append(issue)
        if not groups:
            break
        for key, group in groups.items():
            result[key] = _ensure_foreground(result[key], [issue["background"] for issue in group],
                                             max(issue["threshold"] for issue in group))
    for _ in range(4):
        remaining = theme_contrast_issues(result, preferences)
        if not remaining:
            break
        for key in {issue["key"] for issue in remaining if issue["key"]}:
            result[key] = BUILTIN_THEMES[result.get("base", "light")][key]
    luminance = _relative_luminance(result["accent"])
    if .16 < luminance < .35 and contrast_ratio(readable_on(result["accent"]), result["accent"]) < 4.5:
        result["accent"] = (_move_to_luminance(result["accent"], .35, 1.0) if luminance >= .25
                            else _move_to_luminance(result["accent"], 0.0, .16))
    luminance = _relative_luminance(result["accent"])
    if .16 < luminance < .35 and contrast_ratio(readable_on(result["accent"]), result["accent"]) < 4.5:
        result["accent"] = (_move_to_luminance(result["accent"], .35, 1.0) if luminance >= .25
                            else _move_to_luminance(result["accent"], 0.0, .16))
    result["accent"] = _ensure_foreground(result["accent"], [result["card_background"]], 4.5)
    hero_surfaces = [result["accent"]]
    if hero_active:
        hero_surfaces.extend((result["secondary_accent"], result["hero_gradient"]))
    result["primary_button_text"] = _ensure_foreground(
        readable_on(result["accent"]), hero_surfaces, 4.5)
    for _ in range(3):
        remaining = theme_contrast_issues(result, preferences)
        if not remaining:
            break
        for key in {issue["key"] for issue in remaining if issue["key"]}:
            issues = [issue for issue in remaining if issue["key"] == key]
            result[key] = _ensure_foreground(result[key], [issue["background"] for issue in issues],
                                             max(issue["threshold"] for issue in issues))
    return result


def _number(value: Any, key: str) -> float:
    if isinstance(value, bool):
        raise ThemeError("数值设置不能使用开关值。")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ThemeError("请输入有效的数值。") from None
    if not math.isfinite(result):
        raise ThemeError("数值必须是有限数字。")
    low, high = NUMBER_RANGES[key]
    return round(min(high, max(low, result)), 3)


def _image_reference(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ThemeError("背景图片路径无效。")
    result = value.replace("\\", "/")
    if not re.fullmatch(r"backgrounds/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|webp)", result, re.IGNORECASE):
        raise ThemeError("请使用“导入图片”选择背景，图片会保存在应用的外观目录。")
    return result


def _theme_value(key: str, value: Any) -> Any:
    if key in COLOR_FIELDS:
        return normalize_color(value)
    if key in NUMBER_RANGES:
        return _number(value, key)
    if key == "image_fit":
        if value not in IMAGE_FITS:
            raise ThemeError("图片适应方式无效。")
        return value
    if key == "background_image":
        return _image_reference(value)
    if key == "gradient_direction":
        if value not in GRADIENT_DIRECTIONS:
            raise ThemeError("渐变方向无效。")
        return value
    raise ThemeError("未识别的主题设置：" + str(key))


def _preference_value(key: str, value: Any) -> Any:
    if key == "scale":
        if isinstance(value, bool):
            raise ThemeError("界面缩放请选择 100%、110%、125% 或 150%。")
        try:
            scale = int(value)
        except (TypeError, ValueError, OverflowError):
            raise ThemeError("界面缩放请选择 100%、110%、125% 或 150%。") from None
        if scale not in (100, 110, 125, 150) or str(value).strip() not in (str(scale), str(float(scale))):
            raise ThemeError("界面缩放请选择 100%、110%、125% 或 150%。")
        return scale
    if key == "density":
        if value not in ("comfortable", "standard", "compact"):
            raise ThemeError("信息密度请选择舒适、标准或紧凑。")
        return value
    if key == "logs_expanded":
        if not isinstance(value, bool):
            raise ThemeError("日志默认展开必须是开关值。")
        return value
    if key == "dismissed_update_version":
        if not isinstance(value, str):
            raise ThemeError("不再提醒的版本号必须是文本。")
        return value.strip()
    if key == "ui_mode":
        if value not in ("simple", "advanced"):
            raise ThemeError("界面模式请选择简单或高级。")
        return value
    if key == "home_style":
        if value not in ("simple", "vivid"):
            raise ThemeError("主页风格请选择简洁或炫酷。")
        return value
    raise ThemeError("未识别的阅读偏好：" + str(key))


def _image_info(data: bytes) -> tuple[str, int, int]:
    """Inspect PNG/JPEG/WebP containers without importing Qt or an image library."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        position, dimensions, saw_data, finished = 8, None, False, False
        while position + 12 <= len(data):
            length = struct.unpack_from(">I", data, position)[0]
            kind = data[position + 4:position + 8]
            end = position + 12 + length
            if end > len(data):
                raise ThemeError("PNG 图片不完整，请重新选择图片。")
            payload = data[position + 8:position + 8 + length]
            expected = struct.unpack_from(">I", data, position + 8 + length)[0]
            if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected:
                raise ThemeError("PNG 图片校验失败，请重新选择图片。")
            if dimensions is None:
                if kind != b"IHDR" or length != 13:
                    raise ThemeError("PNG 图片头无效。")
                dimensions = struct.unpack_from(">II", payload)
            if kind == b"IDAT":
                saw_data = saw_data or bool(length)
            if kind == b"IEND":
                finished = length == 0
                break
            position = end
        if dimensions and saw_data and finished:
            return ("png", *dimensions)
        raise ThemeError("PNG 图片不完整，请重新选择图片。")
    if data.startswith(b"\xff\xd8"):
        position = 2
        frame_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        dimensions = None
        while position < len(data):
            if data[position] != 0xFF:
                break
            while position < len(data) and data[position] == 0xFF:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker in (0xD9, 0xDA):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(data):
                break
            length = struct.unpack_from(">H", data, position)[0]
            if length < 2 or position + length > len(data):
                break
            if marker in frame_markers and length >= 8:
                height, width = struct.unpack_from(">HH", data, position + 3)
                dimensions = (width, height)
            position += length
        if dimensions and b"\xff\xda" in data and data.rstrip().endswith(b"\xff\xd9"):
            return ("jpg", *dimensions)
        raise ThemeError("JPEG 图片格式无效或文件不完整。")
    if len(data) >= 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        declared = struct.unpack_from("<I", data, 4)[0] + 8
        if declared != len(data):
            raise ThemeError("WEBP 图片不完整，请重新选择图片。")
        position, dimensions = 12, None
        while position + 8 <= len(data):
            kind = data[position:position + 4]
            length = struct.unpack_from("<I", data, position + 4)[0]
            start, end = position + 8, position + 8 + length
            if end > len(data):
                raise ThemeError("WEBP 图片不完整。")
            payload = data[start:end]
            if kind == b"VP8X" and length >= 10:
                dimensions = (1 + int.from_bytes(payload[4:7], "little"), 1 + int.from_bytes(payload[7:10], "little"))
            elif kind == b"VP8L" and length >= 5 and payload[0] == 0x2F:
                bits = int.from_bytes(payload[1:5], "little")
                dimensions = (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
            elif kind == b"VP8 " and length >= 10 and payload[3:6] == b"\x9d\x01\x2a":
                width, height = struct.unpack_from("<HH", payload, 6)
                dimensions = (width & 0x3FFF, height & 0x3FFF)
            position = end + (length % 2)
        if dimensions:
            return ("webp", *dimensions)
        raise ThemeError("无法识别此 WEBP 图片。")
    raise ThemeError("仅支持有效的 PNG、JPG 和 WEBP 图片。")


class ThemeStore:
    """Editable themes, independent preferences, atomic writes, and session undo."""

    def __init__(self, root_dir: str | os.PathLike[str]):
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.path = self.root_dir / "appearance.json"
        self.background_dir = self.root_dir / "backgrounds"
        self.warnings: list[str] = []
        self._history: list[dict[str, Any]] = []
        self._editing = False
        self._edit_recorded = False
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.background_dir.mkdir(parents=True, exist_ok=True)
        self._provided_themes = copy.deepcopy(BUILTIN_THEMES)
        self._state = self._defaults()
        if self.path.exists():
            try:
                if self.path.stat().st_size > 2 * 1024 * 1024:
                    raise ThemeError("外观配置文件过大。")
                with self.path.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                self._state = self._normalise_document(raw)
            except (ValueError, UnicodeError) as error:
                backup = self.path.with_name("appearance.corrupt-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".json")
                # Preserve the user's original before replacing an unreadable file.
                shutil.copyfile(self.path, backup)
                self.warnings.append("外观配置无法读取，已保留原文件并恢复初始主题。" + str(error))
                self._write(self._state)
        else:
            self._write(self._state)

    def _defaults(self) -> dict[str, Any]:
        return {
            "version": 1, "active_theme": "light",
            "themes": copy.deepcopy(list(self._provided_themes.values())),
            "preferences": dict(DEFAULT_PREFERENCES),
        }

    def _normalise_document(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or not isinstance(raw.get("themes"), list):
            raise ThemeError("外观配置结构无效。")
        if raw.get("version", 1) != 1:
            raise ThemeError("外观配置版本暂不支持。")
        state = self._defaults()
        loaded, ids = [], set()
        for item in raw["themes"]:
            if not isinstance(item, dict):
                continue
            theme_id = item.get("id")
            if not isinstance(theme_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", theme_id) or theme_id in ids:
                continue
            base = theme_id if theme_id in BUILTIN_THEMES else item.get("base", "light")
            if not isinstance(base, str) or base not in BUILTIN_THEMES:
                base = "light"
            theme = copy.deepcopy(self._provided_themes.get(theme_id, BUILTIN_THEMES[base]))
            theme.update(id=theme_id, builtin=theme_id in self._provided_themes, base=base)
            name = item.get("name", theme["name"])
            theme["name"] = name.strip()[:60] if isinstance(name, str) and name.strip() else "未命名主题"
            for key in THEME_FIELDS:
                if key in item:
                    try:
                        theme[key] = _theme_value(key, item[key])
                    except (ThemeError, TypeError):
                        self.warnings.append("已恢复无效的主题设置：" + key)
            corrected = ensure_theme_contrast(theme)
            if corrected != theme and "已自动修正低对比度配色，原始文件仍保留。" not in self.warnings:
                self.warnings.append("已自动修正低对比度配色，原始文件仍保留。")
            theme = corrected
            loaded.append(theme)
            ids.add(theme_id)
        for index, (theme_id, theme) in enumerate(self._provided_themes.items()):
            if theme_id not in ids:
                loaded.insert(index, copy.deepcopy(theme))
        state["themes"] = loaded
        available = {theme["id"] for theme in loaded}
        if isinstance(raw.get("active_theme"), str) and raw["active_theme"] in available:
            state["active_theme"] = raw["active_theme"]
        preferences = raw.get("preferences", {})
        if isinstance(preferences, dict):
            for key in DEFAULT_PREFERENCES:
                if key in preferences:
                    try:
                        state["preferences"][key] = _preference_value(key, preferences[key])
                    except (ThemeError, TypeError):
                        self.warnings.append("已恢复无效的阅读偏好：" + key)
        if not isinstance(preferences, dict) or "ui_mode" not in preferences:
            # 老用户升级：存档里没有界面模式键，默认高级（不隐藏原有功能）
            state["preferences"]["ui_mode"] = "advanced"
        return state

    @property
    def active_id(self) -> str:
        return self._state["active_theme"]

    @property
    def active(self) -> dict[str, Any]:
        return copy.deepcopy(self._find(self._state, self.active_id))

    @property
    def themes(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._state["themes"])

    @property
    def preferences(self) -> dict[str, Any]:
        return dict(self._state["preferences"])

    @property
    def can_undo(self) -> bool:
        return bool(self._history)

    @staticmethod
    def _find(state: Mapping[str, Any], theme_id: str) -> dict[str, Any]:
        for theme in state["themes"]:
            if theme["id"] == theme_id:
                return theme
        raise ThemeError("没有找到这个主题。")

    def _write(self, state: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".appearance-", suffix=".tmp", dir=self.root_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def save(self) -> None:
        self._write(self._state)

    def _commit(self, candidate: dict[str, Any]) -> bool:
        unchanged = {theme["id"]: theme for theme in self._state["themes"]}
        for index, theme in enumerate(candidate["themes"]):
            if unchanged.get(theme["id"]) != theme:
                candidate["themes"][index] = ensure_theme_contrast(theme, candidate["preferences"])
        if candidate == self._state:
            return False
        self._write(candidate)
        if not self._editing or not self._edit_recorded:
            self._history.append(copy.deepcopy(self._state))
            del self._history[:-HISTORY_LIMIT]
            if self._editing:
                self._edit_recorded = True
        self._state = candidate
        return True

    def begin_edit(self) -> None:
        """Group a slider drag into one undo step while still saving every change."""
        if not self._editing:
            self._editing = True
            self._edit_recorded = False

    def end_edit(self) -> None:
        self._editing = False
        self._edit_recorded = False

    def change(self, values: Mapping[str, Any]) -> bool:
        if not isinstance(values, Mapping):
            raise ThemeError("主题更改需要使用键值对。")
        normalised = {key: _theme_value(key, value) for key, value in values.items()}
        candidate = copy.deepcopy(self._state)
        self._find(candidate, self.active_id).update(normalised)
        return self._commit(candidate)

    def change_preferences(self, values: Mapping[str, Any]) -> bool:
        if not isinstance(values, Mapping):
            raise ThemeError("阅读偏好需要使用键值对。")
        normalised = {key: _preference_value(key, value) for key, value in values.items()}
        candidate = copy.deepcopy(self._state)
        candidate["preferences"].update(normalised)
        return self._commit(candidate)

    def select(self, theme_id: str) -> bool:
        self._find(self._state, theme_id)
        self.end_edit()
        candidate = copy.deepcopy(self._state)
        candidate["active_theme"] = theme_id
        return self._commit(candidate)

    @staticmethod
    def _name(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ThemeError("请填写主题名称。")
        result = value.strip()
        if len(result) > 60 or any(ord(char) < 32 for char in result):
            raise ThemeError("主题名称最多 60 个字符，不能包含换行。")
        return result

    def _unique_name(self, name: str) -> str:
        used = {theme["name"].casefold() for theme in self._state["themes"]}
        if name.casefold() not in used:
            return name
        for number in range(2, len(used) + 3):
            proposal = name[:54] + " " + str(number)
            if proposal.casefold() not in used:
                return proposal
        return name[:50] + " " + uuid.uuid4().hex[:6]

    def create(self, name: str | None = None, copy_current: bool = False) -> str:
        self.end_edit()
        current = self.active
        theme = current if copy_current else copy.deepcopy(BUILTIN_THEMES[current["base"]])
        new_id = "theme_" + uuid.uuid4().hex[:16]
        proposed = name if name is not None else (current["name"] + " 副本" if copy_current else "新主题")
        # Automatically generated names may need shortening after a long rename.
        proposed = proposed[:60] if name is None else proposed
        theme.update(id=new_id, name=self._unique_name(self._name(proposed)), builtin=False)
        candidate = copy.deepcopy(self._state)
        candidate["themes"].append(theme)
        candidate["active_theme"] = new_id
        self._commit(candidate)
        return new_id

    def export_theme(self, path: str | os.PathLike[str]) -> None:
        active = self.active
        theme = {"name": active["name"], "base": active["base"],
                 **{key: active[key] for key in THEME_FIELDS if key != "background_image"}}
        Path(path).write_text(json.dumps({
            "format": "vrchat-dance-theme", "version": 1, "theme": theme,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def import_theme(self, path: str | os.PathLike[str]) -> str:
        source = Path(path)
        if not source.is_file() or source.stat().st_size > 2 * 1024 * 1024:
            raise ThemeError("主题文件不存在或过大。")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ThemeError("无法读取主题文件：" + str(error)) from None
        if not isinstance(raw, dict) or raw.get("format") != "vrchat-dance-theme" or raw.get("version") != 1:
            raise ThemeError("这不是受支持的跳舞房助手主题文件。")
        item = raw.get("theme")
        if not isinstance(item, dict):
            raise ThemeError("主题文件缺少配色内容。")
        base = item.get("base", "light")
        if base not in BUILTIN_THEMES:
            raise ThemeError("主题基础模式无效。")
        theme = copy.deepcopy(BUILTIN_THEMES[base])
        for key in THEME_FIELDS:
            if key in item and key != "background_image":
                theme[key] = _theme_value(key, item[key])
        theme["background_image"] = ""
        new_id = "theme_" + uuid.uuid4().hex[:16]
        theme.update(id=new_id, name=self._unique_name(self._name(item.get("name", "导入主题"))), builtin=False)
        candidate = copy.deepcopy(self._state)
        candidate["themes"].append(theme)
        candidate["active_theme"] = new_id
        self._commit(candidate)
        return new_id

    def rename(self, theme_id: str, name: str) -> bool:
        new_name = self._name(name)
        self._find(self._state, theme_id)
        if any(theme["id"] != theme_id and theme["name"].casefold() == new_name.casefold() for theme in self._state["themes"]):
            raise ThemeError("已有同名主题，请使用另一个名称。")
        candidate = copy.deepcopy(self._state)
        self._find(candidate, theme_id)["name"] = new_name
        return self._commit(candidate)

    def delete(self, theme_id: str) -> bool:
        target = self._find(self._state, theme_id)
        if target["builtin"]:
            raise ThemeError("随程序提供的主题需要保留；可以编辑、复制或重置其设置。")
        self.end_edit()
        candidate = copy.deepcopy(self._state)
        candidate["themes"] = [theme for theme in candidate["themes"] if theme["id"] != theme_id]
        if candidate["active_theme"] == theme_id:
            candidate["active_theme"] = target["base"]
        # Copies can share an image, and undo can restore deleted themes.  Keep
        # managed images instead of deleting a file another theme still needs.
        return self._commit(candidate)

    def undo(self) -> bool:
        self.end_edit()
        if not self._history:
            return False
        candidate = copy.deepcopy(self._history[-1])
        self._write(candidate)
        self._state = candidate
        self._history.pop()
        return True

    def reset_current(self) -> bool:
        self.end_edit()
        active = self.active
        defaults = self._provided_themes.get(active["id"], BUILTIN_THEMES[active["base"]])
        return self.change({key: defaults[key] for key in THEME_FIELDS})

    def reset_group(self, group: str) -> bool:
        if group not in THEME_GROUPS:
            raise ThemeError("未识别的设置分组。")
        self.end_edit()
        active = self.active
        defaults = self._provided_themes.get(active["id"], BUILTIN_THEMES[active["base"]])
        return self.change({key: defaults[key] for key in THEME_GROUPS[group]})

    def reset_preferences(self) -> bool:
        self.end_edit()
        return self.change_preferences(DEFAULT_PREFERENCES)

    def background_path(self, theme: Mapping[str, Any] | None = None) -> Path | None:
        value = (theme if theme is not None else self.active).get("background_image")
        if not value:
            return None
        try:
            value = _image_reference(value)
            path = (self.root_dir / value).resolve()
            if path.parent != self.background_dir.resolve():
                return None
            if path.is_file():
                return path
            # Earlier preview versions could reference a packaged image that
            # was never copied into user data. Keep the theme/reference intact;
            # a missing image renders its base color until the user imports one.
            return None
        except (OSError, ThemeError, RuntimeError):
            return None

    def import_background(self, path: str | os.PathLike[str]) -> str:
        source = Path(path).expanduser()
        if not source.is_file():
            raise ThemeError("没有找到这张图片，请重新选择。")
        if source.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            raise ThemeError("仅支持 PNG、JPG 和 WEBP 图片。")
        if source.stat().st_size > MAX_IMAGE_BYTES:
            raise ThemeError("图片最大支持 20 MB，请先缩小图片。")
        with source.open("rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ThemeError("图片最大支持 20 MB，请先缩小图片。")
        extension, width, height = _image_info(data)
        if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS or max(width, height) > MAX_IMAGE_EDGE:
            raise ThemeError("图片尺寸过大：最多 4000 万像素，单边不超过 16384 像素。")
        filename = hashlib.sha256(data).hexdigest() + "." + extension
        target = self.background_dir / filename
        if not target.is_file() or target.stat().st_size != len(data) or hashlib.sha256(target.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
            descriptor, temporary = tempfile.mkstemp(prefix=".background-", suffix=".tmp", dir=self.background_dir)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        relative = "backgrounds/" + filename
        self.change({"background_image": relative})
        return relative
