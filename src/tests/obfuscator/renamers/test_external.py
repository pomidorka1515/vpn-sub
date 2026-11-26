from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

from obfuscator.pipeline import obfuscate_modules
from obfuscator.renamers.allowlist import AllowlistRenamer
from tests.support.obfuscator import allowlisted

__all__: list[str] = []


def test_imported_global_type_only_proxy_is_not_dereferenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "declared_global_fixture.py").write_text("""
from __future__ import annotations
from typing import TYPE_CHECKING, Protocol
class Actual: pass
if TYPE_CHECKING:
    class Interface[T](Protocol): pass
    class Declared(Interface[Actual], Actual): pass
class Opaque:
    def __getattribute__(self, name): raise AssertionError('proxy was dereferenced')
handle: Declared = Opaque()
""", encoding="utf-8")
    (tmp_path / "exported_global_fixture.py").write_text(
        "from declared_global_fixture import handle as exported\n", encoding="utf-8",
    )
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    source = """
from exported_global_fixture import exported as handle
class Record:
    @property
    def value(self): return 7
def read_external(): return handle.value
__result__ = Record().value
"""
    try:
        result, _ = allowlisted(source, ["f:Record:value"])
        assert "handle.value" in result
        namespace: dict[str, Any] = {}
        exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
        assert namespace["__result__"] == 7
    finally:
        sys.modules.pop("declared_global_fixture", None)
        sys.modules.pop("exported_global_fixture", None)


def test_quart_request_proxy_annotation_is_unrelated() -> None:
    source = """
from quart import request
class Record:
    @property
    def path(self): return 7
def read_request(): return request.path
__result__ = Record().path
"""
    result, _ = allowlisted(source, ["f:Record:path"])
    assert "request.path" in result
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


