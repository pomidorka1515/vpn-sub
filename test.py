
import sys
import os
import time
import json
import threading
import subprocess
import tempfile
import importlib
import py_compile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_CONFIG_PATH = "/tmp/test_config.json"
CONFIG_PATH = "../config.json"
BASE_URL = "http://127.0.0.1:5550"

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

passed = failed = 0

def ok(name):
    global passed
    passed += 1
    print(f"  {GREEN}✓{RESET} {name}")

def fail(name, reason=""):
    global failed
    failed += 1
    suffix = f" — {reason}" if reason else ""
    print(f"  {RED}✗{RESET} {name}{suffix}")

def section(title):
    print(f"\n{BOLD}{title}{RESET}")

# ─────────────────────────────────────────────
# 1. Module imports
# ─────────────────────────────────────────────
section("1. Module imports")

for mod in ("config", "loggers", "session", "core", "api", "bots"):
    try:
        importlib.import_module(mod)
        ok(f"import {mod}")
    except ImportError as e:
        fail(f"import {mod}", f"ImportError: {e}")
    except Exception as e:
        fail(f"import {mod}", str(e))

try:
    py_compile.compile("/var/www/sub/new/app.py", doraise=True)
    ok("syntax app.py")
except py_compile.PyCompileError as e:
    fail("syntax app.py", str(e))

# ─────────────────────────────────────────────
# 2. Config — basic API
# ─────────────────────────────────────────────
section("2. Config — basic API")

from config import Config

try:
    shutil.copy(CONFIG_PATH, TEST_CONFIG_PATH)
    c = Config(TEST_CONFIG_PATH)
    ok("Config loads real config.json")
except Exception as e:
    fail("Config loads real config.json", str(e))
    c = None

if c is not None:
    try:
        users = c['users']
        assert isinstance(users, dict)
        ok("cfg['users'] returns dict")
    except Exception as e:
        fail("cfg['users'] returns dict", str(e))

    try:
        _ = c.get('missing_key_xyz', 'default')
        ok("cfg.get() with default")
    except Exception as e:
        fail("cfg.get() with default", str(e))

    try:
        with c.edit() as tx:
            tx['_notified'].append('__test__')
            tx['_notified'].pop()
        ok("edit() transaction (append+pop)")
    except Exception as e:
        fail("edit() transaction", str(e))

    try:
        nested = False
        try:
            with c.edit() as tx:
                with c.edit() as tx2:
                    pass
        except Exception:
            nested = True
        assert nested, "expected nested transaction to raise"
        ok("nested edit() raises")
    except AssertionError as e:
        fail("nested edit() raises", str(e))

# ─────────────────────────────────────────────
# 3. Config — performance (testtime.py)
# ─────────────────────────────────────────────
section("3. Config — performance (testtime.py)")

if c is not None:
    t = time.perf_counter()
    for _ in range(100):
        _ = c['users']
    elapsed = time.perf_counter() - t
    print(f"     100 reads of 'users': {elapsed*1000:.2f}ms  ({elapsed*10:.3f}ms each)")
    if elapsed < 1.0:
        ok("100 reads in < 1s")
    else:
        fail("100 reads in < 1s", f"took {elapsed:.2f}s")

    t = time.perf_counter()
    for _ in range(100):
        with c.edit() as tx:
            tx['_notified'].append('__perf__')
            tx['_notified'].pop()
    elapsed = time.perf_counter() - t
    print(f"     100 mutating transactions: {elapsed*1000:.2f}ms  ({elapsed*10:.3f}ms each)")
    if elapsed < 5.0:
        ok("100 transactions in < 5s")
    else:
        fail("100 transactions in < 5s", f"took {elapsed:.2f}s")

# ─────────────────────────────────────────────
# 4. Config — concurrency (test.py)
# ─────────────────────────────────────────────
section("4. Config — concurrency (10 threads × 100 increments = 1000)")

tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
tmp.close()
try:
    tc = Config(tmp.name)
    tc.update(lambda d: d.setdefault('counter', 0))

    def hammer():
        for _ in range(100):
            with tc.edit() as tx:
                tx['counter'] = tx['counter'] + 1

    threads = [threading.Thread(target=hammer) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    result = tc['counter']
    if result == 1000:
        ok(f"counter == 1000 ✓")
    else:
        fail("counter == 1000", f"got {result} — race condition!")
except Exception as e:
    fail("concurrency test", str(e))
finally:
    try: os.unlink(tmp.name)
    except OSError: pass

# ─────────────────────────────────────────────
# 5. API decorator unit tests (no server)
# ─────────────────────────────────────────────
section("5. API decorators (unit)")

try:
    from api import Route, _ok, _err
    r = Route('GET', '/test', 'handler', 5)
    r.validate()
    ok("Route.validate() passes")
except Exception as e:
    fail("Route.validate() passes", str(e))

try:
    from api import Route
    bad = Route('GET', 'no-leading-slash', 'h', 1)
    raised = False
    try:
        bad.validate()
    except ValueError:
        raised = True
    assert raised
    ok("Route.validate() raises on bad path")
except Exception as e:
    fail("Route.validate() raises on bad path", str(e))

try:
    from flask import Flask
    test_app = Flask("__test__")
    with test_app.test_request_context('/'):
        r_ok, code = _ok("hello", 200, {"x": 1})
        assert code == 200
        data = json.loads(r_ok.data)
        assert data['success'] is True
        assert data['msg'] == "hello"
    ok("_ok() returns success response")
except Exception as e:
    fail("_ok() returns success response", str(e))

try:
    from flask import Flask
    test_app = Flask("__test__")
    with test_app.test_request_context('/'):
        r_err, code = _err("bad", 400)
        assert code == 400
        data = json.loads(r_err.data)
        assert data['success'] is False
    ok("_err() returns error response")
except Exception as e:
    fail("_err() returns error response", str(e))

# ─────────────────────────────────────────────
# 6. Loggers unit test
# ─────────────────────────────────────────────
section("6. Loggers")

try:
    from loggers import Logger
    log = Logger("test")
    log.info("test message")
    log.warning("test warning")
    ok("Logger.info/warning don't raise")
except Exception as e:
    fail("Logger.info/warning don't raise", str(e))

try:
    from loggers import Logger
    log = Logger("test2")
    with log.loading():
        pass
    ok("Logger.loading() context manager")
except Exception as e:
    fail("Logger.loading() context manager", str(e))

# ─────────────────────────────────────────────
# 7. cURL API endpoints
# ─────────────────────────────────────────────
section("7. API endpoints (curl)")

try:
    with open(CONFIG_PATH) as f:
        cfg_data = json.load(f)
    
    API_TOKEN = cfg_data.get('api_token', '')
    URI       = cfg_data.get('uri', 'sub')
    API_URI   = cfg_data.get('api_uri', 'privapi')
except Exception as e:
    fail("read config for curl tests", str(e))
    API_TOKEN = ""
    URI = "sub"
    API_URI = "privapi"

web_base = f"/{URI}"
api_base = f"/sub/{API_URI}"
admin_hdr = {"Authorization": API_TOKEN}
bad_hdr   = {"Authorization": "wrong_token_xyz"}

def curl_code(method, path, headers=None, data=None, timeout=5):
    cmd = ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '-X', method]
    if headers:
        for k, v in headers.items():
            cmd += ['-H', f'{k}: {v}']
    if data is not None:
        cmd += ['-H', 'Content-Type: application/json', '-d', json.dumps(data)]
    cmd.append(f'{BASE_URL}{path}')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return int(r.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError):
        return None

def curl_json(method, path, headers=None, data=None, timeout=5):
    cmd = ['curl', '-s', '-X', method]
    if headers:
        for k, v in headers.items():
            cmd += ['-H', f'{k}: {v}']
    if data is not None:
        cmd += ['-H', 'Content-Type: application/json', '-d', json.dumps(data)]
    cmd.append(f'{BASE_URL}{path}')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout)
    except Exception:
        return None

# Server health probe
ping = curl_code('GET', f'{web_base}/auth')
if ping is None:
    print(f"  {YELLOW}⚠{RESET}  Server not reachable at {BASE_URL} — skipping curl tests")
