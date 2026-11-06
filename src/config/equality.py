from __future__ import annotations

from collections.abc import Mapping
from typing import cast


def json_equal(left: object, right: object) -> bool:
    """Compare nested JSON values without equating different value types."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        left, right = cast(Mapping[object, object], left), cast(Mapping[object, object], right)
        if left.keys() != right.keys():
            return False
        return all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        left, right = cast(list[object], left), cast(list[object], right)
        if len(left) != len(right):
            return False
        return all(map(json_equal, left, right, strict=True))
    return left == right
