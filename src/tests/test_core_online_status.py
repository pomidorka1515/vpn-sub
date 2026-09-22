from __future__ import annotations

from typing import cast

import pytest

from core import Subscription
from errors import PanelUnavailableError
from session import XUiSession
from helpers import FakePanel


def test_online_status_reports_empty_when_all_panels_succeed(
    subscription: Subscription,
) -> None:
    subscription.panels.append(cast(XUiSession, FakePanel(name="panel")))
    status = subscription.panel_svc.get_online_status()
    assert status.users == []
    assert status.panel_health == {"panel": "ok"}


def test_online_status_raises_when_all_panels_fail(subscription: Subscription) -> None:
    subscription.res.panels.append(
        cast(XUiSession, FakePanel(name="panel", post_error=RuntimeError("transport failed")))
    )
    with pytest.raises(PanelUnavailableError):
        subscription.panel_svc.get_online_status()


def test_online_status_reports_empty_without_panels(subscription: Subscription) -> None:
    status = subscription.panel_svc.get_online_status()
    assert status.users == []
    assert status.panel_health == {}
    assert subscription.panel_svc.get_online_status(new=True).users == {}


def test_online_status_marks_malformed_payload_invalid(subscription: Subscription) -> None:
    subscription.panels.append(
        cast(XUiSession, FakePanel(name="panel", post_payload={"success": True, "obj": 7}))
    )
    status = subscription.panel_svc.get_online_status()
    assert status.panel_health == {"panel": "invalid"}
