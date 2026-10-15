from __future__ import annotations

import json
from pathlib import Path
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.lineops import format_single_edit_result
from ast_graph_server.patcher import Patcher
from ast_graph_server.rendering import render_symbol_view
from ast_graph_server.server import ASTGraphServer


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    indexer = Indexer(config, db)
    patcher = Patcher(config, db, indexer)
    return ws, config, db, indexer, patcher


def test_p1_33_discovery_exclude_patterns_do_not_block_direct_read_block(tmp_path: Path) -> None:
    """#33: reports/ and benchmarks/results/ are excluded from discovery/indexing,
    but direct read_block on directory or file works immediately."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "reports" / "bilans_scalpel").mkdir(parents=True)
    (ws / "reports" / "bilans_scalpel" / "bilan1.md").write_text(
        "# Bilan 1\n\n## Résumé\n\nTout est vert.\n", encoding="utf-8"
    )
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "app.py").write_text("def hello():\n    return 42\n", encoding="utf-8")

    # Automatic workspace indexing skips reports/
    indexer.index_workspace()
    assert indexer.should_ignore_relative("reports/bilans_scalpel/bilan1.md")
    assert indexer.ignore_pattern_for("reports/bilans_scalpel/bilan1.md") is None

    # Direct directory listing shows the entries inside reports/bilans_scalpel
    dir_res = patcher.read_block("reports/bilans_scalpel", answer_format="json")
    assert dir_res["status"] == "success"
    names = [c["name"] for c in dir_res["children"]]
    assert "bilan1.md" in names

    # Direct file read works on a flat/unindexed file inside reports/
    (ws / "reports" / "bilans_scalpel" / "note.txt").write_text("ligne 1\nligne 2\n", encoding="utf-8")
    txt_res = patcher.read_block("reports/bilans_scalpel/note.txt", answer_format="json")
    assert txt_res["status"] == "success"
    assert txt_res["kind"] == "file_text"


@pytest.mark.anyio
async def test_p1_29_batch_markdown_parent_rename_rebases_following_child_paths(tmp_path: Path) -> None:
    """#29: When entry 0 in edits[] renames a parent Markdown section, entry 1 targeting
    a child under the old parent path is automatically rebased to the new parent path."""
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "guide.md").write_text(
        "# Ancien Titre\n\nIntro.\n\n## Sous Section\n\nAncien corps.\n",
        encoding="utf-8",
    )
    cfg = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    srv = ASTGraphServer(cfg)
    srv.indexer.index_workspace()

    r_root = json.loads((await srv.mcp.call_tool("read_block", {"path": "docs/guide.md::Ancien Titre", "format": "json"})).content[0].text)
    r_sub = json.loads((await srv.mcp.call_tool("read_block", {"path": "docs/guide.md::Ancien Titre::Sous Section", "format": "json"})).content[0].text)

    batch_res = json.loads(
        (
            await srv.mcp.call_tool(
                "edit_block",
                {
                    "edits": [
                        {
                            "path": "docs/guide.md::Ancien Titre",
                            "expected_hash": r_root["content_hash"],
                            "ops": [{"replace": [1, 1], "content": "# Nouveau Titre"}],
                        },
                        {
                            "path": "docs/guide.md::Ancien Titre::Sous Section",
                            "expected_hash": r_sub["content_hash"],
                            "ops": [{"replace": [3, 3], "content": "Nouveau corps."}],
                        },
                    ],
                    "format": "json",
                },
            )
        ).content[0].text
    )
    assert batch_res["applied"] == 2 and batch_res["refused"] == 0, batch_res
    final_md = (ws / "docs" / "guide.md").read_text(encoding="utf-8")
    assert "# Nouveau Titre" in final_md
    assert "Nouveau corps." in final_md


def test_p1_21_insert_fenced_block_inside_open_markdown_fence_is_refused(tmp_path: Path) -> None:
    """#21: Inserting content that carries ``` inside an already open code fence is refused."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "spec.md").write_text(
        "# Guide\n\n## Part\n\n```csharp\nvar x = 1;\n```\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    sec = patcher.read_block("docs/spec.md::Guide::Part", answer_format="json")
    assert sec["status"] == "success"

    # Line 4 is 'var x = 1;' inside the fence opened at line 3 and closed at line 5
    res = patcher.edit_block(
        "docs/spec.md::Guide::Part",
        ops=[{"insert_after": 4, "content": "```csharp\nvar y = 2;\n```"}],
        expected_hash=sec["content_hash"],
    )
    assert res["status"] == "error"
    assert "open Markdown code fence" in res["error"]
    assert "insert_after: 5" in res["actionable_hint"]


def test_p1_23_newly_repeated_line_across_blank_lines_and_intra_replacement_try_catch() -> None:
    """#23: (1) Duplicate boundary line separated by blank lines is caught;
    (2) A line legitimately repeated twice inside the new replacement (try/catch) is NOT refused."""
    # Case 1: separated by 5 blank lines (raw_gap > 4, but non_blank_gap == 1)
    before_blank = "## Section\n\n1. **First:**\n\n\n\n\n\n2. **Second:**\n"
    after_blank = "## Section\n\n1. **First:**\n2. **Second:**\n\n\n\n\n\n2. **Second:**\n"
    rep = Patcher._newly_repeated_line(before_blank, after_blank, ops=[{"replace": [3, 3], "content": "1. **First:**\n2. **Second:**"}])
    assert rep == ("2. **Second:**", 9)

    # Case 2: line at line 3 replaced by a try/catch that contains the line twice
    before_try = "public void Run()\n{\n    channel.Writer.TryComplete();\n}\n"
    after_try = (
        "public void Run()\n{\n"
        "    try {\n        channel.Writer.TryComplete();\n    }\n"
        "    catch {\n        channel.Writer.TryComplete();\n    }\n}\n"
    )
    rep_try = Patcher._newly_repeated_line(
        before_try,
        after_try,
        ops=[{"replace": [3, 3], "content": "    try {\n        channel.Writer.TryComplete();\n    }\n    catch {\n        channel.Writer.TryComplete();\n    }"}],
    )
    assert rep_try is None


