"""3x-ui panel HTTP client.

``XUiSession`` is the public client. Transport and the two generation caches
live in sibling modules; callers keep importing ``from session import XUiSession``.
"""

from __future__ import annotations

from errors import XUiSessionError

from .client import XUiSession
from .stamp import client_stamp_path as _client_stamp_path
from .stamp import inbound_stamp_path
from .transport import RequestsPanelTransport, XUiPanelTransport

__all__ = [
    "RequestsPanelTransport",
    "XUiPanelTransport",
    "XUiSession",
    "XUiSessionError",
    "_client_stamp_path",
    "inbound_stamp_path",
]
