"""从用户背景图提取协调配色；只读取图片，不修改源文件。"""
from __future__ import annotations

import colorsys
from pathlib import Path

from PIL import Image


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(channel))):02X}" for channel in rgb)


def _rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _mix(first, second, amount):
    a, b = _rgb(first), _rgb(second)
    return _hex(tuple(x * (1 - amount) + y * amount for x, y in zip(a, b)))


def _contrast(first, second):
    def luminance(value):
        channels = [channel / 255 for channel in _rgb(value)]
        linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
                  for channel in channels]
        return sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + .05) / (low + .05)


def palette_from_image(path, base="light"):
    with Image.open(Path(path)) as source:
        image = source.convert("RGB")
        image.thumbnail((180, 180))
        quantized = image.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette()
        counts = sorted(quantized.getcolors() or [], reverse=True)
    colors = []
    total = sum(count for count, _ in counts) or 1
    for count, index in counts:
        rgb = tuple(palette[index * 3:index * 3 + 3])
        hue, lightness, saturation = colorsys.rgb_to_hls(*(channel / 255 for channel in rgb))
        colors.append({"hex": _hex(rgb), "share": count / total, "hue": hue,
                       "lightness": lightness, "saturation": saturation})
    if not colors:
        raise ValueError("图片中没有可用颜色。")
    accent = max(colors, key=lambda color: color["saturation"] * 1.6 + color["share"] * .5)["hex"]
    accent_hue = colorsys.rgb_to_hls(*(channel / 255 for channel in _rgb(accent)))[0]
    secondary = max(colors, key=lambda color: (
        min(abs(color["hue"] - accent_hue), 1 - abs(color["hue"] - accent_hue))
        * (.35 + color["saturation"]) + color["share"] * .12))["hex"]
    highlight = max(colors, key=lambda color: color["saturation"] * .8 + color["lightness"] * .35)["hex"]
    background = (max(colors, key=lambda color: color["lightness"]) if base == "light"
                  else min(colors, key=lambda color: color["lightness"]))["hex"]
    if base == "light":
        background = _mix(background, "#FFFFFF", .72)
    else:
        background = _mix(background, "#0F1420", .48)
    text = "#172133" if _contrast("#172133", background) >= _contrast("#EDF1F8", background) else "#EDF1F8"
    button_text = "#FFFFFF" if _contrast("#FFFFFF", accent) >= _contrast("#182132", accent) else "#182132"
    return {
        "accent": accent, "secondary_accent": secondary, "highlight": highlight,
        "background_color": background, "background_gradient": secondary,
        "background_gradient_opacity": .35, "hero_gradient": secondary,
        "hero_gradient_opacity": .28, "text_primary": text,
        "text_secondary": _mix(text, background, .30),
        "nav_active_background": accent, "nav_active_text": accent,
        "primary_button_text": button_text,
        "button_hover_background": _mix(background, accent, .12),
        "table_header_background": _mix(background, accent, .07),
        "table_header_text": _mix(text, background, .20),
        "table_selection_background": _mix(background, accent, .16),
        "border_color": _mix(background, text, .14),
    }
