"""Every way to CREATE a block, checked one by one.

The tool addresses a PARENT and says where among its children the new block goes.
This file is the inventory: a creation case that is not here does not exist, and a
case that belongs here but cannot be expressed is a hole in the contract.

insert_block(parent_id, where, text)  ->  the id of the new block
  parent_id : the block that receives the child (a document path = the document)
  where     : <the id of a child of parent_id, to insert right after it>
              | parent_id itself, to insert as the FIRST child (no magic string)
  text      : the new block; its FIRST LINE is the title, the rest is the body
"""

from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

# guide (1) > 1. Alpha (2) > 1.1 Detail (3), 1.2 Second (3) ; 2. Beta (2)
FIXTURE = (
    "# Guide\n\n"
    "intro du guide\n\n"
    "## 1. Alpha\ncorps alpha\n\n"
    "### 1.1 Detail\ncorps detail\n\n"
    "### 1.2 Second\ncorps second\n\n"
    "## 2. Beta\ncorps beta\n"
)
NUMBERED = "# Guide\n\n## 1. Alpha\ncorps alpha\n\n## 2. Beta\ncorps beta\n"
FLAT = "Juste du texte.\nDeuxieme ligne.\n"
EMPTY = "\n"
PREAMBLE = "Avant tout.\n\n# Guide\n\ncorps\n"

GUIDE, ALPHA, DETAIL, SECOND, BETA = (
    "d.md#guide", "d.md#alpha", "d.md#alpha/1-1-detail", "d.md#alpha/1-2-second", "d.md#beta",
)

CASES = [
    (
        # "append at the end of the document" = after its last child, whose own
        # subtree is everything that follows it.
        "section de premier niveau, a la fin du document",
        FIXTURE,
        ("d.md", GUIDE, "Gamma\ncorps gamma"),
        ["Guide", "1. Alpha", "1.1 Detail", "1.2 Second", "2. Beta", "Gamma"],
    ),
    (
        "section de premier niveau, avant tout le reste (where = start)",
        FIXTURE,
        ("d.md", "d.md", "Gamma\ncorps gamma"),
        ["Gamma", "Guide", "1. Alpha", "1.1 Detail", "1.2 Second", "2. Beta"],
    ),
    (
        "enfant d une section, en tete de ses enfants",
        FIXTURE,
        (ALPHA, ALPHA, "1.0 Zero\ncorps"),
        ["Guide", "1. Alpha", "1.0 Zero", "1.1 Detail", "1.2 Second", "2. Beta"],
    ),
    (
        "enfant d une section, en fin de ses enfants",
        FIXTURE,
        (ALPHA, SECOND, "1.3 Troisieme\ncorps"),
        ["Guide", "1. Alpha", "1.1 Detail", "1.2 Second", "1.3 Troisieme", "2. Beta"],
    ),
    (
        "enfant insere entre deux enfants",
        FIXTURE,
        (ALPHA, DETAIL, "1.15 Quinze\ncorps"),
        ["Guide", "1. Alpha", "1.1 Detail", "1.15 Quinze", "1.2 Second", "2. Beta"],
    ),
    (
        "petit-enfant : le niveau suit le parent nomme",
        FIXTURE,
        (DETAIL, DETAIL, "Plus profond\ncorps"),
        ["Guide", "1. Alpha", "1.1 Detail", "Plus profond", "1.2 Second", "2. Beta"],
    ),
    (
        "un marqueur # dans le titre est accepte et ignore",
        FIXTURE,
        (ALPHA, ALPHA, "### Nouveau\ncorps"),
        ["Guide", "1. Alpha", "Nouveau", "1.1 Detail", "1.2 Second", "2. Beta"],
    ),
    (
        "le numero est attribue d apres le frere precedent, et le suivant decale",
        NUMBERED,
        (GUIDE, ALPHA, "Gamma\ncorps"),
        ["Guide", "1. Alpha", "2. Gamma", "3. Beta"],
    ),
    (
        "un titre qui porte deja un numero le garde",
        NUMBERED,
        (GUIDE, ALPHA, "9. Gamma\ncorps"),
        ["Guide", "1. Alpha", "9. Gamma", "2. Beta"],
    ),
    (
        "document sans aucun titre : la section cree le premier titre",
        FLAT,
        ("d.md", "d.md", "Titre\ncorps"),
        # the text no heading owns stays first, as a Preamble section
        ["Preamble", "Titre"],
    ),
    (
        "document vide : la section cree le premier titre",
        EMPTY,
        ("d.md", "d.md", "Titre\ncorps"),
        ["Titre"],
    ),
    (
        "document a preambule : la section se place apres le texte propre",
        PREAMBLE,
        ("d.md", "d.md", "Titre\ncorps"),
        ["Preamble", "Titre", "Guide"],
    ),
]


