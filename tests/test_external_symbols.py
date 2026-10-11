"""External (NuGet) symbols: the contract of the packages a .csproj references, CONTRACT ONLY.

Measured cause (03/10/2026): in 10 runs of 55 the agent wrote the Spark builder's type under an
invented name -- SparkSession.Builder (CS0426) or SparkSessionBuilder (CS0246) -- because the real
one, Microsoft.Spark.Sql.Builder, lives in the NuGet package and the workspace never spells it
(`var builder = SparkSession.Builder()`). search_symbols answered nothing and did not say the
symbol was external. Median cost: 12 repair turns per affected run.

Every test here builds a throw-away workspace AND a throw-away NuGet cache (NUGET_PACKAGES), so
nothing depends on what the machine happens to have restored.
"""

import json
import os
import time
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.externals import (
    parse_package_references,
    render_doc_type,
    resolve_package_dir,
)
from ast_graph_server.server import ASTGraphServer

SPARK_XML = """<?xml version="1.0"?>
<doc>
    <assembly><name>Fake.Spark</name></assembly>
    <members>
        <member name="T:Fake.Spark.Sql.SparkSession">
            <summary>The entry point to programming Spark.</summary>
        </member>
        <member name="M:Fake.Spark.Sql.SparkSession.Builder">
            <summary>Creates a Builder object for SparkSession.</summary>
            <returns>Builder object</returns>
        </member>
        <member name="M:Fake.Spark.Sql.SparkSession.Sql(System.String)">
            <summary>Executes a SQL query.</summary>
            <param name="sqlText">SQL query text</param>
            <returns>A <see cref="T:Fake.Spark.Sql.DataFrame"/>.</returns>
        </member>
        <member name="T:Fake.Spark.Sql.Builder">
            <summary>The builder of a <see cref="T:Fake.Spark.Sql.SparkSession"/>.</summary>
        </member>
        <member name="M:Fake.Spark.Sql.Builder.Config(System.String,System.String)">
            <summary>Sets a config option.</summary>
            <param name="key">Key</param>
            <param name="value">Value</param>
        </member>
        <member name="M:Fake.Spark.Sql.Builder.Config(System.String,System.Boolean)">
            <summary>Sets a config option.</summary>
            <param name="key">Key</param>
            <param name="value">Value</param>
        </member>
        <member name="M:Fake.Spark.Sql.Builder.GetOrCreate">
            <summary>Gets an existing SparkSession or creates a new one.</summary>
        </member>
        <member name="P:Fake.Spark.Sql.Builder.Options">
            <summary>The options set so far.</summary>
        </member>
        <member name="T:Fake.Spark.Sql.DataFrame">
            <summary>A distributed collection of rows.</summary>
        </member>
        <member name="M:Fake.Spark.Sql.DataFrame.Select(System.Collections.Generic.IEnumerable{System.String})">
            <summary>Selects columns.</summary>
            <param name="columns">Column names</param>
        </member>
        <member name="F:Fake.Spark.Sql.DataFrame.Nothing">
            <summary>A field: fields are not indexed.</summary>
        </member>
    </members>
</doc>
"""

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Fake.Spark" Version="2.3.0" />
    <PackageReference Include="Never.Restored" Version="9.9.9" />
  </ItemGroup>
