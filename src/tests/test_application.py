from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from requests import Response

from app import AppOptions, AppPaths, Application, create_application


def json_response(data: dict[str, Any], status_code: int = 200) -> Response:
    response = Response()
    response.status_code = status_code
    response._content = json.dumps(data).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


class RecordingTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        return json_response(self.response)


def build_paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(
        data=data,
        backups=data / "backup",
        config=root / "config.json",
        language=Path(__file__).resolve().parents[2] / "lang.jsonc",
        database=data / "state.sqlite3",
        log=data / "log.jsonl",
        audit=data / "audit.jsonl",
        primary_lock=data / ".primary.lock",
    )


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    paths = build_paths(tmp_path)
    project_root = Path(__file__).resolve().parents[2]
    config = json.loads((project_root / "docs/EXAMPLE.config.json").read_text())
    config["$schema"] = "config.schema.json"
    config["api_uri"] = "api"
    config["bot"]["token"] = "123:test-admin-bot"
    config["publicbot"]["token"] = "123:test-public-bot"
    second_panel = dict(config["3xui"]["local_panel"])
    second_panel["name"] = "second"
    config["3xui"]["second_panel"] = second_panel
    paths.config.write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "config.schema.json").write_text(
        (project_root / "config.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return paths


def factory(paths: AppPaths, **options: Any) -> Application:
    return create_application(
        paths=paths,
        options=AppOptions(
            start_background=False,
            panel_transport_factory=lambda: RecordingTransport({"success": True}),
            **options,
        ),
    )


def test_from_env_defaults_to_project_root() -> None:
    project_root = Path(__file__).resolve().parents[2]
    data = project_root / "data"
    assert AppPaths.from_env() == AppPaths(
        data=data,
        backups=data / "backup",
        config=data / "config.json",
        language=project_root / "lang.jsonc",
        database=data / "state.sqlite3",
        log=data / "log.jsonl",
        audit=data / "audit.jsonl",
        primary_lock=data / ".primary.lock",
    )


def test_factory_creates_routes_and_respects_paths(paths: AppPaths) -> None:
    with factory(paths) as runtime:
        client = runtime.app.test_client()
        assert client.get("/sub/api/api/health").status_code == 200
        assert any(rule.rule == "/sub" for rule in runtime.app.url_map.iter_rules())
        sub_response = client.get("/sub")
        assert sub_response.status_code == 401
        assert not sub_response.get_json()["success"]
        assert paths.database.exists()
        assert paths.log.exists()
        assert paths.audit.exists()
        assert paths.primary_lock.exists()
        assert len(runtime.panels) == 2


def test_background_components_are_not_started(paths: AppPaths) -> None:
    with factory(paths) as runtime:
        assert runtime.admin_bot.polling_thread is None
        assert runtime.public_bot.polling_thread is None
        assert not runtime.bandwidth_watcher._threads[0].is_alive()  # pyright: ignore[reportPrivateUsage]


def test_stop_is_idempotent(paths: AppPaths) -> None:
    runtime = factory(paths)
    runtime.stop()
    runtime.stop()
    assert runtime._stop_event.is_set()  # pyright: ignore[reportPrivateUsage]


def test_proxy_fix_sets_remote_addr(paths: AppPaths) -> None:
    remote_addresses: list[str | None] = []
    with factory(paths, proxy_hops=1) as runtime:

        @runtime.app.route("/__remote")
        def remote() -> str:  # pyright: ignore[reportUnusedFunction]
            from flask import request

            remote_addresses.append(request.remote_addr)
            return ""

        with runtime.app.test_client() as client:
            response = client.get(
                "/__remote",
                headers={"X-Forwarded-For": "203.0.113.5"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
            )
            assert response.status_code == 200
        assert remote_addresses == ["203.0.113.5"]


def test_require_proxy_allows_only_loopback(paths: AppPaths) -> None:
    with factory(paths, require_proxy=True) as runtime:
        client = runtime.app.test_client()
        response = client.get(
            "/sub/api/api/health",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert response.status_code == 200
        response = client.get(
            "/sub/api/api/health",
            headers={"X-Forwarded-For": "203.0.113.5"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert response.status_code == 200
        response = client.get(
            "/sub/api/api/health",
            headers={"X-Forwarded-For": "203.0.113.5"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        assert response.status_code == 400
        assert not response.get_json()["success"]


def test_secondary_process_does_not_recover_or_start_bots(paths: AppPaths) -> None:
    paths.data.mkdir(parents=True, exist_ok=True)
    with paths.primary_lock.open("a+b") as lock:
        import fcntl

        fcntl.flock(lock, fcntl.LOCK_EX)
        with factory(paths) as runtime:
            assert not runtime.primary
            with mock.patch.object(runtime.subscription.business_code_svc, "recover_rollback_failures") as recover:
                with mock.patch.object(runtime.bandwidth_watcher, "start") as watcher_start:
                    with mock.patch.object(runtime.admin_bot, "start") as admin_start:
                        with mock.patch.object(runtime.public_bot, "start") as public_start:
                            runtime.start()
            recover.assert_not_called()
            watcher_start.assert_not_called()
            admin_start.assert_not_called()
            public_start.assert_not_called()


def test_panel_failure_closes_created_resources(paths: AppPaths) -> None:
    calls: list[int] = []

    def transport_factory() -> RecordingTransport:
        calls.append(len(calls))
        if len(calls) > 1:
            raise RuntimeError("second panel construction failed")
        return RecordingTransport({"success": True})

    with mock.patch("app.Database") as database:
        database.return_value.close = mock.Mock()
        with pytest.raises(RuntimeError):
            create_application(
                paths=paths,
                options=AppOptions(
                    start_background=False,
                    panel_transport_factory=transport_factory,
                ),
            )
        database.return_value.close.assert_called()
