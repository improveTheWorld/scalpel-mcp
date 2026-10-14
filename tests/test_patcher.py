"""Unit tests for surgical Patcher."""

from pathlib import Path
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    src_dir = ws / "src"
    src_dir.mkdir()

    billing_file = src_dir / "billing.py"
    billing_file.write_text(
        '''"""Billing module."""

class BillingService:
    """Handles payments."""

    def __init__(self):
        pass

    def charge(self, user_id: str, amount: float):
        """Original charge method."""
        return False

    def validate_user(self, user_id: str) -> bool:
        return True
''',
        encoding="utf-8",
    )
    return ws


@pytest.fixture
def patcher_env(workspace: Path) -> tuple[Patcher, DatabaseManager, Indexer, ServerConfig]:
    config = ServerConfig(workspace_root=workspace, db_path=workspace / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    patcher = Patcher(config, db, indexer)
    return patcher, db, indexer, config


def test_a_file_no_parser_claims_returns_its_text_read_only(
    workspace: Path, patcher_env: tuple
) -> None:
    """The Scalpel arm had NO reader for a file that is neither code nor a document.

    The task own documents name deploy_and_run_databricks.ps1; read_block refused it -- "this
    extension is neither" -- and pointed at search_documents, so the agent spent its next two
    turns looking for a route that does not exist: measured on three consecutive runs of the base
    dee367d, about 3 turns each. The baseline arm has read_file; this closes the gap, and ONLY
    for a file the index has nothing to say about.
    """
    patcher, db, indexer, config = patcher_env
    scripts = workspace / "scripts"
    scripts.mkdir()
    (scripts / "deploy.ps1").write_text(
        "param($Name)" + chr(10) + 'Write-Host "hello $Name"' + chr(10), encoding="utf-8"
    )

    payload = patcher.read_block("scripts/deploy.ps1")

    assert payload["status"] == "success", payload
    assert payload["kind"] == "file_text"
    assert payload["read_only"] is False
    assert payload["numbered_text"].startswith("1 |")
    assert "param($Name)" in payload["numbered_text"]
    assert payload["content_hash"] and payload["line_count"] == 2

    stale = patcher.edit_block(
        "scripts/deploy.ps1",
        ops=[{"replace": [1, 1], "content": "param($Target)"}],
        expected_hash="00000000",
    )
    assert stale["status"] == "error"

    edited = patcher.edit_block(
        "scripts/deploy.ps1",
        ops=[{"replace": [1, 1], "content": "param($Target)"}],
        expected_hash=payload["content_hash"],
    )
    assert edited["status"] == "success", edited
    assert "param($Target)" in (scripts / "deploy.ps1").read_text(encoding="utf-8")

    (workspace / ".gitignore").write_text("bin/\nobj/\n", encoding="utf-8")
    gi_read = patcher.read_block(".gitignore")
    batch = patcher.edit_block(
        edits=[
            {
                "path": ".gitignore",
                "expected_hash": gi_read["content_hash"],
                "ops": [{"insert_after": 2, "content": "*.tmp"}],
            },
            {
                "path": "scripts/deploy.ps1",
                "expected_hash": edited["content_hash"],
                "ops": [{"replace": [2, 2], "content": 'Write-Host "deployed $Target"'}],
            },
        ]
    )
    assert batch["status"] == "success" and batch["applied"] == 2, batch
    assert "*.tmp" in (workspace / ".gitignore").read_text(encoding="utf-8")
    assert 'Write-Host "deployed $Target"' in (scripts / "deploy.ps1").read_text(encoding="utf-8")

    # A file a PARSER claims is untouched by this door: still the outline, never its source.
    code = patcher.read_block("src/billing.py", answer_format="json")
    assert "children" in code and "numbered_text" not in code

def test_a_using_directive_joins_its_siblings(workspace: Path, patcher_env: tuple) -> None:
    """A using directive has ONE legal place: above every other declaration.

    Appended at the end (position="end"), it landed AFTER the file-scoped namespace -- CS1529,
    the file stopped compiling while the reply said success with syntax_ok=True. Measured twice
    in one run (base 160d773, t19 and t24: two builds lost, and the agent gave up on the edit).
    Targeting is the point: the new directive takes its place among its siblings.
    """
    patcher, db, indexer, config = patcher_env
    code = workspace / "src" / "Options.cs"
    code.write_text(
        "using System;" + chr(10) + chr(10) + "namespace Demo;" + chr(10) + chr(10)
        + "public sealed class Options" + chr(10) + "{" + chr(10) + "}" + chr(10),
        encoding="utf-8",
    )
    indexer.index_workspace()

    reply = patcher.insert_symbol(
        parent_id="src/Options.cs", code="using Microsoft.Spark.Sql;", position="end"
    )

    assert reply["status"] == "success", reply
    text = code.read_text(encoding="utf-8")
    assert text.index("using System;") < text.index("using Microsoft.Spark.Sql;") < text.index("namespace")


def test_surgical_update_symbol_body(workspace: Path, patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig]) -> None:
    patcher, db, indexer, config = patcher_env
    sym_id = "src/billing.py::BillingService.charge"

    # New implementation - submitted without 4 spaces (unindented)
    new_code = '''def charge(self, user_id: str, amount: float):
    """Updated charge implementation."""
    if not self.validate_user(user_id):
        raise ValueError("Invalid user")
    return True'''

    res = patcher.update_symbol_body(sym_id, new_code)
    assert res["status"] == "success"
    assert res["validation"]["syntax_ok"] is True
    assert "validate_user" in res["validation"]["reparsed_calls"]

    # Verify disk content has exact 4 spaces and untouched neighboring methods
    file_text = (workspace / "src/billing.py").read_text(encoding="utf-8")
    assert '"""Billing module."""' in file_text
    assert "def __init__(self):" in file_text
    assert "def validate_user(self, user_id: str) -> bool:" in file_text
    assert "    def charge(self, user_id: str, amount: float):" in file_text
    assert '        """Updated charge implementation."""' in file_text


