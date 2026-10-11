"""Unit and integration tests for Version 0.2.0 features."""

import json
from pathlib import Path
import time
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.parsers.python_parser import PythonParser
from ast_graph_server.patcher import Patcher
from ast_graph_server.server import ASTGraphServer
from ast_graph_server.watcher import SuppressionRegistry, WorkspaceEventHandler


@pytest.fixture
def v020_env(tmp_path: Path) -> tuple[Patcher, DatabaseManager, Indexer, ServerConfig, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    src = ws / "src"
    src.mkdir()

    app_file = src / "app.py"
    app_file.write_text(
        '''"""App module."""

class Controller:
    """Main controller."""

    def handle(self):
        """Handle request."""
        return 200

    def obsolete(self):
        """Dead code to delete."""
        return 500

    def cleanup(self):
        """Cleanup handler."""
        return True
''',
        encoding="utf-8",
    )

    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    suppression = SuppressionRegistry()
    patcher = Patcher(config, db, indexer, suppression_registry=suppression)
    return patcher, db, indexer, config, ws


def test_delete_symbol(v020_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig, Path]) -> None:
    patcher, db, indexer, config, ws = v020_env
    sym_id = "src/app.py::Controller.obsolete"

    assert db.get_symbol(sym_id) is not None

    res = patcher.delete_symbol(sym_id)
    assert res["status"] == "success"
    assert res["validation"]["syntax_ok"] is True

    # Check file content on disk: obsolete method should be gone
    file_text = (ws / "src/app.py").read_text(encoding="utf-8")
    assert "def obsolete(self):" not in file_text
    assert "def handle(self):" in file_text
    assert "def cleanup(self):" in file_text

    # Symbol should be deleted from SQLite
    assert db.get_symbol(sym_id) is None


def test_actionable_syntax_diagnostics() -> None:
    parser = PythonParser()

    # Broken code: missing closing parenthesis in def
    bad_code = b"def calculate(x, y:\n    return x + y\n"
    syntax_ok, details = parser.validate_syntax(bad_code)
    assert not syntax_ok
    assert len(details) > 0

    first_err = details[0]
    assert first_err.line >= 1
    assert first_err.actionable_hint != ""
    assert "parenthesis" in first_err.actionable_hint.lower() or "syntax" in first_err.actionable_hint.lower()
    assert hasattr(first_err, "to_dict")
    d = first_err.to_dict()
    assert "actionable_hint" in d


def test_suppression_registry(tmp_path: Path) -> None:
    reg = SuppressionRegistry(ttl_seconds=0.3)
    target = tmp_path / "test.py"

    assert not reg.is_suppressed(target)
    reg.suppress(target)
    assert reg.is_suppressed(target)

    time.sleep(0.35)
    assert not reg.is_suppressed(target)


@pytest.mark.anyio
async def test_server_v020_tools(v020_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig, Path]) -> None:
    patcher, db, indexer, config, ws = v020_env
    server = ASTGraphServer(config)

    # Read then edit a block through the MCP tools (no staging: it left the surface)
    read_res = await server.mcp.call_tool("read_block", {"path": "src/app.py::Controller.handle", "format": "json"})
    block = json.loads(read_res.content[0].text)
    edit_res = await server.mcp.call_tool(
        "edit_block",
        {
            "path": "src/app.py::Controller.handle",
            "expected_hash": block["content_hash"],
            "ops": [{"replace": [1, block["line_count"]], "content": "def handle(self):\n    return 204"}],
            "format": "json",
        },
    )
    assert json.loads(edit_res.content[0].text)["status"] == "success"

    # Delete symbol tool
    del_res = await server.mcp.call_tool(
        "delete_block",
        {"path": "src/app.py::Controller.obsolete"},
    )
    del_data = json.loads(del_res.content[0].text)
    assert del_data["status"] == "success"
