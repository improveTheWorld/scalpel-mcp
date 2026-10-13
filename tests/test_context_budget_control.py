import json
from pathlib import Path
import pytest
from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer
from ast_graph_server.indexer import Indexer
from ast_graph_server.database import DatabaseManager


def make_test_server(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    src = ws / "src"
    src.mkdir()
    docs = ws / "docs"
    docs.mkdir()

    for i in range(20):
        (src / f"service_{i}.py").write_text(
            f'"""Module for service {i} operations and management."""\n\n'
            f'class DataService{i}:\n'
            f'    """Service {i} implementation handling queries and transactions."""\n'
            f'    def process_item_{i}(self, item_id: str, count: int) -> bool:\n'
            f'        """Process a batch item for service {i}."""\n'
            f'        return count > 0\n\n'
            f'    def validate_record_{i}(self, record: dict) -> bool:\n'
            f'        """Validate record structure and contents."""\n'
            f'        return bool(record)\n\n'
            f'    def cleanup_expired_{i}(self, max_age: float) -> int:\n'
            f'        """Clean up expired cache records."""\n'
            f'        return 0\n',
            encoding="utf-8",
        )

    (docs / "guide.md").write_text(
        "# Integration Guide\n\n"
        "## Transaction Processing\n\n"
        "Guide on how DataService processes items and transactions.\n",
        encoding="utf-8",
    )

    config = ServerConfig(
        workspace_root=ws,
        db_path=ws / ".agent_graph.db",
    )
    db = DatabaseManager(config.db_path)
    Indexer(config, db).index_workspace()
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


@pytest.mark.anyio
async def test_map_budget_respects_global_envelope(tmp_path: Path) -> None:
    server = make_test_server(tmp_path)
    res = await server.mcp.call_tool(
        "build_context",
        {
            "query": "process_item DataService transaction",
            "budget_tokens": 4000,
            "map": "structure",
            "format": "json",
        },
    )
    payload = json.loads(res.content[0].text)
    assert payload["used_tokens_estimate"] <= 4000
    assert payload["map_tokens"] <= int(4000 * 0.3)
    assert payload["map_chars"] > 0
    assert "[carte]" in payload["text"] or "mini-AST" in payload["text"]
    assert payload["entry_count"] > 0
    assert any("DataService" in e["text"] or "process_item" in e["text"] for e in payload["entries"])


@pytest.mark.anyio
async def test_json_format_respects_budget_without_redundancy(tmp_path: Path) -> None:
    server = make_test_server(tmp_path)
    budget_tokens = 4000
    res = await server.mcp.call_tool(
        "build_context",
        {
            "query": "process_item DataService transaction",
            "budget_tokens": budget_tokens,
            "format": "json",
        },
    )
    payload = json.loads(res.content[0].text)
    assert "context" not in payload
    assert "entries" in payload
    assert "text" in payload

    raw_json_str = res.content[0].text
    estimated_json_tokens = len(raw_json_str) / 1.3
    assert estimated_json_tokens <= budget_tokens * 1.10
    if "json_tokens_estimate" in payload:
        assert payload["json_tokens_estimate"] <= budget_tokens * 1.10


@pytest.mark.anyio
async def test_breadth_ratio_zero_does_not_return_empty(tmp_path: Path) -> None:
    server = make_test_server(tmp_path)
    res = await server.mcp.call_tool(
        "build_context",
        {
            "query": "process_item DataService transaction",
            "breadth_ratio": 0.0,
            "budget_tokens": 4000,
            "format": "json",
        },
    )
    payload = json.loads(res.content[0].text)
    assert payload["entry_count"] > 0
    levels = [e["level"] for e in payload["entries"]]
    assert 3 in levels
    assert payload["entries"][0]["level"] == 3