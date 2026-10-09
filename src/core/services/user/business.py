from __future__ import annotations

import random
import uuid
import time
from collections.abc import Mapping
from dataclasses import asdict
from urllib.parse import quote

from ...common import BaseService, SharedCoreResources
from tracer import Op
from .common import CommonUserService
from ..panel import PanelService
from ..bandwidth import BandwidthService
from ..password import PasswordService
from ..audit import AuditService
from custom_types import (
    ClientPayload, NewUserInfo, PanelClient,
    ResetUserObject, UserInfo,
    UserInfoBandwidth, UserInfoBandwidthTotal
)
from errors import AppError, PanelRejectedError, ValidationError, ConflictError, DuplicateError
from session import XUiSession
from config.constants import JsonValue
from util import *

__all__ = ["BusinessUserService"]


def _client_payload_from(client: PanelClient) -> ClientPayload:
    """Rebuild a full write payload from the panel read model.

    ``clients/update/{email}`` is a full row replace: everything the panel
    returned (uuid, subId, tgId, comment, ...) must be echoed back or it is
    clobbered. Never build update payloads from a hardcoded template.
    """
    return ClientPayload(
        email=client.email,
        id=client.uuid,
        flow=client.flow,
        limitIp=client.limitIp,
        totalGB=client.totalGB,
        expiryTime=client.expiryTime,
        enable=client.enable,
        tgId=client.tgId,
        subId=client.subId,
        comment=client.comment,
        reset=client.reset,
    )


def _panel_post_json(
    panel: XUiSession,
    url: str,
    body: dict[str, JsonValue],
    what: str,
) -> dict[str, object]:
    """POST a JSON mutation and require the panel's success envelope."""
    response = panel.post(url, json=body, headers={'Accept': 'application/json'})
    try:
        content: dict[str, object] = response.json()
    except Exception:
        content = {}
    if response.status_code not in (200, 201) or not content.get('success'):
        raise PanelRejectedError(
            f"Panel {panel.name} {what} rejected: "
            f"{content.get('msg') or response.status_code}"
        )
    return content


def _already_exists(exc: PanelRejectedError) -> bool:
    """True when an add lost a race with a client the snapshot had not seen.

    A stale miss must not be treated as "already attached": the duplicate
    still needs its missing inbounds, and that requires a fresh row.
    """
    message = exc.message.lower()
    return "duplicate" in message or "already exist" in message


