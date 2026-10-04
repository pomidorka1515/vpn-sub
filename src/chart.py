from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from custom_types import BandwidthSnapshot
from util import fmt_bytes
from typing import Literal, Mapping

__all__ = ['bandwidth_chart', 'leaderboard_chart']

_BG       = '#1a1a1d'
_PANEL    = '#232327'
_GRID     = '#2c2c31'
_TEXT     = '#d4d4d8'
_TEXT_DIM = '#8e8e96'
_BORDER   = '#3a3a42'

_REG_DOWN = '#9ca3af'
_REG_UP   = '#d1d5db'
_WL_DOWN  = '#6b7280'
_WL_UP    = '#a1a1aa'

_FONT_PATH = Path(__file__).resolve().parent.parent / 'res' / 'fonts' / 'DejaVuSans.ttf'

_BW_SIZE = (1400, 980)
_LB_SIZE = (1260, 840)

# pyright: reportUnknownMemberType=false

def _calc_bar_width(n_bars: int, *, min_w: float = 0.4, max_w: float = 0.9) -> float:
    """Calculate bar width based on number of bars for stacked bar charts."""
    width = 1.0 - (n_bars - 1) * 0.03
    return max(min_w, min(max_w, width))


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT_PATH), size)


def _text_size(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int]:
    left, top, right, bottom = font.getbbox(text)
    return int(right - left), int(bottom - top)


def _ellipsis(font: ImageFont.FreeTypeFont, text: str, max_width: int) -> str:
    if _text_size(font, text)[0] <= max_width:
        return text
    trimmed = text
    while trimmed and _text_size(font, trimmed + '…')[0] > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + '…') if trimmed else '…'


def _nice_ticks(max_value: float, count: int = 4) -> list[float]:
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


def _save(image: Image.Image) -> io.BytesIO:
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    buf.seek(0)
    return buf


