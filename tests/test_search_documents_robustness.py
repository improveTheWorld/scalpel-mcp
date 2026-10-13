import asyncio
import json
import time
import pytest
from pathlib import Path
from pydantic.fields import FieldInfo

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer
from ast_graph_server.database import path_matches
from ast_graph_server.documents import DocumentSection, ParsedDocument


def make_test_env(tmp_path: Path, files: dict) -> tuple:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        file_path = ws / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    cfg = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(cfg)
    server.initialize(enable_watcher=False)
    return ws, server


@pytest.mark.anyio
async def test_fieldinfo_direct_call_no_crash(tmp_path: Path):
    """Vérifie que search_documents ne plante plus avec TypeError: int() argument ... not 'FieldInfo'
    lors d'un appel direct Python sans arguments ou avec arguments par défaut."""
    ws, server = make_test_env(
        tmp_path,
        {"docs/guide.md": "# Guide\n\n## Installation\n\nInstallez le package avec pip."}
    )
    tool = server.mcp._tool_manager.get_tool("search_documents")

    # Appel direct de la fonction Python enregistrée
    res_default = await tool.fn(query="Installation")
    assert "Installation" in res_default
    assert "[section]" in res_default

    # Appel direct sans aucun argument (liste les documents)
    res_empty = await tool.fn()
    assert "docs/guide.md" in res_empty

    # Appel direct avec des FieldInfo simulés
    res_fieldinfo = await tool.fn(
        query=FieldInfo(default="Installation"),
        path=FieldInfo(default=None),
        limit=FieldInfo(default=5),
        full_top=FieldInfo(default=1),
        include_content=FieldInfo(default=True),
        format=FieldInfo(default="json")
    )
    data = json.loads(res_fieldinfo) if isinstance(res_fieldinfo, str) else res_fieldinfo
    assert data["status"] == "success"
    assert len(data["matches"]) >= 1


def test_database_manager_fieldinfo_robustness(tmp_path: Path):
    """Vérifie la robustesse défensive de DatabaseManager face aux objets FieldInfo."""
    ws, server = make_test_env(
        tmp_path,
        {"docs/doc1.md": "# Doc 1\n\nTexte 1", "docs/doc2.md": "# Doc 2\n\nTexte 2"}
    )
    db = server.db

    # path_matches avec FieldInfo
    assert path_matches("docs/doc1.md", FieldInfo(default="docs/*")) is True
    assert path_matches("docs/doc1.md", FieldInfo(default=None)) is True

    # list_documents avec FieldInfo
    docs = db.list_documents(file_pattern=FieldInfo(default=None), limit=FieldInfo(default=10))
    assert len(docs) == 2

    # search_documents avec FieldInfo sur tous les paramètres numériques
    matches = db.search_documents(
        query=FieldInfo(default="Texte"),
        file_pattern=FieldInfo(default="docs/**"),
        limit=FieldInfo(default=5),
        include_content=FieldInfo(default=True),
        max_content_chars=FieldInfo(default=1000),
        full_top=FieldInfo(default=1),
    )
    assert len(matches) >= 1
    assert matches[0]["content"] is not None


@pytest.mark.anyio
async def test_empty_section_content_hash(tmp_path: Path):
    """Vérifie qu'une section vide (titre seul) avec include_content=True obtient
    content_hash et numbered_text sans planter ni omettre les métriques."""
    ws, server = make_test_env(
        tmp_path,
        {"docs/empty.md": "# Titre Principal\n\n## Section Vide\n\n## Section Suivante\n\nAvec texte."}
    )
    res = await server.mcp.call_tool(
        "search_documents",
        {"query": "Vide", "include_content": True, "format": "json"}
    )
    payload = json.loads(res.content[0].text)
    assert payload["status"] == "success"
    assert len(payload["matches"]) == 1
    match = payload["matches"][0]
    assert match["heading"] == "Section Vide"
    assert "content_hash" in match
    assert "numbered_text" in match
    assert match["content_total_chars"] == 0


