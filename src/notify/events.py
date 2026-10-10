"""Pure quota classification. No Telegram, no database."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .kinds import Kind

if TYPE_CHECKING:
    from custom_types import UserRecord

_GB = 10**9
_DAY = 86400
_EXPIRY_DAYS = 3
_TRAFFIC_BUCKETS = (95, 80)


def _gb(used: int) -> int:
    """Whole gigabytes already consumed. Floor, so 9.6 GB is not "10"."""
    if used <= 0:
        return 0
    return used // _GB


def _traffic_bucket(used: int, limit_gb: int) -> int | None:
    if limit_gb <= 0:
        return None
    limit = limit_gb * _GB
    if used > limit:
        return None
    ratio = used / limit
    for percent in _TRAFFIC_BUCKETS:
        if ratio > percent / 100:
            return percent
    return None


@dataclass(frozen=True, slots=True)
class Notice:
    """One warning a user should hear, if they have not heard this bucket yet."""

    kind: Kind
    bucket: str
    fields: dict[str, int]
    clears: tuple[str, ...] = ()

    @property
    def marker(self) -> str:
        """Bare kind:bucket. Storage prefixes the username separately."""
        if self.bucket:
            return f"{self.kind.kind_id}:{self.bucket}"
        return self.kind.kind_id


def _soon(kind: Kind, bucket: int, **fields: int) -> Notice:
    return Notice(kind, str(bucket), fields)


def classify(state: UserRecord, now: int) -> tuple[Notice, ...]:
    """Warnings implied by one user row.

    Disable decisions stay with the caller: this only describes the message.
    A disabled side does not also emit its near-limit warning.
    """
    notices: list[Notice] = []
    expires_at = int(state["expires_at"])
    if expires_at != 0:
        remaining = expires_at - now
        if remaining <= 0:
            if bool(state["enabled_time"]):
                notices.append(Notice(Kind.EXPIRED, "1", {}, clears=("expiry_soon",)))
        else:
            days = remaining // _DAY
            if 0 <= days <= _EXPIRY_DAYS:
                notices.append(_soon(Kind.EXPIRY_SOON, days, days=days))

    wl_limit = int(state["wl_limit_gb"])
    wl_used = int(state["wl_used"])
    if wl_limit != 0 and wl_used > wl_limit * _GB:
        if bool(state["enabled_wl"]):
            notices.append(
                Notice(
                    Kind.WHITELIST_EXHAUSTED,
                    "1",
                    {"limit_gb": wl_limit},
                    clears=("whitelist_soon",),
                )
            )
    else:
        wl_bucket = _traffic_bucket(wl_used, wl_limit)
        if wl_bucket is not None:
            notices.append(
                _soon(
                    Kind.WHITELIST_SOON,
                    wl_bucket,
                    used_gb=_gb(wl_used),
                    limit_gb=wl_limit,
                    percent=wl_bucket,
                )
            )

    bw_limit = int(state["bw_limit_gb"])
    bw_used = int(state["bw_used"])
    over_main = bw_limit != 0 and bw_used > bw_limit * _GB
    if bool(state["enabled"]) and over_main:
        notices.append(
            Notice(
                Kind.TRAFFIC_EXHAUSTED,
                "1",
                {"limit_gb": bw_limit},
                clears=("traffic_soon",),
            )
        )
    elif bool(state["enabled"]):
        bucket = _traffic_bucket(bw_used, bw_limit)
        if bucket is not None:
            notices.append(
                _soon(
                    Kind.TRAFFIC_SOON,
                    bucket,
                    used_gb=_gb(bw_used),
                    limit_gb=bw_limit,
                    percent=bucket,
                )
            )
    return tuple(notices)


def restored(
    *,
    main: bool = False,
    whitelist: bool = False,
    time: bool = False,
) -> tuple[Notice, ...]:
    """Recovery notices for sides whose panel re-enable just succeeded.

    The caller decides which side was re-enabled. Delivery still requires a
    stored outage marker, and that marker is cleared after a successful send.
    """
    notices: list[Notice] = []
    if main or time:
        clears: list[str] = []
        if main:
            clears.extend(("traffic_exhausted", "traffic_soon"))
        if time:
            clears.extend(("expired", "expiry_soon"))
        notices.append(
            Notice(
                Kind.RESTORED,
                "",
                {},
                clears=tuple(clears),
            )
        )
    if whitelist:
        notices.append(
            Notice(
                Kind.WHITELIST_RESTORED,
                "",
                {},
                clears=("whitelist_exhausted", "whitelist_soon"),
            )
        )
    return tuple(notices)
