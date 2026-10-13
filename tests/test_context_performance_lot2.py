import time
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher
from ast_graph_server.context_builder import ContextBuilder


def create_performance_env(tmp_path: Path, num_files: int = 5, items_per_file: int = 25):
    """Creates a workspace with multiple code and doc files containing 100+ total candidates."""
    ws = tmp_path / "ws"
    src_dir = ws / "src"
    docs_dir = ws / "docs"
    src_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)

    for f_idx in range(num_files):
        # Python code file with several classes and methods
        code_lines = [f"# Code module {f_idx}"]
        for c_idx in range(items_per_file // 5):
            code_lines.append(f"class Service{f_idx}_{c_idx}:")
            code_lines.append(f'    """Service documentation for cluster operations."""')
            for m_idx in range(4):
                code_lines.append(f"    def action_{m_idx}(self, param: int) -> bool:")
                code_lines.append(f'        """Execute action {m_idx} on cluster resources."""')
                code_lines.append(f"        return param > {m_idx}")
        (src_dir / f"module_{f_idx}.py").write_text("\n".join(code_lines), encoding="utf-8")

        # Markdown doc file with sections
        doc_lines = [f"# Documentation Guide {f_idx}", "", "Overview paragraph for cluster management.", ""]
        for s_idx in range(items_per_file // 5):
            doc_lines.append(f"## Section {s_idx}")
            doc_lines.append(f"Detailed cluster instructions for section {s_idx}.")
            doc_lines.append("Content line 1 with cluster tokens.")
            doc_lines.append("Content line 2 with cluster configuration.")
            doc_lines.append("")
        (docs_dir / f"guide_{f_idx}.md").write_text("\n".join(doc_lines), encoding="utf-8")

    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    indexer = Indexer(config, db)
    indexer.index_workspace()

    patcher = Patcher(config, db, indexer)
    builder = ContextBuilder(config, db)
    builder.addresses = patcher
    return ws, config, db, patcher, builder


def test_functional_conformity_canonical_addresses_and_hashes(tmp_path: Path):
    """Test 1: Verify strict functional conformity: canonical '::' addresses, levels 0 to 3, and hashes."""
    _, _, _, _, builder = create_performance_env(tmp_path, num_files=2, items_per_file=10)

    res = builder.build("cluster", budget_tokens=4000, max_level=3)
    entries = res["entries"]
    assert len(entries) > 0, "Should have matches for query 'cluster'"

    # Verify canonical addresses
    symbol_entries = [e for e in entries if e["kind"] == "symbol"]
    section_entries = [e for e in entries if e["kind"] == "section"]

    assert len(symbol_entries) > 0, "Should contain symbol entries"
    assert len(section_entries) > 0, "Should contain section entries"

    for sym in symbol_entries:
        assert "::" in sym["ref_id"], f"Symbol address must be canonical '::', got {sym['ref_id']}"
        assert not sym["ref_id"].isdigit(), f"Symbol ref_id must not be a raw storage id: {sym['ref_id']}"

    for sec in section_entries:
        assert "::" in sec["ref_id"] or "#" in sec["ref_id"], f"Section address must be formatted: {sec['ref_id']}"

    # Verify level 3 entries have content_hash and line_count
    l3_entries = [e for e in entries if e["level"] >= 3]
    assert len(l3_entries) > 0, "Should have deepened at least one entry to level 3"
    for e in l3_entries:
        assert "content_hash" in e, f"Level 3 entry missing content_hash: {e['ref_id']}"
        assert len(e["content_hash"]) == 64 or len(e["content_hash"]) > 0, "Invalid content hash"
        assert e.get("line_count") is not None, f"Level 3 entry missing line_count: {e['ref_id']}"


def test_database_calls_bounded_by_file_count_not_entries_x_levels(tmp_path: Path):
    """Test 2: Verify that DB / disk resolution calls are O(files) and not O(entries * levels)."""
    num_files = 4
    _, _, db, patcher, builder = create_performance_env(tmp_path, num_files=num_files, items_per_file=25)

    orig_get_symbol = db.get_symbol
    orig_get_section = db.get_document_section
    orig_symbol_addresses = patcher.symbol_addresses
    orig_document_addresses = patcher.document_addresses
    orig_block_metrics = patcher.block_metrics
    orig_displayed_id = patcher.displayed_id

    symbol_calls = {}
    section_calls = {}
    symbol_addresses_calls = []
    document_addresses_calls = []
    block_metrics_calls = {}
    displayed_id_calls = []

    def spy_get_symbol(sym_id):
        symbol_calls[sym_id] = symbol_calls.get(sym_id, 0) + 1
        return orig_get_symbol(sym_id)

    def spy_get_section(sec_id):
        section_calls[sec_id] = section_calls.get(sec_id, 0) + 1
        return orig_get_section(sec_id)

    def spy_symbol_addresses(file_path):
        symbol_addresses_calls.append(file_path)
        return orig_symbol_addresses(file_path)

    def spy_document_addresses(doc_id):
        document_addresses_calls.append(doc_id)
        return orig_document_addresses(doc_id)

    def spy_block_metrics(block_ref):
        block_metrics_calls[block_ref] = block_metrics_calls.get(block_ref, 0) + 1
        return orig_block_metrics(block_ref)

    def spy_displayed_id(stored):
        displayed_id_calls.append(stored)
        return orig_displayed_id(stored)

    db.get_symbol = spy_get_symbol
    db.get_document_section = spy_get_section
    patcher.symbol_addresses = spy_symbol_addresses
    patcher.document_addresses = spy_document_addresses
    patcher.block_metrics = spy_block_metrics
    patcher.displayed_id = spy_displayed_id

    try:
        res = builder.build("cluster", budget_tokens=8000, max_level=3, limit=100)
        entries = res["entries"]
        assert len(entries) >= 40, f"Expected 40+ entries, got {len(entries)}"

        # 1. No individual symbol or section must be fetched from DB more than once
        repeated_symbols = {k: v for k, v in symbol_calls.items() if v > 1}
        assert not repeated_symbols, f"get_symbol was called redundantly for: {repeated_symbols}"

        repeated_sections = {k: v for k, v in section_calls.items() if v > 1}
        assert not repeated_sections, f"get_document_section was called redundantly for: {repeated_sections}"

        # 2. symbol_addresses and document_addresses must be called at most once per file
        assert len(symbol_addresses_calls) <= num_files, (
            f"symbol_addresses called {len(symbol_addresses_calls)} times for {num_files} code files: {symbol_addresses_calls}"
        )
        assert len(document_addresses_calls) <= num_files, (
            f"document_addresses called {len(document_addresses_calls)} times for {num_files} doc files: {document_addresses_calls}"
        )

        # 3. block_metrics must not be called redundantly for the same block
        repeated_metrics = {k: v for k, v in block_metrics_calls.items() if v > 1}
        assert not repeated_metrics, f"block_metrics was called redundantly for: {repeated_metrics}"

        # 4. N+1 individual displayed_id calls during ref_id formatting must be eliminated
        # (It should be 0 because batch resolution uses cached symbol_addresses and document_addresses)
        assert len(displayed_id_calls) == 0, (
            f"displayed_id was called {len(displayed_id_calls)} times! N+1 problem detected."
        )

    finally:
        db.get_symbol = orig_get_symbol
        db.get_document_section = orig_get_section
        patcher.symbol_addresses = orig_symbol_addresses
        patcher.document_addresses = orig_document_addresses
        patcher.block_metrics = orig_block_metrics
        patcher.displayed_id = orig_displayed_id


def test_performance_benchmark_100_candidates_latency(tmp_path: Path):
    """Test 3: Benchmark latency with 100+ candidates (< 400ms target)."""
    _, _, _, _, builder = create_performance_env(tmp_path, num_files=6, items_per_file=30)

    # Warm-up / cold run
    start_time = time.perf_counter()
    res = builder.build("cluster", budget_tokens=8000, max_level=3, limit=120)
    elapsed = time.perf_counter() - start_time

    assert len(res["entries"]) >= 50, f"Expected 50+ candidates in context, got {len(res['entries'])}"
    print(f"\n[LATENCY BENCHMARK] build_context on {len(res['entries'])} entries took: {elapsed:.4f}s")
    assert elapsed < 0.6, f"ContextBuilder.build took {elapsed:.3f}s, expected < 0.6s (target < 0.4s)"