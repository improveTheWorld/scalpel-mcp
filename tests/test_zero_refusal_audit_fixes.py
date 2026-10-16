"""Unit tests for the Zero-Refusal & Deterministic Audit fixes (2026-10-16)."""

from pathlib import Path
import json
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.context_builder import ContextBuilder
from ast_graph_server.database import DatabaseManager, path_matches
from ast_graph_server.documents import heading_lines, unclosed_fence_line
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher
from ast_graph_server.server import ASTGraphServer


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    indexer = Indexer(config, db)
    patcher = Patcher(config, db, indexer)
    return ws, config, db, indexer, patcher


def test_csharp_field_declaration_and_struct_record_insert_symbol(tmp_path: Path) -> None:
    """CSharpParser extracts field_declaration names, and insert_symbol supports struct/record/interface."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    cs_file = ws / "src" / "Models.cs"
    cs_file.write_text(
        "namespace Demo;\n\n"
        "public class Service\n"
        "{\n"
        "    private readonly int _count = 0;\n"
        "    public const string DefaultName = \"A\";\n"
        "}\n\n"
        "public struct Point\n"
        "{\n"
        "    public int X { get; set; }\n"
        "}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()

    # 1. Fields are indexed and readable by ::
    field_block = patcher.read_block("src/Models.cs::Service::_count", answer_format="json")
    assert field_block["status"] == "success", field_block
    assert "_count" in field_block["numbered_text"]

    # 2. Inserting a member into a struct without anchor lands inside the struct and returns content_hash
    ins_struct = patcher.insert_symbol("src/Models.cs::Point", "public int Y { get; set; }")
    assert ins_struct["status"] == "success", ins_struct
    assert ins_struct.get("content_hash"), ins_struct
    assert "Point::Y" in str(ins_struct.get("created_symbol_id"))

    # 3. Private nested enum inside a class is allowed by edit_block (#44 fix for CS1527)
    srv_block = patcher.read_block("src/Models.cs::Service", answer_format="json")
    last_ln = srv_block["line_count"]
    edit_priv = patcher.edit_block(
        "src/Models.cs::Service",
        ops=[{"insert_after": last_ln - 1, "content": "    private enum InternalState { Idle, Busy }"}],
        expected_hash=srv_block["content_hash"],
    )
    assert edit_priv["status"] == "success", edit_priv


def test_insert_symbol_auto_routes_member_to_single_container_and_short_anchor(tmp_path: Path) -> None:
    """insert_symbol routes a member under parent='file.cs' to the file's single container and resolves short anchors."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Single.cs").write_text(
        "namespace Demo;\n\n"
        "public class OnlyContainer\n"
        "{\n"
        "    public int First { get; set; }\n"
        "}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()

    # Auto-route from file path to OnlyContainer
    routed = patcher.insert_symbol("src/Single.cs", "public int Second { get; set; }")
    assert routed["status"] == "success", routed
    assert routed.get("routed_to_parent") == "src/Single.cs::OnlyContainer"
    assert routed.get("content_hash")

    # Short anchor name ("First") without file/class prefix
    anchored = patcher.insert_symbol(
        "src/Single.cs::OnlyContainer",
        "public int Middle { get; set; }",
        anchor="First",
    )
    assert anchored["status"] == "success", anchored


@pytest.mark.anyio
async def test_insert_block_batch_fifo_chaining_and_short_markdown_anchor(tmp_path: Path) -> None:
    """insert_block(inserts=[...]) chains entries targeting the same (parent, anchor) in FIFO order."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "guide.md").write_text(
        "# Guide\n\n## Intro\n\nStart text.\n",
        encoding="utf-8",
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    res = await server.mcp.call_tool(
        "insert_block",
        {
            "parent": "docs/guide.md::Guide",
            "anchor": "Intro",  # short anchor name without docs/guide.md::Guide::
            "inserts": [
                {"text": "## Step A\n\nBody A."},
                {"text": "## Step B\n\nBody B."},
                {"text": "## Step C\n\nBody C."},
            ],
        },
    )
    payload = json.loads(res.content[0].text)
    assert payload["status"] == "success", payload
    assert payload["applied"] == 3

    doc_text = (ws / "docs" / "guide.md").read_text(encoding="utf-8")
    pos_a = doc_text.index("## Step A")
    pos_b = doc_text.index("## Step B")
    pos_c = doc_text.index("## Step C")
    assert pos_a < pos_b < pos_c, f"Expected FIFO order A < B < C, got:\n{doc_text}"


def test_commonmark_closing_fence_cannot_carry_info_string() -> None:
    """A line like '```csharp' never closes an already-open fence in unclosed_fence_line or heading_lines."""
    md = (
        "# Title\n\n"
        "```csharp\n"
        "var x = 1;\n"
        "```csharp\n"
        "var y = 2;\n"
        "```\n"
    )
    # The first ```csharp at line 3 is closed by the bare ``` at line 7, leaving no unclosed fence,
    # whereas if ```csharp at line 5 were treated as a closing fence, line 7 would open a new unclosed fence!
    assert unclosed_fence_line(md) is None
    headings = heading_lines(md.splitlines())
    assert len(headings) == 1 and headings[0][2] == "Title"


def test_path_matches_wildcard_and_braces_and_kind_prefix_resolution(tmp_path: Path) -> None:
    """path_matches handles intermediate wildcards and brace expansion; resolver strips kind prefixes."""
    assert path_matches("src/a/Target.cs", "src/*/Target.cs") is True
    assert path_matches("src/a/Other.cs", "src/*/Target.cs") is False
    assert path_matches("src/a/Target.cs", "src/**/*.{cs,md}") is True
    assert path_matches("src/a/Target.py", "src/**/*.{cs,md}") is False

    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "mod.py").write_text("CONST_VAL = 42\ndef run_task():\n    return CONST_VAL\n", encoding="utf-8")
    indexer.index_workspace()

    # Leading 'variable ' or 'function ' copied from read_block outline resolves cleanly
    b1 = patcher.read_block("src/mod.py::variable CONST_VAL", answer_format="json")
    assert b1["status"] == "success", b1
    b2 = patcher.read_block("src/mod.py::function run_task", answer_format="json")
    assert b2["status"] == "success", b2


def test_typescript_type_enum_and_field_extraction_and_context_file_lines(tmp_path: Path) -> None:
    """TypeScriptParser indexes type aliases, enums and class fields; build_context prints [file Lstart-end]."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "types.ts").write_text(
        "import { foo } from './foo';\n\n"
        "export type UserId = string;\n\n"
        "export enum AuthMode {\n"
        "  None = 'none',\n"
        "  Token = 'token',\n"
        "}\n\n"
        "export class Client {\n"
        "  readonly mode: AuthMode = AuthMode.Token;\n"
        "}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()

    t_block = patcher.read_block("src/types.ts::UserId", answer_format="json")
    assert t_block["status"] == "success", t_block
    e_block = patcher.read_block("src/types.ts::AuthMode", answer_format="json")
    assert e_block["status"] == "success", e_block
    f_block = patcher.read_block("src/types.ts::Client::mode", answer_format="json")
    assert f_block["status"] == "success", f_block

    cb = ContextBuilder(config, db)
    cb.addresses = patcher
    ctx = cb.build("AuthMode Client", budget_tokens=8000)
    assert "[file L" in ctx["text"]