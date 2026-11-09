from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast

import pytest
import pytest_asyncio

from api import AsyncApi
from api.config_patch import REQUIRED_KEYS, config_etag
from config import AppConfig, Config, JsonValue
from config.store import AsyncConfig, AsyncLinesConfig
from config.validation import check_config_document
from core import Subscription
from core.services.audit import AuditService
from errors import ValidationError
from tests.support.api import api_app, make_subscription, make_watch
from tests.support.paths import REPO_ROOT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, MutableMapping
    from pathlib import Path

    from quart import Quart
    from quart.wrappers import Response

    from db import Database

ROOT = REPO_ROOT
HEADERS = {"Authorization": "secret-token-at-least-20"}


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, JsonValue]] = []

    async def append(self, record: dict[str, JsonValue]) -> None:
        self.records.append(record)


@pytest_asyncio.fixture
async def config_api(
    tmp_path: Path,
    database: Database,
) -> AsyncIterator[tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit]]:
    path = tmp_path / "config.json"
    data = json.loads((ROOT / "docs/EXAMPLE.config.json").read_text())
    data.update(api_token=HEADERS["Authorization"], sub_name="before", funny_strings=["keep-me"])
    path.write_text(json.dumps(data, indent=4) + "\n")
    async with api_app() as app:
        sub = await make_subscription(database, app=app)
        cfg = AsyncConfig[AppConfig](
            lambda: Config[AppConfig](
                path=path,
                schema_path=ROOT / "config.schema.json",
                validators=(check_config_document,),
                backup_dir=tmp_path / "backup",
                indent=4,
                sync_mode="none",
                start_backup=False,
            ),
            files=sub.res.offloads.files,
        )
        await cfg.start()
        audit = RecordingAudit()
        sub.audit_svc = AuditService(cast(AsyncLinesConfig, audit))
        AsyncApi(
            app=app,
            cfg=cfg,
            audit_cfg=cast(AsyncLinesConfig, audit),
            sub=sub,
            bw=make_watch(database, sub),
            limiter=app.extensions["limiter"],
        )
        try:
            yield app, cfg, path, audit
        finally:
            await cfg.close()


def test_required_keys_match_schema() -> None:
    assert list(REQUIRED_KEYS) == json.loads((ROOT / "config.schema.json").read_text())["required"]


async def set_config(app: Quart, base: str, values: dict[str, JsonValue]) -> Response:
    return await app.test_client().post(
        "/sub/privapi/api/config/set",
        json={"base": base, "values": values},
        headers=HEADERS,
    )


def base_for(path: Path) -> str:
    return config_etag(cast(dict[str, JsonValue], json.loads(path.read_bytes())))


@pytest.mark.asyncio
async def test_config_rejects_multiple_whitelist_panels(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
) -> None:
    app, _, path, _ = config_api
    document = json.loads(path.read_text())
    panels = document["3xui"]
    panel = next(iter(panels.values()))
    panel["whitelist"] = True
    panels["second"] = {**panel, "name": "second"}
    before = path.read_bytes()
    response = await set_config(app, base_for(path), {"3xui": panels})
    assert response.status_code == 400
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_config_get_contract(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
) -> None:
    app, _, path, _ = config_api
    client = app.test_client()
    assert (await client.get("/sub/privapi/api/config/get")).status_code == 401
    assert (await client.post("/sub/privapi/api/config/set", json={})).status_code == 401
    first = await client.get("/sub/privapi/api/config/get", headers=HEADERS)
    second = await client.get("/sub/privapi/api/config/get", headers=HEADERS)
    assert first.status_code == 200
    assert (await first.get_json())["obj"] == json.loads(path.read_bytes())
    body = await first.get_data(as_text=True)
    assert body.count("\n") == 1
    assert body.endswith("\n")
    assert body == json.dumps(await first.get_json(), separators=(",", ":")) + "\n"
    assert body.index('"obj":') < body.index('"$schema":')
    assert first.headers["ETag"] == second.headers["ETag"] == f'"{base_for(path)}"'
    assert first.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_rotation", [False, True])
