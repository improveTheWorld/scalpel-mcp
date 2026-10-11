"""Finding and reading must not cost two turns.

Root causes measured on the pair3 journal (pwaf-prerequisites__scalpel__r0):

  * 10 search_documents calls came back empty because the whole query was
    matched as one substring ("OAuth machine-to-machine M2M U2M service
    principal"); every empty answer bought a rephrase turn;
  * 6 of 8 inspect_symbols calls carried a single symbol, in its own turn, for
    symbols a search_symbols call had just listed in the previous turn;
  * 5 get_document_sections calls asked for more paragraphs than max_full=3
    returned in full, so the remainder had to be fetched afterwards.
"""

import json
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import query_terms
from ast_graph_server.server import ASTGraphServer

GUIDE = (
    "# Integration Guide\n\n"
    "## Databricks OAuth\n\n"
    "The connector supports machine-to-machine (M2M) authentication with a "
    "service principal: set DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET.\n\n"
    "## Billing\n\nInvoices are issued monthly.\n\n"
    "## Support\n\nContact the service desk.\n"
)

CODE = (
    "class SparkConnectOptions:\n"
    "    def ApplyAuthentication(self, token: str) -> bool:\n"
    '        """Attach the OAuth token to the connection."""\n'
    "        return True\n"
)


