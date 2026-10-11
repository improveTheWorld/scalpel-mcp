"""Several titles ADDED in one edit_block call (refusals measured on the campaign, 03/10/2026).

Two shapes, tested separately because they take two different routes:

  * NESTED titles (deeper than the block) added at the block's end: the outline guard used to
    let exactly one through and refuse two or more ("one edit may add at most one");
  * a title at the block's own level followed by deeper ones, and consecutive sibling titles:
    `_title_route` used to serve exactly one title.

In both, the call is served only when it ADDS and nothing else, at the end of the block; the
neighbours that rewrite text, land mid-block or repeat a title stay refused.
"""

from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

DOC = (
    "# Guide\n\n"
    "## Deploy\n\nIntro line.\n\nMore intro.\n\n"
    "## Next\n\nTail.\n"
)
DEPLOY = "docs/g.md::Guide::Deploy"


def make_env(tmp_path: Path, source: str = DOC):
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "g.md").write_text(source, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, db, Patcher(config, db, indexer)


def _text(ws: Path) -> str:
    return (ws / "docs" / "g.md").read_text(encoding="utf-8")


def _headings(db: DatabaseManager) -> list:
    return [row["heading"] for row in db.get_document_sections("docs/g.md")]


# ----------------------------------------------------------------------------- nested titles


def test_two_nested_titles_added_at_the_end_are_served(tmp_path: Path) -> None:
    """Real case (p1_r02, p1_r08, s10 r5/r7/r8): 0 nested before, 2 after, insert_after at the end."""
    ws, db, patcher = make_env(tmp_path)
    read = patcher.read_block(DEPLOY)
    content = "### Auth\n\nOAuth text.\n\n### Telemetry\n\nUser agent text.\n"

    result = patcher.edit_block(
        DEPLOY,
        ops=[{"insert_after": read["line_count"], "content": content}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    assert _headings(db) == ["Guide", "Deploy", "Auth", "Telemetry", "Next"]
    text = _text(ws)
    assert text.index("More intro.") < text.index("### Auth") < text.index("### Telemetry") < text.index("## Next")


def test_a_title_with_its_own_children_is_served_as_nested_additions(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    read = patcher.read_block(DEPLOY)
    content = "### Auth\n\nA.\n\n#### M2M\n\nB.\n\n#### U2M\n\nC.\n"

    result = patcher.edit_block(
        DEPLOY,
        ops=[{"insert_after": read["line_count"], "content": content}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    assert _headings(db) == ["Guide", "Deploy", "Auth", "M2M", "U2M", "Next"]


def test_nested_titles_mixed_with_a_rewrite_stay_refused(tmp_path: Path) -> None:
    """DANGEROUS neighbour: the call also rewrites a line, so it is not a pure addition."""
    ws, db, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(DEPLOY)
    result = patcher.edit_block(
        DEPLOY,
        ops=[
            {"replace": [3, 3], "content": "Rewritten intro."},
            {"insert_after": read["line_count"], "content": "### A\n\nx\n\n### B\n\ny\n"},
        ],
        expected_hash=read["content_hash"],
    )
    assert result["status"] == "error", result
    assert "changes the outline" in result["error"], result
    assert _text(ws) == before


def test_nested_titles_inserted_mid_block_stay_refused(tmp_path: Path) -> None:
    """DANGEROUS neighbour: not at the end, the block's own lines would fall under the new titles."""
    ws, db, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(DEPLOY)
    result = patcher.edit_block(
        DEPLOY,
        ops=[{"insert_after": 3, "content": "### A\n\nx\n\n### B\n\ny\n"}],
        expected_hash=read["content_hash"],
    )
    assert result["status"] == "error", result
    assert _text(ws) == before


def test_the_same_nested_title_twice_stays_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(DEPLOY)
    result = patcher.edit_block(
        DEPLOY,
        ops=[{"insert_after": read["line_count"], "content": "### A\n\nx\n\n### A\n\ny\n"}],
        expected_hash=read["content_hash"],
    )
    assert result["status"] == "error", result
    assert _text(ws) == before


# ----------------------------------------------------------------------------- same-level titles


def test_a_sibling_title_with_its_subtitles_is_served(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    read = patcher.read_block(DEPLOY)
    content = "## Ops\n\nO.\n\n### Logs\n\nL.\n\n### Metrics\n\nM.\n"

    result = patcher.edit_block(
        DEPLOY,
        ops=[{"insert_after": read["line_count"], "content": content}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    assert _headings(db) == ["Guide", "Deploy", "Ops", "Logs", "Metrics", "Next"]


def test_consecutive_sibling_titles_are_served(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    read = patcher.read_block(DEPLOY)
    content = "## A\n\na.\n\n## B\n\nb.\n\n## C\n\nc.\n\n## D\n\nd.\n"

    result = patcher.edit_block(
        DEPLOY,
        ops=[{"insert_after": read["line_count"], "content": content}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    assert _headings(db) == ["Guide", "Deploy", "A", "B", "C", "D", "Next"]
    text = _text(ws)
    assert text.index("## D") < text.index("## Next")


def test_a_title_above_the_block_level_stays_refused(tmp_path: Path) -> None:
    """DANGEROUS neighbour: a level-1 title would not be a sibling of the block."""
    ws, db, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(DEPLOY)
    result = patcher.edit_block(
        DEPLOY,
        ops=[{"insert_after": read["line_count"], "content": "## A\n\na.\n\n# Top\n\nt.\n"}],
        expected_hash=read["content_hash"],
    )
    assert result["status"] == "error", result
    assert _text(ws) == before


def test_siblings_mixed_with_a_rewrite_stay_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(DEPLOY)
    result = patcher.edit_block(
        DEPLOY,
        ops=[
            {"replace": [3, 3], "content": "Rewritten intro."},
            {"insert_after": read["line_count"], "content": "## A\n\na.\n\n## B\n\nb.\n"},
        ],
        expected_hash=read["content_hash"],
    )
    assert result["status"] == "error", result
    assert _text(ws) == before


def test_siblings_clashing_with_an_existing_title_leave_the_file_untouched(tmp_path: Path) -> None:
    """DANGEROUS neighbour: the 2nd sibling repeats a title; whatever is refused, nothing is written."""
    ws, db, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(DEPLOY)
    result = patcher.edit_block(
        DEPLOY,
        ops=[{"insert_after": read["line_count"], "content": "## A\n\na.\n\n## A\n\nb.\n"}],
        expected_hash=read["content_hash"],
    )
    assert result["status"] == "error", result
    assert _text(ws) == before
    assert _headings(db) == ["Guide", "Deploy", "Next"]


def test_siblings_inserted_mid_block_stay_refused(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(DEPLOY)
    result = patcher.edit_block(
        DEPLOY,
        ops=[{"insert_after": 3, "content": "## A\n\na.\n\n## B\n\nb.\n"}],
        expected_hash=read["content_hash"],
    )
    assert result["status"] == "error", result
    assert _text(ws) == before
