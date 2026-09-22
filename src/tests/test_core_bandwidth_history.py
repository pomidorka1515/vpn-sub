from __future__ import annotations

import time

from db import Database
from helpers import create_alice, make_subscription


def _state_payload(ts: int) -> dict[str, object]:
    return {
        "ts": ts,
        "host": {
            "cpu": 1.0,
            "process_count": 2,
            "uptime": 3.0,
            "cpu_info": {"cores": 4, "name": "cpu", "mhz_max": 1000},
            "loadavg": {"load_1m": 0.1, "load_5m": 0.2, "load_15m": 0.3},
            "network": {"sent": 1, "recv": 2},
            "memory": {
                "ram": {"total": 1, "available": 1, "used": 0},
                "swap": {"total": 0, "free": 0, "used": 0},
            },
            "ip": {"ipv4": ["127.0.0.1"], "ipv6": None},
            "connections": {"tcp": 1, "udp": 0},
            "app_memory": {"ram": 1.0, "swap": 0.0},
            "app_uptime": 1.0,
            "app_thread_amount": 1,
            "app_threads": [{"name": "main", "ident": 1, "daemon": False}],
            "app_gc_stats": {
                "gc_counts": [1, 0, 0],
                "gc_thresholds": [1, 1, 1],
                "gc_stats": [
                    {"collections": 1, "collected": 0, "uncollectable": 0},
                ],
            },
        },
        "panels": {},
    }


def test_get_bw_history_clamps_to_window(database: Database) -> None:
    subscription = make_subscription(database)
    create_alice(database)
    now = int(time.time())
    database.add_bandwidth_snapshot("alice", now - 40 * 86400, 1, 2, 3, 4)
    database.add_bandwidth_snapshot("alice", now - 2 * 86400, 5, 6, 7, 8)
    history = subscription.bandwidth_svc.get_bw_history("alice", days=30)
    assert len(history) == 1
    assert (history[0].up, history[0].down, history[0].wl_up, history[0].wl_down) == (
        5, 6, 7, 8,
    )


def test_get_snapshots_hydrates_state_rows(database: Database) -> None:
    subscription = make_subscription(database)
    now = int(time.time())
    database.upsert_state_snapshot(now - 2 * 86400, _state_payload(now - 2 * 86400))
    database.upsert_state_snapshot(now - 40 * 86400, _state_payload(now - 40 * 86400))
    snapshots = subscription.bandwidth_svc.get_snapshots(days=30)
    assert len(snapshots) == 1
    assert snapshots[0].host.process_count == 2
    assert snapshots[0].panels == {}
