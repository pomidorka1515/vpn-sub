"""One-time operational migration: reconcile legacy 3x-ui clients after the v3 upgrade.

After the panel itself upgrades to the clients-first API, its own DB migration
turns old per-inbound settings clients into standalone client rows. Our legacy
emails ``{username}-{8 random chars}`` are all distinct, so each old inbound
client becomes its own unmerged row (same uuid, different emails). The service
only recognizes ``email == username``, so without reconciliation every existing
user looks unsynced and traffic history is stranded on orphan rows.

This script merges each known user's rows into one canonical client with
``email == username``, folds the legacy rows' traffic into its counter, and
deletes the legacy rows. It is the ONLY place legacy email formats are ever
referenced; the service code has zero compat paths.

Run it while the service is stopped (BWatch tracks usage as deltas from
in-memory baselines; a running poller would see one cycle of counter noise at
worst, but stopped is the clean option). Our own ``bw_used`` monthly counters
are unaffected either way.

Rerun safety: the traffic fold is guarded by ``reconcile_seed:{panel}:{user}``
markers in the service DB recording the legacy totals already folded in. A
rerun after a partial failure only folds the increase since the last
successful seed (clamped at zero), so traffic is never counted twice; at worst
a few bytes accrued on a leftover legacy row between runs are dropped (the
safe direction). Markers are removed once a user has no legacy rows left.

Usage::

    venv/bin/python scripts/reconcile_clients.py             # dry-run (default)
    venv/bin/python scripts/reconcile_clients.py --apply     # execute
    venv/bin/python scripts/reconcile_clients.py --panel local_panel --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote

from dacite import from_dict

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from custom_types import (  # noqa: E402
    ClientListResponse,
    ClientPayload,
    InboundListResponse,
    PanelClient,
)
from db import Database  # noqa: E402
from session import XUiSession  # noqa: E402


class ReconcileError(RuntimeError):
    """A panel query or mutation failed."""


class SeedLedger(Protocol):
    """Remembers how much legacy traffic was already folded per user/panel."""

    def get(self, panel: str, username: str) -> tuple[int, int] | None: ...
    def set(self, panel: str, username: str, up: int, down: int) -> None: ...
    def clear(self, panel: str, username: str) -> None: ...


class NullSeedLedger:
    """No-op ledger: fold on every run (tests / no database available)."""

    def get(self, panel: str, username: str) -> tuple[int, int] | None:
        return None

    def set(self, panel: str, username: str, up: int, down: int) -> None:
        pass

    def clear(self, panel: str, username: str) -> None:
        pass


class DbSeedLedger:
    """DB-metadata-backed ledger; keys ``reconcile_seed:{panel}:{username}``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    @staticmethod
    def _key(panel: str, username: str) -> str:
        return f"reconcile_seed:{panel}:{username}"

    def get(self, panel: str, username: str) -> tuple[int, int] | None:
        raw = self._db.get_metadata(self._key(panel, username))
        if raw is None:
            return None
        up_text, separator, down_text = raw.partition(",")
        if not separator:
            return None
        try:
            return int(up_text), int(down_text)
        except ValueError:
            return None

    def set(self, panel: str, username: str, up: int, down: int) -> None:
        self._db.set_metadata(self._key(panel, username), f"{up},{down}")

    def clear(self, panel: str, username: str) -> None:
        self._db.delete_metadata(self._key(panel, username))


@dataclass
class PanelReport:
    panel: str
    created: list[str] = field(default_factory=list[str])
    attached: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    seeded: dict[str, tuple[int, int]] = field(default_factory=dict[str, tuple[int, int]])
    deleted: list[str] = field(default_factory=list[str])
    unknown: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    skipped: list[str] = field(default_factory=list[str])
    errors: list[str] = field(default_factory=list[str])


def _load_uuid_map(database: Database) -> dict[str, str]:
    """Map every known user's panel uuid to the service username."""
    mapping: dict[str, str] = {}
    for username in database.list_users():
        record = database.get_user(username)
        if record is not None:
            mapping[str(record["uuid"])] = username
    return mapping


def _build_panel(name: str, panel_cfg: dict[str, Any]) -> XUiSession:
    nginx_auth: tuple[str, str] | None = None
    nginx_raw: object = panel_cfg.get("nginx_auth")
    if isinstance(nginx_raw, (list, tuple)):
        sequence = cast("Sequence[object]", nginx_raw)
        if len(sequence) == 2:
            nginx_auth = (str(sequence[0]), str(sequence[1]))
    return XUiSession(
        name=str(panel_cfg["name"]),
        address=str(panel_cfg["address"]),
        port=panel_cfg["port"],
        uri=str(panel_cfg["uri"]),
        api_token=str(panel_cfg["token"]),
        https=bool(panel_cfg["https"]),
        nginx_auth=nginx_auth,
        inbounds_list=tuple(int(i) for i in panel_cfg["inbounds_list"]),
        mode=cast(Literal["whitelist", "blacklist"], panel_cfg["mode"]),
        inject_headers=panel_cfg.get("inject_headers"),
    )


