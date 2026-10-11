"""Tests for C# (.cs) support: parsing, mutation and imports."""

import json
from pathlib import Path
from typing import Dict, Tuple

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indentation import IndentationManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.parsers import ParserRegistry
from ast_graph_server.parsers.csharp_parser import CSharpParser
from ast_graph_server.patcher import Patcher

CS_SOURCE = '''using System;

namespace Billing
{
    public class BillingService
    {
        /// <summary>Process a payment.</summary>
        public int Charge(int amount)
        {
            Audit.Log(amount);
            return amount;
        }
    }
}
'''


def make_env(tmp_path: Path, files: Dict[str, str]) -> Tuple[Path, ServerConfig, DatabaseManager, Indexer, Patcher]:
    ws = tmp_path / "ws"
    ws.mkdir()
    for rel, content in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return ws, config, db, indexer, Patcher(config, db, indexer)


def test_csharp_registered() -> None:
    assert isinstance(ParserRegistry().get_parser_for_file("Foo.cs"), CSharpParser)
    assert isinstance(ParserRegistry().get_parser_for_file("src/Nested/Bar.cs"), CSharpParser)


def test_csharp_symbols_signatures_and_calls() -> None:
    parser = CSharpParser()
    result = parser.parse_file("src/Foo.cs", CS_SOURCE.encode("utf-8"))
    assert result.syntax_ok

    by_id = {symbol.id: symbol for symbol in result.symbols}
    assert "src/Foo.cs::Billing.BillingService" in by_id
    method = by_id["src/Foo.cs::Billing.BillingService.Charge"]
    assert method.kind == "method"
    assert method.signature == "public int Charge(int amount)"
    assert "Process a payment" in (method.docstring or "")
    assert any(call.callee_name == "Audit.Log" for call in result.calls)


def test_csharp_uses_specific_kinds() -> None:
    source = '''namespace N
{
    public interface I { void M(); }
    public struct S { public int X; }
    public record R(int Id);
    public enum E { A, B }
    public class C
    {
        public C() { }
        public int P { get; set; }
        public void M2() { }
    }
}
'''
    parser = CSharpParser()
    result = parser.parse_file("src/K.cs", source.encode("utf-8"))
    kinds = {symbol.id: symbol.kind for symbol in result.symbols}
    assert kinds["src/K.cs::N"] == "namespace"
    assert kinds["src/K.cs::N.I"] == "interface"
    assert kinds["src/K.cs::N.S"] == "struct"
    assert kinds["src/K.cs::N.R"] == "record"
    assert kinds["src/K.cs::N.E"] == "enum"
    assert kinds["src/K.cs::N.C"] == "class"
    assert kinds["src/K.cs::N.C.C"] == "constructor"
    assert kinds["src/K.cs::N.C.P"] == "property"
    assert kinds["src/K.cs::N.C.M2"] == "method"


def test_csharp_overloads_get_unique_and_order_independent_ids() -> None:
    """Overload ids must be a function of the declaration, not of file order.

    The walk numbered them #2, #3 in encounter order, so reordering two overloads
    swapped their ids and an id captured before the edit then designated another
    declaration.
    """
    parser = CSharpParser()
    forward = "public class C { public void M(int a) {} public void M(string b) {} public int P { get; set; } public int P(int i) { return i; } }"
    reverse = "public class C { public int P(int i) { return i; } public void M(string b) {} public void M(int a) {} public int P { get; set; } }"

    def mid(source: str):
        result = parser.parse_file("src/C.cs", source.encode("utf-8"))
        ids = [symbol.id for symbol in result.symbols]
        assert len(ids) == len(set(ids))
        return sorted(i for i in ids if "::C.M#" in i)

    forward_ids = mid(forward)
    reverse_ids = mid(reverse)
    assert len(forward_ids) == 2
    assert "#" in forward_ids[0]
    assert forward_ids == reverse_ids

def test_csharp_distinct_generic_arities_keep_distinct_ids() -> None:
    """Same name, different type parameters: the ids must stay distinct.

    This repository declares MultiTypeCategoryMapper<T, R1, R2> and
    ...<T, R1, R2, R3> with identical member signatures. A suffix derived from
    the signature alone collapsed them, and the index died on
    UNIQUE constraint failed: symbols.id.
    """
    source = (
        "internal class M<T, R1> { public string G(string p) => p; }\n"
        "internal class M<T, R1, R2> { public string G(string p) => p; }\n"
    )
    parser = CSharpParser()
    result = parser.parse_file("src/G.cs", source.encode("utf-8"))
    ids = [symbol.id for symbol in result.symbols]
    assert len(ids) == len(set(ids)), ids


