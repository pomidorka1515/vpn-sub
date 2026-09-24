from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, cast

from db import Database
from helpers import USER_UUID, FakePanel, create_alice, make_inbound, make_panel_client
from session import XUiSession

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "reconcile_clients.py"
_SPEC = importlib.util.spec_from_file_location("reconcile_clients", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
rc = importlib.util.module_from_spec(_SPEC)
sys.modules["reconcile_clients"] = rc  # dataclasses resolves types via sys.modules
_SPEC.loader.exec_module(rc)


def _legacy_panel() -> FakePanel:
    """Panel as the v3 DB migration leaves it: one row per legacy email."""
    return FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[
            make_panel_client("alice-aaaaaaaa", [1], up=100, down=200),
            make_panel_client("alice-bbbbbbbb", [2], up=1, down=2),
        ],
    )


def _post_urls(panel: FakePanel) -> list[str]:
    return [url for url, _ in panel.posts]


def test_reconcile_dry_run_reports_without_mutating(database: Database) -> None:
    panel = _legacy_panel()
    create_alice(database)

    report = rc.reconcile_panel(cast(XUiSession, panel), {USER_UUID: "alice"}, apply=False)

    assert report.created == ["alice"]
    assert report.deleted == ["alice-aaaaaaaa", "alice-bbbbbbbb"]
    assert report.seeded["alice"] == (101, 202)
    assert report.errors == []
    assert panel.posts == []  # dry-run: not a single mutation


def test_reconcile_apply_merges_legacy_rows(database: Database) -> None:
    panel = _legacy_panel()
    create_alice(database)

    report = rc.reconcile_panel(cast(XUiSession, panel), {USER_UUID: "alice"}, apply=True)

    assert report.created == ["alice"]
    assert _post_urls(panel).count("panel/api/clients/add") == 1
    add_body = cast(dict[str, Any], panel.posts[0][1]["json"])
    assert add_body["client"]["email"] == "alice"
    assert add_body["client"]["id"] == USER_UUID
    assert add_body["client"]["flow"] == "xtls-rprx-vision"
    assert (add_body["client"]["limitIp"], add_body["client"]["totalGB"]) == (0, 0)
    # union of the group's inbounds
    assert add_body["inboundIds"] == [1, 2]
    assert "panel/api/clients/updateTraffic/alice" in _post_urls(panel)
    seed_body = cast(dict[str, Any], panel.posts[1][1]["json"])
    assert (seed_body["upload"], seed_body["download"]) == (101, 202)
    assert "panel/api/clients/del/alice-aaaaaaaa" in _post_urls(panel)
    assert "panel/api/clients/del/alice-bbbbbbbb" in _post_urls(panel)

    # one canonical client remains, seeded with the summed counters
    assert [c.email for c in panel.clients] == ["alice"]
    assert panel.clients[0].inboundIds == [1, 2]
    assert panel.clients[0].traffic is not None
    assert (panel.clients[0].traffic.up, panel.clients[0].traffic.down) == (101, 202)


def test_reconcile_is_idempotent(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[make_panel_client("alice", [1, 2], up=101, down=202)],
    )
    create_alice(database)

    report = rc.reconcile_panel(cast(XUiSession, panel), {USER_UUID: "alice"}, apply=True)

    assert report.created == []
    assert report.attached == {}
    assert report.seeded == {}
    assert report.deleted == []
    assert panel.posts == []


def test_reconcile_attaches_missing_inbounds_and_seeds_drift(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[
            make_panel_client("alice", [1], up=50, down=60),
            make_panel_client("alice-bbbbbbbb", [2], up=3, down=4),
        ],
    )
    create_alice(database)

    report = rc.reconcile_panel(cast(XUiSession, panel), {USER_UUID: "alice"}, apply=True)

    assert report.created == []
    assert report.attached == {"alice": [2]}
    assert report.seeded == {"alice": (53, 64)}
    assert report.deleted == ["alice-bbbbbbbb"]
    assert "panel/api/clients/alice/attach" in _post_urls(panel)
    assert panel.clients[0].inboundIds == [1, 2]
    assert panel.clients[0].traffic is not None
    assert (panel.clients[0].traffic.up, panel.clients[0].traffic.down) == (53, 64)


def test_reconcile_reports_unknown_uuids_without_touching_them(database: Database) -> None:
    stranger_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("stranger-aaaa", [1], uuid=stranger_uuid)],
    )
    create_alice(database)

    report = rc.reconcile_panel(cast(XUiSession, panel), {USER_UUID: "alice"}, apply=True)

    assert report.unknown == {stranger_uuid: ["stranger-aaaa"]}
    assert report.errors == []
    assert panel.posts == []


