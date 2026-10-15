"""Unit tests for v0.4.4 forensic fixes (#43 to #50)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher
from ast_graph_server.server import ASTGraphServer


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    indexer = Indexer(config, db)
    patcher = Patcher(config, db, indexer)
    return ws, config, db, indexer, patcher


def test_43a_insert_after_splitting_list_item_from_sub_bullet_is_refused(tmp_path: Path) -> None:
    """#43(a): insert_after on an ordered list item title that has an indented sub-bullet on the next line is refused."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "plan.md").write_text(
        "# Plan\n\n## Criteria\n\n"
        "3. **Atomic Merge:**\n"
        "4. **Distributed Worker Execution:**\n"
        "   - Confirm .NET worker processes start.\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    sec = patcher.read_block("docs/plan.md::Plan::Criteria", answer_format="json")

    res = patcher.edit_block(
        "docs/plan.md::Plan::Criteria",
        ops=[{"insert_after": 4, "content": "5. **Databricks Authentication:**\n   - Configure AuthType."}],
        expected_hash=sec["content_hash"],
    )
    assert res["status"] == "error"
    assert "splits that item from its sub-bullet" in res["error"]
    assert "insert_after: 5" in res["actionable_hint"]


def test_43b_replace_multi_item_dropping_trailing_sub_bullet_is_refused(tmp_path: Path) -> None:
    """#43(b): replace [a,b] that re-emits bullets for item 3 but ends on bare title for item 4 dropping its sub-bullet is refused."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "arch.md").write_text(
        "# Arch\n\n## PWAF\n\n"
        "3. **Unity Catalog 3-Tier Resolution**:\n"
        "   - `SparkWriteExtensions.FormatMergeTarget(target)` distinguishes paths.\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    sec = patcher.read_block("docs/arch.md::Arch::PWAF", answer_format="json")

    res = patcher.edit_block(
        "docs/arch.md::Arch::PWAF",
        ops=[
            {
                "replace": [3, 4],
                "content": (
                    "3. **PWAF OAuth Authentication**:\n"
                    "   - Supports OAuth M2M and U2M.\n"
                    "4. **Unity Catalog 3-Tier Resolution**:"
                ),
            }
        ],
        expected_hash=sec["content_hash"],
    )
    assert res["status"] == "error"
    assert res.get("dropped_bullet_line") == 4
    assert "Shrink the replace range" in res["actionable_hint"]


def test_44a_insert_after_before_closing_brace_with_enum_is_refused(tmp_path: Path) -> None:
    """#44(a): insert_after on line N-1 of a C# class with a top-level enum declaration is refused."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Options.cs").write_text(
        "public class SparkConnectOptions\n{\n    public bool AutoStartBackend { get; set; } = true;\n}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    blk = patcher.read_block("src/Options.cs::SparkConnectOptions", answer_format="json")
    assert blk["line_count"] == 4

    res = patcher.edit_block(
        "src/Options.cs::SparkConnectOptions",
        ops=[{"insert_after": 3, "content": "\npublic enum SparkAuthMode\n{\n    None,\n    OAuth\n}"}],
        expected_hash=blk["content_hash"],
    )
    assert res["status"] == "error"
    assert "NEST 'SparkAuthMode' inside" in res["error"]
    assert "insert_after: 4" in res["actionable_hint"]


def test_44bc_insert_block_enum_in_class_refused_and_namespace_anchor_accepted(tmp_path: Path) -> None:
    """#44(b,c): insert_block(parent=Class, text='public enum ...') is refused, while parent=Namespace with anchor=Class succeeds."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Options.cs").write_text(
        "namespace DataLinq.Spark\n{\n    public class SparkConnectOptions\n    {\n        public int Port { get; set; }\n    }\n}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()

    refused = patcher.insert_symbol(
        "src/Options.cs::DataLinq.Spark::SparkConnectOptions",
        "public enum DatabricksOAuthMode\n{\n    None,\n    M2M\n}",
    )
    assert refused["status"] == "error"
    assert "NEST the enum inside class" in refused["error"]

    accepted = patcher.insert_symbol(
        "src/Options.cs::DataLinq.Spark",
        "public enum DatabricksOAuthMode\n{\n    None,\n    M2M\n}",
        anchor="src/Options.cs::DataLinq.Spark::SparkConnectOptions",
    )
    assert accepted["status"] == "success", accepted
    assert "DatabricksOAuthMode" in (ws / "src" / "Options.cs").read_text(encoding="utf-8")


def test_45_markdown_duplicate_toc_item_with_different_ordinal_is_refused(tmp_path: Path) -> None:
    """#45: Replacing line 29 with items 8..12 while leaving old items 9..11 ('[Best Practices](#best-practices)') is refused."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "guide.md").write_text(
        "# Guide\n\n## Table of Contents\n\n"
        "8. [Deploying to Databricks](#deploying-to-databricks)\n"
        "9. [Best Practices](#best-practices)\n"
        "10. [Licensing](#licensing)\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    sec = patcher.read_block("docs/guide.md::Guide::Table of Contents", answer_format="json")

    res = patcher.edit_block(
        "docs/guide.md::Guide::Table of Contents",
        ops=[
            {
                "replace": [3, 3],
                "content": (
                    "8. [Deploying to Databricks](#deploying-to-databricks)\n"
                    "9. [Databricks Partner Compliance](#pwaf)\n"
                    "10. [Best Practices](#best-practices)\n"
                    "11. [Licensing](#licensing)"
                ),
            }
        ],
        expected_hash=sec["content_hash"],
    )
    assert res["status"] == "error"
    assert "Best Practices" in res["error"]


