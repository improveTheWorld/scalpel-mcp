"""get_project_map must stay inside the budget it was given, in the format it answers.

The selection counted only the `path :: title` string of each entry. The text form adds the
section count and a header, the JSON form adds its whole structure: measured on this very
repository, a 200-token request came back ~13% over in text and ~63% over in JSON.
"""
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.indexer import Indexer
from ast_graph_server.server import ASTGraphServer


def _server(tmp_path: Path) -> ASTGraphServer:
    ws = tmp_path / "ws"
    for folder in range(8):
        (ws / f"docs{folder}").mkdir(parents=True)
        (ws / f"src{folder}").mkdir(parents=True)
        for number in range(12):
            (ws / f"docs{folder}" / f"note_{number}.md").write_text(
                f"# A fairly long title for note {folder}-{number}\n\nbody\n", encoding="utf-8"
            )
            (ws / f"src{folder}" / f"module_{number}.py").write_text(
                f"class Service{folder}x{number}:\n    pass\n\n\nclass Helper{folder}x{number}:\n    pass\n",
                encoding="utf-8",
            )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    from ast_graph_server.database import DatabaseManager

    Indexer(config, DatabaseManager(config.db_path)).index_workspace()
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


@pytest.mark.anyio
@pytest.mark.parametrize("answer_format", ["text", "json"])
@pytest.mark.parametrize("budget_tokens", [200, 500, 1000, 2000])
async def test_the_map_stays_inside_the_budget_it_was_given(
    tmp_path: Path, budget_tokens: int, answer_format: str
) -> None:
    server = _server(tmp_path)

    res = await server.mcp.call_tool(
        "get_project_map", {"budget_tokens": budget_tokens, "format": answer_format}
    )
    wire = res.content[0].text

    # ~4 characters a token is the estimate the tool itself uses to turn a budget into characters.
    assert len(wire) / 4 <= budget_tokens, (
        f"{answer_format}: asked {budget_tokens} tokens, got ~{len(wire) // 4} "
        f"({len(wire)} chars, x{len(wire) / 4 / budget_tokens:.2f})"
    )
    # The workspace is bigger than every budget here: a map that was not cut proves nothing.
    assert '"truncated": true' in wire or "truncated: yes" in wire, wire[:200]