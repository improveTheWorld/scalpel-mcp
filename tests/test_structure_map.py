"""The map must carry the SAME facts read_block returns, or the A/B says nothing.

A card that only named the files still left the arm that got it reading every file by
hand to learn its structure: the comparison was then between two ways of discovering,
not between a card and manual discovery.
"""

from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.context_builder import ContextBuilder
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer


def make_env(base: Path):
    ws = base / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "src" / "widget.py").write_text(
        "class Widget:\n    def render(self):\n        return 1\n", encoding="utf-8"
    )
    (ws / "docs" / "guide.md").write_text("# Guide\n\nintro\n\n## Install\n\ncorps\n", encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    Indexer(config, db).index_workspace()
    return ws, config, db


def test_the_default_map_is_still_names_only(tmp_path: Path) -> None:
    ws, config, db = make_env(tmp_path)

    payload = ContextBuilder(config, db).build(
        query="widget render install", budget_tokens=1500, map_tokens=6000
    )

    assert "[carte]" in payload["text"]
    assert "mini-AST" not in payload["text"]
    assert payload["map_structure_files"] == 0


def test_the_structure_map_carries_the_ids_and_spans_read_block_returns(tmp_path: Path) -> None:
    ws, config, db = make_env(tmp_path)

    payload = ContextBuilder(config, db).build(
        query="widget render install", budget_tokens=1500, map_tokens=6000,
        map_detail="structure",
    )
    text = payload["text"]

    assert "[carte]" in text, "the whole tree stays in the card"
    assert "mini-AST" in text
    assert "src/widget.py::Widget" in text
    assert "src/widget.py::Widget.render" in text
    assert "docs/guide.md#install" in text
    assert "Install" in text
    assert payload["map_structure_files"] >= 1
    assert payload["map_structure_truncated"] in (True, False)


def test_the_structure_map_stays_inside_its_budget(tmp_path: Path) -> None:
    """The map is bounded by construction: a full mini-AST of the repository is ~209k tokens."""
    ws, config, db = make_env(tmp_path)

    payload = ContextBuilder(config, db).build(
        query="widget render install", budget_tokens=1500, map_tokens=1500,
        map_detail="structure",
    )

    assert payload["map_chars"] <= int(1500 * ContextBuilder.CHARS_PER_TOKEN) + 1
    assert payload["map_tokens"] == 1500