def test_syntax_barrier_rejection(workspace: Path, patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig]) -> None:
    patcher, db, indexer, config = patcher_env
    sym_id = "src/billing.py::BillingService.charge"
    original_text = (workspace / "src/billing.py").read_text(encoding="utf-8")

    # Invalid Python syntax
    bad_code = "def charge(:"

    res = patcher.update_symbol_body(sym_id, bad_code)
    assert res["status"] == "error"
    assert res["validation"]["syntax_ok"] is False

    # Assert disk file is bit-for-bit unchanged
    current_text = (workspace / "src/billing.py").read_text(encoding="utf-8")
    assert current_text == original_text


def test_optimistic_locking(patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig]) -> None:
    patcher, db, indexer, config = patcher_env
    sym_id = "src/billing.py::BillingService.charge"

    res = patcher.update_symbol_body(
        sym_id,
        "def charge(self): return 1",
        expected_hash="stale_non_existent_hash",
    )
    assert res["status"] == "error"
    assert "Optimistic lock failure" in res["error"]


def test_insert_symbol(workspace: Path, patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig]) -> None:
    patcher, db, indexer, config = patcher_env

    new_method = '''def refund(self, charge_id: str) -> bool:
    """Refund transaction."""
    return True'''

    res = patcher.insert_symbol("src/billing.py::BillingService", new_method)
    assert res["status"] == "success"

    file_text = (workspace / "src/billing.py").read_text(encoding="utf-8")
    assert "    def refund(self, charge_id: str) -> bool:" in file_text

    # Verify newly inserted method was indexed
    sym = db.get_symbol("src/billing.py::BillingService.refund")
    assert sym is not None
    assert sym.kind == "method"


