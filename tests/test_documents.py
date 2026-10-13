"""Tests for document indexing (Markdown, Word, PDF) and the ontology tools."""

import io
import json
import zipfile
from pathlib import Path

import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.documents import (
    DocxDocumentParser,
    DocumentRegistry,
    MarkdownDocumentParser,
    PdfDocumentParser,
    markdown_layout,
)
from ast_graph_server.indexer import Indexer
from ast_graph_server.server import ASTGraphServer

MD = "# Project Title\n\nIntro paragraph.\n\n## Section A\n\nBody A.\n\n### Sub A1\n\nBody A1.\n\n## Section B\n\nBody B.\n"
DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(parts) -> bytes:
    body = ""
    for kind, text in parts:
        if kind == "p":
            body += "<w:p><w:r><w:t>" + text + "</w:t></w:r></w:p>"
        else:
            level = kind.replace("h", "")
            body += (
                '<w:p><w:pPr><w:pStyle w:val="Heading' + level + '"/></w:pPr>'
                "<w:r><w:t>" + text + "</w:t></w:r></w:p>"
            )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="' + DOCX_NS + '"><w:body>' + body + "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def make_pdf(text: str = "Hello PDF World") -> bytes:
    content = "BT /F1 24 Tf 72 720 Td (" + text + ") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content.encode() + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
        b"startxref\n" + str(xref).encode() + b"\n%%EOF"
    )
    return bytes(out)


def make_env(tmp_path: Path, files) -> tuple:
    ws = tmp_path / "ws"
    ws.mkdir()
    for rel, data in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)
    return ws, config, db, indexer


def test_resolve_referenced_files() -> None:
    from ast_graph_server.documents import resolve_referenced_files

    mapping = {
        "LINQ-to-Spark.md": ["DataLinq/docs/LINQ-to-Spark.md"],
        "other.py": ["src/other.py"],
    }
    text = "Publish the guide in c:\\CodeSource\\DataLinq\\docs\\LINQ-to-Spark.md and update other.py."
    resolved = resolve_referenced_files(text, mapping)
    assert "DataLinq/docs/LINQ-to-Spark.md" in resolved
    assert "src/other.py" in resolved


@pytest.mark.anyio
async def test_referenced_files_in_document_tools(tmp_path: Path) -> None:
    ws, config, db, indexer = make_env(
        tmp_path,
        {
            "docs/guide.md": "# Guide\n\nPublish the result in docs/target.md.\n",
            "docs/target.md": "# Target\n\nBody.\n",
        },
    )
    indexer.index_workspace()
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    payload = json.loads(
        (await server.mcp.call_tool("read_block", {"path": "docs/guide.md#0", "format": "json"})).content[0].text
    )
    assert "docs/target.md" in payload["referenced_files"]




@pytest.mark.anyio
async def test_an_empty_search_says_which_filter_hid_the_symbol(tmp_path: Path) -> None:
    """Never assert a cause the tool has not checked.

    The first version of this message blamed "an external package" for a symbol that a
    file_pattern had merely filtered out, and an agent called it out: "the message's
    explanation was misleading since the symbol is local" (mapab2 b r2 of 7c0c5bb).
    """
    ws, config, db, indexer = make_env(
        tmp_path,
        {
            "src/Spark.cs": (
                "namespace DataLinq.Spark;\n\npublic class Spark\n{\n    public int Connect() => 0;\n}\n"
            )
        },
    )
    indexer.index_workspace()
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    payload = json.loads(
        (
            await server.mcp.call_tool(
                "search_symbols",
                {"query": "Spark.Connect", "path": "src/Other.cs", "format": "json"},
            )
        ).content[0].text
    )

    assert payload["count"] == 0
    assert "src/Other.cs" in payload["text"]
    assert "WITHOUT it" in payload["text"]
    assert "src/Spark.cs::Spark.Connect" in payload["text"]
    assert "external package" not in payload["text"]


@pytest.mark.anyio
async def test_an_empty_search_without_a_filter_admits_what_is_not_searched(tmp_path: Path) -> None:
    ws, config, db, indexer = make_env(tmp_path, {"src/a.py": "def f():\n    return 1\n"})
    indexer.index_workspace()
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    payload = json.loads(
        (await server.mcp.call_tool("search_symbols", {"query": "SparkSessionBuilder", "format": "json"})).content[0].text
    )

    assert payload["count"] == 0
    assert "names and ids" in payload["text"]
    assert "not indexed" in payload["text"]


