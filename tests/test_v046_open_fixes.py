import json
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager, path_matches
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


CSHARP_STRUCT = """namespace Demo;

public struct S
{
    private readonly int _count;

    public const string Name = "demo";

    public int A;
}
"""


# --- path_matches: the filter must describe what it says -------------------------------


def test_path_matches_does_not_widen_a_middle_wildcard_to_the_whole_folder() -> None:
    """`src/*/Foo.cs` used to return True for ANY file under src/: the prefix branch fired
    because the LAST segment carried no '*', so a search scoped to one file silently answered
    with the whole tree (a false positive the caller cannot see). A pattern whose LAST segment
    names a FILE must not match a sibling file, whatever the middle wildcard is."""
    assert path_matches("src/x/Other.cs", "src/*/Foo.cs") is False
    assert path_matches("docs/Other.cs", "src/*/Foo.cs") is False
    assert path_matches("src/x/Foo.cs", "src/*/Foo.cs") is True
    # `*` crossing a '/' is fnmatch's own documented behaviour for a glob -- NOT the bug fixed
    # above -- so it is pinned here rather than assumed: rewriting that rule would move every
    # caller's filter.
    assert path_matches("src/x/y/Foo.cs", "src/*/Foo.cs") is True


def test_path_matches_accepts_a_dot_slash_relative_pattern() -> None:
    """Every stored path is relative to the workspace root, so `./src/**/*.cs` matched
    NOTHING: the leading './' was kept and compared against paths that never carry it."""
    assert path_matches("src/a/b/c.cs", "./src/**/*.cs") is True
    assert path_matches("docs/a.md", "./src/**/*.cs") is False


def test_path_matches_still_scopes_a_pattern_that_names_a_folder() -> None:
    """The legitimate folder scope must survive the fix: `DataLinq.Enterprise/spark` is a
    directory, and a directory means everything under it."""
    assert path_matches("DataLinq.Enterprise/spark/src/a.cs", "DataLinq.Enterprise/spark") is True
    assert path_matches("DataLinq.Enterprise/other/a.cs", "DataLinq.Enterprise/spark") is False
    assert path_matches("src/a.cs", "src/**") is True


# --- C# fields: the parser dropped every field and constant ---------------------------


def test_csharp_field_declarations_are_indexed(tmp_path: Path) -> None:
    """Tree-sitter C# carries a FIELD's name one level below a property's
    (field_declaration -> variable_declaration -> variable_declarator -> name).
    Reading only the direct 'name' returned None, so every field and constant of every C#
    file was silently dropped: neither listed by read_block nor addressable."""
    ws, _config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.cs").write_text(CSHARP_STRUCT, encoding="utf-8")
    indexer.index_workspace()

    rows = {str(row["name"]): str(row["id"]) for row in db.get_symbols_for_file("src/a.cs")}
    assert "_count" in rows, rows
    assert "Name" in rows, rows
    assert "A" in rows, rows

    # ...and a field is a BLOCK the caller can read and edit, with its own stamp.
    read = patcher.read_block(rows["_count"], answer_format="json")
    assert read["status"] == "success", read
    assert "private readonly int _count;" in read["numbered_text"]
    assert read["content_hash"]


# --- insert_symbol: a container is a container ----------------------------------------


def test_insert_symbol_into_struct_succeeds(tmp_path: Path) -> None:
    """Only `class` took the member branch while the post-condition accepted
    ('class','struct','interface','record','enum'): on those four the code was appended
    OUTSIDE the type and the post-condition refused the call, 100% of the time."""
    ws, _config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.cs").write_text(CSHARP_STRUCT, encoding="utf-8")
    indexer.index_workspace()

    rows = {str(r["name"]): str(r["id"]) for r in db.get_symbols_for_file("src/a.cs")}
    struct_id = rows["S"]

    res = patcher.insert_symbol(struct_id, code="public int B;")
    assert res["status"] == "success", res
    created = db.get_symbol(struct_id + ".B")
    assert created is not None, sorted(rows)
    text = (ws / "src" / "a.cs").read_text(encoding="utf-8")
    assert "public int B;" in text[text.index("public struct S") :]


