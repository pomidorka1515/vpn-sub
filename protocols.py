from __future__ import annotations

from typing import (
    Protocol, runtime_checkable, Any
)

@runtime_checkable
class AdminBotLike(Protocol):
    """Stub protocol for AdminBot."""

    def msg(self, text: str, parse_mode: str = "HTML") -> None: ...


@runtime_checkable
class PublicBotLike(Protocol):
    """Stub protocol for PublicBot."""

    def msg(
        self, 
        tgid: int | str | None,
        key: str,
        **kwargs: Any
    ) -> None: ... 

