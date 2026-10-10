"""Subscription announcement text."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from util import fmt_bytes, format  # noqa: A004

if TYPE_CHECKING:
    from config import LangConfig
    from custom_types import BandwidthInfo


def build_description(
    lang_cfg: LangConfig,
    name: str,
    lang: str,
    bandwidths: BandwidthInfo,
    status: bool,
    statusTime: bool,
    ts: int,
    bw_limit: int,
    bw_used: int,
    wl_limit: int,
    wl_used: int,
) -> str:
    descTable: dict[str, str] = lang_cfg['description'][lang]

    if status:
        desc = descTable["main"]

        desc = format(
            desc,
            up=fmt_bytes(int(bandwidths.upload)),
            down=fmt_bytes(int(bandwidths.download))
        )

        if ts != 0:
            ts_str = descTable["date"]
            ts_str = format(
                ts_str,
                date=datetime.fromtimestamp(ts, tz=UTC).strftime("%d.%m.%y %H:%M (UTC)"),
                days=str((ts - int(time.time())) // 86400)
            )

            desc = format(
                desc,
                slot_time=ts_str
            )
        else:
            desc = format(
                desc,
                slot_time=""
            )

        if bw_limit != 0:
            bw_limit_str = descTable["bw"]
            bw_limit_str = format(
                bw_limit_str,
                used=fmt_bytes(bw_used),
                limit=f"{bw_limit!s}GB"
            )
            desc = format(
                desc,
                slot_bw=bw_limit_str
            )
        else:
            desc = format(
                desc,
                slot_bw=""
            )

        if wl_limit != 0:
            if wl_used > wl_limit * 10**9:
                wl_bw_str = descTable["wl_bw_exceeded"]
            else:
                wl_bw_str = descTable["wl_bw"]

            wl_bw_str = format(
                wl_bw_str,
                used=fmt_bytes(wl_used),
                limit=f"{wl_limit!s}GB"
            )
            desc = format(
                desc,
                slot_wl_bw=wl_bw_str
            )
        else:
            desc = format(
                desc,
                slot_wl_bw=""
            )

    else:
        if bw_limit == 0:
            desc = descTable["main"]

            desc = format(
                desc,
                up=fmt_bytes(int(bandwidths.upload)),
                down=fmt_bytes(int(bandwidths.download)),

                # aren't needed here
                slot_bw="",
                slot_wl_bw=""
            )
        else:
            desc = descTable["main_exceeded"]
            desc = format(
                desc,
                up=fmt_bytes(int(bandwidths.upload)),
                down=fmt_bytes(int(bandwidths.download)),
                used=fmt_bytes(bw_used),
                limit=f"{bw_limit!s}GB"
            )

        if not statusTime:
            time_str = descTable["date_expired"]
            time_str = format(
                time_str,
                date=datetime.fromtimestamp(ts, tz=UTC).strftime("%d.%m.%y %H:%M (UTC)"),
                days=str(-(ts - int(time.time())) // 86400)
            )

            desc = format(
                desc,
                slot_time=time_str
            )
        else:
            desc = format(
                desc,
                slot_time=""
            )

    final = format(desc, username=name)
    if "{" in final:
        raise ValueError("formatted description contains unformatted placeholders")

    return final
