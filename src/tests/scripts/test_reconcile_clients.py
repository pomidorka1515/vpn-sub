from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import pytest_asyncio
from aiohttp import web

from db import Database
from session import XUiSession
from tests.support.panels import FakePanel, PanelTransport, make_inbound, make_panel_client
from tests.support.paths import REPO_ROOT
from tests.support.users import TOKEN_A, USER_UUID, create_alice
from threadutil import named_to_thread

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from reconcile_clients import PanelReport, SeedIntent

    from config import JsonValue, PanelConfig

_SCRIPT_PATH = REPO_ROOT / "scripts" / "reconcile_clients.py"
_SPEC = importlib.util.spec_from_file_location("reconcile_clients", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
rc = importlib.util.module_from_spec(_SPEC)
sys.modules["reconcile_clients"] = rc  # dataclasses resolves types via sys.modules
_SPEC.loader.exec_module(rc)


@pytest.mark.parametrize("source", ["fallback", "data", "relative_data", "env", "cli"])
def test_main_path_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rc, "_ROOT", tmp_path)
    for name in ("DIR_DATA", "PATH_CONFIG", "PATH_DB"):
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    if source != "fallback":
        data = Path("relative") if source == "relative_data" else tmp_path / "deployment"
        monkeypatch.setenv("DIR_DATA", str(data))
    config = data / "config.json"
    database = data / "state.sqlite3"
    if source in {"env", "cli"}:
        config = tmp_path / "env.json"
        database = tmp_path / "env.sqlite3"
        monkeypatch.setenv("PATH_CONFIG", str(config))
        monkeypatch.setenv("PATH_DB", str(database))
    args: list[str] = []
    if source == "cli":
        config = tmp_path / "cli.json"
        database = tmp_path / "cli.sqlite3"
        args = ["--config", str(config), "--database", str(database)]
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"3xui": {}}), encoding="utf-8")

    async def run(
        panels: dict[str, PanelConfig],
        keys: list[str],
        path: Path,
        *,
        apply: bool,
    ) -> int:
        assert panels == {}
        assert keys == []
        assert path == database
        assert not apply
        return 0

    monkeypatch.setattr(rc, "_run", run)
    assert rc.main(args) == 0


@pytest_asyncio.fixture
async def panel_client() -> AsyncIterator[Callable[[FakePanel], XUiSession]]:
    clients: list[XUiSession] = []

    def build(panel: FakePanel) -> XUiSession:
        client = XUiSession(
            name=panel.name,
            address="127.0.0.1",
            port=8080,
            uri="",
            api_token="test",
            transport=PanelTransport(panel),
        )
        clients.append(client)
        return client

    try:
        yield build
    finally:
        for client in clients:
            await client.close()


async def _reconcile(panel: XUiSession, **kwargs: object) -> PanelReport:
    await panel.start()
    return cast("PanelReport", await rc.reconcile_panel(panel, {USER_UUID: "alice"}, **kwargs))


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


@pytest.mark.asyncio
async def test_reconcile_dry_run_reports_without_mutating(
    panel_client: Callable[[FakePanel], XUiSession],
) -> None:
    panel = _legacy_panel()
    report = await _reconcile(panel_client(panel), apply=False)

    assert report.created == ["alice"]
    assert report.deleted == ["alice-aaaaaaaa", "alice-bbbbbbbb"]
    assert report.seeded["alice"] == (101, 202)
    assert report.errors == []
    assert panel.posts == []  # dry-run: not a single mutation


@pytest.mark.asyncio
async def test_reconcile_apply_merges_legacy_rows(
    panel_client: Callable[[FakePanel], XUiSession],
) -> None:
    panel = _legacy_panel()
    report = await _reconcile(panel_client(panel), apply=True)

    assert report.created == ["alice"]
    assert _post_urls(panel).count("panel/api/clients/add") == 1
    add_body = cast(dict[str, object], panel.posts[0][1]["json"])
    client = cast(dict[str, object], add_body["client"])
    assert client["email"] == "alice"
    assert client["id"] == USER_UUID
    assert client["flow"] == "xtls-rprx-vision"
    assert (client["limitIp"], client["totalGB"]) == (0, 0)
    # union of the group's inbounds
    assert add_body["inboundIds"] == [1, 2]
    assert "panel/api/clients/updateTraffic/alice" in _post_urls(panel)
    seed_body = cast(dict[str, object], panel.posts[1][1]["json"])
    assert (seed_body["upload"], seed_body["download"]) == (101, 202)
    assert "panel/api/clients/del/alice-aaaaaaaa" in _post_urls(panel)
    assert "panel/api/clients/del/alice-bbbbbbbb" in _post_urls(panel)

    # one canonical client remains, seeded with the summed counters
    assert [c.email for c in panel.clients] == ["alice"]
    assert panel.clients[0].inboundIds == [1, 2]
    assert panel.clients[0].traffic is not None
    assert (panel.clients[0].traffic.up, panel.clients[0].traffic.down) == (101, 202)


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(panel_client: Callable[[FakePanel], XUiSession]) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[make_panel_client("alice", [1, 2], up=101, down=202)],
    )
    report = await _reconcile(panel_client(panel), apply=True)

    assert report.created == []
    assert report.attached == {}
    assert report.seeded == {}
    assert report.deleted == []
    assert panel.posts == []