else:
    # ── WebApi public endpoints ──
    code = curl_code('GET', f'{web_base}/auth')
    if code == 200: ok("GET /auth → 200")
    else: fail("GET /auth → 200", f"got {code}")

    code = curl_code('GET', f'{web_base}/webapi/validate?username=testuser123')
    if code == 200: ok("GET /webapi/validate → 200")
    else: fail("GET /webapi/validate → 200", f"got {code}")

    resp = curl_json('GET', f'{web_base}/webapi/validate?username=testuser123')
    if resp and isinstance(resp.get('obj'), dict) and 'valid' in resp['obj']:
        ok("GET /webapi/validate returns obj.valid")
    else:
        fail("GET /webapi/validate returns obj.valid", f"got {resp}")

    code = curl_code('GET', f'{web_base}/redirect?prefix=happ://&url=test')
    if code == 200: ok("GET /redirect (happ://) → 200")
    else: fail("GET /redirect (happ://) → 200", f"got {code}")

    code = curl_code('GET', f'{web_base}/redirect?prefix=http://bad')
    if code == 400: ok("GET /redirect (bad prefix) → 400")
    else: fail("GET /redirect (bad prefix) → 400", f"got {code}")

    # ── WebApi auth-required returns 401 without cookie ──
    for ep, method in [('/webapi/stats', 'GET'), ('/webapi/fingerprints', 'GET'),
                       ('/webapi/profiles?lang=en', 'GET')]:
        code = curl_code(method, f'{web_base}{ep}')
        if code == 401: ok(f"{method} {ep} (no auth) → 401")
        else: fail(f"{method} {ep} (no auth) → 401", f"got {code}")

    # ── WebApi missing fields ──
    code = curl_code('POST', f'{web_base}/webapi/login', data={'username': 'x'})
    if code == 400: ok("POST /webapi/login (missing password) → 400")
    else: fail("POST /webapi/login (missing password) → 400", f"got {code}")

    code = curl_code('POST', f'{web_base}/webapi/login', data={'username': 'x', 'password': 'y'})
    if code == 401: ok("POST /webapi/login (wrong creds) → 401")
    else: fail("POST /webapi/login (wrong creds) → 401", f"got {code}")

    code = curl_code('POST', f'{web_base}/webapi/register', data={})
    if code == 400: ok("POST /webapi/register (no fields) → 400")
    else: fail("POST /webapi/register (no fields) → 400", f"got {code}")

    # ── Admin API auth ──
    code = curl_code('GET', f'{api_base}/api/user/list')
    if code == 401: ok("GET /api/user/list (no auth) → 401")
    else: fail("GET /api/user/list (no auth) → 401", f"got {code}")

    code = curl_code('GET', f'{api_base}/api/user/list', headers=bad_hdr)
    if code == 401: ok("GET /api/user/list (wrong auth) → 401")
    else: fail("GET /api/user/list (wrong auth) → 401", f"got {code}")

    # ── Admin API happy paths ──
    code = curl_code('GET', f'{api_base}/api/user/list', headers=admin_hdr)
    if code == 200: ok("GET /api/user/list (auth) → 200")
    else: fail("GET /api/user/list (auth) → 200", f"got {code}")

    resp = curl_json('GET', f'{api_base}/api/user/list', headers=admin_hdr)
    if resp and resp.get('success') and isinstance(resp.get('obj'), list):
        ok("GET /api/user/list returns obj list")
    else:
        fail("GET /api/user/list returns obj list", f"got {resp}")

    code = curl_code('GET', f'{api_base}/api/user/info', headers=admin_hdr)
    if code == 400: ok("GET /api/user/info (missing ?user) → 400")
    else: fail("GET /api/user/info (missing ?user) → 400", f"got {code}")

    code = curl_code('GET', f'{api_base}/api/panel/status', headers=admin_hdr)
    if code == 200: ok("GET /api/panel/status → 200")
    else: fail("GET /api/panel/status → 200", f"got {code}")

    code = curl_code('GET', f'{api_base}/api/code/list', headers=admin_hdr)
    if code == 200: ok("GET /api/code/list → 200")
    else: fail("GET /api/code/list → 200", f"got {code}")

    code = curl_code('GET', f'{api_base}/api/user/onlines', headers=admin_hdr)
    if code == 200: ok("GET /api/user/onlines → 200")
    else: fail("GET /api/user/onlines → 200", f"got {code}")

    # ── Admin API missing fields ──
    code = curl_code('POST', f'{api_base}/api/user/add', headers=admin_hdr,
                     data={'user': '__test_missing_displayname__'})
    if code == 400: ok("POST /api/user/add (missing displayname) → 400")
    else: fail("POST /api/user/add (missing displayname) → 400", f"got {code}")

    code = curl_code('POST', f'{api_base}/api/user/delete', headers=admin_hdr, data={})
    if code == 400: ok("POST /api/user/delete (missing user) → 400")
    else: fail("POST /api/user/delete (missing user) → 400", f"got {code}")

    code = curl_code('POST', f'{api_base}/api/code/add', headers=admin_hdr,
                     data={'code': '__test__', 'action': 'invalid_action'})
    if code == 400: ok("POST /api/code/add (invalid action) → 400")
    else: fail("POST /api/code/add (invalid action) → 400", f"got {code}")

    code = curl_code('POST', f'{api_base}/api/code/delete', headers=admin_hdr, data={})
    if code == 400: ok("POST /api/code/delete (missing code) → 400")
    else: fail("POST /api/code/delete (missing code) → 400", f"got {code}")

    # ── Rate limit ──
    # /webapi/reset has rate_limit=3; send 4 from same IP, 4th should be 429
    print(f"     Sending 4 rapid POSTs to /webapi/reset (rate_limit=3)...")
    rl_codes = [curl_code('POST', f'{web_base}/webapi/reset') for _ in range(4)]
    if 429 in rl_codes:
        ok(f"Rate limit fires on /webapi/reset (codes: {rl_codes})")
    else:
        fail("Rate limit fires on /webapi/reset", f"codes: {rl_codes} — no 429 seen")

# ─────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────
total = passed + failed
print(f"\n{'─'*44}")
print(f"{BOLD}Results: {passed}/{total} passed{RESET}")
if failed:
    print(f"{RED}{failed} test(s) failed{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}All tests passed!{RESET}")
