"""A refusal must describe the call the caller actually sent -- and the write that followed.

Every case here is one the registered_v2 campaign met, quoted from the raw journals:

  - a batch whose `applied` was 0 announced "the others were applied" (scalpel_16k_round016
    t16, round001 t19, round003 t19), beside a warning that said "nothing else was applied";
    the agent wrote the contradiction into its own report;
  - the form refusal read "Neither form was complete, so nothing was written" on four calls
    (round004 t29, round005 t37, round013 t24, round019 t23) whose arguments had reached the
    tool as an EMPTY map -- benchmarks/agent.py passes `{}` when the call's JSON does not
    parse -- so the sentence described a shape the caller had not sent;
  - view='implementation' announced a cap of 3 implementations per call that `limit` could
    not raise: 31 replies, 148 implementations omitted, in 17 of the 20 scalpel runs, where
    the three calls that followed were always reads of what had been omitted.
"""

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer


GUIDE = "# Guide\n\n## Billing\ncorps\n"

CODE = (
    "class Alpha:\n"
    "    def charge_one(self):\n        return 1\n\n"
    "    def charge_two(self):\n        return 2\n\n"
    "    def charge_three(self):\n        return 3\n\n"
    "    def charge_four(self):\n        return 4\n"
)


@pytest.fixture
def server(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "src").mkdir()
    (ws / "docs" / "guide.md").write_text(GUIDE, encoding="utf-8")
    (ws / "src" / "billing.py").write_text(CODE, encoding="utf-8")
    #: The cap the campaign met is CONFIGURED (2): a reply that still caps below the caller's
    #: `limit` is caught by the code, not by the default happening to be generous.
    config = ServerConfig(
        workspace_root=ws, db_path=ws / ".agent_graph.db", max_implementation_batch=2
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


def guide_text(server: ASTGraphServer) -> str:
    """The file as it is ON DISK: a claim about a write is checked against the write."""
    return (Path(server.config.workspace_root) / "docs" / "guide.md").read_text(encoding="utf-8")


async def call(server: ASTGraphServer, tool: str, arguments: Dict[str, Any]) -> Any:
    if tool == "edit_block":
        arguments = {"format": "json", **arguments}
    answer = await server.mcp.call_tool(tool, arguments)
    raw = answer.content[0].text
    try:
        return json.loads(raw)
    except Exception:
        return raw


IDENT = "docs/guide.md::Guide::Billing"


@pytest.mark.anyio
async def test_a_batch_with_applied_zero_never_says_an_entry_was_applied(
    server: ASTGraphServer,
) -> None:
    """measured on round016 t16: applied 0, refused 2, and every hint said "the others were applied"."""
    payload = await call(
        server,
        "edit_block",
        {
            "path": IDENT,
            "edits": [
                {"ops": [{"replace": [2, 2], "content": "corps un"}]},
                {"ops": [{"replace": [2, 2], "content": "corps deux"}]},
            ],
        },
    )
    assert payload["applied"] == 0 and payload["refused"] == 2, payload
    hints = [payload["actionable_hint"]] + [one["actionable_hint"] for one in payload["results"]]
    for hint in hints:
        assert "the others were applied" not in hint, hint
        assert "the other 0" not in hint, hint
        assert "nothing was written" in hint.lower(), hint
    # ... and the sentence is TRUE: nothing was written.
    assert guide_text(server) == GUIDE


@pytest.mark.anyio
async def test_a_batch_that_did_apply_still_says_how_many(server: ASTGraphServer) -> None:
    """The other side of the same sentence: the hint must READ the count, not drop the news."""
    payload = await call(
        server,
        "edit_block",
        {
            "edits": [
                {"path": IDENT, "ops": [{"replace": [2, 2], "content": "corps ecrit"}]},
                {"ops": [{"replace": [2, 2], "content": "jamais ecrit"}]},
            ]
        },
    )
    assert payload["applied"] == 1 and payload["refused"] == 1, payload
    hint = payload["results"][1]["actionable_hint"]
    assert "1 of the 2 entries of this call were applied" in hint, hint
    assert "corps ecrit" in guide_text(server) and "jamais ecrit" not in guide_text(server)


@pytest.mark.anyio
async def test_the_form_refusal_names_the_keys_received_and_the_one_in_excess(
    server: ASTGraphServer,
) -> None:
    """An agent asked for exactly this: "found 2 path keys; use edits=[...] for several blocks"."""
    payload = await call(server, "edit_block", {"path": IDENT, "edits": []})
    error = payload["error"]
    assert payload["status"] == "error"
    assert "received path, edits" in error, error
    assert "missing ops" in error, error
    assert "in excess" in error, error
    assert "Neither form was complete" not in error, error


@pytest.mark.anyio
async def test_an_empty_call_says_that_nothing_arrived(server: ASTGraphServer) -> None:
    """The four campaign sites: the argument map was empty, and the refusal blamed the SHAPE."""
    payload = await call(server, "edit_block", {})
    assert payload["status"] == "error"
    error = payload["error"]
    assert "NONE of its keys" in error, error
    for key in ("path", "ops", "expected_hash", "edits"):
        assert key in error, (key, error)
    assert "Neither form was complete" not in error, error
    assert "resend it whole" in payload["actionable_hint"], payload["actionable_hint"]


@pytest.mark.anyio
async def test_a_partial_single_block_call_names_the_missing_key(server: ASTGraphServer) -> None:
    payload = await call(server, "edit_block", {"path": IDENT, "expected_hash": "x"})
    error = payload["error"]
    assert "received path, expected_hash" in error, error
    assert "missing ops" in error, error
    assert "Nothing was written" in error, error


@pytest.mark.anyio
async def test_every_match_returned_comes_back_with_its_implementation(
    server: ASTGraphServer,
) -> None:
    """`limit` is the ceiling; a configured batch of 2 must not answer a call that asked for 10."""
    payload = await call(
        server,
        "search_symbols",
        {"query": "charge", "view": "implementation", "limit": 10, "format": "json"},
    )
    matches = payload["matches"]
    assert len(matches) >= 4, payload
    assert len(payload["views"]) == len(matches), payload
    assert not payload.get("errors"), payload.get("errors")

    text = await call(
        server,
        "search_symbols",
        {"query": "charge", "view": "implementation", "limit": 10, "format": "text"},
    )
    raw_text = text["text"] if isinstance(text, dict) else text
    assert "came back without their implementation" not in raw_text, raw_text

    # What the model reads before the call says the same thing the code does.
    registered = {one.name: one.parameters for one in server.mcp._tool_manager.list_tools()}
    described = registered["search_symbols"]["properties"]["view"]["description"]
    assert "every match" in described.lower(), described
    assert "cap" not in described.lower(), described