def test_syntax_rejection_locates_the_edited_region(
    workspace: Path, patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig]
) -> None:
    """The parser reports *file* lines; the caller reasoned on *block* lines.

    Measured on a real run: the agent replaced block lines 90-101, was told
    "Syntax error at line 184", and spent four turns retrying shifted ranges
    because nothing tied the two coordinate systems together.
    """
    patcher, db, indexer, config = patcher_env
    sym_id = "src/billing.py::BillingService.charge"

    res = patcher.update_symbol_body(
        sym_id, "def charge(self, user_id: str, amount: float):\n    return (False"
    )

    assert res["status"] == "error"
    validation = res["validation"]
    first, last = validation["edited_file_lines"]
    assert first == 9  # 'def charge' is the 9th line of the fixture
    assert last > first
    inside = [d for d in validation["error_details"] if "edited_region_line" in d]
    assert inside, validation["error_details"]
    # The two coordinate systems must agree: block line N maps to the file line
    # the region starts at, plus N-1.
    for detail in inside:
        assert detail["file_line"] == first + detail["edited_region_line"] - 1
        assert first <= detail["file_line"] <= last


def test_syntax_errors_outside_the_edited_region_say_so(
    patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig]
) -> None:
    """An error the edit did not contain must be named as such, not left implicit."""
    patcher, db, indexer, config = patcher_env

    class FakeError:
        line = 184
        actionable_hint = "Check braces."

        def __str__(self) -> str:
            return "Syntax error at line 184:0"

        def to_dict(self) -> dict:
            return {"line": 184, "col": 0, "message": "Syntax error at line 184:0"}

    payload = patcher._syntax_error_payload("symbol_id", "x.cs::Y", [FakeError()], "boom", region=(90, 101))

    assert payload["validation"]["edited_file_lines"] == [90, 101]
    detail = payload["validation"]["error_details"][0]
    assert detail["file_line"] == 184
    assert detail["in_edited_region"] is False
    assert "outside the region" in payload["validation"]["note"]

def test_the_syntax_refusal_flags_a_duplicate_closing_delimiter(patcher_env) -> None:
    """An error on the LAST line of the replacement points at a stray delimiter.

    Reproduces mapab2 a r1 of the base 9016f1d: the region was [10, 107] and the error
    sat on region line 98 -- the agent had closed the class a second time, and it
    declared that the tool "did not flag it as a range problem".
    """
    patcher, db, indexer, config = patcher_env

    class FakeError:
        def __str__(self) -> str:
            return "Syntax error at line 107:0"

        def to_dict(self) -> dict:
            return {"line": 107, "col": 0, "message": "Syntax error at line 107:0"}

    payload = patcher._syntax_error_payload(
        "symbol_id", "x.cs::Y", [FakeError()], "boom", region=(10, 107)
    )

    assert payload["validation"]["error_details"][0]["edited_region_line"] == 98
    assert "LAST line" in payload["validation"]["note"]
    assert "closes a delimiter" in payload["validation"]["note"]

def test_a_code_file_path_returns_its_outline_not_its_source(
    patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig],
) -> None:
    """A code file path answers with STRUCTURE: its symbols, never its text.

    Handing the source back would send the model down the flat file-by-file reading it
    was trained on; the outline lets it choose a block. This was the #1 refusal of the
    1a14b7c campaign (6 occurrences in 5 runs, +1 to +3 turns each).
    """
    patcher, db, indexer, config = patcher_env

    result = patcher.read_block("src/billing.py", answer_format="json")

    assert result["status"] == "success"
    assert result["id"] == "src/billing.py"
    # No 'kind': the path was asked for, and the type is carried by the name.
    assert "kind" not in result
    names = [child["name"] for child in result["children"]]
    assert "BillingService" in names
    assert all({"name", "lines", "bytes"} <= set(child) for child in result["children"])
    # Structure only: the raw source is never returned.
    assert "numbered_text" not in result
    assert "def charge" not in str(result)

    # An id that names no block keeps the plain refusal: only a bare path is answered.
    other = patcher.read_block("src/billing.py::Missing")
    assert other["status"] == "error"
    assert "held_by_the_file" not in other


