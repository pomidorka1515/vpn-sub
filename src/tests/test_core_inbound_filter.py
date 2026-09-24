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


def test_getinbounds_uses_cache_within_ttl(subscription: Subscription) -> None:
    panel = FakePanel(inbounds=[make_inbound(1)])
    first = subscription.panel_svc.getinbounds(cast(XUiSession, panel))
    second = subscription.panel_svc.getinbounds(cast(XUiSession, panel))
    assert [i.id for i in first] == [1]
    assert [i.id for i in second] == [1]
    # the second call must hit the cache, not the panel
    assert panel.gets.count("panel/api/inbounds/list") == 1


def test_getinbounds_requeries_after_ttl_expiry(subscription: Subscription) -> None:
    panel = FakePanel(inbounds=[make_inbound(1)])
    subscription.panel_svc.getinbounds(cast(XUiSession, panel))
    # age the cache past the local TTL of 2 seconds
    panel._cache_set_at -= 10  # pyright: ignore[reportPrivateUsage]
    subscription.panel_svc.getinbounds(cast(XUiSession, panel))
    assert panel.gets.count("panel/api/inbounds/list") == 2
