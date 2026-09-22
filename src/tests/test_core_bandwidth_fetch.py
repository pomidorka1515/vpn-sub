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
from custom_types import BandwidthInfo, ClientStats, Inbound
from session import XUiSession
from db import Database


class BandwidthFetchTests(unittest.TestCase):
    USER_UUID = "01234567-89ab-cdef-0123-456789abcdef"

    @staticmethod
    def _subscription(
        database: Database,
        *,
        panels: list[object] | None = None,
        whitelist_panel: object | None = None,
    ) -> Subscription:
        config: dict[str, Any] = {
            "uri": "sub",
            "fingerprints": ["chrome"],
            "salt": "test-salt",
            "domain": "https://example.test",
            "funny_strings": ["test"],
        }
        return Subscription(
            cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}), db=database,
            app=Flask(__name__),
            panels=cast(list[XUiSession], panels or []),
            whitelist_panel=cast(XUiSession | None, whitelist_panel),
        )

    @staticmethod
    def _client(uuid: str, up: int, down: int, inbound_id: int = 1) -> ClientStats:
        return ClientStats(
            id=inbound_id, inboundId=inbound_id, enable=True, email="alice-abcd1234",
            uuid=uuid, subId="", up=up, down=down, allTime=up + down,
            expiryTime=0, total=0, reset=0, lastOnline=0,
        )

    @staticmethod
    def _inbound(inbound_id: int, clients: list[ClientStats]) -> Inbound:
        return Inbound(
            id=inbound_id, up=0, down=0, total=0, allTime=0, remark="test", enable=True,
            expiryTime=0, trafficReset="", lastTrafficResetTime=0, clientStats=clients,
            listen="", port=443, protocol="vless", settings="{}",
            streamSettings=json.dumps({"network": "tcp"}), tag="", sniffing="",
        )

    @staticmethod
    def _panel(inbounds: list[Inbound]) -> object:
        class FakePanel:
            local = True
            dead = True
            inbounds_list: tuple[int, ...] = ()
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

        return FakePanel()

    def test_whitelist_total_uses_only_whitelist_panel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            other_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
            regular = self._panel([
                self._inbound(1, [
                    self._client(self.USER_UUID, up=100, down=200),
                    self._client(other_uuid, up=9_000, down=9_000),
                ]),
            ])
            whitelist = self._panel([
                self._inbound(2, [
                    self._client(self.USER_UUID, up=10, down=20),
                    self._client(other_uuid, up=8_000, down=8_000),
                ]),
            ])
            subscription = self._subscription(
                database, panels=[regular], whitelist_panel=whitelist,
            )
            database.create_user(
                username="alice", uuid=self.USER_UUID,
                token="a" * 40, fingerprint="chrome", displayname="Alice",
            )

            regular_bw = subscription.bandwidth_svc.bandwidth("alice")
            whitelist_bw = subscription.bandwidth_svc.bandwidth("alice", whitelist=True)

            self.assertEqual(regular_bw, BandwidthInfo(100, 200, 300))
            self.assertEqual(whitelist_bw, BandwidthInfo(10, 20, 30))
            database.close()

    def test_whitelist_total_is_zero_without_whitelist_panel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            regular = self._panel([
                self._inbound(1, [self._client(self.USER_UUID, up=100, down=200)]),
            ])
            subscription = self._subscription(database, panels=[regular], whitelist_panel=None)
            database.create_user(
                username="alice", uuid=self.USER_UUID,
                token="a" * 40, fingerprint="chrome", displayname="Alice",
            )

            self.assertEqual(
                subscription.bandwidth_svc.bandwidth("alice"),
                BandwidthInfo(100, 200, 300),
            )
            self.assertEqual(
                subscription.bandwidth_svc.bandwidth("alice", whitelist=True),
                BandwidthInfo(0, 0, 0),
            )
            database.close()

    def test_get_info_keeps_raw_whitelist_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            regular = self._panel([
                self._inbound(1, [self._client(self.USER_UUID, up=100, down=200)]),
            ])
            whitelist = self._panel([
                self._inbound(2, [self._client(self.USER_UUID, up=10, down=20)]),
            ])
            subscription = self._subscription(
                database, panels=[regular], whitelist_panel=whitelist,
            )
            database.create_user(
                username="alice", uuid=self.USER_UUID,
                token="a" * 40, fingerprint="chrome", displayname="Alice",
            )
            subscription.panel_svc.is_online = lambda username: False  # type: ignore[method-assign]
            info = subscription.business_svc.get_info("alice", pretty=False)
            pretty = subscription.business_svc.get_info("alice", pretty=True)
            self.assertEqual(info.bandwidth.wl_total.total, 30)
            self.assertEqual(pretty.bandwidth.wl_total.total, 0.0)
            database.close()

if __name__ == "__main__":
    unittest.main()
