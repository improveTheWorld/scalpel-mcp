"""The listing ANSWER FORMAT: the JSON object tree, or the text form of the same listing.

Scope, pinned here: the format covers LISTINGS only -- a code file's symbols, a folder's
entries, a project file's properties. The TEXT of a block (numbered lines + content hash),
the refusals and the write replies answer the same way in both modes, so a campaign that
compares the two renders varies the listing and nothing else.
"""

import json
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher


OPTIONS_CS = """namespace Demo.Pwaf
{
    public sealed class SparkOptions
    {
        public string Host { get; set; }

        public void ApplyTo(Builder builder) { }

        public void ApplyTo(Builder builder, bool force) { }

        public SparkContext Connect(
            string master,
            string appName) => null;
    }
}
"""

OTHER_CS = """namespace Demo.Pwaf
{
    public class Builder
    {
        public int Depth { get; set; }
    }
}
"""

PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.Spark" Version="2.3.0" />
  </ItemGroup>
</Project>
"""

GUIDE_MD = "# Guide\n\n## Alpha\n\nalpha text\n"


@pytest.fixture
def listing_env(tmp_path: Path) -> tuple[Patcher, ServerConfig]:
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)
    (ws / "src" / "Options.cs").write_text(OPTIONS_CS, encoding="utf-8")
    (ws / "src" / "Builder.cs").write_text(OTHER_CS, encoding="utf-8")
    (ws / "src" / "Demo.csproj").write_text(PROJECT, encoding="utf-8")
    (ws / "docs" / "guide.md").write_text(GUIDE_MD, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    indexer.index_workspace()
    return Patcher(config, db, indexer), config


def test_the_default_format_is_the_text_listing(listing_env) -> None:
    """The shipped default is TEXT: the object tree is asked for, not assumed."""
    patcher, config = listing_env
    assert config.answer_format == "text"
    payload = patcher.read_block("src/Options.cs")
    assert payload["format"] == "text"
    assert payload["text"].startswith("src/Options.cs   ")
    tree = patcher.read_block("src/Options.cs", answer_format="json")
    assert tree["status"] == "success"
    assert tree["children"][0]["name"] == "Demo.Pwaf"
    assert "format" not in tree and "text" not in tree


def test_text_mode_prints_the_address_then_the_tree(listing_env) -> None:
    patcher, config = listing_env
    payload = patcher.read_block("src/Options.cs", answer_format="text")
    assert payload["status"] == "success"
    assert payload["format"] == "text"
    assert payload["id"] == "src/Options.cs"
    text = payload["text"]
    # The block's own address is the FIRST line, written once: the children below it are
    # indentation only, and the file name is never repeated.
    assert text.splitlines()[0].startswith("src/Options.cs   ")
    assert "symbols" in text.splitlines()[0]
    assert "Options.cs" not in text.splitlines()[1]
    # A container is its kind and its name; a member carries what the name cannot say.
    assert "\n  namespace Demo.Pwaf  (" in text
    assert "\n    class SparkOptions  (" in text
    assert "\n      property Host -> string  (" in text
    # An overload prints the discriminator it is ADDRESSED by, once.
    assert "\n      method ApplyTo(Builder) -> void  (" in text
    assert "\n      method ApplyTo(Builder, bool) -> void  (" in text
    # A declaration written across several lines collapses into one line.
    assert "\n      method Connect(string master, string appName) -> SparkContext  (" in text


def test_the_call_format_overrides_the_server_default(listing_env) -> None:
    patcher, config = listing_env
    assert patcher.read_block("src/Options.cs", answer_format="text")["format"] == "text"
    config.answer_format = "text"
    payload = patcher.read_block("src/Options.cs", answer_format="json")
    assert payload["status"] == "success"
    assert "children" in payload and "text" not in payload


def test_a_directory_lists_its_entries_as_text(listing_env) -> None:
    patcher, _config = listing_env
    payload = patcher.read_block("src", answer_format="text")
    first, *rest = payload["text"].splitlines()
    assert first.startswith("src   ")
    assert "3 entries" in first
    assert "\n  Options.cs   " in payload["text"]
    assert any(line.strip().startswith("Builder.cs") for line in rest)


def test_a_project_file_lists_its_named_properties_as_text(listing_env) -> None:
    patcher, _config = listing_env
    payload = patcher.read_block("src/Demo.csproj", answer_format="text")
    text = payload["text"]
    assert text.splitlines()[0].startswith("src/Demo.csproj   ")
    assert "\n  target_frameworks: net8.0" in text
    assert "\n  package_references:" in text
    assert "\n    Microsoft.Spark 2.3.0" in text


def test_the_scope_is_listings_only(listing_env) -> None:
    """The block TEXT and a refusal answer the same in both modes; document roots support text listing on demand."""
    patcher, config = listing_env
    config.answer_format = "text"
    block = patcher.read_block("src/Options.cs::SparkOptions")
    assert " 1 | " in block["numbered_text"]
    assert block["content_hash"]
    assert "format" not in block and "text" not in block
    # Direct read without format keeps the structured dictionary
    document_raw = patcher.read_block("docs/guide.md")
    assert document_raw["children"][0]["name"] == "Guide"
    assert document_raw["content_hash"]
    assert "format" not in document_raw
    # Text listing format is supported when requested or via MCP surface
    document_text = patcher.read_block("docs/guide.md", answer_format="text")
    assert document_text["format"] == "text"
    assert "# Guide" in document_text["text"]
    assert "## Alpha" in document_text["text"]
    refusal = patcher.read_block("src/Missing.cs")
    assert refusal["status"] == "error"
    assert "format" not in refusal


def test_the_mcp_surface_honours_the_format(tmp_path: Path) -> None:
    """The regression: 'format' is a BUILTIN, and the helper that called the patcher was a
    sibling of the tool function, so it read the builtin instead of the argument -- every
    answer stayed JSON while the patcher, called directly, answered text.
    """
    import asyncio

    from ast_graph_server.server import ASTGraphServer

    ws = tmp_path / "mcp"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "Options.cs").write_text(OPTIONS_CS, encoding="utf-8")
    (ws / "src" / "Builder.cs").write_text(OTHER_CS, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    config.answer_format = "text"
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    try:
        default = asyncio.run(server.mcp.call_tool("read_block", {"path": "src/Options.cs"}))
        # AS TEXT: the line breaks arrive as line breaks, not as an escaped JSON field.
        assert default.content[0].text.startswith("src/Options.cs   ")
        assert "\n  namespace Demo.Pwaf  (" in default.content[0].text
        forced = asyncio.run(
            server.mcp.call_tool("read_block", {"path": "src/Options.cs", "format": "json"})
        )
        assert "format" not in json.loads(forced.content[0].text)
        batched = asyncio.run(
            server.mcp.call_tool(
                "read_block",
                {"paths": ["src/Options.cs", "src/Builder.cs"], "format": "text"},
            )
        )
        joined = batched.content[0].text
        assert joined.startswith("src/Options.cs   ") and "\n\nsrc/Builder.cs   " in joined
    finally:
        server.stop() if hasattr(server, "stop") else None


def test_the_environment_sets_the_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AST_GRAPH_ANSWER_FORMAT", raising=False)
    assert ServerConfig.from_env(workspace_arg=str(tmp_path)).answer_format == "text"
    monkeypatch.setenv("AST_GRAPH_ANSWER_FORMAT", "json")
    assert ServerConfig.from_env(workspace_arg=str(tmp_path)).answer_format == "json"


def test_markdown_outline_and_short_hash_edits(listing_env) -> None:
    """Markdown outline renders compact text with short hashes, and edit_block accepts short hash prefixes."""
    patcher, config = listing_env
    # 1. Text outline rendering
    outline = patcher.read_block("docs/guide.md", answer_format="text")
    assert outline["format"] == "text"
    lines = outline["text"].splitlines()
    assert lines[0].startswith("docs/guide.md   ")
    assert "# Guide" in lines[1] and "(2 lines)" in lines[1] and "[h:" not in lines[1]
    assert "## Alpha" in lines[2] and "(3 lines)" in lines[2] and "[h:" not in lines[2]

    # 2. Member path starting with '::' resolves correctly
    alpha = patcher.read_block("::Alpha")
    assert alpha["status"] == "success"
    assert alpha["file_path"] == "docs/guide.md"
    assert "alpha text" in alpha["numbered_text"]

    # 3. edit_block succeeds with 8-character short hash
    full_hash = alpha["content_hash"]
    short_hash = full_hash[:8]
    assert len(short_hash) == 8
    res = patcher.edit_block(
        block_id="::Alpha",
        expected_hash=short_hash,
        ops=[{"replace": [1, 1], "content": "## Alpha\nupdated alpha text\n"}],
    )
    assert res["status"] == "success"
    assert res.get("file_path") == "docs/guide.md" or "docs/guide.md" in str(res.get("id"))

