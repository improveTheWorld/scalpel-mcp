import json
from pathlib import Path
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.relevance import BM25Backend
from ast_graph_server.server import ASTGraphServer


def test_default_ignore_patterns_excludes_reports_and_transcripts(tmp_path: Path) -> None:
    config = ServerConfig(workspace_root=tmp_path, db_path=tmp_path / ".agent_graph.db")
    indexer = Indexer(config, DatabaseManager(config.db_path))

    # Folders and paths that must be ignored by default
    assert indexer.should_ignore_relative("reports")
    assert indexer.should_ignore_relative("reports/run_01.json")
    assert indexer.should_ignore_relative("transcripts")
    assert indexer.should_ignore_relative("transcripts/session_01.json")
    assert indexer.should_ignore_relative("benchmarks/results")
    assert indexer.should_ignore_relative("benchmarks/results/summary.json")

    # Regular source and doc paths should NOT be ignored
    assert not indexer.should_ignore_relative("src/main.py")
    assert not indexer.should_ignore_relative("docs/readme.md")


def test_semantic_weighting_boosts_code_over_json_properties(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = DatabaseManager(db_path)

    # Insert two items with the exact same title & body terms so their raw BM25 is identical
    entries = [
        {
            "kind": "property",
            "ref_id": "config.json::process_payment",
            "file_path": "config.json",
            "title": "process_payment",
            "body": "process_payment handler",
        },
        {
            "kind": "function",
            "ref_id": "src/billing.py::process_payment",
            "file_path": "src/billing.py",
            "title": "process_payment",
            "body": "process_payment handler",
        },
    ]
    db.add_search_entries(entries)

    backend = BM25Backend(db)
    results = backend.search("process_payment", limit=10)

    assert len(results) >= 2
    # Function must rank first due to x1.5 boost vs x0.3 penalty
    assert results[0]["ref_id"] == "src/billing.py::process_payment"
    assert results[0]["kind"] == "function"
    assert results[1]["ref_id"] == "config.json::process_payment"
    assert results[1]["kind"] == "property"

    # Score of function should be ~ 5x the score of property (1.5 / 0.3 = 5.0)
    ratio = results[0]["score"] / results[1]["score"]
    assert pytest.approx(5.0, rel=1e-2) == ratio


@pytest.mark.anyio
async def test_default_max_entries_per_file_preserves_diversity(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    docs = ws / "docs"
    docs.mkdir()
    src = ws / "src"
    src.mkdir()

    # Create a large document with 10 sections matching 'payment'
    doc_lines = ["# Payment Architecture\n\n"]
    for i in range(10):
        doc_lines.append(f"## Section {i} payment handling\n\nDetailed payment processing logic {i}.\n\n")
    (docs / "payment_guide.md").write_text("".join(doc_lines), encoding="utf-8")

    # Create a code file also matching 'payment'
    (src / "payment_service.py").write_text(
        "class PaymentService:\n"
        "    def process_payment(self):\n"
        "        pass\n",
        encoding="utf-8",
    )

    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    # Call build_context WITHOUT specifying max_entries_per_file
    res = await server.mcp.call_tool(
        "build_context",
        {
            "query": "payment",
            "budget_tokens": 8000,
            "min_score": 0.0,
            "format": "json",
        },
    )
    payload = json.loads(res.content[0].text)
    entries = payload.get("entries", [])
    assert len(entries) > 0

    # Count entries per file
    file_counts = {}
    for entry in entries:
        ref_id = entry.get("ref_id", "")
        file_path = ref_id.split("::")[0].split("#")[0]
        file_counts[file_path] = file_counts.get(file_path, 0) + 1

    # Verify no file exceeds default limit of 5
    for file_path, count in file_counts.items():
        assert count <= 5, f"File {file_path} exceeded default max_entries_per_file (got {count} > 5)"

    # Verify diversity: payment_guide.md has at most 5 entries
    guide_count = sum(1 for e in entries if "payment_guide.md" in e.get("ref_id", ""))
    assert 0 < guide_count <= 5