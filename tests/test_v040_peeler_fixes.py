from pathlib import Path
import json
import pytest
from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher
from ast_graph_server.server import ASTGraphServer


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    patcher = Patcher(config, db, indexer)
    return ws, config, db, indexer, patcher


def test_insert_after_zero_non_heading_on_markdown_section_is_refused(tmp_path: Path) -> None:
    ws, _cfg, _db, indexer, patcher = make_env(tmp_path)
    doc = ws / "CHANGELOG.md"
    doc.write_text("# Changelog\n\n## 1.0\n\n### New Features\n\n- Existing item\n", encoding="utf-8")
    indexer.index_workspace()

    read = patcher.read_block("CHANGELOG.md::Changelog::1.0::New Features")
    assert read["status"] == "success"

    res = patcher.edit_block(
        "CHANGELOG.md::Changelog::1.0::New Features",
        ops=[{"insert_after": 0, "content": "- Added OAuth support"}],
        expected_hash=read["content_hash"],
    )
    assert res["status"] == "error"
    assert "insert_after: 0" in res["error"]
    assert "insert_after: 1" in res["actionable_hint"]
    # File must be untouched
    assert "New Features (2)" not in doc.read_text(encoding="utf-8")


def test_insert_after_zero_with_heading_on_markdown_section_still_allowed(tmp_path: Path) -> None:
    ws, _cfg, _db, indexer, patcher = make_env(tmp_path)
    doc = ws / "CHANGELOG.md"
    doc.write_text("# Changelog\n\n## 1.0\n\n### New Features\n\n- Existing item\n", encoding="utf-8")
    indexer.index_workspace()

    read = patcher.read_block("CHANGELOG.md::Changelog::1.0::New Features")
    res = patcher.edit_block(
        "CHANGELOG.md::Changelog::1.0::New Features",
        ops=[{"insert_after": 0, "content": "### Highlights\n\n- Top highlight\n"}],
        expected_hash=read["content_hash"],
    )
    assert res["status"] == "success"
    assert "### Highlights" in doc.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_read_block_on_parent_markdown_section_exposes_child_sections(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    doc = ws / "guide.md"
    doc.write_text(
        "# Guide\n\n## Deploying\n\nIntro line.\n\n### Step 1\n\nBody 1.\n\n### Step 2\n\nBody 2.\n",
        encoding="utf-8",
    )
    cfg = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(cfg)
    server.initialize(enable_watcher=False)

    res_text = (await server.mcp.call_tool("read_block", {"path": "guide.md::Guide::Deploying"})).content[0].text
    assert "children: 2 sub-section(s) under this heading (Step 1, Step 2)" in res_text

    res_json = json.loads(
        (await server.mcp.call_tool("read_block", {"path": "guide.md::Guide::Deploying", "format": "json"})).content[0].text
    )
    assert res_json["child_sections"] == ["Step 1", "Step 2"]


def test_markdown_section_replace_does_not_emit_false_blank_boundary_lost_warning(tmp_path: Path) -> None:
    ws, _cfg, _db, indexer, patcher = make_env(tmp_path)
    doc = ws / "plan.md"
    doc.write_text("# Plan\n\n## Criteria\n\n1. First\n2. Second\n\n## Next\n\nText.\n", encoding="utf-8")
    indexer.index_workspace()

    read = patcher.read_block("plan.md::Plan::Criteria")
    res = patcher.edit_block(
        "plan.md::Plan::Criteria",
        ops=[{"replace": [1, read["line_count"]], "content": "## Criteria\n\n1. First\n2. Second\n3. Third"}],
        expected_hash=read["content_hash"],
    )
    assert res["status"] == "success"
    warnings = res.get("warnings") or []
    assert not any("blank line that separated what follows is GONE" in w for w in warnings), warnings
    assert "\n\n## Next" in doc.read_text(encoding="utf-8")


def test_batch_out_of_bounds_distinguished_from_unreadable_operation(tmp_path: Path) -> None:
    ws, _cfg, _db, indexer, patcher = make_env(tmp_path)
    doc = ws / "doc.md"
    doc.write_text("# Title\n\n## Sec\n\nLine A\nLine B\n", encoding="utf-8")
    indexer.index_workspace()

    read = patcher.read_block("doc.md::Title::Sec")
    res = patcher.edit_block(
        edits=[
            {
                "path": "doc.md::Title::Sec",
                "expected_hash": read["content_hash"],
                "ops": [{"replace": [12, 18], "content": "Out of bounds"}],
            }
        ]
    )
    entry_res = res["results"][0]
    assert entry_res["status"] == "error"
    assert entry_res.get("out_of_bounds") is True
    assert "operation itself is unreadable" not in entry_res["error"]