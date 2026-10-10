from __future__ import annotations

import sqlite3

from helpers import create_alice, make_subscription

from custom_types import BandwidthInfo
from db import Database


def test_leaderboard_total_and_monthly_ranking(database: Database) -> None:
    subscription = make_subscription(database)
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
    database.increment_usage("alice", regular=10, whitelist=4)
    database.create_user(
        username="bob", uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        token="b" * 40, fingerprint="chrome", displayname="Bob",
        bw_limit_gb=2, wl_limit_gb=0,
    )
    database.create_user(
        username="cara", uuid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        token="c" * 40, fingerprint="chrome", displayname="Cara",
        bw_limit_gb=0, wl_limit_gb=3,
    )
    database.increment_usage("bob", regular=30, whitelist=1)
    database.increment_usage("cara", regular=5, whitelist=20)

    totals = {"alice": 100, "bob": 50, "cara": 300}

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        assert whitelist is False
        assert pool is not None
        return {
            user: BandwidthInfo(total, 0, total)
            for user, total in totals.items()
        }

    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]

    assert subscription.leaderboard_svc.leaderboard("total") == {
        "cara": 300, "alice": 100, "bob": 50,
    }
    assert subscription.leaderboard_svc.leaderboard("total", top_n=2) == {
        "cara": 300, "alice": 100,
    }
    assert subscription.leaderboard_svc.leaderboard("total", flip=True, top_n=1) == {
        "bob": 50,
    }
    assert subscription.leaderboard_svc.leaderboard("monthly") == {
        "bob": 30, "alice": 10,
    }
    assert subscription.leaderboard_svc.leaderboard("wl_monthly") == {
        "cara": 20, "alice": 4,
    }
    assert subscription.leaderboard_svc.leaderboard(
        "monthly", use_displaynames=True,
    ) == {"Bob": 30, "Alice": 10}


def test_monthly_leaderboard_reads_users_once(database: Database) -> None:
    subscription = make_subscription(database)
    create_alice(database, bw_limit_gb=1)
    database.create_user(
        username="bob", uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        token="b" * 40, fingerprint="chrome", displayname="Bob",
        bw_limit_gb=1,
    )
    seen: list[str] = []
    original = database._connect

    def traced() -> sqlite3.Connection:
        conn = original()
        conn.set_trace_callback(seen.append)
        return conn

    database._connect = traced  # type: ignore[method-assign]
    try:
        ranked = subscription.leaderboard_svc.leaderboard("monthly", use_displaynames=True)
    finally:
        database._connect = original  # type: ignore[method-assign]

    assert ranked == {"Bob": 0, "Alice": 0}
    assert len([q for q in seen if q.startswith("SELECT")]) == 1
