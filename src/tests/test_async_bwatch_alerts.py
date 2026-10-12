from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription
from helpers import USER_UUID

from bwatch.async_watch import AsyncBWatch
from db import AsyncDatabase
from notify.async_notifier import AsyncNotifier

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize(("used", "disabled"), [(10**9, False), (10**9 + 1, True)])
async def test_terminal_alert_uses_pre_disable_state_and_original_threshold(tmp_path: Path, used: int, disabled: bool) -> None:
    db = AsyncDatabase(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    sent: list[str] = []

    async def send(_id: int, text: str) -> None:
        sent.append(text)

    async def language(_id: int) -> str:
        return "en"

    notifier = AsyncNotifier(texts={"en": {"traffic_exhausted": "exhausted", "traffic_soon": "soon"}}, send=send, language=language)
    watch = AsyncBWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
    try:
        await db.create_user(username="alice", uuid=USER_UUID, token="a" * 40, fingerprint="chrome", displayname="Alice", bw_limit_gb=1, wl_limit_gb=0)
        await db.increment_usages({"alice": (used, 0)})
        state = await db.get_user("alice")
        assert state is not None
        from notify import classify

        notice = classify(state, 0)[0]
        notifier._texts = {"en": {notice.kind.text_key: "warning"}}
        # Deliver through the same path as polling without needing bot wiring.

        async def mappings() -> dict[str, str]:
            return {"alice": "1"}

        db.user_tgids = mappings  # type: ignore[method-assign]
        await watch.check()
        result = await db.get_user("alice")
        assert result is not None
        assert bool(result["enabled"]) is not disabled
        assert sent == ["warning"]
        assert f"alice:{notice.marker}" in await db.notification_markers()
        await watch.check()
        assert sent == ["warning"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()
