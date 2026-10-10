"""Shared palette, fonts, and drawing helpers for chart images."""
from __future__ import annotations

import io
from collections.abc import Mapping

from PIL import Image, ImageDraw, ImageFont

from paths import bundled_root
from util import fmt_bytes

BG       = '#1a1a1d'
PANEL    = '#232327'
GRID     = '#2c2c31'
TEXT     = '#d4d4d8'
TEXT_DIM = '#8e8e96'
BORDER   = '#3a3a42'

REG_DOWN = '#9ca3af'
REG_UP   = '#d1d5db'
WL_DOWN  = '#6b7280'
WL_UP    = '#a1a1aa'

_FONT_PATH = bundled_root() / "res" / "fonts" / "DejaVuSans.ttf"

BW_SIZE = (1400, 980)
LB_SIZE = (1260, 840)


def calc_bar_width(n_bars: int, *, min_w: float = 0.4, max_w: float = 0.9) -> float:
    """Calculate bar width based on number of bars for stacked bar charts."""
    width = 1.0 - (n_bars - 1) * 0.03
    return max(min_w, min(max_w, width))


def load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT_PATH), size)


def text_size(face: ImageFont.FreeTypeFont, text: str) -> tuple[int, int]:
    # Pillow leaves the unused direction/features options untyped.
    left, top, right, bottom = face.getbbox(text)  # pyright: ignore[reportUnknownMemberType]
    return int(right - left), int(bottom - top)


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    fill: str,
) -> None:
    """Keep Pillow's untyped extra text options at the library boundary."""
    draw.text(xy, text, font=font, fill=fill)  # pyright: ignore[reportUnknownMemberType]


def ellipsis(face: ImageFont.FreeTypeFont, text: str, max_width: int) -> str:
    if text_size(face, text)[0] <= max_width:
        return text
    trimmed = text
    while trimmed and text_size(face, trimmed + '…')[0] > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + '…') if trimmed else '…'


def nice_ticks(max_value: float, count: int = 4) -> list[float]:
    """Return 0 plus 1/2/5-scaled steps that cover max_value."""
    if max_value <= 0:
        return [0.0]
    raw = max_value / count
    exp = 0
    probe = raw
    while probe >= 10:
        probe /= 10
        exp += 1
    while probe < 1:
        probe *= 10
        exp -= 1
    base = 10.0
    for candidate in (1.0, 2.0, 5.0, 10.0):
        if probe <= candidate:
            base = candidate
            break
    step = base * (10 ** exp)
    if step <= 0:
        return [0.0, max_value]
    # step >= max/count, so count steps always cover the tallest bar
    return [i * step for i in range(count + 1)]


def save(image: Image.Image) -> io.BytesIO:
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    buf.seek(0)
    return buf


def draw_stacked(
    draw: ImageDraw.ImageDraw,
    *,
    plot: tuple[int, int, int, int],
    labels: list[str],
    bottoms: list[int],
    tops: list[int],
    bottom_color: str,
    top_color: str,
    bar_frac: float,
    title: str,
    legend: tuple[str, str],
    empty_label: str | None,
    fonts: Mapping[str, ImageFont.FreeTypeFont],
) -> None:
    left, top, right, bottom = plot
    draw.rectangle((left, top, right, bottom), fill=PANEL)
    draw_text(draw, (left, top - 28), title, font=fonts['title'], fill=TEXT)

    peak = max((b + t for b, t in zip(bottoms, tops, strict=True)), default=0)
    ticks = nice_ticks(float(peak))
    scale_max = ticks[-1] if ticks[-1] > 0 else 1.0
    plot_h = bottom - top

    for tick in ticks:
        y = bottom - int((tick / scale_max) * plot_h)
        draw.line((left, y, right, y), fill=GRID, width=1)
        label = fmt_bytes(tick)
        tw, _ = text_size(fonts['tick'], label)
        draw_text(draw, (left - tw - 8, y - 7), label, font=fonts['tick'], fill=TEXT_DIM)

    draw.line((left, bottom, right, bottom), fill=BORDER, width=1)
    draw.line((left, top, left, bottom), fill=BORDER, width=1)

    n = max(len(labels), 1)
    slot = (right - left) / n
    bar_w = max(1, int(slot * bar_frac))
    step = max(1, n // 10) if n > 15 else 1

    for i, (label, down, up) in enumerate(zip(labels, bottoms, tops, strict=True)):
        cx = left + slot * (i + 0.5)
        x0 = int(cx - bar_w / 2)
        x1 = x0 + bar_w
        down_h = int((down / scale_max) * plot_h)
        up_h = int((up / scale_max) * plot_h)
        if down_h:
            draw.rectangle((x0, bottom - down_h, x1, bottom), fill=bottom_color)
        if up_h:
            draw.rectangle((x0, bottom - down_h - up_h, x1, bottom - down_h), fill=top_color)
        if i % step == 0:
            tw, _ = text_size(fonts['tick'], label)
            draw_text(draw, (int(cx - tw / 2), bottom + 8), label, font=fonts['tick'], fill=TEXT_DIM)

    if empty_label is not None:
        tw, th = text_size(fonts['body'], empty_label)
        draw_text(
            draw,
            ((left + right - tw) // 2, (top + bottom - th) // 2),
            empty_label,
            font=fonts['body'],
            fill=TEXT_DIM,
        )

    sw = 14
    gap = 8
    down_name, up_name = legend
    up_w, _ = text_size(fonts['tick'], up_name)
    down_w, _ = text_size(fonts['tick'], down_name)
    legend_w = sw + 6 + down_w + 16 + sw + 6 + up_w
    lx = right - legend_w
    ly = top + 8
    draw.rectangle((lx, ly, lx + sw, ly + sw), fill=bottom_color)
    draw_text(draw, (lx + sw + 6, ly - 1), down_name, font=fonts['tick'], fill=TEXT_DIM)
    ux = lx + sw + 6 + down_w + 16
    draw.rectangle((ux, ly, ux + sw, ly + sw), fill=top_color)
    draw_text(draw, (ux + sw + gap, ly - 1), up_name, font=fonts['tick'], fill=TEXT_DIM)