class BusinessUserService(BaseService):
    """User lifecycle mutations against the 3x-ui clients-first API.

    Panel client email == service username: one client per user per panel,
    attached to every eligible VLESS inbound via ``inboundIds``. Panel-side
    limits stay 0 (``totalGB``/``expiryTime``/``limitIp``) on purpose: this
    service (BWatch) is the single quota authority and the panel's own
    depletion / auto-disable logic must never fire. Do not "fix" the zeros.
    """

    def __init__(
        self,
        res: SharedCoreResources,
        *,
        user_svc: CommonUserService,
        panel_svc: PanelService,
        password_svc: PasswordService,
        audit_svc: AuditService,
        bandwidth_svc: BandwidthService
    ) -> None:
        super().__init__(res)
        self.panel_svc: PanelService = panel_svc
        self.password_svc: PasswordService = password_svc
        self.user_svc: CommonUserService = user_svc
        self.audit_svc: AuditService = audit_svc
        self.bandwidth_svc: BandwidthService = bandwidth_svc
    
    def _drop_cache(self, panel: XUiSession | None = None) -> None:
        """Drop cached inbounds. Call after mutations, on the caller.

        ``clear_cache`` is a stamp bump and three local assignments. It does
        not talk to the panel, so it must not occupy a request-pool worker
        or be callable as a nested ``map_panels`` from one. A failed bump
        still fails the mutation: swallowing it would leave other processes
        on the old inbound list for the TTL.
        """
        targets = [panel] if panel else list(self.panels)
        names = [item.name for item in targets]
        try:
            for item in targets:
                item.clear_cache()
        except Exception as exc:
            self.trace(
                Op.user.drop_cache, "failed",
                targets=names, error=type(exc).__name__,
            )
            raise
        self.trace(Op.user.drop_cache, "cleared", targets=names)


    def _invalidate_clients(self, panel: XUiSession) -> None:
        """Drop one panel's client map after a successful client POST.

        A later panel that fails must still see this panel's write as gone
        from the cache. Inbound cache is a different stamp and stays.
        """
        self.panel_svc.invalidate_clients(panel)

    def _client_maps(
        self, panels: list[XUiSession] | tuple[XUiSession, ...],
    ) -> dict[str, Mapping[str, PanelClient]]:
        """One client list per panel, filled before the writes.

        Captured on the caller so workers only read. With more than one panel
        the lists themselves go through ``map_panels``; a worker must not
        call it again. Keyed by panel name, which is unique in config.
        """
        return self.panel_svc.client_maps(panels)

    def get_info(self, username: str, pretty: bool = False) -> UserInfo:
        """Get all info about a user. Raises NotFoundError if it does not exist."""
        
        conf = self.cfg.view()
        user = self.user_svc.user(username)
        bandwidths = self.bandwidth_svc.bandwidth(username=username)
        wl_bandwidths = self.bandwidth_svc.bandwidth(username=username, whitelist=True)
        monthly = int(user['bw_used'])
        wl_monthly = int(user['wl_used'])
        domain = conf['domain']
        uri = conf['uri'].strip('/')
        if pretty:
            bandwidths = bandwidths.format_all_mb()
            wl_bandwidths = wl_bandwidths.format_all_mb()
        info = UserInfo(
            _=random.choice(conf.get('funny_strings', [])),
            token=str(user['token']),
            link=f"{domain}/{uri}?token={user['token']}",
            displayname=str(user['displayname']),
            uuid=str(user['uuid']),
            fingerprint=str(user['fingerprint']),
            enabled=bool(user['enabled']),
            wl_enabled=bool(user['enabled_wl']),
            time=int(user['expires_at']),
            online=self.panel_svc.is_online(username),
            bandwidth=UserInfoBandwidth(
                total=UserInfoBandwidthTotal(
                    upload=bandwidths.upload,
                    download=bandwidths.download,
                    total=bandwidths.total
                ),
                wl_total=UserInfoBandwidthTotal(
                    upload=wl_bandwidths.upload,
                    download=wl_bandwidths.download,
                    total=wl_bandwidths.total
                ),
                monthly=monthly,
                wl_monthly=wl_monthly,
                limit=int(user['bw_limit_gb']),
                wl_limit=int(user['wl_limit_gb'])
            )
        )
        self.trace(
            Op.user.get_info, "ok",
            username=username, pretty=pretty, uuid=info.uuid,
            displayname=info.displayname, fingerprint=info.fingerprint,
            enabled=info.enabled, wl_enabled=info.wl_enabled, time=info.time,
            online=info.online, monthly=monthly, wl_monthly=wl_monthly,
            limit=int(user['bw_limit_gb']), wl_limit=int(user['wl_limit_gb']),
            upload=bandwidths.upload, download=bandwidths.download,
            total=bandwidths.total, wl_upload=wl_bandwidths.upload,
            wl_download=wl_bandwidths.download, wl_total=wl_bandwidths.total,
        )
        return info


    def add_users(
        self,
        username: str,
        _called_internally: bool = False,
        *,
        known_clients: Mapping[str, Mapping[str, PanelClient]] | None = None,
    ) -> None:
        """Sync a user to every panel (idempotent).

        Creates the panel client once per panel with ``email == username``
        and attaches it to every VLESS inbound, or attaches any inbounds
        the existing client is missing (resync path). The panel normalizes
        ``flow`` per inbound, so vision is sent once and stripped where the
        inbound cannot do TLS flow.

        ``known_clients`` is an optional per-panel snapshot from a bulk caller
        (reconcile, admin refresh). It is read-only: this call copies the
        user's row out and does not write the POST back into it, so the next
        user still sees the list taken at the start of the cycle. Omit it
        for a single user; the list is filled here, before the panel workers.
        """
        userid = str(self.user_svc.user(username)["uuid"])
        self.trace(
            Op.user.add_users, "start",
            username=username, uuid=userid,
            called_internally=_called_internally,
            known_clients=known_clients is not None,
            panels=[panel.name for panel in self.panels],
        )
        # Fill before submit. Workers must not call map_panels, and a list
        # taken after this user's own POST must not be reused in this call.
        known: Mapping[str, Mapping[str, PanelClient]] = (
            self._client_maps(tuple(self.panels)) if known_clients is None else known_clients
        )

        def sync(panel: XUiSession, clients: Mapping[str, PanelClient]) -> list[str]:
            """Create or attach on one panel. Debug notes return to the caller."""
            notes: list[str] = []
            inbounds = self.panel_svc.getinbounds(panel)
            inbound_ids: list[int] = []
            for i in inbounds:
                if i.protocol != "vless":
                    notes.append(f"Non-VLESS inbound found ({i.protocol}). Ignoring.")
                    continue
                inbound_ids.append(i.id)
            if not inbound_ids:
                return notes

            existing = clients.get(username)
            if existing is None:
                payload = ClientPayload(
                    email=username,
                    id=userid,
                    flow="xtls-rprx-vision",
                    limitIp=0,
                    totalGB=0,
                    expiryTime=0,
                    enable=True,
                    tgId=0,
                    subId="",
                    comment="",
                    reset=0
                )
                _panel_post_json(
                    panel,
                    "panel/api/clients/add",
                    {"client": asdict(payload), "inboundIds": inbound_ids},
                    "user add",
                )
                self._invalidate_clients(panel)
            elif existing.uuid != userid:
                # Shared-panel collision: the email is owned by a foreign
                # client. Attach would graft our inbounds onto someone
                # else's identity — refuse loudly instead.
                raise PanelRejectedError(
                    f"Panel {panel.name} reports email {username!r} is "
                    f"already used by a different client (uuid {existing.uuid})"
                )
            else:
                missing = [i for i in inbound_ids if i not in existing.inboundIds]
                if missing:
                    _panel_post_json(
                        panel,
                        f"panel/api/clients/{quote(username, safe='')}/attach",
                        {"inboundIds": missing},
                        "user attach",
                    )
                    self._invalidate_clients(panel)
            return notes

        def run(panel: XUiSession) -> list[str]:
            try:
                return sync(panel, known[panel.name])
            except PanelRejectedError as exc:
                if not _already_exists(exc):
                    raise
                # Snapshot missed a client another writer just added. One
                # live get, then attach whatever that row still lacks. Do
                # not treat the duplicate as fully attached.
                self._invalidate_clients(panel)
                live = self.panel_svc.get_client(panel, username)
                if live is None or live.uuid != userid:
                    raise
                inbounds = self.panel_svc.getinbounds(panel)
                missing = [
                    i.id for i in inbounds
                    if i.protocol == "vless" and i.id not in live.inboundIds
                ]
                if missing:
                    _panel_post_json(
                        panel,
                        f"panel/api/clients/{quote(username, safe='')}/attach",
                        {"inboundIds": missing},
                        "user attach",
                    )
                    self._invalidate_clients(panel)
                return [
                    f"Panel {panel.name} client already existed; attached missing inbounds."
                ]

        # First failing panel in list order still raises. Sibling writes
        # may already have landed; they are not cancelled.
        for notes in self.panel_svc.map_panels(self.panels, run):
            for note in notes:
                self.log.debug(note)
        self.trace(
            Op.user.add_users, "synced",
            username=username, uuid=userid,
            called_internally=_called_internally,
            audited=not _called_internally,
        )
        if not _called_internally: self.audit_svc.audit(name="user_refresh", info={"username":username})
        self._drop_cache()

    def delete_user(self,
                    username: str,
                    perma: bool = False
    ) -> None:
        """Delete a user, either from panels or from storage too."""
        self.user_svc.user(username)
        self.trace(
            Op.user.delete_user, "start",
            username=username, perma=perma,
            panels=[panel.name for panel in self.panels],
        )
        known = self._client_maps(tuple(self.panels))

        def delete(panel: XUiSession) -> None:
            # add_users skips panels with no managed VLESS inbounds, so a
            # client absent there is expected — deleting must not fail on it
            if known[panel.name].get(username) is None:
                return
            _panel_post_json(
                panel,
                f"panel/api/clients/del/{quote(username, safe='')}",
                {},
                "user delete",
            )
            self._invalidate_clients(panel)

        self.panel_svc.map_panels(self.panels, delete)
        if perma:
            self.db.delete_user(username)

        self.trace(
            Op.user.delete_user, "deleted",
            username=username, perma=perma,
        )
        self.audit_svc.audit(name="user_delete", info={"username": username, "perma": perma})
        self._drop_cache()

    def _set_enabled(
        self,
        panel: XUiSession,
        username: str,
        enabled: bool,
        clients: Mapping[str, PanelClient],
    ) -> None:
        """Flip a panel client's enable flag via the bulk endpoints.

        One email per call; bulkEnable/bulkDisable preserve every other
        client field by construction. A ``success: false`` reply (unknown
        client, duplicate email, ...) surfaces as ``PanelRejectedError``
        carrying the panel ``msg``.

        A client absent from the panel is expected — ``add_users`` skips
        panels with no managed VLESS inbounds — so there is nothing to
        flip there. Without this skip, one absent-panel rejection would
        block the disable everywhere and BWatch would retry forever.

        ``clients`` is the caller's snapshot, captured before ``map_panels``.
        A worker must not fill it: that would nest a submit on the same pool.
        """
        if clients.get(username) is None:
            self.trace(
                Op.user.set_enabled, "skipped",
                panel=panel.name, username=username, enabled=enabled,
                reason="absent",
            )
            return
        action = "bulkEnable" if enabled else "bulkDisable"
        _panel_post_json(
            panel,
            f"panel/api/clients/{action}",
            {"emails": [username]},
            "user update",
        )
        self.trace(
            Op.user.set_enabled, "applied",
            panel=panel.name, username=username, enabled=enabled, action=action,
        )
        self._invalidate_clients(panel)

    def update_user(self, 
                    username: str, 
                    enable: bool | None = None, 
                    timee: bool | None = None,
                    wl_enable: bool | None = None) -> None:
        """Disable/enable a user. wl_enable controls specifically the whitelist node.
        Raises a domain error if the user is absent or a panel update fails."""
        self.user_svc.user(username)
        self.trace(
            Op.user.update_user, "start",
            username=username, enable=enable, timee=timee, wl_enable=wl_enable,
            whitelist_panel=self.whitelist_panel.name if self.whitelist_panel else None,
        )
        audit_info: dict[str, str | bool] = {"username": username}
        if enable is not None:
            panels = [p for p in self.panels if p != self.whitelist_panel]
            known = self._client_maps(panels)
            self.panel_svc.map_panels(
                panels,
                lambda panel: self._set_enabled(
                    panel, username, enable, known[panel.name],
                ),
            )
            
            fields: dict[str, object] = {"status": enable}
            if timee is not None:
                fields["status_time"] = timee
            self.db.update_user(username, **fields)
            
            audit_info['enable'] = enable
        if wl_enable is not None:
            # Only push the change to the whitelist panel if one is actually
            # configured. Regardless of that, the local `statusWl` bookkeeping
            # below must still be updated -- otherwise (see the `enable` branch
            # above, which always writes to cfg) callers silently get a "success"
            # (None) return value while the requested state never gets recorded,
            # and BWatch.check() would keep re-triggering the same disable/enable
            # every cycle since `statusWl` never flips.
            if self.whitelist_panel:
                wl_known = self.panel_svc.clients_snapshot(self.whitelist_panel)
                self._set_enabled(
                    self.whitelist_panel, username, wl_enable, wl_known,
                )

            self.db.update_user(username, status_wl=wl_enable)

            audit_info['wl_enable'] = wl_enable

        self.trace(
            Op.user.update_user, "updated",
            username=username, enable=enable, timee=timee, wl_enable=wl_enable,
        )
        self.audit_svc.audit(name="user_update", info=audit_info)
        self._drop_cache()

    def add_new_user(
        self,
        username: str,
        displayname: str,
        ext_username: str | None = None,
        ext_password: str | None = None,
        token: str | None = None,
        userid: str | None = None,
        fingerprint: str | None = None,
        limit: int = 0,
        wl_limit: int = 5,
        timee: int = 0
    ) -> NewUserInfo:
        """Adds a new user. Raises a domain error if any argument is incorrect.
        Now also suppports ext username and password (optional)"""
        self.trace(
            Op.user.add_new_user, "start",
            username=username, displayname=displayname,
            ext_username=ext_username, userid=userid, fingerprint=fingerprint,
            limit=limit, wl_limit=wl_limit, timee=timee,
            has_token=token is not None, has_password=ext_password is not None,
        )
        if not isusername(username):
            # the username becomes the panel client email (clients-first API
            # join key) — the panel rejects emails with whitespace/slashes
            raise ValidationError("Invalid username")
        if ext_username:
            if len(ext_username) > 32:
                raise ValidationError("Ext Username too long")
            ext_username = sanitize(ext_username, "external")
        if token is None:
            token = generate_token("sub")
        if userid is None:
            userid = str(uuid.uuid4())
        else:
            if not isuuid(userid):
                raise ValidationError("Invalid UUID")
        conf = self.cfg.view()
        if fingerprint is None:
            fingerprint = random.choice(conf['fingerprints'])
        else:
            if fingerprint not in conf['fingerprints']:
                raise ValidationError("Invalid fingerprint")
        if timee > 2**31:
            raise ValidationError("Invalid timestamp")
        if ext_password is not None:
            ext_password = self.password_svc.hash(ext_password)
        else:
            ext_username = None

        
        if len(displayname) > 16:
            raise ValidationError("Displayname too long")
        displayname = sanitize(displayname, "display")
        try:
            self.db.create_user(
                username=username, uuid=userid, token=token, fingerprint=fingerprint,
                displayname=displayname, expires_at=timee, bw_limit_gb=limit,
                wl_limit_gb=wl_limit, ext_username=ext_username,
                ext_password_hash=ext_password,
            )
        except DuplicateError:
            raise ConflictError("Username or external username exists")
        
        try:
            self.add_users(username=username, _called_internally=True)
            created = NewUserInfo(
                username=username,
                token=token,
                uuid=userid,
                fingerprint=fingerprint,
                displayname=displayname
            )
        except Exception:
            try:
                self.db.delete_user(username)
            except Exception:
                self.log.error("failed to roll back user %s", username, exc_info=True)
                try:
                    self.delete_user(username=username, perma=True)
                except Exception:
                    self.log.critical(
                        "user %s may be inconsistent after failed DB rollback",
                        username,
                        exc_info=True,
                    )
            raise
        finally:
            to_log: dict[str, str] = {"username": username}
            if ext_username:
                to_log['ext_username'] = ext_username
            self.audit_svc.audit(name="user_add", info=to_log)
            self._drop_cache()
        self.trace(
            Op.user.add_new_user, "created",
            username=username, uuid=userid, fingerprint=fingerprint,
            displayname=displayname, ext_username=ext_username,
            limit=limit, wl_limit=wl_limit, timee=timee,
        )
        return created

    def update_params(
        self,
        username: str,
        displayname: str | None = None,
        ext_username: str | None = None,
        ext_password: str | None = None,
        token: str | None = None,
        fingerprint: str | None = None,
        limit: int | None = None,
        wl_limit: int | None = None,
        timee: int | None = None
    ) -> None:
        """Updates certain fields for any user. Changing UUIDs isnt supported."""
        current = self.user_svc.user(username)
        self.trace(
            Op.user.update_params, "start",
            username=username, displayname=displayname, ext_username=ext_username,
            fingerprint=fingerprint, limit=limit, wl_limit=wl_limit, timee=timee,
            has_token=token is not None, has_password=ext_password is not None,
        )
        audit_info: dict[str, str | int] = {"username": username}
        if displayname is None:
            displayname = str(current["displayname"])
        else:
            if len(displayname) > 16:
                raise ValidationError("Displayname too long")
            displayname = sanitize(displayname, "display")
            audit_info['displayname'] = displayname
        token = token if token is not None else str(current["token"])
        if fingerprint is None:
            fingerprint = str(current["fingerprint"])
        else:
            audit_info['fingerprint'] = fingerprint
        conf = self.cfg.view()
        if fingerprint not in conf['fingerprints']:
            raise ValidationError("Invalid fingerprint")
        limit = int(current["bw_limit_gb"]) if limit is None else limit
        wl_limit = int(current["wl_limit_gb"]) if wl_limit is None else wl_limit
        timestamp = int(current["expires_at"]) if timee is None else timee
        if timestamp > 2**31:
            raise ValidationError("Invalid time")
        old_ext = current["ext_username"]
        if ext_username is None:
            ext_username = str(old_ext) if old_ext is not None else None
        else:
            if len(ext_username) > 32:
                raise ValidationError("Username too long")
            ext_username = sanitize(ext_username, "external")
            audit_info['ext_username'] = ext_username
        if ext_password is None:
            password_hash = current["ext_password_hash"]
        else:
            password_hash = self.password_svc.hash(ext_password)
        try:
            self.db.update_user(
                username, displayname=displayname, token=token, fingerprint=fingerprint,
                bw_limit=limit, wl_bw_limit=wl_limit, expiry_time=timestamp,
                ext_username=ext_username, ext_password=password_hash,
            )
        except DuplicateError:
            raise ConflictError("Ext username exists")

        self.trace(
            Op.user.update_params, "updated",
            username=username, displayname=displayname, ext_username=ext_username,
            fingerprint=fingerprint, limit=limit, wl_limit=wl_limit, timee=timestamp,
        )
        self.audit_svc.audit(name="user_update_params", info=audit_info)
        self._drop_cache()

    def _mark_rollback_failure(self, username: str, reason: str) -> None:
        try:
            self.db.set_metadata(
                f"uuid_rollback_failed:{username}",
                f"{int(time.time())}:{reason}",
            )
            self.trace(
                Op.user.mark_rollback_failure, "persisted",
                username=username, reason=reason,
            )
        except Exception:
            self.trace(
                Op.user.mark_rollback_failure, "failed",
                username=username, reason=reason,
            )
            self.log.critical(
                "failed to persist UUID rollback failure marker for user %s",
                username,
                exc_info=True,
            )

    def update_uuid(self, username: str, uid: str) -> None:
        """Seperate method for updating the UUID.
        Potentially dangerous operation, seperate function.

        One atomic ``clients/update/{email}`` per panel (full row replace
        with only ``id`` changed). There is no compensation loop: if a
        later panel (or the DB write) fails, earlier panels keep the new
        UUID and the DB keeps the old one — exactly the state the
        ``uuid_rollback_failed`` marker records for manual repair.
        """
        if not isuuid(uid):
            raise ValidationError("Invalid UUID")
        self.user_svc.user(username)
        self.trace(
            Op.user.update_uuid, "start",
            username=username, uuid=uid,
            panels=[panel.name for panel in self.panels],
        )

        # Single-user path. get_client stays; the list cache is for loops.
        def write(panel: XUiSession) -> AppError | None:
            try:
                current = self.panel_svc.get_client(panel, username)
                if current is None:
                    return None  # not synced to this panel yet
                payload = _client_payload_from(current)
                payload.id = uid
                _panel_post_json(
                    panel,
                    f"panel/api/clients/update/{quote(username, safe='')}",
                    asdict(payload),
                    "UUID update",
                )
                self._invalidate_clients(panel)
            except AppError as exc:
                return exc
            return None

        # Parallel writes, sequential failure handling. The marker names
        # the first failing panel in list order, same as the old loop.
        # Later panels may already hold the new UUID; that is the recorded
        # state, not something this method rolls back.
        for panel, exc in zip(
            self.panels, self.panel_svc.map_panels(self.panels, write), strict=True
        ):
            if exc is None:
                continue
            self.log.critical(
                "update_uuid failed on panel %s", panel.name, exc_info=exc,
            )
            self._mark_rollback_failure(username, f"{panel.name}: {exc.message}")
            raise exc

        try:
            self.db.update_user(username, uuid=uid)
        except DuplicateError:
            self._mark_rollback_failure(username, "db duplicate uuid")
            raise ConflictError("UUID exists")
        self.trace(
            Op.user.update_uuid, "updated",
            username=username, uuid=uid,
        )
        self.audit_svc.audit(name="user_update_uuid", info={"username": username, "uuid": uid})
        self._drop_cache()

    def reset_user(self, username: str) -> ResetUserObject:
        """Resets token and uuid to randomness. Dict with new values on success."""
        self.trace(Op.user.reset_user, "start", username=username)
        newid = str(uuid.uuid4())
        newt = generate_token("sub")
        self.update_uuid(username, newid)
        self.update_params(
            username=username,
            token=newt
        )
        self.user_svc.set_auth_token(username, None)
        self.trace(Op.user.reset_user, "reset", username=username, uuid=newid)
        self.audit_svc.audit(name="user_reset", info={"username": username, "uuid": newid, "token": "redacted"})
        self._drop_cache()
        return ResetUserObject(uuid=newid, token=newt)