async def test_ui_credential_rotation_revokes_browser_not_machine_auth(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
    browser_rotation: bool,
) -> None:
    app, cfg, path, _ = config_api
    client = app.test_client()
    username, password = cfg.view()["api_admin_ui_auth"]
    assert (
        await client.post(
            "/sub/admin/session",
            json={"username": username, "password": password},
        )
    ).status_code == 200
    token = (await (await client.get("/sub/admin/token")).get_json())["obj"]["token"]
    headers = {"Authorization": token}
    get_url = "/sub/privapi/api/config/get"
    assert (await client.get(get_url, headers=headers)).status_code == 200
    assert (
        await set_config(
            app,
            base_for(path),
            {
                "api_admin_ui_auth": [username, password],
            },
        )
    ).status_code == 200
    assert (
        await set_config(
            app,
            base_for(path),
            {
                "api_admin_ui_auth": [username, ""],
            },
        )
    ).status_code == 400
    assert (await client.get(get_url, headers=headers)).status_code == 200
    response = await client.post(
        "/sub/privapi/api/config/set",
        headers=headers if browser_rotation else HEADERS,
        json={"base": base_for(path), "values": {"api_admin_ui_auth": [username, "new-secret"]}},
    )
    assert response.status_code == 200
    assert (await client.get("/sub/admin/token")).status_code == 401
    assert (await client.get("/sub/admin")).status_code == 302
    assert (await client.get(get_url, headers=headers)).status_code == 401
    assert (await client.get(get_url, headers=HEADERS)).status_code == 200
    # Reverting the config must not resurrect the original stored UI cookie.
    assert (
        await set_config(app, base_for(path), {"api_admin_ui_auth": [username, password]})
    ).status_code == 200
    assert (await client.get("/sub/admin/token")).status_code == 401
    assert (
        await client.post(
            "/sub/admin/session",
            json={"username": username, "password": password},
        )
    ).status_code == 200
    fresh = (await (await client.get("/sub/admin/token")).get_json())["obj"]["token"]
    assert fresh != token
    # Machine token rotation retains the boot-captured token until restart.
    new_machine = "replacement-machine-token-12345"
    response = await set_config(app, base_for(path), {"api_token": new_machine})
    assert (await response.get_json())["obj"]["restart"] == ["api_token"]
    assert (await client.get(get_url, headers=HEADERS)).status_code == 200
    assert (await client.get(get_url, headers={"Authorization": new_machine})).status_code == 401
    visible = await client.get(get_url, headers={"Authorization": fresh})
    assert visible.status_code == 200
    assert "api_token" not in (await visible.get_json())["obj"]

    # Reconstructing the API models a serving-process restart: the configured
    # machine token becomes active, while process-bound browser authority dies.
    async with api_app() as restarted:
        sub = cast(Subscription, app.extensions["subscriptions"][0])
        AsyncApi(
            app=restarted,
            cfg=cfg,
            audit_cfg=cast(AsyncLinesConfig, config_api[3]),
            sub=sub,
            bw=make_watch(sub.res.db, sub),
            limiter=restarted.extensions["limiter"],
        )
        new_client = restarted.test_client()
        assert client.cookie_jar is not None
        cookie = next(cookie for cookie in client.cookie_jar if cookie.name == "admin_ui")
        assert cookie.value is not None
        new_client.set_cookie("localhost", "admin_ui", cookie.value, path="/sub/admin")
        assert (await new_client.get("/sub/admin/token")).status_code == 401
        assert (await new_client.get(get_url, headers={"Authorization": fresh})).status_code == 401
        assert (await new_client.get(get_url, headers=HEADERS)).status_code == 401
        assert (
            await new_client.get(get_url, headers={"Authorization": new_machine})
        ).status_code == 200


@pytest.mark.asyncio
async def test_browser_auth_checks_live_credentials_without_waiting_for_session_clear(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
) -> None:
    app, cfg, _, _ = config_api
    client = app.test_client()
    username, password = cfg.view()["api_admin_ui_auth"]
    await client.post("/sub/admin/session", json={"username": username, "password": password})
    token = (await (await client.get("/sub/admin/token")).get_json())["obj"]["token"]

    def rotate(tx: MutableMapping[str, JsonValue]) -> None:
        tx["api_admin_ui_auth"] = [username, "rotated-secret"]

    # No route-level session deletion: credentials must invalidate signatures
    # immediately on publication, including the commit-to-deletion await gap.
    await cfg.mutate(rotate)
    assert (await client.get("/sub/admin/token")).status_code == 401
    assert (
        await client.get(
            "/sub/privapi/api/config/get",
            headers={"Authorization": token},
        )
    ).status_code == 401


