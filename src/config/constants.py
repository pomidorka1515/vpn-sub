from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Literal

SYNC_MODES = Literal['full', 'data', 'none']
CONFIG_TYPES = Literal['json', 'jsonl']

type JsonValue = int | float | Mapping[str, JsonValue] | Sequence[JsonValue] | str | bool | None
type JsonDict = dict[str, JsonValue]


class MISSING_TYPE:
    """Sentinel for missing default values."""

    def __repr__(self) -> str:
        return "<MISSING>"


MISSING: Final[MISSING_TYPE] = MISSING_TYPE()