@pytest.mark.anyio
async def test_a_folder_pattern_scopes_and_an_empty_search_explains_itself(tmp_path: Path) -> None:
    """A directory where a glob was expected used to return NOTHING, silently.

    Three such calls in one run cost three manual widenings (declared by the agent on
    mapab2 a r1 of the base c980d51: "no matches and no next-step guidance").
    """
    ws, config, db, indexer = make_env(tmp_path, {"docs/a.md": "# Guide\n\nBody about billing.\n"})
    indexer.index_workspace()
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    scoped = json.loads(
        (await server.mcp.call_tool("search_documents", {"query": "billing", "path": "docs", "format": "json"})).content[0].text
    )
    assert scoped["count"] == 1

    empty = json.loads(
        (await server.mcp.call_tool("search_documents", {"query": "billing", "path": "other", "format": "json"})).content[0].text
    )
    assert empty["count"] == 0
    assert "WITHOUT it" in empty["text"]
    assert "docs/a.md#guide" in empty["text"]
    # "other" selects no file at all, and the message says exactly that.
    assert "no indexed file matches that pattern" in empty["text"]
    # The note used to teach "(a folder is 'folder/*')" -- advice that outlived the
    # rule it described (a folder now scopes to its content, asserted above).
    assert "folder/*" not in empty["text"]

    # The other cause: the pattern DID select files, none of which carries the term.
    # No cause may be guessed here -- "no match" alone would send the agent looking
    # for a typo in a pattern that was applied correctly.
    absent = json.loads(
        (await server.mcp.call_tool(
            "search_documents", {"query": "invoicing", "path": "docs/a.md", "format": "json"}
        )).content[0].text
    )
    assert absent["count"] == 0
    assert "none carries the query" in absent["text"]
    assert "no indexed file matches" not in absent["text"]


@pytest.mark.anyio
async def test_an_empty_symbol_search_says_what_the_pattern_selected(tmp_path: Path) -> None:
    """The symbol note carries the same clause as the document one.

    It used to answer a filtered search with "and none matches anywhere in the index
    either" even when the pattern had narrowed nothing away -- a small lie that hides
    the real cause, which is that the pattern selects files that do not carry it.
    """
    ws, config, db, indexer = make_env(tmp_path, {"docs/a.md": "# Guide\n\nBody about billing.\n"})
    indexer.index_workspace()
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    empty = json.loads(
        (await server.mcp.call_tool(
            "search_symbols", {"query": "BillingService", "path": "docs", "format": "json"}
        )).content[0].text
    )
    assert empty["count"] == 0
    assert "no indexed file matches that pattern" not in empty["text"]
    assert "none carries the query" in empty["text"]

    nowhere = json.loads(
        (await server.mcp.call_tool(
            "search_symbols", {"query": "BillingService", "path": "nowhere/at/all", "format": "json"}
        )).content[0].text
    )
    assert nowhere["count"] == 0
    assert "no indexed file matches that pattern" in nowhere["text"]


def test_document_registry() -> None:
    registry = DocumentRegistry()
    assert isinstance(registry.get_parser_for_file("a.md"), MarkdownDocumentParser)
    assert isinstance(registry.get_parser_for_file("a.docx"), DocxDocumentParser)
    assert isinstance(registry.get_parser_for_file("a.pdf"), PdfDocumentParser)
    assert registry.get_parser_for_file("a.cs") is None


def test_markdown_sections() -> None:
    document = MarkdownDocumentParser().parse("docs/x.md", MD.encode("utf-8"))
    assert document.title == "Project Title"
    assert [section.heading for section in document.sections] == [
        "Project Title",
        "Section A",
        "Sub A1",
        "Section B",
    ]
    assert document.sections[2].content == "Body A1."


def test_docx_sections() -> None:
    data = make_docx([("h1", "Guide"), ("p", "Intro"), ("h2", "Setup"), ("p", "Steps")])
    document = DocxDocumentParser().parse("guide.docx", data)
    assert document.title == "Guide"
    assert [section.heading for section in document.sections] == ["Guide", "Setup"]
    assert document.sections[1].content == "Steps"


def test_pdf_sections() -> None:
    document = PdfDocumentParser().parse("report.pdf", make_pdf())
    assert document.doc_type == "pdf"
    assert document.sections
    assert "Hello PDF World" in document.sections[0].content


def test_documents_indexed_and_ontology(tmp_path: Path) -> None:
    ws, config, db, indexer = make_env(
        tmp_path,
        {
            "docs/spec.md": MD,
            "guide.docx": make_docx([("h1", "Guide"), ("p", "Intro"), ("h2", "Setup"), ("p", "Steps")]),
            "src/a.py": "def f():\n    return 1\n",
        },
    )
    count = indexer.index_workspace()
    assert count >= 3

    documents = {item["file_path"] for item in db.list_documents()}
    assert {"docs/spec.md", "guide.docx"} <= documents
    assert "src/a.py" not in documents

    document = db.get_document("docs/spec.md")
    assert document["title"] == "Project Title"
    assert document["section_count"] == 4

    toc = db.get_document_sections("docs/spec.md")
    assert [section["heading"] for section in toc] == ["Project Title", "Section A", "Sub A1", "Section B"]

    section = db.get_document_section("docs/spec.md#2")
    assert section["heading"] == "Sub A1"
    assert section["content"] == "Body A1."

    matches = db.search_documents("Body A1")
    assert matches and matches[0]["section_id"] == "docs/spec.md#section-a/sub-a1"

