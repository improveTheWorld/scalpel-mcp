"""The W5 tool defects, pinned where they were fixed (campaign registered_v2, commit ec572f1e).

Each case names the run, the turn and, where the campaign recorded it, the agent's own words.
These are the shapes that cost turns, and every one of them failed before the fix it pins.
"""

from pathlib import Path
import re

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

CHARGE = "src/billing.py::BillingService.charge"
REFUND = "src/billing.py::BillingService.refund"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    src = ws / "src"
    src.mkdir(parents=True)
    (src / "billing.py").write_text(
        '"""Billing module."""\n'
        "\n"
        "class BillingService:\n"
        '    """Handles payments."""\n'
        "\n"
        "    def charge(self, user_id: str, amount: float):\n"
        '        """Original charge method."""\n'
        "        return False\n"
        "\n"
        "    def refund(self, user_id: str) -> bool:\n"
        '        """Refund method."""\n'
        "        return True\n",
        encoding="utf-8",
    )
    docs = ws / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# Guide\n"
        "\n"
        "## Alpha Section\n"
        "\n"
        "Body of alpha.\n"
        "\n"
        "## Beta Section\n"
        "\n"
        "Body of beta.\n",
        encoding="utf-8",
    )
    return ws


@pytest.fixture
def patcher_env(workspace: Path) -> tuple:
    config = ServerConfig(workspace_root=workspace, db_path=workspace / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    patcher = Patcher(config, db, indexer)
    return patcher, db, indexer, config


def test_a_syntax_refusal_carries_its_own_diagnostic(patcher_env: tuple) -> None:
    """W5 1.2, scalpel_16k_round006_ordre1 t15: the refusal named no line.

    Six entries of ONE call came back with "Syntax validation failed. Operation rejected and file
    remains untouched.", while the line, the column and the offending snippet sat in a NEIGHBOURING
    field ('validation.errors') that the agent only read on its next turn.
    """
    patcher, db, indexer, config = patcher_env
    read = patcher.read_block(CHARGE)

    reply = patcher.edit_block(
        CHARGE,
        ops=[{"replace": [3, 3], "content": "        return (False"}],
        expected_hash=read["content_hash"],
    )

    assert reply["status"] == "error", reply
    detail = reply["validation"]["errors"][0]
    assert detail in reply["error"], reply["error"]
    assert re.search(r"line \d+:\d+", reply["error"]), reply["error"]


def test_a_batch_with_one_syntax_failure_writes_nothing(patcher_env: tuple, workspace: Path) -> None:
    """W5 1.2, t15 -> t17: half a batch is a file the caller cannot reason about.

    Seven entries were sent; one was applied and six were refused for syntax. By the next call the
    hash the agent reused was dead ("Stale read"), and the six were only written at t17.
    """
    patcher, db, indexer, config = patcher_env
    source = workspace / "src" / "billing.py"
    before = source.read_bytes()
    charge = patcher.read_block(CHARGE)
    refund = patcher.read_block(REFUND)

    reply = patcher._edit_batch([
        {
            "path": CHARGE,
            "expected_hash": charge["content_hash"],
            "ops": [{"replace": [3, 3], "content": "        return 1 > 0"}],
        },
        {
            "path": REFUND,
            "expected_hash": refund["content_hash"],
            "ops": [{"replace": [3, 3], "content": "        return (True"}],
        },
    ])

    assert reply["status"] == "error", reply
    assert reply["applied"] == 0 and reply["refused"] == 2, reply
    assert source.read_bytes() == before, "a refused batch wrote: " + repr(source.read_bytes())
    # Nothing was written, so every hash the caller sent still opens its block.
    assert patcher.read_block(CHARGE)["content_hash"] == charge["content_hash"]
    assert patcher.read_block(REFUND)["content_hash"] == refund["content_hash"]
    # The refused entry is named, and the refused-with-it entry says so.
    assert "entry 1" in reply["actionable_hint"], reply["actionable_hint"]
    assert reply["results"][1].get("batch_atomic") is True, reply["results"][1]


def test_a_batch_of_valid_entries_lands_whole(patcher_env: tuple, workspace: Path) -> None:
    """The other half of the rule: when every entry passes, the batch is applied as sent."""
    patcher, db, indexer, config = patcher_env
    source = workspace / "src" / "billing.py"
    charge = patcher.read_block(CHARGE)
    refund = patcher.read_block(REFUND)

    reply = patcher._edit_batch([
        {
            "path": CHARGE,
            "expected_hash": charge["content_hash"],
            "ops": [{"replace": [3, 3], "content": "        return 1 > 0"}],
        },
        {
            "path": REFUND,
            "expected_hash": refund["content_hash"],
            "ops": [{"replace": [3, 3], "content": "        return 2 > 0"}],
        },
    ])

    assert reply["applied"] == 2 and reply["refused"] == 0, reply
    text = source.read_text(encoding="utf-8")
    assert "return 1 > 0" in text and "return 2 > 0" in text


def test_a_dead_symbol_id_hands_over_the_nearest_addresses(patcher_env: tuple) -> None:
    """W5 1.8, scalpel_16k_round005_ordre0 t22: "the file exists, but it no longer declares a
    symbol with that id" named no living id, and the agent left the tool for search_symbols."""
    patcher, db, indexer, config = patcher_env

    reply = patcher.read_block("src/billing.py::BillingService.charge_gone")

    assert reply["status"] == "error", reply
    candidates = reply.get("candidates") or []
    assert candidates, reply
    assert all(one["path"].startswith("src/billing.py::") for one in candidates), candidates
    # What is named is an address the reader accepts, not a listing to go and fetch.
    assert patcher.read_block(candidates[0]["path"])["status"] == "success"


def test_a_dead_paragraph_id_hands_over_the_nearest_addresses(patcher_env: tuple) -> None:
    """The same defect on a document: scalpel_16k_round004_ordre0 t11."""
    patcher, db, indexer, config = patcher_env

    reply = patcher.read_block("docs/guide.md::Alfa Section")

    assert reply["status"] == "error", reply
    candidates = reply.get("candidates") or []
    assert candidates, reply
    assert patcher.read_block(candidates[0]["path"])["status"] == "success"


def test_a_namespace_refusal_points_at_a_tool_that_exists(patcher_env: tuple, workspace: Path) -> None:
    """W5 1.10, scalpel_16k_round007_ordre1 t24: the hint named "a text tool".

    The agent of that run: "the actionable_hint points to a text tool that does not exist on this
    surface" -- and the 11 tools of the campaign are read_file, read_block, edit_block, edit_file,
    grep, build, list_files, search_documents, search_symbols, insert_block, build_context.
    """
    patcher, db, indexer, config = patcher_env
    source = workspace / "src" / "Options.cs"
    source.write_text(
        "namespace Demo;\n"
        "\n"
        "public sealed class Options\n"
        "{\n"
        "    public int Value { get; set; }\n"
        "}\n",
        encoding="utf-8",
    )
    indexer.index_workspace()
    namespaces = [
        row for row in db.get_symbols_for_file("src/Options.cs") if row["kind"] == "namespace"
    ]
    assert namespaces, "the C# parser declares the namespace"

    reply = patcher.edit_block(
        str(namespaces[0]["id"]), ops=[{"replace": [1, 1], "content": "namespace Other;"}]
    )

    assert reply["status"] == "error", reply
    hint = reply["validation"]["actionable_hint"]
    assert "edit_block" in hint or "insert_block" in hint, hint
    # It may SAY the surface has no text tool; it must not tell the caller to use one.
    assert "use a text tool" not in hint.lower(), hint
    assert source.read_text(encoding="utf-8").startswith("namespace Demo;"), "refused, not written"


def test_a_write_outside_the_block_reports_what_it_wrote(patcher_env: tuple) -> None:
    """W5 4.1, scalpel_16k_round020_ordre1 t14: the reply's two evidence fields described the
    block BEFORE the write.

    The agent of that run: "the reply's numbered_text field returned the *unchanged* 85-line
    block rather than the newly created sibling, so the only evidence the enum had been created
    was the file_listing field."
    """
    patcher, db, indexer, config = patcher_env
    read = patcher.read_block(CHARGE)
    old_hash = read["content_hash"]

    reply = patcher.edit_block(
        CHARGE,
        ops=[
            {
                "insert_after": read["line_count"],
                "content": "\n    def created(self):\n        return 42\n",
            }
        ],
        expected_hash=old_hash,
    )

    assert reply["status"] == "success", reply
    assert reply["written_outside_block"] is True, reply
    assert reply["content_hash"] != old_hash, "the evidence is the block from before the write"
    assert "def created" in reply["numbered_text"], reply["numbered_text"]
    assert "return 42" in reply["numbered_text"], reply["numbered_text"]
    written_id = reply["written_block_id"]
    assert written_id.endswith("created"), written_id
    # The hash and the text are the ones an edit of THAT block expects, so they can be used.
    state = patcher.block_metrics(written_id)
    assert state is not None and state["content_hash"] == reply["content_hash"]
    assert state["numbered_text"] == reply["numbered_text"]
    assert state["line_count"] == reply["line_count"]

