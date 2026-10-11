"""Tests for targeted batch error reporting per entry index and path."""
import json
from pathlib import Path
import pytest
from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer

CODE = (
    "class BillingService:\n"
    "    def charge(self, amount: int) -> bool:\n"
    "        return amount > 0\n"
    "\n"
    "    def refund(self, amount: int) -> bool:\n"
    "        return amount > 0\n"
    "\n"
    "    def balance(self) -> int:\n"
    "        return 100\n"
)

CHARGE = "src/billing.py::BillingService.charge"
REFUND = "src/billing.py::BillingService.refund"
BALANCE = "src/billing.py::BillingService.balance"


@pytest.fixture
def server(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "billing.py").write_text(CODE, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    s = ASTGraphServer(config)
    s.initialize(enable_watcher=False)
    return s


async def get_hash(server: ASTGraphServer, path: str) -> str:
    res = await server.mcp.call_tool("read_block", {"path": path, "format": "json"})
    data = json.loads(res.content[0].text)
    return data["content_hash"]


@pytest.mark.anyio
async def test_batch_partial_failure_text_format(server: ASTGraphServer) -> None:
    charge_hash = await get_hash(server, CHARGE)
    balance_hash = await get_hash(server, BALANCE)

    edits = [
        {"path": CHARGE, "ops": [{"replace": [2, 2], "content": "        return amount > 10"}], "expected_hash": charge_hash},
        {"path": REFUND, "ops": [{"replace": [2, 2], "content": "        return amount > 20"}], "expected_hash": "bad_hash_invalid"},
        {"path": BALANCE, "ops": [{"replace": [2, 2], "content": "        return 200"}], "expected_hash": balance_hash},
    ]

    text_ans = await server.mcp.call_tool("edit_block", {"edits": edits})
    text = text_ans.content[0].text

    assert "[ok] applied 2/3 edits (1 refused):" in text
    assert "[refused] entry 1" in text
    assert "BillingService::refund" in text


@pytest.mark.anyio
async def test_batch_partial_failure_json_format(server: ASTGraphServer) -> None:
    charge_hash = await get_hash(server, CHARGE)
    balance_hash = await get_hash(server, BALANCE)

    edits = [
        {"path": CHARGE, "ops": [{"replace": [2, 2], "content": "        return amount > 10"}], "expected_hash": charge_hash},
        {"path": REFUND, "ops": [{"replace": [2, 2], "content": "        return amount > 20"}], "expected_hash": "bad_hash_invalid"},
        {"path": BALANCE, "ops": [{"replace": [2, 2], "content": "        return 200"}], "expected_hash": balance_hash},
    ]

    json_ans = await server.mcp.call_tool("edit_block", {"edits": edits, "format": "json"})
    data = json.loads(json_ans.content[0].text)

    assert data["applied"] == 2
    assert data["refused"] == 1
    assert len(data["results"]) == 3
    assert data["results"][0]["status"] == "success"
    assert data["results"][0].get("entry_index") == 0
    assert data["results"][1]["status"] == "error"
    assert data["results"][1].get("entry_index") == 1
    assert data["results"][2]["status"] == "success"
    assert data["results"][2].get("entry_index") == 2


@pytest.mark.anyio
async def test_batch_all_refused_names_entry_indices(server: ASTGraphServer) -> None:
    edits = [
        {"path": CHARGE, "ops": [{"replace": [2, 2], "content": "        return amount > 10"}], "expected_hash": "bad1"},
        {"path": REFUND, "ops": [{"replace": [2, 2], "content": "        return amount > 20"}], "expected_hash": "bad2"},
    ]

    json_ans = await server.mcp.call_tool("edit_block", {"edits": edits, "format": "json"})
    data = json.loads(json_ans.content[0].text)

    assert data["applied"] == 0
    assert data["refused"] == 2
    assert "entry 0" in (data.get("error", "") + data.get("actionable_hint", ""))
    assert "entry 1" in (data.get("error", "") + data.get("actionable_hint", ""))
    assert data["results"][0].get("entry_index") == 0
    assert data["results"][1].get("entry_index") == 1