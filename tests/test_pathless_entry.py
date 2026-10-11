"""A batch entry that carries its stamp and no path is served when ONE live block owns the stamp.

Measured on the campaign journals (03/10/2026): 17 entries of 9 calls were refused with "entry N
of edits has no path" although every one carried an expected_hash. The hash names a block only
when it names exactly one: a hash shared by two blocks, or by none, keeps the refusal.
"""

import json
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer

GUIDE = (
    "# Guide\n\n## Billing\n\nInvoices are issued monthly.\n\n"
    "## Support\n\nContact the service desk.\n"
)
#: Two sections whose TEXT is byte-identical: their stamps are the same, so a stamp alone does
#: not say which one is meant.
TWINS = "# Twins\n\n## Notes\n\nsame text\n\n## Notes\n\nsame text\n\n## End\n\nz\n"


@pytest.fixture
def env(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "guide.md").write_text(GUIDE, encoding="utf-8")
    (ws / "docs" / "twins.md").write_text(TWINS, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


async def _call(env: ASTGraphServer, tool: str, args: dict) -> dict:
    if tool == "read_block":
        args = {"format": "json", **args}
    return json.loads((await env.mcp.call_tool(tool, args)).content[0].text)


@pytest.mark.anyio
async def test_pathless_entries_with_a_unique_stamp_are_served_and_say_so(env: ASTGraphServer) -> None:
    billing = "docs/guide.md::Guide::Billing"
    support = "docs/guide.md::Guide::Support"
    read = await _call(env, "read_block", {"paths": [billing, support]})
    hashes = {block["id"]: block["content_hash"] for block in read["blocks"]}

    payload = await _call(env, "edit_block", {"edits": [
        {"ops": [{"replace": [3, 3], "content": "Invoices are issued weekly."}],
         "expected_hash": hashes[billing]},
        {"ops": [{"replace": [3, 3], "content": "Contact the help desk."}],
         "expected_hash": hashes[support]},
    ], "format": "json"})

    assert payload["applied"] == 2 and payload["refused"] == 0, payload
    warnings = " ".join(payload.get("warnings") or [])
    assert "entry 0 of edits carried no path" in warnings and billing in warnings, payload
    assert "entry 1 of edits carried no path" in warnings and support in warnings, payload
    text = (env.config.workspace_root / "docs" / "guide.md").read_text(encoding="utf-8")
    assert "weekly" in text and "help desk" in text, text


@pytest.mark.anyio
async def test_a_stamp_shared_by_two_blocks_keeps_the_refusal(env: ASTGraphServer) -> None:
    """The DANGEROUS neighbour: writing to a guessed copy would edit the wrong block."""
    path = env.config.workspace_root / "docs" / "twins.md"
    before = path.read_text(encoding="utf-8")
    read = await _call(env, "read_block", {"paths": ["docs/twins.md::Twins::Notes"]})
    stamp = read["blocks"][0]["content_hash"]
    # Two blocks really hold this very text: the hash is ambiguous by construction.
    assert before.count("## Notes\n\nsame text") == 2
    assert env.patcher._unique_owner_of_hash(stamp) is None

    payload = await _call(env, "edit_block", {"edits": [
        {"ops": [{"replace": [3, 3], "content": "changed"}], "expected_hash": stamp},
        {"ops": [{"replace": [3, 3], "content": "changed"}], "expected_hash": stamp},
    ]})

    assert payload["applied"] == 0, payload
    assert all("has no path" in one["error"] for one in payload["results"]), payload
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.anyio
async def test_a_stamp_that_matches_no_block_keeps_the_refusal(env: ASTGraphServer) -> None:
    path = env.config.workspace_root / "docs" / "guide.md"
    before = path.read_text(encoding="utf-8")
    payload = await _call(env, "edit_block", {"edits": [
        {"ops": [{"replace": [3, 3], "content": "x"}], "expected_hash": "0" * 64},
        {"ops": [{"replace": [3, 3], "content": "y"}], "expected_hash": "1" * 64},
    ]})
    assert payload["applied"] == 0, payload
    assert all("has no path" in one["error"] for one in payload["results"]), payload
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.anyio
async def test_a_stale_index_row_is_not_trusted(env: ASTGraphServer) -> None:
    """The index says block X has this stamp, the disk no longer does: no inference."""
    path = env.config.workspace_root / "docs" / "guide.md"
    read = await _call(env, "read_block", {"paths": ["docs/guide.md::Guide::Billing"]})
    stamp = read["blocks"][0]["content_hash"]
    path.write_text(GUIDE.replace("monthly", "yearly"), encoding="utf-8")  # index not refreshed
    assert env.patcher._unique_owner_of_hash(stamp) is None