@pytest.mark.asyncio
async def test_reconcile_attaches_missing_inbounds_and_seeds_drift(
    panel_client: Callable[[FakePanel], XUiSession],
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[
            make_panel_client("alice", [1], up=50, down=60),
            make_panel_client("alice-bbbbbbbb", [2], up=3, down=4),
        ],
    )
    report = await _reconcile(panel_client(panel), apply=True)

    assert report.created == []
    assert report.attached == {"alice": [2]}
    assert report.seeded == {"alice": (53, 64)}
    assert report.deleted == ["alice-bbbbbbbb"]
    assert "panel/api/clients/alice/attach" in _post_urls(panel)
    assert panel.clients[0].inboundIds == [1, 2]
    assert panel.clients[0].traffic is not None
    assert (panel.clients[0].traffic.up, panel.clients[0].traffic.down) == (53, 64)


@pytest.mark.asyncio
async def test_reconcile_reports_unknown_uuids_without_touching_them(
    panel_client: Callable[[FakePanel], XUiSession],
) -> None:
    stranger_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("stranger-aaaa", [1], uuid=stranger_uuid)],
    )
    report = await _reconcile(panel_client(panel), apply=True)

    assert report.unknown == {stranger_uuid: ["stranger-aaaa"]}
    assert report.errors == []
    assert panel.posts == []


@pytest.mark.asyncio
async def test_reconcile_skips_user_without_managed_vless_inbounds(
    panel_client: Callable[[FakePanel], XUiSession],
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1, protocol="trojan")],
        clients=[make_panel_client("alice-aaaaaaaa", [1])],
    )
    report = await _reconcile(panel_client(panel), apply=True)

    assert report.created == []
    assert report.skipped == ["alice: no managed VLESS inbounds on panel panel"]
    assert panel.posts == []


@pytest.mark.asyncio
async def test_reconcile_surfaces_panel_rejection(
    panel_client: Callable[[FakePanel], XUiSession],
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice-aaaaaaaa", [1])],
        post_payload={"success": False, "msg": "duplicate email", "obj": None},
    )
    report = await _reconcile(panel_client(panel), apply=True)

    assert report.errors == ["panel panel: client add for alice rejected: duplicate email"]


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"3xui": {"p1": {"name": "panel"}}}),
        encoding="utf-8",
    )
    return config_path


@pytest.mark.asyncio
async def test_main_is_dry_run_by_default(
    tmp_path: Path,
    database: Database,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = _legacy_panel()

    def fake_build_panel(name: str, cfg: PanelConfig, *, stamp_dir: str) -> XUiSession:
        return XUiSession(
            name=panel.name,
            address="127.0.0.1",
            port=8080,
            uri="",
            api_token="test",
            transport=PanelTransport(panel),
        )

    monkeypatch.setattr(rc, "_build_panel", fake_build_panel)
    await create_alice(database)

    code = await named_to_thread(
        "test:reconcile:cli",
        rc.main,
        [
            "--config",
            str(_write_config(tmp_path)),
            "--database",
            str(db_path),
            "--panel",
            "p1",
        ],
    )

    assert code == 0
    assert panel.posts == []


def test_main_rejects_unknown_panel_key(
    tmp_path: Path,
    database: Database,
    db_path: Path,
    monkeypatch: object,
) -> None:
    code = rc.main(
        [
            "--config",
            str(_write_config(tmp_path)),
            "--database",
            str(db_path),
            "--panel",
            "nope",
        ],
    )
    assert code == 2


class RecordingLedger:
    """SeedLedger double: records folds and clears."""

    def __init__(self) -> None:
        self.folded: dict[tuple[str, str], tuple[int, int]] = {}
        self.cleared: list[tuple[str, str]] = []
        self.intents: dict[tuple[str, str], SeedIntent] = {}

    async def get(self, panel: str, username: str) -> tuple[int, int] | None:
        return self.folded.get((panel, username))

    async def set(self, panel: str, username: str, up: int, down: int) -> None:
        self.folded[panel, username] = (up, down)

    async def clear(self, panel: str, username: str) -> None:
        self.folded.pop((panel, username), None)
        self.cleared.append((panel, username))

    async def get_intent(self, panel: str, username: str) -> SeedIntent | None:
        return self.intents.get((panel, username))

    async def set_intent(self, panel: str, username: str, intent: SeedIntent) -> None:
        self.intents[panel, username] = intent

    async def clear_intent(self, panel: str, username: str) -> None:
        self.intents.pop((panel, username), None)


@pytest.mark.asyncio
async def test_rerun_after_failed_delete_folds_only_new_traffic(
    panel_client: Callable[[FakePanel], XUiSession],
) -> None:
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
    ledger = RecordingLedger()
    await ledger.set("panel", "alice", 101, 202)  # first run's fold

    report = await _reconcile(
        panel_client(panel),
        apply=True,
        ledger=ledger,
    )

    seed_urls = [u for u, _ in panel.posts if "updateTraffic/alice" in u]
    assert len(seed_urls) == 1
    seed_body = cast(
        dict[str, object],
        next(kw["json"] for u, kw in panel.posts if "updateTraffic/alice" in u),
    )
    assert (seed_body["upload"], seed_body["download"]) == (111, 202)
    assert ledger.folded["panel", "alice"] == (111, 202)
    # the delete failure is reported, not swallowed
    assert report.errors


@pytest.mark.asyncio
async def test_ledger_cleared_when_no_legacy_rows_remain(
    panel_client: Callable[[FakePanel], XUiSession],
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1], up=101, down=202)],
    )
    ledger = RecordingLedger()
    await ledger.set("panel", "alice", 101, 202)

    report = await _reconcile(
        panel_client(panel),
        apply=True,
        ledger=ledger,
    )

    assert ("panel", "alice") in ledger.cleared
    assert report.seeded == {}
    assert panel.posts == []