def test_csharp_syntax_barrier() -> None:
    parser = CSharpParser()
    ok, errors = parser.validate_syntax(b"public class X { public void M( { }", "Foo.cs")
    assert not ok
    assert errors and errors[0].actionable_hint


def test_csharp_update_symbol_body(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path, {"src/Foo.cs": CS_SOURCE})
    new_code = "public int Charge(int amount)\n{\n    Audit.Log(amount * 2);\n    return amount * 2;\n}"

    result = patcher.update_symbol_body("src/Foo.cs::Billing.BillingService.Charge", new_code)

    assert result["status"] == "success", result
    text = (ws / "src/Foo.cs").read_text(encoding="utf-8")
    assert "amount * 2" in text
    assert "namespace Billing" in text
    assert "using System;" in text


def test_scalpel_toolset_indexes_csharp(tmp_path: Path) -> None:
    import asyncio
    from ast_graph_server.server import ASTGraphServer
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "Foo.cs").write_text(CS_SOURCE, encoding="utf-8")
    server = ASTGraphServer(ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db"))
    server.initialize(enable_watcher=False)

    found = server.db.search_symbols(query="Charge")
    assert any("Charge" in match["symbol_id"] for match in found)


def test_csharp_indentation_protects_verbatim_string() -> None:
    manager = IndentationManager()
    code = 'public void M()\n{\n    var sql = @"SELECT 1\nFROM t";\n}'
    rebased = manager.rebase_indentation(code, target_col=4, file_path="Foo.cs")
    lines = rebased.splitlines()
    assert lines[0] == "    public void M()"
    assert lines[3] == 'FROM t";'

FILE_SCOPED_CS = '''using System;

namespace DataLinq.SparkQuery.UnitTests;

public class CriteriaTests
{
    [Fact]
    public void Existing()
    {
        Assert.True(true);
    }
}
'''


def test_csharp_insert_member_lands_on_one_ast_level(tmp_path: Path) -> None:
    """A member inserted into a file-scoped-namespace class lands at ONE indent
    step, not two.

    insert_symbol used `code.strip()` before re-basing: strip de-indents only the
    first line, so every following line was shifted one level too far. The member
    came out at 8 columns; update_symbol_body then re-based edits onto that wrong
    column and answered noop, so the agent looped on a cosmetic re-indentation.
    """
    ws, config, db, indexer, patcher = make_env(tmp_path, {"src/T.cs": FILE_SCOPED_CS})
    code = (
        "    [Fact]\n"
        "    public void Added()\n"
        "    {\n"
        "        var x = 1;\n"
        "    }"
    )

    result = patcher.insert_symbol("src/T.cs::CriteriaTests", code)

    assert result["status"] == "success", result
    assert result["indent"]["columns"] == 4
    text = (ws / "src/T.cs").read_text(encoding="utf-8")
    assert "\n    [Fact]\n    public void Added()\n" in text
    assert "\n        [Fact]" not in text


def test_csharp_update_repairs_a_wrong_column(tmp_path: Path) -> None:
    """The target column comes from the AST depth, not from the bytes, so an
    update can repair a member an earlier bug left at the wrong column. Before,
    update re-based onto the existing column and answered noop: the wrong
    indentation could never be fixed through the tool."""
    bad = FILE_SCOPED_CS.replace(
        "    [Fact]\n    public void Existing()\n    {\n        Assert.True(true);\n    }",
        "    [Fact]\n        public void Existing()\n        {\n            Assert.True(true);\n        }",
    )
    ws, config, db, indexer, patcher = make_env(tmp_path, {"src/T.cs": bad})

    result = patcher.update_symbol_body(
        "src/T.cs::CriteriaTests.Existing",
        "public void Existing()\n{\n    Assert.True(true);\n}",
    )

    assert result["status"] == "success", result
    assert result["indent"]["columns"] == 4
    text = (ws / "src/T.cs").read_text(encoding="utf-8")
    assert "\n    public void Existing()\n" in text

def test_update_symbol_body_fragments_ignore_indentation(tmp_path: Path) -> None:
    """The agent quotes a few lines at ANY indentation; the tool locates and re-bases.

    Resending the whole block measured ~1 900 characters per edit, 99.3% identical
    on a one-line change. Fragment mode sends only the changed text and, since the
    tool owns indentation, the quoted text may sit at column 0.
    """
    ws, config, db, indexer, patcher = make_env(tmp_path, {"src/T.cs": FILE_SCOPED_CS})

    result = patcher.update_symbol_body(
        "src/T.cs::CriteriaTests.Existing",
        edits=[{"old": "Assert.True(true);", "new": "Assert.True(false);"}],
    )

    assert result["status"] == "success", result
    text = (ws / "src/T.cs").read_text(encoding="utf-8")
    # Applied, and re-based to the body's real column (8), not to column 0.
    assert "\n        Assert.True(false);\n" in text
    assert "Assert.True(true);" not in text


def test_update_symbol_body_fragment_must_be_unique(tmp_path: Path) -> None:
    """An ambiguous fragment writes nothing: a silent wrong target is worse than an error."""
    source = "public class C\n{\n    public void M()\n    {\n        var a = 1;\n        var b = 1;\n    }\n}\n"
    ws, config, db, indexer, patcher = make_env(tmp_path, {"src/T.cs": source})
    before = (ws / "src/T.cs").read_text(encoding="utf-8")

    result = patcher.update_symbol_body("src/T.cs::C.M", edits=[{"old": "= 1;", "new": "= 2;"}])

    assert result["status"] == "error", result
    assert "2 times" in result["error"]
    assert (ws / "src/T.cs").read_text(encoding="utf-8") == before


def test_update_symbol_body_applies_several_fragments_atomically(tmp_path: Path) -> None:
    ws, config, db, indexer, patcher = make_env(tmp_path, {"src/T.cs": FILE_SCOPED_CS})
    result = patcher.update_symbol_body(
        "src/T.cs::CriteriaTests.Existing",
        edits=[
            {"old": "Assert.True(true);", "new": "Assert.False(false);"},
            {"old": "public void Existing()", "new": "public void Existing()"},
        ],
    )
    assert result["status"] == "success", result
    assert "Assert.False(false);" in (ws / "src/T.cs").read_text(encoding="utf-8")


def test_csharp_file_scoped_mutations_converge(tmp_path: Path) -> None:
    """insert -> update -> delete on a file-scoped-namespace class must converge.

    One run burned 17 mutations deleting and re-inserting the same five test
    methods: the insert landed at 8 columns and update could never repair it.
    """
    ws, config, db, indexer, patcher = make_env(tmp_path, {"src/T.cs": FILE_SCOPED_CS})
    code = "    [Fact]\n    public void Added()\n    {\n        var x = 1;\n    }"

    inserted = patcher.insert_symbol("src/T.cs::CriteriaTests", code)
    assert inserted["status"] == "success", inserted
    assert inserted["indent"]["columns"] == 4
    assert "\n    [Fact]\n    public void Added()\n" in (ws / "src/T.cs").read_text(encoding="utf-8")

    # Re-submitting the exact stored body (attribute included: the parser's span
    # covers `[Fact]` and the leading indentation) changes nothing, and says why.
    again = patcher.update_symbol_body(
        "src/T.cs::CriteriaTests.Added",
        "    [Fact]\n    public void Added()\n    {\n        var x = 1;\n    }",
    )
    assert again["status"] == "noop", again
    assert again["reason"] == "identical_after_reindent"
    assert again["indent"]["columns"] == 4

    deleted = patcher.delete_symbol("src/T.cs::CriteriaTests.Added")
    assert deleted["status"] == "success", deleted
    assert "Added" not in (ws / "src/T.cs").read_text(encoding="utf-8")

CS_FILE_SCOPED = """using System;

namespace DataLinq.Spark;

public class Spark
{
    public static int Connect() => 0;
}
"""


def test_a_member_never_lands_before_a_file_scoped_namespace(tmp_path: Path) -> None:
    """CS8956: a member before `namespace X;` parses as C# but does not compile.

    On mapab2 a r1 the tool answered success with syntax_ok=True and the build failed
    two turns later, on code the agent had every reason to trust.
    """
    ws, config, db, indexer, patcher = make_env(tmp_path, {"src/Spark.cs": CS_FILE_SCOPED})
    code = "public enum SparkAuthMode\n{\n    Pat,\n}\n"

    inside = patcher.insert_symbol("src/Spark.cs::DataLinq.Spark", code, position="start")
    assert inside["status"] == "success", inside
    text = (ws / "src" / "Spark.cs").read_text(encoding="utf-8")
    assert text.index("namespace DataLinq.Spark;") < text.index("enum SparkAuthMode")
    assert text.index("enum SparkAuthMode") < text.index("class Spark")

    # The module-level form had the same hole: the first symbol of the file IS the
    # namespace, so "position=start" inserted before the declaration.
    (ws / "src" / "Spark.cs").write_text(CS_FILE_SCOPED, encoding="utf-8")
    indexer.index_file(ws / "src" / "Spark.cs")
    module = patcher.insert_symbol("src/Spark.cs", code, position="start")
    assert module["status"] == "success", module
    text = (ws / "src" / "Spark.cs").read_text(encoding="utf-8")
    assert text.index("namespace DataLinq.Spark;") < text.index("enum SparkAuthMode")