def _envelope(panel: XUiSession, response: Any, what: str) -> dict[str, Any]:
    try:
        data: dict[str, Any] = dict(response.json())
    except Exception as exc:
        raise ReconcileError(f"panel {panel.name}: {what} returned invalid JSON") from exc
    if response.status_code != 200 or not data.get("success"):
        raise ReconcileError(
            f"panel {panel.name}: {what} failed: {data.get('msg') or response.status_code}"
        )
    return data


def _managed_vless_ids(panel: XUiSession) -> list[int]:
    """VLESS inbound IDs this service manages on the panel (mode filter applied).

    Reconciled attachments are intersected with this set so the canonical
    client ends up exactly where ``add_users`` would have put it.
    """
    data = _envelope(panel, panel.get("panel/api/inbounds/list"), "inbound list query")
    inbounds = from_dict(InboundListResponse, data).obj
    listed = set(panel.inbounds_list)
    if panel.mode == "whitelist":
        inbounds = [i for i in inbounds if i.id in listed]
    else:
        inbounds = [i for i in inbounds if i.id not in listed]
    return sorted(i.id for i in inbounds if i.protocol == "vless")


def _list_clients(panel: XUiSession) -> list[PanelClient]:
    data = _envelope(panel, panel.get("panel/api/clients/list"), "client list query")
    return from_dict(ClientListResponse, data).obj


def _post(panel: XUiSession, url: str, body: dict[str, Any], what: str) -> None:
    response = panel.post(url, json=body, headers={"Accept": "application/json"})
    try:
        content: dict[str, Any] = dict(response.json())
    except Exception:
        content = {}
    if response.status_code not in (200, 201) or not content.get("success"):
        raise ReconcileError(
            f"panel {panel.name}: {what} rejected: {content.get('msg') or response.status_code}"
        )


def reconcile_panel(
    panel: XUiSession,
    uuid_map: dict[str, str],
    *,
    apply: bool,
    ledger: SeedLedger | None = None,
) -> PanelReport:
    """Merge one panel's legacy client rows into canonical ``email == username`` clients.

    Idempotent: a rerun finds the canonical client, attaches nothing, folds no
    traffic (unless a leftover legacy row accrued), and deletes nothing.
    """
    report = PanelReport(panel=panel.name)
    seed_ledger = ledger if ledger is not None else NullSeedLedger()
    vless_ids = _managed_vless_ids(panel)
    clients = _list_clients(panel)

    groups: dict[str, list[PanelClient]] = {}
    for client in clients:
        groups.setdefault(client.uuid, []).append(client)

    for uuid_value, rows in sorted(groups.items()):
        username = uuid_map.get(uuid_value)
        if username is None:
            report.unknown[uuid_value] = sorted(r.email for r in rows)
            continue
        try:
            _reconcile_user(
                panel, report, username, uuid_value, rows, vless_ids,
                apply=apply, ledger=seed_ledger,
            )
        except ReconcileError as exc:
            report.errors.append(str(exc))
    return report


