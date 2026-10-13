"""insert_block CREATES: one child of a directory, named by the first line of text.

Roadmap section 12.3 etape 3: insert_block is the only door that modifies a block's
children. A directory's children are FILES, so inserting into one creates the file. The
name carries the type -- a trailing slash marks a directory -- because 12.2 says identity
IS the name: a separate "kind" field could disagree with the name, and two blocks could
then share one.

Unifying the primitives (12.3 etape 4) does NOT include code yet: that step is gated on
etape 2 (symbols resolved by re-parsing), so a code file still refuses a child here and
names insert_symbol as the door that exists.
"""

from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher


def make_env(base: Path):
    ws = base / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "src").mkdir(parents=True)
    (ws / "docs" / "existing.md").write_text("# Titre\n\ncorps\n", encoding="utf-8")
    (ws / "src" / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    Indexer(config, db).index_workspace()
    return ws, db, Patcher(config, db, Indexer(config, db))


def test_a_file_created_in_a_directory_is_listed_by_it(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("docs", text="runbook.md\n# Runbook\n\ncorps\n")

    assert result["status"] == "success", result
    assert result["kind"] == "file"
    assert result["block_id"] == "docs/runbook.md"
    assert result["readable_as_a_block"] is True
    assert (ws / "docs" / "runbook.md").read_text(encoding="utf-8") == "# Runbook\n\ncorps"
    # The listing names a child SHORT, inside the parent whose path is written above.
    assert "runbook.md" in {child["name"] for child in patcher.read_block("docs", answer_format="json")["children"]}


def test_a_directory_is_created_by_a_trailing_slash(tmp_path: Path) -> None:
    """Two calls, because a block sits DIRECTLY in its parent: no separator in a name."""
    ws, db, patcher = make_env(tmp_path)

    made = patcher.insert_block("docs", text="guides/")

    assert made["status"] == "success", made
    assert made["kind"] == "directory"
    assert made["name"] == "guides/"
    assert (ws / "docs" / "guides").is_dir()
    # The trailing slash IS the type, and it is the name itself that carries it.
    assert "guides/" in {child["name"] for child in patcher.read_block("docs", answer_format="json")["children"]}

    inside = patcher.insert_block(made["block_id"], text="intro.md\n# Intro\n\ncorps")

    assert inside["status"] == "success", inside
    assert inside["block_id"] == "docs/guides/intro.md"
    assert [child["name"] for child in patcher.read_block("docs/guides", answer_format="json")["children"]] == [
        "intro.md"
    ]
    assert [child["name"] for child in patcher.read_block("docs/guides/intro.md", answer_format="json")["children"]] == [
        "Intro"
    ]


def test_the_hash_returned_by_insert_block_is_the_one_edit_block_expects(tmp_path: Path) -> None:
    """The reply hands over the STATE it just made, hash included: guessing costs a turn.

    Measured on a r4 of the base 5014c43 (t21 -> t23): the agent inserted this very section, got
    the id and the numbered text with NO hash, sent the SHA-256 of the empty string as
    expected_hash, took the -- correct -- stale-read refusal for a tool fault, and declared it as
    an anomaly. One snapshot, four fields: the stamp cannot describe numbering other than the one
    returned with it.
    """
    ws, db, patcher = make_env(tmp_path)

    made = patcher.insert_block("docs/existing.md", text="## Neuf\n\ncorps\n")

    assert made["status"] == "success", made
    assert made["content_hash"] == patcher.read_block(made["created_block_id"])["content_hash"]
    # ... and the stamp is ACCEPTED by the guard: no read_block, no guessed hash.
    edited = patcher.edit_block(
        made["created_block_id"],
        ops=[{"replace": [1, 1], "content": "# Neuf"}],
        expected_hash=made["content_hash"],
    )
    assert "Stale read" not in str(edited.get("error", "")), edited


def _doubly_numbered(snippet: str) -> list:
    """Lines of a diff snippet that carry TWO 'N | ' prefixes: the numbered text was numbered again."""
    import re

    return [line for line in snippet.splitlines() if re.match(r"^[+\- ] *\d+ \| *\d+ \| ", line)]


def test_the_snippet_of_a_write_outside_a_code_block_numbers_each_line_once(tmp_path: Path) -> None:
    """insert_after the LAST line of a code block writes into the FILE; the snippet showing the
    new sibling used to print '+ 1 | 1 | def b():' -- the block's numbered text, numbered twice."""
    ws, db, patcher = make_env(tmp_path)
    block = patcher.read_block("src/a.py::A")

    edited = patcher.edit_block(
        "src/a.py::A",
        ops=[{"insert_after": block["line_count"], "content": "\n\ndef b():\n    return 1"}],
        expected_hash=block["content_hash"],
    )

    assert edited["status"] == "success" and edited.get("written_outside_block"), edited
    assert "def b():" in edited["diff_snippet"], edited["diff_snippet"]
    assert _doubly_numbered(edited["diff_snippet"]) == [], edited["diff_snippet"]


def test_the_snippet_of_a_sibling_section_numbers_each_line_once(tmp_path: Path) -> None:
    """The same defect on the Markdown route: a heading inserted after the block's last line is
    created as a SIBLING, and its snippet was built from numbered text too."""
    ws, db, patcher = make_env(tmp_path)
    block = patcher.read_block("docs/existing.md::Titre")

    edited = patcher.edit_block(
        "docs/existing.md::Titre",
        ops=[{"insert_after": block["line_count"], "content": "\n# Autre\n\ncorps deux"}],
        expected_hash=block["content_hash"],
    )

    assert edited["status"] == "success" and edited.get("title_route"), edited
    assert "Autre" in edited["diff_snippet"], edited["diff_snippet"]
    assert _doubly_numbered(edited["diff_snippet"]) == [], edited["diff_snippet"]


def test_a_code_file_created_in_a_directory_is_indexed(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("src", text="b.py\nclass B:\n    def m(self):\n        pass\n")

    assert result["status"] == "success", result
    assert result["readable_as_a_block"] is True
    outline = patcher.read_block("src/b.py", answer_format="json")
    assert "B" in {child["name"] for child in outline["children"]}


def test_the_id_returned_by_insert_block_is_accepted_by_read_block(tmp_path: Path) -> None:
    """The only guarantee that matters: a write never makes the next read false."""
    ws, db, patcher = make_env(tmp_path)

    made = patcher.insert_block("docs", text="note.md\n# Note\n\ncorps")

    read_back = patcher.read_block(made["block_id"], answer_format="json")
    assert read_back["status"] == "success", read_back
    assert [child["name"] for child in read_back["children"]] == ["Note"]
    # And the NAME read_block prints is, with the file's own path, an address the editing
    # path resolves: one resolver, so the path that was read is the path that is written.
    section = patcher.read_block("docs/note.md::Note")
    assert section["status"] == "success", section
    deeper = patcher.edit_block(
        "docs/note.md::Note",
        ops=[{"insert_after": section["line_count"], "content": "\n## Sous-section\n\ncorps"}],
        expected_hash=section["content_hash"],
    )
    assert deeper["status"] == "success", deeper


def test_insert_block_multiple_headings_reports_all_created_section_ids(tmp_path: Path) -> None:
    """When insert_block receives text with multiple headings, created_section_ids lists ALL of them."""
    ws, db, patcher = make_env(tmp_path)
    patcher.insert_block("docs", text="guide.md\n# Guide\n\nIntroduction.")

    multi_text = "### Authentication\n\nOAuth details.\n\n### PWAF Compliance\n\nCompliance criteria."
    res = patcher.insert_block("docs/guide.md::Guide", text=multi_text)

    assert res["status"] == "success", res
    assert len(res["created_section_ids"]) == 2, res["created_section_ids"]
    assert any("authentication" in s for s in res["created_section_ids"])
    assert any("compliance" in s for s in res["created_section_ids"])


def test_a_name_only_text_creates_an_empty_file(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("docs", text="vide.md")

    assert result["status"] == "success", result
    assert (ws / "docs" / "vide.md").read_bytes() == b""
    assert result["bytes"] == 0


def test_an_existing_name_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    before = (ws / "docs" / "existing.md").read_bytes()

    as_file = patcher.insert_block("docs", text="existing.md\n# Autre\n\ncorps")
    as_dir = patcher.insert_block("docs", text="existing.md/")

    assert as_file["status"] == "error"
    assert "already exists" in as_file["error"]
    assert as_file["existing_type"] == "file"
    assert as_dir["status"] == "error"
    assert (ws / "docs" / "existing.md").read_bytes() == before


def test_a_name_with_a_separator_is_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("docs", text="sub/deep.md\n# Deep\n\ncorps")

    assert result["status"] == "error"
    assert "DIRECT child" in result["error"]
    assert "text='sub/'" in result["actionable_hint"]
    assert not (ws / "docs" / "sub").exists()


def test_a_name_no_file_system_accepts_is_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("docs", text="a:b.md\n# X\n\ncorps")

    assert result["status"] == "error"
    assert "no file system accepts" in result["error"]


def test_where_has_no_meaning_under_a_directory(tmp_path: Path) -> None:
    """A directory orders its children by NAME, so there is no position to name."""
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("docs", where="docs/existing.md", text="autre.md\n# Autre")

    assert result["status"] == "error"
    assert "children by NAME" in result["error"]
    assert not (ws / "docs" / "autre.md").exists()
    # Omitting where, or passing the parent, both work.
    assert patcher.insert_block("docs", where="docs", text="autre.md\n# Autre")["status"] == "success"


def test_a_path_that_does_not_exist_yet_says_how_to_create_it(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("docs/runbook.md", text="Section\ncorps")

    assert result["status"] == "error"
    assert "does not exist yet" in result["error"]
    assert "insert_block(parent='docs', text='runbook.md')" in result["actionable_hint"]
    assert "text='runbook.md/'" in result["actionable_hint"]


def test_a_code_file_refuses_a_child_and_names_insert_block(tmp_path: Path) -> None:
    """12.3 etape 4 (unify) is gated on etape 2 (symbols re-parsed): code waits."""
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("src/a.py", text="class B:\n    pass\n")

    assert result["status"] == "error"
    assert "is a FILE" in result["error"]
    assert "insert_block" in result["actionable_hint"]


def test_insert_block_preserves_trailing_newline_in_content(tmp_path: Path) -> None:
    """insert_block strictly preserves trailing newline in created files."""
    ws, db, patcher = make_env(tmp_path)

    # 1. With explicit content ending in newline
    content_with_nl = "def hello():\n    return True\n"
    res1 = patcher.insert_block(parent="src", text="file_nl.py", content=content_with_nl)
    assert res1["status"] == "success"
    written1 = (ws / "src" / "file_nl.py").read_bytes()
    assert written1 == content_with_nl.encode("utf-8")
    assert written1.endswith(b"\n")
    assert len(written1) == len(content_with_nl.encode("utf-8"))

    # 2. With explicit content without newline
    content_no_nl = "def hello():\n    return False"
    res2 = patcher.insert_block(parent="src", text="file_no_nl.py", content=content_no_nl)
    assert res2["status"] == "success"
    written2 = (ws / "src" / "file_no_nl.py").read_bytes()
    assert written2 == content_no_nl.encode("utf-8")
    assert not written2.endswith(b"\n")

