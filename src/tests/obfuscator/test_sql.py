from __future__ import annotations

import ast
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from obfuscator.pipeline import obfuscate, obfuscate_modules
from obfuscator.sql import minify_sql, minify_sql_literals

__all__: list[str] = []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("\n SELECT *\n FROM users WHERE id = ? \n", "SELECT*FROM users WHERE id=?"),
        ("SELECT a, b FROM t WHERE a >= 1 AND b != 2", "SELECT a,b FROM t WHERE a>=1 AND b!=2"),
        ("SELECT 1;\n SELECT 2;", "SELECT 1;SELECT 2;"),
        ("SELECT :name, @name, $name, ?123", "SELECT:name,@name,$name,?123"),
        ("SELECT 1 /* comment */ + 2 -- comment\n AS n", "SELECT 1+2 AS n"),
        ("SELECT 1 -- comment\r still comment\n + 2", "SELECT 1+2"),
        ("SELECT - - 1 AS n", "SELECT- -1 AS n"),
        ("SELECT 1 FROM t", "SELECT 1 FROM t"),
        ("SELECT 'a  b\nc', \"a  b\", x'00ff'", "SELECT'a  b\nc',\"a  b\",x'00ff'"),
        ("PRAGMA foreign_keys = ON", "PRAGMA foreign_keys=ON"),
        ("CREATE TABLE t (a INTEGER, b TEXT)", "CREATE TABLE t(a INTEGER,b TEXT)"),
        ("INSERT INTO t (a) VALUES (?)", "INSERT INTO t(a)VALUES(?)"),
        ("WITH c AS (SELECT 1 AS n) SELECT n FROM c", "WITH c AS(SELECT 1 AS n)SELECT n FROM c"),
    ],
)
def test_minimal_sql(source: str, expected: str) -> None:
    assert minify_sql(source) == expected
    assert minify_sql(expected) == expected


@pytest.mark.parametrize(
    "source",
    [
        "",
        "hello world",
        "SELECT",
        " SELECT ",
        "SELECT FROM t",
        "SELECT * FROM",
        "UPDATE t SET",
        "INSERT INTO t",
        "WHERE a = ?",
        "ORDER BY a",
        "SELECT 1; garbage",
        "SELECT 'unterminated",
        "SELECT (1",
        "SELECT 1 +",
        "SELECT 1 /* unterminated",
        "SELECT {fields} FROM t",
        "DELETE FROM",
        "/* only comments */",
        # Trigger wrappers must be complete, and their bodies are checked too.
        "CREATE TRIGGER t AFTER INSERT ON a BEGIN INSERT INTO b VALUES(NEW.x)",
        "CREATE TRIGGER t AFTER INSERT ON a BEGIN INSERT INTO b VALUES(NEW.x);",
        "CREATE TRIGGER t AFTER INSERT ON a INSERT INTO b VALUES(NEW.x); END",
        "CREATE TRIGGER t WHEN BEGIN INSERT INTO b VALUES(NEW.x); END",
        "CREATE TRIGGER t WHEN OLD.x = NEW.x BEGIN INSERT INTO b VALUES(; END",
        "CREATE TRIGGER t AFTER INSERT ON a BEGIN SELECT 1 +; END",
        "CREATE TRIGGER missing",
    ],
)
def test_non_sql_incomplete_and_unsupported_strings_are_unchanged(source: str) -> None:
    assert minify_sql(source) == source


def test_unsupported_syntax_does_not_log_warnings(caplog: pytest.LogCaptureFixture) -> None:
    minify_sql.cache_clear()
    source = "CREATE TRIGGER t AFTER INSERT ON a INSERT INTO b VALUES(NEW.x); END"
    assert minify_sql(source) == source
    assert not caplog.records


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1 + 2 AS n, - - 3 AS m, 1.5e-2 AS x",
        "SELECT 1 AS n -- comment\n UNION ALL SELECT 2 AS n ORDER BY n",
        "SELECT 'a  b\nc' AS value, 'it''s fine' AS quote, X'00FF' AS blob",
        'SELECT 1 AS "a  b", 2 AS [bracket name], 3 AS `backtick name`',
        "SELECT ?1 + :n AS n, @value AS v, $text AS t",
        "WITH c(n) AS (VALUES (1), (2)) SELECT n * 2 AS doubled FROM c WHERE n > 0",
    ],
)
def test_sqlite_results_and_parameters_are_preserved(query: str) -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        bindings: dict[str, Any] = {"1": 4, "n": 5, "value": "a b", "text": "x\ny"}
        original = connection.execute(query, bindings)
        expected = original.fetchall(), original.description
        compressed = connection.execute(minify_sql(query), bindings)
        assert (compressed.fetchall(), compressed.description) == expected


