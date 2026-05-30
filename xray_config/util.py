from __future__ import annotations

import dataclasses
import dacite
from typing import (
    ClassVar, Protocol,
    Literal, Callable, Mapping,
    Any,
    cast, get_args, get_origin,
    runtime_checkable
)
from .validators import TYPE_HOOKS
from .core import INBOUND_MAP, OUTBOUND_MAP

__all__ = [
    "DACITE_CONFIG",
    "from_dict",
    "to_dict"
]

# mypy: disable-error-code="redundant-cast"
# NOTE: all these casts are for pyright;
# NOTE: mypy doesnt have an issue

@runtime_checkable
class DataclassInstance(Protocol):
    """A protocol for dataclass instances, matching _typeshed.DataclassInstance."""
    __dataclass_fields__: ClassVar[dict[str, Any]]

@runtime_checkable
class SlottedDataclassInstance(Protocol):
    """Protocol matching instances of slotted dataclasses."""
    __dataclass_fields__: ClassVar[dict[str, Any]]
    __slots__: ClassVar[tuple[str, ...]]


# sentinel class to mark values that should be treated as Literals
# dacite type_hooks can use this class to trigger special handling
class LiteralValue:
    """Wrapper to signal that a value should be coerced to a Literal type."""
    __slots__ = ('value', 'literal_type')

    def __init__(self, value: object, literal_type: type) -> None:
        self.value = value
        self.literal_type = literal_type


def _is_literal(tp: type) -> bool:
    """Check if a type is a Literal type."""
    return get_origin(tp) is Literal


def _get_literal_values(tp: type) -> tuple[object, ...]:
    """Extract the allowed values from a Literal type."""
    return get_args(tp)


def _coerce_to_literal[_T](value: _T, literal_type: type) -> _T | LiteralValue:
    """Coerce a value to a Literal type if it matches allowed values."""
    allowed = _get_literal_values(literal_type)
    if value in allowed:
        return LiteralValue(value, literal_type)
    return value


def _scan_and_wrap_literals(data: Mapping[str, object], target_type: type) -> Mapping[str, object]:
    """Recursively scan data and wrap values that should become Literals."""
    if not dataclasses.is_dataclass(target_type):
        return data

    result: dict[str, object] = {}
    fields = {f.name: f for f in dataclasses.fields(target_type)}

    for key, value in data.items():
        if key not in fields:
            result[key] = value
            continue

        field = fields[key]
        field_type = cast(type, field.type)

        if value is None:
            result[key] = value
            continue

        if _is_literal(field_type):
            result[key] = _coerce_to_literal(value, field_type)
        elif dataclasses.is_dataclass(field_type) and isinstance(value, dict):
            result[key] = _scan_and_wrap_literals(cast(dict[str, object], value), field_type)
        elif isinstance(value, dict):
            result[key] = _deep_scan_nested_dict(cast(dict[str, object], value), cast(type, field_type))
        elif isinstance(value, list) and value:
            origin = get_origin(field_type)
            if origin is list:
                item_type = get_args(field_type)[0] if get_args(field_type) else None
                if item_type and dataclasses.is_dataclass(item_type):
                    result[key] = [
                        _scan_and_wrap_literals(cast(dict[str, object], item), cast(type, item_type))
                        if isinstance(item, dict) else item
                        for item in cast(list[object], value)
                    ]
                else:
                    result[key] = value
            else:
                result[key] = value
        else:
            result[key] = value

    return result


def _deep_scan_nested_dict(data: Mapping[str, object], containing_type: type) -> Mapping[str, object]:
    """Recursively scan a dict that lives inside a dataclass field.

    If the field type is itself a dataclass, recurse into it.
    If the field type is a Mapping/dict with a dataclass value type, recurse into it.
    """
    if dataclasses.is_dataclass(containing_type):
        return _scan_and_wrap_literals(data, containing_type)

    origin = get_origin(containing_type)
    if origin is not None:
        args = get_args(containing_type)
        if len(args) >= 2:
            value_type = args[1]
            if dataclasses.is_dataclass(value_type):
                return _scan_and_wrap_literals(data, cast(type[DataclassInstance], value_type))
    
    return data