def test_p2_30_js_ts_export_preserved_on_line_1_and_imports_block(tmp_path: Path) -> None:
    """#30: JS/TS exported functions include 'export' on line 1, and top-level imports form ::imports."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "mod.js").write_text(
        "import fs from 'node:fs';\nimport path from 'node:path';\n\n"
        "export function greet(name) {\n  return 'hi ' + name;\n}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()

    imp = patcher.read_block("src/mod.js::imports", answer_format="json")
    assert imp["status"] == "success"
    assert "import fs" in imp["numbered_text"] and "import path" in imp["numbered_text"]

    fn = patcher.read_block("src/mod.js::greet", answer_format="json")
    assert fn["status"] == "success"
    assert "export function greet" in fn["numbered_text"].splitlines()[0]

    # Replacing line 1 with 'export function greet...' does not create 'export export'
    ed = patcher.edit_block(
        "src/mod.js::greet",
        ops=[{"replace": [1, 1], "content": "export function greet(name = 'world') {"}],
        expected_hash=fn["content_hash"],
    )
    assert ed["status"] == "success", ed
    assert "export export" not in (ws / "src" / "mod.js").read_text(encoding="utf-8")


def test_p2_31_csharp_top_level_statements_and_csproj_xml_editing(tmp_path: Path) -> None:
    """#31: C# Program.cs without a class exposes ::top_level_statements, and .csproj is editable."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "App").mkdir(parents=True)
    (ws / "App" / "Program.cs").write_text(
        "using System;\n\nvar msg = \"Hello\";\nConsole.WriteLine(msg);\n",
        encoding="utf-8",
    )
    (ws / "App" / "App.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk\">\n  <PropertyGroup>\n    <TargetFramework>net8.0</TargetFramework>\n  </PropertyGroup>\n</Project>\n",
        encoding="utf-8",
    )
    indexer.index_workspace()

    tls = patcher.read_block("App/Program.cs::top_level_statements", answer_format="json")
    assert tls["status"] == "success"
    assert "Console.WriteLine" in tls["numbered_text"]

    ed_cs = patcher.edit_block(
        "App/Program.cs::top_level_statements",
        ops=[{"replace": [1, 1], "content": "var msg = \"Updated\";"}],
        expected_hash=tls["content_hash"],
    )
    assert ed_cs["status"] == "success", ed_cs

    # .csproj summary still works on bare path, ::xml returns numbered XML, and edit_block edits it
    summary = patcher.read_block("App/App.csproj", answer_format="json")
    assert summary["status"] == "success"
    assert summary["target_frameworks"] == ["net8.0"]
    assert summary.get("content_hash")

    xml_block = patcher.read_block("App/App.csproj::xml", answer_format="json")
    assert xml_block["status"] == "success"
    assert "<TargetFramework>net8.0</TargetFramework>" in xml_block["numbered_text"]

    ed_proj = patcher.edit_block(
        "App/App.csproj",
        ops=[{"replace": [3, 3], "content": "    <TargetFramework>net9.0</TargetFramework>"}],
        expected_hash=xml_block["content_hash"],
    )
    assert ed_proj["status"] == "success", ed_proj
    assert "net9.0" in (ws / "App" / "App.csproj").read_text(encoding="utf-8")


def test_p3_22_27_34_rendering_headers_and_utf16_bom(tmp_path: Path) -> None:
    """#22, #27, #34: implementation view shows block lines 1..N, sibling creation header is explicit,
    and UTF-16LE BOM files are readable as file_text."""
    view_txt = render_symbol_view({
        "view": "implementation",
        "file": "src/A.cs",
        "lines": [53, 105],
        "line_count": 53,
        "content_hash": "12345678",
        "numbered_code": "1 | public void M() {}",
    })
    assert "L53-105 (block lines 1..53)" in view_txt

    sib_txt = format_single_edit_result({
        "status": "success",
        "block_id": "docs/a.md::Guide::Intro",
        "created_section_id": "docs/a.md::Guide::NewSec",
        "content_hash": "abcdef12",
        "host_content_hash": "11223344",
        "line_count": 10,
        "host_block_unchanged": True,
    })
    assert "[ok] created sibling after docs/a.md::Guide::Intro -> docs/a.md::Guide::NewSec" in sib_txt
    assert "(host block unchanged [h:11223344])" in sib_txt

    ws, config, db, indexer, patcher = make_env(tmp_path)
    utf16_file = ws / "utf16_log.txt"
    utf16_file.write_bytes(b"\xff\xfe" + "Hello UTF16\nLine 2\n".encode("utf-16-le"))
    res16 = patcher.read_block("utf16_log.txt", answer_format="json")
    assert res16["status"] == "success"
    assert "Hello UTF16" in res16["numbered_text"]