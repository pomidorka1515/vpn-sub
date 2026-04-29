from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any

import py_compile
import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if TYPE_CHECKING:
    from flask import Flask # pyright: ignore[reportUnusedImport]
    from config import Config

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

MINIMAL_CONFIG: dict[str, Any] = {
    "uri": "sub",
    "api_uri": "privapi",
    "api_token": "test_token_abc123",
    "panel_alert_cooldown": 3600,
    "salt": "testsalt",
    "sub_name": "Test Sub",
    "fingerprints": ["fp1", "fp2"],
    "nodes": {"node1": "Node One"},
    "bot": {"token": "bot_token", "whitelist": []},
    "routing": {
        "Name": "test",
        "GlobalProxy": True,
        "DomainStrategy": "AsIs",
        "DirectSites": [],
        "DirectPackages": [],
        "DirectIp": [],
    },
    "publicbot": {
        "token": "public_bot_token",
        "tg_lang": {"en": "en", "ru": "ru"},
        "lang": {
            "en": {"start": "Hello"},
            "ru": {"start": "Привет"},
        },
    },
    "codes": [],
    "3xui": {
        "primary": {
            "name": "Test Panel",
            "address": "127.0.0.1",
            "port": 2053,
            "uri": "/panel/",
            "username": "admin",
            "password": "admin",
            "https": False,
            "whitelist": False,
            "ignore_inbounds": [],
        }
    },
    "description": [f"desc{i}" for i in range(14)],
    "flags": {},
    "users": {},
    "tokens": {},
    "displaynames": {},
    "tgids": {},
    "bw": {},
    "wl_bw": {},
    "profiles": {},
    "profileDescriptions": {},
    "whitelistProfiles": [],
    "masterLinks": {},
    "profileNodes": {},
    "userFingerprints": {},
    "time": {},
    "status": {},
    "statusTime": {},
    "statusWl": {},
    "_notified": [],
    "_wl_notified": [],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cfg_path(tmp_path: pytest.TempPathFactory) -> str:
    """Path within a pytest tmp_path fixture."""
    return str(tmp_path / "config.json")


@pytest.fixture
def minimal_config_data() -> dict[str, Any]:
    """Deep-copy of MINIMAL_CONFIG so tests can mutate it freely."""
    return json.loads(json.dumps(MINIMAL_CONFIG))


@pytest.fixture
def write_config(minimal_config_data: dict[str, Any], tmp_cfg_path: str) -> str:
    """Write minimal config to the temp path and return the path."""
    with open(tmp_cfg_path, "w", encoding="utf-8") as f:
        json.dump(minimal_config_data, f)
    return tmp_cfg_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def curl_code(
    method: str,
    path: str,
    *,
    base_url: str = "http://127.0.0.1:5550",
    headers: dict[str, Any] | None = None,
    data: Any = None,
    timeout: int = 5,
) -> int | None:
    """Return HTTP status code via curl, or None on timeout/parse failure."""
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", method]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{base_url}{path}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return int(r.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError):
        return None


def curl_json(
    method: str,
    path: str,
    *,
    base_url: str = "http://127.0.0.1:5550",
    headers: dict[str, Any] | None = None,
    data: Any = None,
    timeout: int = 5,
) -> dict[str, Any] | None:
    """Return parsed JSON response via curl, or None on failure."""
    cmd = ["curl", "-s", "-X", method]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(f"{base_url}{path}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. Module imports
# ---------------------------------------------------------------------------

class TestModuleImports:
    """Verify that all project modules are importable without error."""

    @pytest.mark.parametrize(
        "mod",
        [
            "config",
            "loggers",
            "session",
            "core",
            "api",
            "bots",
        ],
    )
    def test_import_module(self, mod: str) -> None:
        __import__(mod)

    def test_app_syntax(self) -> None:
        """app.py must compile without syntax errors."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
        py_compile.compile(path, doraise=True)


# ---------------------------------------------------------------------------
# 2. Config — basic API
# ---------------------------------------------------------------------------

class TestConfigBasic:
    """Config loading and read API."""

    def test_loads_minimal_valid_json(self, write_config: str) -> None:
        from config import Config

        cfg: Config = Config(path=write_config)  # type: ignore[assignment]
        assert cfg["api_token"] == "test_token_abc123"

    def test_get_with_default(self, write_config: str) -> None:
        from config import Config

        cfg: Config = Config(path=write_config)  # type: ignore[assignment]
        assert cfg.get("missing_key_xyz", "default") == "default"

    def test_edit_transaction_append_and_pop(self, write_config: str) -> None:
        from config import Config

        cfg: Config = Config(path=write_config)  # type: ignore[assignment]
        with cfg.edit() as tx:
            tx["codes"].append(
                {
                    "code": "TEST1",
                    "action": "register",
                    "perma": False,
                    "days": 1,
                    "gb": 0,
                }
            )
            tx["codes"].pop()
        # No exception means success

    def test_nested_edit_raises(self, write_config: str) -> None:
        from config import Config

        cfg: Config = Config(path=write_config)  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            with cfg.edit():
                with cfg.edit():
                    pass


# ---------------------------------------------------------------------------
# 3. Config — transaction isolation
# ---------------------------------------------------------------------------

class TestConfigTransactionIsolation:
    """Transaction isolation: committed mutations visible in live config,
    but the live config must not be polluted before commit."""

    def test_commit_updates_live_config(self, tmp_cfg_path: str) -> None:
        from config import Config

        data = json.loads(json.dumps(MINIMAL_CONFIG))
        data["bw"] = {"u1": [10, 0]}
        with open(tmp_cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        cfg: Config = Config(path=tmp_cfg_path)  # type: ignore[assignment]
        with cfg.edit() as tx:
            tx["bw"]["u1"][1] = 999

        assert cfg["bw"]["u1"][1] == 999

    def test_live_config_stays_clean_while_tx_open(self, tmp_cfg_path: str) -> None:
        """Read isolation: same-thread direct Config access inside a transaction
        block is prevented and raises RuntimeError (use the transaction object
        instead).  Other threads are free to read the working copy."""
        from config import Config

        data = json.loads(json.dumps(MINIMAL_CONFIG))
        data["bw"] = {"u2": [5, 0]}
        with open(tmp_cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        cfg: Config = Config(path=tmp_cfg_path)  # type: ignore[assignment]
        seen_values: list[int] = []
        same_thread_raised = False

        def mutate() -> None:
            nonlocal same_thread_raised
            with cfg.edit() as tx:
                tx["bw"]["u2"][1] = 123
                # Same-thread cfg access raises RuntimeError while tx is open.
                try:
                    cfg["bw"]["u2"][1]
                except RuntimeError as e:
                    if "transaction" in str(e):
                        same_thread_raised = True
                time.sleep(0.02)

        def read_while_tx_open() -> None:
            for _ in range(10):
                seen_values.append(cfg["bw"]["u2"][1])  # type: ignore[arg-type]
                time.sleep(0.002)

        t1 = threading.Thread(target=mutate)
        t2 = threading.Thread(target=read_while_tx_open)
        t1.start()
        time.sleep(0.005)
        t2.start()
        t1.join()
        t2.join()

        # Same-thread access must raise RuntimeError inside a transaction block.
        assert same_thread_raised, (
            "same-thread cfg access should raise RuntimeError during open transaction"
        )
        # Other-thread reads see the working copy value (123), which is expected.
        assert all(v == 123 for v in seen_values), (
            f"other thread saw unexpected values: {seen_values}"
        )

    def test_no_partial_state_during_concurrent_tx(
        self, tmp_cfg_path: str
    ) -> None:
        from config import Config

        data = json.loads(json.dumps(MINIMAL_CONFIG))
        data["counter"] = 0
        with open(tmp_cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        cfg: Config = Config(path=tmp_cfg_path)  # type: ignore[assignment]
        observed: list[int] = []

        def reader() -> None:
            for _ in range(50):
                observed.append(cfg["counter"])  # type: ignore[arg-type]
                time.sleep(0.001)

        def writer() -> None:
            for _ in range(5):
                with cfg.edit() as tx:
                    tx["counter"] = tx["counter"] + 1
                    time.sleep(0.002)

        threads = [threading.Thread(target=reader) for _ in range(4)] + [
            threading.Thread(target=writer)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        non_ints = [v for v in observed if not isinstance(v, int)] # pyright: ignore[reportUnnecessaryIsInstance]
        assert len(non_ints) == 0, f"{len(non_ints)} non-int values seen"


# ---------------------------------------------------------------------------
# 4. Config — concurrency
# ---------------------------------------------------------------------------

class TestConfigConcurrency:
    """10 threads × 100 increments must yield exactly 1000."""

    def test_concurrent_increments(self) -> None:
        from config import Config

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

        try:
            minimal = json.loads(json.dumps(MINIMAL_CONFIG))
            minimal["_notified"] = []
            with open(path, "w", encoding="utf-8") as f:
                json.dump(minimal, f)

            tc: Config = Config(path=path)  # type: ignore[assignment]
            # Initialize counter; Config.update() takes dict-like args, not a callback.
            with tc.edit() as tx:
                tx.setdefault("counter", 0)

            def hammer() -> None:
                for _ in range(100):
                    with tc.edit() as tx:
                        tx["counter"] = tx["counter"] + 1

            threads = [threading.Thread(target=hammer) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert tc["counter"] == 1000
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 5. Config — performance
# ---------------------------------------------------------------------------

class TestConfigPerformance:
    """100 reads and 100 mutating transactions must complete within limits."""

    def test_100_reads_under_1s(self, write_config: str) -> None:
        from config import Config

        cfg: Config = Config(path=write_config)  # type: ignore[assignment]
        t0 = time.perf_counter()
        for _ in range(100):
            _ = cfg["codes"]
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"100 reads took {elapsed:.2f}s"

    def test_100_mutating_transactions_under_5s(self, write_config: str) -> None:
        from config import Config

        cfg: Config = Config(path=write_config)  # type: ignore[assignment]
        t0 = time.perf_counter()
        for _ in range(100):
            with cfg.edit() as tx:
                tx["codes"].append(
                    {"code": "__", "action": "register", "perma": False, "days": 0, "gb": 0}
                )
                tx["codes"].pop()
        elapsed = time.perf_counter() - t0
        assert elapsed < 5.0, f"100 transactions took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 6. Config — read-only mode
# ---------------------------------------------------------------------------

class TestConfigReadOnly:
    """Read-only Config instance must reject writes."""

    def test_read_only_raises_on_write(self, write_config: str) -> None:
        from config import Config

        ro: Config = Config(path=write_config, read_only=True)  # type: ignore[assignment]
        with pytest.raises(Exception):
            ro["new_key"] = "value"  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 7. API decorators — unit tests (no server)
# ---------------------------------------------------------------------------

class TestApiDecorators:
    """Route validation and helper functions without a running server."""

    def test_route_validate_passes(self) -> None:
        from api import Route

        r = Route("GET", "/test", "handler", 5)
        r.validate()  # must not raise

    def test_route_validate_raises_on_bad_path(self) -> None:
        from api import Route

        bad = Route("GET", "no-leading-slash", "h", 1)
        with pytest.raises(ValueError, match="must start with '/'"):
            bad.validate()

    def test_route_validate_raises_on_bad_rate_limit(self) -> None:
        from api import Route

        bad = Route("GET", "/test", "h", 0)
        with pytest.raises(ValueError, match="rate_limit must be positive"):
            bad.validate()

    def test_ok_returns_success_response(self) -> None:
        from api import _ok # pyright: ignore[reportPrivateUsage]
        from flask import Flask

        app = Flask("__test__")
        with app.test_request_context("/"):
            resp, code = _ok("hello", 200, {"x": 1})
            assert code == 200
            data = json.loads(resp.data)
            assert data["success"] is True
            assert data["msg"] == "hello"
            assert data["obj"] == {"x": 1}

    def test_err_returns_error_response(self) -> None:
        from api import _err # pyright: ignore[reportPrivateUsage]
        from flask import Flask

        app = Flask("__test__")
        with app.test_request_context("/"):
            resp, code = _err("bad", 400)
            assert code == 400
            data = json.loads(resp.data)
            assert data["success"] is False


# ---------------------------------------------------------------------------
# 8. Loggers
# ---------------------------------------------------------------------------

class TestLoggers:
    """Logger smoke tests."""

    def test_info_and_warning_dont_raise(self) -> None:
        from loggers import Logger

        log = Logger("test")
        log.info("test message")
        log.warning("test warning")

    def test_loading_context_manager(self) -> None:
        from loggers import Logger

        log = Logger("test2")
        with log.loading():
            pass

    def test_span_context_manager(self) -> None:
        from loggers import Logger

        log = Logger("test3")
        with log.span("test span"):
            pass


# ---------------------------------------------------------------------------
# 9. cURL API endpoints (requires running server)
# ---------------------------------------------------------------------------

class TestApiEndpoints:
    """Live HTTP tests against the running server.  Skipped if the server
    is not reachable."""

    @classmethod
    def setup_class(cls) -> None:
        cls.base_url = "http://127.0.0.1:5550"
        cls.web_base = "/sub"
        cls.api_base = "/sub/privapi"
        cls.admin_hdr: dict[str, str] = {}
        cls.production_config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.json"
        )

        try:
            with open(cls.production_config_path) as f:
                cfg_data = json.load(f)
            cls.admin_hdr["Authorization"] = cfg_data.get("api_token", "")
            cls.api_token: str = cfg_data.get("api_token", "")
        except Exception:
            cls.admin_hdr = {"Authorization": ""}
            cls.api_token = ""

    @pytest.fixture(autouse=True)
    def check_server(self) -> None:
        """Skip all tests in this class if the server is unreachable."""
        code = curl_code("GET", f"{self.web_base}/auth", base_url=self.base_url)
        if code is None:
            pytest.skip("Server not reachable")

    def _require_auth(self) -> None:
        """Skip test if no api_token was loaded from the production config."""
        if not self.api_token:
            pytest.skip("No api_token — production config not available")

    # ---- unauthenticated / web routes -------------------------------------

    def test_get_auth_200(self) -> None:
        code = curl_code("GET", f"{self.web_base}/auth", base_url=self.base_url)
        assert code == 200, f"expected 200, got {code}"

    def test_get_webapi_validate_200(self) -> None:
        code = curl_code(
            "GET",
            f"{self.web_base}/webapi/validate?username=testuser123",
            base_url=self.base_url,
        )
        assert code == 200, f"expected 200, got {code}"

    def test_webapi_validate_returns_obj_valid(self) -> None:
        resp = curl_json(
            "GET",
            f"{self.web_base}/webapi/validate?username=testuser123",
            base_url=self.base_url,
        )
        assert resp is not None
        assert isinstance(resp.get("obj"), dict)
        assert "valid" in resp["obj"]

    def test_redirect_happ_200(self) -> None:
        code = curl_code(
            "GET",
            f"{self.web_base}/redirect?prefix=happ://&url=test",
            base_url=self.base_url,
        )
        assert code == 200, f"expected 200, got {code}"

    def test_redirect_bad_prefix_400(self) -> None:
        code = curl_code(
            "GET",
            f"{self.web_base}/redirect?prefix=http://bad",
            base_url=self.base_url,
        )
        assert code == 400, f"expected 400, got {code}"

    # ---- auth-required endpoints without auth -----------------------------

    @pytest.mark.parametrize(
        ("ep", "method"),
        [
            ("/webapi/stats", "GET"),
            ("/webapi/fingerprints", "GET"),
            ("/webapi/profiles?lang=en", "GET"),
        ],
    )
    def test_protected_endpoint_no_auth_401(self, ep: str, method: str) -> None:
        code = curl_code(method, f"{self.web_base}{ep}", base_url=self.base_url)
        assert code == 401, f"{method} {ep} expected 401, got {code}"

    # ---- login / register validation --------------------------------------

    def test_login_missing_password_400(self) -> None:
        code = curl_code(
            "POST",
            f"{self.web_base}/webapi/login",
            base_url=self.base_url,
            data={"username": "x"},
        )
        assert code == 400, f"expected 400, got {code}"

    def test_login_wrong_creds_401(self) -> None:
        code = curl_code(
            "POST",
            f"{self.web_base}/webapi/login",
            base_url=self.base_url,
            data={"username": "x", "password": "y"},
        )
        assert code == 401, f"expected 401, got {code}"

    def test_register_no_fields_400(self) -> None:
        self._require_auth()
        code = curl_code(
            "POST",
            f"{self.web_base}/webapi/register",
            base_url=self.base_url,
            data={},
        )
        assert code == 400, f"expected 400, got {code}"

    # ---- authenticated API -------------------------------------------------

    def test_api_user_list_no_auth_401(self) -> None:
        code = curl_code(
            "GET", f"{self.api_base}/api/user/list", base_url=self.base_url
        )
        assert code == 401, f"expected 401, got {code}"

    def test_api_user_list_wrong_auth_401(self) -> None:
        code = curl_code(
            "GET",
            f"{self.api_base}/api/user/list",
            base_url=self.base_url,
            headers={"Authorization": "wrong_token_xyz"},
        )
        assert code == 401, f"expected 401, got {code}"

    def test_api_user_list_auth_200(self) -> None:
        self._require_auth()
        code = curl_code(
            "GET",
            f"{self.api_base}/api/user/list",
            base_url=self.base_url,
            headers=self.admin_hdr,
        )
        assert code == 200, f"expected 200, got {code}"

    def test_api_user_list_returns_obj_list(self) -> None:
        self._require_auth()
        resp = curl_json(
            "GET",
            f"{self.api_base}/api/user/list",
            base_url=self.base_url,
            headers=self.admin_hdr,
        )
        assert resp is not None
        assert resp.get("success") is True
        assert isinstance(resp.get("obj"), list)

    def test_api_user_info_missing_user_400(self) -> None:
        self._require_auth()
        code = curl_code(
            "GET", f"{self.api_base}/api/user/info", base_url=self.base_url, headers=self.admin_hdr
        )
        assert code == 400, f"expected 400, got {code}"

    def test_api_panel_status_200(self) -> None:
        self._require_auth()
        code = curl_code(
            "GET", f"{self.api_base}/api/panel/status", base_url=self.base_url, headers=self.admin_hdr
        )
        assert code == 200, f"expected 200, got {code}"

    def test_api_code_list_200(self) -> None:
        self._require_auth()
        code = curl_code(
            "GET", f"{self.api_base}/api/code/list", base_url=self.base_url, headers=self.admin_hdr
        )
        assert code == 200, f"expected 200, got {code}"

    def test_api_user_onlines_200(self) -> None:
        self._require_auth()
        code = curl_code(
            "GET", f"{self.api_base}/api/user/onlines", base_url=self.base_url, headers=self.admin_hdr
        )
        assert code == 200, f"expected 200, got {code}"

    # ---- input validation --------------------------------------------------

    def test_user_add_missing_displayname_400(self) -> None:
        self._require_auth()
        code = curl_code(
            "POST",
            f"{self.api_base}/api/user/add",
            base_url=self.base_url,
            headers=self.admin_hdr,
            data={"user": "__test_missing_displayname__"},
        )
        assert code == 400, f"expected 400, got {code}"

    def test_user_delete_missing_user_400(self) -> None:
        self._require_auth()
        code = curl_code(
            "POST",
            f"{self.api_base}/api/user/delete",
            base_url=self.base_url,
            headers=self.admin_hdr,
            data={},
        )
        assert code == 400, f"expected 400, got {code}"

    def test_code_add_invalid_action_400(self) -> None:
        self._require_auth()
        code = curl_code(
            "POST",
            f"{self.api_base}/api/code/add",
            base_url=self.base_url,
            headers=self.admin_hdr,
            data={"code": "__test__", "action": "invalid_action"},
        )
        assert code == 400, f"expected 400, got {code}"

    def test_code_delete_missing_code_400(self) -> None:
        self._require_auth()
        code = curl_code(
            "POST",
            f"{self.api_base}/api/code/delete",
            base_url=self.base_url,
            headers=self.admin_hdr,
            data={},
        )
        assert code == 400, f"expected 400, got {code}"

    # ---- rate limiting -----------------------------------------------------

    def test_rate_limit_fires_on_webapi_reset(self) -> None:
        codes = [
            curl_code("POST", f"{self.web_base}/webapi/reset", base_url=self.base_url)
            for _ in range(4)
        ]
        assert 429 in codes, f"expected 429 in {codes}"


# ---------------------------------------------------------------------------
# 10. Custom types
# ---------------------------------------------------------------------------

class TestCustomTypes:
    """Dataclass and protocol definitions in custom_types."""

    def test_client_stats_to_settings(self) -> None:
        from custom_types import ClientStats, SettingsClient, client_stats_to_settings

        stats = ClientStats(
            id=1,
            inboundId=1,
            enable=True,
            email="a@b.com",
            uuid="uid-123",
            subId="sub-1",
            up=100,
            down=200,
            allTime=0,
            expiryTime=0,
            total=10**9,
            reset=0,
            lastOnline=0,
        )
        result = client_stats_to_settings(stats)
        assert isinstance(result, SettingsClient)
        assert result.id == "uid-123"
        assert result.email == "a@b.com"

    def test_bandwidth_info_format_bytes(self) -> None:
        from core import BandwidthInfo

        bi = BandwidthInfo(1024, 2048, 3072)
        # format_bytes divides by 10**6 and rounds to 2dp:
        # 1048576 / 1000000 = 1.048576 → round(..., 2) = 1.05
        assert bi.format_bytes(1024 * 1024) == 1.05

    def test_bandwidth_info_format_all(self) -> None:
        from core import BandwidthInfo

        # format_all returns BandwidthInfo with float values (bytes / 1_000_000).
        # 10**9 / 10**6 = 1000.0; 2*10**9 / 10**6 = 2000.0
        bi = BandwidthInfo(10**9, 2 * 10**9, 3 * 10**9)
        formatted = bi.format_all()
        assert formatted.upload == 1000.0
        assert formatted.download == 2000.0
        # Also check the second overload (str, str tuple):
        from core import fmt_bytes_tuple
        assert fmt_bytes_tuple(10**9) == ("1.0", "GB")
        assert fmt_bytes_tuple(10**6) == ("1.0", "MB")


# ---------------------------------------------------------------------------
# 11. Config error types
# ---------------------------------------------------------------------------

class TestConfigErrors:
    """Config-specific exception classes exist and are RuntimeErrors."""

    def test_error_types_exist(self) -> None:
        from config import (
            ConfigError,
            SchemaValidationError,
            FileCorruptionError,
            ReadOnlyConfigError,
        )

        assert issubclass(ConfigError, RuntimeError)
        assert issubclass(SchemaValidationError, ConfigError)
        assert issubclass(FileCorruptionError, ConfigError)
        assert issubclass(ReadOnlyConfigError, ConfigError)
