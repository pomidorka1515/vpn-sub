from __future__ import annotations

import io
from datetime import datetime, timezone
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from core import BandwidthSnapshot, fmt_bytes
from typing import cast
__all__ = ['bandwidth_chart']

# Palette — gray with subtle accents
_BG       = '#1a1a1d'
_PANEL    = '#232327'
_GRID     = '#2c2c31'
_TEXT     = '#d4d4d8'
_TEXT_DIM = '#8e8e96'
_BORDER   = '#3a3a42'

_REG_DOWN = '#9ca3af'  # gray-400
_REG_UP   = '#d1d5db'  # gray-300
_WL_DOWN  = '#6b7280'  # gray-500
_WL_UP    = '#a1a1aa'  # zinc-400

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
    }
}



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
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_bytes(v)))


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
    fig.patch.set_facecolor(_BG)

    header = f'{t["bandwidth"]} — {label}' if label else t["bandwidth"]
    period = f'{len(snaps)} {t["day"]}' if len(snaps) == 1 else f'{len(snaps)} {t["days"]}'
    fig.suptitle(header, color=_TEXT, fontsize=13, fontweight='500',
                 x=0.07, y=0.97, ha='left')
    fig.text(0.07, 0.935, period, color=_TEXT_DIM, fontsize=10, ha='left')
    
    bar_kwargs = dict(width=0.75, edgecolor='none', zorder=2)

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
            for i, lbl in enumerate(ax.get_xticklabels()):
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
