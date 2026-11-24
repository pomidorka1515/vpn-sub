from __future__ import annotations

import ast
import re
from functools import lru_cache, partial
from itertools import pairwise
from typing import TYPE_CHECKING

from sqlglot import exp
from sqlglot.dialects.sqlite import SQLite
from sqlglot.errors import ErrorLevel, ParseError, TokenError
from sqlglot.parsers.sqlite import SQLiteParser

from .parallel import ordered_map

if TYPE_CHECKING:
    from concurrent.futures import ProcessPoolExecutor

__all__: list[str] = ["minify_sql", "minify_sql_literals"]

# Keep the original spelling of every SQLite token, especially quotes, blobs,
# operators and parameter names/numbers. A generator may rewrite these.
_TOKEN: re.Pattern[str] = re.compile(
    r"(?P<space>[ \t\r\n\f]+)|(?P<comment>--[^\n]*|/\*[\s\S]*?\*/)"
    r"|(?P<blob>[xX]'[0-9a-fA-F]*')"
    r"|(?P<quote>'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\])"
    r"|(?P<parameter>\?[0-9]*|[:@$][\w]+)"
    r"|(?P<number>0[xX][0-9a-fA-F](?:_?[0-9a-fA-F])*"
    r"|(?:[0-9](?:_?[0-9])*(?:\.(?:[0-9](?:_?[0-9])*)?)?"
    r"|\.[0-9](?:_?[0-9])*)(?:[eE][+-]?[0-9](?:_?[0-9])*)?)"
    r"|(?P<word>[^\W\d][\w$]*)"
    r"|(?P<operator>->>|->|\|\||<<|>>|<=|>=|==|!=|<>|[(),.;+*/%~&|<>=!-])",
)
_STATEMENTS: frozenset[str] = frozenset(
    {
        "select",
        "union",
        "intersect",
        "except",
        "values",
        "insert",
        "update",
        "delete",
        "create",
        "drop",
        "alter",
        "pragma",
        "transaction",
        "commit",
        "rollback",
        "attach",
        "detach",
        "analyze",
        "vacuum",
    }
)


class StrictParser(SQLiteParser):
    def _warn_unsupported(self) -> None:
        # SQLGlot's fallback accepts unparsed text and logs a warning. Neither
        # is appropriate when deciding whether an arbitrary Python string is SQL.
        raise ParseError("unsupported SQL statement")


def tokens(source: str) -> list[tuple[str, str]] | None:
    result: list[tuple[str, str]] = []
    offset = 0
    while offset < len(source):
        match = _TOKEN.match(source, offset)
        if match is None or match.lastgroup is None:
            return None
        kind = match.lastgroup
        if source.startswith("/*", offset) and kind != "comment":
            return None
        if kind not in {"space", "comment"}:
            result.append((kind, match.group()))
        offset = match.end()
    return result


class TriggerParser:
    """Validate SQLite's trigger wrapper; SQLGlot validates its expressions/body."""

    def __init__(self, parts: list[tuple[str, str]]) -> None:
        self.parts: list[tuple[str, str]] = parts
        self.position: int = 0

    def take(self, *words: str) -> bool:
        end = self.position + len(words)
        if [text.upper() for _, text in self.parts[self.position : end]] != list(words):
            return False
        self.position = end
        return True

    def identifier(self) -> bool:
        if self.position >= len(self.parts) or self.parts[self.position][0] not in {
            "word",
            "quote",
        }:
            return False
        self.position += 1
        return True

    def qualified_name(self) -> bool:
        return self.identifier() and (not self.take(".") or self.identifier())

    def either(self, *words: str) -> bool:
        """Consume the first matching word sequence, if any."""
        return any(self.take(word) for word in words)

    def complete(self) -> bool:
        if not self.take("CREATE"):
            return False
        self.either("TEMP", "TEMPORARY")
        if not self.take("TRIGGER"):
            return False
        if self.take("IF") and not self.take("NOT", "EXISTS"):
            return False
        if not self.qualified_name():
            return False
        if self.take("INSTEAD"):
            if not self.take("OF"):
                return False
        else:
            self.either("BEFORE", "AFTER")
        if self.take("UPDATE"):
            if self.take("OF"):
                if not self.identifier():
                    return False
                while self.take(","):
                    if not self.identifier():
                        return False
        elif not (self.take("INSERT") or self.take("DELETE")):
            return False
        if not self.take("ON") or not self.qualified_name():
            return False
        if self.take("FOR") and not self.take("EACH", "ROW"):
            return False
        if self.take("WHEN"):
            start = self.position
            while (
                self.position < len(self.parts) and self.parts[self.position][1].upper() != "BEGIN"
            ):
                self.position += 1
            expression = " ".join(text for _, text in self.parts[start : self.position])
            if not expression or not complete_statements(f"SELECT ({expression})"):
                return False
        if not self.take("BEGIN"):
            return False
        # The outer END follows a mandatory body semicolon. CASE ... END and
        # quoted strings containing semicolons remain inside each body query.
        end = len(self.parts)
        if self.parts[-1][1] == ";":
            end -= 1
        if end <= self.position or self.parts[end - 1][1].upper() != "END":
            return False
        body = self.parts[self.position : end - 1]
        if not body or body[-1][1] != ";":
            return False
        start = 0
        for index, (_, text) in enumerate(body):
            if text != ";":
                continue
            query = body[start:index]
            if not query or query[0][1].upper() not in {"INSERT", "UPDATE", "DELETE", "SELECT"}:
                return False
            if not complete_statements(" ".join(text for _, text in query)):
                return False
            start = index + 1
        return start == len(body)


