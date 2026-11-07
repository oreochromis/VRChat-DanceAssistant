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
                       "dismissed_update_version": "", "ui_mode": "simple", "home_style": "vivid"}

_COMMON = {
    "background_image": "", "background_opacity": 1.0,
    "background_gradient": "#E9ECFF", "background_gradient_opacity": 0.0,
    "hero_gradient": "#39B8C6", "hero_gradient_opacity": 0.0,
    "gradient_direction": "diagonal",
    "image_fit": "cover", "image_opacity": 0.35, "image_blur": 0.0,
    "overlay_opacity": 0.0, "border_opacity": 0.8, "border_width": 1.0, "radius": 12.0,
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
        "text_secondary": "#5B667A", "card_background": "#FFFFFF",
        "card_opacity": 0.94, "border_color": "#DDE2EB", "border_opacity": 0.8,
        "background_opacity": 0.97, "image_blur": 9.5, "radius": 12.6,
        "highlight": "#A67E32", "success_color": "#1E7C4A", "warning_color": "#936318",
        "error_color": "#BB4254", "info_color": "#306ACA", "nav_text": "#5F6A81",
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
    "miku": {
        **_COMMON, "id": "miku", "name": "葱绿", "builtin": True, "base": "light",
        "background_image": "backgrounds/af2c10375e25de4ee8b084a60f2a5447a7e6f9b2168d9f868bca4b8b5c9b7410.png",
        "image_blur": 3.5,
        "accent": "#13746E", "background_color": "#EFFAF8", "overlay_color": "#EFFAF8",
        "nav_background": "#F7FFFD", "nav_opacity": 0.82, "nav_text": "#4D7270",
        "nav_active_background": "#39C5BB", "nav_active_opacity": 0.16,
        "nav_active_text": "#16706A", "text_primary": "#173638",
        "text_secondary": "#506B6D", "card_background": "#FFFFFF",
        "card_opacity": 0.94, "border_color": "#C8E4DF", "border_opacity": 0.8,
        "secondary_accent": "#AB506B", "highlight": "#A34F7D",
        "success_color": "#18794F", "info_color": "#276A91",
        "button_hover_background": "#DDF4EF", "table_header_background": "#E3F4F1",
        "table_header_text": "#456B69", "table_selection_background": "#D7F0EC",
        "scrollbar_color": "#73A8A4", "background_gradient": "#97E1DB",
        "background_gradient_opacity": 0.12, "hero_gradient": "#297875",
        "hero_gradient_opacity": 0.12,
    },
    "eva01": {
        **_COMMON, "id": "eva01", "name": "机甲", "builtin": True, "base": "dark",
        "background_image": "backgrounds/c7f970cac572ef33fed3ef96731a68c93a66c341889d0e6852ea99689e5df414.png",
        "image_blur": 5.2,
        "accent": "#A7D46F", "background_color": "#15111F", "overlay_color": "#15111F",
        "nav_background": "#1B1427", "nav_opacity": 0.9, "nav_text": "#B7A7C9",
        "nav_active_background": "#7A4A9E", "nav_active_opacity": 0.22,
        "nav_active_text": "#E2C8FF", "text_primary": "#F0EAF7",
        "text_secondary": "#B7A7C9", "card_background": "#21182E",
        "card_opacity": 0.96, "border_color": "#49355E", "border_opacity": 0.7,
        "secondary_accent": "#B69AE8", "highlight": "#F0B45D",
        "success_color": "#8ED9A5", "warning_color": "#F0B45D",
        "error_color": "#F39AA9", "info_color": "#698ECF", "button_background": "#2D213D",
        "button_text": "#F0EAF7", "button_hover_background": "#3A2A4C",
        "primary_button_text": "#182132", "input_background": "#291D38",
        "table_header_background": "#2B1D3B", "table_header_text": "#C3B1D6",
        "table_selection_background": "#46305F", "scrollbar_color": "#756185",
        "background_gradient": "#241635", "background_gradient_opacity": 0.45,
        "hero_gradient": "#8BC75A", "hero_gradient_opacity": 0.18,
    },
    "jirai": {
        **_COMMON, "id": "jirai", "name": "地瓜", "builtin": True, "base": "light",
        "background_image": "backgrounds/5ea05a19a106bc1d69fc87dcc389ccce655a4bba7a3e7893bfbc8dcdabee440c.png",
        "image_blur": 8.7,
        "accent": "#B72D6B", "background_color": "#FFF4F8", "overlay_color": "#FFF4F8",
        "nav_background": "#FFF8FB", "nav_opacity": 0.82, "nav_text": "#895E71",
        "nav_active_background": "#D94F91", "nav_active_opacity": 0.14,
        "nav_active_text": "#A42662", "text_primary": "#32232C",
        "text_secondary": "#795B69", "card_background": "#FFFBFD",
        "card_opacity": 0.94, "border_color": "#EBCBD9", "border_opacity": 0.8,
        "secondary_accent": "#8D4EA3", "highlight": "#7E4CA0",
        "success_color": "#29734F", "error_color": "#AC304F", "info_color": "#7355B7",
        "button_hover_background": "#F9E2EE", "table_header_background": "#F9E7F0",
        "table_header_text": "#795468", "table_selection_background": "#F4D6E7",
        "scrollbar_color": "#B78FA3", "background_gradient": "#F9D8E8",
        "background_gradient_opacity": 0.35, "hero_gradient": "#B94577",
        "hero_gradient_opacity": 0.16,
    },
    "maimai": {
        **_COMMON, "id": "maimai", "name": "萌舞", "builtin": True, "base": "light",
        "background_image": "backgrounds/72046423e2eeee0ba84501a697010ce3ac89f198061422ba33d2de5565db9b79.png",
        "accent": "#4C5BC4", "background_color": "#F7F8FF", "overlay_color": "#F7F8FF",
        "nav_background": "#FFFFFF", "nav_opacity": 0.82, "nav_text": "#5F6689",
        "nav_active_background": "#5865D8", "nav_active_opacity": 0.13,
        "nav_active_text": "#414FB8", "text_primary": "#20243A",
        "text_secondary": "#5B637C", "card_background": "#FFFFFF",
        "card_opacity": 0.94, "border_color": "#DDE0F0", "border_opacity": 0.8,
        "secondary_accent": "#137A7F", "highlight": "#A56B12",
        "success_color": "#1B7A4A", "warning_color": "#925B12", "error_color": "#B33A55",
        "button_hover_background": "#E9ECFF", "table_header_background": "#F0F1FA",
        "table_header_text": "#555D78", "table_selection_background": "#E2E6FA",
        "scrollbar_color": "#98A1BA", "background_gradient": "#FFE3F1",
        "background_gradient_opacity": 0.32, "hero_gradient": "#AF4B7D",
        "hero_gradient_opacity": 0.16,
    },
    "neko": {
        **_COMMON, "id": "neko", "name": "猫猫", "builtin": True, "base": "light",
        "background_image": "backgrounds/36f8401105b76ec92adb1a31d812cf37fb012b54c2096469515a9615d34d454b.png",
        "table_selection_background": "#DDDCD0", "table_header_background": "#E9E6DE",
        "nav_text": "#59647A", "warning_color": "#936318", "table_header_text": "#414855",
        "background_color": "#F2EEE9", "button_hover_background": "#E2E0D6",
        "border_color": "#D3D1D0", "error_color": "#BB4254", "accent": "#59653D",
        "nav_active_text": "#5E6B40", "info_color": "#306ACA", "text_secondary": "#3D4149",
        "success_color": "#1E7C4A", "nav_active_background": "#6D7B4A",
        "nav_background": "#FFFFFF", "card_background": "#FFFFFF", "hero_gradient": "#393231",
        "background_gradient_opacity": 0.35, "text_primary": "#172133",
        "secondary_accent": "#393231", "radius": 12.6, "background_gradient": "#D7D6D6",
        "hero_gradient_opacity": 0.28, "overlay_color": "#F3F5F9", "highlight": "#877E73",
        "nav_opacity": 0.82, "card_opacity": 0.94, "nav_active_opacity": 0.13,
    },
    "inu": {
        **_COMMON, "id": "inu", "name": "狗狗", "builtin": True, "base": "light",
        "background_image": "backgrounds/30bc17d0d84072649032978059cf05ef847a8a3c1566d9d40429d5cf09780c95.png",
        "table_selection_background": "#EDE9DD", "table_header_background": "#F7F5F0",
        "nav_text": "#5F6A81", "warning_color": "#936318", "table_header_text": "#444B59",
        "background_color": "#FFFFFF", "button_hover_background": "#F1EEE5",
        "border_color": "#DFE0E2", "error_color": "#BB4254", "accent": "#836A25",
        "nav_active_text": "#806824", "info_color": "#306ACA", "text_secondary": "#525863",
        "success_color": "#1E7C4A", "nav_active_background": "#8E7328",
        "nav_background": "#FFFFFF", "card_background": "#FFFFFF", "hero_gradient": "#A45948",
        "background_gradient_opacity": 0.35, "text_primary": "#172133",
        "secondary_accent": "#A45948", "radius": 12.6, "background_gradient": "#E8CFCA",
        "hero_gradient_opacity": 0.28, "overlay_color": "#F3F5F9", "highlight": "#A6872F",
        "nav_opacity": 0.82, "card_opacity": 0.94, "nav_active_opacity": 0.13,
    },
    "chaomian": {
        **_COMMON, "id": "chaomian", "name": "炒面", "builtin": True, "base": "light",
        "background_image": "backgrounds/d038f70dbee7a3e71738520a18579afbc6fb539303e891ef5405cbce4bc79cb3.png",
        "table_selection_background": "#E8E6E4", "table_header_background": "#F3F2F0",
        "nav_text": "#5F6A81", "warning_color": "#936318", "table_header_text": "#434B59",
        "background_color": "#FCFBFA", "button_hover_background": "#EDEBEA",
        "border_color": "#DCDCDE", "error_color": "#BB4254", "accent": "#716A68",
        "nav_active_text": "#706967", "info_color": "#306ACA", "text_secondary": "#5C626F",
        "success_color": "#1E7C4A", "nav_active_background": "#7C7572",
        "nav_background": "#FFFFFF", "card_background": "#FFFFFF", "hero_gradient": "#566F70",
        "background_gradient_opacity": 0.35, "text_primary": "#172133",
        "secondary_accent": "#566F70", "radius": 12.6, "background_gradient": "#ADDEDF",
        "hero_gradient_opacity": 0.28, "overlay_color": "#F3F5F9", "highlight": "#908784",
        "nav_opacity": 0.82, "primary_button_text": "#EEEEEE",
        "card_opacity": 0.94, "nav_active_opacity": 0.13,
    },
    "jidan": {
        **_COMMON, "id": "jidan", "name": "鸡蛋", "builtin": True, "base": "light",
        "background_image": "backgrounds/21c84731dd456b241e2b1179dc6cb741ac248029a8970947ad62e90d1f879ad6.png",
        "table_selection_background": "#E8E5E3", "table_header_background": "#F3F2F0",
        "nav_text": "#5F6A81", "warning_color": "#936318", "table_header_text": "#434B59",
        "background_color": "#FCFBFA", "button_hover_background": "#EDEBE8",
        "border_color": "#DCDCDE", "error_color": "#BB4254", "accent": "#74675D",
        "nav_active_text": "#73665C", "info_color": "#306ACA", "text_secondary": "#484C57",
        "success_color": "#1E7C4A", "nav_active_background": "#827468",
        "nav_background": "#FFFFFF", "card_background": "#FFFFFF", "hero_gradient": "#464648",
        "background_gradient_opacity": 0.35, "text_primary": "#172133",
        "secondary_accent": "#464648", "radius": 12.6, "background_gradient": "#D5D5D6",
        "hero_gradient_opacity": 0.28, "overlay_color": "#F3F5F9", "highlight": "#968678",
        "nav_opacity": 0.82, "card_opacity": 0.94, "nav_active_opacity": 0.13,
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
    return ("#000000" if contrast_ratio("#000000", background) >= contrast_ratio("#FFFFFF", background)
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
    vivid = (preferences or {}).get("home_style") == "vivid"
    gradient_opacity = max(theme["background_gradient_opacity"], .24) if vivid else theme["background_gradient_opacity"]
    gradient = _blend_colors(theme["background_gradient"], background, gradient_opacity)
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
    if vivid:
        card_gradient = _blend_colors(theme["secondary_accent"], background, .10)
        check("多彩卡片主要文字", "text_primary", card_gradient)
        check("多彩卡片辅助文字", "text_secondary", card_gradient)
        check("多彩卡片标题", "accent", card_gradient)
        check("多彩导航文字", "nav_text", _blend_colors(theme["secondary_accent"], background, .07))
        check("多彩导航选中项文字", "nav_active_text", _blend_colors(theme["secondary_accent"], nav, .12))
        check("多彩表头文字", "table_header_text", _blend_colors(theme["hero_gradient"], card, .10))
        for label, key in (("本地命中", "success_color"), ("回源", "warning_color"),
                           ("节省流量", "highlight"), ("后续队列", "secondary_accent")):
            surface = _blend_colors(theme[key], background, .12)
            check(label + "指标数字", key, surface, 3.0)
            check(label + "指标标题", "text_secondary", surface)
    hero_backgrounds = [theme["accent"]]
    if theme["hero_gradient_opacity"] > 0 or vivid:
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
        for key in sorted({issue["key"] for issue in remaining if issue["key"]}):
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
        result["primary_button_text"], hero_surfaces, 4.5)
    for _ in range(3):
        remaining = theme_contrast_issues(result, preferences)
        if not remaining:
            break
        for key in sorted({issue["key"] for issue in remaining if issue["key"]}):
            issues = [issue for issue in remaining if issue["key"] == key]
            result[key] = _ensure_foreground(result[key], [issue["background"] for issue in issues],
                                             max(issue["threshold"] for issue in issues))
    for key in sorted({issue["key"] for issue in theme_contrast_issues(result, preferences) if issue["key"]}):
        result[key] = min(("#000000", "#FFFFFF"), key=lambda color: len(
            theme_contrast_issues({**result, key: color}, preferences)))
    if theme_contrast_issues(result, preferences):
        return copy.deepcopy(BUILTIN_THEMES[result.get("base", "light")])
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
        if value not in ("minimal", "simple", "advanced"):
            raise ThemeError("界面模式请选择极简、标准或高级。")
        return value
    if key == "home_style":
        if value not in ("simple", "vivid"):
            raise ThemeError("界面风格请选择简洁或多彩。")
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


def _windows_dark_mode():
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except (OSError, ValueError, TypeError):
        return False


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
        initial_theme = "dark" if not self.path.exists() and _windows_dark_mode() else "light"
        self._state = self._defaults(initial_theme)
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

    def _defaults(self, active_theme="light") -> dict[str, Any]:
        preferences = dict(DEFAULT_PREFERENCES)
        return {
            "version": 1, "active_theme": active_theme,
            "themes": [ensure_theme_contrast(theme, preferences)
                       for theme in self._provided_themes.values()],
            "preferences": preferences,
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
            base = BUILTIN_THEMES[theme_id]["base"] if theme_id in BUILTIN_THEMES else item.get("base", "light")
            if not isinstance(base, str) or base not in ("light", "dark"):
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
        for index, theme in enumerate(state["themes"]):
            state["themes"][index] = ensure_theme_contrast(theme, state["preferences"])
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
        style_changed = (candidate["preferences"].get("home_style")
                         != self._state["preferences"].get("home_style"))
        for index, theme in enumerate(candidate["themes"]):
            if style_changed or unchanged.get(theme["id"]) != theme:
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

    def reorder(self, theme_ids: list[str]) -> bool:
        if len(theme_ids) != len(self._state["themes"]) or set(theme_ids) != {
                theme["id"] for theme in self._state["themes"]}:
            raise ThemeError("主题顺序包含缺失或重复项目。")
        candidate = copy.deepcopy(self._state)
        by_id = {theme["id"]: theme for theme in candidate["themes"]}
        candidate["themes"] = [by_id[theme_id] for theme_id in theme_ids]
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
        base = BUILTIN_THEMES[base]["base"]
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
            packaged = Path(__file__).resolve().parents[1] / "assets" / "backgrounds" / Path(value).name
            return packaged if packaged.is_file() else None
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