def test_a_stale_section_id_names_the_closest_one(tmp_path: Path) -> None:
    """An id is the slug of the CURRENT heading, so a rename renames the id.

    "section not found" alone made the caller read the whole outline to discover
    that (declared by the agent on mapab2 a r1 of the base 86872df).
    """
    ws, config, db, indexer = make_env(tmp_path, {"docs/spec.md": MD})
    indexer.index_workspace()

    result = db.read_document_sections(
        ["docs/spec.md#section-a/sub-a1", "docs/spec.md#section-a/sub-a1-renamed"]
    )

    assert [section["section_id"] for section in result["sections"]] == [
        "docs/spec.md#section-a/sub-a1"
    ]
    hint = result["errors"]["docs/spec.md#section-a/sub-a1-renamed"]
    assert "closest id" in hint
    assert "docs/spec.md#section-a/sub-a1" in hint


@pytest.mark.anyio
async def test_get_document_sections_batch(tmp_path: Path) -> None:
    ws, config, db, indexer = make_env(tmp_path, {"docs/spec.md": MD})
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    payload = json.loads(
        (
            await server.mcp.call_tool(
                "read_block",
                {
                    "paths": ["docs/spec.md#1", "docs/spec.md#2", "docs/spec.md#3"],
                    "format": "json",
                },
            )
        ).content[0].text
    )
    assert len(payload["blocks"]) == 3
    assert "Body A" in payload["blocks"][0]["numbered_text"]

    missing = json.loads(
        (
            await server.mcp.call_tool(
                "read_block", {"paths": ["docs/spec.md#99"], "format": "json"}
            )
        ).content[0].text
    )
    assert missing["blocks"][0]["status"] == "error"
    assert "docs/spec.md#99" in json.dumps(missing["blocks"][0])


