from __future__ import annotations

import ast
from typing import Any

import pytest

pytest.importorskip("python_minifier")

import obfuscator

__all__: list[str] = []


def test_annotations_resolve_deeply_chained_receivers_and_returns() -> None:
    source = """
class Database:
    def _query(self, long_parameter):
        return long_parameter * 2
class Resources:
    db: Database
class Service:
    def __init__(self, res: Resources):
        self.res = res
    def _database(self) -> Database:
        return self.res.db
    def run(self):
        return self._database()._query(long_parameter=4)
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    tree = ast.parse(result)
    methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not {"_query", "_database"} & methods
    assert not any(
        isinstance(node, ast.AnnAssign) or (isinstance(node, ast.arg) and node.annotation)
        for node in ast.walk(tree)
    )
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    database = namespace["Database"]()
    resources = namespace["Resources"]()
    resources.db = database
    service = namespace["Service"](resources)
    assert service.run() == 8


def test_annotations_follow_nested_dataclass_and_container_chains() -> None:
    source = """
from dataclasses import dataclass
@dataclass
class Database:
    def _query(self, long_parameter):
        return long_parameter * 2
@dataclass
class Pool:
    db: Database
@dataclass
class Config:
    pool: Pool
    replicas: list[Pool]
class Service:
    def __init__(self, config: Config):
        self.config = config
    def _config(self) -> Config:
        return self.config
    def run(self):
        db, rest = self._config().pool.db, self._config().replicas
        return db._query(long_parameter=4) + rest[0].db._query(long_parameter=5)
result = Service(Config(Pool(Database()), [Pool(Database())])).run()
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    methods = {
        node.name for node in ast.walk(ast.parse(result)) if isinstance(node, ast.FunctionDef)
    }
    assert not {"_query", "_config"} & methods
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 18


def test_annotations_follow_async_for_and_context_manager_chains() -> None:
    source = """
import asyncio
class Database:
    def _query(self, long_parameter):
        return long_parameter * 2
    def __enter__(self) -> "Database":
        return self
    def __exit__(self, *args):
        return None
class Service:
    def _database(self) -> Database:
        return Database()
    def _databases(self) -> list[Database]:
        return [Database()]
    async def _async_database(self) -> Database:
        return Database()
    async def run(self):
        total = 0
        with self._database() as db:
            total += db._query(long_parameter=4)
        for item in self._databases():
            total += item._query(long_parameter=5)
        total += (await self._async_database())._query(long_parameter=6)
        return total
result = asyncio.run(Service().run())
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    methods = {
        node.name for node in ast.walk(ast.parse(result)) if isinstance(node, ast.FunctionDef)
    }
    assert not {"_query", "_database", "_databases", "_async_database"} & methods
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 30


def test_annotations_follow_dataclass_factories_and_mapping_values() -> None:
    source = """
from dataclasses import dataclass, field
class Database:
    def _query(self, long_parameter):
        return long_parameter * 2
@dataclass
class Service:
    db: Database = field(default_factory=Database)
    databases: dict[str, Database] = field(default_factory=dict)
    def run(self):
        return self.db._query(long_parameter=4) + self.databases.get("main")._query(long_parameter=5)
result = Service(db=Database(), databases={"main": Database()}).run()
"""
    result = obfuscator.obfuscate(source, "fixture.py")
    methods = {
        node.name for node in ast.walk(ast.parse(result)) if isinstance(node, ast.FunctionDef)
    }
    assert "_query" not in methods
    namespace: dict[str, Any] = {}
    exec(result, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["result"] == 18
