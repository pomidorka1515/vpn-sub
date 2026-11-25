from __future__ import annotations

import inspect
from types import ModuleType

import pytest

from obfuscator.renamers.external import _static_member

__all__: list[str] = []


def test_static_member_matches_raw_class_and_module_lookup() -> None:
    class Base:
        inherited: int = 1

    class Owner(Base):
        @property
        def member(self) -> int:
            raise AssertionError("static inspection must not invoke a descriptor")

    module = ModuleType("fixture")
    vars(module)["ordinary"] = Owner
    vars(module)["__dict__"] = "shadowed"
    for owner, names in (
        (Owner, ("member", "inherited", "__dict__")),
        (module, ("ordinary", "__dict__", "__class__")),
    ):
        for name in names:
            assert _static_member(owner, name) is inspect.getattr_static(owner, name)
        assert _static_member(owner, "missing", None) is None
        with pytest.raises(AttributeError, match="missing"):
            _static_member(owner, "missing")
