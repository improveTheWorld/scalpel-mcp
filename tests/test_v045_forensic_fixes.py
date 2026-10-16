"""Unit tests for v0.4.5 forensic fixes (#51 to #57)."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher
from ast_graph_server.server import ASTGraphServer, empty_document_search_note, empty_symbol_search_note


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    indexer = Indexer(config, db)
    patcher = Patcher(config, db, indexer)
    return ws, config, db, indexer, patcher


def test_51_file_line_vs_block_line_coordinate_mismatch_reported_with_numbered_text(tmp_path: Path) -> None:
    """#51: When replace [n, n] fails syntax validation because `n` is the file line of a declaration at block line `n - start_line + 1`, the refusal names both coordinates and returns numbered_text."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    # 9 preamble lines so SparkConnectOptions starts on file line 10
    lines = ["using System;\n"] + [f"// header {i}\n" for i in range(1, 8)] + ["namespace DataLinq.Spark;\n"]
    class_lines = []
    class_lines.append("public class SparkConnectOptions\n")  # block line 1 = file line 10
    class_lines.append("{\n")  # block line 2 = file line 11
    for i in range(3, 65):
        class_lines.append(f"    public int Prop{i} {{ get; set; }}\n")
    # block line 65 = file line 74:
    class_lines.append("    internal SparkSessionBuilder ApplyAuthentication(SparkSessionBuilder builder, string token)\n")
    class_lines.append("    {\n")  # 66
    class_lines.append("        switch (token)\n")  # 67
    class_lines.append("        {\n")  # 68
    class_lines.append("            case \"a\":\n")  # 69
    class_lines.append("                if (!string.IsNullOrWhiteSpace(token))\n")  # 70
    class_lines.append("                    return builder;\n")  # 71
    class_lines.append("                if (token.Length > 0)\n")  # 72
    class_lines.append("                if (!string.IsNullOrEmpty(token))\n")  # 73
    class_lines.append("                    builder = builder;\n")  # block line 74 = body of braceless if!
    class_lines.append("                break;\n")  # 75
    class_lines.append("        }\n")  # 76
    class_lines.append("        return builder;\n")  # 77
    class_lines.append("    }\n")  # 78
    class_lines.append("}\n")  # 79

    (ws / "src" / "SparkConnectOptions.cs").write_text("".join(lines + class_lines), encoding="utf-8")
    indexer.index_workspace()

    blk = patcher.read_block("src/SparkConnectOptions.cs::SparkConnectOptions", answer_format="json")
    assert blk["line_count"] == 79

    # Agent mistakenly uses file line 74 instead of block line 65 to replace ApplyAuthentication signature
    res = patcher.edit_block(
        "src/SparkConnectOptions.cs::SparkConnectOptions",
        ops=[
            {
                "replace": [74, 74],
                "content": "    internal Microsoft.Spark.Sql.Builder ApplyAuthentication(Microsoft.Spark.Sql.Builder builder, string token)",
            }
        ],
        expected_hash=blk["content_hash"],
    )
    assert res["status"] == "error"
    assert "Coordinate mismatch" in res["error"]
    assert "file line 74 is block line 65" in res["error"]
    assert "replace: [65, 65]" in res["actionable_hint"]
    assert res.get("content_hash") == blk["content_hash"]
    assert res.get("numbered_text")


def test_52_markdown_ordered_list_out_of_order_inversion_is_refused(tmp_path: Path) -> None:
    """#52: Inserting item '4.' before item '3.' (producing 1, 2, 4, 3) is refused with exact insert_after line."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "architecture.md").write_text(
        "# Architecture\n\n## Compliance\n\n"
        "1. **Local vs. Databricks Runtime**:\n"
        "   - Local JVM.\n"
        "2. **PWAF Telemetry**:\n"
        "   - Injects userAgent.\n"
        "3. **Unity Catalog 3-Tier Resolution**:\n"
        "   - FormatMergeTarget resolves catalog.schema.table.\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    sec = patcher.read_block("docs/architecture.md::Architecture::Compliance", answer_format="json")

    # Line 6 is '- Injects userAgent.' (end of item 2), line 7 is '3. **Unity Catalog...**', line 8 is its sub-bullet
    res = patcher.edit_block(
        "docs/architecture.md::Architecture::Compliance",
        ops=[
            {
                "insert_after": 6,
                "content": "4. **Databricks Authentication**:\n   - OAuth M2M and PAT.",
            }
        ],
        expected_hash=sec["content_hash"],
    )
    assert res["status"] == "error"
    assert "out-of-order sequence (4. before 3.)" in res["error"]
    assert "insert_after: 8" in res["actionable_hint"]


def test_53_delete_and_insert_after_cancelling_to_noop_explains_bottom_up(tmp_path: Path) -> None:
    """#53: delete [a, b] + insert_after: b with identical content warns that bottom-up ops cancelled each other out."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "architecture.md").write_text(
        "# Architecture\n\n## Compliance\n\n"
        "4. **Databricks Auth**:\n"
        "   - OAuth M2M.\n"
        "3. **Unity Catalog**:\n"
        "   - Merge target.\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    sec = patcher.read_block("docs/architecture.md::Architecture::Compliance", answer_format="json")

    res = patcher.edit_block(
        "docs/architecture.md::Architecture::Compliance",
        ops=[
            {"delete": [3, 4]},
            {"insert_after": 4, "content": "4. **Databricks Auth**:\n   - OAuth M2M."},
        ],
        expected_hash=sec["content_hash"],
    )
    assert res["status"] == "noop"
    assert "cancelled each other out" in res["message"]
    assert any("cancelled each other out" in w for w in res.get("warnings") or [])