def test_reconcile_skips_user_without_managed_vless_inbounds(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1, protocol="trojan")],
        clients=[make_panel_client("alice-aaaaaaaa", [1])],
    )
    create_alice(database)

    report = rc.reconcile_panel(cast(XUiSession, panel), {USER_UUID: "alice"}, apply=True)

    assert report.created == []
    assert report.skipped == ["alice: no managed VLESS inbounds on panel panel"]
    assert panel.posts == []


def test_reconcile_surfaces_panel_rejection(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice-aaaaaaaa", [1])],
        post_payload={"success": False, "msg": "duplicate email", "obj": None},
    )
    create_alice(database)

    report = rc.reconcile_panel(cast(XUiSession, panel), {USER_UUID: "alice"}, apply=True)

    assert report.errors == ["panel panel: client add for alice rejected: duplicate email"]


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"3xui": {"p1": {"name": "panel"}}}), encoding="utf-8",
    )
    return config_path


def test_main_is_dry_run_by_default(
    tmp_path: Path, database: Database, db_path: Path, monkeypatch: Any,
) -> None:
    panel = _legacy_panel()

    def fake_build_panel(name: str, cfg: dict[str, Any]) -> XUiSession:
        return cast(XUiSession, panel)

    monkeypatch.setattr(rc, "_build_panel", fake_build_panel)
    create_alice(database)

    code = rc.main([
        "--config", str(_write_config(tmp_path)),
        "--database", str(db_path),
        "--panel", "p1",
    ])

    assert code == 0
    assert panel.posts == []


def test_main_rejects_unknown_panel_key(
    tmp_path: Path, database: Database, db_path: Path, monkeypatch: Any,
) -> None:
    create_alice(database)
    code = rc.main([
        "--config", str(_write_config(tmp_path)),
        "--database", str(db_path),
        "--panel", "nope",
    ])
    assert code == 2


class RecordingLedger:
    """SeedLedger double: records folds and clears."""

    def __init__(self) -> None:
        self.folded: dict[tuple[str, str], tuple[int, int]] = {}
        self.cleared: list[tuple[str, str]] = []

    def get(self, panel: str, username: str) -> tuple[int, int] | None:
        return self.folded.get((panel, username))

    def set(self, panel: str, username: str, up: int, down: int) -> None:
        self.folded[(panel, username)] = (up, down)

    def clear(self, panel: str, username: str) -> None:
        self.folded.pop((panel, username), None)
        self.cleared.append((panel, username))


def test_rerun_after_failed_delete_folds_only_new_traffic(database: Database) -> None:
    """Partial failure (legacy delete rejected) must not re-fold old traffic.

    First run seeded the canonical client with the group total; the legacy
    row survived and later accrued 10 more bytes. The rerun folds exactly
    the increase, never the full legacy sum again.
    """
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[
            make_panel_client("alice", [1], up=101, down=202),
            make_panel_client("alice-aaaaaaaa", [1], up=111, down=202),
        ],
        # seed succeeds, the legacy delete after it fails
        post_queue=[
            {"success": True, "msg": "", "obj": None},
            {"success": False, "msg": "delete rejected", "obj": None},
        ],
    )
    create_alice(database)
    ledger = RecordingLedger()
    ledger.set("panel", "alice", 101, 202)  # first run's fold

    report = rc.reconcile_panel(
        cast(XUiSession, panel), {USER_UUID: "alice"}, apply=True, ledger=ledger,
    )

    seed_urls = [u for u, _ in panel.posts if "updateTraffic/alice" in u]
    assert len(seed_urls) == 1
    seed_body = cast(
        dict[str, Any],
        next(kw["json"] for u, kw in panel.posts if "updateTraffic/alice" in u),
    )
    assert (seed_body["upload"], seed_body["download"]) == (111, 202)
    assert ledger.folded[("panel", "alice")] == (111, 202)
    # the delete failure is reported, not swallowed
    assert report.errors


def test_ledger_cleared_when_no_legacy_rows_remain(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1], up=101, down=202)],
    )
    create_alice(database)
    ledger = RecordingLedger()
    ledger.set("panel", "alice", 101, 202)

    report = rc.reconcile_panel(
        cast(XUiSession, panel), {USER_UUID: "alice"}, apply=True, ledger=ledger,
    )

    assert ("panel", "alice") in ledger.cleared
    assert report.seeded == {}
    assert panel.posts == []


def test_db_seed_ledger_round_trip(database: Database) -> None:
    ledger = rc.DbSeedLedger(database)

    assert ledger.get("panel", "alice") is None
    ledger.set("panel", "alice", 101, 202)
    assert ledger.get("panel", "alice") == (101, 202)
    ledger.clear("panel", "alice")
    assert ledger.get("panel", "alice") is None
