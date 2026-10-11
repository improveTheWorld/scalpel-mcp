"""An empty symbol search must show what the document search would return.

Measured on the 25/09 corpus (subagent s2): 71 runs each spent a turn asking
search_symbols for a requirement that the Dossier states in prose and that
search_documents already answers with hits. The message said "try
search_documents" without showing the hits, so the next turn guessed.
"""

import json
from pathlib import Path

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
    (src / "unrelated.py").write_text("class Unrelated:\n    pass\n", encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


def _text(response) -> str:
    raw = response.content[0].text
    try:
        data = json.loads(raw)
        return data["text"] if isinstance(data, dict) else str(data)
    except Exception:
        return raw


@pytest.mark.anyio
async def test_an_empty_symbol_search_shows_the_document_hits(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    response = await server.mcp.call_tool("search_symbols", {"query": "OAuth"})
    text = _text(response)
    assert "No symbol matches" in text
    assert "document paragraph(s) do" in text
    assert "onboarding.md" in text


@pytest.mark.anyio
async def test_an_empty_symbol_search_without_document_hits_says_so(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    response = await server.mcp.call_tool("search_symbols", {"query": "ZzzNotAnything"})
    text = _text(response)
    assert "No symbol matches" in text
    assert "No document paragraph carries it either" in text
