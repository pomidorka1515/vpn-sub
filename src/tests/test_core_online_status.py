from __future__ import annotations

from typing import Any, cast
from flask import Flask
import json
import tempfile
import unittest
from requests import Response

from core import Subscription
from config import ConfigLike
from session import XUiSession
from db import Database
from errors import PanelUnavailableError


class OnlineStatusTests(unittest.TestCase):
    @staticmethod
    def _subscription(database: Database) -> Subscription:
        config: dict[str, Any] = {
            "uri": "sub",
            "fingerprints": ["chrome"],
            "salt": "test-salt",
            "domain": "https://example.test",
            "funny_strings": ["test"],
        }
        return Subscription(
            cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}), db=database,
            app=Flask(__name__), panels=[], whitelist_panel=None,
        )

    def test_online_status_reports_empty_when_all_panels_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)

            class EmptyPanel:
                name = "panel"
                dead = False

                def post(self, url: str, **kwargs: object) -> Response:
                    response = Response()
                    response.status_code = 200
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps({"success": True, "obj": []}).encode()
                    return response

            subscription.panels.append(cast(XUiSession, EmptyPanel()))
            status = subscription.panel_svc. get_online_status()
            self.assertEqual(status.users, [])
            self.assertEqual(status.panel_health, {"panel": "ok"})
            database.close()

    def test_online_status_raises_when_all_panels_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)

            class FailedPanel:
                name = "panel"
                dead = False

                def post(self, url: str, **kwargs: object) -> Response:
                    raise RuntimeError("transport failed")

            subscription.res.panels.append(cast(XUiSession, FailedPanel()))
            with self.assertRaises(PanelUnavailableError):
                subscription.panel_svc.get_online_status()
            database.close()

    def test_online_status_reports_empty_without_panels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)
            status = subscription.panel_svc.get_online_status()
            self.assertEqual(status.users, [])
            self.assertEqual(status.panel_health, {})
            self.assertEqual(subscription.panel_svc.get_online_status(new=True).users, {})
            database.close()

    def test_online_status_marks_malformed_payload_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)

            class InvalidPanel:
                name = "panel"
                dead = False

                def post(self, url: str, **kwargs: object) -> Response:
                    response = Response()
                    response.status_code = 200
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps({"success": True, "obj": 7}).encode()
                    return response

            subscription.panels.append(cast(XUiSession, InvalidPanel()))
            status = subscription.panel_svc.get_online_status()
            self.assertEqual(status.panel_health, {"panel": "invalid"})
            database.close()

if __name__ == "__main__":
    unittest.main()
