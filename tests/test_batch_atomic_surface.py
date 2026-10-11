"""The batch promise ON THE SURFACE: a syntax barrier refuses the WHOLE batch.

Measured on scalpel_16k_round006_ordre1 t15 -> t17. Seven entries went out in ONE call and the
reply was "applied": 1, "refused": 6: the valid entry WAS written, that partial write moved the
file, and the six refusals came back with the expected_hash they had been read against already
dead -- t16 retried and answered "Stale read" five times, and the six only landed at t17.

The rule and its implementation already existed (Patcher._edit_batch, pinned by
tests/test_patcher_w5.py::test_a_batch_with_one_syntax_failure_writes_nothing) but NOTHING CALLED
IT: the tool loop applied the entries one by one. These tests pin the WIRING -- a batch is one
intent, so a mixed batch writes NOTHING -- and the sentence BOTH surfaces tell the model.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer
from ast_graph_server.toolset_schemas import SCALPEL_SCHEMAS


CODE = (
    '"""Billing module."""\n'
    "\n"
    "class BillingService:\n"
    '    """Handles payments."""\n'
    "\n"
    "    def charge(self, user_id: str, amount: float):\n"
    '        """Original charge method."""\n'
    "        return False\n"
    "\n"
    "    def refund(self, user_id: str) -> bool:\n"
    '        """Refund method."""\n'
    "        return True\n"
)

CHARGE = "src/billing.py::BillingService.charge"
REFUND = "src/billing.py::BillingService.refund"

#: Line 3 of a Python block: one parses, the other does not.
VALID = "        return 1 > 0"
REPAIRED = "        return 2 > 0"
BROKEN = "        return (True"

#: The one sentence both surfaces must carry: the mission's "une seule verite".
ALL_OR_NOTHING = "a batch is ALL-OR-NOTHING"


@pytest.fixture
def server(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "billing.py").write_text(CODE, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


def source(server: ASTGraphServer) -> str:
    """The file as it is ON DISK: a claim about a write is checked against the write."""
    return (Path(server.config.workspace_root) / "src" / "billing.py").read_text(encoding="utf-8")


async def call(server: ASTGraphServer, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if tool in ("edit_block", "read_block"):
        arguments = {"format": "json", **arguments}
    answer = await server.mcp.call_tool(tool, arguments)
    return json.loads(answer.content[0].text)


async def block_hash(server: ASTGraphServer, path: str) -> str:
    return (await call(server, "read_block", {"path": path}))["content_hash"]


@pytest.mark.anyio
async def test_a_mixed_batch_writes_nothing_and_keeps_both_hashes_alive(
    server: ASTGraphServer,
) -> None:
    """THE wiring test: 1 valid entry + 1 that breaks the syntax, sent through the SURFACE.

    Without this test Patcher._edit_batch stays dead code: the loop called edit_block entry by
    entry, so the valid entry was written, the file moved, and the refused entries' hashes died.
    """
    before = source(server)
    charge = await block_hash(server, CHARGE)
    refund = await block_hash(server, REFUND)

    payload = await call(
        server,
        "edit_block",
        {
            "edits": [
                {"path": CHARGE, "ops": [{"replace": [3, 3], "content": VALID}], "expected_hash": charge},
                {"path": REFUND, "ops": [{"replace": [3, 3], "content": BROKEN}], "expected_hash": refund},
            ]
        },
    )

    assert payload["status"] == "error", payload
    assert payload["applied"] == 0 and payload["refused"] == 2, payload
    assert source(server) == before, "the mixed batch wrote half of itself: " + source(server)
    # Nothing was written, so every expected_hash the caller sent still opens its block ...
    assert await block_hash(server, CHARGE) == charge
    assert await block_hash(server, REFUND) == refund
    # ... and repairing the entry the reply names is the WHOLE repair: the rest is re-sent as is.
    repaired = await call(
        server,
        "edit_block",
        {
            "edits": [
                {"path": CHARGE, "ops": [{"replace": [3, 3], "content": VALID}], "expected_hash": charge},
                {"path": REFUND, "ops": [{"replace": [3, 3], "content": REPAIRED}], "expected_hash": refund},
            ]
        },
    )
    assert repaired["status"] == "success", repaired
    assert repaired["applied"] == 2 and repaired["refused"] == 0, repaired
    assert VALID in source(server) and REPAIRED in source(server)


@pytest.mark.anyio
async def test_a_batch_of_valid_entries_lands_whole(server: ASTGraphServer) -> None:
    """The other half of the rule: when every entry passes, the batch is applied as sent."""
    charge = await block_hash(server, CHARGE)
    refund = await block_hash(server, REFUND)

    payload = await call(
        server,
        "edit_block",
        {
            "edits": [
                {"path": CHARGE, "ops": [{"replace": [3, 3], "content": VALID}], "expected_hash": charge},
                {"path": REFUND, "ops": [{"replace": [3, 3], "content": REPAIRED}], "expected_hash": refund},
            ]
        },
    )

    assert payload["status"] == "success", payload
    assert payload["applied"] == 2 and payload["refused"] == 0, payload
    assert [one["status"] for one in payload["results"]] == ["success", "success"], payload
    text = source(server)
    assert VALID in text and REPAIRED in text


@pytest.mark.anyio
async def test_a_batch_of_one_entry_keeps_the_single_call_behaviour(
    server: ASTGraphServer,
) -> None:
    """A one-entry batch is the single call it always was: same fields, same write."""
    charge = await block_hash(server, CHARGE)

    payload = await call(
        server,
        "edit_block",
        {
            "edits": [
                {"path": CHARGE, "ops": [{"replace": [3, 3], "content": VALID}], "expected_hash": charge}
            ]
        },
    )

    assert payload["status"] == "success", payload
    assert payload["applied"] == 1 and payload["refused"] == 0, payload
    one = payload["results"][0]
    assert one["status"] == "success", one
    assert "BillingService::charge" in one["block_id"], one
    # The reply still carries the rewritten block and its NEW hash, so the next call needs no read.
    assert one["content_hash"] and one["content_hash"] != charge, one
    assert one["numbered_text"], one
    assert VALID in source(server)
 
 
@pytest.mark.anyio
async def test_batch_edit_default_text_format(server: ASTGraphServer) -> None:
    """When format is omitted, edit_block returns compact text summary."""
    charge = await block_hash(server, CHARGE)
    refund = await block_hash(server, REFUND)
    ans = await server.mcp.call_tool(
        "edit_block",
        {
            "edits": [
                {"path": CHARGE, "ops": [{"replace": [3, 3], "content": VALID}], "expected_hash": charge},
                {"path": REFUND, "ops": [{"replace": [3, 3], "content": REPAIRED}], "expected_hash": refund},
            ]
        },
    )
    text = ans.content[0].text
    assert "[ok] applied 2/2 edits" in text
    assert "BillingService::charge" in text
    assert "BillingService::refund" in text
    assert "[h:" in text



def edit_block_texts(server: ASTGraphServer) -> Dict[str, Any]:
    """Both faces of the contract: what the MODEL reads, and what the SERVER declares."""
    harness = next(spec for spec in SCALPEL_SCHEMAS if spec["function"]["name"] == "edit_block")["function"]
    server_side = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}["edit_block"]
    return {
        "harness tool description": harness["description"],
        "harness edits description": harness["parameters"]["properties"]["edits"]["description"],
        "server tool description": server_side.description,
        "server edits description": (server_side.input_schema or {})["properties"]["edits"]["description"],
    }


def test_both_surfaces_say_a_batch_is_all_or_nothing(server: ASTGraphServer) -> None:
    """The description the MODEL reads said the opposite of the code, on the same two surfaces.

    benchmarks/toolsets.py still promised "an entry that is refused leaves the others applied"
    while the patcher refused the whole batch, and the reply's count was the only way to learn
    what had happened. No existing test pinned that sentence (searched: only the description
    itself carried it), so this one pins the corrected contract on BOTH faces at once.
    """
    texts = edit_block_texts(server)
    for where, text in texts.items():
        assert ALL_OR_NOTHING in text, where + " no longer says " + ALL_OR_NOTHING
        assert "how many entries were applied" in text, where
        assert "an entry that is refused leaves the others applied" not in text, where
    # The two faces say it with the SAME words, not two paraphrases of it.
    for shared in (
        ALL_OR_NOTHING,
        "how many entries were applied",
        "nothing is written for ANY entry",
    ):
        assert shared in texts["harness tool description"], shared
        assert shared in texts["server tool description"], shared
