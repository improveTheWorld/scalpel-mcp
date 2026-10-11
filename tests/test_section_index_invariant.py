"""Invariant between the section table and the file it describes.

An identifier is a NAME (path#slug-of-heading). The text of a block is found by
POSITION (order_index). Both live in different places -- the table and the file --
so they can disagree, and when they do, read_block returns the name of one
paragraph with the text of another, and edit_block writes there.

Measured on mapab/a r1: get_document_section(#9-databricks-...) answered with the
OAuth heading and the Archive text, then edit_block renamed the Archive section.
These tests pin the invariant that must hold after every mutation: the table is
exactly the table the file implies, name and position included.
"""

from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.documents import markdown_layout, stable_section_ids
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

MD = (
    "# Guide\n\n"
    "## 7. Unity Catalog\n- trois niveaux.\n\n"
    "### 8. Telemetrie\n- user agent.\n- spark.databricks.driver.userAgent.\n\n"
    "### 9. Archive & Scratchpad Hygiene\n- ne pas stocker les audits dans les sources.\n\n"
    "### 10. Cross-Referencing Checklist\n- verifier par plusieurs motifs.\n"
)


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "guidance.md").write_text(MD, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, db, Patcher(config, db, indexer)


def assert_table_matches_file(ws: Path, db: DatabaseManager, step: str) -> None:
    """The table must be exactly what the file implies: name AND position."""
    text = (ws / "guidance.md").read_text(encoding="utf-8")
    layout = markdown_layout(text)
    rows = db.get_document_sections("guidance.md")

    assert [r["order_index"] for r in rows] == list(range(len(rows))), step
    assert len(rows) == len(layout), "%s: %d rows for %d headings" % (step, len(rows), len(layout))
    for row, item in zip(rows, layout):
        assert row["order_index"] == item["index"], step
        assert row["heading"] == item["heading"], "%s: row %d says %r, file says %r" % (
            step, row["order_index"], row["heading"], item["heading"])
        assert row["level"] == item["level"], step

    # No stale name may survive: the identifiers in the table are exactly the ones
    # the live file produces.
    assert [r["id"] for r in rows] == stable_section_ids("guidance.md", [
        type("S", (), {"heading": i["heading"], "level": i["level"], "content": ""})()
        for i in layout
    ]), step


def test_the_table_follows_every_document_mutation(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    assert_table_matches_file(ws, db, "indexation initiale")

    steps = []

    def block(identifier):
        return patcher.read_block(identifier)

    def edit(identifier, ops, label):
        read = block(identifier)
        result = patcher.edit_block(identifier, ops=ops, expected_hash=read["content_hash"])
        assert result["status"] == "success", (label, result)
        steps.append(label)
        assert_table_matches_file(ws, db, label)
        return result

    edit(
        "guidance.md#unity-catalog/telemetrie",
        [{"replace": [2, 2], "content": "- user agent (modifie)."}],
        "corps edite",
    )
    edit(
        "guidance.md#unity-catalog/archive-scratchpad-hygiene",
        [{"replace": [1, 1], "content": "### 9. Hygiene Archive"}],
        "renommage",
    )
    edit(
        "guidance.md#unity-catalog/telemetrie",
        [{"insert_after": 3, "content": "\n#### Detail\n- detail."}],
        "ajout d un paragraphe",
    )

    inserted = patcher.insert_block(
        "guidance.md#unity-catalog",
        "guidance.md#unity-catalog/cross-referencing-checklist",
        "11. New Section\ncorps.",
    )
    assert inserted["status"] == "success", inserted
    assert_table_matches_file(ws, db, "insertion")

    removed = patcher.delete_document_section("guidance.md#unity-catalog/new-section")
    assert removed["status"] == "success", removed
    assert_table_matches_file(ws, db, "suppression")

    root = patcher.read_block("guidance.md")
    wrote = patcher.edit_block(
        "guidance.md", ops=[{"insert_after": 0, "content": "Avant le titre."}], expected_hash=root["content_hash"]
    )
    assert wrote["status"] == "success", wrote
    assert_table_matches_file(ws, db, "racine editee")

    assert len(steps) == 3


def test_a_section_is_resolved_by_name_even_when_the_table_lags(tmp_path: Path) -> None:
    """The FILE decides, not the table: a stale order_index cannot point elsewhere.

    Measured on mapab/a r1: the table was one step behind the file, so read_block
    answered with the NAME of one paragraph and the TEXT of its neighbour, and the
    next edit rewrote the wrong section. An identifier is a name, so it is matched
    against the names the live file produces.
    """
    ws, db, patcher = make_env(tmp_path)
    before = (ws / "guidance.md").read_text(encoding="utf-8")
    # A section is inserted on disk ONLY: every order_index below it shifts by one
    # and the table is never told.
    (ws / "guidance.md").write_text(
        before.replace("## 7. Unity Catalog", "## 6. Newcomer\ncorps\n\n## 7. Unity Catalog"),
        encoding="utf-8",
    )

    read = patcher.read_block("guidance.md#unity-catalog/archive-scratchpad-hygiene")

    assert read["status"] == "success", read
    assert "Archive & Scratchpad Hygiene" in read["numbered_text"].splitlines()[0]
    # The new name resolves too, and a name the file no longer produces is simply
    # not found -- never silently swapped for a neighbour.
    assert patcher.read_block("guidance.md#newcomer")["status"] == "success"
    assert patcher.read_block("guidance.md#gone")["status"] == "error"