@pytest.mark.anyio
async def test_exotic_queries_and_like_wildcards(tmp_path: Path):
    """Vérifie le comportement avec des requêtes courtes, ponctuations ou wildcards LIKE."""
    ws, server = make_test_env(
        tmp_path,
        {
            "docs/csharp.md": "# C# Development\n\nUtilisation de C# 12 et .NET 8.\n\n## 100% Coverage\n\nObjectif 100% de tests.",
        }
    )
    # Requête de 2 caractères (ex: "c#")
    res_cs = await server.mcp.call_tool("search_documents", {"query": "c#", "format": "json"})
    data_cs = json.loads(res_cs.content[0].text)
    assert data_cs["status"] == "success"

    # Requête avec wildcard '%'
    res_pct = await server.mcp.call_tool("search_documents", {"query": "100%", "format": "json"})
    data_pct = json.loads(res_pct.content[0].text)
    assert data_pct["status"] == "success"

    # Requête avec ponctuation pure
    res_punct = await server.mcp.call_tool("search_documents", {"query": "???", "format": "json"})
    data_punct = json.loads(res_punct.content[0].text)
    assert data_punct["status"] == "success"


@pytest.mark.anyio
async def test_path_filter_variations(tmp_path: Path):
    """Vérifie tous les modes de filtrage de path (dossier nu, trailing slash, glob, exact)."""
    ws, server = make_test_env(
        tmp_path,
        {
            "sub/a.md": "# Sub A\n\nContenu A",
            "sub/deep/b.md": "# Deep B\n\nContenu B",
            "other/c.md": "# Other C\n\nContenu C",
        }
    )
    # Dossier nu
    r1 = await server.mcp.call_tool("search_documents", {"query": "Contenu", "path": "sub", "format": "json"})
    matches1 = json.loads(r1.content[0].text)["matches"]
    assert len(matches1) == 2

    # Dossier avec trailing slash
    r2 = await server.mcp.call_tool("search_documents", {"query": "Contenu", "path": "sub/", "format": "json"})
    matches2 = json.loads(r2.content[0].text)["matches"]
    assert len(matches2) == 2

    # Glob récursif
    r3 = await server.mcp.call_tool("search_documents", {"query": "Contenu", "path": "sub/**", "format": "json"})
    matches3 = json.loads(r3.content[0].text)["matches"]
    assert len(matches3) == 2

    # Fichier exact
    r4 = await server.mcp.call_tool("search_documents", {"query": "Contenu", "path": "other/c.md", "format": "json"})
    matches4 = json.loads(r4.content[0].text)["matches"]
    assert len(matches4) == 1
    assert matches4[0]["file_path"] == "other/c.md"


@pytest.mark.anyio
async def test_stress_and_large_volume(tmp_path: Path):
    """Stress-test : grand volume de documents, recherche multi-termes et gros contenus."""
    files = {}
    for i in range(40):
        sections = []
        for j in range(5):
            body = f"Section {j} du document {i}. MotClePartage information importante."
            if i == 0 and j == 0:
                # Gros contenu de 6000 caractères
                body += "\n" + ("Ligne de remplissage pour gros volume.\n" * 150)
            sections.append(f"## Section {j}\n\n{body}")
        files[f"docs/doc_{i:02d}.md"] = f"# Document {i}\n\n" + "\n\n".join(sections)

    ws, server = make_test_env(tmp_path, files)

    # 1. Listing complet sans query
    list_res = await server.mcp.call_tool("search_documents", {"format": "json"})
    list_data = json.loads(list_res.content[0].text)
    assert list_data["count"] == 40
    assert len(list_data["documents"]) == 40

    # 2. Recherche avec limit élevé et include_content=True
    search_res = await server.mcp.call_tool(
        "search_documents",
        {"query": "MotClePartage", "limit": 100, "include_content": True, "format": "json"}
    )
    search_data = json.loads(search_res.content[0].text)
    assert search_data["status"] == "success"
    assert search_data["count"] == 100

    # 3. Vérifier la gestion de la troncation sur le gros contenu
    big_match = next(m for m in search_data["matches"] if m["file_path"] == "docs/doc_00.md" and m["heading"] == "Section 0")
    assert big_match.get("content_truncated") is True
    assert big_match["content_total_chars"] > 4000
    assert len(big_match["content"]) == 4000