def _draw_stacked(
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
    draw.rectangle((left, top, right, bottom), fill=_PANEL)
    draw.text((left, top - 28), title, font=fonts['title'], fill=_TEXT)

    peak = max((b + t for b, t in zip(bottoms, tops)), default=0)
    ticks = _nice_ticks(float(peak))
    scale_max = ticks[-1] if ticks[-1] > 0 else 1.0
    plot_h = bottom - top

    for tick in ticks:
        y = bottom - int((tick / scale_max) * plot_h)
        draw.line((left, y, right, y), fill=_GRID, width=1)
        label = fmt_bytes(tick)
        tw, _ = _text_size(fonts['tick'], label)
        draw.text((left - tw - 8, y - 7), label, font=fonts['tick'], fill=_TEXT_DIM)

    draw.line((left, bottom, right, bottom), fill=_BORDER, width=1)
    draw.line((left, top, left, bottom), fill=_BORDER, width=1)

    n = max(len(labels), 1)
    slot = (right - left) / n
    bar_w = max(1, int(slot * bar_frac))
    step = max(1, n // 10) if n > 15 else 1

    for i, (label, down, up) in enumerate(zip(labels, bottoms, tops)):
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
            tw, _ = _text_size(fonts['tick'], label)
            draw.text((int(cx - tw / 2), bottom + 8), label, font=fonts['tick'], fill=_TEXT_DIM)

    if empty_label is not None:
        tw, th = _text_size(fonts['body'], empty_label)
        draw.text(
            ((left + right - tw) // 2, (top + bottom - th) // 2),
            empty_label,
            font=fonts['body'],
            fill=_TEXT_DIM,
        )

    sw = 14
    gap = 8
    down_name, up_name = legend
    up_w, _ = _text_size(fonts['tick'], up_name)
    down_w, _ = _text_size(fonts['tick'], down_name)
    legend_w = sw + 6 + down_w + 16 + sw + 6 + up_w
    lx = right - legend_w
    ly = top + 8
    draw.rectangle((lx, ly, lx + sw, ly + sw), fill=bottom_color)
    draw.text((lx + sw + 6, ly - 1), down_name, font=fonts['tick'], fill=_TEXT_DIM)
    ux = lx + sw + 6 + down_w + 16
    draw.rectangle((ux, ly, ux + sw, ly + sw), fill=top_color)
    draw.text((ux + sw + gap, ly - 1), up_name, font=fonts['tick'], fill=_TEXT_DIM)


def bandwidth_chart(
    snapshots: list[BandwidthSnapshot],
    *,
    label: str | None = None,
    lang: Mapping[str, str],
    bar_width: float | None = None,
) -> io.BytesIO | None:
    """Render two stacked bar charts (regular + whitelist) into a single PNG.

    Args:
        snapshots: daily bandwidth records, will be sorted ascending by ts
        label: optional, included in the suptitle if provided
        lang: language table, usually taken from `lang_cfg`.
        bar_width: optional, bar width override. Defaults to auto-calculated
            based on number of data points to maintain good visual density.

    Returns:
        BytesIO containing PNG bytes, positioned at start. None if no data.
    """
    if not snapshots:
        return None

    snaps = sorted(snapshots, key=lambda s: s.ts)
    labels = [
        datetime.fromtimestamp(s.ts, tz=timezone.utc).strftime('%m/%d')
        for s in snaps
    ]
    reg_up = [s.up for s in snaps]
    reg_down = [s.down for s in snaps]
    wl_up = [s.wl_up for s in snaps]
    wl_down = [s.wl_down for s in snaps]

    width, height = _BW_SIZE
    image = Image.new('RGB', (width, height), _BG)
    draw = ImageDraw.Draw(image)
    fonts: dict[str, ImageFont.FreeTypeFont] = {
        'header': _font(22),
        'sub': _font(16),
        'title': _font(18),
        'body': _font(16),
        'tick': _font(14),
    }

    header = f'{lang["bandwidth"]} — {label}' if label else lang['bandwidth']
    period = f'{len(snaps)} {lang["day"]}' if len(snaps) == 1 else f'{len(snaps)} {lang["days"]}'
    draw.text((40, 18), header, font=fonts['header'], fill=_TEXT)
    draw.text((40, 48), period, font=fonts['sub'], fill=_TEXT_DIM)

    n = len(labels)
    bar_frac = _calc_bar_width(n) if bar_width is None else bar_width
    legend = (lang['download'], lang['upload'])
    margin_l, margin_r = 110, 36
    panels = (
        (lang['regular_traffic'], reg_down, reg_up, _REG_DOWN, _REG_UP, 130, 500),
        (lang['whitelist_traffic'], wl_down, wl_up, _WL_DOWN, _WL_UP, 580, 930),
    )
    for title, bottoms, tops, bottom_color, top_color, top, bottom in panels:
        empty = lang['no_data'] if not any(bottoms) and not any(tops) else None
        _draw_stacked(
            draw,
            plot=(margin_l, top, width - margin_r, bottom),
            labels=labels,
            bottoms=bottoms,
            tops=tops,
            bottom_color=bottom_color,
            top_color=top_color,
            bar_frac=bar_frac,
            title=title,
            legend=legend,
            empty_label=empty,
            fonts=fonts,
        )
    return _save(image)


def leaderboard_chart(
    data: dict[str, int],
    *,
    bandwidth_type: Literal["total", "monthly", "wl_monthly"],
    lang: Mapping[str, str],
) -> io.BytesIO | None:
    """Render a leaderboard of users by bandwidth.

    Args:
        data: leaderboard data, usually taken from core.Subscription.leaderboard().
            structure: {"username": 123456, "second_place": 123123}
        bandwidth_type: type of bandwidth.
        lang: language table, usually taken from `lang_cfg`
    """
    if not data:
        return None

    bw_key = f'bw_type_{bandwidth_type}'
    bw_label = lang.get(bw_key, lang['bw_type_total'])
    sorted_users = sorted(data.items(), key=lambda x: x[1], reverse=True)[:15]

    width, height = _LB_SIZE
    image = Image.new('RGB', (width, height), _BG)
    draw = ImageDraw.Draw(image)
    header_font = _font(22)
    tick_font = _font(14)

    header = f'{lang["leaderboard"]} — {bw_label}'
    draw.text((40, 18), header, font=header_font, fill=_TEXT)

    label_col = 220
    value_col = 130
    left = 40 + label_col
    right = width - 36 - value_col
    top = 90
    bottom = height - 70
    draw.rectangle((left, top, right, bottom), fill=_PANEL)

    peak = max((v for _, v in sorted_users), default=0)
    ticks = _nice_ticks(float(peak))
    scale_max = ticks[-1] if ticks[-1] > 0 else 1.0
    plot_w = right - left

    for tick in ticks:
        x = left + int((tick / scale_max) * plot_w)
        draw.line((x, top, x, bottom), fill=_GRID, width=1)
        label = fmt_bytes(tick)
        tw, _ = _text_size(tick_font, label)
        draw.text((x - tw // 2, bottom + 10), label, font=tick_font, fill=_TEXT_DIM)

    draw.line((left, bottom, right, bottom), fill=_BORDER, width=1)
    draw.line((left, top, left, bottom), fill=_BORDER, width=1)

    n = len(sorted_users)
    slot = (bottom - top) / max(n, 1)
    bar_h = max(8, int(slot * 0.62))
    for i, (name, value) in enumerate(sorted_users):
        cy = top + slot * (i + 0.5)
        y0 = int(cy - bar_h / 2)
        y1 = y0 + bar_h
        bar_w = int((value / scale_max) * plot_w) if scale_max else 0
        if bar_w:
            draw.rectangle((left, y0, left + bar_w, y1), fill=_REG_DOWN)
        shown = _ellipsis(tick_font, name, label_col - 16)
        tw, th = _text_size(tick_font, shown)
        draw.text((left - 12 - tw, int(cy - th / 2)), shown, font=tick_font, fill=_TEXT_DIM)
        value_label = fmt_bytes(value)
        draw.text((left + bar_w + 8, int(cy - th / 2)), value_label, font=tick_font, fill=_TEXT_DIM)

    return _save(image)
