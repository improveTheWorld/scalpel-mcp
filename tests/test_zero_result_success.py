"""A zero-result success must SAY WHY IT IS EMPTY -- in every format it can answer in.

Measured first on the 25/09 corpus: 364 `search_symbols` calls came back with count 0,
214 of them with an EMPTY text and 150 with the checked explanation. The 214 are not a
separate bug: the branch that fills the text was introduced by 7c0c5bb (25/09 21:32), and
every one of them is stamped before it while every run after it carries the note (census
of the journals, direct read: benchmarks/results/**/journals/*.jsonl).

What that fix left open is the SECOND format of the same tool. `format="json"` answered
`{"matches": []}` -- no status, no count, no note -- so the very call the text branch
explains was still mute one enum value away. These tests pin the invariant on both
formats: a success with nothing to show names the cause and, when a filter did the
eliminating, names the remedy and the count it removed.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer


def make_server(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "workspace"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "onboarding.md").write_text(
        "# OAuth onboarding\n\nAuthentification OAuth M2M is required for PWAF.\n",
        encoding="utf-8",
    )
    src = ws / "src"
    src.mkdir()
    (src / "billing.py").write_text(
        "class BillingService:\n    def charge(self):\n        pass\n", encoding="utf-8"
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


async def call(server: ASTGraphServer, args: dict) -> Any:
    response = await server.mcp.call_tool("search_symbols", args)
    raw = response.content[0].text
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _nonzero_note(payload: Any) -> str:
    """The explanation of an empty success, whichever field carries it."""
    if isinstance(payload, str):
        assert payload.strip()
        return payload
    assert payload["status"] == "success", payload
    assert payload["count"] == 0, payload
    note = payload.get("text") or payload.get("note") or ""
    assert note.strip(), payload
    return note


@pytest.mark.anyio
async def test_a_zero_result_success_in_text_says_why_and_what_the_filter_removed(
    tmp_path: Path,
) -> None:
    """The reported case: a query scoped to a folder that does not carry it.

    The agent must tell "nothing matches" from "the filter eliminated everything" -- and
    must be told the count the filter removed, because that count is the reason to retry
    without it.
    """
    server = make_server(tmp_path)
    payload = await call(
        server, {"query": "charge", "path": "docs", "view": "implementation"}
    )
    note = _nonzero_note(payload)
    assert "file_pattern 'docs'" in note
    assert "none carries the query" in note
    assert "1 match the same query WITHOUT it" in note
    assert "billing.py" in note
    assert "Retry without file_pattern, or widen it." in note


@pytest.mark.anyio
async def test_a_zero_result_success_in_json_says_why_too(tmp_path: Path) -> None:
    """The hole the text fix left: the same call in JSON was a bare {"matches": []}.

    No status, no count, no note -- the caller could not even tell an empty search from a
    malformed answer.
    """
    server = make_server(tmp_path)
    payload = await call(server, {"query": "NothingAtAllLikeThis", "format": "json"})
    note = _nonzero_note(payload)
    assert "No symbol matches 'NothingAtAllLikeThis'" in note


@pytest.mark.anyio
async def test_a_json_zero_result_under_a_filter_counts_and_removes_the_filter(
    tmp_path: Path,
) -> None:
    server = make_server(tmp_path)
    payload = await call(server, {"query": "charge", "path": "docs", "format": "json"})
    note = _nonzero_note(payload)
    assert "file_pattern 'docs'" in note
    assert "1 match the same query WITHOUT it" in note


@pytest.mark.anyio
async def test_a_json_answer_that_found_something_gains_no_note(tmp_path: Path) -> None:
    """The note explains an EMPTY answer: a full one must not carry it."""
    server = make_server(tmp_path)
    payload = await call(server, {"query": "charge", "format": "json"})
    assert payload["matches"], payload
    assert "note" not in payload


@pytest.mark.anyio
async def test_a_pattern_no_indexed_file_matches_names_the_remedy(tmp_path: Path) -> None:
    """The other half of the complaint: the pattern matched no indexed file at all.

    Told apart from "the files are there but none carries the query", which is a
    different next move.
    """
    server = make_server(tmp_path)
    payload = await call(server, {"query": "charge", "path": "src/*.rb"})
    note = _nonzero_note(payload)
    assert "no indexed file matches that pattern" in note
    assert "1 match the same query WITHOUT it" in note
    assert "Retry without file_pattern, or widen it." in note


@pytest.mark.anyio
async def test_a_pattern_only_unparsed_files_match_says_which_kind_of_file_it_is(
    tmp_path: Path,
) -> None:
    """A .ps1 is on disk but no parser claims it: "check the path" would be false."""
    server = make_server(tmp_path)
    (tmp_path / "workspace" / "run.ps1").write_text("Write-Host 'x'\n", encoding="utf-8")
    payload = await call(server, {"query": "charge", "path": "**/*.ps1"})
    note = _nonzero_note(payload)
    assert "no INDEXED file matches it, but 1 file(s) do on disk" in note


@pytest.mark.anyio
async def test_a_zero_result_without_any_filter_says_what_was_checked(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    payload = await call(server, {"query": "ZzzNotAnything"})
    note = _nonzero_note(payload)
    assert "No symbol matches 'ZzzNotAnything'" in note
    assert "Matching is on names and ids, never on bodies" in note


@pytest.mark.anyio
async def test_the_document_search_json_face_carries_the_same_note(tmp_path: Path) -> None:
    """The sibling tool had the same hole: `search_documents` json returned {"matches": []}.

    195 of the 227 zero-count document searches in the corpus were just as mute as the
    symbol ones -- the note only ever reached the text form.
    """
    server = make_server(tmp_path)
    response = await server.mcp.call_tool(
        "search_documents", {"query": "NothingAtAllLikeThis", "format": "json"}
    )
    payload = json.loads(response.content[0].text)
    assert payload["status"] == "success" and payload["count"] == 0, payload
    assert "No paragraph matches 'NothingAtAllLikeThis'" in payload["note"]

