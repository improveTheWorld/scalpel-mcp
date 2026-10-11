"""The document root block: a Markdown file is a block whose id is its path.

Its own content is the text no heading owns -- the preamble, or the whole file
when it has no heading at all -- and its children are the sections. Before this,
the parent node of every section was unreachable: a preamble could not be read or
edited, and a file without any heading could not be edited at all.
"""

import hashlib
from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.parsers.base import compute_content_hash
from ast_graph_server.patcher import Patcher

MD_PREAMBLE = "Avant structure.\n\n# Titre\n\ncorps\n"
MD_FLAT = "Juste du texte.\nDeuxieme ligne.\n"
MD_HEADED = "# Titre\n\ncorps\n"


def make_env(tmp_path: Path, content: str):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.md").write_text(content, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, db, Patcher(config, db, indexer)


def test_the_preamble_is_a_block_addressed_by_the_file_path(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, MD_PREAMBLE)

    read = patcher.read_block("notes.md")

    assert read["status"] == "success", read
    # ONE envelope: the path written once, the file's size, the complete list of the
    # paragraphs -- and, because the root block IS the text no heading owns, that text
    # numbered with its hash. No 'kind': the .md extension already says what it is.
    assert read["id"] == "notes.md"
    assert "kind" not in read
    assert read["numbered_text"] == "1 | Avant structure.\n2 | "
    # The indexed preamble row and the document root are the same block.
    assert read["content_hash"] == patcher.read_block("notes.md#preamble")["content_hash"]

    result = patcher.edit_block(
        "notes.md",
        ops=[{"replace": [1, 1], "content": "Avant modifie."}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    text = (ws / "notes.md").read_text(encoding="utf-8")
    assert text == "Avant modifie.\n\n# Titre\n\ncorps\n"


def test_a_document_without_any_heading_is_editable_by_its_path(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, MD_FLAT)

    read = patcher.read_block("notes.md")

    assert read["status"] == "success", read
    assert read["numbered_text"] == "1 | Juste du texte.\n2 | Deuxieme ligne."
    # The synthetic section the indexer keeps for searchability is the same block.
    assert patcher.read_block("notes.md#notes")["content_hash"] == read["content_hash"]

    result = patcher.edit_block(
        "notes.md", ops=[{"replace": [2, 2], "content": "Ligne deux modifiee."}], expected_hash=read["content_hash"]
    )

    assert result["status"] == "success", result
    assert (ws / "notes.md").read_text(encoding="utf-8") == "Juste du texte.\nLigne deux modifiee.\n"


def test_text_can_be_written_before_the_first_heading(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, MD_HEADED)

    read = patcher.read_block("notes.md")
    # The ROOT block holds the WHOLE document, so line_count is the file's: it used to
    # report the preamble's (0 here), which told the caller the document was empty.
    assert read["line_count"] == 3

    result = patcher.edit_block(
        "notes.md", ops=[{"insert_after": 0, "content": "Avant le titre."}], expected_hash=read["content_hash"]
    )

    assert result["status"] == "success", result
    assert (ws / "notes.md").read_text(encoding="utf-8") == "Avant le titre.\n\n# Titre\n\ncorps\n"


def test_a_heading_in_the_documents_own_text_is_refused(tmp_path: Path) -> None:
    """A heading is a child node: the document root does not create them."""
    ws, db, patcher = make_env(tmp_path, MD_PREAMBLE)
    read = patcher.read_block("notes.md")
    before = (ws / "notes.md").read_text(encoding="utf-8")

    result = patcher.edit_block(
        "notes.md",
        ops=[{"insert_after": 1, "content": "\n## Nouveau"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "error"
    assert "new paragraph" in result["error"]
    assert "insert_block" in result["actionable_hint"]
    assert (ws / "notes.md").read_text(encoding="utf-8") == before


def test_the_title_is_edited_through_its_own_section(tmp_path: Path) -> None:
    """Changing the document title is an ordinary edit of the H1 block.

    The root block owns the text around the structure; the title is the first
    child node, so it is renamed where it lives -- the number and the place stay
    the tool's business.
    """
    ws, db, patcher = make_env(tmp_path, MD_HEADED)
    block_id = "notes.md#titre"
    read = patcher.read_block(block_id)

    result = patcher.edit_block(
        block_id, ops=[{"replace": [1, 1], "content": "# Titre modifie"}], expected_hash=read["content_hash"]
    )

    assert result["status"] == "success", result
    assert (ws / "notes.md").read_text(encoding="utf-8") == "# Titre modifie\n\ncorps\n"
    assert result["resulting_section_ids"] == ["notes.md#titre-modifie"]


MD_SECTIONS_ONLY = "# Guide\n\n## Configuration\n\n- a\n- b\n\n## Deployment\n\n- x\n"


def test_a_section_hash_sent_to_the_file_block_names_the_section(tmp_path: Path) -> None:
    """Run 1 of base ea25ac6 (mapab2/a), encoded here.

    The agent read a SECTION, then addressed the FILE with that section's hash. The guard answered
    "the block changed since you read it" -- a cause that is false, nothing had changed, these are
    two blocks -- and its hint prescribed an edit on the root, whose own text is empty, so the next
    call could only answer "replace range must be inside 1..0". 11 refusals from t21 to t52, first
    accepted edit_block at t63, the run cut at the 80-turn ceiling with a broken build.
    """
    ws, db, patcher = make_env(tmp_path, MD_SECTIONS_ONLY)

    section = patcher.read_block("notes.md#configuration")
    root = patcher.read_block("notes.md")
    assert section["status"] == "success" and root["status"] == "success"
    assert root["numbered_text"] == ""
    assert root["content_hash"] == compute_content_hash(b"")
    assert root["content_hash"] != section["content_hash"]

    refused = patcher.edit_block(
        "notes.md",
        ops=[{"replace": [3, 3], "content": "- a modifie"}],
        expected_hash=section["content_hash"],
    )

    assert refused["status"] == "error", refused
    assert "changed since you read it" not in refused["error"], refused
    assert refused["expected_hash_belongs_to"] == "notes.md::Guide::Configuration"
    assert "notes.md::Guide::Configuration" in refused["error"]
    assert "edit_block(path='notes.md::Guide::Configuration'" in refused["actionable_hint"]

    # The repair the hint prescribes works as written: same hash, same ops, the block it belongs to.
    applied = patcher.edit_block(
        "notes.md::Guide::Configuration",
        ops=[{"replace": [3, 3], "content": "- a modifie"}],
        expected_hash=section["content_hash"],
    )

    assert applied["status"] == "success", applied
    assert (ws / "notes.md").read_text(encoding="utf-8") == (
        "# Guide\n\n## Configuration\n\n- a modifie\n- b\n\n## Deployment\n\n- x\n"
    )


def test_inserting_a_paragraph_returns_the_state_of_what_it_created(tmp_path: Path) -> None:
    """An insert must answer the next question, or the caller pays a read to ask it.

    Measured on run 1 of base f2a9649: the agent inserted a section and then spent TWO read_block
    calls (the parent, then the new siblings) finding what it had just written. The reply now carries
    the created block's ADDRESS and its numbered text -- no hash, because an insert has nothing to
    guard, and no second coordinate system to confuse with the block-relative numbers.
    """
    ws, db, patcher = make_env(tmp_path, MD_SECTIONS_ONLY)

    result = patcher.insert_block(
        parent_id="notes.md::Guide::Deployment",
        where="notes.md::Guide::Deployment",
        text="### Worker Init Script\n\nRuns before the cluster.",
    )

    assert result["status"] == "success", result
    assert result["created_block_id"].endswith("Worker Init Script")
    assert result["line_count"] == 4
    assert result["numbered_text"].startswith("1 | ### Worker Init Script")

    # The address returned is enough to continue: no read_block in between.
    applied = patcher.edit_block(
        result["created_block_id"],
        ops=[{"replace": [3, 3], "content": "Runs BEFORE the cluster."}],
    )
    assert applied["status"] == "success", applied
    text = (ws / "notes.md").read_text(encoding="utf-8")
    assert "Runs BEFORE the cluster." in text


def test_an_empty_block_explains_itself_when_the_op_needs_lines(tmp_path: Path) -> None:
    """The other half of the same run: t24 and t38 sent the empty root's OWN hash -- which MATCHES, so
    the guard had nothing to say -- and `apply_ops` answered "op 0: replace range must be inside 1..0".

    True, and it names no way out. The block is a document root holding only the preamble, so the
    refusal keeps the range complaint AND says what the block is and which call gives it a first line.
    The guard must not fire on its own: `insert_after 0` on an empty root is the legitimate way to
    write before the first heading, and the test below this one pins it.
    """
    ws, db, patcher = make_env(tmp_path, MD_SECTIONS_ONLY)
    before = (ws / "notes.md").read_text(encoding="utf-8")

    root = patcher.read_block("notes.md")
    refused = patcher.edit_block(
        "notes.md",
        ops=[{"replace": [1, 1], "content": "- injecte"}],
        expected_hash=root["content_hash"],
    )

    assert refused["status"] == "error", refused
    assert "must be inside 1..0" in refused["error"], refused
    assert "carries no text of its own" in refused["error"], refused
    assert "child block(s) hold the text" in refused["error"], refused
    assert "insert_after 0" in refused["actionable_hint"]
    assert (ws / "notes.md").read_text(encoding="utf-8") == before

    # The op the hint names is accepted by the same block, with the same hash.
    applied = patcher.edit_block(
        "notes.md",
        ops=[{"insert_after": 0, "content": "Avant le titre."}],
        expected_hash=root["content_hash"],
    )
    assert applied["status"] == "success", applied
    assert (ws / "notes.md").read_text(encoding="utf-8").startswith("Avant le titre.")

