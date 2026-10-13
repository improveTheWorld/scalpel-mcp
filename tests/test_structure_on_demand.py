"""read_block answers STRUCTURE for a path: a directory, a code file, a project file.

Roadmap section 12 -- everything is a block: a name, a parent, ordered children. A
directory has children, not a line range. read_block never returns raw file text, so the
model is not sent back to the flat file-by-file reading it was trained on.

The form is UNIQUE (docs/PATCH_READ_BLOCK_PATH.md section 1): the path is written ONCE,
then the block's own size, then the COMPLETE list of its children -- each with its SHORT
name inside its parent, its line span and its size -- nested by indentation. There is no
kind, no offset and no cap: a listing that stops early is a listing the caller cannot act
on, and the harness budget is 15x the worst listing of this repository.
"""

import json
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher

#: Keys the unique form does NOT carry any more. Asserted by absence, not by convention:
#: a field that comes back unnoticed is a field the next reader will start using.
REMOVED_KEYS = {
    "kind", "offset", "next_offset", "offset_beyond_end", "truncated",
    "child_ids", "child_ids_truncated", "child_next_offset", "entries", "entry_count",
    "symbols", "symbol_count", "sections", "section_count",
}


def make_env(base: Path):
    ws = base / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "src" / "sub").mkdir(parents=True)
    (ws / "docs" / "spec.md").write_text("# Titre\n\nintro\n\n## Section\ncorps\n", encoding="utf-8")
    (ws / "src" / "a.py").write_text(
        "class A:\n    def m(self):\n        pass\n\n    def n(self):\n        pass\n", encoding="utf-8"
    )
    (ws / "src" / "b.json").write_text('{"name": "x", "deps": {"a": 1}}\n', encoding="utf-8")
    (ws / "src" / "p.csproj").write_text(
        '<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>'
        '<ItemGroup><PackageReference Include="Microsoft.Spark" Version="2.1.0"/></ItemGroup></Project>\n',
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    Indexer(config, db).index_workspace()
    return ws, db, Patcher(config, db, Indexer(config, db))


def test_a_directory_is_a_block_with_children_not_a_range(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    result = patcher.read_block("src", answer_format="json")
    assert result["status"] == "success"
    # The path is written ONCE, and the type is carried by the name: '/' marks a directory.
    assert result["id"] == "src"
    assert result["line_count"] > 0 and result["bytes"] > 0
    assert "numbered_text" not in result
    assert REMOVED_KEYS & set(result) == set()
    children = {child["name"]: child for child in result["children"]}
    assert set(children) == {"a.py", "b.json", "p.csproj", "sub/"}
    # A directory has no size of its own: what it holds is one call away.
    assert set(children["sub/"]) == {"name"}
    assert set(children["a.py"]) == {"name", "lines", "bytes"}
    assert children["a.py"]["lines"][0] == 1


def test_a_code_file_is_outlined_with_spans_and_sizes(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    result = patcher.read_block("src/a.py", answer_format="json")
    assert result["status"] == "success"
    assert result["id"] == "src/a.py"
    assert result["line_count"] == 6 and result["bytes"] > 0
    assert REMOVED_KEYS & set(result) == set()
    holders = {child["name"]: child for child in result["children"]}
    assert {"A"} <= set(holders)
    # The hierarchy is the NESTING, not a repeated prefix: the members of A are named
    # 'm' and 'n' under it, never 'A.m' a second time.
    assert [child["name"] for child in holders["A"]["children"]] == ["m", "n"]
    assert all(set(child) == {"name", "lines", "bytes"} for child in holders["A"]["children"])
    assert "def m" not in str(result)


def test_a_markdown_file_lists_its_paragraphs_and_the_text_no_heading_owns(tmp_path: Path) -> None:
    """A .md path answers BOTH ways at once: its paragraph tree, and its root text.

    The root block -- the text no heading owns -- IS the file addressed by its path, so it
    keeps its editing buffer (numbered_text + content_hash) next to the complete list of
    its paragraphs.
    """
    ws, db, patcher = make_env(tmp_path)
    result = patcher.read_block("docs/spec.md", answer_format="json")
    assert result["status"] == "success"
    assert result["id"] == "docs/spec.md"
    assert result["line_count"] == 6 and result["chars"] > 0
    assert result["content_hash"] and "numbered_text" in result
    assert REMOVED_KEYS & set(result) == set()
    assert [child["name"] for child in result["children"]] == ["Titre"]
    section = result["children"][0]["children"][0]
    assert section["name"] == "Section"
    assert set(section) == {"name", "lines", "chars"}
    # A paragraph is named by its TITLE, never by the stored slug it is indexed under.
    assert "spec.md#" not in str(result)


def test_a_project_file_returns_a_structured_summary_not_the_xml(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    result = patcher.read_block("src/p.csproj", answer_format="json")
    assert result["status"] == "success"
    assert result["id"] == "src/p.csproj"
    assert result["line_count"] >= 1 and result["bytes"] > 0
    assert {"package": "Microsoft.Spark", "version": "2.1.0"} in result["package_references"]
    assert "net8.0" in result["target_frameworks"]
    assert "<Project>" not in str(result)


def test_a_json_file_returns_its_keys_not_its_text(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    result = patcher.read_block("src/b.json", answer_format="json")
    assert result["id"] == "src/b.json"
    assert set(result["top_level_keys"]) == {"name", "deps"}
    assert "deps" in result["nested_keys"]
    assert "numbered_text" not in result


def test_the_names_read_block_prints_are_the_paths_read_block_accepts(tmp_path: Path) -> None:
    """One model for both directions: a child is addressed by the name printed above."""
    ws, db, patcher = make_env(tmp_path)
    root = patcher.read_block("docs/spec.md", answer_format="json")
    name = root["children"][0]["name"]

    child = patcher.read_block("docs/spec.md::" + name)

    assert child["status"] == "success", child
    assert child["numbered_text"].startswith("1 | # Titre")


def make_big_env(base: Path):
    """One workspace holding each listing at a size past its old cap."""
    ws = base / "big"
    (ws / "data").mkdir(parents=True)
    for index in range(250):
        (ws / "data" / ("f%03d.txt" % index)).write_text("x\n", encoding="utf-8")
    (ws / "huge.md").write_text(
        "".join("## Section %d\n\ncorps\n\n" % index for index in range(400)), encoding="utf-8"
    )
    (ws / "many.py").write_text(
        "".join("class C%d:\n    pass\n\n" % index for index in range(320)), encoding="utf-8"
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    Indexer(config, db).index_workspace()
    return ws, db, Patcher(config, db, Indexer(config, db))


def test_a_huge_markdown_listing_is_complete_never_capped_and_never_paged(tmp_path: Path) -> None:
    """The outline used to stop at 300 and page, and 5 000 headings were unreachable."""
    ws, db, patcher = make_big_env(tmp_path)

    page = patcher.read_block("huge.md", answer_format="json")

    assert [child["name"] for child in page["children"]][:2] == ["Section 0", "Section 1"]
    assert len(page["children"]) == 400
    assert REMOVED_KEYS & set(page) == set()
    assert len(str(page)) < 200000


def test_a_directory_listing_is_complete_and_never_paged(tmp_path: Path) -> None:
    ws, db, patcher = make_big_env(tmp_path)

    listing = patcher.read_block("data", answer_format="json")

    assert len(listing["children"]) == 250
    assert {child["name"] for child in listing["children"]} >= {"f000.txt", "f249.txt"}
    assert REMOVED_KEYS & set(listing) == set()


def test_a_code_outline_is_complete_and_never_paged(tmp_path: Path) -> None:
    ws, db, patcher = make_big_env(tmp_path)

    outline = patcher.read_block("many.py", answer_format="json")

    assert [child["name"] for child in outline["children"]][:2] == ["C0", "C1"]
    assert len(outline["children"]) == 320
    assert REMOVED_KEYS & set(outline) == set()


def test_an_exported_typescript_declaration_is_indexed(tmp_path: Path) -> None:
    """`export class X` hides the declaration inside an export_statement.

    The parser never entered the wrapper, so every exported declaration vanished and an
    exported-only .ts read as "declares no container" -- a false refusal on an indexed file.
    """
    ws = tmp_path / "ts"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "exported.ts").write_text(
        "export class A {}\n\nexport interface B {}\n\nexport function c(): void {}\n",
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    Indexer(config, db).index_workspace()
    patcher = Patcher(config, db, Indexer(config, db))

    outline = patcher.read_block("src/exported.ts", answer_format="json")

    assert outline["status"] == "success", outline
    assert {"A", "B", "c"} <= {child["name"] for child in outline["children"]}


def make_summary_env(base: Path):
    ws = base / "proj"
    ws.mkdir(parents=True)
    (ws / "bad.csproj").write_text("<Project><PropertyGroup>", encoding="utf-8")
    (ws / "bad.json").write_text("{not json", encoding="utf-8")
    (ws / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    (ws / "p.props").write_text(
        "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>",
        encoding="utf-8",
    )
    (ws / "c.yaml").write_text("name: x\ndeps:\n  a: 1\n", encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    Indexer(config, db).index_workspace()
    return ws, db, Patcher(config, db, Indexer(config, db))


def test_a_recognised_project_file_that_does_not_parse_says_so(tmp_path: Path) -> None:
    """The old refusal claimed the extension was unsupported, which was flatly false."""
    ws, db, patcher = make_summary_env(tmp_path)

    csproj = patcher.read_block("bad.csproj")
    assert csproj["status"] == "error"
    assert "recognises" in csproj["error"] and "does not parse" in csproj["error"]

    bad_json = patcher.read_block("bad.json")
    assert bad_json["status"] == "error"
    assert "does not parse" in bad_json["error"]

    array = patcher.read_block("list.json")
    assert array["status"] == "error"
    assert "valid JSON" in array["error"]


def test_the_xml_family_and_yaml_are_summarised_without_claiming_validation(tmp_path: Path) -> None:
    ws, db, patcher = make_summary_env(tmp_path)

    props = patcher.read_block("p.props", answer_format="json")
    assert props["status"] == "success"
    assert props["target_frameworks"] == ["net8.0"]

    yaml = patcher.read_block("c.yaml", answer_format="json")
    assert yaml["top_level_keys"] == ["name", "deps"]
    assert yaml["validated"] is False


def test_an_ignored_tree_names_the_pattern_and_hides_the_names(tmp_path: Path) -> None:
    """An ignored directory used to read as EMPTY, with only a count: no way to tell.

    The PATTERN is named, never the hidden file names: explaining an empty listing must
    not publish what the ignore list exists to hide.
    """
    ws = tmp_path / "ign"
    (ws / "node_modules").mkdir(parents=True)
    (ws / "node_modules" / "dep.js").write_text("export const x = 1;\n", encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path, workspace_root=ws)
    Indexer(config, db).index_workspace()
    patcher = Patcher(config, db, Indexer(config, db))

    result = patcher.read_block("node_modules", answer_format="json")

    assert result["status"] == "success"
    assert result["ignored_entries"] >= 1
    assert "node_modules" in result["ignored_by"]
    assert "dep.js" not in json.dumps(result)


def test_read_block_has_no_offset_parameter(tmp_path: Path) -> None:
    """No pagination anywhere: the parameter is GONE, not dropped in silence.

    Dropping it in silence was the bug the old refusal existed for: a paging agent
    re-read page one and could not tell.
    """
    ws, db, patcher = make_env(tmp_path)

    with pytest.raises(TypeError):
        patcher.read_block("docs/spec.md", offset=5)

def test_a_js_file_without_extracted_symbols_returns_file_text(tmp_path: Path) -> None:
    ws, db, patcher = make_env(tmp_path)
    (ws / "src" / "client.js").write_text(
        "import './polyfill.js';\nconsole.log('client ready');\n",
        encoding="utf-8",
    )
    Indexer(patcher.config, db).index_workspace()

    result = patcher.read_block("src/client.js")
    assert result["status"] == "success", result
    assert result["kind"] == "file_text"
    assert result["content_hash"]
    assert "console.log('client ready');" in result["numbered_text"]
