"""Stable user-notification kinds.

Copy lives in ``lang.jsonc``. A kind only names the text key, the dedupe
scope, and which condition clears the marker.
"""
from __future__ import annotations

from enum import Enum


class Scope(Enum):
    """How long a successful send suppresses the next one."""

    THRESHOLD = "threshold"
    EPISODE = "episode"
    ONCE = "once"


class Kind(Enum):
    """One user-facing warning. Copy is the text key, not this enum."""

    EXPIRY_SOON = ("expiry_soon", "warning_expiry_soon", Scope.THRESHOLD)
    EXPIRED = ("expired", "warning_expired", Scope.EPISODE)
    TRAFFIC_SOON = ("traffic_soon", "warning_traffic_soon", Scope.THRESHOLD)
    TRAFFIC_EXHAUSTED = ("traffic_exhausted", "warning_traffic_exhausted", Scope.EPISODE)
    WHITELIST_SOON = ("whitelist_soon", "warning_whitelist_soon", Scope.THRESHOLD)
    WHITELIST_EXHAUSTED = ("whitelist_exhausted", "warning_whitelist_exhausted", Scope.EPISODE)
    RESTORED = ("restored", "warning_restored", Scope.ONCE)
    WHITELIST_RESTORED = ("whitelist_restored", "warning_whitelist_restored", Scope.ONCE)

    @property
    def opens_recovery(self) -> Kind | None:
        """Recovery this outage makes newsworthy, or None."""
        if self in (Kind.TRAFFIC_EXHAUSTED, Kind.EXPIRED):
            return Kind.RESTORED
        if self is Kind.WHITELIST_EXHAUSTED:
            return Kind.WHITELIST_RESTORED
        return None

    def __init__(
        self,
        kind_id: str,
        text_key: str,
        scope: Scope,
    ) -> None:
        self.kind_id = kind_id
        self.text_key = text_key
        self.scope = scope