@pytest.mark.anyio
async def test_project_map_tool(tmp_path: Path) -> None:
    ws, config, db, indexer = make_env(
        tmp_path,
        {"docs/spec.md": MD, "src/a.py": "class A:\n    def f(self):\n        pass\n"},
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    payload = json.loads(
        (await server.mcp.call_tool("get_project_map", {"budget_tokens": 1000, "format": "json"})).content[0].text
    )
    assert payload["summary"]["symbols"] >= 1
    assert any(item["file_path"] == "docs/spec.md" for item in payload["documents"])

    compact = (await server.mcp.call_tool("get_project_map", {"budget_tokens": 1000})).content[0].text
    assert "docs/" in compact and "spec.md" in compact
    assert len(compact) < len(json.dumps(payload))


@pytest.mark.anyio
async def test_an_empty_list_documents_says_why_it_is_empty(tmp_path: Path) -> None:
    """An empty body reads as "the file does not exist" for a file that is not a document.

    An agent asked about three .cs files, got `count 0` with no text at all, and had to
    infer the rule itself: "the empty success result is ambiguous" (mapab2 b r2 of the
    base c819545). The index knows the pattern matched source files, so the answer says so.
    """
    ws, config, db, indexer = make_env(
        tmp_path, {"docs/spec.md": MD, "src/a.py": "class A:\n    def f(self):\n        pass\n"}
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    code = json.loads(
        (await server.mcp.call_tool("search_documents", {"path": "src/a.py", "format": "json"})).content[0].text
    )
    assert code["count"] == 0
    assert "indexed file(s) do: src/a.py" in code["text"]
    assert "Markdown, Word and PDF" in code["text"]

    nowhere = json.loads(
        (await server.mcp.call_tool("search_documents", {"path": "nowhere/x.md", "format": "json"})).content[0].text
    )
    assert nowhere["count"] == 0
    assert "no indexed file matches it" in nowhere["text"]


@pytest.mark.anyio
async def test_a_pattern_that_matches_only_unparsed_files_says_so(tmp_path: Path) -> None:
    """A `.csproj` is on disk and in no index: "check the path" was the wrong hint.

    The agent asked for a csproj TargetFramework, was told to check a path it had right, and
    asked for the signal that this extension is simply never parsed (declared on mapab2 a r1
    of the base 93f3a9a).
    """
    ws, config, db, indexer = make_env(
        tmp_path,
        {"docs/spec.md": MD, "src/app.csproj": "<Project><PropertyGroup/></Project>\n"},
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    payload = json.loads(
        (await server.mcp.call_tool(
            "search_documents", {"query": "TargetFramework", "path": "src/*.csproj", "format": "json"}
        )).content[0].text
    )

    assert payload["count"] == 0
    assert "no INDEXED file matches it" in payload["text"]
    assert "1 file(s) do on disk" in payload["text"]

@pytest.mark.anyio
async def test_an_empty_project_map_section_says_what_the_filter_selected(tmp_path: Path) -> None:
    """A bare `files:` under a filter cannot be told from a path that does not exist.

    The agent asked for the map of `scripts/databricks/*` and concluded: "I could not
    confirm whether those two scripts exist on disk from this output" (mapab2 b r2 of the
    base c819545). A script declares no container, so this tool never lists it -- and now
    it says that instead of leaving the section blank.
    """
    ws, config, db, indexer = make_env(
        tmp_path, {"docs/spec.md": MD, "src/helper.py": "x = 1\n"}
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    empty = (await server.mcp.call_tool("get_project_map", {"path": "src/*"})).content[0].text
    assert "files:" in empty
    assert "indexed file(s) matching 'src/*' declares a container" in empty
    assert "no document among the 1 indexed file(s) matching 'src/*'" in empty

    nowhere = (await server.mcp.call_tool("get_project_map", {"path": "nowhere/*"})).content[0].text
    assert "no indexed file matches 'nowhere/*'" in nowhere

@pytest.mark.anyio
async def test_document_mcp_tools(tmp_path: Path) -> None:
    ws, config, db, indexer = make_env(tmp_path, {"docs/spec.md": MD})
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    listed = json.loads(
        (await server.mcp.call_tool("search_documents", {"format": "json"})).content[0].text
    )
    assert any(item["file_path"] == "docs/spec.md" for item in listed["documents"])

    searched = json.loads(
        (await server.mcp.call_tool("search_documents", {"query": "Sub A1", "format": "json"})).content[0].text
    )
    assert searched["matches"]
    section_id = searched["matches"][0]["section_id"]

    section = json.loads((await server.mcp.call_tool("read_block", {"path": section_id, "format": "json"})).content[0].text)
    assert "Body A1" in section["numbered_text"]

    toc_text = (
        await server.mcp.call_tool(
            "read_block", {"path": "docs/spec.md"}
        )
    ).content[0].text
    assert "# Project Title" in toc_text
    assert "## Section A" in toc_text

    toc = json.loads(
        (
            await server.mcp.call_tool(
                "read_block", {"path": "docs/spec.md", "format": "json"}
            )
        ).content[0].text
    )
    assert toc["status"] == "success"
    def _descendants(node):
        return sum(1 + _descendants(k) for k in (node.get("children") or []))

    # La liste d un .md est imbriquee : on compte les descendants, pas les enfants directs.
    assert _descendants(toc) == 4

    # delete_block dispatche sur la NATURE du bloc : un paragraphe de .md part par la route
    # document, un symbole par la route code. Le nom du tool ne dit plus le type, donc ce
    # dispatche doit etre epingle ici.
    deleted = json.loads(
        (await server.mcp.call_tool("delete_block", {"path": "docs/spec.md#project-title"})).content[0].text
    )
    assert deleted["status"] == "success"

    # Il n y a plus deux emissions a comparer : read_block rend UNE forme, et la liste d un .md
    # est deja le bloc compact (une seule enveloppe, imbriquee, sans identifiants repetes).

MD_FENCED = (
    "# Guide\n\n## Deploying\n\n"
    "```bash\n"
    "# install the worker\n"
    "# export the environment variables\n"
    "```\n\n"
    "### Submitting via CLI\n\nbody\n\n"
    "## Best Practices\n\nbody\n"
)


def test_a_hash_inside_a_code_fence_is_not_a_heading() -> None:
    """A "# comment" in a bash block is code. Read as a level-1 heading it swallowed

    every real section below it: wrong parents, wrong numbering, and an insert_block
    that refused a legitimate placement (measured on mapab2 b r1).
    """
    layout = markdown_layout(MD_FENCED)

    assert [item["heading"] for item in layout] == [
        "Guide",
        "Deploying",
        "Submitting via CLI",
        "Best Practices",
    ]
    assert [int(item["level"]) for item in layout] == [1, 2, 3, 2]


def test_an_unclosed_fence_swallows_the_rest_but_a_longer_one_closes_it() -> None:
    text = "# Guide\n\n```text\n# not a heading\n"
    assert [item["heading"] for item in markdown_layout(text)] == ["Guide"]

    reopened = "# Guide\n\n```text\n````\n## Section\nbody\n"
    assert [item["heading"] for item in markdown_layout(reopened)] == ["Guide", "Section"]
