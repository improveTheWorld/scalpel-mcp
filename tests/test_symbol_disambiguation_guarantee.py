from pathlib import Path
import pytest
from typing import List, Optional, Tuple

from ast_graph_server.parsers.base import BaseParser, ParseResult, SymbolInfo, CallInfo, SyntaxErrorDetail
from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer


class UnsafeDummyParser(BaseParser):
    """A parser that deliberately produces duplicate symbol IDs and omits _disambiguate_overloads."""

    def can_parse(self, file_path: str) -> bool:
        return file_path.endswith(".dummy")

    def validate_syntax(self, code: bytes, file_path: Optional[str] = None) -> Tuple[bool, List[SyntaxErrorDetail]]:
        return True, []

    def parse_file(self, file_path: str, content: bytes) -> ParseResult:
        # Deliberately produces duplicate IDs without calling _disambiguate_overloads
        symbols = [
            SymbolInfo(
                id=f"{file_path}::item",
                file_path=file_path,
                parent_id=None,
                name="item",
                kind="property",
                signature="item 1",
                docstring=None,
                start_byte=0,
                end_byte=10,
                start_line=1,
                end_line=1,
                content_hash="h1",
            ),
            SymbolInfo(
                id=f"{file_path}::item",
                file_path=file_path,
                parent_id=None,
                name="item",
                kind="property",
                signature="item 2",
                docstring=None,
                start_byte=11,
                end_byte=20,
                start_line=2,
                end_line=2,
                content_hash="h2",
            ),
        ]
        return ParseResult(symbols=symbols, calls=[])


def test_indexer_guarantees_symbol_uniqueness_even_if_parser_omits_it(tmp_path: Path):
    """Indexer must not crash when a parser returns duplicate symbol IDs."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    dummy_file = ws / "test.dummy"
    dummy_file.write_text("item 1\nitem 2\n", encoding="utf-8")

    db_path = tmp_path / "graph.db"
    config = ServerConfig(workspace_root=ws, db_path=db_path)
    db = DatabaseManager(db_path, workspace_root=ws)
    indexer = Indexer(config, db)

    # Register the unsafe parser
    indexer.registry._parsers.insert(0, UnsafeDummyParser())

    # This call must NOT raise sqlite3.IntegrityError: UNIQUE constraint failed: symbols.id
    success = indexer.index_file(dummy_file)
    assert success is True

    stored_symbols = db.get_symbols_for_file("test.dummy")
    assert len(stored_symbols) == 2
    # The IDs must have been disambiguated and unique
    ids = [s["id"] for s in stored_symbols]
    assert len(set(ids)) == 2


def test_database_upsert_file_symbols_safety_net(tmp_path: Path):
    """upsert_file_symbols must handle accidental duplicate IDs defensively without crashing."""
    db = DatabaseManager(tmp_path / "graph.db", workspace_root=tmp_path)
    symbols = [
        SymbolInfo(
            id="file.py::foo",
            file_path="file.py",
            parent_id=None,
            name="foo",
            kind="function",
            signature="def foo(): pass",
            docstring=None,
            start_byte=0,
            end_byte=10,
            start_line=1,
            end_line=1,
            content_hash="h1",
        ),
        SymbolInfo(
            id="file.py::foo",
            file_path="file.py",
            parent_id=None,
            name="foo",
            kind="function",
            signature="def foo(): pass",
            docstring=None,
            start_byte=11,
            end_byte=20,
            start_line=2,
            end_line=2,
            content_hash="h2",
        ),
    ]

    # This call must NOT raise sqlite3.IntegrityError: UNIQUE constraint failed: symbols.id
    db.upsert_file_symbols("file.py", symbols, [])

    stored = db.get_symbols_for_file("file.py")
    assert len(stored) == 2
    ids = [s["id"] for s in stored]
    assert len(set(ids)) == 2