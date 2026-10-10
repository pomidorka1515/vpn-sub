"""
Module for logging detailed actions across Subscription and BWatch.
"""
from __future__ import annotations

import json
import logging

from typing import Self
from enum import StrEnum

from config import JsonValue
from loggers import Logger, Colors

__all__ = ["Op", "trace"]

class TraceOp(StrEnum):
    """
    Base class for trace operations.
    ``color`` is intended to be an ANSI color code from ``loggers``.
    """
    color: str

    def __new__(cls, value: str, color: str = "") -> Self:
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.color = color
        return obj

    @property
    def group(self) -> str:
        return self.value.split(".", 1)[0]

    @property
    def action(self) -> str:
        return self.value.split(".", 1)[1]

class Op:
    class user(TraceOp):
        add_users = "user.add_users", Colors.CYAN
        delete_user = "user.delete_user", Colors.RED
        update_user = "user.update_user", Colors.CYAN
        add_new_user = "user.add_new_user", Colors.GREEN
        update_params = "user.update_params", Colors.CYAN
        update_uuid = "user.update_uuid", Colors.YELLOW
        reset_user = "user.reset_user", Colors.YELLOW
        set_enabled = "user.set_enabled", Colors.YELLOW
        drop_cache = "user.drop_cache", Colors.GREY
        mark_rollback_failure = "user.mark_rollback_failure", Colors.MAGENTA
        get_info = "user.get_info", Colors.GREY

    class code(TraceOp):
        apply_bonus_code = "code.apply_bonus_code", Colors.GREEN
        add_code = "code.add_code", Colors.GREEN
        delete_code = "code.delete_code", Colors.RED

    class register(TraceOp):
        register_with_code = "register.register_with_code", Colors.GREEN
        rollback_registered_user = "register.rollback_registered_user", Colors.YELLOW
        recover_rollback_failures = "register.recover_rollback_failures", Colors.MAGENTA
        clear_rollback_failure = "register.clear_rollback_failure", Colors.GREEN

    class auth(TraceOp):
        set_auth_token = "auth.set_auth_token", Colors.YELLOW

    class telegram(TraceOp):
        set_telegram_language = "telegram.set_telegram_language", Colors.CYAN
        set_telegram_user = "telegram.set_telegram_user", Colors.CYAN

    class password(TraceOp):
        validate_credentials = "password.validate_credentials", Colors.YELLOW

    class panel(TraceOp):
        map_panels = "panel.map_panels", Colors.CYAN
        getstatus = "panel.getstatus", Colors.GREY
        getinbounds = "panel.getinbounds", Colors.GREY
        clients_snapshot = "panel.clients_snapshot", Colors.GREY
        invalidate_clients = "panel.invalidate_clients", Colors.YELLOW
        get_client = "panel.get_client", Colors.GREY
        list_clients = "panel.list_clients", Colors.GREY
        client_traffic = "panel.client_traffic", Colors.GREY
        online_snapshot = "panel.online_snapshot", Colors.GREY
        load_online_snapshot = "panel.load_online_snapshot", Colors.GREY

    class bandwidth(TraceOp):
        bandwidth = "bandwidth.bandwidth", Colors.GREY
        all_traffic = "bandwidth.all_traffic", Colors.GREY

    class leaderboard(TraceOp):
        leaderboard = "leaderboard.leaderboard", Colors.GREY

def trace(
    logger: Logger,
    operation: TraceOp,
    event: str,
    **fields: JsonValue
) -> None:
    """
    Logs a detailed action (DEBUG level)
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug((
        f"{operation.color}{operation}{Colors.RESET}: {event}" \
        + (" " if fields else "") + " ".join(
            f"{k}=" + (
                v if isinstance(v, str)
                else json.dumps(v, ensure_ascii=True, separators=(",", ":"))
            )
            for k, v in fields.items()
        )
    ))
