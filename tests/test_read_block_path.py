"""The path grammar of read_block: a printed NAME is an address, and the hash never is.

Frozen decision: docs/PATCH_READ_BLOCK_PATH.md sections 2, 3 and 6.
- the separator is '::' -- a code member (file.cs::Classe::Methode) and a paragraph
  (file.md::Section mere::Paragraphe) are written the same way;
- a SHORT name is accepted while it is unique in its file; a name several blocks of one
  file share is REFUSED, and the refusal NAMES the candidates;
- an overload is discriminated by its PARAMETER LIST, which is what the surface prints --
  the stored '#<sha1>' suffix never appears in a response;
- the historical 'file.md#slug' grammar is still accepted on input.
"""

import json
import re
from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

#: The stored overload discriminator: 8 hex characters after a '#'. It may live in the
#: index, never on the surface.
OVERLOAD_HASH = re.compile(r"#[0-9a-f]{8}\b")

CSHARP = """namespace N
{
    public class Over
    {
        public int Do(string a, int b) { return 1; }
        public int Do(string a) { return 2; }
    }
}
"""

MARKDOWN = """# Guide

intro

## Quick Start

corps

### New Features

one

### New Features

one bis

## Historique

### New Features

two
"""



#: Two SIBLING paragraphs of one title: the printer ranks the second one, and the rank is
#: what keeps the two paths distinct. Neither may be picked silently.
DUPLICATE_SIBLINGS = """# Notes

## Reprise

corps

### Details

un

### Details

deux
"""


#: Two halves of one PARTIAL class, each holding the same member signature. The parameter
#: list discriminates nothing here, so the printed name needs a rank or the two candidates
#: are the same address twice (torture, defect 5, measured on a real repository).
PARTIAL_PAIR = """namespace N
{
    public partial class Mapper
    {
        public string GetColumnName(string a) { return a; }
    }

    public partial class Mapper
    {
        public string GetColumnName(string a) { return a; }
    }
}
"""


