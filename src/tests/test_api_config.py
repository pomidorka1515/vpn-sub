from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import pytest_asyncio
from async_api_helpers import api_app, make_subscription, make_watch

from api import AsyncApi
from api.config_patch import REQUIRED_KEYS, config_etag
from config import AppConfig, Config
from config.store import AsyncConfig
from core.services.audit import AuditService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from quart import Quart
    from quart.wrappers import Response

    from config import JsonValue
    from config.store import AsyncLinesConfig
    from db import Database

ROOT = Path(__file__).resolve().parents[2]
HEADERS = {"Authorization": "secret-token"}


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, JsonValue]] = []

    async def append(self, record: dict[str, JsonValue]) -> None:
        self.records.append(record)


@pytest_asyncio.fixture
async def config_api(
    tmp_path: Path, database: Database,
) -> AsyncIterator[tuple[Quart, AsyncConfig[AppConfig], Path, RecordingAudit]]:
    path = tmp_path / "config.json"
    data = json.loads((ROOT / "docs/EXAMPLE.config.json").read_text())
    data.update(api_token="secret-token", sub_name="before", funny_strings=["keep-me"])
    path.write_text(json.dumps(data, indent=4) + "\n")
    async for app in api_app():
        sub = await make_subscription(database, app=app)
        cfg = AsyncConfig[AppConfig](
            lambda: Config[AppConfig](
                path=path,
                schema_path=ROOT / "config.schema.json",
                backup_dir=tmp_path / "backup",
                indent=4,
                sync_mode="none",
                start_backup=False,
            ),
            files=sub.res.offloads.files,
        )
        await cfg.start()
        audit = RecordingAudit()
        sub.audit_svc = AuditService(cast("AsyncLinesConfig", audit))
        AsyncApi(
            app=app,
            cfg=cfg,
            audit_cfg=cast("AsyncLinesConfig", audit),
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
        "/sub/privapi/api/config/set", json={"base": base, "values": values}, headers=HEADERS,
    )


def base_for(path: Path) -> str:
    return config_etag(cast("dict[str, JsonValue]", json.loads(path.read_bytes())))


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
    assert ": " not in body
    assert ", " not in body
    assert body.index('"obj":') < body.index('"$schema":')
    assert first.headers["ETag"] == second.headers["ETag"] == f'"{base_for(path)}"'
    assert first.headers["Cache-Control"] == "no-store"


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
        ({"3xui": {}}, "3xui must contain"),
        ({"$schema": "other.json"}, "$schema"),
        ({"domain": None}, "required"),
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