@pytest.mark.asyncio
async def test_config_commit_and_conflict(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
) -> None:
    app, cfg, path, audit = config_api
    original = json.loads(path.read_bytes())
    old_base = base_for(path)
    response = await set_config(app, f'"{old_base}"', {"sub_name": "after"})
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["msg"] == "Updated"
    assert payload["obj"]["restart"] == []
    assert payload["obj"]["base"] == base_for(path)
    original["sub_name"] = "after"
    assert json.loads(path.read_bytes()) == original
    assert '\n    "sub_name": "after"' in path.read_text()
    assert path.read_text().endswith("\n")
    assert cfg.view()["sub_name"] == "after"
    assert audit.records[-1]["action"] == "config_update"
    assert audit.records[-1]["info"] == {"keys": ["sub_name"]}
    assert "after" not in json.dumps(audit.records[-1]["info"])
    before = path.read_bytes()
    response = await set_config(app, old_base, {"sub_name": "lost"})
    assert response.status_code == 409
    assert (await response.get_json())["obj"]["base"] == base_for(path)
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("before", "after"), [(1, True), (True, 1), (1, 1.0), (1.0, 1)])
async def test_config_commits_nested_type_only_changes(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
    before: float | bool,
    after: float | bool,
) -> None:
    app, cfg, path, audit = config_api
    template = json.loads(path.read_bytes())["json_template"]
    template["meta"] = {"values": [{"value": before}]}
    assert (await set_config(app, base_for(path), {"json_template": template})).status_code == 200
    old_base = base_for(path)
    audit.records.clear()

    template["meta"]["values"][0]["value"] = after
    response = await set_config(app, old_base, {"json_template": template})
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["msg"] == "Updated"
    assert payload["obj"] == {"base": base_for(path), "restart": []}
    assert payload["obj"]["base"] != old_base
    live_template = cast(
        dict[str, dict[str, list[dict[str, float | bool]]]], cfg.view()["json_template"]
    )
    disk_template = json.loads(path.read_bytes())["json_template"]
    for document in (disk_template, live_template):
        value = document["meta"]["values"][0]["value"]
        assert type(value) is type(after)
        assert value == after
    assert audit.records[-1]["info"] == {"keys": ["json_template"]}

    unchanged = await set_config(app, base_for(path), {"json_template": template})
    assert (await unchanged.get_json())["msg"] == "Unchanged"
    assert len(audit.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_failure", [False, True])
async def test_config_response_retains_own_revision_during_concurrent_edit(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
    monkeypatch: pytest.MonkeyPatch,
    audit_failure: bool,
) -> None:
    app, cfg, path, audit = config_api
    committed = asyncio.Event()
    resume = asyncio.Event()
    backup_now = cfg.backup_now

    async def pause_first_backup() -> None:
        if not committed.is_set():
            committed.set()
            await resume.wait()
        await backup_now()

    monkeypatch.setattr(cfg, "backup_now", pause_first_backup)
    if audit_failure:
        append = audit.append

        async def fail_first_audit(record: dict[str, JsonValue]) -> None:
            if record["info"] == {"keys": ["sub_name"]}:
                raise OSError("audit unavailable")
            await append(record)

        monkeypatch.setattr(audit, "append", fail_first_audit)
    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        first = tasks.create_task(set_config(app, base_for(path), {"sub_name": "first"}))
        await committed.wait()
        first_base = base_for(path)
        assert cfg.view()["sub_name"] == "first"
        assert not first.done()

        second = await set_config(app, first_base, {"sub_name": "second", "3xui": {}})
        assert second.status_code == 200
        second_payload = await second.get_json()
        second_base = base_for(path)
        assert second_base != first_base
        assert second_payload["obj"] == {"base": second_base, "restart": ["3xui"]}
        resume.set()

    first_response = first.result()
    assert first_response.status_code == (202 if audit_failure else 200)
    first_payload = await first_response.get_json()
    assert first_payload["obj"] == {
        "base": first_base,
        "restart": [],
        **({"committed": True, "audit_pending": True} if audit_failure else {}),
    }

    before = path.read_bytes()
    records = list(audit.records)
    stale = await set_config(app, first_payload["obj"]["base"], {"sub_name": "lost"})
    assert stale.status_code == 409
    assert (await stale.get_json())["obj"]["base"] == second_base
    assert path.read_bytes() == before
    assert cfg.view()["sub_name"] == "second"
    assert audit.records == records


@pytest.mark.asyncio
async def test_empty_panels_are_patchable(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
) -> None:
    app, cfg, path, _ = config_api
    response = await set_config(app, base_for(path), {"3xui": {}})
    assert response.status_code == 200
    assert cfg.view()["3xui"] == {}
    assert (await response.get_json())["obj"]["restart"] == ["3xui"]


@pytest.mark.asyncio
async def test_support_text_patch_is_live_and_discord_remains_optional(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
) -> None:
    app, cfg, path, _ = config_api
    response = await set_config(app, base_for(path), {"discord": None})
    assert response.status_code == 200
    assert "discord" not in cfg.view()
    response = await set_config(
        app,
        base_for(path),
        {"support_text": {"telegram": ["Support EN", "Поддержка RU"]}},
    )
    assert response.status_code == 200
    assert (await response.get_json())["obj"]["restart"] == []
    assert cfg.view().get("support_text") == {"telegram": ["Support EN", "Поддержка RU"]}
    response = await set_config(app, base_for(path), {"support_text": None})
    assert response.status_code == 200
    assert (await response.get_json())["obj"]["restart"] == []
    assert "support_text" not in cfg.view()


@pytest.mark.asyncio
async def test_unknown_profile_node_preserves_config(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
) -> None:
    app, cfg, path, audit = config_api
    before = path.read_bytes()
    profiles = json.loads(before)["profiles"]
    profiles["profile1"]["node"] = "missing"
    response = await set_config(app, base_for(path), {"profiles": profiles})
    assert response.status_code == 400
    assert "not a key of nodes" in (await response.get_json())["msg"]
    assert path.read_bytes() == before
    assert cfg.view() == json.loads(before)
    assert not audit.records
    assert not (path.parent / "backup").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"nope": 1}, "Unknown key"),
        ({"panel_alert_cooldown": "soon"}, "Schema validation"),
        ({"$schema": "other.json"}, "$schema"),
        ({"domain": None}, "required"),
        ({"api_admin_ui_auth": ["", "password"]}, "entries must not be empty"),
        ({"api_admin_ui_auth": ["admin", ""]}, "entries must not be empty"),
    ],
)
async def test_rejected_patch_preserves_file(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
    values: dict[str, JsonValue],
    message: str,
) -> None:
    app, cfg, path, audit = config_api
    before = path.read_bytes()
    snapshot = cfg.view()
    response = await set_config(app, base_for(path), values)
    assert response.status_code == 400
    assert message in (await response.get_json())["msg"]
    assert path.read_bytes() == before
    assert cfg.view() == snapshot
    assert not audit.records
    assert not (path.parent / "backup").exists()


