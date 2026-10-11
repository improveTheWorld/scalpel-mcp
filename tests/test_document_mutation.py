"""Tests for Markdown document mutation (paragraph editing)."""

from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

MD = "# Project Title\n\nIntro paragraph.\n\n## Section A\n\nBody A.\n\n## Section B\n\nBody B.\n"


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "spec.md").write_text(MD, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, config, db, indexer, Patcher(config, db, indexer)


def test_update_document_section(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path)

    result = patcher.update_document_section("docs/spec.md#1", "New body A.\n\nSecond paragraph.")

    assert result["status"] == "success"
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "## Section A\n\nNew body A.\n\nSecond paragraph." in text
    assert "## Section B\n\nBody B." in text  # untouched
    assert "New body A." in db.get_document_section("docs/spec.md#1")["content"]


def test_update_document_section_fragment_edits(tmp_path: Path) -> None:
    """A paragraph can be corrected by quoting only the changed sentence."""
    ws, config, db, indexer, patcher = make_env(tmp_path)
    result = patcher.update_document_section(
        "docs/spec.md#section-a",
        edits=[{"old": "Body A.", "new": "Body A, revised."}],
    )
    assert result["status"] == "success", result
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "Body A, revised." in text
    assert "## Section A" in text


def test_insert_block_after(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path)

    result = patcher.insert_block(
        "docs/spec.md#project-title", "docs/spec.md#1", "Section C\nBody C."
    )

    assert result["status"] == "success"
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert text.index("Section C") < text.index("Section B")
    assert "Section C" in [section["heading"] for section in db.get_document_sections("docs/spec.md")]

def test_section_ids_are_invariant_under_insertion(tmp_path: Path) -> None:
    """Inserting a paragraph must not renumber the ids of the others.

    `file#<order_index>` made every later id shift, so an id read before an edit
    could silently designate a different paragraph after it (measured 533 times
    across 22 of 25 runs). Stable ids are keyed on the heading instead.
    """
    ws, config, db, indexer, patcher = make_env(tmp_path)
    before = {s["heading"]: s["id"] for s in db.get_document_sections("docs/spec.md")}

    result = patcher.insert_block(before["Project Title"], before["Project Title"], "Inserted\nBody.")

    assert result["status"] == "success", result
    after = {s["heading"]: s["id"] for s in db.get_document_sections("docs/spec.md")}
    assert after["Inserted"] == "docs/spec.md#inserted"
    for heading in ("Section A", "Section B"):
        assert after[heading] == before[heading], heading
    # An id captured before the insertion still resolves to the same paragraph.
    assert db.get_document_section(before["Section B"])["heading"] == "Section B"


def test_insert_block_at_end(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path)

    # the document's own children are its top-level sections: the last one here is
    # "# Project Title", and the new block goes after its whole subtree.
    top = db.get_document_sections("docs/spec.md")[0]["id"]
    result = patcher.insert_block("docs/spec.md", top, "Appendix\nExtra.")

    assert result["status"] == "success"
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8").rstrip().endswith("Extra.")
    assert db.get_document_sections("docs/spec.md")[-1]["heading"] == "Appendix"


def test_an_edit_does_not_normalise_the_gap_before_the_next_heading(tmp_path: Path) -> None:
    """The caller replaces a BLOCK, not the spacing after it.

    Normalising that gap to one blank line made a one-line preamble edit come back with
    a different line_count: the file gained a line nobody wrote, so the agent had to
    re-read to find out why (happy-verifier N3).
    """
    ws, config, db, indexer, patcher = make_env(tmp_path)

    for original in ("preamble\n# Titre\n\ncorps\n", "preamble\n\n\n# Titre\n\ncorps\n"):
        target = ws / "docs" / "spec.md"
        target.write_text(original, encoding="utf-8")
        indexer.index_file(target)
        read = patcher.read_block("docs/spec.md")

        result = patcher.edit_block(
            "docs/spec.md", ops=[{"replace": [1, 1], "content": "preamble X"}],
            expected_hash=read["content_hash"],
        )

        assert result["status"] == "success", result
        assert target.read_text(encoding="utf-8") == original.replace("preamble", "preamble X")


def test_document_mutation_rejects_non_markdown(tmp_path: Path) -> None:
    """A path that is not a Markdown document cannot take a paragraph.

    The refusal now says which door DOES exist: the old bare "no document or section"
    left the caller with nothing to do, since the path it named does not exist either.
    """
    ws, config, db, indexer, patcher = make_env(tmp_path)
    result = patcher.insert_block("docs/other.docx", "docs/other.docx", "Heading\nContent")
    assert result["status"] == "error"
    assert "does not exist yet" in result["error"]
    assert "insert_block(parent='docs'" in result["actionable_hint"]
    assert not (ws / "docs" / "other.docx").exists()