def _literal_type_hook(value: object) -> object:
    """Type hook that unwraps LiteralValue instances and validates against Literal types."""
    if isinstance(value, LiteralValue):
        allowed = _get_literal_values(value.literal_type)
        if value.value not in allowed:
            raise ValueError(f"value {value.value} is not in the allowed list")
        return value.value
    return value


_EXTENDED_HOOKS: dict[type[object], Callable[..., Any]] = {**TYPE_HOOKS, LiteralValue: _literal_type_hook}

DACITE_CONFIG = dacite.Config(
    type_hooks=_EXTENDED_HOOKS,
    strict=True,
    cast=[str, int, float, bool],
    
)


def _map_pass_keys(data: Mapping[str, object]) -> dict[str, object]:
    """Recursively map 'pass' -> 'pass_' for dataclass JSON serialization."""
    result: dict[str, object] = {}
    for k, v in data.items():
        if k == "pass":
            result["pass_"] = _map_pass_keys(cast(dict[str, object], v)) if isinstance(v, dict) else v
        elif isinstance(v, dict):
            result[k] = _map_pass_keys(cast(dict[str, object], v))
        elif isinstance(v, list):
            result[k] = [_map_pass_keys(cast(dict[str, object], item)) if isinstance(item, dict) else item for item in cast(list[object], v)]
        else:
            result[k] = v
    return result


def _reverse_pass_keys(data: Mapping[str, object]) -> dict[str, object]:
    """Recursively map 'pass_' -> 'pass' after dataclass serialization."""
    result: dict[str, object] = {}
    for k, v in data.items():
        if k == "pass_":
            result["pass"] = _reverse_pass_keys(cast(dict[str, object], v)) if isinstance(v, dict) else v
        elif isinstance(v, dict):
            result[k] = _reverse_pass_keys(cast(dict[str, object], v))
        elif isinstance(v, list):
            result[k] = [
                _reverse_pass_keys(cast(dict[str, object], item)) if isinstance(item, dict) else item
                for item in cast(list[object], v)
            ]
        else:
            result[k] = v
    return result


def _resolve_settings(data: Mapping[str, object], inbound: bool) -> Mapping[str, object]:
    """Pre-process settings dict based on protocol for discriminated union resolution.

    Dacite can't resolve which union member to use for settings based on the
    sibling 'protocol' field, so we pre-convert the settings dict to the correct
    typed dataclass before dacite processes it.
    """
    protocol = cast(str, data.get("protocol", ""))
    settings = data.get("settings")

    settings_map = INBOUND_MAP if inbound else OUTBOUND_MAP
    settings_type = settings_map.get(protocol)

    if settings_type and isinstance(settings, dict):
        converted_settings = cast(type, dacite.from_dict(settings_type, _map_pass_keys(cast(dict[str, object], settings)), DACITE_CONFIG))
        return {**data, "settings": converted_settings}

    return data


def _resolve_inbounds_outbounds(data: Mapping[str, object], inbound: bool) -> dict[str, object]:
    """Recursively pre-process inbounds/outbounds lists to resolve discriminated unions."""
    result: dict[str, object] = {}

    for key, value in data.items():
        if key in ("inbounds", "outbounds") and isinstance(value, list):
            items: list[dict[str, object]] = cast(list[dict[str, object]], value)
            is_inbound = key == "inbounds"
            result[key] = [_resolve_settings(item, is_inbound) for item in items]
        elif isinstance(value, dict):
            result[key] = _resolve_inbounds_outbounds(cast(dict[str, object], value), inbound)
        else:
            result[key] = value

    return result


