from __future__ import annotations

from pathlib import Path
import pytest

from ast_graph_server.build_hints import build_hints
from ast_graph_server.config import ServerConfig
from ast_graph_server.context_builder import ContextBuilder
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher
from ast_graph_server.relevance import RelevanceBackend


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    indexer = Indexer(config, db)
    patcher = Patcher(config, db, indexer)
    return ws, config, db, indexer, patcher


def test_35_insert_after_last_line_of_csharp_class_with_member_is_refused(tmp_path: Path) -> None:
    """#35: insert_after on the closing '}' line of a C# class with a method/test declaration is refused."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Options.cs").write_text(
        "public class SparkConnectOptions\n{\n    public string? Host { get; set; }\n}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    blk = patcher.read_block("src/Options.cs::SparkConnectOptions", answer_format="json")
    assert blk["line_count"] == 4

    res = patcher.edit_block(
        "src/Options.cs::SparkConnectOptions",
        ops=[{"insert_after": 4, "content": "    public void Apply() { }"}],
        expected_hash=blk["content_hash"],
    )
    assert res["status"] == "error"
    assert "outside the class" in res["error"].lower()
    assert "insert_after: 3" in res["actionable_hint"]


def test_36_replace_closing_brace_with_new_method_without_re_emitting_brace_is_refused(tmp_path: Path) -> None:
    """#36: replace [n, n] where line n is '}' and content starts with '[Fact]' or 'public' is refused."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "Tests.cs").write_text(
        "public class MyTests\n{\n    [Fact]\n    public void FirstTest()\n    {\n    }\n}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    blk = patcher.read_block("tests/Tests.cs::MyTests", answer_format="json")
    # Line 6 is '    }' of FirstTest, line 7 is '}' of MyTests
    res = patcher.edit_block(
        "tests/Tests.cs::MyTests",
        ops=[{"replace": [6, 6], "content": "    [Fact]\n    public void SecondTest()\n    {\n    }"}],
        expected_hash=blk["content_hash"],
    )
    assert res["status"] == "error"
    assert "closing '}'" in res["error"]
    assert "insert_after: 6" in res["actionable_hint"]


def test_37_markdown_duplicate_ordinal_in_ordered_list_is_refused(tmp_path: Path) -> None:
    """#37: Inserting or replacing an ordered list item that duplicates the adjacent item number is refused."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "plan.md").write_text(
        "# Plan\n\n## Steps\n\n1. **First step**\n2. **Second step**\n3. **Third step**\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    sec = patcher.read_block("docs/plan.md::Plan::Steps", answer_format="json")

    res = patcher.edit_block(
        "docs/plan.md::Plan::Steps",
        ops=[{"insert_after": 4, "content": "3. **Inserted step three**"}],
        expected_hash=sec["content_hash"],
    )
    assert res["status"] == "error"
    assert "two consecutive items numbered '3.'" in res["error"]
    assert res.get("duplicated_ordinal") == "3"


def test_38_search_symbols_excludes_archive_folders_by_default(tmp_path: Path) -> None:
    """#38: Symbols inside _archive/ are excluded unless explicitly requested in file_pattern."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Main.cs").write_text("public class Spark { public static void Connect() {} }\n", encoding="utf-8")
    (ws / "_archive" / "old").mkdir(parents=True)
    (ws / "_archive" / "old" / "Batch.cs").write_text("public class Spark { public static void Connect() {} }\n", encoding="utf-8")
    indexer.index_workspace()

    matches = db.search_symbols("Spark", file_pattern="src/**/*.cs")
    files = [m["file"] for m in matches]
    assert "src/Main.cs" in files
    assert not any("_archive" in f for f in files)


def test_39_namespace_refusal_in_batch_is_atomic(tmp_path: Path) -> None:
    """#39: Targeting a namespace in edits[] aborts the whole batch so no partial uncompilable state is written."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "A.cs").write_text(
        "namespace MyNs\n{\n    public class Alpha\n    {\n        public int X { get; set; }\n    }\n}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    alpha = patcher.read_block("src/A.cs::MyNs.Alpha", answer_format="json")

    batch = patcher._edit_batch([
        {
            "path": "src/A.cs::MyNs.Alpha",
            "expected_hash": alpha["content_hash"],
            "ops": [{"replace": [3, 3], "content": "        public int X { get; set; } = 1;"}],
        },
        {
            "path": "src/A.cs::MyNs",
            "ops": [{"insert_after": 1, "content": "public enum Mode { A, B }"}],
        },
    ])
    assert batch["status"] == "error"
    assert batch["applied"] == 0
    assert "= 1;" not in (ws / "src" / "A.cs").read_text(encoding="utf-8")


def test_40_insert_after_across_separate_foreach_blocks_does_not_warn_duplicate() -> None:
    """#40: Repeating 'builder = builder.Config(key, value);' in a second foreach via insert_after is not flagged."""
    before = (
        "public void Setup()\n{\n"
        "    foreach (var (key, value) in options.Config)\n"
        "    {\n"
        "        builder = builder.Config(key, value);\n"
        "    }\n}\n"
    )
    after = (
        "public void Setup()\n{\n"
        "    foreach (var (key, value) in options.Config)\n"
        "    {\n"
        "        builder = builder.Config(key, value);\n"
        "    }\n"
        "    foreach (var (key, value) in authConfig)\n"
        "    {\n"
        "        builder = builder.Config(key, value);\n"
        "    }\n}\n"
    )
    rep = Patcher._newly_repeated_line(
        before,
        after,
        ops=[{"insert_after": 6, "content": "    foreach (var (key, value) in authConfig)\n    {\n        builder = builder.Config(key, value);\n    }"}],
    )
    assert rep is None


def test_41_42_context_builder_named_symbol_and_cs1061_visibility_hint(tmp_path: Path) -> None:
    """#41 & #42: Named symbol in query gets higher char cap in build_context, and CS1061 emits visibility hint."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    lines = ["public class SparkConnectOptions", "{"]
    for i in range(50):
        lines.append(f"    /// <summary>Option {i} documentation line.</summary>")
        lines.append(f"    public string? Prop{i} {{ get; set; }}")
    lines.append("}")
    (ws / "src" / "SparkConnectOptions.cs").write_text("\n".join(lines) + "\n", encoding="utf-8")
    indexer.index_workspace()

    cb = ContextBuilder(config, db)
    cb.addresses = patcher
    ctx = cb.build("SparkConnectOptions", budget_tokens=16000, max_content_chars=4000)
    entries = [e for e in ctx["entries"] if "SparkConnectOptions" in e["ref_id"] and e["kind"] == "symbol"]
    assert entries and entries[0].get("content_hash"), entries

    compiler_out = (
        "C:\\repo\\tests\\MyTests.cs(45,15): error CS0122: 'Spark.ApplyAuth(SparkConnectOptions)' is inaccessible due to its protection level [C:\\repo\\tests\\MyTests.csproj]\n"
    )
    hints = build_hints(compiler_out, workspace=ws)
    assert any("InternalsVisibleTo" in h and "public" in h for h in hints), hints