def make_env(base: Path):
    ws = base / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "src" / "a.py").write_text(
        "class A:\n    def m(self):\n        pass\n\n    def n(self):\n        pass\n", encoding="utf-8"
    )
    (ws / "src" / "ov.cs").write_text(CSHARP, encoding="utf-8")
    (ws / "docs" / "guide.md").write_text(MARKDOWN, encoding="utf-8")
    (ws / "docs" / "dup.md").write_text(DUPLICATE_SIBLINGS, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    Indexer(config, db).index_workspace()
    return ws, db, Patcher(config, db, Indexer(config, db))


def test_a_short_name_unique_in_its_file_is_a_whole_address(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    read = patcher.read_block("src/a.py::m")

    assert read["status"] == "success", read
    assert read["id"] == "src/a.py::m"
    assert read["numbered_text"].startswith("1 |     def m(self):")


def test_a_dotted_chain_addresses_a_code_member(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    read = patcher.read_block("src/a.py::A::n")

    assert read["status"] == "success", read
    assert read["numbered_text"].startswith("1 |     def n(self):")


def test_a_heading_chain_addresses_a_paragraph(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    read = patcher.read_block("docs/guide.md::Quick Start::New Features")

    assert read["status"] == "success", read
    assert read["numbered_text"].startswith("1 | ### New Features")

    # The chain may be written from the very top: the outermost heading is the file's
    # own title, and both spellings designate the same paragraph.
    from_the_top = patcher.read_block("docs/guide.md::Guide::Quick Start::New Features")
    assert from_the_top["content_hash"] == read["content_hash"]


def test_case_spaces_and_punctuation_are_folded(tmp_path: Path) -> None:
    """The caller copies the printed name by hand: the server folds what it types."""
    ws, db, patcher = make_env(tmp_path)

    folded = patcher.read_block("docs/guide.md::quick-start::new  features")

    assert folded["status"] == "success", folded
    assert folded["numbered_text"].startswith("1 | ### New Features")


def test_a_name_several_paragraphs_share_is_refused_and_they_are_named(tmp_path: Path) -> None:
    """"New Features" is a name, not an address, when the file holds it twice."""
    ws, db, patcher = make_env(tmp_path)

    refused = patcher.read_block("docs/guide.md::New Features")

    assert refused["status"] == "error", refused
    assert "names 3 blocks" in refused["error"]
    # The false cause -- "the file no longer holds a paragraph with that name" -- is gone:
    # the paragraph EXISTS, three times, and only the name failed to designate one of them.
    assert "no longer" not in refused["error"]
    names = [candidate["name"] for candidate in refused["candidates"]]
    # Two of them are SIBLINGS: the rank is printed, so no two names read the same under
    # one parent. The third sits under another heading, and its own chain says which.
    assert "New Features (2)" in names
    paths = [candidate["path"] for candidate in refused["candidates"]]
    assert len(set(paths)) == 3, paths
    for candidate in refused["candidates"]:
        assert candidate["lines"]
        resolved = patcher.read_block(candidate["path"])
        assert resolved["status"] == "success", (candidate, resolved)


def test_two_siblings_of_one_title_are_never_guessed(tmp_path: Path) -> None:
    """The bare title names two paragraphs, so it designates neither of them.

    Matching the printed rank as if it were part of the title made the bare name miss the
    second sibling: the read then answered with the FIRST one, silently -- a caller that
    asked for a name and got a paragraph it did not choose.
    """
    ws, db, patcher = make_env(tmp_path)

    refused = patcher.read_block("docs/dup.md::Details")

    assert refused["status"] == "error", refused
    assert "names 2 blocks" in refused["error"]
    assert [candidate["name"] for candidate in refused["candidates"]] == [
        "Details",
        "Details (2)",
    ]
    # The printed rank is an address of its own, and it reaches the SECOND paragraph.
    second = patcher.read_block("docs/dup.md::Reprise::Details (2)")
    assert second["status"] == "success", second
    assert "deux" in second["numbered_text"]
    first = patcher.read_block("docs/dup.md::Reprise::Details")
    assert "un" in first["numbered_text"] and "deux" not in first["numbered_text"]


def test_the_parameter_list_discriminates_an_overload(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    refused = patcher.read_block("src/ov.cs::Over::Do")
    assert refused["status"] == "error", refused
    assert "names 2 blocks" in refused["error"]
    assert [candidate["name"] for candidate in refused["candidates"]] == [
        "Do(string, int)",
        "Do(string)",
    ]

    typed = patcher.read_block("src/ov.cs::Over::Do(string)")
    assert typed["status"] == "success", typed
    assert "public int Do(string a) { return 2; }" in typed["numbered_text"]


def test_the_overload_hash_never_appears_in_a_response(tmp_path: Path) -> None:
    """The stored discriminator stays in the index: it is not a value a caller can judge."""
    ws, db, patcher = make_env(tmp_path)

    # The index really does carry the hash: the test would prove nothing otherwise.
    with db.get_connection() as conn:
        stored = [
            str(row[0])
            for row in conn.execute("SELECT id FROM symbols WHERE file_path = ?", ("src/ov.cs",))
        ]
    assert any(OVERLOAD_HASH.search(identifier) for identifier in stored), stored

    responses = [
        patcher.read_block("src/ov.cs"),
        patcher.read_block("src/ov.cs::Over::Do(string)"),
        patcher.read_block("src/ov.cs::Over::Do"),
        patcher.edit_block(
            "src/ov.cs::Over::Do(string)",
            ops=[{"replace": [1, 1], "content": "        public int Do(string a) { return 3; }"}],
            expected_hash=patcher.read_block("src/ov.cs::Over::Do(string)")["content_hash"],
        ),
    ]
    for response in responses:
        assert not OVERLOAD_HASH.search(json.dumps(response)), response


def test_the_legacy_slug_grammar_is_still_accepted_on_input(tmp_path: Path) -> None:
    """'#' is gone from what the surface PRINTS; a path already written with it still works."""
    ws, db, patcher = make_env(tmp_path)

    legacy = patcher.read_block("docs/guide.md#quick-start")
    printed = patcher.read_block("docs/guide.md::Quick Start")

    assert legacy["status"] == "success", legacy
    assert legacy["content_hash"] == printed["content_hash"]
    assert "#quick-start" not in json.dumps(patcher.read_block("docs/guide.md"))


def test_the_edit_path_accepts_the_same_address_as_the_read(tmp_path: Path) -> None:
    """ONE resolver for both: the path that was read is the path that is written."""
    ws, db, patcher = make_env(tmp_path)
    path = "docs/guide.md::Quick Start::New Features"
    read = patcher.read_block(path)

    written = patcher.edit_block(
        path,
        ops=[{"replace": [1, 1], "content": "### Nouvelles fonctionnalites"}],
        expected_hash=read["content_hash"],
    )

    assert written["status"] == "success", written
    # The reply names the address the caller used, never the stored one.
    assert written["block_id"] == path
    assert not OVERLOAD_HASH.search(json.dumps(written))
    assert "Nouvelles fonctionnalites" in (ws / "docs" / "guide.md").read_text(encoding="utf-8")

def test_the_printed_path_seats_a_child_like_the_stored_id(tmp_path: Path) -> None:
    """A paragraph path READ fine and was refused as a PARENT: insert_block answered only to
    the stored `#slug`, which the surface no longer prints. One vocabulary means both
    directions speak it."""
    ws, db, patcher = make_env(tmp_path)

    made = patcher.insert_block("docs/guide.md::Quick Start", text="Etape\ncorps")

    assert made["status"] == "success", made
    text = (ws / "docs" / "guide.md").read_text(encoding="utf-8")
    assert text.index("Etape") > text.index("## Quick Start")


def test_the_full_ancestor_chain_seats_a_child_too(tmp_path: Path) -> None:
    """The path the surface prints for a nested paragraph carries every ancestor."""
    ws, db, patcher = make_env(tmp_path)

    made = patcher.insert_block("docs/guide.md::Guide::Quick Start", text="Etape\ncorps")

    assert made["status"] == "success", made
    assert (ws / "docs" / "guide.md").read_text(encoding="utf-8").count("Etape") == 1


def test_the_printed_path_places_a_sibling_through_the_anchor(tmp_path: Path) -> None:
    """`anchor` names a child, so it is a path like every other name on the surface."""
    ws, db, patcher = make_env(tmp_path)

    made = patcher.insert_block(
        "docs/guide.md::Guide", where="docs/guide.md::Quick Start", text="Apres\ncorps"
    )

    assert made["status"] == "success", made
    text = (ws / "docs" / "guide.md").read_text(encoding="utf-8")
    assert text.index("Apres") > text.index("corps", text.index("## Quick Start"))

def test_a_bare_name_is_refused_as_a_path_and_points_at_the_block(tmp_path: Path) -> None:
    """`insert_block(parent='A')` answered "it does not exist yet -- create a FILE named A
    in '.'" about a class that lives one `::` away. The refusal names that path."""
    ws, db, patcher = make_env(tmp_path)

    refused = patcher.insert_block("A", text="def o(self):\n    pass")

    assert refused["status"] == "error", refused
    assert "is a NAME, not a path" in refused["error"]
    assert "src/a.py::A" in refused["actionable_hint"]
    assert not (ws / "A").exists()


def test_an_unknown_name_still_says_how_to_create_it(tmp_path: Path) -> None:
    """The advice to create the file is right when NOTHING carries that name."""
    ws, db, patcher = make_env(tmp_path)

    refused = patcher.insert_block("Nowhere", text="Nowhere\ncorps")

    assert "does not exist yet" in refused["error"]
    assert "insert_block(parent='.', text='Nowhere')" in refused["actionable_hint"]

def test_a_zero_parameter_overload_prints_and_reads_with_its_parens(tmp_path: Path) -> None:
    """The listing printed a bare `Do` for the zero-parameter overload while its sibling
    printed `Do(string a, int b)`: the bare one read back as ambiguous, so the surface
    printed a path it then refused. `()` is a parameter list, not the absence of one."""
    ws, db, patcher = make_env(tmp_path)
    (ws / "src" / "ov.cs").write_text(
        CSHARP.replace(
            "public int Do(string a, int b) { return 1; }",
            "public int Do() { return 0; }\n        public int Do(string a, int b) { return 1; }",
        ),
        encoding="utf-8",
    )
    patcher.indexer.index_workspace()

    listing = patcher.read_block("src/ov.cs", answer_format="json")
    holder = next(child for child in listing["children"] if child["name"] == "N")
    klass = next(child for child in holder["children"] if child["name"] == "Over")
    names = [child["name"] for child in klass["children"]]
    assert "Do()" in names, names

    read = patcher.read_block("src/ov.cs::Over::Do()")
    assert read["status"] == "success", read
    assert "public int Do()" in read["numbered_text"]


def test_the_displayed_path_of_an_overload_never_carries_the_stored_hash(tmp_path: Path) -> None:
    """`search_symbols` was the ONE surface that printed the sha1 -- on a real query, 19 lines
    out of 20. What is displayed must also be what re-reads."""
    ws, db, patcher = make_env(tmp_path)

    rows = db.search_symbols("Do", file_pattern="src/ov.cs", limit=10)
    assert rows

    for row in rows:
        displayed = patcher.displayed_symbol_id(row["symbol_id"])
        assert "#" not in displayed, displayed
        assert patcher.read_block(displayed)["status"] == "success", displayed


def test_a_parent_outside_the_workspace_says_so(tmp_path: Path) -> None:
    """`insert_block(parent="../outside")` answered "No document or section has the identifier"
    -- a cause `read_block` never gives. A path that leaves the workspace is not a name nobody
    has yet, and the refusal that hides it invents a fact the code never checked."""
    ws, db, patcher = make_env(tmp_path)

    refused = patcher.insert_block("../outside", text="x.md\n# X")

    assert refused["status"] == "error", refused
    assert "outside the workspace root" in refused["error"], refused
    assert not (tmp_path / "outside").exists()

def test_two_declarations_of_one_name_are_ranked_and_the_rank_reads_back(tmp_path: Path) -> None:
    """The real repository declares a partial class twice, each half holding the same member
    signature: the parameter list discriminated nothing, and the refusal named two candidates
    whose `path` was identical to the character -- while advising the caller to "call
    read_block with the one you want". A rank makes the advice followable."""
    ws, db, patcher = make_env(tmp_path)
    (ws / "src" / "part.cs").write_text(PARTIAL_PAIR, encoding="utf-8")
    patcher.indexer.index_workspace()

    ambiguous = patcher.read_block("src/part.cs::N::Mapper::GetColumnName(string)")

    assert ambiguous["status"] == "error", ambiguous
    paths = [candidate["path"] for candidate in ambiguous["candidates"]]
    assert paths == [
        "src/part.cs::N::Mapper::GetColumnName",
        "src/part.cs::N::Mapper (2)::GetColumnName",
    ], paths
    # And every path the refusal names is an address that reads back.
    for path in paths:
        read = patcher.read_block(path)
        assert read["status"] == "success", (path, read)

def test_a_member_needs_a_container_and_the_printed_chain_finds_it(tmp_path: Path) -> None:
    """Two faces of one defect.

    `insert_symbol("src/ov.cs", code="public void NewMethod() { }")` answered SUCCESS with
    syntax_ok=True and no id, after writing the method beside the namespace -- code the C#
    compiler refuses. And `src/ov.cs::N::Over`, the path this surface PRINTS, resolved to
    nothing in the code route (the lookup was a stored-id lookup), so the insert silently
    fell back to the FILE and landed outside the type.
    """
    ws, db, patcher = make_env(tmp_path)

    refused = patcher.insert_symbol("src/ov.cs", code="public void NewMethod() { }")

    assert refused["status"] == "error", refused
    assert "MEMBER declaration" in refused["error"]
    assert "src/ov.cs::N::Over" in refused["actionable_hint"]
    assert "NewMethod" not in (ws / "src" / "ov.cs").read_text(encoding="utf-8")

    seated = patcher.insert_symbol("src/ov.cs::N::Over", code="public void Added() { }")

    assert seated["status"] == "success", seated
    text = (ws / "src" / "ov.cs").read_text(encoding="utf-8")
    assert "public void Added()" in text
    # Inside the class, not beside the namespace: four spaces deeper than the declaration.
    assert "        public void Added()" in text


def test_deleting_a_paragraph_names_the_children_it_reparents(tmp_path: Path) -> None:
    """The description said a deletion removes "everything it holds". For a document that is
    false -- a paragraph's span is its heading and its OWN text, so the paragraphs under it
    survive and move up -- and the reply said nothing about what it had just reparented."""
    ws, db, patcher = make_env(tmp_path)

    removed = patcher.delete_block("docs/guide.md::Quick Start")

    assert removed["status"] == "success", removed
    assert removed["reparented"] == ["New Features", "New Features"], removed
    assert "### New Features" in (ws / "docs" / "guide.md").read_text(encoding="utf-8")


def test_delete_block_removes_a_paragraph_named_by_its_printed_path(tmp_path: Path) -> None:
    """The announced form has to work in BOTH directions: `read_block('d.md::A')` answered
    with the paragraph while `delete_block('d.md::A')` answered "Section ... not found in
    graph database" -- a false cause, since the section was right there."""
    ws, db, patcher = make_env(tmp_path)

    removed = patcher.delete_block("docs/guide.md::Quick Start")

    assert removed["status"] == "success", removed
    assert "## Quick Start" not in (ws / "docs" / "guide.md").read_text(encoding="utf-8")


def test_delete_block_names_a_directory_and_a_document_root_as_such(tmp_path: Path) -> None:
    """Neither is "not found in graph database": both READ fine, they are simply not a block
    this tool removes."""
    ws, db, patcher = make_env(tmp_path)

    directory = patcher.delete_block("docs")
    root = patcher.delete_block("docs/guide.md")

    assert "is a DIRECTORY" in directory["error"], directory
    assert "is the DOCUMENT ITSELF" in root["error"], root
    assert (ws / "docs" / "guide.md").is_file()

def test_two_sibling_paragraphs_of_one_title_print_two_addresses(tmp_path: Path) -> None:
    """The symbol printer has carried a rank for a shared name since this night; the paragraph
    printer printed `docs/guide.md::New Features` TWICE, so the second paragraph had no address
    at all -- the ambiguity refusal named it, but nothing the surface printed could reach it."""
    ws, db, patcher = make_env(tmp_path)

    rows = db.get_document_sections("docs/guide.md")
    printed = [
        patcher.displayed_section_id(str(row["id"]))
        for row in rows
        if str(row.get("heading")) == "New Features"
    ]

    assert len(printed) >= 3, printed
    assert len(set(printed)) == len(printed), printed
    for path in printed:
        assert patcher.read_block(path)["status"] == "success", path

def test_a_name_several_blocks_carry_is_counted_honestly(tmp_path: Path) -> None:
    """The refusal announced "1 block(s)" for a title THREE paragraphs carry: the count was
    taken after de-duplicating the printed paths, and two paragraphs of one title print the
    same short path. The count is a fact about the workspace; the list of paths is a fact about
    what can be typed -- they must not be the same number by accident."""
    ws, db, patcher = make_env(tmp_path)

    refused = patcher.read_block("New Features")

    assert refused["status"] == "error", refused
    assert "3 block(s)" in refused["error"], refused
    assert "docs/guide.md::New Features" in refused["actionable_hint"], refused

def test_a_bare_name_is_not_a_path_and_read_block_names_the_block(tmp_path: Path) -> None:
    """`read_block('A')` answered "check the spelling" about a class that exists: no typo,
    just a name written where a path belongs. The refusal names the path now."""
    ws, db, patcher = make_env(tmp_path)

    refused = patcher.read_block("A")

    assert refused["status"] == "error", refused
    assert "is a NAME, not a path" in refused["error"]
    assert "src/a.py::A" in refused["actionable_hint"]


def test_read_block_depth_parameter_on_directory_and_file(tmp_path: Path) -> None:
    """read_block accepts depth to prune nested listing children."""
    ws, db, patcher = make_env(tmp_path)

    # 1. Directory listing with answer_format="json"
    listing_dir = patcher.read_block("src", answer_format="json")
    assert listing_dir["status"] == "success"
    assert any(c["name"] == "a.py" for c in listing_dir["children"])

    # 2. File listing with answer_format="json"
    outline = patcher.read_block("src/a.py", answer_format="json")
    assert outline["status"] == "success"
    assert any(c["name"] == "A" for c in outline["children"])
