import json
import string
import random
import uuid
import time
from dataclasses import asdict
from typing import cast

from ...common import BaseService, SharedCoreResources
from .common import CommonUserService
from ..panel import PanelService
from ..bandwidth import BandwidthService
from ..password import PasswordService
from ..audit import AuditService
from custom_types import (
    SettingsClient, NewUserInfo, 
    client_stats_to_settings, ResetUserObject, UserInfo,
    UserInfoBandwidth, UserInfoBandwidthTotal
)
from errors import PanelRejectedError, ValidationError, ConflictError, DuplicateError
from session import XUiSession
from util import *

__all__ = ["BusinessUserService"]

class BusinessUserService(BaseService):
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
        """Drop cached inbounds. Call after mutations."""
        targets = [panel] if panel else list(self.panels)
        for p in targets:
            p.clear_cache()


    def get_info(self, username: str, pretty: bool = False) -> UserInfo:
        """Get all info about a user. Raises NotFoundError if it does not exist."""
        
        conf = self.cfg.copy()
        user = self.user_svc.user(username)
        bandwidths = self.bandwidth_svc.bandwidth(username=username)
        wl_bandwidths = self.bandwidth_svc.bandwidth(username=username, whitelist=True)
        monthly = int(user['bw_used'])
        wl_monthly = int(user['wl_used'])
        domain = self.cfg['domain']
        if pretty:
            bandwidths = bandwidths.format_all_mb()
            wl_bandwidths = wl_bandwidths.format_all_mb()
        return UserInfo(
            _=random.choice(cast(list[str], conf.get('funny_strings', []))),
            token=str(user['token']),
            link=f"{domain}/sub?token={user['token']}",
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


    def add_users(self, username: str, _called_internally: bool = False) -> None:
        """Sync users to panels."""
        userid = str(self.user_svc.user(username)["uuid"])
        panels = self.panels

        payload = SettingsClient(
            id=userid,
            flow="",
            email="",
            limitIp=0,
            totalGB=0,
            expiryTime=0,
            enable=True,
            tgId="",
            subId="",
            comment="",
            reset=0
        )

        for panel in panels:
            inbounds = self.panel_svc.getinbounds(panel)
            list_2_add: list[int] = []
            need_vision: list[int] = []
            for i in inbounds:
                if i.protocol != "vless":
                    self.log.debug(f"Non-VLESS inbound found ({i.protocol}). Ignoring.")
                    continue
                already_exists = any(v.uuid == userid for v in i.clientStats)
                if already_exists:
                    continue
                list_2_add.append(i.id)
                streamsettings = json.loads(i.streamSettings)
                if streamsettings['network'] in ('tcp', 'raw'):
                    need_vision.append(i.id)
            
            
            for j in list_2_add:
                client = payload
                client.email = f"{username}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}"
                client.flow = "xtls-rprx-vision" if j in need_vision else ""
                data: dict[str, int | str] = {
                    'id': j,
                    'settings': json.dumps({"clients": [asdict(client)]})
                }

                resp = panel.post(
                    url=f"panel/api/inbounds/addClient",
                    data=data,
                    headers={'Accept': 'application/json'}
                )
                content = resp.json()
                if not (resp.status_code in (200, 201) and content.get('success')):
                    raise PanelRejectedError("Panel rejected user update")
        if not _called_internally: self.audit_svc.audit(name="user_refresh", info={"username":username})
        self._drop_cache()

    def delete_user(self,
                    username: str,
                    perma: bool = False
    ) -> None:
        """Delete a user, either from panels or from storage too."""
        userid = str(self.user_svc.user(username)["uuid"])
        panels = self.panels

        for panel in panels:
            inbounds = self.panel_svc.getinbounds(panel)

            for j in inbounds:
                response = panel.post(
                    f"panel/api/inbounds/{str(j.id)}/delClient/{userid}",
                    headers={'Accept': 'application/json'}
                )
                content = response.json()
                if not (response.status_code in (200, 201) and content.get('success')):
                    raise PanelRejectedError("Panel rejected user deletion")
        if perma:
            self.db.delete_user(username)

        self.audit_svc.audit(name="user_delete", info={"username": username, "perma": perma})
        self._drop_cache()

    def update_user(self, 
                    username: str, 
                    enable: bool | None = None, 
                    timee: bool | None = None, 
                    wl_enable: bool | None = None) -> None:
        """Disable/enable a user. wl_enable controls specifically the whitelist node.
        Raises a domain error if the user is absent or a panel update fails."""
        userid = str(self.user_svc.user(username)["uuid"])
        audit_info: dict[str, str | bool] = {"username": username}
        if enable is not None:
            panels = list(self.panels)
            if self.whitelist_panel: panels.remove(self.whitelist_panel)
            for panel in panels:
                inbounds = self.panel_svc.getinbounds(panel)
                l = [i.id for i in inbounds if i.protocol == "vless"]
                the = {str(vi.id): vx for vi in inbounds for vx in vi.clientStats if vx.uuid == userid}
                for k in l:
                    if str(k) not in the: continue 
                    payload = client_stats_to_settings(the[str(k)])
                    payload.enable = enable
                    payload.id = userid
                    response = panel.post(
                        f"panel/api/inbounds/updateClient/{userid}",
                        data={'id': k, 'settings': json.dumps({"clients": [asdict(payload)]})},
                        headers={'Accept': 'application/json'}
                    )
                    content = response.json()
                    if not (response.status_code in (200, 201) and content.get('success')):
                        raise PanelRejectedError("Panel rejected user update")
            
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
                panel = self.whitelist_panel
                inbounds = self.panel_svc.getinbounds(panel)
                l = [i.id for i in inbounds if i.protocol == "vless"]
                the = {str(vi.id): vx for vi in inbounds for vx in vi.clientStats if vx.uuid == userid}

                for k in l:
                    if str(k) not in the: continue
                    payload = client_stats_to_settings(the[str(k)])
                    payload.enable = wl_enable
                    payload.id = userid

                    response = panel.post(
                        f"panel/api/inbounds/updateClient/{userid}",
                        data={'id': k, 'settings': json.dumps({"clients": [asdict(payload)]})},
                        headers={'Accept': 'application/json'}
                    )
                    content = response.json()
                    if not (response.status_code in (200, 201) and content.get('success')):
                        raise PanelRejectedError("Panel rejected whitelist user update")

            self.db.update_user(username, status_wl=wl_enable)

            audit_info['wl_enable'] = wl_enable

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
        if fingerprint is None:
            fingerprint = random.choice(self.cfg['fingerprints'])
        else:
            if fingerprint not in self.cfg['fingerprints']:
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
            return NewUserInfo(
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
        if fingerprint not in self.cfg['fingerprints']:
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

        self.audit_svc.audit(name="user_update_params", info=audit_info)
        self._drop_cache()

    def _mark_rollback_failure(self, username: str, reason: str) -> None:
        try:
            self.db.set_metadata(
                f"uuid_rollback_failed:{username}",
                f"{int(time.time())}:{reason}",
            )
        except Exception:
            self.log.critical(
                "failed to persist UUID rollback failure marker for user %s",
                username,
                exc_info=True,
            )

    def _rollback_user_uuid(
        self,
        username: str,
        old_uid: str,
        new_uid: str,
        successful: list[tuple[XUiSession, int, SettingsClient, bool]],
    ) -> None:
        """Restore old UUID on panels that were already updated before a failure.
        Logs but never raises — rollback failures are logged, not propagated."""
        failures: list[str] = []
        for panel, inbound_id, settings, had_vision in successful:
            settings.id = old_uid
            settings.flow = "xtls-rprx-vision" if had_vision else ""
            data: dict[str, int | str] = {
                'id': inbound_id,
                'settings': json.dumps({"clients": [asdict(settings)]})
            }
            try:
                resp = panel.post(
                    f"panel/api/inbounds/updateClient/{new_uid}",
                    data=data,
                    headers={'Accept': 'application/json'}
                )
                if not (resp.ok and resp.json().get('success')):
                    failures.append(f"{panel.name}:{inbound_id}:panel rejected rollback")
                    self.log.critical(
                        f"_rollback_user_uuid: panel {panel.name} inbound {inbound_id} "
                        f"failed to restore UUID {old_uid}: {resp.json().get('msg', 'unknown')}"
                    )
            except Exception as e:
                failures.append(f"{panel.name}:{inbound_id}:exception")
                self.log.critical(
                    f"_rollback_user_uuid: panel {panel.name} inbound {inbound_id} "
                    f"exception during rollback: {e}",
                    exc_info=True,
                )
        if failures:
            self._mark_rollback_failure(username, ",".join(failures))

    def update_uuid(self, username: str, uid: str) -> None:
        """Seperate method for updating the UUID.
        Potentially dangerous operation, seperate function."""
        if not isuuid(uid):
            raise ValidationError("Invalid UUID")
        olduid = str(self.user_svc.user(username)["uuid"])
        successful: list[tuple['XUiSession', int, SettingsClient, bool]] = []

        for panel in self.panels:
            inbounds = self.panel_svc.getinbounds(panel)
            l: list[int] = []
            need_vision: list[int] = []
            for i in inbounds:
                l.append(i.id)
            the: dict[str, SettingsClient] = {}
            for vi in inbounds:
                for vx in vi.clientStats:
                    if vx.uuid == olduid:
                        the[str(vi.id)] = client_stats_to_settings(vx)
                        if json.loads(vi.streamSettings)['network'] in ("tcp", "raw"):
                            need_vision.append(vi.id)
                        break
            for k in l:
                if str(k) not in the:
                    continue
                payload = the[str(k)]
                payload.id = uid
                payload.flow = "xtls-rprx-vision" if k in need_vision else ""
                data: dict[str, int | str] = {
                    'id': k,
                    'settings': json.dumps({"clients": [asdict(payload)]})
                }
                response = panel.post(
                    f"panel/api/inbounds/updateClient/{olduid}",
                    data=data,
                    headers={'Accept': 'application/json'}
                )
                if not (response.status_code in (200, 201) and response.json().get('success')):
                    err_msg: str = response.json().get('msg', 'panel rejected update')
                    self.log.critical(f"update_uuid failed on panel {panel.name}: {err_msg}")
                    self._rollback_user_uuid(username, olduid, uid, successful)
                    raise PanelRejectedError("Panel rejected UUID update")

                successful.append((panel, k, the[str(k)], k in need_vision))

        try:
            self.db.update_user(username, uuid=uid)
        except DuplicateError:
            self._rollback_user_uuid(username, olduid, uid, successful)
            raise ConflictError("UUID exists")
        self.audit_svc.audit(name="user_update_uuid", info={"username": username, "uuid": uid})
        self._drop_cache()

    def reset_user(self, username: str) -> ResetUserObject:
        """Resets token and uuid to randomness. Dict with new values on success."""
        newid = str(uuid.uuid4())
        newt = generate_token("sub")
        self.update_uuid(username, newid)
        self.update_params(
            username=username,
            token=newt
        )
        self.user_svc.set_auth_token(username, None)
        self.audit_svc.audit(name="user_reset", info={"username": username, "uuid": newid, "token": "redacted"})
        self._drop_cache()
        return ResetUserObject(uuid=newid, token=newt)