def make_env(base: Path, index: int, document: str):
    ws = base / ("case" + str(index))
    ws.mkdir(parents=True)
    (ws / "d.md").write_text(document, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, db, Patcher(config, db, indexer)


@pytest.mark.parametrize("label,document,call,expected", CASES, ids=[case[0] for case in CASES])
def test_creation_case(tmp_path: Path, label, document, call, expected) -> None:
    index = [case[0] for case in CASES].index(label)
    ws, db, patcher = make_env(tmp_path, index, document)

    result = patcher.insert_block(*call)

    assert result["status"] == "success", (label, result)
    assert [section["heading"] for section in db.get_document_sections("d.md")] == expected, label
    assert result["block_id"] == result["created_section_ids"][0]


def test_a_text_that_carries_headings_creates_several_blocks(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, 90, FIXTURE)

    result = patcher.insert_block("d.md", "d.md", "Bloc\n### Sous un\ncorps\n\n### Sous deux\ncorps")

    assert result["status"] == "success", result
    assert [section["heading"] for section in db.get_document_sections("d.md")] == [
        "Bloc", "Sous un", "Sous deux", "Guide", "1. Alpha", "1.1 Detail", "1.2 Second", "2. Beta",
    ]
    assert len(result["created_section_ids"]) == 3
    assert result["block_id"] == "d.md#bloc"


def test_the_new_block_id_is_returned(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, 91, FIXTURE)

    result = patcher.insert_block(ALPHA, ALPHA, "1.0 Zero\ncorps")

    assert result["block_id"] == result["created_section_ids"][0]
    assert db.get_document_section(result["block_id"])["heading"] == "1.0 Zero"


def test_where_must_be_a_child_of_the_parent(tmp_path: Path) -> None:
    """`1.1 Detail` is a grandchild of guide: the new block would not be its sibling.

    The refusal names the parent that actually holds it -- the file knows it -- so
    the caller does not have to guess it or read the whole outline to find it.
    """
    ws, db, patcher = make_env(tmp_path, 92, FIXTURE)
    before = (ws / "d.md").read_text(encoding="utf-8")

    result = patcher.insert_block(GUIDE, DETAIL, "Nouveau\ncorps")

    assert result["status"] == "error"
    assert "child" in result["error"]
    assert result["parent_of_where"] == ALPHA
    assert "d.md#alpha" in result["actionable_hint"]
    assert (ws / "d.md").read_text(encoding="utf-8") == before


def test_names_are_resolved_in_the_file_not_in_the_stored_table(tmp_path: Path) -> None:
    """The table may lag the file: an id is a name, the file says where it is.

    A heading is written on disk without re-indexing, so every stored order_index
    is one position behind. Resolved by position, the call refused or wrote beside
    the wrong paragraph; resolved by name, it lands where the caller asked.
    """
    ws, db, patcher = make_env(tmp_path, 96, FIXTURE)
    path = ws / "d.md"
    path.write_text("# Avant\n\ncorps avant\n\n" + FIXTURE, encoding="utf-8")

    result = patcher.insert_block(ALPHA, SECOND, "1.3 Troisieme\ncorps")

    assert result["status"] == "success", result
    assert result["block_id"] == "d.md#alpha/1-3-troisieme"
    text = path.read_text(encoding="utf-8")
    assert (
        text.index("1.1 Detail")
        < text.index("1.2 Second")
        < text.index("1.3 Troisieme")
        < text.index("2. Beta")
    )


def test_a_block_without_a_title_is_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, 93, FIXTURE)

    result = patcher.insert_block(GUIDE, GUIDE, "   \n  \n")

    assert result["status"] == "error"
    assert "title" in result["error"]


def test_a_duplicate_title_in_the_same_parent_is_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, 94, FIXTURE)
    before = (ws / "d.md").read_text(encoding="utf-8")

    result = patcher.insert_block(GUIDE, ALPHA, "2. Beta\ncorps")

    assert result["status"] == "error"
    assert "already exists" in result["error"]
    assert (ws / "d.md").read_text(encoding="utf-8") == before




FENCED = (
    "# Guide\n\n## Deploying\n\n```bash\n# install\n# export\n```\n\n"
    "### 1. Packaging\n\nbody\n\n### 2. Submitting via CLI\n\nbody\n\n"
    "## Best Practices\n\nbody\n"
)


def test_a_code_fence_does_not_reparent_the_sections_below_it(tmp_path: Path) -> None:
    """The bug behind mapab2 b r1: the "# " comments made the real sections orphans."""
    ws, db, patcher = make_env(tmp_path, 97, FENCED)

    result = patcher.insert_block(
        "d.md#deploying", "d.md#deploying/submitting-via-cli", "3. Partner Compliance\ncorps"
    )

    assert result["status"] == "success", result
    assert result["parent_id"] == "d.md#deploying"
    assert result["level"] == 3


def test_an_unknown_parent_is_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, 95, FIXTURE)

    result = patcher.insert_block("d.md#nope", "d.md#nope", "Nouveau\ncorps")

    assert result["status"] == "error"
    assert "No document or section" in result["error"]


def test_the_parent_id_selects_the_first_position(tmp_path: Path) -> None:
    """Agents pass the parent as its own `where`, meaning "its first child".

    There is no 'start' string any more. Agents already passed the parent as its own
    where argument, meaning "its first child", on 6 of 21 campaign runs (a r3/r5/r8,
    b r0/r1/r4); the old interface refused it, costing a turn each time.
    """
    ws, db, patcher = make_env(tmp_path, 98, FIXTURE)

    result = patcher.insert_block(ALPHA, ALPHA, "1.0 Zero\ncorps")

    assert result["status"] == "success", result
    assert [section["heading"] for section in db.get_document_sections("d.md")] == [
        "Guide", "1. Alpha", "1.0 Zero", "1.1 Detail", "1.2 Second", "2. Beta",
    ]
    # A document path used as its own `where` is the same "first child".
    second = patcher.insert_block("d.md", "d.md", "Avant tout\ncorps")
    assert second["status"] == "success", second


def test_where_may_be_omitted_to_insert_first(tmp_path: Path) -> None:
    """The resilient form: insert_block(parent_id, text=...) inserts first.

    where is optional; omitting it -- like passing the parent id -- means "first child",
    so a caller that only wants to prepend needs no magic value.
    """
    ws, db, patcher = make_env(tmp_path, 99, FIXTURE)

    result = patcher.insert_block(ALPHA, text="1.0 Zero\ncorps")

    assert result["status"] == "success", result
    assert [section["heading"] for section in db.get_document_sections("d.md")] == [
        "Guide", "1. Alpha", "1.0 Zero", "1.1 Detail", "1.2 Second", "2. Beta",
    ]
