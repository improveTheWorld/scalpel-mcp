import json
from pathlib import Path
import pytest
from src.ast_graph_server.config import ServerConfig
from src.ast_graph_server.server import ASTGraphServer


def make_test_server(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "workspace"
    src = ws / "src"
    src.mkdir(parents=True)
    code = (
        "def helper_fn():\n"
        "    return 42\n"
        "\n"
        "def main_process():\n"
        "    return helper_fn()\n"
    )
    (src / "app.py").write_text(code, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


@pytest.mark.anyio
async def test_search_symbols_contract_view_exposes_callers(tmp_path: Path) -> None:
    server = make_test_server(tmp_path)
    res = await server.mcp.call_tool(
        "search_symbols",
        {"query": "helper_fn", "view": "contract", "format": "text"},
    )
    text = res.content[0].text
    assert "callers:" in text, f"Attendu 'callers:' dans la sortie, obtenu :\n{text}"
    assert "src/app.py::main_process" in text, text


@pytest.mark.anyio
async def test_search_symbols_contract_view_json_exposes_callers(tmp_path: Path) -> None:
    server = make_test_server(tmp_path)
    res = await server.mcp.call_tool(
        "search_symbols",
        {"query": "helper_fn", "view": "contract", "format": "json"},
    )
    payload = json.loads(res.content[0].text)
    view_payload = next(v for sid, v in payload["views"].items() if "helper_fn" in sid)
    assert "callers" in view_payload, f"Attendu 'callers' dans le view JSON, obtenu : {view_payload}"
    assert "src/app.py::main_process" in view_payload["callers"]


@pytest.mark.anyio
async def test_search_symbols_implementation_view_exposes_callers(tmp_path: Path) -> None:
    server = make_test_server(tmp_path)
    res = await server.mcp.call_tool(
        "search_symbols",
        {"query": "helper_fn", "view": "implementation", "format": "text"},
    )
    text = res.content[0].text
    assert "callers:" in text, f"Attendu 'callers:' dans l'en-tête d'implémentation, obtenu :\n{text}"
    assert "src/app.py::main_process" in text, text