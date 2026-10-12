"""Owned AsyncTeleBot polling without the library's detached update tasks."""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING, Any, cast

from telebot import asyncio_helper

from app_runtime.tasks import TaskRegistry

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from telebot.async_telebot import AsyncTeleBot

    from loggers import Logger

    from .handler_backend import ConversationRouter

_BOT_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+", re.IGNORECASE)
_POLLING_RETRY_DELAY = 3


class _AsyncExceptionHandler:
    def __init__(self, log: Logger, token: str) -> None:
        self.log, self.token = log, token

    async def handle(self, exception: Exception) -> bool:
        self.log.warning(
            "telegram handler failed: %s", _sanitize_polling_error(exception, self.token)
        )
        return True


def _sanitize_polling_error(error: BaseException, token: object) -> str:
    text = f"{type(error).__name__}: {error}"
    if isinstance(token, str) and token:
        text = text.replace(token, "{TOKEN}")
        _, separator, secret = token.partition(":")
        if separator and secret:
            text = text.replace(secret, "{TOKEN}")
    return _BOT_TOKEN_IN_URL.sub("/bot{TOKEN}", text)


class AsyncTelegramSession:
    """Runtime owner of telebot's loop-wide shared aiohttp session.

    Close after both bot resources have settled; individual bots cannot close
    the shared client while a sibling is polling or delivering notifications.
    """

    async def start(self) -> None:
        # Lazy creation remains with telebot's first request.
        pass

    async def close(self) -> None:
        session = cast("ClientSession | None", asyncio_helper.session_manager.session)
        if session is not None and not session.closed:
            await session.close()


class TelegramPollingMixin:
    bot: AsyncTeleBot
    log: Logger
    _conversations: ConversationRouter

    def _initialize_polling(self) -> None:
        self.bot.exception_handler = cast(Any, _AsyncExceptionHandler(self.log, self.bot.token))
        self._polling_task: asyncio.Task[None] | None = None
        self._handlers = TaskRegistry(capacity=64, report_failures=False)
        self._stopping = asyncio.Event()
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._stopping.is_set():
            raise RuntimeError("bot is closed")
        if self._polling_task is None:
            self._polling_task = asyncio.create_task(
                self._poll_forever(), name=f"{type(self).__name__}:polling"
            )

    async def _poll_forever(self) -> None:
        offset = 0
        while not self._stopping.is_set():
            try:
                updates = await self.bot.get_updates(
                    offset=offset,
                    timeout=20,
                    request_timeout=30,
                    allowed_updates=["message", "callback_query"],
                )  # pyright: ignore[reportUnknownMemberType]
                # Small bounded batches keep other chats responsive. Entry
                # handlers acquire conversation locks in update arrival order.
                for start in range(0, len(updates), 8):
                    if self._stopping.is_set():
                        return
                    batch = updates[start : start + 8]
                    await asyncio.gather(
                        *(self.bot.process_new_updates([update]) for update in batch)
                    )
                    offset = batch[-1].update_id + 1
            except Exception as error:
                self.log.warning(
                    "telegram polling failed: %s", _sanitize_polling_error(error, self.bot.token)
                )
                delay = _POLLING_RETRY_DELAY
                result: object = getattr(error, "result_json", None)
                if isinstance(result, dict):
                    parameters = cast("dict[str, object]", result).get("parameters")
                    retry = (
                        cast("dict[str, object]", parameters).get("retry_after")
                        if isinstance(parameters, dict)
                        else None
                    )
                    if isinstance(retry, int) and retry > 0:
                        delay = retry
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    async def stop(self) -> None:
        await self.close()

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name=f"{type(self).__name__}:close"
            )
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        self._stopping.set()
        self._handlers.stop_admission()
        if self._polling_task is not None:
            self._polling_task.cancel()
            await asyncio.gather(self._polling_task, return_exceptions=True)
        try:
            async with asyncio.timeout(30):
                await self._handlers.drain()
        except TimeoutError:
            await self._handlers.drain(cancel=True)
        self._conversations.clear_all()
