"""Tests for Markdown mutation guards: no nested/deduplicated headings, and a delete primitive."""

import json
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher
from ast_graph_server.server import ASTGraphServer

MD = "# Project Title\n\nIntro paragraph.\n\n## Section A\n\nBody A.\n\n## Section B\n\nBody B.\n"

# A changelog repeats the same sub-heading under each released version.
MD_CHANGELOG = (
    "# Changelog\n\n"
    "## v1\n\n### New Features\n\nFirst release notes.\n\n"
    "## v2\n\n### New Features\n\nSecond release notes.\n"
)


def make_env(tmp_path: Path, content: str = MD):
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "spec.md").write_text(content, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, db, Patcher(config, db, indexer)


def test_update_with_a_heading_replaces_the_section_instead_of_nesting(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.update_document_section("docs/spec.md#1", "## Section A renamed\n\nNew body.")

    assert result["status"] == "success", result
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "## Section A renamed\n\nNew body." in text
    assert "## Section A\n\n## Section A renamed" not in text
    assert "Body A." not in text
    assert text.count("Section A") == 1
    assert "## Section B\n\nBody B." in text


def test_update_body_only_still_preserves_the_heading(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.update_document_section("docs/spec.md#1", "New body A.\n\nSecond paragraph.")

    assert result["status"] == "success", result
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "## Section A\n\nNew body A.\n\nSecond paragraph." in text


def test_update_to_a_heading_used_by_another_section_is_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.update_document_section("docs/spec.md#1", "## Section B\n\nSomething else.")

    assert result["status"] == "error"
    assert "already exists" in result["error"]
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == MD


def test_insert_duplicate_heading_is_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("docs/spec.md#project-title", "docs/spec.md#1", "Section B\nDuplicate.")

    assert result["status"] == "error"
    assert "already exists" in result["error"]
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == MD


def test_insert_distinct_heading_is_allowed(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("docs/spec.md#project-title", "docs/spec.md#1", "Section C\nBody C.")

    assert result["status"] == "success", result
    assert "## Section C\n\nBody C." in (ws / "docs" / "spec.md").read_text(encoding="utf-8")


def test_update_with_identical_content_is_a_noop(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    path = ws / "docs" / "spec.md"
    before = path.read_text(encoding="utf-8")

    result = patcher.update_document_section("docs/spec.md#1", "Body A.")

    assert result["status"] == "noop", result
    assert path.read_text(encoding="utf-8") == before


def test_delete_document_section_removes_heading_and_body(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.delete_document_section("docs/spec.md#1")

    assert result["status"] == "success", result
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "Section A" not in text
    assert "Body A." not in text
    assert "## Section B\n\nBody B." in text
    assert [section["heading"] for section in db.get_document_sections("docs/spec.md")] == [
        "Project Title", "Section B"
    ]



MD_NUMBERED = (
    "# Roadmap\n\n## Operators\n\n### 2.1 Fully Implemented\n\nbody\n\n"
    "### 2.2 Extended Features\n\nbody\n\n### 2.3 Not Implemented\n\nbody\n"
)


MD_NUMBERED_INSERT = (
    "# Guide\n\n## Part\n\n### Architecture\n\nbody\n\n"
    "### 1. Worker Init Script\n\nbody\n\n### 2. Packaging\n\nbody\n"
)


MD_EDGE = "# Guide\n\n## Part\n\nligne un\nligne deux\nligne trois\nligne quatre\nligne cinq\n"


def test_a_replacement_that_repeats_the_following_line_is_reported(tmp_path: Path) -> None:
    """`replace [3,4]` ending on line 5's text leaves that line in the file twice.

    Six edits across four runs did exactly this, and the caller blamed the tool for the copy:
    "the tool duplicated the boundary line" (mapab2 a r1 of the base 6a56161, and the same
    shape on the bases f1fb645, c819545 and 957f704). The range is inclusive; the tool now
    points at the duplicate instead of leaving the caller to find it in the returned text.
    """
    ws, db, patcher = make_env(tmp_path, MD_EDGE)
    block_id = "docs/spec.md#part"
    read = patcher.read_block(block_id)

    result = patcher.edit_block(
        block_id,
        ops=[{"replace": [3, 4], "content": "ligne un bis\nligne trois\nligne quatre"}],
        expected_hash=read["content_hash"],
    )

    # REFUSED, not applied-and-warned: four occurrences in one run (base f831a4e) left the file
    # duplicated until a second call repaired it, and the agent's own words were "a warning that
    # accompanies a successful write is easy to miss and leaves the artifact broken in the
    # meantime". The refusal costs the same turn as the repair and never writes the wrong thing.
    assert result["status"] == "error", result
    assert result["duplicated_line"] == "ligne trois"
    assert "INCLUSIVE" in result["error"]
    # The block is untouched, and its CURRENT numbered text comes back with the refusal, so the
    # repaired call needs no read_block.
    assert "ligne trois" in result["numbered_text"]
    assert patcher.read_block(block_id)["content_hash"] == read["content_hash"]

    # A line that was ALREADY repeated is not a copy the edit made: a C# block is full of
    # "/// </summary>", and comparing boundaries once told an agent its correct edit had
    # duplicated that comment, which the agent verified and reported (base a20a845).
    already = patcher.edit_block(
        block_id,
        ops=[{"replace": [3, 3], "content": "ligne un modifie"}],
        expected_hash=read["content_hash"],
    )
    assert already["status"] == "success", already

    # And the two copies must sit NEXT TO each other: two identical statements far apart are
    # two real statements. Warning about those was the second false positive of this check
    # (declared on mapab2 a r1 of the base 10ad4d5, about "builder = builder").


def test_two_copies_far_apart_are_not_a_duplicate(tmp_path: Path) -> None:
    """Two identical statements far apart are two statements, not a copy.

    "builder = builder" appeared once and twice after the edit, sixteen lines apart, both
    real statements of a fluent chain: the agent reported the warning as a false positive
    (declared on mapab2 a r1 of the base 10ad4d5).
    """
    ws, db, patcher = make_env(tmp_path, "# Doc\n\n## Part\n\nligne un\nligne deux\nligne trois\n")
    block_id = "docs/spec.md#part"
    read = patcher.read_block(block_id)

    result = patcher.edit_block(
        block_id,
        ops=[
            {
                "replace": [3, 3],
                "content": "ligne un\ninsertion a\ninsertion b\ninsertion c\ninsertion d\nligne un",
            }
        ],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    assert not any("twice" in warning for warning in (result.get("warnings") or []))

def test_the_renumbering_of_following_siblings_is_reported(tmp_path: Path) -> None:
    """The tool rewrites headings the caller never mentioned: it says which.

    An agent saw `1. Worker Init Script` become `2.`, got no warning, re-listed the whole
    document to find out, and spent four turns restoring numbers the tool keeps coherent on
    purpose -- twice in one run (declared on mapab2 a r1 of the base e3a8c08: "silently
    renumbered the pre-existing sibling headings").
    """
    ws, db, patcher = make_env(tmp_path, MD_NUMBERED_INSERT)

    result = patcher.insert_block(
        "docs/spec.md#part", "docs/spec.md#part/architecture", "Authentication\n\nbody"
    )

    assert result["status"] == "success", result
    assert result["number"] == 1
    assert result["heading"] == "1. Authentication"
    assert len(result["warnings"]) == 1
    assert "renumbered the sibling headings" in result["warnings"][0]
    assert "'1. Worker Init Script' -> '2. Worker Init Script'" in result["warnings"][0]
    assert "### 2. Worker Init Script" in (ws / "docs" / "spec.md").read_text(encoding="utf-8")

def test_a_two_level_number_warns_about_the_number_it_collides_with(tmp_path: Path) -> None:
    """`2.3` collides with `2.3`, not with `2.1` and `2.2`.

    The warning compared only the leading digit: "section number '2.' is already used by:
    2.1 ...; 2.2 ...; 2.3 ..." names two headings that do not conflict and buries the one
    that does. The agent reported it as ambiguous, on the same call whose success it failed
    to notice (mapab2 b r2 of the base 957f704).
    """
    ws, db, patcher = make_env(tmp_path, MD_NUMBERED)

    result = patcher.insert_block(
        "docs/spec.md#operators",
        "docs/spec.md#operators/2-2-extended-features",
        "2.3 PWAF Compliance\n\nbody",
    )

    assert result["status"] == "success", result
    assert result["warnings"] == ["section number '2.3' is already used by: 2.3 Not Implemented"]


def test_a_path_that_is_not_indexed_says_so(tmp_path: Path) -> None:
    """A file nobody parses is not a file that does not exist -- and it can be READ.

    An agent asked for `deploy_and_run_databricks.ps1`, got the bare "No symbol or Markdown
    section has this identifier", and reported that it "did not indicate that the file is
    simply unindexed (as opposed to non-existent)" (mapab2 b r2 of the base 957f704). The
    refusal that answered it named the cause and left NO route: on three consecutive runs of
    the base dee367d the same script cost about 3 turns each, spent looking for a reader this
    arm did not have (the baseline arm has read_file). A file no parser and no document reader
    claims now returns its text, numbered and READ-ONLY.
    """
    ws, db, patcher = make_env(tmp_path)
    (ws / "docs" / "deploy.ps1").write_text("Write-Host hi\n", encoding="utf-8")

    script = patcher.read_block("docs/deploy.ps1")
    assert script["status"] == "success", script
    assert script["read_only"] is True and script["kind"] == "file_text"
    assert "Write-Host hi" in script["numbered_text"]

    missing = patcher.read_block("docs/nowhere.ps1")
    assert missing["status"] == "error"
    assert "no file on disk has this path either" in missing["error"]

def test_deleting_one_repeated_heading_does_not_repoint_another(tmp_path: Path) -> None:
    """A name must never change meaning because a sibling disappeared elsewhere.

    Measured relation on mapab2 b r1 of the base edfe80a: after the agent deleted its
    own duplicate `### New Features` under `[1.3.2-dev]`, `#new-features-2` designated
    the `[1.3.1]` paragraph, so a read taken after the deletion described another
    release. A deleted name must name nothing, and say so.
    """
    ws, db, patcher = make_env(tmp_path, MD_CHANGELOG)

    features = [
        row["id"] for row in db.get_document_sections("docs/spec.md")
        if row["heading"] == "New Features"
    ]
    assert features == [
        "docs/spec.md#v1/new-features",
        "docs/spec.md#v2/new-features",
    ]

    assert patcher.delete_document_section(features[0])["status"] == "success"
    # The surviving twin keeps the name it had, and the deleted name is not found.
    assert patcher.read_block(features[1])["status"] == "success"
    gone = patcher.read_block(features[0])
    assert gone["status"] == "error"

def test_repeated_heading_under_a_new_parent_is_allowed(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, MD_CHANGELOG)

    # Add a new version section, then its own "New Features" child. The title
    # already exists under v1 and v2, but not under v3, so this must be allowed.
    assert patcher.insert_block(
        "docs/spec.md#changelog", "docs/spec.md#v2", "v3\nThird release."
    )["status"] == "success"
    v3 = next(s for s in db.get_document_sections("docs/spec.md") if s["heading"] == "v3")
    result = patcher.insert_block(v3["id"], v3["id"], "New Features\nThird release features.")

    assert result["status"] == "success", result
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8").count("### New Features") == 3


def test_same_scope_duplicate_heading_is_still_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, MD_CHANGELOG)
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    # Appending another "### New Features" lands under v2, which already has one.
    result = patcher.insert_block("docs/spec.md#v2", "docs/spec.md#v2", "New Features\nDuplicate.")

    assert result["status"] == "error"
    assert "already exists" in result["error"]
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before


def test_update_with_a_shifted_identifier_is_refused(tmp_path: Path) -> None:
    """Identifiers are positional: after an insertion, a stale id points elsewhere.

    Without the expected-heading guard the tool silently rewrote the wrong
    section and reported success.
    """
    ws, db, patcher = make_env(tmp_path)
    patcher.insert_block("docs/spec.md#project-title", "docs/spec.md#1", "Section A bis\nBody.")

    result = patcher.update_document_section("docs/spec.md#2", "New body.", expected_heading="Section B")

    assert result["status"] == "error"
    assert "Section A bis" in result["error"]
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "New body." not in text
    # with the heading that really sits there, the same call goes through
    assert patcher.update_document_section(
        "docs/spec.md#2", "New body.", expected_heading="Section A bis"
    )["status"] == "success"


def test_delete_with_a_shifted_identifier_is_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    patcher.insert_block("docs/spec.md#project-title", "docs/spec.md#1", "Section A bis\nBody.")
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    result = patcher.delete_document_section("docs/spec.md#2", expected_heading="Section B")

    assert result["status"] == "error"
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before


def test_insert_reports_every_section_it_created(tmp_path: Path) -> None:
    """A heading inside the content becomes a section: the ids must be reported.

    Without them the agent re-read the outline, read the first id it guessed, saw
    only the intro paragraph, and concluded its content had been truncated.
    """
    ws, db, patcher = make_env(tmp_path)

    result = patcher.insert_block("docs/spec.md#project-title", "docs/spec.md#project-title",
        "PWAF\nIntro.\n\n### Req 1\n\nbody 1\n\n### Req 2\n\nbody 2",
    )

    assert result["status"] == "success"
    ids = result["created_section_ids"]
    assert len(ids) == 3
    assert [db.get_document_section(item)["heading"] for item in ids] == ["PWAF", "Req 1", "Req 2"]


def test_update_reports_the_sections_its_content_creates(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.update_document_section("docs/spec.md#1", "New body.\n\n### Added\n\nsub")

    assert result["status"] == "success"
    headings = [db.get_document_section(item)["heading"] for item in result["resulting_section_ids"]]
    assert headings[0] == "Section A"
    assert "Added" in headings
    created = [db.get_document_section(item)["heading"] for item in result["created_section_ids"]]
    assert created == ["Added"]
    assert not any("new section of the same level" in warning for warning in result["warnings"])


def test_a_plain_body_edit_creates_no_section(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.update_document_section("docs/spec.md#1", "Just a new body.")

    assert result["status"] == "success"
    assert result["created_section_ids"] == []


# The block whose own level a submitted title takes. A '### 9.' sent to the
# '### 8.' block used to SPLIT it (the reply showed section 8 only, and the walk
# that reports resulting_section_ids stopped at the new sibling); a call that only
# ADDS it is now CREATED as the sibling it is -- see Patcher._title_route.
MD_SPLIT = (
    "# Guide\n\n## Part\n\n"
    "### 8. Databricks PWAF Partner Telemetry (User-Agent)\n- a\n- b\n- c\n\n"
    "### 10. Next Section\n\nBody next.\n"
)


#: The real call, replayed from 13 of the 20 runs of registered_v2: one insert_after
#: carrying a '### 9.' heading, sent to a '### 8.' block. `insert_after 4` is the block's
#: LAST line: only blank lines of the block's own follow it, which is what makes 'after the
#: block' the place the caller asked for (see _title_route_off_point).
SIBLING_OPS = [
    {"insert_after": 4, "content": "\n### 9. Databricks PWAF OAuth Authentication\n- x\n- y"}
]
BLOCK_ID = "docs/spec.md#part/databricks-pwaf-partner-telemetry-user-agent"
SIBLING_ID = "docs/spec.md#part/databricks-pwaf-oauth-authentication"


def test_edit_block_creates_the_sibling_a_title_at_its_own_level_asks_for(tmp_path: Path) -> None:
    """A call that only ADDS a paragraph at the block's level CREATES it, in one turn.

    The real shape, replayed from registered_v2: '### 9.' inserted after a line of the
    '### 8.' block. The refusal cost one turn in 13 runs out of 20, and every one of
    the 13 was repaired on the very next turn by a successful insert_block with the
    parent the hint named -- the whole repair was already known, so refusing spent a
    turn on nothing and carried 322,366 characters to the end of the run. Nothing is
    left to infer here: the heading is a paragraph the caller created, the level it
    must take is the one it carries, and the holder is the one the hint used to name.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    before_hash = read["content_hash"]

    result = patcher.edit_block(BLOCK_ID, ops=SIBLING_OPS, expected_hash=before_hash)

    assert result["status"] == "success", result
    # EVERY field insert_block answers with, so the caller has nothing to guess.
    assert result["block_id"] == SIBLING_ID
    assert result["parent_id"] == "docs/spec.md#part"
    assert result["where"] == BLOCK_ID
    assert result["heading"] == "9. Databricks PWAF OAuth Authentication"
    assert result["level"] == 3
    assert result["number"] == 9
    assert result["created_section_ids"] == [SIBLING_ID]
    assert db.get_document_section(SIBLING_ID) is not None
    # The placement, said out loud, and the guarantee that goes with it.
    assert result["placement"] == "after"
    assert result["host_block_unchanged"] is True
    assert result["host_content_hash"] == before_hash
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert text.index("### 9. Databricks PWAF OAuth Authentication") > text.index("- b")
    # The block kept its own lines: the insert did NOT swallow what followed it.
    assert "### 8. Databricks PWAF Partner Telemetry (User-Agent)\n- a\n- b\n- c\n" in text
    assert text.count("### 9. Databricks PWAF OAuth Authentication") == 1
    # Its hash is still the one the caller sent: the host block did not move.
    assert patcher.read_block(BLOCK_ID)["content_hash"] == before_hash
    assert any("SIBLING" in warning for warning in result["warnings"])
    assert any("renumbered" in warning for warning in result["warnings"])


def test_a_mixed_call_that_would_split_the_section_is_still_refused(tmp_path: Path) -> None:
    """One DELETE beside the insertion: the block itself moves, so nothing is routed."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    result = patcher.edit_block(
        BLOCK_ID,
        ops=SIBLING_OPS + [{"delete": [4, 4]}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "error"
    assert "split" in result["error"]
    assert "insert_block" in result["actionable_hint"]
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before
    assert db.get_document_section(SIBLING_ID) is None


def test_a_title_above_the_block_level_is_still_refused(tmp_path: Path) -> None:
    """'## ' inside a '### ' block: that holder is not this block's, so nothing is inferred."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    result = patcher.edit_block(
        BLOCK_ID,
        ops=[{"insert_after": 3, "content": "\n## 9. Databricks PWAF OAuth Authentication\n- x\n- y"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "error"
    assert "split" in result["error"]
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before
    assert db.get_document_section(SIBLING_ID) is None


def test_two_titles_in_one_insertion_are_still_refused(tmp_path: Path) -> None:
    """Two headings in one insertion: which is the sibling and which the child is a guess."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    result = patcher.edit_block(
        BLOCK_ID,
        ops=[{"insert_after": 3, "content": "\n### 9. First New\n- x\n\n### 11. Second New\n- y"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "error"
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before
    assert db.get_document_section("docs/spec.md#part/first-new") is None


#: A block whose body carries a CLOSED fence: line 5 opens it, line 7 closes it, and
#: '### 9. Fake Heading' (line 6) is CODE the block already carries.
MD_FENCED_BODY = (
    "# Guide\n\n## Part\n\n"
    "### 8. Eight\n\n"
    "text\n\n"
    "```\n"
    "### 9. Fake Heading\n"
    "```\n"
)
FENCED_BLOCK = "docs/spec.md#part/eight"


def test_an_insertion_inside_an_open_fence_is_written_as_code(tmp_path: Path) -> None:
    """D1: the point decides -- after the OPENING fence the new lines are CODE, not a title.

    Measured by the verifier on the frozen revision: this call created a real '9. Boss'
    section elsewhere and wrote the caller's code NOWHERE in the fence. HEAD writes it
    verbatim, and so does this: the title is read from the RESULT, where a heading inside a
    fence is not a heading.
    """
    ws, db, patcher = make_env(tmp_path, MD_FENCED_BODY)
    read = patcher.read_block(FENCED_BLOCK)

    result = patcher.edit_block(
        FENCED_BLOCK,
        ops=[{"insert_after": 5, "content": "\n### 9. Boss\n- x"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    assert not result.get("title_route")
    # INSIDE the fence, between the opening and the closing marker.
    assert "```\n\n### 9. Boss\n- x\n### 9. Fake Heading\n```" in (
        ws / "docs" / "spec.md"
    ).read_text(encoding="utf-8")
    # And nothing was created anywhere: the outline is the one the block already had.
    assert [row["heading"] for row in db.get_document_sections("docs/spec.md")] == [
        "Guide",
        "Part",
        "8. Eight",
    ]


def test_a_title_the_block_carries_only_inside_a_fence_is_never_a_split(tmp_path: Path) -> None:
    """D2: a heading line inside a CLOSED fence is code, so a title equal to it is NEW.

    The old guard read the block's RAW lines, found '### 9. Fake Heading' there -- inside
    the fence -- called it 'already present', and wrote the submitted title INSIDE the
    block: the document carried it twice and the block was cut in two ('...-2'). A title at
    the block's own level is now CREATED as the sibling it is, never written inside.
    """
    ws, db, patcher = make_env(tmp_path, MD_FENCED_BODY)
    read = patcher.read_block(FENCED_BLOCK)

    result = patcher.edit_block(
        FENCED_BLOCK,
        ops=[{"insert_after": 7, "content": "\n### 9. Fake Heading\n- z"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    assert result.get("title_route") is True
    assert result["block_id"] == "docs/spec.md#part/fake-heading"
    rows = db.get_document_sections("docs/spec.md")
    # NO split: '8. Eight' is ONE section, and the new title is a section of its own.
    assert [row["heading"] for row in rows] == [
        "Guide",
        "Part",
        "8. Eight",
        "9. Fake Heading",
    ]
    assert not any(str(row["id"]).endswith("-2") for row in rows)
    # The code the block carries inside its fence is intact.
    assert "```\n### 9. Fake Heading\n```" in (ws / "docs" / "spec.md").read_text(encoding="utf-8")


#: The block edited here is the LAST one of the file: seating a paragraph after it re-lays
#: the end of the file, so the host's OWN text can gain a blank line.
MD_LAST_BLOCK = "# Guide\n\n## Part\n\n### 8. Eight\n- a\n- b\n"


def test_the_host_hash_is_the_one_the_file_holds_now(tmp_path: Path) -> None:
    """D3: no field may be false -- and this one asserted a fact it had not verified.

    Measured by the verifier on the frozen revision: the reply handed back the OLD host
    hash beside 'still opens it', and the next edit sent with that hash answered 'Stale
    read'. The host is now RE-READ after the write, the flag is true only when the file
    says so, and the stamp returned really opens the block.
    """
    ws, db, patcher = make_env(tmp_path, MD_LAST_BLOCK)
    block_id = "docs/spec.md#part/eight"
    read = patcher.read_block(block_id)

    result = patcher.edit_block(
        block_id,
        ops=[{"insert_after": 3, "content": "\n### 9. Nine\n- x"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    current = patcher.read_block(block_id)["content_hash"]
    assert result["host_content_hash"] == current
    assert result["host_block_unchanged"] == (current == read["content_hash"])
    # The measurement of the round: seating after the LAST block DOES move the host.
    assert result["host_block_unchanged"] is False
    assert "is the LAST block of the file" in result["warnings"][0]
    assert current in result["warnings"][0]
    # The stamp the reply hands out OPENS the block: this is what the old reply broke.
    chained = patcher.edit_block(
        block_id,
        ops=[{"replace": [2, 2], "content": "- a changed"}],
        expected_hash=result["host_content_hash"],
    )
    assert chained["status"] == "success", chained


def test_a_call_that_also_writes_into_the_block_is_refused(tmp_path: Path) -> None:
    """D4: two insertions, one title -- the block itself moves, so nothing is routed.

    Creating the section AND rewriting the block are two writes, and a call that can
    half-apply is worse than a refusal. The condition is written in the schema the model
    reads: an unwritten condition behind a refusal is what the verifier found here.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    result = patcher.edit_block(
        BLOCK_ID,
        ops=SIBLING_OPS + [{"insert_after": 2, "content": "- extra"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "error"
    assert "insert_block" in result["actionable_hint"]
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before
    assert db.get_document_section(SIBLING_ID) is None


def test_the_insertion_point_decides_where_the_title_lands(tmp_path: Path) -> None:
    """D5: before, inside and at the end are THREE answers, not one.

    The verifier measured the frozen revision producing the SAME file for insert_after 0
    and insert_after 3, both with placement 'after'. A paragraph at this level is seated
    after the block, so a point the block's own lines still follow cannot be honoured: it
    is refused, and the refusal names the line.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)

    # INSIDE the block: refused, in words that name the point.
    refused = patcher.edit_block(
        BLOCK_ID,
        ops=[{"insert_after": 3, "content": "\n### 9. OAuth Tokens\n- x"}],
        expected_hash=read["content_hash"],
    )
    assert refused["status"] == "error"
    assert "asked for at line 3" in refused["error"]
    assert "cannot be respected" in refused["error"]
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == MD_SPLIT

    # BEFORE the block: honoured where it was asked for, and the block is NOT cut.
    before_block = patcher.edit_block(
        BLOCK_ID,
        ops=[{"insert_after": 0, "content": "\n### 9. OAuth Tokens\n- x"}],
        expected_hash=read["content_hash"],
    )
    assert before_block["status"] == "success", before_block
    assert before_block["resulting_section_ids"] == ["docs/spec.md#part/oauth-tokens"]
    # The block's heading field is ITS heading, not the one that was inserted before it.
    assert before_block["heading"] == "8. Databricks PWAF Partner Telemetry (User-Agent)"
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert text.index("### 9. OAuth Tokens") < text.index("### 8. Databricks")
    assert patcher.read_block(BLOCK_ID)["numbered_text"] == read["numbered_text"]


def test_the_schema_names_every_condition_that_refuses_a_routing() -> None:
    """The conditions the route applies must be the ones the model can READ.

    Two refusals had their condition written down nowhere: a call that also writes into
    the block, and an insertion point the block's own lines still follow. A refusal whose
    cause is not in the text the model reads is a turn lost to a guess.
    """
    from ast_graph_server.toolset_schemas import SCALPEL_SCHEMAS

    description = next(
        spec["function"]["description"]
        for spec in SCALPEL_SCHEMAS
        if spec["function"]["name"] == "edit_block"
    )
    assert "also writes into the block is refused" in description
    assert "the block's own lines still follow" in description
    assert "whose point falls inside an open fence" in description
    assert "host_block_unchanged is true only when it really did not move" in description
    assert "PASSES THE SAME GUARDS" in description


def test_an_insertion_that_leaves_a_fence_unclosed_is_refused(tmp_path: Path) -> None:
    """S0: the route wrote what HEAD refuses, and the rest of the document left the outline.

    The inserted content opens a code fence and never closes it. '10. Next Section' stops
    being a heading, so the document loses a section -- an edit the ordinary path refuses
    (unclosed_fence_line on the RESULT) and that HEAD refuses. The route decided before
    that guard; it now runs it on a DRY RUN of the text it proposes, before any byte moves.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    outline_before = [row["heading"] for row in db.get_document_sections("docs/spec.md")]
    assert "10. Next Section" in outline_before

    result = patcher.edit_block(
        BLOCK_ID,
        ops=[{"insert_after": 4, "content": "\n### 9. Nine\n- x\n```"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "error", result
    assert "fence" in result["error"]
    assert not result.get("title_route")
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before
    # The structure the unclosed fence would have swallowed is intact.
    assert [row["heading"] for row in db.get_document_sections("docs/spec.md")] == outline_before


def test_a_title_inserted_before_the_block_is_never_its_own_duplicate(tmp_path: Path) -> None:
    """S2/S3: the reply compared the block to ITSELF, and guessed where the lines landed.

    With `insert_after 0` the block's own heading moves past `start_offset` while the reply
    still names it, so `_structure_warnings` reported 'section number 8 used by 8. Databricks
    ...' in a document holding exactly ONE '8' -- and the reply also said the lines had
    landed 'after its LAST line' when they landed BEFORE its first. Both are read now.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)

    result = patcher.edit_block(
        BLOCK_ID,
        ops=[{"insert_after": 0, "content": "\n### 9. OAuth Tokens\n- x"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    warnings = " ".join(result.get("warnings") or [])
    # Never the block as a duplicate of itself...
    assert "already used by" not in warnings
    assert "8. Databricks PWAF Partner Telemetry (User-Agent)" not in warnings
    # ...and never a landing claim the call does not support.
    assert "after its LAST line" not in warnings
    assert "BEFORE its first line" in warnings
    assert [row["heading"] for row in db.get_document_sections("docs/spec.md")] == [
        "Guide",
        "Part",
        "9. OAuth Tokens",
        "8. Databricks PWAF Partner Telemetry (User-Agent)",
        "10. Next Section",
    ]


def test_the_route_passes_every_guard_the_ordinary_path_applies(tmp_path: Path) -> None:
    """Walk Patcher.RESULT_GUARDS: one scenario per guard, refused by BOTH paths.

    The route reaches the file by another path (insert_block), so a guard it does not run
    is a guard the tool no longer has. The tuple is the enumeration; this test fails when a
    name is added without a scenario, so the list cannot rot into a comment.
    """
    from ast_graph_server.lineops import apply_ops

    scenarios = {
        # The inserted content opens a code fence and never closes it.
        "unclosed_code_fence": (
            {"insert_after": 4, "content": "\n### 9. Nine\n- x\n```"},
            "fence",
        ),
        # The title a sibling already carries, word for word.
        "duplicate_sibling_title": (
            {"insert_after": 4, "content": "\n### 10. Next Section\n- x"},
            "already exists",
        ),
        # A heading ABOVE the block's level: the holder is not this block's.
        "outline_of_the_edited_block": (
            {"insert_after": 4, "content": "\n## 9. Nine\n- x"},
            "split",
        ),
    }
    assert set(scenarios) == set(Patcher.RESULT_GUARDS), (
        "a guard of RESULT_GUARDS has no scenario, or a scenario names no guard",
        sorted(Patcher.RESULT_GUARDS),
        sorted(scenarios),
    )

    for guard in Patcher.RESULT_GUARDS:
        op, marker = scenarios[guard]

        # The route is CONSULTED by edit_block, so this is the routed path.
        ws, db, patcher = make_env(tmp_path / ("route_" + guard), MD_SPLIT)
        read = patcher.read_block(BLOCK_ID)
        before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
        routed = patcher.edit_block(BLOCK_ID, ops=[op], expected_hash=read["content_hash"])
        assert routed["status"] == "error", (guard, routed)
        assert marker in routed["error"], (guard, routed["error"])
        assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before, guard
        assert db.get_document_section(SIBLING_ID) is None, guard

        # The same result offered to the ordinary path, with nothing to route on.
        ws2, db2, plain = make_env(tmp_path / ("plain_" + guard), MD_SPLIT)
        _kind, _meta, content, _abs, _raw, _span = plain._block_and_kind(BLOCK_ID)
        applied = apply_ops(content, [op], lambda fragment, _column: fragment).text
        ordinary = plain.update_document_section(BLOCK_ID, new_content=applied)
        assert ordinary["status"] == "error", (guard, ordinary)
        assert (ws2 / "docs" / "spec.md").read_text(encoding="utf-8") == MD_SPLIT, guard


NEXT_ID = "docs/spec.md#part/next-section"

#: The four database writes an index pass makes. A DRY RUN must make none of them: on an
#: index deliberately behind the file they were all four emitted by a call that a guard
#: then refused.
DB_WRITES = ("upsert_document", "reset_search_index", "add_search_entries", "set_file_hash")


def spy_on_database_writes(db) -> dict:
    """Count the database writes a call makes: a name is present only if it was called."""
    counts = {}
    for name in DB_WRITES:
        real = getattr(db, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            counts[_name] = counts.get(_name, 0) + 1
            return _real(*args, **kwargs)

        setattr(db, name, spy)
    return counts


def database_state(db) -> dict:
    """What the database HOLDS, over every table the four write methods touch.

    The BYTES of a live SQLite file are not an instrument. SQLite checkpoints its
    write-ahead log on its own, without a single write from this code: measured, a run that
    wrote nothing came back with `-wal` truncated to 0 byte and the change counter of `db`
    rewritten, so a byte-for-byte comparison reported a trace that did not exist. A flaky
    instrument is worse than a weaker one -- it manufactures alarms about correct code.
    Counting the WRITES (spy_on_database_writes) plus reading back what every one of them
    would have changed is the measurement that answers the question.
    """
    return {
        "file_hashes": db.get_file_hashes(),
        "indexed_paths": sorted(db.get_indexed_paths()),
        "sections": [
            (row["id"], row["heading"], row.get("content_hash"))
            for row in db.get_document_sections("docs/spec.md")
        ],
        "search_hits": len(db.search_index("Databricks")),
    }

def test_a_refused_route_leaves_no_trace_even_when_the_index_is_behind(tmp_path: Path) -> None:
    """The dry run must leave NO consequence -- the index included, not only the file.

    Measured on an index deliberately BEHIND the file: a routed call that a guard then
    refused still left four database writes, moved indexed_files and documents.content_hash
    and doubled the WAL, while the same refusal through the ordinary path left zero. The
    file write was already suppressed; the INDEX was the other half of the consequence.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    spec = ws / "docs" / "spec.md"
    read = patcher.read_block(BLOCK_ID)
    writes = spy_on_database_writes(db)

    # LAG THE INDEX ON PURPOSE: the file changes on disk and the database keeps its old
    # hash, so the next index pass WOULD re-index. That is the state the trace needs.
    spec.write_text(MD_SPLIT + "\n<!-- note added out of band -->\n", encoding="utf-8")
    before = database_state(db)

    refused = patcher.edit_block(
        BLOCK_ID,
        ops=[{"insert_after": 4, "content": "\n### 9. Nine\n- x\n```"}],
        expected_hash=read["content_hash"],
    )

    assert refused["status"] == "error", refused
    assert "fence" in refused["error"]
    assert writes == {}, writes
    assert database_state(db) == before

    # The CONTROL: the same verdict through the ordinary path writes nothing either.
    ws2, db2, plain = make_env(tmp_path / "ordinary", MD_SPLIT)
    plain_read = plain.read_block(BLOCK_ID)
    plain_writes = spy_on_database_writes(db2)
    (ws2 / "docs" / "spec.md").write_text(
        MD_SPLIT + "\n<!-- note added out of band -->\n", encoding="utf-8"
    )
    plain_before = database_state(db2)

    ordinary = plain.edit_block(
        BLOCK_ID,
        ops=[{"replace": [4, 4], "content": "- c\n```"}],
        expected_hash=plain_read["content_hash"],
    )

    assert ordinary["status"] == "error", ordinary
    assert "fence" in ordinary["error"]
    assert plain_writes == {}, plain_writes
    assert database_state(db2) == plain_before


def test_A4_a_batch_entry_stale_because_of_its_own_sibling_is_applied(tmp_path: Path) -> None:
    """A change the TOOL made, inside the same call, is not a staleness event -- PROVEN.

    Entry 0 creates a paragraph and renumbers the following sibling while doing it ('10.
    Next Section' -> '11. Next Section'); entry 1 was read before that. The renumbering moved
    line 1 of that block; entry 1 inserts after line 2, whose TEXT is untouched, and the tool
    can show it: the stamp is the block's hash in the bytes this call started from, the hash
    now is its hash in the bytes this call last wrote, and every line the entry addresses
    holds the same text. So the entry is APPLIED against the fresh text, and the reply says
    so -- a batch that refused this would refuse work it can prove is well aimed.

    What moved here is the NEIGHBOUR block, NOT entry 1's own lines: the reply therefore
    carries no LINE rebase on entry 1 (rebase.your_lines is empty and moved_by_entry is None)
    and the warning of the FILE form -- "another entry of THIS call wrote the same FILE" --
    because the cause is the file, not a line that moved under the entry.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    first = patcher.read_block(BLOCK_ID)
    second = patcher.read_block(NEXT_ID)

    reply = patcher._edit_batch([
        {"path": BLOCK_ID, "ops": SIBLING_OPS, "expected_hash": first["content_hash"]},
        {
            "path": NEXT_ID,
            "ops": [{"insert_after": 2, "content": "- extra"}],
            "expected_hash": second["content_hash"],
        },
    ])

    assert reply["refused"] == 0, reply
    assert reply["applied"] == 2, reply
    repaired = reply["results"][1]
    assert repaired["status"] == "success", repaired
    assert repaired["expected_hash_rebased"] is True, repaired
    assert repaired["rebase"] == {"your_lines": [], "moved_by_entry": None}, repaired
    assert any(
        "another entry of THIS call wrote the same FILE" in one
        for one in repaired["warnings"] or []
    ), repaired
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "- extra" in text
    assert "### 11. Next Section" in text


def test_X6_an_entry_whose_block_gained_a_line_is_rebased_onto_the_line_it_read(
    tmp_path: Path,
) -> None:
    """X6: the caller's 1-based numbers still designate the lines it read.

    Entry 0 inserts one line into the SAME block (4 -> 5 lines); entry 1, read before that,
    replaces line 3 -- the line the caller read as '- b'. The insertion moved that line down
    one number, so following the TEXT (what the old proof did) would either write on the
    wrong line or refuse. The tool follows the IDENTITY instead: line 3 of the version read
    is now the block's fourth line, and the replacement is written THERE.

    The old contract refused this entry for staleness. It is now SERVED, and the reply says
    which entry moved the line (rebased_by 0), which line it rebased (rebase.your_lines [3])
    and warns that the lines aimed at had MOVED.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    assert "- b" in read["numbered_text"]

    reply = patcher._edit_batch([
        {
            "path": BLOCK_ID,
            "ops": [{"insert_after": 1, "content": "- inserted"}],
            "expected_hash": read["content_hash"],
        },
        {
            "path": BLOCK_ID,
            "ops": [{"replace": [3, 3], "content": "REPLACED"}],
            "expected_hash": read["content_hash"],
        },
    ])

    assert reply["applied"] == 2, reply
    assert reply["refused"] == 0, reply
    served = reply["results"][1]
    assert served["status"] == "success", served
    assert served["expected_hash_rebased"] is True, served
    assert served["rebased_by"] == 0, served
    assert served["rebase"] == {"your_lines": [3], "moved_by_entry": 0}, served
    assert any("had MOVED" in one for one in served["warnings"] or []), served
    # THE WRITE IS ON THE LINE THE CALLER READ. '- b' was line 3 of the version read; the
    # insertion above it made that line number 4, and 'REPLACED' is written there -- not on
    # the line ('- a') that now occupies number 3.
    numbered = patcher.read_block(BLOCK_ID)["numbered_text"]
    assert "4 | REPLACED" in numbered, numbered
    assert "3 | - a" in numbered and "5 | - c" in numbered, numbered
    assert "- b" not in numbered, numbered


def test_a_batch_entry_stale_from_outside_the_call_is_still_refused(tmp_path: Path) -> None:
    """The repair answers for the batch's OWN writes and nothing else.

    The stamp here is one version behind because of an edit made in a SEPARATE call: the
    tool did not cause it, so the refusal stands, unchanged, and the line count check never
    comes into it.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    stale = patcher.read_block(BLOCK_ID)["content_hash"]
    outside = patcher.edit_block(
        BLOCK_ID,
        ops=[{"insert_after": 4, "content": "- added elsewhere"}],
        expected_hash=stale,
    )
    assert outside["status"] == "success", outside
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    reply = patcher._edit_batch(
        [{"path": BLOCK_ID, "ops": [{"insert_after": 4, "content": "- x"}], "expected_hash": stale}]
    )

    assert reply["applied"] == 0, reply
    entry = reply["results"][0]
    assert entry["status"] == "error", entry
    assert "Stale read" in entry["error"], entry
    assert "expected_hash_rebased" not in entry
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before



def edit_little(patcher, block_id: str, content: str = "- added") -> dict:
    """One harmless insertion at a block's LAST line, against its current stamp."""
    read = patcher.read_block(block_id)
    last = len(read["numbered_text"].splitlines())
    return {
        "path": block_id,
        "ops": [{"insert_after": last, "content": content}],
        "expected_hash": read["content_hash"],
    }


def test_A1_an_outside_writer_with_the_same_line_count_is_still_refused(tmp_path: Path) -> None:
    """A1: the stamp the CALLER sent is compared with the bytes the call started from.

    An outside writer displaced a line WITHOUT changing the number of lines. The previous
    proof compared the file's line count with itself, twice, and never read the caller's
    stamp -- so it repaired. Here (i) fails: the stamp is not the block's hash in the bytes
    this call started from."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    spec = ws / "docs" / "spec.md"
    stale = patcher.read_block(BLOCK_ID)["content_hash"]
    spec.write_text(MD_SPLIT.replace("- b", "- B"), encoding="utf-8")
    before = spec.read_text(encoding="utf-8")
    assert stale != patcher.read_block(BLOCK_ID)["content_hash"]

    reply = patcher._edit_batch(
        [{"path": BLOCK_ID, "ops": [{"insert_after": 4, "content": "- x"}], "expected_hash": stale}]
    )

    assert reply["applied"] == 0, reply
    entry = reply["results"][0]
    assert "Stale read" in entry["error"], entry
    assert "expected_hash_rebased" not in entry
    assert spec.read_text(encoding="utf-8") == before


def test_A2_an_outside_writer_that_changed_the_line_count_is_still_refused(tmp_path: Path) -> None:
    """A2: 4 lines read, 5 lines in the file -- the case the line-count proof let through."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    spec = ws / "docs" / "spec.md"
    stale = patcher.read_block(BLOCK_ID)["content_hash"]
    spec.write_text(MD_SPLIT.replace("- c", "- c\n- d"), encoding="utf-8")
    before = spec.read_text(encoding="utf-8")
    assert stale != patcher.read_block(BLOCK_ID)["content_hash"]

    reply = patcher._edit_batch(
        [{"path": BLOCK_ID, "ops": [{"insert_after": 4, "content": "- x"}], "expected_hash": stale}]
    )

    assert reply["applied"] == 0, reply
    entry = reply["results"][0]
    assert "Stale read" in entry["error"], entry
    assert "expected_hash_rebased" not in entry
    assert spec.read_text(encoding="utf-8") == before


def test_A3_a_block_that_did_not_move_is_applied_without_any_line_rebase(
    tmp_path: Path,
) -> None:
    """A3: another entry writes the SAME file while THIS block did not move -- never refused.

    This is the ONE case of the series that is not a refusal, and on purpose: the block this
    entry edits is byte for byte what the caller read, so every one of its line numbers still
    designates the line it read, and refusing would refuse every valid entry of every batch.

    What the reply says is the other half of the truth: this call wrote the FILE after the
    read, so the entry is answered with expected_hash_rebased true, rebase.your_lines EMPTY and
    moved_by_entry None -- NO line of this block was rebased. No FILE-form warning: this block's
    own text did not change, so the stamp it sent was NOT behind (the warning is pinned by
    test_A4 and test_T3, where the tool itself rewrote the neighbour's heading). A reply that
    claimed a line rebase here would be reporting a move that never took place.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    first = patcher.read_block(NEXT_ID)
    second = patcher.read_block(BLOCK_ID)

    reply = patcher._edit_batch([
        edit_little(patcher, NEXT_ID, "- extra"),
        edit_little(patcher, BLOCK_ID, "- below"),
    ])

    assert reply["applied"] == 2, reply
    assert reply["refused"] == 0, reply
    current = reply["results"][1]
    assert current["status"] == "success", current
    assert current["expected_hash_rebased"] is True, current
    assert current["rebase"] == {"your_lines": [], "moved_by_entry": None}, current
    assert "rebased_by" not in current, current
    assert not any(
        "another entry of THIS call wrote the same FILE" in one
        for one in current.get("warnings") or []
    ), current
    assert first["content_hash"] != "" and second["content_hash"] != ""


def test_a_sibling_write_that_moved_nothing_adds_no_warning(tmp_path: Path) -> None:
    """The hash of a block is the hash of ITS OWN text: writing a sibling block of the same file
    does not move it (measured: the old stamp still opens the block after the sibling's write).
    The FILE-form warning said the stamp was 'one version behind' -- false here -- and that it
    is 'NOT reusable', which holds for every write and which the reply's own content_hash
    already says. A warning has to teach the model something it cannot read elsewhere in the
    reply; when nothing was rebased there is nothing to teach."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)

    reply = patcher._edit_batch([
        edit_little(patcher, NEXT_ID, "- extra"),
        edit_little(patcher, BLOCK_ID, "- below"),
    ])

    assert reply["applied"] == 2, reply
    current = reply["results"][1]
    assert current["status"] == "success", current
    assert current["rebase"]["your_lines"] == [], current
    assert not any("same FILE" in one for one in current.get("warnings") or []), current


def test_A5_a_batch_that_never_wrote_the_file_keeps_the_refusal(tmp_path: Path) -> None:
    """A5: the batch wrote ANOTHER file; this entry's file was never its own doing."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    spec = ws / "docs" / "spec.md"
    (ws / "docs" / "other.md").write_text("# Other\n\n## Part\n\nBody.\n", encoding="utf-8")
    patcher.indexer.index_workspace()

    stale = patcher.read_block(BLOCK_ID)["content_hash"]
    spec.write_text(MD_SPLIT.replace("- b", "- B"), encoding="utf-8")
    before = spec.read_text(encoding="utf-8")

    reply = patcher._edit_batch([
        edit_little(patcher, "docs/other.md#part"),
        {"path": BLOCK_ID, "ops": [{"insert_after": 4, "content": "- x"}], "expected_hash": stale},
    ])

    assert reply["applied"] == 1, reply
    assert reply["refused"] == 1, reply
    entry = reply["results"][1]
    assert "Stale read" in entry["error"], entry
    assert "expected_hash_rebased" not in entry
    assert spec.read_text(encoding="utf-8") == before


def test_A6_an_outside_writer_DURING_the_call_is_caught(tmp_path: Path, monkeypatch) -> None:
    """A6: an entry is stale because of a write that happened WHILE the call was running."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    spec = ws / "docs" / "spec.md"
    first = patcher.read_block(BLOCK_ID)
    second = patcher.read_block(NEXT_ID)
    real_entry = Patcher._batch_entry
    calls = []

    def spy(self, entry, index):
        # _edit_batch calls every entry TWICE (the validating pass, then the real one), so
        # the 4th call is entry 1's real pass -- after entry 0 has written and before entry
        # 1 does. That is an OUTSIDE writer landing in the middle of the call.
        calls.append(index)
        if len(calls) == 4:
            spec.write_text(
                spec.read_text(encoding="utf-8").replace("Body next.", "Body next. moved"),
                encoding="utf-8",
            )
        return real_entry(self, entry, index)

    monkeypatch.setattr(Patcher, "_batch_entry", spy)

    reply = patcher._edit_batch([
        {
            "path": BLOCK_ID,
            "ops": [{"insert_after": 4, "content": "\n### 9. Nine\n- x"}],
            "expected_hash": first["content_hash"],
        },
        edit_little(patcher, NEXT_ID, "- extra"),
    ])

    assert reply["refused"] == 1, reply
    entry = reply["results"][1]
    assert "Stale read" in entry["error"], entry
    assert "expected_hash_rebased" not in entry
    # and the write the outside writer made is still the file's content for that block
    assert "Body next. moved" in spec.read_text(encoding="utf-8")


def test_V1_a_batch_entry_that_REPLACES_the_line_it_read_is_refused(tmp_path: Path) -> None:
    """V1: both entries write the SAME line the caller read -- a REAL conflict, refused.

    Entry 0 rewrites line 2 ('- a' -> '- A'); entry 1, read before that, replaces THAT SAME
    line 2. This is not staleness and it is not a guess about text: the two entries aim at
    one line, serving both would let the second overwrite a line the first already rewrote,
    and the tool cannot know which one was meant to win. The refusal NAMES the entry that got
    there first (rebased_by 0) and the line, and nothing of the refused entry is written."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    assert "2 | - a" in read["numbered_text"]

    reply = patcher._edit_batch([
        {
            "path": BLOCK_ID,
            "ops": [{"replace": [2, 2], "content": "- A"}],
            "expected_hash": read["content_hash"],
        },
        {
            "path": BLOCK_ID,
            "ops": [{"replace": [2, 2], "content": "REPLACED"}],
            "expected_hash": read["content_hash"],
        },
    ])

    assert reply["applied"] == 1, reply
    assert reply["refused"] == 1, reply
    refused = reply["results"][1]
    assert refused["conflict"] is True, refused
    assert refused["rebased_by"] == 0, refused
    assert "entry 0" in refused["error"], refused
    assert "expected_hash_rebased" not in refused, refused
    numbered = patcher.read_block(BLOCK_ID)["numbered_text"]
    assert "REPLACED" not in numbered
    assert "2 | - A" in numbered


def test_T1_a_line_inserted_ABOVE_the_aimed_lines_is_rebased_onto_the_line_read(
    tmp_path: Path,
) -> None:
    """T1: an insertion above the aimed lines SHIFTS them -- the tool follows the IDENTITY.

    Entry 0 inserts one line above; entry 1, read before that, replaces line 4 -- the line
    the caller read as '- c'. Its text moved one number down, so a text comparison would
    either write on the wrong line or refuse. The rebaser carries the number across the move
    the tool itself made, and the replacement lands on the line the caller read; the reply
    names the entry that moved it and the line it rebased.

    The old contract refused this entry. It is now SERVED -- and, crucially, ON THE LINE THE
    CALLER READ: the write must not fall on the line that took number 4 in the moved text.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    assert "4 | - c" in read["numbered_text"]

    reply = patcher._edit_batch([
        {
            "path": BLOCK_ID,
            "ops": [{"insert_after": 1, "content": "- inserted above"}],
            "expected_hash": read["content_hash"],
        },
        {
            "path": BLOCK_ID,
            "ops": [{"replace": [4, 4], "content": "AIMED"}],
            "expected_hash": read["content_hash"],
        },
    ])

    assert reply["applied"] == 2, reply
    assert reply["refused"] == 0, reply
    served = reply["results"][1]
    assert served["status"] == "success", served
    assert served["expected_hash_rebased"] is True, served
    assert served["rebased_by"] == 0, served
    assert served["rebase"] == {"your_lines": [4], "moved_by_entry": 0}, served
    numbered = patcher.read_block(BLOCK_ID)["numbered_text"]
    # '- c' was line 4 of the version read; the insertion made it number 5, and 'AIMED' is
    # written THERE -- not on number 4, which is '- b' now.
    assert "5 | AIMED" in numbered, numbered
    assert "4 | - b" in numbered, numbered


def test_T2_a_line_inserted_BELOW_the_aimed_lines_applies_the_entry(tmp_path: Path) -> None:
    """T2: an insertion BELOW the aimed lines leaves their text intact -- THE tolerance.

    The block gained a line, so the caller's stamp is one version behind; but the entry aims
    ABOVE the insertion, so NO line it addresses moved, and the operations mean exactly what
    they meant when it read them. Refusing here would refuse a well-aimed edit, which is the
    intolerance this proof exists to remove. The reply reports the stamp is not reusable
    (expected_hash_rebased true) WITHOUT claiming a line rebase: rebase.your_lines is empty
    and moved_by_entry is None, and the warning is the FILE one, never the MOVED one."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    last = len(read["numbered_text"].splitlines())
    assert "3 | - b" in read["numbered_text"]

    reply = patcher._edit_batch([
        {
            "path": BLOCK_ID,
            "ops": [{"insert_after": last, "content": "- appended below"}],
            "expected_hash": read["content_hash"],
        },
        {
            "path": BLOCK_ID,
            "ops": [{"replace": [3, 3], "content": "- B"}],
            "expected_hash": read["content_hash"],
        },
    ])

    assert reply["refused"] == 0, reply
    assert reply["applied"] == 2, reply
    repaired = reply["results"][1]
    assert repaired["status"] == "success", repaired
    assert repaired["expected_hash_rebased"] is True, repaired
    assert repaired["rebase"] == {"your_lines": [], "moved_by_entry": None}, repaired
    assert "rebased_by" not in repaired, repaired
    numbered = patcher.read_block(BLOCK_ID)["numbered_text"]
    assert "3 | - B" in numbered
    assert "- appended below" in numbered


def test_T2b_a_replaced_NEIGHBOUR_line_that_is_not_aimed_at_applies(tmp_path: Path) -> None:
    """T2b: a neighbour line the entry does not address may change -- that is still right.

    Entry 0 rewrites line 2; entry 1 addresses line 3 only. Line 3 still holds what the
    caller read, so the operations mean what they meant. This is the case a line COUNT
    happened to accept and a whole-block text comparison would have refused: aiming at the
    LINES the operations name is what makes the permission exact."""
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)

    reply = patcher._edit_batch([
        {
            "path": BLOCK_ID,
            "ops": [{"replace": [2, 2], "content": "- A"}],
            "expected_hash": read["content_hash"],
        },
        {
            "path": BLOCK_ID,
            "ops": [{"replace": [3, 3], "content": "- B"}],
            "expected_hash": read["content_hash"],
        },
    ])

    assert reply["refused"] == 0, reply
    assert reply["applied"] == 2, reply
    assert reply["results"][1]["expected_hash_rebased"] is True
    # The NEIGHBOUR line moved, not the one this entry aims at: the reply reports the stamp
    # is not reusable and claims NO line rebase (your_lines empty, moved_by_entry None).
    assert reply["results"][1]["rebase"] == {"your_lines": [], "moved_by_entry": None}
    assert "rebased_by" not in reply["results"][1]
    numbered = patcher.read_block(BLOCK_ID)["numbered_text"]
    assert "2 | - A" in numbered and "3 | - B" in numbered


#: Two IDENTICAL lines, on purpose: the text of the aimed line is not an identity.
MD_DUPLICATED = "# Guide\n\n## Part\n\n### 8. Block\n- dup\n- dup\n- tail\n"
DUP_ID = "docs/spec.md#part/block"


def test_D1_a_duplicated_line_inserted_above_the_aimed_one_lands_on_the_line_read(
    tmp_path: Path,
) -> None:
    """The TEXT is not the IDENTITY: with a repeated line only the NUMBER places the write.

    Entry 0 inserts one more copy of the aimed line ABOVE it; entry 1 replaces line 3. That
    line still reads '- dup' in both versions, so a comparison by text says nothing about
    POSITION -- and following the text puts the write on the copy entry 0 just inserted,
    leaving the line the caller read untouched. The identity does better: line 3 of the
    version read is the SECOND '- dup', which is now the fourth line, and the write lands
    THERE. This is the case that decided the design (benchmarks/.work/CAS_ONE_SHOT.md case
    H): the file must hold '- dup / - dup / - REPLACED / - tail'.
    """
    ws, db, patcher = make_env(tmp_path, MD_DUPLICATED)
    read = patcher.read_block(DUP_ID)
    assert "3 | - dup" in read["numbered_text"]

    reply = patcher._edit_batch([
        {
            "path": DUP_ID,
            "ops": [{"insert_after": 1, "content": "- dup"}],
            "expected_hash": read["content_hash"],
        },
        {
            "path": DUP_ID,
            "ops": [{"replace": [3, 3], "content": "- REPLACED"}],
            "expected_hash": read["content_hash"],
        },
    ])

    assert reply["applied"] == 2, reply
    assert reply["refused"] == 0, reply
    served = reply["results"][1]
    assert served["status"] == "success", served
    assert served["expected_hash_rebased"] is True, served
    assert served["rebased_by"] == 0, served
    assert served["rebase"] == {"your_lines": [3], "moved_by_entry": 0}, served
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "### 8. Block\n- dup\n- dup\n- REPLACED\n- tail" in text, text


def test_D2_a_duplicated_ANCHOR_for_insert_after_follows_the_line_read(tmp_path: Path) -> None:
    """An `insert_after` whose anchor text repeats: the marker follows the NUMBER read.

    Entry 0 inserts one more '- dup' above; entry 1 asks for `insert_after 3`. Three identical
    '- dup' lines now stand in the block, so the anchor's TEXT cannot say which one was meant.
    The number the caller read can: line 3 of the version read is now the fourth line, so the
    marker is seated after THAT copy -- the one entry 0 did not create -- and the reply reports
    the rebase (rebased_by 0, rebase.your_lines [3]).
    """
    ws, db, patcher = make_env(tmp_path, MD_DUPLICATED)
    read = patcher.read_block(DUP_ID)

    reply = patcher._edit_batch([
        {
            "path": DUP_ID,
            "ops": [{"insert_after": 1, "content": "- dup"}],
            "expected_hash": read["content_hash"],
        },
        {
            "path": DUP_ID,
            "ops": [{"insert_after": 3, "content": "- MARK"}],
            "expected_hash": read["content_hash"],
        },
    ])

    assert reply["applied"] == 2, reply
    assert reply["refused"] == 0, reply
    served = reply["results"][1]
    assert served["status"] == "success", served
    assert served["expected_hash_rebased"] is True, served
    assert served["rebased_by"] == 0, served
    assert served["rebase"] == {"your_lines": [3], "moved_by_entry": 0}, served
    numbered = patcher.read_block(DUP_ID)["numbered_text"]
    # Three '- dup' lines: the copy entry 0 created comes first, and the marker is seated
    # after line 4 -- the copy the caller read on line 3 -- then '- tail'.
    assert numbered.splitlines() == [
        "1 | ### 8. Block",
        "2 | - dup",
        "3 | - dup",
        "4 | - dup",
        "5 | - MARK",
        "6 | - tail",
    ], numbered


CODE_FILE = "def compute(a, b):\n    total = a + b\n    return total\n\n\ndef other(x):\n    return x\n"
#: A root block that is NOT empty: text before the first heading. (A Markdown file that starts
#: with a heading has an EMPTY root, which only 'insert_after 0' can write -- see
#: `_empty_root_refusal` -- so it cannot carry this scenario.)
ROOT_DOC = "Root text line one.\nRoot text line two.\n\n## Section\n\nBody.\n"


def make_code_env(tmp_path: Path):
    """A workspace with a Markdown file AND a code file, both indexed."""
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "src").mkdir(parents=True)
    (ws / "docs" / "spec.md").write_text(MD, encoding="utf-8")
    (ws / "docs" / "notes.md").write_text(ROOT_DOC, encoding="utf-8")
    (ws / "src" / "app.py").write_text(CODE_FILE, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, db, Patcher(config, db, indexer)


def test_T4_the_tolerance_covers_a_CODE_SYMBOL_and_a_DOCUMENT_ROOT(tmp_path: Path) -> None:
    """(a): the proof reads ANY block kind, not only a Markdown section.

    Measured before this fix: the same shape was APPLIED on a section and REFUSED on a code
    symbol and on a file root, because the resolution went through `markdown_layout` alone and
    answered None for anything that is not a Markdown heading. The entry's block is now
    captured with `_block_and_kind`, which resolves a section, a symbol and a root alike -- so a
    batch that inserts BELOW the aimed lines applies on a symbol exactly as on a section.
    """
    ws, db, patcher = make_code_env(tmp_path)
    symbol = db.get_symbols_for_file("src/app.py")[0]["id"]
    assert symbol.endswith("::compute")

    cases = [
        (symbol, "    total = a + b + 1", "# appended"),
        ("docs/notes.md", "Root text line two changed.", "Root text appended."),
    ]
    for target, replaced, appended in cases:
        read = patcher.read_block(target)
        last = len(read["numbered_text"].splitlines())
        reply = patcher._edit_batch([
            {
                "path": target,
                "ops": [{"insert_after": last, "content": appended}],
                "expected_hash": read["content_hash"],
            },
            {
                "path": target,
                "ops": [{"replace": [2, 2], "content": replaced}],
                "expected_hash": read["content_hash"],
            },
        ])
        # THE defect, for every kind: before this fix both of these were REFUSED where the
        # same shape on a Markdown section applied. Both entries now land.
        assert reply["refused"] == 0, (target, reply)
        assert reply["applied"] == 2, (target, reply)
        # The flag is never FALSE: it is a report of a repair, not a decoration. Whether one
        # was needed here depends on the block's hash, which T3/A4/T2 pin on sections; T4 pins
        # the TOLERANCE (no refusal) on kinds the Markdown-only resolution could not even name.
        answer = reply["results"][1]
        assert answer.get("expected_hash_rebased") in (None, True), (target, answer)

    assert "total = a + b + 1" in (ws / "src" / "app.py").read_text(encoding="utf-8")
    assert "Root text line two changed." in (ws / "docs" / "notes.md").read_text(encoding="utf-8")


def test_V3_two_batch_entries_on_the_SAME_line_are_refused(tmp_path: Path) -> None:
    """V3: both entries write the SAME line -- the REAL conflict, and it is named.

    Entry 1 never read what it overwrites: entry 0 already wrote line 3 of the version the
    caller read. This is one of the only two refusals the batch keeps, and the reply says so
    as a FACT, not as a word to grep: conflict is true, rebased_by is 0 (the entry that got
    there first), and nothing of the refused entry is written.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)

    reply = patcher._edit_batch([
        {
            "path": BLOCK_ID,
            "ops": [{"replace": [3, 3], "content": "- FIRST"}],
            "expected_hash": read["content_hash"],
        },
        {
            "path": BLOCK_ID,
            "ops": [{"replace": [3, 3], "content": "- SECOND"}],
            "expected_hash": read["content_hash"],
        },
    ])

    assert reply["applied"] == 1, reply
    assert reply["refused"] == 1, reply
    refused = reply["results"][1]
    assert refused["conflict"] is True, refused
    assert refused["rebased_by"] == 0, refused
    assert "line 3" in refused["error"], refused
    assert "expected_hash_rebased" not in refused, refused
    numbered = patcher.read_block(BLOCK_ID)["numbered_text"]
    assert "- SECOND" not in numbered
    assert "- FIRST" in numbered


def test_T3_the_rebase_field_names_the_real_cause_and_never_a_refusal(tmp_path: Path) -> None:
    """T3: the field is a MEASUREMENT, not a decoration -- and it names the cause that applies.

    Two directions, because either one alone can be faked. (a) A REFUSED entry carries no
    rebase field and no "version behind" warning anywhere: a refusal is not a rebase. (b) A
    SERVED entry whose stamp this call invalidated carries expected_hash_rebased true, and
    the warning names the cause that REALLY applies: another entry of THIS call wrote the
    same FILE. Nothing moved under this entry's own lines, so rebase.your_lines is EMPTY and
    moved_by_entry is None -- a reply claiming a line rebase here would report a move that
    never took place.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    spec = ws / "docs" / "spec.md"

    # (a) stale from OUTSIDE the call: refused, and no field anywhere.
    stale = patcher.read_block(BLOCK_ID)["content_hash"]
    spec.write_text(spec.read_text(encoding="utf-8").replace("- b", "- B"), encoding="utf-8")
    refused = patcher._edit_batch([
        {"path": BLOCK_ID, "ops": [{"insert_after": 4, "content": "- x"}], "expected_hash": stale},
    ])
    assert refused["applied"] == 0, refused
    assert "expected_hash_rebased" not in refused
    for answer in refused["results"]:
        assert "expected_hash_rebased" not in answer, answer
        assert not any("version behind" in one for one in answer.get("warnings") or []), answer

    # (b) stale because of THIS call's own write, and the aimed lines are intact: the entry
    #     is served, flagged, and the warning names the FILE -- not a line that did not move.
    ws2, db2, plain = make_env(tmp_path / "repaired", MD_SPLIT)
    first = plain.read_block(BLOCK_ID)
    second = plain.read_block(NEXT_ID)
    repaired = plain._edit_batch([
        {"path": BLOCK_ID, "ops": SIBLING_OPS, "expected_hash": first["content_hash"]},
        {
            "path": NEXT_ID,
            "ops": [{"insert_after": 2, "content": "- extra"}],
            "expected_hash": second["content_hash"],
        },
    ])
    assert repaired["refused"] == 0, repaired
    answer = repaired["results"][1]
    assert answer["expected_hash_rebased"] is True, answer
    assert answer["rebase"] == {"your_lines": [], "moved_by_entry": None}, answer
    assert "rebased_by" not in answer, answer
    assert any(
        "another entry of THIS call wrote the same FILE" in one
        for one in answer["warnings"]
    ), repaired


def test_a_markdown_section_is_snapshotted_so_a_refused_batch_can_put_it_back(
    tmp_path: Path,
) -> None:
    """`_remember_file` must resolve the FILE behind a `doc#slug` address.

    A Markdown entry names a PARAGRAPH, not a file. Without the '#' the resolver looked for
    a file of that literal name, found none and snapshotted NOTHING -- so a batch refused on
    the second pass put .py files back and left the Markdown file MODIFIED while its reply
    said `batch_atomic: true` / NOTHING was written. That reply is the one an agent reads to
    decide whether it may re-send, so it was false in the worst place.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    spec = ws / "docs" / "spec.md"
    before = spec.read_bytes()
    originals: dict = {}

    patcher._remember_file({"path": BLOCK_ID}, originals)

    assert list(originals) == [str(spec)], originals
    spec.write_text("# clobbered by a half-applied batch\n", encoding="utf-8")
    assert spec.read_bytes() != before
    patcher._restore_files(originals)
    assert spec.read_bytes() == before


def test_a_guard_cannot_be_implemented_on_one_side_only(tmp_path: Path, monkeypatch) -> None:
    """The registry IS the implementation, and the route runs ALL of it.

    The hole a verifier found: a guard declared in the registry, given its scenarios, and
    implemented only in the ordinary path -- nothing fell. `RESULT_GUARDS` now DERIVES from
    `RESULT_GUARD_RUNNERS`, so a name without an implementation cannot be written; and the
    spy below proves the ROUTE reaches every runner of the map, through the same entry
    point the ordinary path uses.
    """
    assert set(Patcher.RESULT_GUARDS) == set(Patcher.RESULT_GUARD_RUNNERS)
    assert Patcher.RESULT_GUARDS, "an empty registry would make this test vacuous"

    called = []
    for name, runner in list(Patcher.RESULT_GUARD_RUNNERS.items()):

        def spy(self, text, proposed, section_id, context, _runner=runner, _name=name):
            called.append(_name)
            return _runner(self, text, proposed, section_id, context)

        monkeypatch.setitem(Patcher.RESULT_GUARD_RUNNERS, name, spy)

    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)
    result = patcher.edit_block(BLOCK_ID, ops=SIBLING_OPS, expected_hash=read["content_hash"])

    assert result["status"] == "success", result
    assert result.get("title_route") is True
    routed = set(called)
    assert routed == set(Patcher.RESULT_GUARDS), routed

    # ...and the ORDINARY path reaches every one of them too: a guard implemented on one
    # side only makes one of these two assertions fall, in either direction.
    called.clear()
    ws2, db2, plain = make_env(tmp_path / "ordinary", MD_SPLIT)
    plain_read = plain.read_block(BLOCK_ID)
    plain.edit_block(
        BLOCK_ID,
        ops=[{"insert_after": 4, "content": "- added"}],
        expected_hash=plain_read["content_hash"],
    )
    assert set(called) == set(Patcher.RESULT_GUARDS), called


#: Text before the first heading: a preamble, whose layout level is 0.
MD_PREAMBLE = "Intro paragraph.\n\n## Alpha\n\nBody A.\n"


def test_a_preamble_keeps_its_heading_edit_inside_itself(tmp_path: Path) -> None:
    """A preamble carries no heading of its own, so the routing cannot take its edit.

    _leading_markdown_heading answers None on a FIRST line that is not a heading, so the
    preamble's block level is its layout level, 0 -- and a heading is never level 0. The
    level condition of the routing therefore fails on its own, the edit is accepted
    INSIDE the block as it always was, and the reply carries no insert_block field.
    """
    ws, db, patcher = make_env(tmp_path, MD_PREAMBLE)
    read = patcher.read_block("docs/spec.md#preamble")

    result = patcher.edit_block(
        "docs/spec.md#preamble",
        ops=[{"insert_after": 2, "content": "\n## New Preamble Section\n- z"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    # The routing did not take it: this is the in-block reply, with none of its fields.
    assert not result.get("title_route")
    assert "parent_id" not in result and "level" not in result
    assert "## New Preamble Section" in (ws / "docs" / "spec.md").read_text(encoding="utf-8")


def test_the_refusal_names_the_parent_so_one_call_repairs_it(tmp_path: Path) -> None:
    """A refusal that STILL stands carries the parent, and calling it back works.

    The shape is the MIXED one -- an insertion beside a delete -- which _title_route
    leaves alone, because the block itself would move and there would be nothing to
    infer. The parent the hint names is the one the routing uses, and the identifier
    insert_block returns for it is, byte for byte, the one the routing produces.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    read = patcher.read_block(BLOCK_ID)

    refusal = patcher.edit_block(
        BLOCK_ID,
        ops=SIBLING_OPS + [{"delete": [4, 4]}],
        expected_hash=read["content_hash"],
    )
    parent_id = refusal["actionable_hint"].split("parent='")[1].split("'")[0]

    repaired = patcher.insert_block(
        parent_id, BLOCK_ID, "9. Databricks PWAF OAuth Authentication\n- x\n- y"
    )

    assert repaired["status"] == "success", repaired
    assert repaired["block_id"] == SIBLING_ID
    assert repaired["level"] == 3


def test_the_routed_identifier_is_the_one_insert_block_returns(tmp_path: Path) -> None:
    """The routing is not a second authority on identity: it is insert_block's own.

    An identifier is the path of the headings that hold the paragraph, with the
    number removed (documents.stable_section_ids), so the same paragraph written by
    either path must carry the SAME id. Two environments, one call each: the routing
    on the left, insert_block(parent=holder, anchor=the block) on the right.
    """
    _ws_a, _db_a, routed_patcher = make_env(tmp_path / "routed", MD_SPLIT)
    read = routed_patcher.read_block(BLOCK_ID)
    routed = routed_patcher.edit_block(
        BLOCK_ID, ops=SIBLING_OPS, expected_hash=read["content_hash"]
    )

    _ws_b, _db_b, manual = make_env(tmp_path / "manual", MD_SPLIT)
    inserted = manual.insert_block(
        "docs/spec.md#part", BLOCK_ID, "9. Databricks PWAF OAuth Authentication\n- x\n- y"
    )

    assert routed["status"] == "success", routed
    assert inserted["status"] == "success", inserted
    assert routed["block_id"] == inserted["block_id"] == SIBLING_ID
    assert routed["created_section_ids"] == inserted["created_section_ids"]
    assert routed["heading"] == inserted["heading"]
    assert routed["level"] == inserted["level"]
    assert routed["number"] == inserted["number"]


def test_a_batch_entry_that_creates_a_sibling_writes_nothing_in_the_preview_pass(
    tmp_path: Path, monkeypatch
) -> None:
    """A batch runs TWO passes: the routing must decide the same and write on the last.

    Patcher._edit_batch validates every entry first with the writes suppressed, and only
    then applies them. The spy below records, at each suppressed write, what the file
    still held: the routing REACHED the write -- the same decision as the real pass -- and
    the disk had not moved at that moment.
    """
    ws, db, patcher = make_env(tmp_path, MD_SPLIT)
    spec = ws / "docs" / "spec.md"
    original = spec.read_text(encoding="utf-8")
    read = patcher.read_block(BLOCK_ID)

    seen_during_preview = []
    real_write = Patcher._atomic_write

    def spy(self, target_path, content_bytes):
        if self._preview_pass:
            seen_during_preview.append(spec.read_text(encoding="utf-8"))
        return real_write(self, target_path, content_bytes)

    monkeypatch.setattr(Patcher, "_atomic_write", spy)

    reply = patcher._edit_batch(
        [{"path": BLOCK_ID, "ops": SIBLING_OPS, "expected_hash": read["content_hash"]}]
    )

    assert reply["applied"] == 1, reply
    entry = reply["results"][0]
    assert entry["status"] == "success", entry
    # The batch reply speaks the printed grammar, so the id is the one it prints.
    assert entry["block_id"] == patcher.displayed_id(SIBLING_ID)
    assert entry["host_block_unchanged"] is True
    assert entry["title_route"] is True
    # The suppressed pass DID reach the creation, and the file was untouched then.
    assert seen_during_preview, "the preview pass never reached the write"
    assert all(text == original for text in seen_during_preview)
    text = spec.read_text(encoding="utf-8")
    assert text.count("### 9. Databricks PWAF OAuth Authentication") == 1
    assert patcher.read_block(BLOCK_ID)["content_hash"] == read["content_hash"]


MD_SUBSECTIONS = (
    "# Title\n\n## Section A\n\n### Old sub\n\nbody old\n\n"
    "### Second sub\n\nbody second\n\n## Section B\n\nBody B.\n"
)


def test_edit_block_retitles_a_sub_heading_in_place(tmp_path: Path) -> None:
    """Renaming a paragraph keeps its node: simple resolution, so it applies.

    The number and the place belong to the outline; the title is text. Renaming
    is therefore an in-block edit, and the reply carries the id the paragraph now
    has -- the agent never has to renumber anything itself.
    """
    ws, db, patcher = make_env(tmp_path, MD_SUBSECTIONS)
    block_id = "docs/spec.md#section-a/old-sub"
    read = patcher.read_block(block_id)

    result = patcher.edit_block(
        block_id, ops=[{"replace": [1, 1], "content": "### Renamed sub"}], expected_hash=read["content_hash"]
    )

    assert result["status"] == "success", result
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "### Renamed sub" in text
    assert "### Old sub" not in text
    assert result["resulting_section_ids"] == ["docs/spec.md#section-a/renamed-sub"]
    # The heading the block has NOW: reporting the pre-edit one made an agent believe
    # its retitle had not been applied (declared on mapab2 b r1 of the base a619198).
    assert result["heading"] == "Renamed sub"


def test_edit_block_adds_one_sub_heading(tmp_path: Path) -> None:
    """Adding exactly one deeper paragraph is a simple resolution too."""
    ws, db, patcher = make_env(tmp_path, MD_SUBSECTIONS)
    block_id = "docs/spec.md#section-a"
    read = patcher.read_block(block_id)

    result = patcher.edit_block(
        block_id,
        ops=[{"insert_after": read["line_count"], "content": "\n### Added sub\n\nbody"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    created = [db.get_document_section(item)["heading"] for item in result["created_section_ids"]]
    assert created == ["Added sub"]


def test_edit_block_refuses_two_headings_in_one_edit(tmp_path: Path) -> None:
    """Two new nodes at once, NOT as a pure append at the block's end: refused.

    The pure append of nested titles at the end is served since motif 3 (see
    tests/test_multi_titles.py). Inserted in the MIDDLE of the block, the outline
    reply still cannot describe them, and the tool refuses.
    """
    ws, db, patcher = make_env(tmp_path, "# Title\n\n## Section A\n\nline one\n\nline two\n\n## Section B\n\nBody B.\n")
    block_id = "docs/spec.md#section-a"
    read = patcher.read_block(block_id)
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    result = patcher.edit_block(
        block_id,
        ops=[{"insert_after": 3, "content": "\n### Added one\n\nbody\n\n### Added two\n\nbody"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "error"
    assert "outline" in result["error"]
    assert "insert_block" in result["actionable_hint"]
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before


MD_DEEP = "# Title\n\n### Deep section\n\nbody\n"


def test_insert_warns_when_a_section_number_is_already_used(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path, "# Title\n\n## 9. Archive\n\nbody\n")

    result = patcher.insert_block("docs/spec.md#title", "docs/spec.md#archive", "9. New section\nbody")

    assert result["status"] == "success"
    assert any("already used by" in warning for warning in result["warnings"])


MD_TWO_PARENTS = (
    "# Guide\n\n"
    "## 1. First\n\n### 1. String Comparisons\n\nbody\n\n"
    "## 2. Second\n\n### 9. Settings\n\nbody\n"
)


def test_a_number_under_another_parent_is_not_a_conflict(tmp_path: Path) -> None:
    """The same number in another list is not a clash.

    Declared on mapab2 a r1 of the base ef07986: the warning sent the agent to
    "repair" numbers the tool had already renumbered correctly (two turns).
    """
    ws, db, patcher = make_env(tmp_path, MD_TWO_PARENTS)

    result = patcher.insert_block("docs/spec.md#second", "docs/spec.md#second", "1. New child\nbody")

    assert result["status"] == "success", result
    assert not any("already used by" in warning for warning in result["warnings"])


def test_insert_can_renumber_the_following_sections(tmp_path: Path) -> None:
    """One call inserts the section and renumbers the rest, instead of two passes."""
    ws, db, patcher = make_env(
        tmp_path, "# Title\n\n## 9. Archive\n\nbody\n\n## 10. Scratchpad\n\nbody\n"
    )

    result = patcher.insert_block("docs/spec.md#title", "docs/spec.md#title", "9. PWAF Authentication\nbody")

    assert result["status"] == "success"
    headings = [section["heading"] for section in db.get_document_sections("docs/spec.md")]
    assert "9. PWAF Authentication" in headings
    assert "10. Archive" in headings
    assert "11. Scratchpad" in headings
    assert not any("already used" in warning for warning in result["warnings"])


def test_delete_unknown_section_is_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)

    result = patcher.delete_document_section("docs/spec.md#99")

    assert result["status"] == "error"
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == MD

# A fence that is never closed: everything after it stops being Markdown, so the
# section that carries it swallows the rest of the file. mapab2 a r3 left one behind
# and spent 50 turns trying to repair a document no tool would touch any more.
MD_UNCLOSED_FENCE = (
    "# Guide\n\n"
    "## Quick Start\n\n"
    "```bash\n"
    "dotnet build\n\n"
    "## Connecting\n\nbody\n\n"
    "### First Query\n\nbody\n"
)

MD_HEALTHY = "# Guide\n\n## Section\n\nbody\n"


def test_an_edit_that_leaves_a_fence_unclosed_is_refused(tmp_path: Path) -> None:
    """The edit that cost 50 turns: it must be refused, not applied."""
    ws, db, patcher = make_env(tmp_path, MD_HEALTHY)
    block_id = "docs/spec.md#section"
    read = patcher.read_block(block_id)
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    result = patcher.edit_block(
        block_id,
        ops=[{"insert_after": 3, "content": "```bash\ndotnet build"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "error"
    assert "fence" in result["error"]
    assert "```" in result["actionable_hint"]
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before


def test_the_unclosed_fence_can_be_repaired_by_deleting_it(tmp_path: Path) -> None:
    """The repair the guard used to forbid: exposing a heading is not writing one."""
    ws, db, patcher = make_env(tmp_path, MD_UNCLOSED_FENCE)
    assert [row["heading"] for row in db.get_document_sections("docs/spec.md")] == [
        "Guide",
        "Quick Start",
    ]
    read = patcher.read_block("docs/spec.md#quick-start")
    assert read["line_count"] == 12  # the block swallowed the rest of the file

    result = patcher.edit_block(
        "docs/spec.md#quick-start",
        ops=[{"delete": [3, 3]}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    assert [row["heading"] for row in db.get_document_sections("docs/spec.md")] == [
        "Guide",
        "Quick Start",
        "Connecting",
        "First Query",
    ]


#: D7 -- THE UNIT OF A SPAN. Sixty 'e' accented, each TWO bytes and ONE character, sit
#: before the section: the layout measures its offsets in the CHARACTERS of the decoded
#: text, and `_begin_block_state` sliced the file's BYTES with them. The bytes actually
#: read were therefore a window taken somewhere ELSE in the file -- measured on the campaign
#: repo (LINQ-to-Spark.md: a 16-line block read as 11 lines), and here: a valid `insert_after`
#: on the last line read_block had just printed was refused with "insert_after must be
#: inside 0..1", while the SAME call sent alone (no batch) was served.
MD_ACCENTED = "# Guide\n\n" + "\u00e9" * 60 + "\n\n## Target\n\nb1\nb2\nb3\n"
ACCENTED_SECTION = "docs/spec.md::Guide::Target"


def test_D7_a_sections_span_indexes_the_bytes_it_is_returned_with(tmp_path: Path) -> None:
    """(kind, meta, content, path, raw, span): `span` applied to `raw` must reproduce `content`.

    The span is what a later write re-selects the block with -- `_begin_block_state` and
    `_record_write` slice file BYTES -- so a span counted in characters does not merely
    misreport a line count: it hands the rebaser a DIFFERENT text than read_block printed.
    """
    _ws, _db, patcher = make_env(tmp_path, MD_ACCENTED)

    resolved = patcher._block_and_kind(ACCENTED_SECTION)
    assert resolved is not None, "the section must resolve"
    _kind, _meta, content, _abs_path, raw, span = resolved
    start, end = int(span[0]), int(span[1])

    assert raw[start:end].decode("utf-8", "surrogateescape") == content
    # And the window is the RIGHT one: it starts on the heading, not 60 bytes earlier.
    assert start == raw.index(b"## Target")
    assert content.splitlines() == ["## Target", "", "b1", "b2", "b3"]


def test_D7_a_batch_entry_anchored_on_the_last_line_read_block_printed_is_served(
    tmp_path: Path,
) -> None:
    """The observed defect: ONE entry in `edits=[...]` is enough to trigger it.

    read_block prints 5 lines and the entry anchors on line 5 -- the block's own last line.
    The batch path builds its rebaser from the bytes the span selects, so it read a window
    that was not the block and refused a call the plain form served unchanged.
    """
    ws, _db, patcher = make_env(tmp_path, MD_ACCENTED)
    read = patcher.read_block(ACCENTED_SECTION)
    assert read["status"] == "success", read
    assert read["line_count"] == 5, read
    assert read["numbered_text"].splitlines()[-1].startswith("5 | b3")

    reply = patcher._edit_batch([{
        "path": ACCENTED_SECTION,
        "expected_hash": read["content_hash"],
        "ops": [{"insert_after": read["line_count"], "content": "\nBATCH-ANCHOR\n"}],
    }])

    entry = reply["results"][0]
    assert entry["status"] == "success", entry
    assert "BATCH-ANCHOR" in (ws / "docs" / "spec.md").read_text(encoding="utf-8")


def test_D7_a_served_batch_entry_writes_the_line_the_caller_read(tmp_path: Path) -> None:
    """CONTROL: a write that IS served lands on the line the caller numbered, not a neighbour.

    The shifted window can contain lines that make an operation LEGAL without making it the
    one the caller aimed at: this asserts the exact file, line for line.
    """
    ws, _db, patcher = make_env(tmp_path, MD_ACCENTED)
    read = patcher.read_block(ACCENTED_SECTION)
    assert read["numbered_text"].splitlines()[2] == "3 | b1"

    reply = patcher._edit_batch([{
        "path": ACCENTED_SECTION,
        "expected_hash": read["content_hash"],
        "ops": [{"replace": [3, 3], "content": "B1-READ"}],
    }])

    entry = reply["results"][0]
    assert entry["status"] == "success", entry
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert text == "# Guide\n\n" + "\u00e9" * 60 + "\n\n## Target\n\nB1-READ\nb2\nb3\n"
    # The line the caller read as 3 is still line 3, and it carries what was written.
    assert patcher.read_block(ACCENTED_SECTION)["numbered_text"].splitlines()[2] == "3 | B1-READ"


#: The REAL shape of the campaign refusals (p1_r07/r15/r18, LINQ-to-Spark.md): a two-entry
#: batch, the FIRST entry an `insert_after <line_count>` on a 16-line block that holds a fenced
#: mermaid diagram, the second a `replace` on an earlier section; accented text sits before
#: both. The refusal said "insert_after must be inside 0..11" while the same refusal carried
#: line_count=16.
MD_BATCH_REAL = (
    "# Guide\n\n" + "\u00e9" * 60 + "\n\n## Quick\n\n### Connect\nc1\nc2\nc3\n\n"
    "## Deploying\n\nIntro\n\n### Architecture\n\n```mermaid\ngraph LR\n    subgraph X\n"
    "        A[a]\n        B[b]\n        C[c]\n        D[d]\n    end\n\n"
    "    A -->|x & y| B\n    B -->|z| C\n    C -->|w| D\n```\n\n### Next\nn\n"
)


def test_D7_a_two_entry_batch_anchored_on_a_16_line_block_is_served(tmp_path: Path) -> None:
    ws, _db, patcher = make_env(tmp_path, MD_BATCH_REAL)
    arch_id = "docs/spec.md::Guide::Deploying::Architecture"
    con_id = "docs/spec.md::Guide::Quick::Connect"
    arch = patcher.read_block(arch_id)
    con = patcher.read_block(con_id)
    assert arch["line_count"] == 16, arch

    reply = patcher._edit_batch([
        {"path": arch_id, "expected_hash": arch["content_hash"],
         "ops": [{"insert_after": arch["line_count"], "content": "TAIL"}]},
        {"path": con_id, "expected_hash": con["content_hash"],
         "ops": [{"replace": [3, 3], "content": "c2a\nc2b\nc2c"}]},
    ])

    assert reply["applied"] == 2 and reply["refused"] == 0, reply
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert "TAIL" in text, text
    assert "c2a\nc2b\nc2c\n" in text, text


def test_D7_an_anchor_really_beyond_the_block_is_still_refused_in_a_batch(tmp_path: Path) -> None:
    """The DANGEROUS neighbour: an insert_after past the block's real end stays refused."""
    ws, _db, patcher = make_env(tmp_path, MD_BATCH_REAL)
    arch_id = "docs/spec.md::Guide::Deploying::Architecture"
    arch = patcher.read_block(arch_id)
    before = (ws / "docs" / "spec.md").read_text(encoding="utf-8")

    reply = patcher._edit_batch([
        {"path": arch_id, "expected_hash": arch["content_hash"],
         "ops": [{"insert_after": arch["line_count"] + 5, "content": "TAIL"}]},
    ])

    assert reply["applied"] == 0 and reply["refused"] == 1, reply
    assert "inside 0.." + str(arch["line_count"]) in reply["results"][0]["error"], reply
    assert (ws / "docs" / "spec.md").read_text(encoding="utf-8") == before


#: The SECOND producer of the same span: the document's own root text (`_document_block`,
#: span (0, end)). The non-ASCII bytes live in the preamble here, and the whole root is
#: before them -- so the same shift applies to the document block.
MD_ROOT_ACCENTED = "\u00e9" * 60 + "\n\n# Guide\n\n## Target\n\nb1\nb2\nb3\n"


def test_D7_the_document_root_span_indexes_the_bytes_it_is_returned_with(tmp_path: Path) -> None:
    _ws, _db, patcher = make_env(tmp_path, MD_ROOT_ACCENTED)

    resolved = patcher._block_and_kind("docs/spec.md")
    assert resolved is not None
    kind, _meta, content, _abs_path, raw, span = resolved
    assert kind == "document"
    assert int(span[0]) == 0
    assert int(span[1]) == raw.index(b"# Guide")
    assert raw[int(span[0]):int(span[1])].decode("utf-8", "surrogateescape") == content


#: The THIRD producer of the span, the LEGACY positional alias (`db.get_document_section`
#: on `doc#N`): it is still reachable -- `docs/spec.md#1` answers the second heading -- so it
#: receives the same byte span as the live one.
def test_D7_a_legacy_positional_span_indexes_the_bytes_it_is_returned_with(tmp_path: Path) -> None:
    _ws, _db, patcher = make_env(tmp_path, MD_ACCENTED)

    resolved = patcher._block_and_kind("docs/spec.md#1")
    assert resolved is not None
    _kind, _meta, content, _abs_path, raw, span = resolved
    assert content.splitlines() == ["## Target", "", "b1", "b2", "b3"]
    assert int(span[0]) == raw.index(b"## Target")
    assert raw[int(span[0]):int(span[1])].decode("utf-8", "surrogateescape") == content


def test_D7_a_batch_entry_on_a_legacy_positional_alias_is_served(tmp_path: Path) -> None:
    ws, _db, patcher = make_env(tmp_path, MD_ACCENTED)
    read = patcher.read_block("docs/spec.md#1")
    assert read["status"] == "success", read
    assert read["line_count"] == 5, read

    reply = patcher._edit_batch([{
        "path": "docs/spec.md#1",
        "expected_hash": read["content_hash"],
        "ops": [{"insert_after": read["line_count"], "content": "\nLEGACY-ANCHOR\n"}],
    }])

    entry = reply["results"][0]
    assert entry["status"] == "success", entry
    assert "LEGACY-ANCHOR" in (ws / "docs" / "spec.md").read_text(encoding="utf-8")


def test_D7_a_batch_entry_on_the_document_root_is_served(tmp_path: Path) -> None:
    ws, _db, patcher = make_env(tmp_path, MD_ROOT_ACCENTED)
    read = patcher.read_block("docs/spec.md")
    assert read["status"] == "success", read
    # The document root's OWN text is the preamble: 60 accented e plus the blank line.
    # (line_count of a document root is the FILE's size -- a documented choice -- so the
    # anchor is taken from the numbered text, the coordinate system edit_block uses.)
    numbered = read["numbered_text"].splitlines()
    assert len(numbered) == 2, numbered

    reply = patcher._edit_batch([{
        "path": "docs/spec.md",
        "expected_hash": read["content_hash"],
        "ops": [{"insert_after": len(numbered), "content": "\nROOT-EDIT\n"}],
    }])

    entry = reply["results"][0]
    assert entry["status"] == "success", entry
    text = (ws / "docs" / "spec.md").read_text(encoding="utf-8")
    assert text.index("ROOT-EDIT") < text.index("# Guide")

# ---------------------------------------------------------------------------
# D7, ROUND 2 -- THE SPAN MUST INDEX THE DISK BYTES, EXACTLY, even for a file the
# UTF-8 decoder CANNOT read. Round 1 measured the span on the text decoded with
# errors="replace": one invalid byte becomes U+FFFD, which re-encodes to THREE bytes,
# so the window drifted by +2 bytes per invalid byte -- 60 bytes 0xFF put span [191,211]
# on a 91-byte file, an EMPTY slice, and the D7 refusal came back on the very file it
# had fixed for valid UTF-8. The byte span is now measured on a decode that RE-ENCODES
# to the same bytes (errors="surrogateescape"); that text stays inside the span, and
# errors="replace" remains the codec of everything the agent reads.
# ---------------------------------------------------------------------------


def make_env_raw(tmp_path: Path, raw: bytes):
    """make_env, but the file is written BYTE for BYTE: an invalid byte survives."""
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "spec.md").write_bytes(raw)
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, db, Patcher(config, db, indexer)


TARGET_BLOCK_BYTES = b"## Target\n\nb1\nb2\nb3\n"


def invalid_section(pad: bytes) -> bytes:
    return b"# Guide\n\n" + pad + b"\n\n## Target\n\nb1\nb2\nb3\n"


#: One, four and sixty bytes the UTF-8 decoder cannot read, BEFORE the block.
INVALID_PADS = {"1_invalid_byte": b"\xff", "4_invalid_bytes": b"\xff" * 4, "60_invalid_bytes": b"\xff" * 60}


def block_of(patcher, raw: bytes, address: str = ACCENTED_SECTION):
    """(kind, content, disk bytes, span) of a resolved block -- measured, not asserted."""
    resolved = patcher._block_and_kind(address)
    assert resolved is not None, "the block must resolve: " + address
    kind, _meta, content, _abs_path, disk, span = resolved
    first, last = int(span[0]), int(span[1])
    return kind, content, disk, (first, last)


@pytest.mark.parametrize("label,pad", sorted(INVALID_PADS.items()))
def test_D7_r2_the_span_indexes_the_disk_bytes_for_an_invalid_pad(
    tmp_path: Path, label: str, pad: bytes
) -> None:
    """THE regression of round 1: replace-mode U+FFFD is 3 bytes, the invalid byte is 1."""
    raw = invalid_section(pad)
    _ws, _db, patcher = make_env_raw(tmp_path, raw)

    _kind, content, disk, (first, last) = block_of(patcher, raw)

    assert first == raw.index(b"## Target"), (label, first, raw.index(b"## Target"))
    assert disk[first:last] == TARGET_BLOCK_BYTES, (label, disk[first:last])
    assert content == TARGET_BLOCK_BYTES.decode("utf-8"), (label, content)
    assert disk == raw, "the `raw_bytes` member must BE the file on disk"


@pytest.mark.parametrize("label,pad", sorted(INVALID_PADS.items()))
def test_D7_r2_a_batch_entry_is_served_for_an_invalid_pad(
    tmp_path: Path, label: str, pad: bytes
) -> None:
    """The verifier symptom, on the very files round 1 broke: a batch of ONE entry."""
    raw = invalid_section(pad)
    ws, _db, patcher = make_env_raw(tmp_path, raw)
    read = patcher.read_block(ACCENTED_SECTION)
    assert read["status"] == "success", read
    assert read["line_count"] == 5, (label, read)

    reply = patcher._edit_batch([{
        "path": ACCENTED_SECTION,
        "expected_hash": read["content_hash"],
        "ops": [{"insert_after": read["line_count"], "content": "\nBATCH-ANCHOR\n"}],
    }])

    entry = reply["results"][0]
    assert entry["status"] == "success", (label, entry)
    on_disk = (ws / "docs" / "spec.md").read_bytes()
    assert b"BATCH-ANCHOR" in on_disk, label
    assert TARGET_BLOCK_BYTES in on_disk, label


def test_D7_r2_the_batch_and_the_single_call_write_the_same_bytes(tmp_path: Path) -> None:
    """THE invariant of the defect: `edits=[one entry]` and the plain form agree. """
    raw = invalid_section(b"\xff" * 60)
    op = {"insert_after": 5, "content": "\nSAME-ANCHOR\n"}

    ws_batch, _db_b, batch = make_env_raw(tmp_path / "batch", raw)
    read_batch = batch.read_block(ACCENTED_SECTION)
    entry = batch._edit_batch([{
        "path": ACCENTED_SECTION,
        "expected_hash": read_batch["content_hash"],
        "ops": [op],
    }])["results"][0]

    ws_single, _db_s, single = make_env_raw(tmp_path / "single", raw)
    read_single = single.read_block(ACCENTED_SECTION)
    plain = single.edit_block(ACCENTED_SECTION, ops=[op], expected_hash=read_single["content_hash"])

    assert entry["status"] == "success", entry
    assert plain["status"] == "success", plain
    assert (ws_batch / "docs" / "spec.md").read_bytes() == (ws_single / "docs" / "spec.md").read_bytes()


#: The other documents the verifier listed: an astral character, CRLF, valid accented UTF-8.
ASTRA = "\U0001f600"
ACCEPTED_DOCS = {
    "astral_emoji": invalid_section(("\u00e9\n" + ASTRA * 30 + "\n").encode("utf-8")),
    "valid_accented": invalid_section(("\u00e9" * 60).encode("utf-8")),
    "crlf": b"# Guide\r\n\r\n" + b"\xff" * 4 + b"\r\n\r\n## Target\r\n\r\nb1\r\nb2\r\nb3\r\n",
}


@pytest.mark.parametrize("label,raw", sorted(ACCEPTED_DOCS.items()))
def test_D7_r2_the_span_stays_exact_for_the_other_documents(
    tmp_path: Path, label: str, raw: bytes
) -> None:
    ws, _db, patcher = make_env_raw(tmp_path, raw)

    _kind, content, disk, (first, last) = block_of(patcher, raw)
    expected = raw[raw.index(b"## Target"):]
    assert disk[first:last] == expected, (label, disk[first:last], expected)
    assert content == expected.decode("utf-8", "replace"), label

    read = patcher.read_block(ACCENTED_SECTION)
    assert read["status"] == "success", (label, read)
    reply = patcher._edit_batch([{
        "path": ACCENTED_SECTION,
        "expected_hash": read["content_hash"],
        "ops": [{"insert_after": read["line_count"], "content": "\nOK-ANCHOR\n"}],
    }])
    assert reply["results"][0]["status"] == "success", (label, reply["results"][0])
    assert b"OK-ANCHOR" in (ws / "docs" / "spec.md").read_bytes(), label


def _root_doc(pad: bytes) -> bytes:
    return pad + b"\n\n# Guide\n\n## Target\n\nb1\nb2\nb3\n"


ROOT_PADS = {
    "valid_accented": ("\u00e9" * 60).encode("utf-8"),
    "60_invalid_bytes": b"\xff" * 60,
}


@pytest.mark.parametrize("label,pad", sorted(ROOT_PADS.items()))
def test_D7_r2_the_document_root_span_and_batch_hold_for_its_preamble(
    tmp_path: Path, label: str, pad: bytes
) -> None:
    raw = _root_doc(pad)
    ws, _db, patcher = make_env_raw(tmp_path, raw)

    _kind, content, disk, (first, last) = block_of(patcher, raw, "docs/spec.md")
    assert (first, last) == (0, raw.index(b"# Guide")), (label, first, last)
    assert content == raw[: last].decode("utf-8", "replace"), label

    read = patcher.read_block("docs/spec.md")
    assert read["status"] == "success", (label, read)
    numbered = read["numbered_text"].splitlines()
    reply = patcher._edit_batch([{
        "path": "docs/spec.md",
        "expected_hash": read["content_hash"],
        "ops": [{"insert_after": len(numbered), "content": "\nROOT-ANCHOR\n"}],
    }])
    assert reply["results"][0]["status"] == "success", (label, reply["results"][0])
    assert b"ROOT-ANCHOR" in (ws / "docs" / "spec.md").read_bytes(), label


def test_D7_r2_the_legacy_alias_holds_for_an_invalid_pad(tmp_path: Path) -> None:
    raw = invalid_section(b"\xff" * 60)
    ws, _db, patcher = make_env_raw(tmp_path, raw)

    _kind, content, disk, (first, last) = block_of(patcher, raw, "docs/spec.md#1")
    assert disk[first:last] == TARGET_BLOCK_BYTES, disk[first:last]
    assert content == TARGET_BLOCK_BYTES.decode("utf-8"), content

    read = patcher.read_block("docs/spec.md#1")
    reply = patcher._edit_batch([{
        "path": "docs/spec.md#1",
        "expected_hash": read["content_hash"],
        "ops": [{"insert_after": read["line_count"], "content": "\nLEGACY-R2\n"}],
    }])
    assert reply["results"][0]["status"] == "success", reply["results"][0]
    assert b"LEGACY-R2" in (ws / "docs" / "spec.md").read_bytes()


def test_D7_r2_an_empty_file_resolves_to_a_zero_length_span(tmp_path: Path) -> None:
    _ws, _db, patcher = make_env_raw(tmp_path, b"")

    resolved = patcher._block_and_kind("docs/spec.md")
    assert resolved is not None, "an empty document is still a document"
    assert tuple(int(one) for one in resolved[5]) == (0, 0), resolved[5]
    assert resolved[2] == ""


def test_D7_r2_a_file_with_no_heading_is_its_whole_text(tmp_path: Path) -> None:
    raw = b"\xff" * 4 + b"\n\nplain body\n\nmore\n"
    ws, _db, patcher = make_env_raw(tmp_path, raw)

    _kind, content, disk, (first, last) = block_of(patcher, raw, "docs/spec.md")
    assert (first, last) == (0, len(raw)), (first, last, len(raw))
    assert content == raw.decode("utf-8", "replace")

    read = patcher.read_block("docs/spec.md")
    numbered = read["numbered_text"].splitlines()
    reply = patcher._edit_batch([{
        "path": "docs/spec.md",
        "expected_hash": read["content_hash"],
        "ops": [{"insert_after": len(numbered), "content": "\nNO-HEADING\n"}],
    }])
    assert reply["results"][0]["status"] == "success", reply["results"][0]
    assert b"NO-HEADING" in (ws / "docs" / "spec.md").read_bytes()


def test_D7_r2_the_reply_key_contract_is_unchanged_by_invalid_bytes(tmp_path: Path) -> None:
    """The shape of the answer does not depend on whether the file decodes."""
    _ws_a, _db_a, ascii_patcher = make_env(tmp_path / "ascii", MD_ACCENTED)
    _ws_b, _db_b, invalid_patcher = make_env_raw(tmp_path / "invalid", invalid_section(b"\xff" * 60))

    def keys(patcher):
        read = patcher.read_block(ACCENTED_SECTION)
        reply = patcher._edit_batch([{
            "path": ACCENTED_SECTION,
            "expected_hash": read["content_hash"],
            "ops": [{"insert_after": read["line_count"], "content": "\nKEYS\n"}],
        }])
        entry = reply["results"][0]
        assert entry["status"] == "success", entry
        return sorted(read), sorted(reply), sorted(entry)

    assert keys(ascii_patcher) == keys(invalid_patcher)


@pytest.fixture
def surface_server(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "g.md").write_bytes(invalid_section(b"\xff" * 60))
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


@pytest.mark.anyio
async def test_D7_r2_the_mcp_surface_serves_and_serializes_an_invalid_preamble(
    surface_server: ASTGraphServer,
) -> None:
    """Through the REAL surface: the reply must serialize (no lone surrogate can reach it)."""
    address = "docs/g.md::Guide::Target"
    read = json.loads(
        (await surface_server.mcp.call_tool("read_block", {"path": address, "format": "json"})).content[0].text
    )
    assert read["status"] == "success", read
    assert read["line_count"] == 5, read

    answer = json.loads(
        (await surface_server.mcp.call_tool("edit_block", {
            "format": "json",
            "edits": [{
                "path": address,
                "expected_hash": read["content_hash"],
                "ops": [{"insert_after": read["line_count"], "content": "\nSURFACE-ANCHOR\n"}],
            }]
        })).content[0].text
    )
    assert answer["applied"] == 1, answer
    assert answer["results"][0]["status"] == "success", answer["results"][0]
    on_disk = (Path(surface_server.config.workspace_root) / "docs" / "g.md").read_bytes()
    assert b"SURFACE-ANCHOR" in on_disk
    assert TARGET_BLOCK_BYTES in on_disk