@pytest.fixture
def env(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "src").mkdir()
    (ws / "docs" / "guide.md").write_text(GUIDE, encoding="utf-8")
    (ws / "src" / "spark.py").write_text(CODE, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


@pytest.mark.anyio
async def test_a_single_entry_edits_takes_the_top_level_path(env: ASTGraphServer) -> None:
    """The shape the model reaches for NINE times: edits=[one entry] beside a top-level path.

    Measured on a r2 of the base 15711e6: FIVE refusals in ONE run (t9, t12, t15, t18, t20), one
    turn each, always the same shape -- a single entry with no path next to a top-level path. With
    one entry the intent is unambiguous, so the path fills it, and the reply says so.
    """
    ident = "docs/guide.md::Integration Guide::Billing"
    block = json.loads((await env.mcp.call_tool("read_block", {"paths": [ident], "format": "json"})).content[0].text)["blocks"][0]
    payload = json.loads(
        (
            await env.mcp.call_tool(
                "edit_block",
                {
                    "path": ident,
                    "edits": [
                        {
                            "ops": [{"replace": [1, 1], "content": "## Billing (revised)"}],
                            "expected_hash": block["content_hash"],
                        }
                    ],
                    "format": "json",
                },
            )
        ).content[0].text
    )
    assert payload["status"] == "success", payload
    assert payload["applied"] == 1 and payload["refused"] == 0, payload
    assert any("top-level path" in one for one in payload.get("warnings") or []), payload


@pytest.mark.anyio
async def test_several_entries_without_a_path_name_the_offending_entry(env: ASTGraphServer) -> None:
    """One path cannot fill several entries: the refusal says WHICH entry, and why.

    The message used to read "an entry of edits has no path", with no index -- four occurrences
    across the campaign had an agent re-derive which entry it was (backlog §9).
    """
    ident = "docs/guide.md::Integration Guide::Billing"
    payload = json.loads(
        (
            await env.mcp.call_tool(
                "edit_block",
                {
                    "path": ident,
                    "edits": [{"ops": [], "expected_hash": "x"}, {"ops": [], "expected_hash": "y"}],
                },
            )
        ).content[0].text
    )
    errors = [one.get("error", "") for one in payload["results"]]
    assert errors[0].startswith("entry 0 of edits has no path"), errors
    assert errors[1].startswith("entry 1 of edits has no path"), errors
    assert all("SEVERAL entries" in one for one in errors), errors


def test_query_terms_splits_and_deduplicates() -> None:
    assert query_terms("OAuth machine-to-machine M2M service principal") == [
        "oauth",
        "machine",
        "to",
        "m2m",
        "service",
        "principal",
    ]
    assert query_terms("SparkConnectOptions.UserAgent") == ["sparkconnectoptions.useragent"]
    assert query_terms("   ") == []


def test_multi_word_query_never_comes_back_empty(env: ASTGraphServer) -> None:
    # No paragraph contains this phrasing verbatim.
    matches = env.db.search_documents("OAuth service principal connection rotation", limit=8)
    assert matches, "a multi-word question must return its best partial evidence"
    assert matches[0]["section_id"] == "docs/guide.md#databricks-oauth"


def test_search_documents_inlines_the_best_matches(env: ASTGraphServer) -> None:
    matches = env.db.search_documents("service principal authentication", limit=8, full_top=1)
    assert len(matches) >= 2
    assert matches[0]["section_id"] == "docs/guide.md#databricks-oauth"
    assert matches[0]["content"].startswith("The connector supports")
    assert "content" not in matches[1]


def test_search_documents_reports_a_cut_body_as_cut(env: ASTGraphServer) -> None:
    matches = env.db.search_documents("service principal authentication", full_top=1, max_content_chars=20)
    assert matches[0]["content_truncated"] is True
    assert matches[0]["content_total_chars"] > 20


@pytest.mark.anyio
async def test_batch_read_returns_every_requested_paragraph(env: ASTGraphServer) -> None:
    payload = json.loads(
        (
            await env.mcp.call_tool(
                "read_block",
                {
                    "paths": ["docs/guide.md#0", "docs/guide.md#1", "docs/guide.md#2", "docs/guide.md#3"],
                    "format": "json",
                },
            )
        ).content[0].text
    )
    # Un lot = UN tour : chaque bloc revient avec son texte numerote et son hash.
    assert len(payload["blocks"]) == 4
    assert "The connector supports" in payload["blocks"][1]["numbered_text"]


@pytest.mark.anyio
async def test_a_batch_has_one_form_whatever_its_blocks_answer(env: ASTGraphServer) -> None:
    """TEXT: one text, a block per paragraph, an error rendered as a line of that text.
    JSON: always the {"blocks": [...]} envelope. Never an envelope holding bare strings."""
    good = ["docs/guide.md#0", "docs/guide.md#1"]
    bad = ["docs/guide.md#98", "docs/guide.md#99"]

    async def read(paths, fmt):
        return (await env.mcp.call_tool("read_block", {"paths": paths, "format": fmt})).content[0].text

    for paths, errors in ((good, 0), (bad, 2), ([good[0], bad[0], good[1]], 1)):
        text = await read(paths, "text")
        assert not text.lstrip().startswith("{"), text
        assert text.count("[h:") == len(paths) - errors, text
        assert text.count("[error] ") == errors, text

        envelope = json.loads(await read(paths, "json"))
        statuses = [block["status"] for block in envelope["blocks"]]
        assert statuses == ["error" if address in bad else "success" for address in paths], envelope
        assert all(isinstance(block, dict) for block in envelope["blocks"]), envelope

    # Order is the caller's order: the error sits BETWEEN the two blocks, not after them.
    mixed = await read([good[0], bad[0], good[1]], "text")
    assert mixed.index("[h:") < mixed.index("[error] ") < mixed.rindex("[h:"), mixed


@pytest.mark.anyio
async def test_search_documents_tool_inlines_content(env: ASTGraphServer) -> None:
    payload = json.loads(
        (
            await env.mcp.call_tool(
                "search_documents", {"query": "service principal authentication", "format": "json"}
            )
        ).content[0].text
    )
    assert payload["matches"][0]["content"].startswith("The connector supports")


@pytest.mark.anyio
async def test_search_symbols_can_read_what_it_finds(env: ASTGraphServer) -> None:
    payload = json.loads(
        (
            await env.mcp.call_tool(
                "search_symbols",
                {"query": "ApplyAuthentication", "view": "contract", "format": "json"},
            )
        ).content[0].text
    )
    symbol_id = payload["matches"][0]["symbol_id"]
    assert payload["views"][symbol_id]["view"] == "contract"
    assert "token: str" in payload["views"][symbol_id]["signature"]

    text = (
        await env.mcp.call_tool(
            "search_symbols", {"query": "ApplyAuthentication", "view": "implementation"}
        )
    ).content[0].text
    assert "implementation src/spark.py" in text
    assert "return True" in text


def test_extracted_symbol_view_matches_inspect_symbols(env: ASTGraphServer) -> None:
    payload, error = env._symbol_view("src/spark.py::SparkConnectOptions.ApplyAuthentication", "contract")
    assert error is None
    assert payload["view"] == "contract"
    missing, failure = env._symbol_view("src/spark.py::Nope", "contract")
    assert missing is None and "not found" in failure
