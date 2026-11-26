from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, get_type_hints

from obfuscator.imports import optimize_imports
from obfuscator.pipeline import obfuscate_modules

__all__: list[str] = []


def test_group_and_prune_import_table() -> None:
    tree = ast.parse("""
from __future__ import annotations
from math import sqrt, ceil
import json
from os.path import join
import re
from math import floor
from os.path import basename
import decimal
__result__ = sqrt(9), floor(2.5), join('a', basename('b')), json.loads('3'), re.escape('.')
""")
    optimize_imports(tree)
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 4
    assert [alias.name for alias in imports[1].names] == ["sqrt", "floor"]
    assert [alias.name for alias in imports[2].names] == ["json", "re"]
    assert [alias.name for alias in imports[3].names] == ["join", "basename"]
    namespace: dict[str, Any] = {}
    exec(compile(tree, "fixture.py", "exec"), namespace)  # ruff: ignore[exec-builtin]
    assert namespace["__result__"] == (3, 2, "a/b", 3, "\\.")


def test_release_removes_dead_imports_and_retains_runtime_dependencies() -> None:
    result = obfuscate_modules(
        {Path("main.py"): """
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
import decimal
import xml.etree.ElementTree
def unused(): return sqrt(4)
@dataclass
class Record:
    value: 'datetime'
__result__ = xml.etree.ElementTree.fromstring('<x/>').tag
"""},
        [],
        jobs=1,
    )
    assert "decimal" not in result.source
    assert "sqrt" not in result.source
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin]
    assert namespace["__result__"] == "x"
    assert get_type_hints(namespace["Record"], globalns=namespace)["value"].__name__ == "datetime"


def test_reflection_and_nonleading_imports_are_preserved() -> None:
    tree = ast.parse("import json\nglobals()\nif True:\n import re\n")
    optimize_imports(tree)
    assert "import json" in ast.unparse(tree)
    assert "import re" in ast.unparse(tree)