def _reconcile_user(
    panel: XUiSession,
    report: PanelReport,
    username: str,
    uuid_value: str,
    rows: list[PanelClient],
    vless_ids: list[int],
    *,
    apply: bool,
    ledger: SeedLedger,
) -> None:
    canonical = next((c for c in rows if c.email == username), None)
    legacy_rows = [r for r in rows if r.email != username]
    target_ids = sorted({i for r in rows for i in r.inboundIds} & set(vless_ids))
    legacy_up = sum(r.traffic.up for r in legacy_rows if r.traffic is not None)
    legacy_down = sum(r.traffic.down for r in legacy_rows if r.traffic is not None)
    legacy = sorted(r.email for r in legacy_rows)
    email_path = quote(username, safe="")

    if canonical is None and not target_ids:
        report.skipped.append(
            f"{username}: no managed VLESS inbounds on panel {panel.name}"
        )
        return

    if canonical is None:
        payload = ClientPayload(
            email=username,
            id=uuid_value,
            flow="xtls-rprx-vision",
            limitIp=0,
            totalGB=0,
            expiryTime=0,
            enable=True,
            tgId="",
            subId="",
            comment="",
            reset=0,
        )
        report.created.append(username)
        if apply:
            _post(
                panel,
                "panel/api/clients/add",
                {"client": asdict(payload), "inboundIds": target_ids},
                f"client add for {username}",
            )
    else:
        missing = [i for i in target_ids if i not in canonical.inboundIds]
        if missing:
            report.attached[username] = missing
            if apply:
                _post(
                    panel,
                    f"panel/api/clients/{email_path}/attach",
                    {"inboundIds": missing},
                    f"client attach for {username}",
                )

    if not legacy:
        # nothing left to fold; the seed marker has served its purpose
        if apply:
            ledger.clear(panel.name, username)
        return

    current: tuple[int, int] = (
        (canonical.traffic.up, canonical.traffic.down)
        if canonical is not None and canonical.traffic is not None
        else (0, 0)
    )
    folded = ledger.get(panel.name, username)
    if folded is None:
        delta = (legacy_up, legacy_down)
    else:
        # only fold what accrued since the last successful seed; clamp at
        # zero so a partially-deleted group can never re-fold old traffic
        delta = (
            max(0, legacy_up - folded[0]),
            max(0, legacy_down - folded[1]),
        )
    if delta != (0, 0) or folded is None:
        seed_value = (current[0] + delta[0], current[1] + delta[1])
        report.seeded[username] = seed_value
        if apply:
            _post(
                panel,
                f"panel/api/clients/updateTraffic/{email_path}",
                {"upload": seed_value[0], "download": seed_value[1]},
                f"traffic seed for {username}",
            )
            ledger.set(panel.name, username, legacy_up, legacy_down)

    for email in legacy:
        report.deleted.append(email)
        if apply:
            _post(
                panel,
                f"panel/api/clients/del/{quote(email, safe='')}",
                {},
                f"legacy client delete for {email}",
            )


def _print_report(key: str, report: PanelReport, *, apply: bool) -> None:
    verb = "" if apply else "would "
    print(f"[{key}] panel {report.panel}:")
    for username in report.created:
        print(f"  {verb}create canonical client for '{username}'")
    for username, ids in report.attached.items():
        print(f"  {verb}attach inbounds {ids} to '{username}'")
    for username, (up, down) in report.seeded.items():
        print(f"  {verb}seed traffic for '{username}': up={up} down={down}")
    for email in report.deleted:
        print(f"  {verb}delete legacy client '{email}'")
    for uuid_value, emails in report.unknown.items():
        print(f"  skip unknown uuid {uuid_value} (emails: {', '.join(emails)})")
    for reason in report.skipped:
        print(f"  skip: {reason}")
    for error in report.errors:
        print(f"  ERROR: {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile legacy 3x-ui client rows into clients-first canonical clients.",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("PATH_CONFIG", str(_ROOT / "data" / "config.json")),
        help="service config path (default: data/config.json or PATH_CONFIG)",
    )
    parser.add_argument(
        "--database",
        default=os.getenv("PATH_DB", str(_ROOT / "data" / "state.sqlite3")),
        help="service database path (default: data/state.sqlite3 or PATH_DB)",
    )
    parser.add_argument(
        "--panel",
        action="append",
        default=None,
        help="panel config key to reconcile (repeatable; default: all panels)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="execute changes (default: dry-run)",
    )
    args = parser.parse_args(argv)

    with open(args.config, encoding="utf-8") as handle:
        config: dict[str, Any] = json.load(handle)
    panels_cfg: dict[str, dict[str, Any]] = config["3xui"]
    selected_keys = list(panels_cfg) if not args.panel else list(args.panel)
    unknown_keys = sorted(set(args.panel or ()) - set(panels_cfg))
    if unknown_keys:
        print(f"unknown panel key(s): {', '.join(unknown_keys)}", file=sys.stderr)
        return 2

    database = Database(path=Path(args.database))
    ledger: SeedLedger = DbSeedLedger(database)
    exit_code = 0
    try:
        uuid_map = _load_uuid_map(database)
        for key in selected_keys:
            panel = _build_panel(key, panels_cfg[key])
            try:
                report = reconcile_panel(
                    panel, uuid_map, apply=args.apply, ledger=ledger,
                )
            except ReconcileError as exc:
                print(f"[{key}] FAILED: {exc}")
                exit_code = 1
                continue
            finally:
                panel.close()
            _print_report(key, report, apply=args.apply)
            if report.errors:
                exit_code = 1
    finally:
        database.close()

    if not args.apply:
        print("dry run — no changes applied (pass --apply to execute)")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
