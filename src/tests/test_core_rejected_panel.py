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
from errors import PanelRejectedError


class RejectedPanelTests(unittest.TestCase):
    def test_add_users_raises_panel_rejected_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            config: dict[str, Any] = {
                "uri": "sub",
                "fingerprints": ["chrome"],
                "salt": "test-salt",
                "domain": "https://example.test",
                "funny_strings": ["test"],
            }
            subscription = Subscription(
                cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}), db=database,
                app=Flask(__name__), panels=[], whitelist_panel=None,
            )
            database.create_user(
                username="alice", uuid="01234567-89ab-cdef-0123-456789abcdef",
                token="a" * 40, fingerprint="chrome", displayname="Alice",
                ext_username="alice-login", ext_password_hash="hash",
            )
            inbound = Inbound(
                id=1, up=0, down=0, total=0, allTime=0, remark="test", enable=True,
                expiryTime=0, trafficReset="", lastTrafficResetTime=0, clientStats=[],
                listen="", port=443, protocol="vless", settings="{}",
                streamSettings=json.dumps({"network": "tcp"}), tag="", sniffing="",
            )

            class RejectedPanel:
                local = False
                inbounds_list = ()
                mode = "blacklist"
                cache: list[Inbound] | None = None
                cache_time = 0

                def get(self, url: str) -> Response:
                    response = Response()
                    response.status_code = 200
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps(
                        {"success": True, "msg": "", "obj": [asdict(inbound)]}
                    ).encode()
                    return response

                def post(self, url: str, **kwargs: object) -> Response:
                    response = Response()
                    response.status_code = 201
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps(
                        {"success": False, "msg": "rejected", "obj": None}
                    ).encode()
                    return response

            subscription.res.panels.append(cast(XUiSession, RejectedPanel()))
            with self.assertRaises(PanelRejectedError):
                subscription.business_svc.add_users("alice")
            database.close()

if __name__ == "__main__":
    unittest.main()
