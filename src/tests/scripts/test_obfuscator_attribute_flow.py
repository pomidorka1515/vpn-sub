from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from obfuscator.pipeline import obfuscate_modules
from obfuscator.renamers.allowlist import AllowlistRenamer

__all__: list[str] = []

RECORD: str = """
class Record:
    def __init__(self): self._value = 7
record = Record()
"""


def execute(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    AllowlistRenamer(tree).apply(["attr:Record:_value"])
    namespace: dict[str, Any] = {}
    exec(ast.unparse(tree), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    return namespace


@pytest.mark.parametrize("usage", [
    "def invoke(callback, tx): return callback(tx)\n__result__ = invoke(lambda item: item._value, record)",
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "alias = read\n__result__ = invoke(tx=record, callback=alias)"),
    ("def read(item): return getattr(item, '_value')\n"
     "def invoke(callback=read, *, tx): return callback(tx)\n__result__ = invoke(tx=record)"),
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "__result__ = invoke(**{'callback': read, 'tx': record})"),
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "def forward(action, item): return invoke(action, item)\n__result__ = forward(read, record)"),
    ("def read(item): return item._value\ndef other(item): return getattr(item, '_value')\n"
    "def invoke(callback, tx): return callback(tx)\n"
    "__result__ = invoke(read if True else other, record)"),
    ("def identity(item): return item\ndef forward(item): return identity(item)\n"
     "__result__ = forward(record)._value"),
    ("def read(item, count):\n if count: return read(item, count - 1)\n return item._value\n"
     "__result__ = read(record, 3)"),
    ("def factory(): return record\ndef invoke(callback): return callback()\n"
     "__result__ = invoke(factory)._value"),
    ("class Reader:\n def read(self, item): return item._value\n"
    "def invoke(callback, tx): return callback(tx)\nreader = Reader()\n"
    "alias = reader.read\n__result__ = invoke(alias, record)"),
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\n__result__ = holder.item._value"),
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\n__result__ = getattr(holder, 'item')._value"),
    ("class Holder: pass\nholder = Holder()\ndef register(callback): holder.callback = callback\n"
     "def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "register(read)\n__result__ = invoke(holder.callback, record)"),
])
def test_closed_receiver_and_callback_flows(usage: str) -> None:
    assert execute(RECORD + usage)["__result__"] == 7


@pytest.mark.parametrize("usage", [
    "def invoke(callback, tx): return callback(tx)\ninvoke(external, record)",
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "invoke(read if flag else external, record)"),
    "def helper(item): external(item)\nhelper(record)",
    "def helper(item): return getattr(item, input())\nhelper(record)",
    "def helper(item): return item.__dict__\nhelper(record)",
    "def identity(item): return item\nexternal(identity(record))",
    "def read(item): return item._value\nread(record)\nexternal(read)",
    "external(lambda: record)",
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "external(invoke)\ninvoke(read, record)"),
    "items = [record]\nexternal(items)",
    "unknown.field = record",
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\nexternal(holder)"),
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\nexternal(getattr(holder, 'item'))"),
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\nexternal(vars(holder))"),
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "unknown.callback = invoke\ninvoke(read, record)"),
    "external(record.__init__)",
    ("def decorate(function): return external(function)\nclass Reader:\n"
     " @decorate\n def read(self, item): return item._value\nReader().read(record)"),
    ("class Reader:\n def read(self, item): return item._value\nclass Unknown: pass\n"
     "reader = Reader() if flag else Unknown()\nreader.read(record)"),
    ("class Holder: pass\nholder = Holder()\ndef read(item): return item._value\n"
     "holder.callback = read\nsetattr(holder, 'callback', external)\nholder.callback(record)"),
    "record.unknown_method()",
    "def invoke(callback, tx): return callback(tx)\ninvoke(*arguments, tx=record)",
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "invoke(read, record, **arguments)"),
    "def invoke(callback, tx): return callback(tx)\ninvoke(record=record, callback=external)",
])
def test_unverifiable_flows_fail_closed(usage: str) -> None:
    with pytest.raises(ValueError, match=r"Escaping|reflect|Reflect|Unresolved|Ambiguous"):
        AllowlistRenamer(ast.parse(RECORD + usage)).apply(["attr:Record:_value"])


def test_context_transaction_callbacks_after_linking_and_automatic_renames() -> None:
    source = """
from typing import Self
class ConfigTransaction:
    def __init__(self): self.data = 7
    def __enter__(self) -> Self: return self
    def __exit__(self, *args): pass
class Config:
    def edit(self) -> ConfigTransaction: return ConfigTransaction()
    def mutate(self, callback):
        with self.edit() as tx:
            return callback(tx)
def read(item): return item.data
__result__ = Config().mutate(read)
"""
    result = obfuscate_modules({Path("main.py"): source}, ["attr:ConfigTransaction:data"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


def test_bound_static_and_class_callbacks() -> None:
    source = RECORD + """
class Reader:
    @staticmethod
    def static(item): return item._value
    @classmethod
    def class_method(cls, item): return item._value
def invoke(callback, tx): return callback(tx)
__result__ = invoke(Reader.static, record), invoke(Reader.class_method, record)
"""
    assert execute(source)["__result__"] == (7, 7)


def test_unknown_callback_does_not_partially_mutate_tree() -> None:
    tree = ast.parse(RECORD + "def invoke(callback, tx): callback(tx)\ninvoke(external, record)")
    before = ast.dump(tree)
    with pytest.raises(ValueError, match="unresolved consumer"):
        AllowlistRenamer(tree).apply(["attr:Record:_value"])
    assert ast.dump(tree) == before
