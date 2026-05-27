from __future__ import annotations

import io
from datetime import datetime, timezone
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.axes import Axes
from matplotlib.patches import Rectangle
from core import BandwidthSnapshot, fmt_bytes
from typing import cast, TypedDict, Literal
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

### NOTE ###
# Matplotlib typings arent perfect.
# Having >20 pyright errors with strict type checking 
# is normal, its just noise.
### NOTE ###

_LANG = {
    "ru": {
        "bandwidth": "Использование трафика",
        "days": "дн.",
        "day": "день",
        "regular_traffic": "Обычный трафик",
        "whitelist_traffic": "Белый список",
        "download": "Загрузка",
        "upload": "Отдача",
        "no_data": "Нет данных",
        "leaderboard": "Таблица лидеров",
        "bw_type_total": "весь трафик",
        "bw_type_monthly": "трафик за месяц",
        "bw_type_wl_monthly": "WL-трафик за месяц"
    },
    "en": {
        "bandwidth": "Bandwidth",
        "days": "days",
        "day": "day",
        "regular_traffic": "Regular traffic",
        "whitelist_traffic": "Whitelist traffic",
        "download": "Download",
        "upload": "Upload",
        "no_data": "No data",
        "leaderboard": "Leaderboard",
        "bw_type_total": "total bandwidth",
        "bw_type_monthly": "monthly bandwidth",
        "bw_type_wl_monthly": "whitelist monthly bandwidth"
    }
}

class _BarKwargs(TypedDict):
    width: float
    edgecolor: str
    zorder: int

def _format_ticks(v: float, pos: float | None) -> str:
    return fmt_bytes(v) 

