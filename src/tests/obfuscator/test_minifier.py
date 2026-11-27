from __future__ import annotations

import ast

import pytest
import python_minifier
from python_minifier.module_printer import ModulePrinter

from obfuscator.minifier import _BufferedModulePrinter, _Fragments, _PreservedNames, minify

__all__: list[str] = []


def test_fragment_buffer_trims_across_boundaries() -> None:
    fragments = _Fragments()
    assert fragments == ""
    fragments += "hello;"
    fragments += "\n\t"
    fragments += ""
    assert fragments[-1] == "\t"
    fragments.rstrip("\n\t;")
    assert str(fragments) == "hello"
    fragments.rstrip("helo")
    assert fragments == ""
    with pytest.raises(IndexError):
        _ = fragments[-1]


@pytest.mark.parametrize("single_line", [False, True])
@pytest.mark.parametrize(
    "source",
    [
        "",
        "x = 1\ny = 2\n",
        "def f(x):\n if x:\n  return f'{x!r}'\n else:\n  return None\n",
        "class C:\n def m(self):\n  try: return 'é'\n  finally: print('done')\n",
        "match value:\n case [first, *rest]: result = (first, rest)\n case _: pass\n",
        "x = t'Hello {name}'\n",
    ],
)
def test_buffered_printer_matches_dependency(source: str, single_line: bool) -> None:
    tree = ast.parse(source)
    expected = ModulePrinter(prefer_single_line=single_line)(tree)  # type: ignore[no-untyped-call]
    actual = _BufferedModulePrinter(prefer_single_line=single_line)(tree)
    assert actual == expected


def test_minifier_preserves_dependency_behavior_without_global_patching() -> None:
    source = "def long_name(value):\n return value + 1\nresult = long_name(3)\n"
    original = python_minifier.minify
    assert minify(source, rename_globals=True) == original(source, rename_globals=True)
    assert python_minifier.minify is original


def test_indexed_preserved_names_match_plain_minifier_lists() -> None:
    source = (
        "from math import ceil\n"
        "def long_name(value):\n"
        "    preserved_local = ceil(value)\n"
        "    return preserved_local + value\n"
        "result = long_name(3)\n"
    )
    locals_ = ["value", "preserved_local"]
    globals_ = ["long_name", "result"]
    expected = minify(source, rename_globals=True, preserve_locals=locals_.copy(), preserve_globals=globals_.copy())
    assert minify(
        source, rename_globals=True,
        preserve_locals=_PreservedNames(locals_), preserve_globals=_PreservedNames(globals_),
    ) == expected
