"""Guards that stop an agent from corrupting a file through the AST tools.

These lock the exact failure mode observed in a real benchmark run: a C# file
with a file-scoped namespace, an XML doc comment above a class, and an agent
that re-submitted the class (with its comment) then tried to delete it.
"""

from pathlib import Path
from typing import Dict, Tuple

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

CS_FILE_SCOPED = '''using System;

namespace DataLinq.SparkQuery.UnitTests;

/// <summary>
/// Validates PWAF criteria.
/// </summary>
public class DatabricksPwafCriteriaTests
{
    /// <summary>First test.</summary>
    [Fact]
    public void A() { }

    [Fact]
    public void B() { }
}
'''

CLASS_ID = "src/T.cs::DatabricksPwafCriteriaTests"
METHOD_ID = "src/T.cs::DatabricksPwafCriteriaTests.A"
NAMESPACE_ID = "src/T.cs::DataLinq.SparkQuery.UnitTests"

CLASS_WITH_DOC = '''/// <summary>
/// Validates PWAF criteria and OAuth.
/// </summary>
public class DatabricksPwafCriteriaTests
{
    /// <summary>First test.</summary>
    [Fact]
    public void A() { }

    [Fact]
    public void B() { }
}'''


def make_env(tmp_path: Path) -> Tuple[Path, Patcher]:
    ws = tmp_path / "ws"
    src = ws / "src"
    src.mkdir(parents=True)
    (src / "T.cs").write_text(CS_FILE_SCOPED, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, Patcher(config, db, indexer)


def test_update_with_doc_comment_replaces_it_instead_of_duplicating(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)

    result = patcher.update_symbol_body(CLASS_ID, CLASS_WITH_DOC)

    assert result["status"] == "success", result
    text = (ws / "src" / "T.cs").read_text(encoding="utf-8")
    assert text.count("public class") == 1
    # one doc block above the class, one above the method
    assert text.count("/// <summary>") == 2
    assert "OAuth" in text
    assert "Validates PWAF criteria.\n" not in text


def test_update_without_doc_comment_preserves_the_existing_one(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)

    result = patcher.update_symbol_body(METHOD_ID, "[Fact]\npublic void A() { return; }")

    assert result["status"] == "success", result
    text = (ws / "src" / "T.cs").read_text(encoding="utf-8")
    assert "First test" in text
    assert "return;" in text


def test_delete_symbol_removes_its_leading_doc_comment(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)

    result = patcher.delete_symbol(CLASS_ID)

    assert result["status"] == "success", result
    text = (ws / "src" / "T.cs").read_text(encoding="utf-8")
    assert "public class" not in text
    assert "summary" not in text


def test_namespace_update_is_refused_and_leaves_file_untouched(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)
    before = (ws / "src" / "T.cs").read_bytes()
    full = before.decode("utf-8")

    result = patcher.update_symbol_body(NAMESPACE_ID, full)

    assert result["status"] == "error"
    assert "namespace" in result["error"]
    assert (ws / "src" / "T.cs").read_bytes() == before


def test_namespace_delete_is_refused(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)

    result = patcher.delete_symbol(NAMESPACE_ID)

    assert result["status"] == "error"
    assert (ws / "src" / "T.cs").read_text(encoding="utf-8") == CS_FILE_SCOPED


def test_identical_update_is_a_noop(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)
    path = ws / "src" / "T.cs"
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    sym = db.get_symbol(METHOD_ID)
    before = path.read_bytes()
    region = before[sym.start_byte:sym.end_byte].decode("utf-8")

    result = patcher.update_symbol_body(METHOD_ID, region)

    assert result["status"] == "noop", result
    assert path.read_bytes() == before


def test_duplicate_container_insert_is_refused(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)
    before = (ws / "src" / "T.cs").read_bytes()

    result = patcher.insert_symbol(
        NAMESPACE_ID, "public class DatabricksPwafCriteriaTests { }", position="end"
    )

    assert result["status"] == "error"
    assert "duplicate" in result["error"]
    assert (ws / "src" / "T.cs").read_bytes() == before


def test_insert_into_a_class_lands_inside_the_class(tmp_path: Path) -> None:
    """A member inserted at the end of a class must be a member, not a sibling.

    Anchoring on the class symbol's end_byte dropped it after the closing brace:
    tree-sitter accepted the file and the braces balanced, but the code no longer
    compiled.
    """
    ws, patcher = make_env(tmp_path)

    result = patcher.insert_symbol(CLASS_ID, "[Fact]\npublic void NewTest() { }", position="end")

    assert result["status"] == "success", result
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    assert db.get_symbol(CLASS_ID + ".NewTest") is not None
    text = (ws / "src" / "T.cs").read_text(encoding="utf-8")
    assert text.index("NewTest") < text.rindex("}")


def test_inspect_symbols_recovers_from_an_out_of_band_edit(tmp_path: Path) -> None:
    """A slice read with stale offsets used to come back wrong and unflagged."""
    import json

    ws, patcher = make_env(tmp_path)
    target = ws / "src" / "T.cs"
    target.write_text(CS_FILE_SCOPED, encoding="utf-8")
    patcher._reindex(target)
    symbol_id = CLASS_ID + ".A"

    # Edit the file outside the tools: every stored offset shifts by two lines.
    target.write_text("// inserted\n// inserted\n" + CS_FILE_SCOPED, encoding="utf-8")

    payload = patcher.read_block(symbol_id)

    assert "public void A()" in payload["numbered_text"]


def test_legitimate_method_overload_insert_is_allowed(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)

    result = patcher.insert_symbol(CLASS_ID, "[Fact]\npublic void A(int extra) { }", position="end")

    assert result["status"] == "success", result
    assert "extra" in (ws / "src" / "T.cs").read_text(encoding="utf-8")

def test_two_same_named_python_functions_are_indexed_apart(tmp_path: Path) -> None:
    """Legal Python that used to kill the SERVER.

    Both `def f` walked away with one id, so the indexer died on
    `UNIQUE constraint failed: symbols.id` and `python -m ast_graph_server` never started
    on that workspace. The C# parser has had a declaration-derived suffix for a long time;
    Python did not. Both must also be ADDRESSABLE, which is the whole point of the suffix.
    """
    import json

    ws, patcher = make_env(tmp_path)
    patcher.config.answer_format = "json"
    m_file = ws / "m.py"
    m_file.write_text(
        "def f(x):\n    return x\n\n\ndef f(x, y):\n    return x + y\n", encoding="utf-8"
    )
    patcher._reindex(m_file)

    outline = patcher.read_block("m.py")
    names = [child["name"] for child in outline["children"]]
    assert names == ["f(x)", "f(x, y)"]

    for name in names:
        read = patcher.read_block("m.py::" + name)
        assert read["status"] == "success", read
