from __future__ import annotations

from typing import cast

from core import Subscription
from session import XUiSession
from helpers import FakePanel, make_inbound


def _filter(subscription: Subscription, mode: str, listed: tuple[int, ...]) -> list[int]:
    panel = FakePanel(
        inbounds=[make_inbound(1), make_inbound(2), make_inbound(3)],
        mode=mode,
        inbounds_list=listed,
    )
    return [inbound.id for inbound in subscription.panel_svc.getinbounds(cast(XUiSession, panel))]


def test_blacklist_drops_listed_inbound_ids(subscription: Subscription) -> None:
    assert _filter(subscription, "blacklist", (2,)) == [1, 3]


def test_whitelist_keeps_only_listed_inbound_ids(subscription: Subscription) -> None:
    assert _filter(subscription, "whitelist", (2,)) == [2]


def test_empty_whitelist_keeps_no_inbounds(subscription: Subscription) -> None:
    assert _filter(subscription, "whitelist", ()) == []


def test_empty_blacklist_keeps_all_inbounds(subscription: Subscription) -> None:
    assert _filter(subscription, "blacklist", ()) == [1, 2, 3]
