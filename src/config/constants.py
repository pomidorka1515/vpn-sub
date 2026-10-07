from typing import Literal
from collections.abc import Mapping, Sequence

SYNC_MODES = Literal['full', 'data', 'none']
CONFIG_TYPES = Literal['json', 'jsonl']

type JsonValue = int | float | Mapping[str, JsonValue] | Sequence[JsonValue] | str | bool | None
type JsonDict = dict[str, JsonValue]
