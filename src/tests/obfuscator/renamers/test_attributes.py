from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from obfuscator.pipeline import obfuscate_modules
from obfuscator.renamers.allowlist import AllowlistRenamer, read_allowlist
from obfuscator.renamers.base import ArgumentRenamer

__all__: list[str] = []


def test_attribute_parser() -> None:
    assert read_allowlist('["attr:pkg/models.py:Outer:Inner:_value"]') == [
        "attr:pkg/models.py:Outer:Inner:_value"
    ]


def test_class_wide_attributes_and_reflection() -> None:
    source = """
class Record:
    def setup(self): self._value = 4
    def read(self): return self._value << 1
class Other:
    def setup(self): self._value = 9
record = Record()
record.setup()
other = Other()
other.setup()
setattr(record, '_value', getattr(record, '_value') + 1)
__result__ = record.read(), other._value, hasattr(record, '_value')
delattr(record, '_value')
__deleted__ = not hasattr(record, '_value')
"""
    result = obfuscate_modules({Path("main.py"): source}, ["attr:Record:_value"], jobs=1)
    assert "attr:Record:_value" in result.renames
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (10, 9, True)
    assert namespace["__deleted__"] is True


def test_deep_attribute_chains_and_nested_selectors() -> None:
    depth = 30
    chain = ".child" * depth
    source = """
class Outer:
    class Inner:
        def setup(self): self._value = 7
"""
    # Explicit concrete field assignments are statically inferable at every level.
    for index in range(depth):
        child = "Outer.Inner" if index == 0 else f"Link{index - 1}"
        source += (
            f"class Link{index}:\n    def __init__(self): self.child = {child}()\n"
            f"link{index} = Link{index}()\n"
        )
    source += f"link{depth - 1}{chain}.setup()\n__result__ = link{depth - 1}{chain}._value\n"
    result = obfuscate_modules(
        {Path("main.py"): source}, ["attr:main.py:Outer:Inner:_value"], jobs=1
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


@pytest.mark.parametrize(
    "usage",
    [
        "getattr(record, input())",
        "vars(record)",
        "record.__dict__",
        "print(record)",
        "unknown._value",
        "getattr(unknown, '_value')",
        "record.__getattribute__('_value')",
    ],
)
def test_unsafe_attribute_uses_fail_closed(usage: str) -> None:
    source = f"class Record:\n def setup(self): self._value = 4\nrecord = Record()\n{usage}"
    with pytest.raises(ValueError, match=r"reflect|Reflect|Escaping|Unresolved"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_attribute_subclass_conflict() -> None:
    source = """
class Record:
    def setup(self): self._value = 4
class Child(Record):
    _value = 9
"""
    with pytest.raises(ValueError, match="conflict"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_constructor_reflection_rejected() -> None:
    source = """
class Record:
    def __init__(self, value): self.value = value
record = Record(value=1)
getattr(record, '__init__')(value=2)
"""
    with pytest.raises(ValueError, match="reflected constructor"):
        AllowlistRenamer(ast.parse(source)).apply(["a:Record:__init__:value"])


def test_constructor_keywords_and_aliases() -> None:
    source = """
class Record:
    def __init__(self, value, /, *, extra=2): self.value = value + extra
Alias = Record
__result__ = Alias(3, **{'extra': 4}).value
"""
    tree = ast.parse(source)
    renames = AllowlistRenamer(tree).apply(
        ["a:Record:__init__:value", "a:Record:__init__:extra"]
    )
    assert len(renames) == 2
    namespace: dict[str, Any] = {}
    exec(ast.unparse(tree), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


def test_automatic_constructor_positional_receiver() -> None:
    tree = ast.parse(
        "class Record:\n def __init__(self, /, *, value): self.value = value\n"
        "__result__ = Record(value=5).value"
    )
    ArgumentRenamer({"sample": tree}).run()
    initializer = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assert initializer.args.kwonlyargs[0].arg != "value"
    namespace: dict[str, Any] = {}
    exec(ast.unparse(tree), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 5


def test_chain_through_automatically_renamed_self_and_selected_attributes() -> None:
    source = """
class Leaf:
    def setup(self): self._value = 8
class Holder:
    def __init__(self): self.child = Leaf()
    def read(self):
        self.child.setup()
        return self.child._value
__result__ = Holder().read()
"""
    entries = ["attr:Holder:child", "attr:Leaf:_value"]
    result = obfuscate_modules({Path("main.py"): source}, entries, jobs=1)
    assert set(entries) <= result.renames.keys()
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 8


def test_inherited_attribute_and_class_default() -> None:
    tree = ast.parse("""
class Base:
    _value = 3
    def read(self): return self._value
class Child(Base): pass
item = Child()
item._value += 2
__result__ = item.read(), Base._value
""")
    AllowlistRenamer(tree).apply(["attr:Base:_value"])
    namespace: dict[str, Any] = {}
    exec(ast.unparse(tree), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (5, 3)


@pytest.mark.parametrize(
    "source",
    [
        "class Record:\n __slots__ = ('_value',)\n def setup(self): self._value = 4",
        "class Record:\n @property\n def _value(self): return 4",
        "class Record(Unknown):\n def setup(self): self._value = 4",
        (
            "class Record:\n def __setattr__(self, name, value): pass\n"
            " def setup(self): self._value = 4"
        ),
        "class Record:\n _value = object()",
    ],
)
def test_dynamic_class_attributes_are_rejected(source: str) -> None:
    with pytest.raises(ValueError, match=r"dynamic|descriptor|external inheritance"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_conflicting_allowlist_kinds_are_rejected() -> None:
    tree = ast.parse("class Record:\n _value = 4\n__result__ = Record._value")
    with pytest.raises(ValueError, match="Conflicting"):
        AllowlistRenamer(tree).apply(["attr:Record:_value", "v:Record:_value"])


def test_attribute_file_qualifier_disambiguates_classes() -> None:
    result = obfuscate_modules(
        {
            Path("first.py"): "class Record:\n def setup(self): self._value = 1",
            Path("second.py"): "class Record:\n def setup(self): self._value = 2",
            Path("main.py"): "from first import Record as First\n"
            "from second import Record as Second\n"
            "first = First()\nsecond = Second()\nfirst.setup()\nsecond.setup()\n"
            "__result__ = first._value, second._value",
        },
        ["attr:first.py:Record:_value"],
        jobs=1,
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (1, 2)


@pytest.mark.parametrize("iterable", [
    "[*panels, *([whitelist] if whitelist else [])]",
    "[*panels, whitelist]",
    "[*panels, *[whitelist]]",
    "{*panels, whitelist}",
])
def test_unrelated_receivers_in_unpacked_containers(iterable: str) -> None:
    source = f"""
class LinesConfig:
    def __init__(self): self.log = 7
    def read(self): return self.log
class Panel:
    def __init__(self): self.log = 11
def build_panels() -> tuple[list[Panel], Panel | None]:
    return [Panel()], Panel()
def run():
    panels, whitelist = build_panels()
    result = 0
    for panel in {iterable}:
        result += panel.log
        result += getattr(panel, 'log')
    return result
__result__ = run(), LinesConfig().read()
"""
    result = obfuscate_modules({Path("main.py"): source}, ["attr:LinesConfig:log"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (44, 7)


@pytest.mark.parametrize("annotation", ["", ": list[LinesConfig | Panel]"])
def test_unpacked_unknown_or_polymorphic_receiver_fails_closed(annotation: str) -> None:
    source = f"""
class LinesConfig:
    log = 7
class Panel:
    log = 11
def read(panels{annotation}):
    for panel in [*panels]:
        panel.log
"""
    with pytest.raises(ValueError, match=r"Unresolved receiver|Ambiguous polymorphic"):
        obfuscate_modules({Path("main.py"): source}, ["attr:LinesConfig:log"], jobs=1)