def _strip_none(data: Mapping[str, object]) -> dict[str, object]:
    """Recursively remove None values from a dict."""
    result: dict[str, object] = {}
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, dict):
            stripped = _strip_none(cast(dict[str, object], v))
            if stripped:
                result[k] = stripped
        elif isinstance(v, list):
            stripped_list = _strip_list(cast(list[object], v))
            if stripped_list:
                result[k] = stripped_list
        else:
            result[k] = v
    return result


def _strip_list(items: list[object]) -> list[object]:
    """Recursively strip None values from a list, handling nested lists."""
    result: list[object] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, dict):
            stripped = _strip_none(cast(dict[str, object], item))
            if stripped:
                result.append(stripped)
        elif isinstance(item, list):
            nested = _strip_list(cast(list[object], item))
            if nested:
                result.append(nested)
        else:
            result.append(item)
    return result



def _strip_dataclass_none[_TJ: SlottedDataclassInstance](data_class: _TJ) -> _TJ: # SlottedDataclassInstance
    """Remove fields equal to None recursively from a slotted dataclass.

    Returns a new instance without modifying the original.
    Handles nested slotted dataclasses and lists of them.
    """
    cls = type(data_class)
    fields = dataclasses.fields(cls)
    kwargs: dict[str, object] = {}

    for field in fields:
        value = getattr(data_class, field.name)
        if value is None:
            continue

        if isinstance(value, SlottedDataclassInstance):
            value = _strip_dataclass_none(value)
        elif isinstance(value, list) and value:
            stripped_list: list[object] = []
            for item in cast(list[object], value):
                if isinstance(item, SlottedDataclassInstance):
                    stripped = _strip_dataclass_none(item)
                    # Only include if non-empty after stripping
                    if stripped is not None: # pyright: ignore[reportUnnecessaryComparison]
                        stripped_list.append(stripped)
                elif isinstance(item, (list, dict)):
                    # Handle nested lists containing slotted dataclasses
                    stripped_list.append(_strip_nested_in_list(cast(list[object] | dict[str, object], item)))
                else:
                    stripped_list.append(item)
            value = stripped_list if stripped_list else None
        elif isinstance(value, dict):
            value = _strip_none(cast(dict[str, object], value))

        if value is not None:
            kwargs[field.name] = value
    
    return cls(**kwargs)


def _strip_nested_in_list(item: object) -> object:
    """Strip nones from nested structures within a list item."""
    if isinstance(item, SlottedDataclassInstance):
        return _strip_dataclass_none(item)
    if isinstance(item, dict):
        return _strip_none(cast(dict[str, object], item))
    if isinstance(item, list):
        return _strip_list(cast(list[object], item))
    return item



def from_dict[_TV: SlottedDataclassInstance](data_class: type[_TV], data: Mapping[str, object]) -> _TV:
    """Deserialize a dataclass from a dict, with special handling for discriminated unions.

    For XrayConfig (and nested configs containing inbounds/outbounds), pre-processes
    the settings field based on the protocol value before dacite resolves union types.
    Also handles Literal type coercion by wrapping values with metadata for dacite's type hooks.
    """
    if data_class.__name__ == "XrayConfig":
        return _strip_dataclass_none(
            dacite.from_dict(
                data_class=data_class,
                data=_scan_and_wrap_literals(
                    data=_resolve_inbounds_outbounds(
                        data=_map_pass_keys(
                            data=data
                        ),
                        inbound=True
                    ),
                    target_type=data_class
                ),
                config=DACITE_CONFIG
            )
        )
    
    return _strip_dataclass_none(
        dacite.from_dict(
            data_class=data_class,
            data=_scan_and_wrap_literals(
                data=_map_pass_keys(
                    data=data
                ),
                target_type=data_class
            ),
            config=DACITE_CONFIG
        )
    )
def to_dict(obj: SlottedDataclassInstance) -> dict[str, object]:
    return _strip_none(
        _reverse_pass_keys(
            dataclasses.asdict(
                obj
            )
        )
    )