@pytest.mark.anyio
async def test_46_and_49_file_path_with_symbol_hash_routes_and_no_pathless_warning_in_text(tmp_path: Path) -> None:
    """#46 & #49: Passing a whole code file path with a unique symbol expected_hash routes to the symbol, and text mode emits no 'carried no path' warning."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Options.cs").write_text(
        "public class SparkConnectOptions\n{\n    public int Port { get; set; } = 15002;\n}\n",
        encoding="utf-8",
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    blk = server.patcher.read_block("src/Options.cs::SparkConnectOptions", answer_format="json")
    sym_hash = blk["content_hash"]

    # #46: Pass file path 'src/Options.cs' instead of 'src/Options.cs::SparkConnectOptions'
    res = await server.mcp.call_tool(
        "edit_block",
        {
            "edits": [
                {
                    "path": "src/Options.cs",
                    "expected_hash": sym_hash,
                    "ops": [{"insert_after": 2, "content": "    public string? Host { get; set; }"}],
                }
            ],
            "format": "text",
        },
    )
    text_out = res.content[0].text
    assert "[ok] applied 1/1 edits" in text_out, text_out
    assert "Host" in (ws / "src" / "Options.cs").read_text(encoding="utf-8")

    # #49: Pathless entry in text mode does not emit 'carried no path' warning
    blk2 = server.patcher.read_block("src/Options.cs::SparkConnectOptions", answer_format="json")
    res2 = await server.mcp.call_tool(
        "edit_block",
        {
            "edits": [
                {
                    "expected_hash": blk2["content_hash"],
                    "ops": [{"insert_after": 3, "content": "    public string? Token { get; set; }"}],
                }
            ],
            "format": "text",
        },
    )
    text_out2 = res2.content[0].text
    assert "[ok] applied 1/1 edits" in text_out2, text_out2
    assert "carried no path" not in text_out2


def test_47_insert_block_document_parent_with_h2_anchor_under_single_h1_routes_automatically(tmp_path: Path) -> None:
    """#47: insert_block(parent='doc.md', anchor='doc.md::H1::SectionA') routes to H1 parent when doc has a single H1."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "guide.md").write_text(
        "# LINQ-to-Spark\n\n## Deploying to Databricks\n\nBody A.\n\n## Best Practices\n\nBody B.\n",
        encoding="utf-8",
    )
    indexer.index_workspace()

    res = patcher.insert_block(
        parent="docs/guide.md",
        anchor="docs/guide.md::LINQ-to-Spark::Deploying to Databricks",
        text="## Databricks Partner Compliance\n\nPWAF body.",
    )
    assert res["status"] == "success", res
    assert res["level"] == 2
    assert "Databricks Partner Compliance" in (ws / "docs" / "guide.md").read_text(encoding="utf-8")


def test_48_written_outside_block_lists_all_created_sibling_symbols(tmp_path: Path) -> None:
    """#48: Inserting multiple sibling methods after the last line of a method lists all created symbols in the header."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "PwafTests.cs").write_text(
        "public class PwafTests\n{\n    public void TestOne()\n    {\n    }\n}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    m1 = patcher.read_block("tests/PwafTests.cs::PwafTests::TestOne", answer_format="json")

    res = patcher.edit_block(
        "tests/PwafTests.cs::PwafTests::TestOne",
        ops=[
            {
                "insert_after": m1["line_count"],
                "content": (
                    "\n    public void TestTwo()\n    {\n    }\n"
                    "\n    public void TestThree()\n    {\n    }\n"
                ),
            }
        ],
        expected_hash=m1["content_hash"],
    )
    from ast_graph_server.lineops import format_single_edit_result

    formatted = format_single_edit_result(res)
    assert "created 2 siblings" in formatted, formatted
    assert "TestTwo" in formatted and "TestThree" in formatted, formatted


def test_50_search_symbols_path_filter_note_explains_declarations_vs_usages(tmp_path: Path) -> None:
    """#50: Empty symbol search under a path filter states clearly that no symbol is declared in the matched files."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Options.cs").write_text("public class SparkConnectOptions {}\n", encoding="utf-8")
    (ws / "src" / "Context.cs").write_text("public class SparkContext {}\n", encoding="utf-8")
    indexer.index_workspace()

    from ast_graph_server.server import empty_symbol_search_note

    note = empty_symbol_search_note(db, "SparkConnectOptions", "all", "src/Context.cs")
    assert "1 indexed file(s) match the path filter, and none declares a symbol matching the query" in note
    assert "filter matches symbol declarations, not usages" in note