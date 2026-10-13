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

def test_yaml_sequence_and_json_object_array_outline(tmp_path: Path):
    ws, db, patcher = make_env(tmp_path)
    patch_yml = (
        "- target: dsh-tool-scalpel\n"
        "  insert:\n"
        "    - id: item-1\n"
        "- target: other-pkg\n"
    )
    (ws / "cordis.patch.yml").write_text(patch_yml, encoding="utf-8")
    (ws / "queries.json").write_text('[{"query": "q1"}, {"query": "q2"}]\n', encoding="utf-8")
    Indexer(patcher.config, db).index_workspace()

    res_json = patcher.read_block("cordis.patch.yml", answer_format="json")
    assert res_json["status"] == "success"
    assert res_json["top_level_keys"] == ["[0]", "[1]"]
    assert "children" in res_json and len(res_json["children"]) == 2
    assert res_json["children"][0]["name"] == "[0]"
    assert "content_hash" in res_json

    res_text = patcher.read_block("cordis.patch.yml", answer_format="text")
    assert res_text["status"] == "success"
    assert "[0]" in res_text["text"]
    assert "target" in res_text["text"]

    arr_json = patcher.read_block("queries.json", answer_format="json")
    assert arr_json["status"] == "success"
    assert arr_json["top_level_keys"] == ["[0]", "[1]"]
    assert "children" in arr_json and len(arr_json["children"]) == 2


def test_yaml_and_json_unknown_key_suggests_near_candidates(tmp_path: Path):
    ws, db, patcher = make_env(tmp_path)
    (ws / "app.json").write_text('{"services": {"web": {"port": 8080}}}\n', encoding="utf-8")
    Indexer(patcher.config, db).index_workspace()

    bad_yml = patcher.read_block("docker-compose.yml::services.unknown")
    assert bad_yml["status"] == "error"
    assert "No :: address exists in this file" not in bad_yml.get("actionable_hint", "")
    assert bad_yml.get("candidates")
    assert any(c["path"].startswith("docker-compose.yml::") for c in bad_yml["candidates"])

    bad_json = patcher.read_block("app.json::services.unknown")
    assert bad_json["status"] == "error"
    assert "No :: address exists in this file" not in bad_json.get("actionable_hint", "")
    assert bad_json.get("candidates")
    assert any(c["path"].startswith("app.json::") for c in bad_json["candidates"])