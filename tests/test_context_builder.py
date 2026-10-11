"""Tests for relevance ranking and the budgeted context builder."""

from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.context_builder import ContextBuilder
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.relevance import BM25Backend, HybridBackend, VectorBackend, cosine, fts_query


def make_env(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)
    (ws / "src" / "billing.py").write_text(
        "class BillingService:\n"
        "    def charge(self, user_id: str, amount: float) -> bool:\n"
        '        """Process the payment for a user."""\n'
        "        return True\n\n"
        "    def unrelated_helper(self):\n"
        '        """Format a greeting message."""\n'
        '        return "hi"\n',
        encoding="utf-8",
    )
    (ws / "docs" / "guide.md").write_text(
        "# Integration Guide\n\n"
        "## Databricks OAuth\n\n"
        "Use OAuth machine-to-machine with a client id and client secret.\n\n"
        "## Gardening\n\n"
        "Water the plants.\n",
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    Indexer(config, db).index_workspace()
    return ws, config, db


def test_fts_query_sanitizes() -> None:
    assert fts_query("ab * cd") == '"ab" OR "cd"'
    assert fts_query("") == ""


def test_bm25_ranks_relevant_symbol(tmp_path: Path) -> None:
    ws, config, db = make_env(tmp_path)
    results = BM25Backend(db).search("process payment user", limit=10)
    refs = [item["ref_id"] for item in results]
    assert any("charge" in ref for ref in refs)


def test_context_builder_respects_budget_and_deepens(tmp_path: Path) -> None:
    ws, config, db = make_env(tmp_path)
    builder = ContextBuilder(config, db)

    small = builder.build("Databricks OAuth client secret", budget_tokens=300, max_level=3)
    assert small["used_tokens_estimate"] <= 300
    assert any("OAuth" in entry["text"] for entry in small["entries"])

    flat = builder.build("Databricks OAuth client secret", budget_tokens=8000, max_level=0)
    assert all(entry["level"] == 0 for entry in flat["entries"])

    large = builder.build("Databricks OAuth client secret", budget_tokens=8000, max_level=3)
    assert max(entry["level"] for entry in large["entries"]) == 3
    assert large["entry_count"] >= flat["entry_count"]


def test_budget_reporting_covers_the_wire_payload(tmp_path: Path) -> None:
    """The reported cost must describe the payload the model actually receives.

    `entries` JSON carries ref_id, kind, level and score on top of the content
    and tokenises as punctuation-heavy text, so it costs materially more tokens
    than the identical content emitted as a labelled text block. Reporting only
    the text figure hid that difference.
    """
    ws, config, db = make_env(tmp_path)
    builder = ContextBuilder(config, db)

    payload = builder.build("Databricks OAuth client secret", budget_tokens=8000, max_level=3)

    # Same content, two emissions: JSON must not be the cheaper one.
    assert payload["json_tokens_estimate"] > payload["used_tokens_estimate"]
    assert 0.0 < payload["budget_fill_ratio"] <= 1.0
    assert payload["full_content_count"] == sum(
        1 for entry in payload["entries"] if entry["level"] >= 3
    )


def test_budget_tracks_real_tokens_not_just_characters(tmp_path: Path) -> None:
    """The budget is in tokens but the packer measures characters, so the
    conversion factor decides whether the budget is real. At 4 chars/token a
    payload overshot its budget by ~50% once measured with a real tokenizer."""
    ws, config, db = make_env(tmp_path)
    builder = ContextBuilder(config, db)

    payload = builder.build("Databricks OAuth client secret", budget_tokens=300, max_level=3)

    # The estimate the builder reports must stay inside the requested budget.
    assert payload["used_tokens_estimate"] <= 300
    assert payload["budget_fill_ratio"] <= 1.0
    # And it must be derived from the measured ratio, not from a 4 chars/token
    # assumption that under-counts this kind of content.
    assert ContextBuilder.CHARS_PER_TOKEN < 4.0
    assert payload["used_tokens_estimate"] == int(len(payload["text"]) / ContextBuilder.CHARS_PER_TOKEN)


def test_dossier_section_keys_are_usable_by_the_reading_tools(tmp_path: Path) -> None:
    """A Dossier section must be keyed by the identifier `get_document_section`
    accepts (`<file>#<n>`), not by a decorative rendering of its heading.

    When the keys did not work, the agent had to pay a `list_document_sections`
    call per document just to rebuild them: 23% of one run's tool-result volume.
    """
    ws, config, db = make_env(tmp_path)
    builder = ContextBuilder(config, db)

    payload = builder.build("Databricks OAuth", budget_tokens=8000, max_level=3)
    section_entries = [entry for entry in payload["entries"] if entry["kind"] == "section"]
    assert section_entries, "the fixture should yield at least one section entry"

    for entry in section_entries:
        ref = entry["ref_id"]
        # Usable: the exact id shape the tools consume, which the database stores.
        assert "#" in ref, ref
        assert "::" not in ref, ref
        if entry["level"] >= 1:
            # And it really resolves.
            assert db.get_document_section(ref) is not None, ref

    # The human-facing suffix is not worth paying tokens for.
    assert "(heading level" not in payload["text"]


def test_document_outline_pairs_headings_with_identifiers(tmp_path: Path) -> None:
    """A document entry at full detail must expose section identifiers, so the
    agent can jump straight to a paragraph instead of rebuilding an outline."""
    ws, config, db = make_env(tmp_path)
    builder = ContextBuilder(config, db)

    document = db.get_document("docs/guide.md")
    assert document is not None
    # (text, extra): the extra carries the block's stamp, and this builder has no address printers
    # injected, so it renders the degraded form -- storage ids, and no stamp it could not certify.
    text, extra = builder._document_text(document, 3)

    assert "docs/guide.md#databricks-oauth" in text
    assert "Databricks OAuth" in text
    assert extra == {}


def test_cosine_and_vector_backend(tmp_path: Path) -> None:
    ws, config, db = make_env(tmp_path)
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([], [1.0]) == 0.0

    # Without an embedder the vector backend is inert (graceful degradation).
    assert VectorBackend(db, None).search("oauth") == []

    db.store_embeddings([("docs/guide.md#1", [1.0, 0.0]), ("docs/guide.md#2", [0.0, 1.0])])
    stored = dict(db.all_embeddings())
    assert len(stored) == 2


def test_hybrid_backend_fuses_vector_and_bm25(tmp_path: Path) -> None:
    ws, config, db = make_env(tmp_path)
    db.store_embeddings([("docs/guide.md#1", [1.0, 0.0]), ("docs/guide.md#2", [0.0, 1.0])])

    class FakeEmbedder:
        def __call__(self, text):
            return [1.0, 0.0] if "oauth" in text.lower() else [0.0, 1.0]

    backend = HybridBackend([BM25Backend(db), VectorBackend(db, FakeEmbedder())])
    results = backend.search("oauth", limit=10)
    assert any(item["ref_id"] == "docs/guide.md#1" for item in results)


def test_project_map_is_query_independent_and_budgeted(tmp_path: Path) -> None:
    """The Dossier gains an optional structural map: folders and file names.

    It must be off by default (historical behaviour unchanged), present when
    asked, independent of the query, and still inside the token budget.
    """
    ws, config, db = make_env(tmp_path)
    builder = ContextBuilder(config, db)

    off = builder.build("Databricks OAuth", budget_tokens=8000, max_level=3)
    assert off["map_chars"] == 0
    assert "[carte]" not in off["text"]

    on = builder.build("Databricks OAuth", budget_tokens=8000, max_level=3, map_ratio=0.2)
    assert on["map_chars"] > 0
    assert on["text"].startswith("[carte]")
    assert "billing.py" in on["text"]
    assert "guide.md" in on["text"]

    # Same structure whatever the intent: the map is not a search result.
    other = builder.build("Gardening plants", budget_tokens=8000, max_level=3, map_ratio=0.2)
    assert other["map_chars"] == on["map_chars"]
    assert other["text"][: other["map_chars"]] == on["text"][: on["map_chars"]]
    assert on["used_tokens_estimate"] <= 8000


def test_map_tokens_gives_the_map_its_own_budget(tmp_path: Path) -> None:
    """A map budget ON TOP of the Dossier: sections untouched.

    So an A/B of the map changes ONE thing (the map), not the selection.
    `map_ratio` instead carves the map out of the section budget, which compared
    a map against a *smaller* selection -- measured on 25/09, the arm with the map
    got a shorter prompt than the arm without it.
    """
    ws, config, db = make_env(tmp_path)
    builder = ContextBuilder(config, db)

    without = builder.build("Databricks OAuth", budget_tokens=8000, max_level=3)
    assert without["map_chars"] == 0
    assert without["map_tokens"] == 0

    with_map = builder.build("Databricks OAuth", budget_tokens=8000, max_level=3, map_tokens=600)
    assert with_map["map_chars"] > 0
    assert with_map["map_tokens"] > 0
    assert with_map["text"].startswith("[carte]")
    # The section selection is IDENTICAL: the map did not eat into it.
    assert with_map["entry_count"] == without["entry_count"]
    assert with_map["text"].endswith(without["text"])

    # The two forms size the map deliberately differently: map_tokens gives it its
    # OWN budget, map_ratio takes it out of the sections' budget.
    assert with_map["map_budget_chars"] == int(600 * builder.CHARS_PER_TOKEN)
    assert with_map["map_truncated"] is False

    # A partial tree must be reported, never silently injected.
    tiny = builder.build("Databricks OAuth", budget_tokens=8000, max_level=3, map_tokens=1)
    assert tiny["map_truncated"] is True
    carved = builder.build("Databricks OAuth", budget_tokens=8000, max_level=3, map_ratio=0.2)
    assert carved["map_chars"] > 0
    assert carved["map_budget_chars"] == int(int(8000 * builder.CHARS_PER_TOKEN) * 0.2)


def test_context_min_score_drops_the_tail(tmp_path: Path) -> None:
    """A relevance floor is what turns a technical query into a usable Dossier.

    Measured on the real repository: a technical query at >= 10 keeps 20 entries
    (the code, its tests, the public doc) instead of hundreds of low-score
    neighbours. The task wording must not be floored -- its top entries are the
    business documents that carry the requirement.
    """
    ws, config, db = make_env(tmp_path)
    builder = ContextBuilder(config, db)

    full = builder.build("Databricks OAuth", budget_tokens=8000, max_level=3)
    assert full["entry_count"] > 0
    assert full["min_score"] is None

    assert builder.build("Databricks OAuth", budget_tokens=8000, max_level=3, min_score=1e9)["entry_count"] == 0
    floored = builder.build("Databricks OAuth", budget_tokens=8000, max_level=3, min_score=0.0)
    assert floored["entry_count"] == full["entry_count"]
    assert floored["min_score"] == 0.0


def test_detail_levels_map_to_increasing_budgets() -> None:
    budgets = ContextBuilder.DETAIL_LEVEL_BUDGETS
    assert sorted(budgets) == [1, 2, 3, 4, 5]
    assert budgets[1] < budgets[3] < budgets[5]


def test_render_project_tree_groups_by_directory() -> None:
    from ast_graph_server.rendering import render_project_tree

    text = render_project_tree(["a/b/c.py", "a/b/d.py", "a/e.md", "top.txt"])
    assert "[carte] 4 fichiers, 3 dossiers" in text
    assert "a/b/ (2): c.py, d.py" in text
    assert "a/ (1): e.md" in text
    assert "top.txt" in text

    small = render_project_tree(["a/f%03d.py" % i for i in range(200)], budget_chars=120)
    assert "tronquee au budget" in small


def test_context_builder_caps_entries_per_file(tmp_path: Path) -> None:
    """Verifies that max_entries_per_file limits the number of entries from any single file."""
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    # Create a document with many sections matching the same query
    doc_lines = ["# Big Multi Section Document\n\n"]
    for i in range(10):
        doc_lines.append(f"## Section {i} Target Subject\n\nContent for section {i} discussing Target Subject in detail.\n\n")
    (ws / "docs" / "big_doc.md").write_text("".join(doc_lines), encoding="utf-8")

    # Create another document matching the query
    (ws / "docs" / "other_doc.md").write_text(
        "# Other Document\n\n## Target Subject Alternative\n\nAlternative details on Target Subject.\n",
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    Indexer(config, db).index_workspace()
    builder = ContextBuilder(config, db)

    # Without cap: big_doc dominates
    uncapped = builder.build("Target Subject", budget_tokens=8000, max_level=0, max_entries_per_file=None)
    big_doc_entries_uncapped = [e for e in uncapped["entries"] if "big_doc.md" in e["ref_id"]]
    assert len(big_doc_entries_uncapped) > 3

    # With cap=2: big_doc has at most 2 entries
    capped = builder.build("Target Subject", budget_tokens=8000, max_level=0, max_entries_per_file=2)
    big_doc_entries_capped = [e for e in capped["entries"] if "big_doc.md" in e["ref_id"]]
    assert len(big_doc_entries_capped) <= 2
    assert capped["max_entries_per_file"] == 2

    # Verify other_doc is included in capped results
    other_doc_entries = [e for e in capped["entries"] if "other_doc.md" in e["ref_id"]]
    assert len(other_doc_entries) > 0