def test_schema_mutations_and_upserts() -> None:
    statements = [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT NOT NULL)",
        "INSERT INTO t (id, value) VALUES (1, 'first') ON CONFLICT(id) DO NOTHING",
        "INSERT INTO t (id, value) VALUES (1, 'next') ON CONFLICT(id) DO UPDATE SET value = excluded.value",
        "UPDATE t SET value = 'last' WHERE id = 1 RETURNING value",
        "DELETE FROM t WHERE id = 1 RETURNING id",
    ]
    with (
        closing(sqlite3.connect(":memory:")) as original,
        closing(sqlite3.connect(":memory:")) as compressed,
    ):
        for statement in statements:
            assert (
                compressed.execute(minify_sql(statement)).fetchall()
                == original.execute(statement).fetchall()
            )


def test_triggers_are_minified_and_keep_their_behavior() -> None:
    statements = [
        """CREATE TABLE users (username TEXT PRIMARY KEY, uuid TEXT NOT NULL)""",
        """CREATE TABLE usage (username TEXT, uuid TEXT, kind TEXT)""",
        """CREATE TRIGGER usage_identity AFTER UPDATE OF uuid ON users
           WHEN OLD.uuid != NEW.uuid BEGIN
               DELETE FROM usage WHERE username = NEW.username;
           END""",
        """CREATE TRIGGER usage_insert AFTER INSERT ON users BEGIN
               INSERT INTO usage VALUES (NEW.username, NEW.uuid, 'main');
               INSERT INTO usage VALUES (NEW.username, NEW.uuid, 'wl');
           END""",
        """CREATE TRIGGER usage_case AFTER DELETE ON users BEGIN
               INSERT INTO usage VALUES (
                   OLD.username, OLD.uuid, CASE WHEN OLD.uuid = '' THEN 'empty;x' ELSE 'set' END
               );
           END""",
    ]
    expected = [
        "CREATE TABLE users(username TEXT PRIMARY KEY,uuid TEXT NOT NULL)",
        "CREATE TABLE usage(username TEXT,uuid TEXT,kind TEXT)",
        "CREATE TRIGGER usage_identity AFTER UPDATE OF uuid ON users WHEN OLD.uuid!=NEW.uuid BEGIN DELETE FROM usage WHERE username=NEW.username;END",
        "CREATE TRIGGER usage_insert AFTER INSERT ON users BEGIN INSERT INTO usage VALUES(NEW.username,NEW.uuid,'main');INSERT INTO usage VALUES(NEW.username,NEW.uuid,'wl');END",
        """CREATE TRIGGER usage_case AFTER DELETE ON users BEGIN INSERT INTO usage VALUES(OLD.username,OLD.uuid,CASE WHEN OLD.uuid=''THEN'empty;x'ELSE'set'END);END""",
    ]
    with (
        closing(sqlite3.connect(":memory:")) as original,
        closing(sqlite3.connect(":memory:")) as compressed,
    ):
        for statement, minimal in zip(statements, expected, strict=True):
            assert minify_sql(statement) == minimal
            original.execute(statement)
            compressed.execute(minimal)
        for connection in (original, compressed):
            connection.executemany(
                "INSERT INTO users VALUES (?, ?)",
                [("alice", "a"), ("bob", "b"), ("carol", "")],
            )
            connection.execute("UPDATE users SET uuid = 'z' WHERE username = 'alice'")
            connection.execute("DELETE FROM users WHERE username = 'bob'")
            connection.execute("DELETE FROM users WHERE username = 'carol'")
        queries = "SELECT username, uuid, kind FROM usage ORDER BY username, uuid, kind"
        assert compressed.execute(queries).fetchall() == original.execute(queries).fetchall()


def test_ast_preserves_dynamic_boundaries_and_fstrings() -> None:
    source = """
query = "SELECT a, b FROM t "
query += "WHERE a = ?"
prefix = " SELECT 1 + 2 "
dynamic = f"SELECT 1 + 2 {query}"
raw = b"SELECT 1 + 2"
"""
    tree = ast.parse(source)
    minify_sql_literals(tree)
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["query"] == "SELECT a,b FROM t WHERE a = ?"
    assert namespace["prefix"] == " SELECT 1+2 "
    assert namespace["dynamic"] == "SELECT 1 + 2 SELECT a,b FROM t WHERE a = ?"
    assert namespace["raw"] == b"SELECT 1 + 2"


def test_single_module_and_linked_release_pipelines() -> None:
    source = '__result__ = "SELECT a, b FROM t WHERE id = ?"\n'
    for output in (
        obfuscate(source, "fixture.py"),
        obfuscate_modules({Path("main.py"): source}, []).source,
    ):
        namespace: dict[str, Any] = {}
        exec(compile(output, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
        assert namespace["__result__"] == "SELECT a,b FROM t WHERE id=?"
