from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from helpers import create_alice, make_subscription

from custom_types import BandwidthInfo

if TYPE_CHECKING:

    from db import Database


@pytest.mark.asyncio
async def test_leaderboard_total_and_monthly_ranking(database: Database) -> None:
    subscription = (await make_subscription(database))
    (await create_alice(database, bw_limit_gb=1, wl_limit_gb=1))
    (await database.increment_usage("alice", regular=10, whitelist=4))
    (await database.create_user(
        username="bob",
        uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        token="b" * 40,
        fingerprint="chrome",
        displayname="Bob",
        bw_limit_gb=2,
        wl_limit_gb=0,
    ))
    (await database.create_user(
        username="cara",
        uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        token="c" * 40,
        fingerprint="chrome",
        displayname="Cara",
        bw_limit_gb=0,
        wl_limit_gb=3,
    ))
    (await database.increment_usage("bob", regular=30, whitelist=1))
    (await database.increment_usage("cara", regular=5, whitelist=20))

    totals = {"alice": 100, "bob": 50, "cara": 300}

    async def all_traffic(whitelist: bool = False, *, work: str = "foreground") -> dict[str, BandwidthInfo]:
        assert whitelist is False
        assert work == "background"
        return {user: BandwidthInfo(total, 0, total) for user, total in totals.items()}

    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]

    assert (await subscription.leaderboard_svc.leaderboard("total")) == {
        "cara": 300,
        "alice": 100,
        "bob": 50,
    }
    assert (await subscription.leaderboard_svc.leaderboard("total", top_n=2)) == {
        "cara": 300,
        "alice": 100,
    }
    assert (await subscription.leaderboard_svc.leaderboard("total", flip=True, top_n=1)) == {
        "bob": 50,
    }
    assert (await subscription.leaderboard_svc.leaderboard("monthly")) == {
        "bob": 30,
        "alice": 10,
    }
    assert (await subscription.leaderboard_svc.leaderboard("wl_monthly")) == {
        "cara": 20,
        "alice": 4,
    }
    assert (await subscription.leaderboard_svc.leaderboard(
        "monthly",
        use_displaynames=True,
    )) == {"Bob": 30, "Alice": 10}
