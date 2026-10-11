"""Integration tests for ASTGraphServer MCP tools."""

from pathlib import Path
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer


@pytest.fixture
def test_server(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "workspace"
    ws.mkdir()
    src = ws / "src"
    src.mkdir()

    billing_file = src / "billing.py"
    billing_file.write_text(
        '''class BillingService:
    def charge(self, user_id: str, amount: float):
        """Process charge."""
        if not self.validate_user(user_id):
            return None
        return "receipt_123"

    def validate_user(self, user_id: str) -> bool:
        """Verify user status."""
        return True
''',
        encoding="utf-8",
    )

    gateway_file = src / "stripe.py"
    gateway_file.write_text(
        '''class StripeGateway:
    def post_charge(self, amount: float):
        """Post charge to Stripe."""
        return True
''',
        encoding="utf-8",
    )

    config = ServerConfig(
        workspace_root=ws,
        db_path=ws / ".agent_graph.db",
        max_implementation_batch=2,
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


@pytest.mark.anyio
async def test_search_symbols_tool(test_server: ASTGraphServer) -> None:
    res = await test_server.mcp.call_tool("search_symbols", {"query": "charge", "format": "json"})
    # Result text is a JSON string
    import json
    data = json.loads(res.content[0].text)
    matches = data["matches"]
    assert len(matches) >= 2  # charge and post_charge
    sym_ids = [m["symbol_id"] for m in matches]
    assert "src/billing.py::BillingService.charge" in sym_ids


@pytest.mark.anyio
async def test_a_search_never_answers_a_match_it_returns_without_its_source(
    test_server: ASTGraphServer,
) -> None:
    """`limit` is the ceiling -- not the hidden cap this fixture still configures (2).

    Two defects met here, and both are pinned by measurement. First, a search answered every
    match it skipped with an `[error] implementation view limit exceeded` line: a query matching
    50 symbols produced 45 of them for symbols the caller never asked about, burying the two it
    wanted (declared on mapab2 a r1 of the base 9662958). Second, and worse, view='implementation'
    promised the source of what it found while a hidden cap of `max_implementation_batch`
    silently answered 3 of them whatever `limit` said: 31 replies announced omitted
    implementations, 148 were left out, in 17 of the 20 scalpel runs of registered_v2 -- and the
    three calls that followed were always reads of the omitted ones.
    """
    import json

    res = await test_server.mcp.call_tool(
        "search_symbols", {"query": "service", "view": "implementation", "limit": 10}
    )
    text = res.content[0].text
    assert "limit exceeded" not in text
    assert "[error]" not in text
    assert "came back without their implementation" not in text
    assert text.count(" | hash ") >= 3
    # And the paths are still there to be read directly -- printed as the surface prints
    # them (an ancestry chain with `::`), not as the stored id (`BillingService.charge`,
    # which is a coordinate the caller cannot retype).
    assert "src/billing.py::BillingService::charge" in text

@pytest.mark.anyio
async def test_implementation_view_is_directly_editable(test_server: ASTGraphServer) -> None:
    """The implementation view carries read_block's numbers and hash: edit, no read."""
    import json

    res = await test_server.mcp.call_tool(
        "read_block", {"paths": ["src/billing.py::BillingService.charge"], "format": "json"}
    )
    payload = json.loads(res.content[0].text)["blocks"][0]
    block = test_server.patcher.read_block("src/billing.py::BillingService.charge")
    assert payload["numbered_text"] == block["numbered_text"]
    assert payload["content_hash"] == block["content_hash"]
    assert payload["line_count"] == block["line_count"]

    # Edit straight from the discovery payload: the numbers and the hash are the ones
    # edit_block expects, so no read_block is inserted between the read and the write.
    target = next(line for line in payload["numbered_text"].splitlines() if "receipt_123" in line)
    line_number = int(target.split("|", 1)[0].strip())
    out = await test_server.mcp.call_tool(
        "edit_block",
        {
            "path": "src/billing.py::BillingService.charge",
            "expected_hash": payload["content_hash"],
            "ops": [
                {"replace": [line_number, line_number], "content": '        return "receipt_456"'}
            ],
            "format": "json",
        },
    )
    edited = json.loads(out.content[0].text)
    assert edited["status"] == "success", edited


@pytest.mark.anyio
async def test_read_block_answers_a_numbered_block_as_text_by_default(test_server: ASTGraphServer) -> None:
    """A symbol or a section is TEXT on the wire (header, then numbered lines), not escaped JSON.

    `format` used to be honoured by LISTINGS only: a block always came back as a JSON envelope
    whose line breaks and quotes were escaped. `format='json'` still returns the structure.
    """
    import json

    address = "src/billing.py::BillingService.charge"
    block = test_server.patcher.read_block(address)

    res = await test_server.mcp.call_tool("read_block", {"path": address})
    text = res.content[0].text
    header, body = text.split("\n", 1)
    assert not text.lstrip().startswith("{"), text
    assert "src/billing.py::BillingService::charge" in header and f"[h:{block['content_hash']}]" in header, header
    assert body.startswith(block["numbered_text"].splitlines()[0]), body
    assert "receipt_123" in body

    structured = json.loads(
        (await test_server.mcp.call_tool("read_block", {"path": address, "format": "json"})).content[0].text
    )
    assert structured["content_hash"] == block["content_hash"]
    assert structured["numbered_text"] == block["numbered_text"]


@pytest.mark.anyio
async def test_search_implementation_text_is_numbered(test_server: ASTGraphServer) -> None:
    """The text form the agent actually reads shows the numbers and the hash."""
    res = await test_server.mcp.call_tool(
        "search_symbols",
        {"query": "charge", "view": "implementation", "format": "text", "limit": 1},
    )
    text = res.content[0].text
    assert " | hash " in text
    assert "1 | " in text

@pytest.mark.anyio
async def test_update_and_insert_tools(test_server: ASTGraphServer) -> None:
    import json
    # Update tool
    new_code = '''def charge(self, user_id: str, amount: float):
        """Updated charge."""
        return "new_receipt"'''

    read_res = await test_server.mcp.call_tool(
        "read_block", {"path": "src/billing.py::BillingService.charge", "format": "json"}
    )
    block = json.loads(read_res.content[0].text)
    assert block["status"] == "success"
    assert block["numbered_text"].startswith("1 | ")

    update_res = await test_server.mcp.call_tool(
        "edit_block",
        {
            "path": "src/billing.py::BillingService.charge",
            "expected_hash": block["content_hash"],
            "ops": [{"replace": [1, block["line_count"]], "content": new_code}],
            "format": "json",
        },
    )
    data = json.loads(update_res.content[0].text)
    assert data["status"] == "success", data
    assert data["content_hash"] != block["content_hash"]

    # Insert tool
    insert_res = await test_server.mcp.call_tool(
        "insert_block",
        {
            "parent": "src/billing.py::BillingService",
            "text": "def refund(self): return True",
        },
    )
    ins_data = json.loads(insert_res.content[0].text)
    assert ins_data["status"] == "success"

    # `manage_imports` is gone: imports are an ordinary block the agent reads and edits.
