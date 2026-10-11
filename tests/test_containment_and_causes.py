"""Containment of STORED rows, and refusals that name the cause they tested.

Containing the indexer is not enough: a row written by another tool -- or left by a run
before the fix -- was still served in full by the search, the listing and the project map.
"""

import json
from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher


def make_env(base: Path):
    ws = base / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "src").mkdir()
    (ws / "docs" / "ok.md").write_text("# Public\n\nNothing to see.\n", encoding="utf-8")
    (ws / "src" / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, config, db, indexer, Patcher(config, db, indexer)


def inject_outside_document(db: DatabaseManager, outside: Path) -> None:
    """Write the row the way a pre-fix build, or another tool, could have left it."""
    outside.mkdir(parents=True, exist_ok=True)
    leak = outside / "leak.md"
    leak.write_text("# Financial Secrets\n\nThe code name is ZEUS-7742.\n", encoding="utf-8")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO documents (id, file_path, doc_type, title, section_count, content_hash, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(leak), str(leak), "markdown", "Financial Secrets", 1, "x", "now"),
        )
        conn.execute(
            "INSERT INTO document_sections (id, document_id, heading, level, order_index, content, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(leak) + "#financial-secrets", str(leak), "Financial Secrets", 1, 0,
             "The code name is ZEUS-7742.", "x"),
        )
        conn.commit()


def test_a_stored_row_that_leaves_the_workspace_is_never_served(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path)
    inject_outside_document(db, tmp_path / "outside")

    assert [row["file_path"] for row in db.list_documents()] == ["docs/ok.md"]
    assert db.search_documents("ZEUS") == []
    mapped = db.get_project_map()
    assert "leak.md" not in json.dumps(mapped)
    assert mapped["summary"]["documents"] == 1
    assert mapped["summary"]["sections"] == 1
    assert "ZEUS" not in json.dumps(mapped)


def test_the_filter_is_the_callers_choice_and_is_off_by_default(tmp_path: Path) -> None:
    """A caller that never says which root it is describing gets the raw table."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    inject_outside_document(db, tmp_path / "outside")

    unfiltered = DatabaseManager(config.db_path)

    assert len(unfiltered.list_documents()) == 2


def test_a_file_hidden_by_an_ignore_pattern_says_so(tmp_path: Path) -> None:
    """It used to claim the extension was unsupported, which was false for a .py."""
    ws = tmp_path / "ws"
    (ws / "node_modules").mkdir(parents=True)
    (ws / "node_modules" / "evil.py").write_text("class Evil:\n    pass\n", encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    indexer = Indexer(config, db)
    indexer.index_workspace()

    result = Patcher(config, db, indexer).read_block("node_modules/evil.py")

    assert result["status"] == "error"
    assert "ignore pattern" in result["error"]
    assert "node_modules" in result["error"]
    assert "neither" not in result["error"]


def test_a_path_that_leaves_the_workspace_says_that_and_not_a_trailing_dot(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path)

    for bad in ("..", "../..", "../../outside"):
        result = patcher.read_block(bad)
        assert "resolves outside the workspace root" in result["error"], (bad, result)


def test_the_workspace_root_is_reachable_as_dot(tmp_path: Path) -> None:
    """Refusing every spelling of the root left the top-level layout unreachable."""
    ws, config, db, indexer, patcher = make_env(tmp_path)

    root = patcher.read_block(".", answer_format="json")

    assert root["status"] == "success"
    # The canonical id of the root is '.', and it is the ONLY path in the answer: the
    # children are named short, the '/' is the type.
    assert root["id"] == "."
    assert {child["name"] for child in root["children"]} == {"docs/", "src/"}
    # '' still names nothing: an empty id is not the root.
    assert patcher.read_block("")["status"] == "error"


def test_an_alias_of_the_root_is_answered_under_its_canonical_id(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "sub").mkdir()

    result = patcher.read_block("sub/..", answer_format="json")

    assert result["status"] == "success"
    assert result["id"] == "."
    assert {child["name"] for child in result["children"]} == {"docs/", "src/", "sub/"}


def test_a_symbol_the_file_no_longer_declares_says_so(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path)
    (ws / "src" / "a.py").write_text("class B:\n    pass\n", encoding="utf-8")
    indexer.index_file(ws / "src" / "a.py")

    result = patcher.read_block("src/a.py::A")

    assert result["status"] == "error"
    assert "no longer declares" in result["error"]
    assert "src/a.py" in result["actionable_hint"]