@pytest.mark.parametrize("declaration", [
    "class Declared(Actual, Unknown): pass",
    "class Declared(Declared): pass",
    "class Declared(Protocol): pass",
    "Declared = Actual | Unknown",
    "class Declared(Actual, metaclass=Unknown): pass",
    "@unknown\n    class Declared(Actual): pass",
    "class Declared(Actual): pass\n    class Declared(Unknown): pass",
])
def test_imported_global_uncertain_type_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration: str,
) -> None:
    (tmp_path / "uncertain_global_fixture.py").write_text(f"""
from __future__ import annotations
from typing import TYPE_CHECKING, Protocol
class Actual: pass
if TYPE_CHECKING:
    {declaration}
handle: Declared = object()
""", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    source = """
from uncertain_global_fixture import handle
class Record:
    @property
    def value(self): return 7
def read_external(): return handle.value
__result__ = Record().value
"""
    try:
        with pytest.raises(ValueError, match="Unresolved"):
            allowlisted(source, ["f:Record:value"])
    finally:
        sys.modules.pop("uncertain_global_fixture", None)


def test_imported_global_related_base_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "related_global_fixture.py").write_text("""
from __future__ import annotations
from typing import TYPE_CHECKING
class Actual: pass
if TYPE_CHECKING:
    class Declared(Actual): pass
handle: Declared = object()
""", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    source = """
from related_global_fixture import Actual, handle
class Record(Actual):
    @property
    def value(self): return 7
def read_external(): return handle.value
__result__ = Record().value
"""
    try:
        with pytest.raises(ValueError, match="Unresolved"):
            allowlisted(source, ["f:Record:value"])
    finally:
        sys.modules.pop("related_global_fixture", None)


@pytest.mark.parametrize("base", [
    "MutableMapping[str, int]", "abc.MutableMapping[str, int]", "Alias", "ABC.MutableMapping",
])
def test_external_mapping_unrelated_members(base: str) -> None:
    source = f"""
from collections.abc import MutableMapping
import collections.abc as abc
Alias = MutableMapping
ABC = abc
class ConfigTransaction({base}):
    def __init__(self, config):
        self._config = config
        self.data = config
        self.original = None
        self._lock_fp = None
        self.owner_thread_id = None
        self._cfg_lock_acquired = False
    def __enter__(self):
        cfg = self._config
        self.data = cfg
        return self
    def __exit__(self, *args):
        cfg = self._config
        self.original = cfg
    def __getitem__(self, key): return self._config[key]
    def __setitem__(self, key, value): self._config[key] = value
    def __delitem__(self, key): del self._config[key]
    def __iter__(self): return iter(self._config)
    def __len__(self): return len(self._config)
    def require_active(self): return self._config['a']
record = ConfigTransaction(config={{'a': 7}})
with record:
    __result__ = record.require_active(), record.get('a')
"""
    entries = [
        "c:ConfigTransaction", "a:ConfigTransaction:__init__:config",
        "attr:ConfigTransaction:_config", "f:ConfigTransaction:require_active",
        "attr:ConfigTransaction:data", "attr:ConfigTransaction:original",
        "attr:ConfigTransaction:_lock_fp", "attr:ConfigTransaction:owner_thread_id",
        "attr:ConfigTransaction:_cfg_lock_acquired", "v:ConfigTransaction:__enter__:cfg",
        "v:ConfigTransaction:__exit__:cfg",
    ]
    result = obfuscate_modules({Path("main.py"): source}, entries, jobs=1)
    assert set(entries) <= result.renames.keys()
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (7, 7)


@pytest.mark.parametrize("entry", ["f:Record:get", "a:Record:get:key", "attr:Record:keys"])
def test_external_overrides_rejected_even_without_callers(entry: str) -> None:
    tree = ast.parse("""
from collections.abc import MutableMapping
class Record(MutableMapping):
    keys = 1
    def get(self, key): return key
""")
    with pytest.raises(ValueError, match="External inherited member conflict"):
        AllowlistRenamer(tree).apply([entry])


@pytest.mark.parametrize("base", ["missing.Base", "factory()", "dict", "Alias"])
def test_unverifiable_external_bases_rejected(base: str) -> None:
    tree = ast.parse(f"""
Alias = Alias
class Record({base}):
    def setup(self): self._value = 1
""")
    with pytest.raises(ValueError, match="Cannot verify external inheritance"):
        AllowlistRenamer(tree).apply(["attr:Record:_value"])


def test_external_instance_field_conflict() -> None:
    tree = ast.parse("""
from collections import UserDict
class Record(UserDict):
    def setup(self): self.data = {}
""")
    with pytest.raises(ValueError, match=r"conflict|Cannot verify external inheritance"):
        AllowlistRenamer(tree).apply(["attr:Record:data"])


@pytest.mark.parametrize("field", ["_external", "_helper"])
def test_external_implementation_fields_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    (tmp_path / "external_field_fixture.py").write_text("""
def helper(receiver):
    return receiver._helper
class Parent:
    def read(this):
        return this._external + helper(this)
""", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    tree = ast.parse(f"""
from external_field_fixture import Parent
class Record(Parent):
    def setup(self): self.{field} = 1
""")
    with pytest.raises(ValueError, match="External inherited member conflict"):
        AllowlistRenamer(tree).apply([f"attr:Record:{field}"])


def test_other_external_class_unrelated_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "external_parent_fixture.py").write_text("""
class Parent:
    def inherited(self): return 3
""", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    tree = ast.parse("""
from external_parent_fixture import Parent
class Record(Parent):
    def setup(self): self._value = 4
    def read(self): return self._value + self.inherited()
record = Record()
record.setup()
__result__ = record.read()
""")
    entries = ["attr:Record:_value", "f:Record:read"]
    assert set(AllowlistRenamer(tree).apply(entries)) == set(entries)
    namespace: dict[str, Any] = {}
    exec(ast.unparse(tree), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


def test_unrelated_type_reflection_does_not_block_attributes() -> None:
    tree = ast.parse("""
class Record:
    def setup(self): self._value = 4
class Other: pass
other = Other()
getattr(type(other), input())
""")
    assert "attr:Record:_value" in AllowlistRenamer(tree).apply(["attr:Record:_value"])


def test_type_reflection_on_selected_family_still_rejected() -> None:
    tree = ast.parse("""
class Record:
    def setup(self): self._value = 4
record = Record()
getattr(type(record), input())
""")
    with pytest.raises(ValueError, match="Dynamic reflection"):
        AllowlistRenamer(tree).apply(["attr:Record:_value"])


@pytest.mark.parametrize(("index", "source"), [
    (0, "class Parent:\n def read(this, key): return getattr(this, key)\n"),
    (1, "class Meta(type): pass\nclass Parent(metaclass=Meta): pass\n"),
    (2, "class Parent:\n read = len\n"),
    (3, "class Parent:\n def read(self, iter): return iter(self)\n"),
])
def test_dynamic_external_implementations_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int, source: str,
) -> None:
    module = f"external_dynamic_fixture_{index}"
    (tmp_path / f"{module}.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    tree = ast.parse(f"""
from {module} import Parent
class Record(Parent):
    def setup(self): self._value = 1
""")
    with pytest.raises(ValueError, match="Cannot verify external inheritance"):
        AllowlistRenamer(tree).apply(["attr:Record:_value"])