def complete_statements(source: str) -> bool:
    parts = tokens(source)
    if parts:
        prefix = [text.upper() for _, text in parts[:3]]
        if prefix[:2] == ["CREATE", "TRIGGER"] or prefix in (
            ["CREATE", "TEMP", "TRIGGER"],
            ["CREATE", "TEMPORARY", "TRIGGER"],
        ):
            return TriggerParser(parts).complete()
    try:
        statements = StrictParser(error_level=ErrorLevel.RAISE).parse(
            SQLite.Tokenizer().tokenize(source),
            source,
        )
    except ParseError, TokenError, RecursionError:
        return False
    if not any(statement is not None for statement in statements):
        return False
    for statement in statements:
        if statement is None:
            continue
        if statement.key not in _STATEMENTS:
            return False
        for node in statement.walk():
            if isinstance(node, exp.Command) or node.error_messages():
                return False
            # SQLGlot deliberately permits partially built statements; these
            # optional AST fields are mandatory in complete SQLite statements.
            if isinstance(node, (exp.Select, exp.Update)) and not node.expressions:
                return False
            if isinstance(node, (exp.Update, exp.Insert, exp.Delete)) and not node.this:
                return False
            if isinstance(node, exp.Insert) and not (node.expression or node.args.get("default")):
                return False
            if isinstance(node, exp.Create):
                kind = node.args.get("kind")
                if kind == "TABLE" and not (isinstance(node.this, exp.Schema) or node.expression):
                    return False
                if kind == "VIEW" and not node.expression:
                    return False
    return True


@lru_cache(maxsize=4096)
def minify_sql(source: str, *, preserve_edges: bool = False) -> str:
    """Remove only optional SQLite whitespace/comments from complete statements."""
    parts = tokens(source)
    if not parts:
        return source
    # Normalize bind syntax only for the parser, never in the emitted query.
    parsed = _TOKEN.sub(
        lambda match: "?" if match.lastgroup == "parameter" else match.group(),
        source,
    )
    if not complete_statements(parsed):
        return source
    output = [parts[0][1]]
    for previous, current in pairwise(parts):
        left, right = previous[1], current[1]
        # SQLite rejects numeric tokens immediately followed by identifiers,
        # even though many generic SQL tokenizers accept e.g. `1FROM`.
        word_boundary = (left[-1].isalnum() or left[-1] in "_$") and (
            right[0].isalnum() or right[0] in "_$"
        )
        separator = (
            " "
            if (
                word_boundary or left + right == "/*" or tokens(left + right) != [previous, current]
            )
            else ""
        )
        output.extend((separator, right))
    result = "".join(output)
    if preserve_edges:
        result = (" " if source[0].isspace() else "") + result
        result += " " if source[-1].isspace() else ""
    return result if len(result) < len(source) else source


class SQLMinifier(ast.NodeTransformer):
    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.JoinedStr:
        # An f-string's literal pieces are not independent SQL statements.
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, str):
            # Literals can be concatenated indirectly (query += fragment).
            # Retain one original boundary separator to keep that safe.
            node.value = minify_sql(node.value, preserve_edges=True)
        return node


def minify_sql_literals(tree: ast.Module, *, executor: ProcessPoolExecutor | None = None) -> None:
    """Minify complete SQL literals without executing application queries."""
    if executor is None:
        SQLMinifier().visit(tree)
        return
    # Only strings cross process boundaries; keep the linked AST and its shared
    # node identities in the parent. Do not descend into f-string fragments.
    literals: list[tuple[ast.Constant, str]] = []
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.JoinedStr):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append((node, node.value))
        pending.extend(ast.iter_child_nodes(node))
    values = list(dict.fromkeys(value for _, value in literals))
    replacements = dict(
        zip(
            values,
            ordered_map(executor, partial(minify_sql, preserve_edges=True), values, chunksize=32),
            strict=True,
        )
    )
    for node, value in literals:
        node.value = replacements[value]