def test_search_documents_fts5_performance_and_special_queries(tmp_path: Path):
    """Vérifie que search_documents utilise FTS5 (< 30 ms sur 1000+ sections) et gère les opérateurs piégeux."""
    ws, server = make_test_env(
        tmp_path,
        {
            "docs/csharp.md": "# C# Development\n\nUtilisation de C# 12 et .NET 8.\n\n## 100% Coverage\n\nObjectif 100% de tests ???",
        },
    )
    db = server.db

    # Peupler 1200 sections pour valider la performance FTS5
    bulk_sections = [
        DocumentSection(
            heading=f"Section {i}",
            level=2,
            order_index=i,
            content=f"Documentation standard de la section {i} avec du texte générique.",
        )
        for i in range(1200)
    ]
    bulk_sections.append(
        DocumentSection(
            heading="Syntax & Operators Guide",
            level=2,
            order_index=1200,
            content='C++ programming with pointer* and AND logic and "quote UniqueFastToken2026',
        )
    )
    db.upsert_document(
        "docs/bulk.md",
        ParsedDocument(doc_type="markdown", title="Bulk Doc", sections=bulk_sections),
        content_hash="bulk_hash",
    )

    # Chauffer la connexion SQLite puis mesurer la recherche FTS5 sur > 1200 sections
    db.search_documents("UniqueFastToken2026", limit=8)
    t0 = time.perf_counter()
    fast_matches = db.search_documents("UniqueFastToken2026", limit=8)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert len(fast_matches) == 1
    assert fast_matches[0]["heading"] == "Syntax & Operators Guide"
    assert elapsed_ms < 30.0, f"search_documents FTS5 trop lent sur 1200 sections: {elapsed_ms:.2f} ms"

    # Requêtes avec opérateurs FTS5 piégeux ou caractères spéciaux (zéro OperationalError)
    for tricky_query, expected_heading in [
        ("logic AND", "Syntax & Operators Guide"),
        ('programming "quote', "Syntax & Operators Guide"),
        ("C++", "Syntax & Operators Guide"),
        ("100%", "100% Coverage"),
        ("???", "100% Coverage"),
        ("c#", "C# Development"),
    ]:
        res = db.search_documents(tricky_query, limit=8)
        assert res, f"Aucun résultat pour la requête piégeuse {tricky_query!r}"
        assert any(m["heading"] == expected_heading for m in res), (
            f"Heading {expected_heading!r} absent pour {tricky_query!r}: {res}"
        )


def test_get_project_map_sql_optimization_and_deduplication(tmp_path: Path):
    """Vérifie que get_project_map retourne les bons totaux summary et déduplique les titres H1 sans N+1."""
    doc_md = (
        "# Guide Architecture\n\n"
        "Intro guide.\n\n"
        "## Section 1\n\nTexte 1.\n\n"
        "## Section 2\n\nTexte 2.\n\n"
        "## Section 3\n\nTexte 3.\n\n"
        "## Section 4\n\nTexte 4.\n\n"
        "## Section 5\n\nTexte 5.\n\n"
        "## Section 6\n\nTexte 6.\n\n"
        "## Section 7\n\nTexte 7.\n"
    )
    ws, server = make_test_env(
        tmp_path,
        {
            "docs/arch.md": doc_md,
            "docs/second.md": "# Second Doc\n\n## Alpha\n\nBody alpha.\n",
            "src/core.py": "class CoreEngine:\n    def run(self):\n        return 42\n\ndef helper():\n    return CoreEngine().run()\n",
        },
    )
    fmap = server.db.get_project_map(budget_tokens=8000, answer_format="json")
    summary = fmap["summary"]
    assert summary["documents"] == 2
    assert summary["sections"] == 10  # 8 dans arch.md + 2 dans second.md
    assert summary["symbols"] >= 3
    assert summary["document_types"] == {"markdown": 2}
    assert "class" in summary["symbol_kinds"]

    docs_by_path = {d["file_path"]: d for d in fmap["documents"]}
    assert "docs/arch.md" in docs_by_path
    arch_doc = docs_by_path["docs/arch.md"]
    assert arch_doc["title"] == "Guide Architecture"
    assert arch_doc["sections"] == 8
    # H1 "Guide Architecture" dédupliqué, et plafonné aux 6 premiers H2
    assert arch_doc["section_titles"] == [
        "Section 1",
        "Section 2",
        "Section 3",
        "Section 4",
        "Section 5",
        "Section 6",
    ]