def test_inserting_a_symbol_returns_its_address_and_its_state(
    patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig],
) -> None:
    """The symbol path returned no id at all: the caller could not name, read or edit what it had
    just created.

    Measured on run 1 of base 30d62b4: the agent inserted a member, then read the whole enclosing
    class to find the created name it needed as the next anchor.
    """
    patcher, db, indexer, config = patcher_env

    result = patcher.insert_symbol(
        parent_id="src/billing.py::BillingService",
        code="def refund(self, user_id: str) -> bool:\n    return True",
        position="end",
    )

    assert result["status"] == "success", result
    assert result["created_symbol_id"].endswith("refund")
    assert result["created_symbol_id"] in result["created_symbol_ids"]
    assert result["line_count"] >= 2
    assert result["numbered_text"].startswith("1 |")

    # One call, no read: the returned address opens the created symbol.
    applied = patcher.edit_block(
        result["created_symbol_id"],
        ops=[{"insert_after": 1, "content": "    # checked"}],
    )
    assert applied["status"] == "success", applied


def test_editing_a_code_file_path_refuses_instead_of_pretending(
    workspace: Path, patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig]
) -> None:
    """A WRITE that names a whole code file must refuse, never answer with the read outline.

    Run 1 of base 30d62b4 (mapab2/a), declared by the agent at t61 and reproduced at
    benchmarks/.work/_repro_writefile.py: `edit_block(path=.../SparkConnectOptions.cs)` returned
    `status: success` carrying the file's outline, and the file on disk did not change. The agent
    tried the same edit three times, read the file back four times, and edited two other files to
    isolate it -- ~11 turns, 16% of the run's cost -- before working around it through a symbol.
    """
    patcher, db, indexer, config = patcher_env
    path = "src/billing.py"
    before = (workspace / path).read_bytes()

    # The READ keeps answering with structure: that part is deliberate, and pinned above.
    assert patcher.read_block(path)["status"] == "success"

    refused = patcher.edit_block(path, ops=[{"insert_after": 1, "content": "# x"}], expected_hash=None)

    assert refused["status"] == "error", refused
    assert "not a block inside one" in refused["error"]
    assert "NOTHING was written" in refused["error"]
    assert "BillingService" in refused["holds"]
    assert "read_block('src/billing.py')" in refused["actionable_hint"]
    # The point of the refusal: nothing on disk moved.
    assert (workspace / path).read_bytes() == before

def test_inserting_into_a_file_that_does_not_exist_names_the_files_that_can_take_it(
    patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig],
) -> None:
    """No tool creates a file, so the refusal says which existing one can take the code."""
    patcher, db, indexer, config = patcher_env

    result = patcher.insert_symbol("src/new_module.py", "class New:\n    pass\n")

    assert result["status"] == "error"
    assert "does not exist" in result["error"]
    assert any("billing.py" in name for name in result["files_that_can_take_it"])
    assert "insert_block" in result["actionable_hint"]

def test_delete_block_batch_paths(
    workspace: Path,
    patcher_env: tuple[Patcher, DatabaseManager, Indexer, ServerConfig],
) -> None:
    patcher, db, indexer, config = patcher_env
    doc = workspace / "doc_test.md"
    doc.write_text("# Titre\n\n## Section 1\n\nCorps 1\n\n## Section 2\n\nCorps 2\n", encoding="utf-8")
    indexer.index_workspace()

    res = patcher.delete_block(paths=["doc_test.md::Titre::Section 1", "doc_test.md::Titre::Section 2"])
    assert res["status"] == "success"
    assert res["deleted_count"] == 2
    assert len(res["results"]) == 2
