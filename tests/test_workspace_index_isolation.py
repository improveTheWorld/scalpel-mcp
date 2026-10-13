from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.documents import DocumentSection, ParsedDocument
from ast_graph_server.indexer import Indexer
from ast_graph_server.parsers.base import SymbolInfo
from ast_graph_server.server import ASTGraphServer


def test_workspace_index_isolation_and_orphan_purge(tmp_path: Path) -> None:
    """Ensure phantom entries from a parent workspace or deleted files are isolated and purged."""
    ws = tmp_path / "scalpel-mcp"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)

    app_py = ws / "src" / "app.py"
    app_py.write_text("def run_app() -> int:\n    return 42\n", encoding="utf-8")

    guide_md = ws / "docs" / "guide.md"
    guide_md.write_text("# Guide\n\n## Overview\nReal workspace guide documentation.\n", encoding="utf-8")

    db_path = ws / ".agent_graph.db"

    # 1) Populate a SQLite database with parent-workspace paths that do not exist under `ws`
    # alongside the real paths.
    seed_db = DatabaseManager(db_path, workspace_root=None)

    # Real symbols & docs
    real_sym = SymbolInfo(
        id="src/app.py::run_app",
        file_path="src/app.py",
        parent_id=None,
        name="run_app",
        kind="function",
        signature="def run_app() -> int:",
        docstring=None,
        start_byte=0,
        end_byte=35,
        start_line=1,
        end_line=2,
        content_hash="aaaa1111",
    )
    seed_db.upsert_file_symbols("src/app.py", [real_sym], [])
    seed_db.set_file_hash("src/app.py", "hash_real_app")
    seed_db.add_search_entries(
        [
            {
                "kind": "symbol",
                "ref_id": "src/app.py::run_app",
                "file_path": "src/app.py",
                "title": "run_app",
                "body": "def run_app() -> int: return 42",
            }
        ]
    )

    real_doc = ParsedDocument(
        doc_type="markdown",
        title="Guide",
        sections=[
            DocumentSection(
                heading="Overview",
                level=2,
                order_index=0,
                content="Real workspace guide documentation.",
            )
        ],
    )
    seed_db.upsert_document("docs/guide.md", real_doc, "hash_real_guide")
    seed_db.set_file_hash("docs/guide.md", "hash_real_guide")
    seed_db.add_search_entries(
        [
            {
                "kind": "section",
                "ref_id": "docs/guide.md::Overview",
                "file_path": "docs/guide.md",
                "title": "Overview",
                "body": "Real workspace guide documentation.",
            }
        ]
    )

    # Phantom symbols & docs from a parent workspace (e.g. C:\CodeSource\Scalpel-clean)
    phantom_sym = SymbolInfo(
        id="scalpel-mcp/src/app.py::run_app",
        file_path="scalpel-mcp/src/app.py",
        parent_id=None,
        name="run_app",
        kind="function",
        signature="def run_app() -> int:",
        docstring=None,
        start_byte=0,
        end_byte=35,
        start_line=1,
        end_line=2,
        content_hash="bbbb2222",
    )
    seed_db.upsert_file_symbols("scalpel-mcp/src/app.py", [phantom_sym], [])
    seed_db.set_file_hash("scalpel-mcp/src/app.py", "hash_phantom_app")
    seed_db.add_search_entries(
        [
            {
                "kind": "symbol",
                "ref_id": "scalpel-mcp/src/app.py::run_app",
                "file_path": "scalpel-mcp/src/app.py",
                "title": "run_app",
                "body": "def run_app() -> int: return 42",
            }
        ]
    )

    for phantom_doc_path in ("scalpel-benchmarks/report.md", "reports/bilan.md"):
        p_doc = ParsedDocument(
            doc_type="markdown",
            title="Phantom Report",
            sections=[
                DocumentSection(
                    heading="Overview",
                    level=2,
                    order_index=0,
                    content="Phantom guide documentation from parent workspace.",
                )
            ],
        )
        seed_db.upsert_document(phantom_doc_path, p_doc, "hash_phantom_doc")
        # Intentionally leave `reports/bilan.md` out of indexed_files to test orphan table purge!
        if phantom_doc_path == "scalpel-benchmarks/report.md":
            seed_db.set_file_hash(phantom_doc_path, "hash_phantom_doc")
        seed_db.add_search_entries(
            [
                {
                    "kind": "section",
                    "ref_id": f"{phantom_doc_path}::Overview",
                    "file_path": phantom_doc_path,
                    "title": "Overview",
                    "body": "Phantom guide documentation from parent workspace.",
                }
            ]
        )

    seed_db.set_meta("workspace_root", str(tmp_path.resolve()))

    # 2) Open a new DatabaseManager instance rooted at `ws` on the pre-existing DB
    db = DatabaseManager(db_path, workspace_root=ws)

    # 3) Verify get_project_map, search_documents, search_symbols, and search_index (build_context)
    # ignore 100% of phantom entries and only return `src/app.py` and `docs/guide.md`.
    proj_map = db.get_project_map(budget_tokens=8000, answer_format="json")
    map_files = [f["file_path"] for f in proj_map["files"]]
    map_docs = [d["file_path"] for d in proj_map["documents"]]
    assert map_files == ["src/app.py"]
    assert map_docs == ["docs/guide.md"]
    assert proj_map["summary"]["symbols"] == 1
    assert proj_map["summary"]["documents"] == 1

    doc_hits = db.search_documents(query="Overview")
    assert [d["file_path"] for d in doc_hits] == ["docs/guide.md"]

    listed_docs = db.list_documents()
    assert [d["file_path"] for d in listed_docs] == ["docs/guide.md"]

    sym_hits = db.search_symbols(query="run_app")
    assert [s["file"] for s in sym_hits] == ["src/app.py"]

    sym_hits_scoped = db.search_symbols(query="run_app", file_pattern="src/**")
    assert [s["file"] for s in sym_hits_scoped] == ["src/app.py"]

    idx_hits = db.search_index(query="run_app OR Overview")
    assert {h["file_path"] for h in idx_hits} == {"src/app.py", "docs/guide.md"}

    cfg = ServerConfig(workspace_root=ws, db_path=db_path)
    server = ASTGraphServer(cfg)
    ctx = server.context_builder.build(query="run_app Overview", budget_tokens=4000, format="json")
    ctx_refs = {e["ref_id"] for e in ctx["entries"]}
    assert all(not r.startswith(("scalpel-mcp/", "scalpel-benchmarks/", "reports/")) for r in ctx_refs)
    assert any(r.startswith("src/app.py") for r in ctx_refs)

    # 4) Verify Indexer.index_workspace() purges orphan entries and updates meta["workspace_root"]
    server.indexer.index_workspace(force=False)

    assert server.db.get_meta("workspace_root") == str(ws.resolve())
    with server.db.get_connection() as conn:
        remaining_syms = {r["file_path"] for r in conn.execute("SELECT file_path FROM symbols")}
        remaining_docs = {r["file_path"] for r in conn.execute("SELECT file_path FROM documents")}
        remaining_idx = {r["file_path"] for r in conn.execute("SELECT file_path FROM search_index")}
    assert remaining_syms == {"src/app.py"}
    assert remaining_docs == {"docs/guide.md"}
    assert remaining_idx <= {"src/app.py", "docs/guide.md"}