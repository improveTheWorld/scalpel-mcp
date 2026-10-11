"""An edit of a C# namespace that ONLY adds using directives above it is served.

Measured on the campaign journals (03/10/2026): 8 calls on `SparkConnectOptions.cs::DataLinq.Spark`,
every one either `insert_after 0` of `using ...;` or `replace [1, 1]` re-typing the namespace
line unchanged under new usings, and every one refused as "a namespace declaration". Anything
that would rewrite the namespace keeps that refusal.
"""

from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

CS_FILE_SCOPED = """using System;

namespace DataLinq.Spark;

public class Options
{
    public int X { get; set; }
}
"""
NS = "src/Options.cs::DataLinq.Spark"


def make_env(tmp_path: Path, source: str = CS_FILE_SCOPED):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Options.cs").write_text(source, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, Patcher(config, db, indexer)


def _text(ws: Path) -> str:
    return (ws / "src" / "Options.cs").read_text(encoding="utf-8")


def test_insert_after_0_of_a_using_on_the_namespace_goes_to_the_using_region(tmp_path: Path) -> None:
    """Real case 1 (p1_r01, r05, r16, r17, b1_r09): insert_after 0 of `using X;`."""
    ws, patcher = make_env(tmp_path)
    read = patcher.read_block(NS)
    assert read["status"] == "success", read

    result = patcher.edit_block(
        NS,
        ops=[{"insert_after": 0, "content": "using Microsoft.Spark.Sql;\n"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    assert result.get("using_route") is True
    text = _text(ws)
    assert text.index("using System;") < text.index("using Microsoft.Spark.Sql;") < text.index(
        "namespace DataLinq.Spark;"
    ), text
    assert text.count("namespace DataLinq.Spark;") == 1
    assert any("NOT rewritten" in one for one in result["warnings"]), result


def test_replace_1_1_that_retypes_the_namespace_line_under_a_using_is_served(tmp_path: Path) -> None:
    """Real case 2 (p1_r06, r07): replace [1, 1] = `using ...;` + blank + the same namespace line."""
    ws, patcher = make_env(tmp_path)
    read = patcher.read_block(NS)

    result = patcher.edit_block(
        NS,
        ops=[{"replace": [1, 1], "content": "using Microsoft.Spark.Sql;\n\nnamespace DataLinq.Spark;"}],
        expected_hash=read["content_hash"],
    )

    assert result["status"] == "success", result
    text = _text(ws)
    assert "using Microsoft.Spark.Sql;" in text
    assert text.count("namespace DataLinq.Spark;") == 1, text
    assert text.index("using Microsoft.Spark.Sql;") < text.index("namespace DataLinq.Spark;")


def test_the_route_works_inside_a_batch(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)
    read = patcher.read_block(NS)

    reply = patcher._edit_batch([{
        "path": NS, "expected_hash": read["content_hash"],
        "ops": [{"insert_after": 0, "content": "using Microsoft.Spark.Sql;\n"}],
    }])

    assert reply["applied"] == 1, reply
    assert "using Microsoft.Spark.Sql;" in _text(ws)


# --------------------------------------------------------------------------- dangerous neighbours


def _refused_untouched(ws: Path, result: dict, before: str) -> None:
    assert result["status"] == "error", result
    assert "namespace declaration" in result["error"], result
    assert _text(ws) == before


def test_a_rewrite_of_the_namespace_line_stays_refused(tmp_path: Path) -> None:
    """The using is there, but the namespace name CHANGES: a rewrite, not an addition."""
    ws, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(NS)
    result = patcher.edit_block(
        NS,
        ops=[{"replace": [1, 1], "content": "using Microsoft.Spark.Sql;\n\nnamespace Other.Name;"}],
        expected_hash=read["content_hash"],
    )
    _refused_untouched(ws, result, before)


def test_a_using_mixed_with_other_code_stays_refused(tmp_path: Path) -> None:
    """A prefix that is not ONLY usings (here a class) is not the routable request."""
    ws, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(NS)
    result = patcher.edit_block(
        NS,
        ops=[{"insert_after": 0, "content": "using Microsoft.Spark.Sql;\npublic class Sneaky { }\n"}],
        expected_hash=read["content_hash"],
    )
    _refused_untouched(ws, result, before)


def test_a_using_statement_is_not_a_directive(tmp_path: Path) -> None:
    """`using var x = ...;` is a statement, never routed to the using region."""
    ws, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(NS)
    result = patcher.edit_block(
        NS,
        ops=[{"insert_after": 0, "content": "using var context = Spark.Connect();\n"}],
        expected_hash=read["content_hash"],
    )
    _refused_untouched(ws, result, before)


def test_a_using_already_in_the_file_keeps_the_refusal(tmp_path: Path) -> None:
    """Nothing to add: the tool does not pretend it did something."""
    ws, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(NS)
    result = patcher.edit_block(
        NS,
        ops=[{"insert_after": 0, "content": "using System;\n"}],
        expected_hash=read["content_hash"],
    )
    _refused_untouched(ws, result, before)


def test_a_delete_on_the_namespace_stays_refused(tmp_path: Path) -> None:
    ws, patcher = make_env(tmp_path)
    before = _text(ws)
    read = patcher.read_block(NS)
    result = patcher.edit_block(NS, ops=[{"delete": [1, 1]}], expected_hash=read["content_hash"])
    assert result["status"] == "error", result
    assert _text(ws) == before
