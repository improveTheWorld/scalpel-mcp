from pathlib import Path
import pytest
from src.ast_graph_server.config import ServerConfig
from src.ast_graph_server.database import DatabaseManager
from src.ast_graph_server.indexer import Indexer
from src.ast_graph_server.patcher import Patcher


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    compose = (
        "version: '3.8'\n"
        "services:\n"
        "  web:\n"
        "    image: nginx:1.24\n"
        "    ports:\n"
        "      - 80:80\n"
    )
    (ws / "docker-compose.yml").write_text(compose, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    patcher = Patcher(config, db, indexer)
    return ws, db, patcher


def test_yaml_read_block_mapping(tmp_path: Path):
    ws, db, patcher = make_env(tmp_path)
    res = patcher.read_block("docker-compose.yml::services.web")
    assert res["status"] == "success"
    assert res["kind"] == "symbol"
    assert "nginx:1.24" in res["numbered_text"]
    assert "content_hash" in res


def test_yaml_edit_block_success(tmp_path: Path):
    ws, db, patcher = make_env(tmp_path)
    res = patcher.read_block("docker-compose.yml::services.web.image")
    h = res["content_hash"]

    new_image = "image: nginx:1.25-alpine"
    edit_res = patcher.edit_block(
        "docker-compose.yml::services.web.image",
        ops=[{"replace": [1, 1], "content": new_image}],
        expected_hash=h,
    )
    assert edit_res["status"] == "success"
    content = (ws / "docker-compose.yml").read_text(encoding="utf-8")
    assert "nginx:1.25-alpine" in content


def test_yaml_edit_block_syntax_barrier_refuses_invalid_yaml(tmp_path: Path):
    ws, db, patcher = make_env(tmp_path)
    res = patcher.read_block("docker-compose.yml::services.web.image")
    h = res["content_hash"]

    # Invalid indentation or unclosed quote
    invalid_content = 'image: "nginx:1.25-alpine'
    edit_res = patcher.edit_block(
        "docker-compose.yml::services.web.image",
        ops=[{"replace": [1, 1], "content": invalid_content}],
        expected_hash=h,
    )
    assert edit_res["status"] == "error"
    assert "syntax" in edit_res["error"].lower() or "check" in edit_res["error"].lower()