@pytest.mark.asyncio
async def test_db_seed_ledger_round_trip(database: Database) -> None:
    ledger = rc.DbSeedLedger(database)

    assert await ledger.get("panel", "alice") is None
    await ledger.set("panel", "alice", 101, 202)
    assert await ledger.get("panel", "alice") == (101, 202)
    await ledger.clear("panel", "alice")
    assert await ledger.get("panel", "alice") is None
    intent = rc.SeedIntent((101, 202), (111, 222))
    await ledger.set_intent("panel", "alice", intent)
    assert await rc.DbSeedLedger(database).get_intent("panel", "alice") == intent
    await ledger.clear_intent("panel", "alice")
    assert await ledger.get_intent("panel", "alice") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["intent", "before_write", "lost_ack", "commit", "clear"])
async def test_seed_intent_recovers_partial_failure_without_double_counting(
    panel_client: Callable[[FakePanel], XUiSession],
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[
            make_panel_client("alice", [1], up=10, down=20),
            make_panel_client("alice-aaaaaaaa", [1], up=100, down=200),
        ],
    )
    ledger = rc.DbSeedLedger(database)
    original_post = rc._post
    original_set = database.set_metadata
    original_delete = database.delete_metadata

    async def interrupted_post(
        client: XUiSession,
        url: str,
        body: dict[str, JsonValue],
        what: str,
    ) -> None:
        if failure == "before_write":
            raise RuntimeError("interrupted")
        await original_post(client, url, body, what)
        if failure == "lost_ack":
            raise RuntimeError("interrupted")

    async def interrupted_set(key: str, value: str) -> None:
        if (failure == "intent" and key.endswith(":intent")) or (
            failure == "commit" and not key.endswith(":intent")
        ):
            raise RuntimeError("interrupted")
        await original_set(key, value)

    async def interrupted_delete(key: str) -> None:
        if failure == "clear" and key.endswith(":intent"):
            raise RuntimeError("interrupted")
        await original_delete(key)

    with monkeypatch.context() as patch:
        patch.setattr(rc, "_post", interrupted_post)
        patch.setattr(database, "set_metadata", interrupted_set)
        patch.setattr(database, "delete_metadata", interrupted_delete)
        with pytest.raises(RuntimeError, match="interrupted"):
            await _reconcile(panel_client(panel), apply=True, ledger=ledger)

    pending = await ledger.get_intent("panel", "alice")
    if failure == "intent":
        assert pending is None
        assert panel.posts == []
    else:
        assert pending == rc.SeedIntent((100, 200), (110, 220))
    assert len(panel.clients) == 2  # no deletion before the seed is committed

    # A new ledger instance reads the durable intent. Dry-run must not consume it.
    recovered_ledger = rc.DbSeedLedger(database)
    posts = len(panel.posts)
    dry_report = await _reconcile(panel_client(panel), apply=False, ledger=recovered_ledger)
    assert dry_report.seeded == {"alice": (110, 220)}
    assert len(panel.posts) == posts
    assert await recovered_ledger.get_intent("panel", "alice") == pending

    report = await _reconcile(panel_client(panel), apply=True, ledger=recovered_ledger)
    assert report.errors == []
    assert [client.email for client in panel.clients] == ["alice"]
    traffic = panel.clients[0].traffic
    assert traffic is not None
    assert (traffic.up, traffic.down) == (110, 220)  # never 210 / 420
    assert await recovered_ledger.get_intent("panel", "alice") is None
    posts = len(panel.posts)
    await _reconcile(panel_client(panel), apply=True, ledger=recovered_ledger)
    assert len(panel.posts) == posts


