"""Unit tests for SQLite DatabaseManager."""

import pytest
from pathlib import Path
from ast_graph_server.database import DatabaseManager
from ast_graph_server.parsers.base import CallInfo, SymbolInfo


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    db_file = tmp_path / "test.db"
    return DatabaseManager(db_file)


def test_schema_initialization(db: DatabaseManager) -> None:
    with db.get_connection() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t["name"] for t in tables]
        assert "symbols" in table_names
        assert "symbol_calls" in table_names
        assert "staged_changes" in table_names


def test_upsert_and_search_symbols(db: DatabaseManager) -> None:
    sym1 = SymbolInfo(
        id="src/billing.py::BillingService",
        file_path="src/billing.py",
        parent_id=None,
        name="BillingService",
        kind="class",
        signature="class BillingService:",
        docstring="Handles invoicing.",
        start_byte=0,
        end_byte=100,
        start_line=1,
        end_line=10,
        content_hash="hash1",
    )
    sym2 = SymbolInfo(
        id="src/billing.py::BillingService.charge",
        file_path="src/billing.py",
        parent_id="src/billing.py::BillingService",
        name="charge",
        kind="method",
        signature="def charge(self, amount: float):",
        docstring="Process payment.",
        start_byte=20,
        end_byte=80,
        start_line=3,
        end_line=8,
        content_hash="hash2",
    )
    calls = [
        CallInfo(
            caller_id=sym2.id,
            callee_name="validate_user",
            call_expr="self.validate_user",
        )
    ]

    db.upsert_file_symbols("src/billing.py", [sym1, sym2], calls)

    # Test search exact
    matches = db.search_symbols("charge")
    assert len(matches) == 1
    assert matches[0]["symbol_id"] == sym2.id
    assert matches[0]["kind"] == "method"

    # Test search with kind filter
    matches_class = db.search_symbols("Billing", kind="class")
    assert len(matches_class) == 1
    assert matches_class[0]["symbol_id"] == sym1.id

    # Test search with file pattern
    matches_glob = db.search_symbols("charge", file_pattern="src/*.py")
    assert len(matches_glob) == 1

    matches_none = db.search_symbols("charge", file_pattern="tests/*.py")
    assert len(matches_none) == 0

    # Test get_symbol
    fetched = db.get_symbol(sym2.id)
    assert fetched is not None
    assert fetched.name == "charge"
    assert fetched.signature == "def charge(self, amount: float):"

    # Test get_symbol_calls
    call_records = db.get_symbol_calls(sym2.id)
    assert len(call_records) == 1
    assert call_records[0]["callee_name"] == "validate_user"
    assert call_records[0]["call_expr"] == "self.validate_user"


def test_cascade_deletion(db: DatabaseManager) -> None:
    sym = SymbolInfo(
        id="src/foo.py::foo",
        file_path="src/foo.py",
        parent_id=None,
        name="foo",
        kind="function",
        signature="def foo():",
        docstring=None,
        start_byte=0,
        end_byte=20,
        start_line=1,
        end_line=2,
        content_hash="h1",
    )
    calls = [CallInfo(caller_id=sym.id, callee_name="bar", call_expr="bar")]
    db.upsert_file_symbols("src/foo.py", [sym], calls)

    assert len(db.get_symbol_calls(sym.id)) == 1

    db.delete_file_symbols("src/foo.py")
    assert db.get_symbol(sym.id) is None
    assert len(db.get_symbol_calls(sym.id)) == 0


def _symbols(db: DatabaseManager, *names: str) -> None:
    """One upsert per FILE: a second call replaces the first one's symbols."""
    db.upsert_file_symbols(
        "src/T.cs",
        [
            SymbolInfo(
                id="src/T.cs::" + name,
                file_path="src/T.cs",
                parent_id=None,
                name=name,
                kind="enum",
                signature="public enum " + name,
                docstring="",
                start_byte=index * 20,
                end_byte=index * 20 + 10,
                start_line=index + 1,
                end_line=index + 2,
                content_hash="h-" + name,
            )
            for index, name in enumerate(names)
        ],
        [],
    )


def test_search_symbols_matches_term_by_term(db: DatabaseManager) -> None:
    """A multi-word query is a union of terms, never a phrase that matches nothing.

    "ApplyAuthentication AuthMode SparkAuthMode" returned count 0 while each term
    alone found its symbol: the caller split the query by hand and concluded the
    symbols did not exist (measured on mapab2 a r1).
    """
    _symbols(db, "ApplyAuthentication", "SparkAuthMode", "AuthMode")

    found = db.search_symbols("ApplyAuthentication AuthMode SparkAuthMode")
    names = [match["symbol_id"] for match in found]

    assert "src/T.cs::ApplyAuthentication" in names
    assert "src/T.cs::SparkAuthMode" in names
    # SparkAuthMode carries two of the three terms, so it ranks first.
    assert names[0] == "src/T.cs::SparkAuthMode"

    # A phrase that matches nothing still returns nothing, honestly.
    assert db.search_symbols("NowhereToBeFound") == []

def test_search_symbols_accepts_a_bare_file_name(db: DatabaseManager) -> None:
    """`file_pattern="SparkContext.cs"` is what the caller holds; it must match.

    Matching the full path alone returned nothing at all, silently, and the agent
    concluded the file carried no such symbol (reported by the agent on mapab2 b of
    the base 7c40a9e).
    """
    _symbols(db, "Connect", "SparkConnectOptions")

    assert db.search_symbols("Connect", file_pattern="SparkContext.cs") == []
    assert db.search_symbols("Connect", file_pattern="T.cs")
    assert db.search_symbols("Connect", file_pattern="src/T.cs")
    assert db.search_symbols("Connect", file_pattern="src/*.cs")
    assert db.search_symbols("Connect", file_pattern="T*")
    assert db.search_symbols("Connect", file_pattern="src")  # a folder scopes to its content
