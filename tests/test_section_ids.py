"""A paragraph's identifier is its title -- never its number.

The number is structure: the tools assign it, move it and correct it. Leaving it
in the identifier had two measured effects: renumbering one paragraph changed the
ids of every paragraph after it, and an id like `#9-archive-scratchpad-hygiene`
reads as a position, which is the confusion behind a run writing into the wrong
section.
"""

from ast_graph_server.documents import DocumentSection, section_title, stable_section_ids


def section(heading: str, level: int = 3) -> DocumentSection:
    return DocumentSection(heading=heading, level=level, content="body", order_index=0)


def test_the_paragraph_number_is_not_part_of_the_identifier() -> None:
    ids = stable_section_ids("d.md", [section("7. Unity Catalog"), section("8. Telemetrie")])
    assert ids == ["d.md#unity-catalog", "d.md#telemetrie"]


def test_renumbering_does_not_move_a_single_identifier() -> None:
    before = stable_section_ids("d.md", [section("8. Telemetrie"), section("9. Archive")])
    after = stable_section_ids("d.md", [section("9. Telemetrie"), section("10. Archive")])
    assert before == after


def test_a_version_number_is_not_a_paragraph_number() -> None:
    ids = stable_section_ids("d.md", [section("1.3.2-dev - Unreleased")])
    assert ids == ["d.md#1-3-2-dev-unreleased"]


def test_titles_that_differ_only_by_their_number_stay_distinct() -> None:
    ids = stable_section_ids("d.md", [section("1. Notes"), section("2. Notes")])
    assert ids == ["d.md#notes", "d.md#notes-2"]


def test_a_heading_without_a_number_is_untouched() -> None:
    ids = stable_section_ids("d.md", [section("Preamble"), section("Databricks OAuth")])
    assert ids == ["d.md#preamble", "d.md#databricks-oauth"]



def test_a_duplicated_title_is_qualified_by_the_path_of_its_parents() -> None:
    """A bare occurrence suffix re-points when a sibling is deleted.

    Measured relation, mapab2 b r1 of the base edfe80a: after the agent deleted its own
    `### New Features` under `[1.3.2-dev]`, the id `#new-features-2` designated the
    `[1.3.1]` paragraph -- a name that silently changed meaning under a mutation made
    elsewhere. The agent declared it: "the id is positional, not stable".
    """
    changelog = [
        section("Changelog", level=1),
        section("[1.3.2-dev] - Unreleased", level=2),
        section("New Features", level=3),
        section("New Features", level=3),
        section("[1.3.1] - 2026-05", level=2),
        section("New Features", level=3),
    ]
    assert stable_section_ids("c.md", changelog) == [
        "c.md#changelog",
        "c.md#1-3-2-dev-unreleased",
        "c.md#1-3-2-dev-unreleased/new-features",
        "c.md#1-3-2-dev-unreleased/new-features-2",
        "c.md#1-3-1-2026-05",
        "c.md#1-3-1-2026-05/new-features",
    ]

    # Delete the agent's own duplicate under 1.3.2-dev: the paragraph under 1.3.1 keeps
    # its name, and the deleted name names nothing rather than its neighbour.
    after = stable_section_ids("c.md", [s for index, s in enumerate(changelog) if index != 3])
    assert "c.md#1-3-1-2026-05/new-features" in after
    assert "c.md#1-3-2-dev-unreleased/new-features" in after
    assert "c.md#1-3-2-dev-unreleased/new-features-2" not in after


def test_the_top_level_heading_is_the_document_and_is_not_repeated() -> None:
    """The path names the paragraphs below the title: the file part names the title."""
    nested = [
        section("Guide", level=1),
        section("Install", level=2),
        section("Steps", level=3),
    ]
    assert stable_section_ids("g.md", nested) == [
        "g.md#guide",
        "g.md#install",
        "g.md#install/steps",
    ]


def test_section_title_only_strips_a_leading_number() -> None:
    assert section_title("9. Archive") == "Archive"
    assert section_title("10) Archive") == "Archive"
    assert section_title("1.3.2-dev") == "1.3.2-dev"
    assert section_title("Etape 3 : les prerequis") == "Etape 3 : les prerequis"