def _style_axes(ax: Axes, title: str) -> None:
    """Apply consistent gray-themed styling to a subplot."""
    ax.set_facecolor(_PANEL)
    ax.set_title(title, color=_TEXT, fontsize=11, fontweight='500', 
                 loc='left', pad=10)
    
    ax.grid(axis='y', color=_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        ax.spines[side].set_color(_BORDER)
        ax.spines[side].set_linewidth(0.8)
    
    ax.tick_params(colors=_TEXT_DIM, labelsize=9, length=0)
    ax.yaxis.set_major_formatter(FuncFormatter(_format_ticks))


def bandwidth_chart(
    snapshots: list[BandwidthSnapshot],
    *,
    label: str | None = None,
    lang: str = "en",
) -> io.BytesIO | None:
    """Render two stacked bar charts (regular + whitelist) into a single PNG.

    Args:
        snapshots: daily bandwidth records, will be sorted ascending by ts
        label: optional, included in the suptitle if provided
        lang: language code ("ru" or "en"), defaults to "en"

    Returns:
        BytesIO containing PNG bytes, positioned at start. None if no data.
    """
    if not snapshots:
        return None

    t = _LANG.get(lang, _LANG["en"])
    snaps = sorted(snapshots, key=lambda s: s.ts)
    
    labels = [datetime.fromtimestamp(s.ts, tz=timezone.utc).strftime('%m/%d') 
              for s in snaps]
    
    reg_up    = [s.up    for s in snaps]
    reg_down  = [s.down  for s in snaps]
    wl_up     = [s.wl_up for s in snaps]
    wl_down   = [s.wl_down for s in snaps]
    # fig, (ax_reg, ax_wl)
    result = plt.subplots(
        2, 1,
        figsize=(10, 7),
        dpi=140,
        gridspec_kw={'hspace': 0.35},
    )
    fig = result[0]
    ax_reg = cast(Axes, result[1][0])
    ax_wl = cast(Axes, result[1][1])


    fig.patch.set_facecolor(_BG)  # type: ignore[attr-defined]

    header = f'{t["bandwidth"]} — {label}' if label else t["bandwidth"]
    period = f'{len(snaps)} {t["day"]}' if len(snaps) == 1 else f'{len(snaps)} {t["days"]}'
    fig.suptitle(header, color=_TEXT, fontsize=13, fontweight='500',
                 x=0.07, y=0.97, ha='left')
    fig.text(0.07, 0.935, period, color=_TEXT_DIM, fontsize=10, ha='left')
    
    bar_kwargs = _BarKwargs(width=0.75, edgecolor='none',zorder=2)
    _style_axes(ax_reg, t["regular_traffic"])
    ax_reg.bar(labels, reg_down, color=_REG_DOWN, label=t["download"], **bar_kwargs)
    ax_reg.bar(labels, reg_up, bottom=reg_down, color=_REG_UP,
               label=t["upload"], **bar_kwargs)

    _style_axes(ax_wl, t["whitelist_traffic"])
    ax_wl.bar(labels, wl_down, color=_WL_DOWN, label=t["download"], **bar_kwargs)
    ax_wl.bar(labels, wl_up, bottom=wl_down, color=_WL_UP,
              label=t["upload"], **bar_kwargs)
    
    if len(labels) > 15:
        step = max(1, len(labels) // 10)
        for ax in (ax_reg, ax_wl):
            for i, lbl in enumerate(ax.get_xticklabels()):  # type: ignore[operator]
                if i % step != 0:
                    lbl.set_visible(False)
    
    legend_kwargs = dict(
        loc='upper right',
        frameon=False,
        fontsize=9,
        labelcolor=_TEXT_DIM,
        handlelength=1.2,
        handleheight=0.8,
        columnspacing=1.0,
        ncol=2,
    )
    ax_reg.legend(**legend_kwargs)
    ax_wl.legend(**legend_kwargs)
    
    if not any(reg_up) and not any(reg_down):
        ax_reg.text(0.5, 0.5, t["no_data"], transform=ax_reg.transAxes,
                    ha='center', va='center', color=_TEXT_DIM, fontsize=10)
    if not any(wl_up) and not any(wl_down):
        ax_wl.text(0.5, 0.5, t["no_data"], transform=ax_wl.transAxes,
                   ha='center', va='center', color=_TEXT_DIM, fontsize=10)
    
    fig.subplots_adjust(left=0.09, right=0.97, top=0.89, bottom=0.08)
    
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format='png', facecolor=_BG, edgecolor='none')
    finally:
        plt.close(fig)
    buf.seek(0)
    return buf

def leaderboard_chart(
    data: dict[str, int],
    *,
    bandwidth_type: Literal["total", "monthly", "wl_monthly"],
    lang: str = "en"
) -> io.BytesIO | None:
    """Render a leaderboard of users by bandwidth.

    Args:
        data: leaderboard data, usually taken from core.Subscription.leaderboard().
            structure: {"username": 123456, "second_place": 123123}
        bandwidth_type: type of bandwidth.
        lang: language, en or ru (defaults to en)
    """
    if not data:
        return None

    t = _LANG.get(lang, _LANG["en"])

    bw_key = f"bw_type_{bandwidth_type}"
    bw_label = t.get(bw_key, t["bw_type_total"])

    # sort descending, take top 15
    sorted_users = sorted(data.items(), key=lambda x: x[1], reverse=True)[:15]
    usernames = [u for u, _ in sorted_users]
    values = [v for _, v in sorted_users]

    fig, ax = plt.subplots(figsize=(9, 6), dpi=140)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_PANEL)

    header = f'{t["leaderboard"]} — {bw_label}'
    fig.suptitle(header, color=_TEXT, fontsize=13, fontweight='500',
                 x=0.07, y=0.97, ha='left')

    colors = [_REG_DOWN] * len(usernames)
    bars = ax.barh(usernames, values, color=colors, edgecolor='none', zorder=2)

    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(FuncFormatter(_format_ticks))

    ax.grid(axis='x', color=_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)

    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        ax.spines[side].set_color(_BORDER)
        ax.spines[side].set_linewidth(0.8)

    ax.tick_params(colors=_TEXT_DIM, labelsize=9, length=0)
    ax.set_xlabel('')
    ax.set_ylabel('')

    # value labels on bars
    for bar, val in zip(bars, values):
        bar = cast(Rectangle, bar)
        ax.text(
            bar.get_width(), bar.get_y() + bar.get_height() / 2,
            f' {fmt_bytes(val)}',
            va='center', ha='left', color=_TEXT_DIM, fontsize=9
        )

    fig.subplots_adjust(left=0.18, right=0.95, top=0.88, bottom=0.08)

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format='png', facecolor=_BG, edgecolor='none')
    finally:
        plt.close(fig)
    buf.seek(0)
    return buf