from pathlib import Path
import pytest
from src.ast_graph_server.config import ServerConfig
from src.ast_graph_server.database import DatabaseManager
from src.ast_graph_server.indexer import Indexer
from src.ast_graph_server.patcher import Patcher


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    pkg = (
        '{\n'
        '  "name": "my-service",\n'
        '  "version": "1.0.0",\n'
        '  "dependencies": {\n'
        '    "express": "^4.18.2",\n'
        '    "lodash": "^4.17.21"\n'
        '  }\n'
        '}\n'
    )
    (ws / "package.json").write_text(pkg, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    patcher = Patcher(config, db, indexer)
    return ws, db, patcher


def test_json_read_block_property(tmp_path: Path):
    ws, db, patcher = make_env(tmp_path)
    res = patcher.read_block("package.json::dependencies")
    assert res["status"] == "success"
    assert res["kind"] == "symbol"
    assert "express" in res["numbered_text"]
    assert "content_hash" in res


def test_json_edit_block_success(tmp_path: Path):
    ws, db, patcher = make_env(tmp_path)
    res = patcher.read_block("package.json::dependencies")
    h = res["content_hash"]

    new_deps = (
        '  "dependencies": {\n'
        '    "express": "^5.0.0",\n'
        '    "lodash": "^4.17.21"\n'
        '  }'
    )
    edit_res = patcher.edit_block(
        "package.json::dependencies",
        ops=[{"replace": [1, res["line_count"]], "content": new_deps}],
        expected_hash=h,
    )
    assert edit_res["status"] == "success"
    assert "^5.0.0" in (ws / "package.json").read_text(encoding="utf-8")


def test_json_edit_block_syntax_barrier_refuses_invalid_json(tmp_path: Path):
    ws, db, patcher = make_env(tmp_path)
    res = patcher.read_block("package.json::dependencies")
    h = res["content_hash"]

    # Trailing comma in JSON is invalid
    invalid_deps = (
        '  "dependencies": {\n'
        '    "express": "^5.0.0",\n'
        '  }'
    )
    edit_res = patcher.edit_block(
        "package.json::dependencies",
        ops=[{"replace": [1, res["line_count"]], "content": invalid_deps}],
        expected_hash=h,
    )
    assert edit_res["status"] == "error"
    assert "syntax error" in edit_res["error"].lower() or "check" in edit_res["error"].lower()