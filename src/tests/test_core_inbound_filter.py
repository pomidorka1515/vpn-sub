from __future__ import annotations

from typing import Any, cast
from flask import Flask
import json
import tempfile
import unittest
from dataclasses import asdict
from requests import Response

from core import Subscription
from config import ConfigLike
from custom_types import Inbound
from session import XUiSession
from db import Database


class InboundFilterTests(unittest.TestCase):
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

    @staticmethod
    def _inbound(inbound_id: int) -> Inbound:
        return Inbound(
            id=inbound_id, up=0, down=0, total=0, allTime=0, remark="test", enable=True,
            expiryTime=0, trafficReset="", lastTrafficResetTime=0, clientStats=[],
            listen="", port=443, protocol="vless", settings="{}",
            streamSettings=json.dumps({"network": "tcp"}), tag="", sniffing="",
        )

    def _filter(self, mode: str, listed: tuple[int, ...]) -> list[int]:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)
            inbounds = [self._inbound(1), self._inbound(2), self._inbound(3)]

            class FilterPanel:
                local = True
                inbounds_list = listed
                mode = "blacklist"
                cache: list[Inbound] | None = None
                cache_time = 0
                name = "panel"

                def get(self, url: str) -> Response:
                    response = Response()
                    response.status_code = 200
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps(
                        {"success": True, "msg": "", "obj": [asdict(i) for i in inbounds]}
                    ).encode()
                    return response

            panel = FilterPanel()
            panel.mode = mode
            filtered = subscription.panel_svc.getinbounds(cast(XUiSession, panel))
            database.close()
            return [inbound.id for inbound in filtered]

    def test_blacklist_drops_listed_inbound_ids(self) -> None:
        self.assertEqual(self._filter("blacklist", (2,)), [1, 3])

    def test_whitelist_keeps_only_listed_inbound_ids(self) -> None:
        self.assertEqual(self._filter("whitelist", (2,)), [2])

    def test_empty_whitelist_keeps_no_inbounds(self) -> None:
        self.assertEqual(self._filter("whitelist", ()), [])

    def test_empty_blacklist_keeps_all_inbounds(self) -> None:
        self.assertEqual(self._filter("blacklist", ()), [1, 2, 3])

if __name__ == "__main__":
    unittest.main()
