from __future__ import annotations

import asyncio
import logging
from typing import cast
from unittest.mock import MagicMock

import pytest

from bots.telegram import TelegramAdminBot
from bots.telegram.logging import AsyncBotLogging
from loggers.delivery import DeliveryResult, LogDelivery
from tests.support.telegram import telegram_mock


def make_bot(recipients: list[int]) -> tuple[TelegramAdminBot, MagicMock]:
    bot = TelegramAdminBot.__new__(TelegramAdminBot)
    telegram = telegram_mock()
    bot.bot = telegram
    bot.admin_uids = recipients
    bot.log = MagicMock()
    return bot, telegram


@pytest.mark.asyncio
async def test_partial_failures_retry_only_unsuccessful_recipients() -> None:
    bot, telegram = make_bot([10, 20, 30, 20])
    attempts: list[int] = []

    async def send(uid: int, text: str, *, parse_mode: str) -> None:
        attempts.append(uid)
        if uid == 20 and attempts.count(20) < 3:
            raise RuntimeError("secret-token")

    telegram.send_message.side_effect = send
    owner = AsyncBotLogging(bot)
    await owner.start()
    owner.delivery.submit("record", important=True, fallback_text="record")
    await owner.close()

    assert attempts == [10, 20, 30, 20, 20]
    assert owner.delivery.failed == 0
    cast(MagicMock, bot.log).exception.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("recipients", [[10, 20], []])
async def test_exhaustion_or_empty_admins_count_and_fallback_safely(
    recipients: list[int], capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    bot, telegram = make_bot(recipients)
    telegram.send_message.side_effect = RuntimeError("secret-token https://example.invalid/token")
    owner = AsyncBotLogging(bot)
    await owner.start()
    owner.delivery.submit("record", important=True, fallback_text="record")
    await owner.close()

    assert owner.delivery.failed == 1
    assert telegram.send_message.await_count == len(recipients) * 3
    assert capsys.readouterr().err == "logs:Telegram: log delivery failed\n"
    assert "secret-token" not in caplog.text
    cast(MagicMock, bot.log).exception.assert_not_called()


@pytest.mark.asyncio
async def test_partial_failure_exhaustion_does_not_resend_successes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bot, telegram = make_bot([10, 20])

    async def send(uid: int, text: str, *, parse_mode: str) -> None:
        if uid == 20:
            raise RuntimeError("secret-token")

    telegram.send_message.side_effect = send
    owner = AsyncBotLogging(bot)
    await owner.start()
    owner.delivery.submit("record", important=False, fallback_text="record")
    await owner.close()

    assert [call.args[0] for call in telegram.send_message.await_args_list] == [10, 20, 20, 20]
    assert owner.delivery.failed == 1
    assert capsys.readouterr().err == "logs:Telegram: log delivery failed\n"


@pytest.mark.asyncio
async def test_retry_is_guarded_against_recursion_and_close_drains() -> None:
    bot, telegram = make_bot([10, 20])
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []
    logger = logging.getLogger("test-telegram-retry-recursion")

    async def send(uid: int, text: str, *, parse_mode: str) -> None:
        calls.append(uid)
        if uid == 20 and calls.count(20) == 1:
            raise RuntimeError("first failure")
        if uid == 20:
            logger.error("recursive transport log")
            entered.set()
            await release.wait()

    telegram.send_message.side_effect = send
    owner = AsyncBotLogging(bot)
    await owner.start()
    logger.warning("original")
    await entered.wait()
    closing = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert calls == [10, 20, 20]
    assert owner.delivery.failed == 0
    assert owner.delivery.dropped == 0


@pytest.mark.asyncio
async def test_transport_cancellation_propagates() -> None:
    bot, telegram = make_bot([10, 20])
    telegram.send_message.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await bot.send_log_alert("record")
    assert telegram.send_message.await_count == 1


@pytest.mark.asyncio
async def test_operational_alert_and_msg_contracts_remain_unchanged() -> None:
    bot, telegram = make_bot([10, 20])
    telegram.send_message.side_effect = [None, RuntimeError("failed"), None, None]
    assert await bot.send_alert("operational") is False
    await bot.msg("compatibility")
    assert telegram.send_message.await_count == 4
    cast(MagicMock, bot.log).exception.assert_called_once()
    bot.admin_uids = []
    assert await bot.send_alert("empty") is False


@pytest.mark.asyncio
async def test_generic_delivery_handles_none_and_explicit_failure() -> None:
    async def deliver(value: str) -> DeliveryResult | None:
        return None if value == "success" else DeliveryResult(failed_recipients=(10,))

    fallback: list[str] = []
    delivery = LogDelivery(deliver, name="test", fallback=fallback.append)
    await delivery.start()
    delivery.submit("success", important=False, fallback_text="")
    delivery.submit("failure", important=False, fallback_text="")
    await delivery.close()
    assert delivery.failed == 1
    assert fallback == ["test: log delivery failed"]
