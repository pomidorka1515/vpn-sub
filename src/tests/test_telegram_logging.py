from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock

import pytest

from bots.telegram.logging import AsyncBotLogging
from loggers import Logger
from threadutil import named_to_thread


@pytest.mark.asyncio
async def test_colliding_and_future_loggers_follow_delivery_lifecycle() -> None:
    bot = Mock(msg=AsyncMock())
    earlier = Logger("test-telegram-panel")
    current = Logger("test-telegram-panel")
    assert logging.getLogger(current.name) is current

    owner = AsyncBotLogging(bot)
    await owner.start()
    try:
        await owner.start()
        later = await named_to_thread("test:logger:create", Logger, current.name)
        future = Logger("test-telegram-future")
        loggers = [earlier, current, later, future]
        for index, logger in enumerate(loggers):
            await named_to_thread("test:logger:emit", logger.warning, "warning %s", index)
        future.info("not an alert")
    finally:
        await owner.close()
        await owner.close()

    assert [call.args[0] for call in bot.msg.await_args_list] == [
        f"<code>WARNING [{logger.name}] warning {index}</code>"
        for index, logger in enumerate(loggers)
    ]
    after_close = Logger(current.name)
    for logger in [*loggers, after_close]:
        assert owner.handler not in logger.handlers
        logger.warning("after close")
    assert owner.delivery.dropped == 0

    # A replacement runtime must see the old instances without reviving the old handler.
    replacement = AsyncBotLogging(bot)
    await replacement.start()
    try:
        earlier.error("new runtime")
    finally:
        await replacement.close()
    assert bot.msg.await_count == len(loggers) + 1
    assert bot.msg.await_args.args == ("<code>ERROR [test-telegram-panel] new runtime</code>",)
    assert owner.delivery.dropped == 0


@pytest.mark.asyncio
async def test_telegram_delivery_deduplicates_propagated_records() -> None:
    bot = Mock(msg=AsyncMock())
    parent = logging.getLogger("test-telegram-parent")
    child = logging.getLogger("test-telegram-parent.child")
    custom = Logger("test-telegram-custom")
    custom.parent = parent
    child.setLevel(logging.WARNING)
    owner = AsyncBotLogging(bot)
    await owner.start()
    try:
        child.warning("standard")
        custom.warning("custom")
    finally:
        await owner.close()

    assert [call.args[0] for call in bot.msg.await_args_list] == [
        "<code>WARNING [test-telegram-parent.child] standard</code>",
        "<code>WARNING [test-telegram-custom] custom</code>",
    ]
    for logger in [parent, child, custom]:
        assert owner.handler not in logger.handlers