def test_insert_symbol_returns_the_stamp_of_what_it_created(tmp_path: Path) -> None:
    """The reply used to carry the id and the numbering WITHOUT the hash, so editing what
    one had just created cost a read_block."""
    ws, _config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.cs").write_text(CSHARP_STRUCT, encoding="utf-8")
    indexer.index_workspace()

    rows = {str(r["name"]): str(r["id"]) for r in db.get_symbols_for_file("src/a.cs")}
    struct_id = rows["S"]

    res = patcher.insert_symbol(struct_id, code="public int B;")
    assert res["status"] == "success", res
    state = patcher.block_metrics(struct_id + ".B")
    assert state is not None
    assert res.get("content_hash") == state["content_hash"]
    assert res.get("line_count") == state["line_count"]


def test_insert_symbol_accepts_the_container_as_its_own_anchor_and_inserts_first(
    tmp_path: Path,
) -> None:
    """`anchor=<parent>` is how the Markdown route asks for "insert FIRST"; on a code parent
    the same word was refused as "not a direct child of itself"."""
    ws, _config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.cs").write_text(CSHARP_STRUCT, encoding="utf-8")
    indexer.index_workspace()

    rows = {str(r["name"]): str(r["id"]) for r in db.get_symbols_for_file("src/a.cs")}
    struct_id = rows["S"]

    res = patcher.insert_symbol(struct_id, code="public int First;", anchor=struct_id)
    assert res["status"] == "success", res
    text = (ws / "src" / "a.cs").read_text(encoding="utf-8")
    assert text.index("public int First;") < text.index("public int A;")


def test_insert_symbol_routes_a_member_into_the_single_container(tmp_path: Path) -> None:
    """A member named under the FILE (not the container) is not ambiguous when the file holds
    ONE type: the refusal itself named that type, so the tool now seats the member inside it."""
    ws, _config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.cs").write_text(CSHARP_STRUCT, encoding="utf-8")
    indexer.index_workspace()

    rows = {str(r["name"]): str(r["id"]) for r in db.get_symbols_for_file("src/a.cs")}
    struct_id = rows["S"]

    res = patcher.insert_symbol("src/a.cs", code="public void Added() {}")
    assert res["status"] == "success", res
    assert res.get("routed_to_parent")
    assert db.get_symbol(struct_id + ".Added") is not None, sorted(rows)
    assert any("ONE" in str(w) for w in res.get("warnings") or [])


def test_insert_symbol_private_nested_type_is_allowed(tmp_path: Path) -> None:
    """C# forbids `private enum` at namespace level (CS1527): it is legal ONLY inside a class,
    so the nesting guard must not refuse it."""
    ws, _config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.cs").write_text(CSHARP_STRUCT, encoding="utf-8")
    indexer.index_workspace()

    rows = {str(r["name"]): str(r["id"]) for r in db.get_symbols_for_file("src/a.cs")}
    struct_id = rows["S"]

    res = patcher.insert_symbol(struct_id, code="private enum State { On, Off }")
    assert res["status"] == "success", res
    assert db.get_symbol(struct_id + ".State") is not None


# --- insert_block: the batch keeps the caller's order, content keeps its bytes --------


@pytest.mark.anyio
async def test_insert_block_batch_keeps_the_caller_order(tmp_path: Path) -> None:
    """Every entry of a batch targeted the same (parent, anchor), so each landed at the same
    position and pushed the previous one DOWN: [A, B, C] was written C, B, A."""
    ws, config, _db, _indexer, _patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "a.md").write_text("# Doc\n\n## Sec\n\nbody\n", encoding="utf-8")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    res = await server.mcp.call_tool(
        "insert_block",
        {
            "parent": "docs/a.md::Doc",
            "inserts": [
                {"text": "Alpha\nbody alpha"},
                {"text": "Beta\nbody beta"},
                {"text": "Gamma\nbody gamma"},
            ],
        },
    )
    payload = json.loads(res.content[0].text)
    assert payload["status"] == "success", payload
    assert payload["applied"] == 3

    text = (ws / "docs" / "a.md").read_text(encoding="utf-8")
    assert text.index("Alpha") < text.index("Beta") < text.index("Gamma")


@pytest.mark.anyio
async def test_insert_block_content_is_written_byte_for_byte(tmp_path: Path) -> None:
    """`content` is the BODY: when it is supplied the title is `text` and the body is written
    as sent, trailing newline included (the flattened text= form went through a strip)."""
    ws, config, _db, _indexer, _patcher = make_env(tmp_path)
    (ws / "docs").mkdir(parents=True)
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    res = await server.mcp.call_tool(
        "insert_block",
        {"parent": "docs", "text": "new.md", "content": "hello\n"},
    )
    payload = json.loads(res.content[0].text)
    assert payload["status"] == "success", payload
    assert (ws / "docs" / "new.md").read_bytes() == b"hello\n"