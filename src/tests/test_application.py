from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from app import AppOptions, AppPaths, Application, create_application
from requests import Response


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


class ApplicationFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = build_paths(self.root)
        project_root = Path(__file__).resolve().parents[2]
        config = json.loads((project_root / "docs/EXAMPLE.config.json").read_text())
        config["$schema"] = "config.schema.json"
        config["api_uri"] = "api"
        config["bot"]["token"] = "123:test-admin-bot"
        config["publicbot"]["token"] = "123:test-public-bot"
        second_panel = dict(config["3xui"]["local_panel"])
        second_panel["name"] = "second"
        config["3xui"]["second_panel"] = second_panel
        self.paths.config.write_text(json.dumps(config), encoding="utf-8")
        (self.root / "config.schema.json").write_text(
            (project_root / "config.schema.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def factory(self, **options: Any) -> Application:
        return create_application(
            paths=self.paths,
            options=AppOptions(
                start_background=False,
                panel_transport_factory=lambda: RecordingTransport({"success": True}),
                **options,
            ),
        )

    def test_from_env_defaults_to_project_root(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        data = project_root / "data"
        self.assertEqual(
            AppPaths.from_env(),
            AppPaths(
                data=data,
                backups=data / "backup",
                config=data / "config.json",
                language=project_root / "lang.jsonc",
                database=data / "state.sqlite3",
                log=data / "log.jsonl",
                audit=data / "audit.jsonl",
                primary_lock=data / ".primary.lock",
            ),
        )

    def test_factory_creates_routes_and_respects_paths(self) -> None:
        with self.factory() as runtime:
            client = runtime.app.test_client()
            self.assertEqual(client.get("/sub/api/api/health").status_code, 200)
            self.assertTrue(
                any(rule.rule == "/sub" for rule in runtime.app.url_map.iter_rules())
            )
            sub_response = client.get("/sub")
            self.assertEqual(sub_response.status_code, 401)
            self.assertFalse(sub_response.get_json()["success"])
            self.assertTrue(self.paths.database.exists())
            self.assertTrue(self.paths.log.exists())
            self.assertTrue(self.paths.audit.exists())
            self.assertTrue(self.paths.primary_lock.exists())
            self.assertEqual(len(runtime.panels), 2)

    def test_background_components_are_not_started(self) -> None:
        with self.factory() as runtime:
            self.assertIsNone(runtime.admin_bot.polling_thread)
            self.assertIsNone(runtime.public_bot.polling_thread)
            self.assertFalse(runtime.bandwidth_watcher._threads[0].is_alive())  # pyright: ignore[reportPrivateUsage]

    def test_stop_is_idempotent(self) -> None:
        runtime = self.factory()
        runtime.stop()
        runtime.stop()
        self.assertTrue(runtime._stop_event.is_set())  # pyright: ignore[reportPrivateUsage]

    def test_proxy_fix_sets_remote_addr(self) -> None:
        remote_addresses: list[str | None] = []

        with self.factory(proxy_hops=1) as runtime:

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
                self.assertEqual(response.status_code, 200)
            self.assertEqual(remote_addresses, ["203.0.113.5"])

    def test_require_proxy_allows_only_loopback(self) -> None:
        with self.factory(require_proxy=True) as runtime:
            client = runtime.app.test_client()

            # local tools and healthchecks do not need proxy headers
            response = client.get(
                "/sub/api/api/health",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(response.status_code, 200)

            # the local reverse proxy path also passes through
            response = client.get(
                "/sub/api/api/health",
                headers={"X-Forwarded-For": "203.0.113.5"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(response.status_code, 200)

            # spoofed proxy headers do not bypass the peer check
            response = client.get(
                "/sub/api/api/health",
                headers={"X-Forwarded-For": "203.0.113.5"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.get_json()["success"])

    def test_secondary_process_does_not_recover_or_start_bots(self) -> None:
        self.paths.data.mkdir(parents=True, exist_ok=True)
        with self.paths.primary_lock.open("a+b") as lock:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.factory() as runtime:
                self.assertFalse(runtime.primary)
                with mock.patch.object(runtime.subscription, "recover_rollback_failures") as recover:
                    with mock.patch.object(runtime.bandwidth_watcher, "start") as watcher_start:
                        with mock.patch.object(runtime.admin_bot, "start") as admin_start:
                            with mock.patch.object(runtime.public_bot, "start") as public_start:
                                runtime.start()
                recover.assert_not_called()
                watcher_start.assert_not_called()
                admin_start.assert_not_called()
                public_start.assert_not_called()

    def test_panel_failure_closes_created_resources(self) -> None:
        calls: list[int] = []

        def transport_factory() -> RecordingTransport:
            calls.append(len(calls))
            if len(calls) > 1:
                raise RuntimeError("second panel construction failed")
            return RecordingTransport({"success": True})

        with mock.patch("app.Database") as database:
            database.return_value.close = mock.Mock()
            with self.assertRaises(RuntimeError):
                create_application(
                    paths=self.paths,
                    options=AppOptions(
                        start_background=False,
                        panel_transport_factory=transport_factory,
                    ),
                )
            database.return_value.close.assert_called()


if __name__ == "__main__":
    unittest.main()