</Project>
"""

PROGRAM = """namespace App
{
    public class SparkSessionFactory
    {
        public object Create()
        {
            var builder = SparkSession.Builder();
            return builder.GetOrCreate();
        }
    }
}
"""


def _write_package(nuget: Path, package: str, version: str, xml: str, tfm: str = "netstandard2.0") -> Path:
    folder = nuget / package.lower() / version / "lib" / tfm
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (package + ".xml")
    path.write_text(xml, encoding="utf-8")
    return path


@pytest.fixture
def nuget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "nuget"
    root.mkdir()
    monkeypatch.setenv("NUGET_PACKAGES", str(root))
    monkeypatch.delenv("SCALPEL_EXTERNAL_SYMBOLS", raising=False)
    _write_package(root, "Fake.Spark", "2.3.0", SPARK_XML)
    return root


def _workspace(tmp_path: Path, csproj: str = CSPROJ) -> Path:
    ws = tmp_path / "ws"
    (ws / "App").mkdir(parents=True)
    (ws / "App" / "App.csproj").write_text(csproj, encoding="utf-8")
    (ws / "App" / "Program.cs").write_text(PROGRAM, encoding="utf-8")
    return ws


def _server(ws: Path) -> ASTGraphServer:
    config = ServerConfig(workspace_root=ws, db_path=ws.parent / "graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    return server


def _payload(response):
    raw = response.content[0].text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw  # read_block returns a text answer as plain text


def _text(response) -> str:
    data = _payload(response)
    if isinstance(data, dict):
        return str(data.get("text", data))
    return str(data)


# -- search -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sparksession_builder_surfaces_the_real_builder_type_marked_external(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    response = await server.mcp.call_tool("search_symbols", {"query": "SparkSession Builder", "format": "json"})
    data = _payload(response)
    text = _text(response)
    assert "Fake.Spark.Sql.Builder [external_type] :: type Fake.Spark.Sql.Builder" in text
    assert "external -- package Fake.Spark 2.3.0, contract only, not editable" in text
    assert "Fake.Spark.Sql.SparkSession.Builder [external_method]" in text
    # The method that the agent mistook for a type says what it returns, and says it was inferred.
    assert "returns: Fake.Spark.Sql.Builder (inferred from the <returns> doc text" in text
    # The type lists its members: the names the agent is about to call.
    assert "members:" in text and "Config()" in text and "GetOrCreate()" in text
    # The summary travels with the contract.
    assert "doc: The builder of a Fake.Spark.Sql.SparkSession." in text
    # The count is the WORKSPACE count; externals are counted apart.
    assert data["count"] >= 1 and data["external_count"] >= 2


@pytest.mark.anyio
async def test_workspace_matches_stay_first(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    text = _text(await server.mcp.call_tool("search_symbols", {"query": "SparkSession Builder"}))
    workspace_line = text.index("SparkSessionFactory")
    external_section = text.index("external symbols (NuGet packages")
    assert workspace_line < external_section


@pytest.mark.anyio
async def test_builder_config_finds_the_builder_and_groups_the_overloads(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    text = _text(await server.mcp.call_tool("search_symbols", {"query": "Builder Config"}))
    assert "Fake.Spark.Sql.Builder.Config [external_method] :: Fake.Spark.Sql.Builder.Config(string key, string value)" in text
    assert "  overload: Fake.Spark.Sql.Builder.Config(string key, bool value)" in text
    assert text.count("Fake.Spark.Sql.Builder.Config [external_method]") == 1
    assert "Fake.Spark.Sql.Builder [external_type]" in text


@pytest.mark.anyio
async def test_the_invented_name_sparksessionbuilder_still_finds_the_real_one(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    text = _text(await server.mcp.call_tool("search_symbols", {"query": "SparkSessionBuilder"}))
    assert "Fake.Spark.Sql.SparkSession.Builder [external_method]" in text
    assert "Fake.Spark.Sql.Builder [external_type]" in text
    assert "listed because Fake.Spark.Sql.SparkSession.Builder returns it" in text


@pytest.mark.anyio
async def test_the_declaring_type_survives_longer_names_holding_both_terms(tmp_path: Path, nuget: Path) -> None:
    """Live check on the target: 'Builder Config' listed Builder.Config, then YamlDotNet's
    SerializerBuilder.ConfigureDefaultValuesHandling (both terms, as substrings), and never the
    Builder type itself once the workspace answered (cap 3)."""
    yaml = "<doc><members>" + "".join(
        '<member name="M:Yaml.S' + str(i) + 'erializerBuilder.ConfigureDefaults' + str(i) + '"><summary>x</summary></member>'
        for i in range(4)
    ) + "</members></doc>"
    _write_package(nuget, "Yaml", "1.0.0", yaml)
    csproj = CSPROJ.replace(
        '<PackageReference Include="Never.Restored" Version="9.9.9" />',
        '<PackageReference Include="Yaml" Version="1.0.0" />',
    )
    ws = _workspace(tmp_path, csproj)
    (ws / "App" / "Configs.cs").write_text(
        "namespace App { " + " ".join("public class ConfigBuilder" + str(i) + " { }" for i in range(6)) + " }",
        encoding="utf-8",
    )
    server = _server(ws)
    data = _payload(await server.mcp.call_tool("search_symbols", {"query": "Builder Config", "format": "json"}))
    assert data["count"] >= 5 and data["external_count"] == 3
    assert "Fake.Spark.Sql.Builder.Config [external_method]" in data["text"]
    assert "Fake.Spark.Sql.Builder [external_type]" in data["text"]
    assert "listed because Fake.Spark.Sql.Builder.Config declares it" in data["text"]


@pytest.mark.anyio
async def test_an_empty_workspace_answer_says_the_symbol_is_external(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    data = _payload(await server.mcp.call_tool("search_symbols", {"query": "GetOrCreate", "format": "json"}))
    assert data["count"] == 0
    assert "No symbol matches" in data["text"]
    assert "Fake.Spark.Sql.Builder.GetOrCreate [external_method]" in data["text"]
    assert "contract only, not editable" in data["text"]


@pytest.mark.anyio
async def test_a_path_filter_scopes_to_the_workspace_and_drops_externals(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    data = _payload(await server.mcp.call_tool("search_symbols", {"query": "Builder", "path": "App/**", "format": "json"}))
    assert "external" not in data["text"]
    assert "external_count" not in data


@pytest.mark.anyio
async def test_the_kind_filter_applies_to_externals(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    text = _text(await server.mcp.call_tool("search_symbols", {"query": "Builder", "kind": "method"}))
    assert "[external_method]" in text
    assert "[external_type]" not in text
    text = _text(await server.mcp.call_tool("search_symbols", {"query": "Builder", "kind": "namespace"}))
    assert "[external_" not in text


@pytest.mark.anyio
async def test_externals_are_capped(tmp_path: Path, nuget: Path) -> None:
    members = "".join(
        '<member name="T:Many.Things.Thing' + str(i) + '"><summary>Thing ' + str(i) + "</summary></member>"
        for i in range(40)
    )
    _write_package(nuget, "Many.Things", "1.0.0", "<doc><members>" + members + "</members></doc>")
    csproj = CSPROJ.replace(
        '<PackageReference Include="Never.Restored" Version="9.9.9" />',
        '<PackageReference Include="Many.Things" Version="1.0.0" />',
    )
    server = _server(_workspace(tmp_path, csproj))
    data = _payload(await server.mcp.call_tool("search_symbols", {"query": "Thing", "format": "json"}))
    assert data["count"] == 0
    assert data["external_count"] == 5
    # With workspace matches, the complement shrinks to 3.
    (tmp_path / "ws" / "App" / "Things.cs").write_text(
        "namespace App { " + " ".join("public class Thing" + str(i) + " { }" for i in range(6)) + " }",
        encoding="utf-8",
    )
    server.indexer.index_workspace()
    data = _payload(await server.mcp.call_tool("search_symbols", {"query": "Thing", "format": "json"}))
    assert data["count"] >= 5
    assert data["external_count"] == 3


@pytest.mark.anyio
async def test_the_json_form_carries_the_external_marking(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    data = _payload(await server.mcp.call_tool("search_symbols", {"query": "SparkSession Builder", "format": "json"}))
    assert all(not match.get("external") for match in data["matches"])
    builder = [m for m in data["external_matches"] if m["symbol_id"] == "Fake.Spark.Sql.Builder"]
    assert builder and builder[0]["external"] is True and builder[0]["editable"] is False
    assert builder[0]["kind"] == "external_type"
    assert builder[0]["package"] == "Fake.Spark" and builder[0]["versions"] == ["2.3.0"]


# -- read / write -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_block_on_an_external_type_returns_its_contract(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    text = _text(await server.mcp.call_tool("read_block", {"path": "Fake.Spark.Sql.Builder"}))
    assert "contract only, not editable" in text
    assert "there is no source text" in text
    assert "Fake.Spark.Sql.Builder.Config(string key, string value)" in text
    assert "Fake.Spark.Sql.Builder.Config(string key, bool value)" in text
    assert "Fake.Spark.Sql.Builder.GetOrCreate()" in text
    assert "[external_property] Fake.Spark.Sql.Builder.Options" in text


@pytest.mark.anyio
async def test_read_block_on_an_external_method_lists_its_overloads(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    text = _text(await server.mcp.call_tool("read_block", {"path": "Fake.Spark.Sql.Builder.Config(string, bool)"}))
    assert "Fake.Spark.Sql.Builder.Config(string key, string value)" in text
    assert "Fake.Spark.Sql.Builder.Config(string key, bool value)" in text


@pytest.mark.anyio
async def test_edit_block_refuses_an_external_symbol(tmp_path: Path, nuget: Path) -> None:
    ws = _workspace(tmp_path)
    server = _server(ws)
    before = (ws / "App" / "Program.cs").read_bytes()
    data = _payload(
        await server.mcp.call_tool(
            "edit_block",
            {"path": "Fake.Spark.Sql.Builder", "ops": [{"replace": [1, 1], "content": "x"}]},
        )
    )
    assert data["status"] == "error" and data["external"] is True
    assert "EXTERNAL symbol" in data["error"] and "Fake.Spark 2.3.0" in data["error"]
    assert "read_block(path='Fake.Spark.Sql.Builder')" in data["actionable_hint"]
    assert (ws / "App" / "Program.cs").read_bytes() == before


@pytest.mark.anyio
async def test_a_batch_naming_an_external_symbol_is_refused_whole(tmp_path: Path, nuget: Path) -> None:
    ws = _workspace(tmp_path)
    server = _server(ws)
    before = (ws / "App" / "Program.cs").read_bytes()
    data = _payload(
        await server.mcp.call_tool(
            "edit_block",
            {
                "edits": [
                    {
                        "path": "App/Program.cs::SparkSessionFactory::Create",
                        "ops": [{"insert_after": 1, "content": "            // touched"}],
                    },
                    {"path": "Fake.Spark.Sql.Builder.Config", "ops": [{"replace": [1, 1], "content": "x"}]},
                ]
            },
        )
    )
    assert data["status"] == "error" and data["applied"] == 0
    assert data["error"].startswith("entry 1 of edits:")
    assert (ws / "App" / "Program.cs").read_bytes() == before


@pytest.mark.anyio
async def test_delete_and_insert_refuse_an_external_symbol(tmp_path: Path, nuget: Path) -> None:
    server = _server(_workspace(tmp_path))
    deleted = _payload(await server.mcp.call_tool("delete_block", {"path": "Fake.Spark.Sql.Builder"}))
    assert deleted["status"] == "error" and deleted["external"] is True
    inserted = _payload(
        await server.mcp.call_tool("insert_block", {"parent": "Fake.Spark.Sql.Builder", "text": "void X() { }"})
    )
    assert inserted["status"] == "error" and inserted["external"] is True


@pytest.mark.anyio
async def test_a_workspace_path_is_never_shadowed_by_an_external_name(tmp_path: Path, nuget: Path) -> None:
    ws = _workspace(tmp_path)
    # A workspace FILE spelled like an external type: the workspace wins.
    (ws / "Fake.Spark.Sql.Builder").write_text("workspace text\n", encoding="utf-8")
    server = _server(ws)
    text = _text(await server.mcp.call_tool("read_block", {"path": "Fake.Spark.Sql.Builder"}))
    assert "workspace text" in text
    assert "contract only" not in text


# -- indexing -----------------------------------------------------------------------------


def test_the_package_is_parsed_once_then_served_from_the_cache(tmp_path: Path, nuget: Path) -> None:
    ws = _workspace(tmp_path)
    server = _server(ws)
    stats = server.indexer.externals.last_stats
    assert [entry.split(" (")[0] for entry in stats["indexed"]] == ["Fake.Spark 2.3.0"]
    assert stats["missing"] == ["Never.Restored 9.9.9"]
    server.indexer.index_workspace()
    stats = server.indexer.externals.last_stats
    assert stats["indexed"] == [] and stats["cached"] == ["Fake.Spark 2.3.0"]
    # The XML changes: the package is parsed again.
    xml = nuget / "fake.spark" / "2.3.0" / "lib" / "netstandard2.0" / "Fake.Spark.xml"
    xml.write_text(SPARK_XML.replace("Sets a config option.", "Sets ONE config option."), encoding="utf-8")
    os.utime(xml, (time.time() + 5, time.time() + 5))
    server.indexer.index_workspace()
    assert [entry.split(" (")[0] for entry in server.indexer.externals.last_stats["indexed"]] == ["Fake.Spark 2.3.0"]


def test_a_reference_removed_from_the_project_removes_its_symbols(tmp_path: Path, nuget: Path) -> None:
    ws = _workspace(tmp_path)
    server = _server(ws)
    assert server.indexer.externals.count() > 0
    (ws / "App" / "App.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"></Project>', encoding="utf-8")
    server.indexer.index_workspace()
    assert server.indexer.externals.count() == 0


def test_fields_and_unreferenced_packages_are_not_indexed(tmp_path: Path, nuget: Path) -> None:
    _write_package(nuget, "Not.Referenced", "1.0.0", '<doc><members><member name="T:Not.Referenced.X"/></members></doc>')
    server = _server(_workspace(tmp_path))
    externals = server.indexer.externals
    assert externals.lookup("Fake.Spark.Sql.DataFrame.Nothing") == []
    assert externals.lookup("Not.Referenced.X") == []
    assert externals.lookup("Fake.Spark.Sql.DataFrame")[0]["kind"] == "external_type"


def test_the_feature_can_be_switched_off(tmp_path: Path, nuget: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCALPEL_EXTERNAL_SYMBOLS", "0")
    server = _server(_workspace(tmp_path))
    assert server.indexer.externals is None


def test_central_package_versions_are_read(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "src" / "App").mkdir(parents=True)
    (ws / "Directory.Packages.props").write_text(
        '<Project><ItemGroup><PackageVersion Include="Fake.Spark" Version="2.3.0" /></ItemGroup></Project>',
        encoding="utf-8",
    )
    csproj = ws / "src" / "App" / "App.csproj"
    csproj.write_text(
        '<Project><ItemGroup><PackageReference Include="Fake.Spark" />'
        '<PackageReference Update="Other" Version="1.0.0" />'
        '<PackageReference Include="Prop.Version" Version="$(SomeVersion)" />'
        '<PackageReference Include="Child.Version"><Version>3.1.0</Version></PackageReference>'
        "</ItemGroup></Project>",
        encoding="utf-8",
    )
    assert parse_package_references(csproj, ws) == [
        ("Fake.Spark", "2.3.0"),
        ("Prop.Version", None),
        ("Child.Version", "3.1.0"),
    ]


def test_package_resolution_follows_the_nuget_minimum_rule(tmp_path: Path) -> None:
    for version in ("1.0.0", "1.2.0", "2.0.0"):
        (tmp_path / "pkg" / version).mkdir(parents=True)
    assert resolve_package_dir(tmp_path, "Pkg", "1.2.0").name == "1.2.0"
    assert resolve_package_dir(tmp_path, "Pkg", "1.1.0").name == "1.2.0"
    assert resolve_package_dir(tmp_path, "Pkg", "1.*").name == "1.2.0"
    assert resolve_package_dir(tmp_path, "Pkg", "[2.0.0]").name == "2.0.0"
    assert resolve_package_dir(tmp_path, "Pkg", "3.0.0") is None
    assert resolve_package_dir(tmp_path, "Absent", "1.0.0") is None


def test_doc_types_render_as_csharp() -> None:
    assert render_doc_type("System.Collections.Generic.IEnumerable{System.String}") == "IEnumerable<string>"
    assert render_doc_type("System.Int32[]") == "int[]"
    assert render_doc_type("System.Int32@") == "ref int"
    assert render_doc_type("System.Collections.Generic.Dictionary{System.String,``0}", (), ("TValue",)) == (
        "Dictionary<string, TValue>"
    )
    assert render_doc_type("Fake.Spark.Sql.Column") == "Fake.Spark.Sql.Column"
