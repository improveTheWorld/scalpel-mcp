"""Concurrency tests verifying multi-agent access to shared SQLite DB in WAL mode."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from ast_graph_server.database import DatabaseManager
from ast_graph_server.parsers.base import SymbolInfo


def test_concurrent_reads_and_writes(tmp_path: Path) -> None:
    db_file = tmp_path / "shared.db"
    db = DatabaseManager(db_file)

    def worker(worker_id: int) -> int:
        local_db = DatabaseManager(db_file)
        sym = SymbolInfo(
            id=f"src/file_{worker_id}.py::func_{worker_id}",
            file_path=f"src/file_{worker_id}.py",
            parent_id=None,
            name=f"func_{worker_id}",
            kind="function",
            signature=f"def func_{worker_id}():",
            docstring=None,
            start_byte=0,
            end_byte=30,
            start_line=1,
            end_line=2,
            content_hash=f"hash_{worker_id}",
        )
        # Perform write
        local_db.upsert_file_symbols(f"src/file_{worker_id}.py", [sym], [])

        # Perform read
        results = local_db.search_symbols("func_")
        return len(results)

    # 10 simulated concurrent agents writing and reading simultaneously
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker, i) for i in range(10)]
        results = [f.result() for f in futures]

    assert len(results) == 10
    assert all(r > 0 for r in results)

    # All 10 symbols should be present in the DB
    all_syms = db.search_symbols("func_")
    assert len(all_syms) == 10