@pytest.mark.asyncio
async def test_config_optional_deletion_and_restart(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
) -> None:
    app, _, path, _ = config_api
    response = await set_config(
        app,
        base_for(path),
        {"funny_strings": None, "domain": "https://other.test", "bot": None, "publicbot": None},
    )
    assert response.status_code == 200
    assert (await response.get_json())["obj"]["restart"] == ["bot", "publicbot"]
    updated = json.loads(path.read_bytes())
    assert "funny_strings" not in updated
    assert "bot" not in updated
    assert "publicbot" not in updated
    panels = updated["3xui"]
    panels["local_panel"]["name"] = "renamed"
    response = await set_config(app, base_for(path), {"3xui": panels})
    assert (await response.get_json())["obj"]["restart"] == ["3xui"]


@pytest.mark.asyncio
async def test_unchanged_patch_does_not_write(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
) -> None:
    app, _, path, audit = config_api
    before = path.read_bytes(), path.stat().st_mtime_ns
    response = await set_config(app, base_for(path), {"sub_name": "before"})
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["msg"] == "Unchanged"
    assert payload["obj"]["restart"] == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not audit.records


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("audit unavailable"), ValidationError("audit failed")])
async def test_config_audit_failure_preserves_committed_result(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    app, cfg, path, audit = config_api
    expected = json.loads(path.read_bytes())
    expected.update(sub_name="after", **{"3xui": {}})

    async def fail_append(record: dict[str, JsonValue]) -> None:
        assert record["action"] == "config_update"
        assert record["info"] == {"keys": ["3xui", "sub_name"]}
        raise failure

    monkeypatch.setattr(audit, "append", fail_append)
    response = await set_config(app, base_for(path), {"sub_name": "after", "3xui": {}})
    assert response.status_code == 202
    payload = await response.get_json()
    assert payload["success"] is True
    assert "Do not retry" in payload["msg"]
    assert payload["obj"] == {
        "base": config_etag(expected),
        "restart": ["3xui"],
        "committed": True,
        "audit_pending": True,
    }
    assert json.loads(path.read_bytes()) == expected
    assert cfg.view() == expected
    assert not audit.records


@pytest.mark.asyncio
async def test_failed_commit_does_not_publish_or_audit(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, cfg, path, audit = config_api
    before = path.read_bytes()

    def fail_write(data: dict[str, JsonValue]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cfg._require_store(), "atomic_write", fail_write)
    response = await set_config(app, base_for(path), {"sub_name": "after"})
    assert response.status_code == 500
    assert path.read_bytes() == before
    assert cfg.view()["sub_name"] == "before"
    assert not audit.records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"{", "application/json"),
        (b"[]", "application/json"),
        (b"{}", "text/plain"),
        (b'{"values":{}}', "application/json"),
        (b"x" * (1024 * 1024 + 1), "application/json"),
    ],
)
async def test_malformed_and_oversized_bodies(
    config_api: tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit],
    data: bytes,
    content_type: str,
) -> None:
    _, cfg, path, audit = config_api
    before = path.read_bytes()
    response = (
        await config_api[0]
        .test_client()
        .post(
            "/sub/privapi/api/config/set",
            data=data,
            headers={**HEADERS, "Content-Type": content_type},
        )
    )
    assert response.status_code == (413 if len(data) > 1024 * 1024 else 400)
    assert path.read_bytes() == before
    assert cfg.view() == json.loads(before)
    assert not audit.records