@pytest.mark.asyncio
async def test_pending_seed_resolved_before_folding_new_traffic(
    panel_client: Callable[[FakePanel], XUiSession],
    database: Database,
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[
            make_panel_client("alice", [1], up=113, down=223),
            make_panel_client("alice-aaaaaaaa", [1], up=105, down=207),
        ],
    )
    ledger = rc.DbSeedLedger(database)
    await ledger.set_intent("panel", "alice", rc.SeedIntent((100, 200), (110, 220)))
    report = await _reconcile(panel_client(panel), apply=True, ledger=ledger)
    assert report.errors == []
    assert report.seeded == {"alice": (118, 230)}
    seed_bodies = [kw["json"] for url, kw in panel.posts if "updateTraffic" in url]
    assert seed_bodies == [{"upload": 118, "download": 230}]


@pytest.mark.asyncio
async def test_rejected_seed_recovery_keeps_intent_and_legacy_rows(
    panel_client: Callable[[FakePanel], XUiSession],
    database: Database,
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[
            make_panel_client("alice", [1], up=10, down=20),
            make_panel_client("alice-aaaaaaaa", [1], up=105, down=207),
        ],
        post_queue=[{"success": False, "msg": "try later", "obj": None}],
    )
    ledger = rc.DbSeedLedger(database)
    intent = rc.SeedIntent((100, 200), (110, 220))
    await ledger.set_intent("panel", "alice", intent)
    report = await _reconcile(panel_client(panel), apply=True, ledger=ledger)
    assert report.errors
    assert len(panel.clients) == 2
    assert await ledger.get_intent("panel", "alice") == intent
    assert await ledger.get("panel", "alice") is None

    report = await _reconcile(panel_client(panel), apply=True, ledger=ledger)
    assert report.errors == []
    seed_bodies = [kw["json"] for url, kw in panel.posts if "updateTraffic" in url]
    assert seed_bodies == [
        {"upload": 110, "download": 220},  # rejected recovery
        {"upload": 110, "download": 220},  # exact target, not current + legacy
        {"upload": 115, "download": 227},  # only then fold new traffic
    ]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cli_run_against_panel_endpoint_preserves_usage_and_reruns(
    tmp_path: Path,
    unused_tcp_port: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    panel = _legacy_panel()
    database_path = tmp_path / "test.sqlite3"
    database = Database(path=database_path, start_backup=False)
    await database.start()
    try:
        await database.create_user(
            username="alice",
            uuid=USER_UUID,
            token=TOKEN_A,
            fingerprint="chrome",
            displayname="Alice",
        )
        before = await database.get_user("alice")
    finally:
        await database.close()

    async def endpoint(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer test"
        route = request.path.lstrip("/")
        response = (
            panel.get(route)
            if request.method == "GET"
            else panel.post(route, json=await request.json())
        )
        return web.Response(
            body=response.content,
            status=response.status_code,
            content_type="application/json",
        )

    app = web.Application()
    app.router.add_route("*", "/{route:.*}", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
        cfg: PanelConfig = {
            "name": "panel",
            "address": "127.0.0.1",
            "port": unused_tcp_port,
            "uri": "",
            "token": "test",
            "https": False,
            "inbounds_list": [],
            "mode": "blacklist",
            "whitelist": False,
        }
        assert await rc._run({"p1": cfg}, ["p1"], database_path, apply=False) == 0
        assert panel.posts == []
        assert "dry run — no changes applied" in capsys.readouterr().out

        assert await rc._run({"p1": cfg}, ["p1"], database_path, apply=True) == 0
        assert [client.email for client in panel.clients] == ["alice"]
        posts = len(panel.posts)
        assert await rc._run({"p1": cfg}, ["p1"], database_path, apply=True) == 0
        assert len(panel.posts) == posts
    finally:
        await runner.cleanup()

    reopened = Database(path=database_path, start_backup=False)
    await reopened.start()
    try:
        assert await reopened.get_user("alice") == before
        assert await reopened.get_metadata("reconcile_seed:panel:alice") is None
    finally:
        await reopened.close()
