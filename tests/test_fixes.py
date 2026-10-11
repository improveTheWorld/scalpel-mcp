"""Regression tests for the audit fixes (staging, imports, insert position, safety)."""

import ast
from pathlib import Path
from typing import Dict, Tuple

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indentation import IndentationManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.parsers.typescript_parser import TypeScriptParser
from ast_graph_server.patcher import Patcher


def make_env(tmp_path: Path, files: Dict[str, str]) -> Tuple[Path, ServerConfig, DatabaseManager, Indexer, Patcher]:
    ws = tmp_path / "ws"
    ws.mkdir()
    for rel, content in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, config, db, indexer, Patcher(config, db, indexer)


# ---------------------------------------------------------------------------
# insert_symbol position
# ---------------------------------------------------------------------------

def test_insert_symbol_position_start_in_class(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(
        tmp_path, {"c.py": "class A:\n    def first(self):\n        return 1\n"}
    )
    result = patcher.insert_symbol("c.py::A", "def inserted(self):\n    return 2", position="start")
    assert result["status"] == "success"
    text = (ws / "c.py").read_text(encoding="utf-8")
    assert text.index("def inserted") < text.index("def first")
    ast.parse(text)


def test_insert_symbol_position_start_decorated_first(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(
        tmp_path,
        {"c.py": "class A:\n    @property\n    def first(self):\n        return 1\n"},
    )
    result = patcher.insert_symbol("c.py::A", "def inserted(self):\n    return 2", position="start")
    assert result["status"] == "success"
    text = (ws / "c.py").read_text(encoding="utf-8")
    assert text.index("def inserted") < text.index("@property")
    ast.parse(text)


def test_insert_symbol_position_end_default(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(
        tmp_path, {"c.py": "class A:\n    def first(self):\n        return 1\n"}
    )
    patcher.insert_symbol("c.py::A", "def appended(self):\n    return 2")
    text = (ws / "c.py").read_text(encoding="utf-8")
    assert text.index("def first") < text.index("def appended")
    ast.parse(text)


# ---------------------------------------------------------------------------
# Workspace boundary
# ---------------------------------------------------------------------------

def test_insert_symbol_blocks_path_traversal(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path, {"m.py": "x = 1\n"})
    outside = tmp_path / "OUTSIDE.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    result = patcher.insert_symbol("../OUTSIDE.py", "def evil():\n    pass")

    assert result["status"] == "error"
    assert outside.read_text(encoding="utf-8") == "x = 1\n"


# ---------------------------------------------------------------------------
# Polyglot parsing
# ---------------------------------------------------------------------------

def test_tsx_validate_syntax_accepts_jsx() -> None:
    parser = TypeScriptParser()
    tsx = b"const C = () => {\n  const x = 1;\n  return <div>{x}</div>;\n};\n"
    ok, errors = parser.validate_syntax(tsx, "src/c.tsx")
    assert ok, [str(e) for e in errors]


def test_tsx_validate_syntax_still_rejects_broken_code() -> None:
    parser = TypeScriptParser()
    ok, _errors = parser.validate_syntax(b"const x = ;\n", "src/c.tsx")
    assert not ok


def test_indentation_protects_ts_template_literal() -> None:
    manager = IndentationManager()
    code = "function f() {\n  const t = \x60line1\nline2\x60;\n  return t;\n}"
    rebased = manager.rebase_indentation(code, target_col=2, file_path="a.ts")
    lines = rebased.splitlines()
    assert lines[2] == "line2\x60;"