def test_54_search_documents_path_filter_wording_and_displayed_section_ids(tmp_path: Path) -> None:
    """#54: empty_document_search_note says 'none contains a paragraph' (not 'declares a symbol') and formats section_ids with '::'."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "other").mkdir(parents=True)
    (ws / "docs" / "guide.md").write_text(
        "# Main Guide\n\n## OAuth Setup\n\nConfigure OAuthClientId and OAuthClientSecret.\n",
        encoding="utf-8",
    )
    (ws / "other" / "notes.md").write_text(
        "# Notes\n\n## Misc\n\nNothing relevant here.\n",
        encoding="utf-8",
    )
    indexer.index_workspace()

    doc_note = empty_document_search_note(db, "OAuthClientId", "other/*.md", patcher=patcher)
    assert "none contains a paragraph matching the query" in doc_note
    assert "declares a symbol" not in doc_note
    assert "docs/guide.md::Main Guide::OAuth Setup" in doc_note
    assert "#oauth-setup" not in doc_note

    sym_note = empty_symbol_search_note(db, "OAuthClientId", "all", None, patcher=patcher)
    assert "docs/guide.md::Main Guide::OAuth Setup" in sym_note
    assert "#oauth-setup" not in sym_note


def test_56_markdown_replace_overwriting_opening_code_fence_is_refused_with_clear_hint(tmp_path: Path) -> None:
    """#56: Replacing [23, 31] where line 23 is '```csharp' and replacement starts with code + closing '```' is refused with an explicit opening-fence message."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "spark.md").write_text(
        "# LINQ-to-Spark\n\n## Connecting\n\n"
        "**Advanced Configuration:**\n\n"
        "```csharp\n"
        "using var context = Spark.Connect();\n"
        "```\n\n"
        "**Next Example:**\n\n"
        "```csharp\n"
        "var df = context.Read();\n"
        "```\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    sec = patcher.read_block("docs/spark.md::LINQ-to-Spark::Connecting", answer_format="json")

    # Line 5 is '```csharp', line 6 is 'using var...', line 7 is '```'
    res = patcher.edit_block(
        "docs/spark.md::LINQ-to-Spark::Connecting",
        ops=[
            {
                "replace": [5, 7],
                "content": (
                    "using var context = Spark.Connect();\n"
                    "```\n\n"
                    "**Auth:**\n\n"
                    "```csharp\n"
                    "o.AuthMode = DatabricksAuthMode.OAuth;\n"
                    "```"
                ),
            }
        ],
        expected_hash=sec["content_hash"],
    )
    assert res["status"] == "error"
    assert "the opening code fence" in res["error"]
    assert "Include '```csharp' on the first line" in res["actionable_hint"]


@pytest.mark.anyio
async def test_57_all_refused_batch_strips_oversized_numbered_text_on_non_stale_errors(tmp_path: Path) -> None:
    """#57: When an entire batch is refused (0/N) for a rule guard (not stale/wrong-block), oversized numbered_text is omitted to prevent context bloat."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    body_lines = "\n".join(f"Line {i} of padding text to make the section longer than 600 chars." for i in range(1, 20))
    (ws / "docs" / "a.md").write_text(
        f"# Doc A\n\n## Sec A\n\n{body_lines}\n1. **Item 1**:\n   - Sub-bullet 1 that must not be dropped.\n",
        encoding="utf-8",
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    sec = server.patcher.read_block("docs/a.md::Doc A::Sec A", answer_format="json")
    last_ln = sec["line_count"]
    res = await server.mcp.call_tool(
        "edit_block",
        {
            "edits": [
                {
                    "path": "docs/a.md::Doc A::Sec A",
                    "expected_hash": sec["content_hash"],
                    "ops": [{"replace": [last_ln - 1, last_ln], "content": "1. **Item 1 Renamed**:"}],
                }
            ]
        },
    )
    payload = json.loads(res.content[0].text)
    assert payload["status"] == "error"
    assert payload["applied"] == 0
    assert "numbered_text" not in payload["results"][0]