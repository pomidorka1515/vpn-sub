from __future__ import annotations

from typing import TYPE_CHECKING

from bwatch import BWatch
from tests.support.config import config_mock, subscription_config

if TYPE_CHECKING:
    from core import Subscription
    from db import Database


def make_watch(database: Database, subscription: Subscription) -> BWatch:
    return BWatch(
        cfg=config_mock(subscription_config()),
        db=database,
        sub=subscription,
        scheduled=False,
    )
