import json
from pathlib import Path
import pytest

from ast_graph_server.rendering import render_project_map


def test_render_project_map_tree_hierarchy_and_sections():
    payload = {
        "budget_tokens": 8000,
        "used_tokens_estimate": 100,
        "truncated": False,
        "summary": {"symbols": 10, "documents": 3, "sections": 5},
        "documents": [
            {
                "file_path": "README.md",
                "title": "Scalpel",
                "section_titles": ["Présentation", "Usage"],
                "sections": 2,
            },
            {
                "file_path": "docs/spec.md",
                "title": "Spécification",
                "section_titles": ["Vue d'ensemble", "Architecture"],
                "sections": 2,
            },
            {
                "file_path": "docs/guides/intro.md",
                "title": "Guide de démarrage",
                "section_titles": ["Prérequis", "Installation"],
                "sections": 2,
            },
        ],
        "files": [
            {
                "file_path": "main.py",
                "symbols": ["class App"],
            },
            {
                "file_path": "src/utils.py",
                "symbols": ["class Helper"],
            },
            {
                "file_path": "src/parser/a.py",
                "symbols": ["class A", "class Token"],
            },
            {
                "file_path": "src/parser/b.py",
                "symbols": ["class B"],
            },
        ],
    }

    result = render_project_map(payload)

    # Vérification que les dossiers parents sont factorisés et n'apparaissent qu'une fois
    lines = result.splitlines()
    
    # 1. Vérification des sections de documents nommées
    assert "Vue d'ensemble, Architecture" in result or ("Vue d'ensemble" in result and "Architecture" in result)
    assert "Prérequis, Installation" in result or ("Prérequis" in result and "Installation" in result)

    # 2. Vérification de l'arborescence des dossiers
    # 'docs/' doit apparaître comme dossier parent
    docs_dir_lines = [l for l in lines if l.strip() == "docs/"]
    assert len(docs_dir_lines) == 1, f"Expected exactly 1 'docs/' line, got: {docs_dir_lines}"
    
    # 'guides/' doit être indenté sous 'docs/'
    guides_dir_lines = [l for l in lines if l.strip() == "guides/"]
    assert len(guides_dir_lines) == 1, f"Expected exactly 1 'guides/' line, got: {guides_dir_lines}"
    
    # 'intro.md' ne doit pas porter le préfixe complet répété 'docs/guides/intro.md'
    assert "docs/guides/intro.md" not in result
    assert "intro.md (2 sections)" in result
    assert "::Prérequis" in result
    assert "::Installation" in result
    # 3. Vérification de l'arborescence des fichiers de code
    src_dir_lines = [l for l in lines if l.strip() == "src/"]
    assert len(src_dir_lines) == 1, f"Expected exactly 1 'src/' line, got: {src_dir_lines}"
    
    parser_dir_lines = [l for l in lines if l.strip() == "parser/"]
    assert len(parser_dir_lines) == 1, f"Expected exactly 1 'parser/' line, got: {parser_dir_lines}"
    
    assert "src/parser/a.py" not in result
    assert "a.py (2 symboles)" in result
    assert "::class A" in result
    assert "::class Token" in result
    assert "b.py (1 symboles)" in result
    assert "::class B" in result


@pytest.mark.anyio
async def test_get_project_map_integration_tree_and_sections(tmp_path: Path):
    from tests.test_documents import make_env
    from ast_graph_server.server import ASTGraphServer

    doc_content = "# Guide\n\n## Prerequis\n\nContenu\n\n## Installation\n\nContenu\n"
    py_content = "class ParserA:\n    pass\n"

    ws, config, db, indexer = make_env(
        tmp_path,
        {
            "docs/guides/intro.md": doc_content,
            "src/parser/a.py": py_content,
        },
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    text_map = (await server.mcp.call_tool("get_project_map", {"budget_tokens": 1000})).content[0].text
    lines = text_map.splitlines()

    # Le dossier 'docs/' puis 'guides/'
    assert any(l.strip() == "docs/" for l in lines)
    assert any(l.strip() == "guides/" for l in lines)
    assert any("intro.md" in l for l in lines)
    assert any("::Prerequis" in l for l in lines)

    # Le dossier 'src/' puis 'parser/'
    assert any(l.strip() == "src/" for l in lines)
    assert any(l.strip() == "parser/" for l in lines)
    assert any("a.py" in l for l in lines)
    assert any("::class ParserA" in l for l in lines)


@pytest.mark.anyio
async def test_get_project_map_max_depth_filtering(tmp_path: Path):
    from tests.test_documents import make_env
    from ast_graph_server.server import ASTGraphServer

    ws, config, db, indexer = make_env(
        tmp_path,
        {
            "root_doc.md": "# Root Document\n",
            "deep/sub/doc.md": "# Deep Document\n",
            "root_file.py": "class RootClass:\n    pass\n",
            "deep/sub/file.py": "class DeepClass:\n    pass\n",
        },
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    # max_depth = 1 : only root-level files
    res_depth1 = (await server.mcp.call_tool("get_project_map", {"budget_tokens": 1000, "max_depth": 1})).content[0].text
    assert "root_doc.md" in res_depth1
    assert "root_file.py" in res_depth1
    assert "deep/" not in res_depth1
    assert "sub/" not in res_depth1
    assert "DeepClass" not in res_depth1
    assert "Deep Document" not in res_depth1
    # max_depth = None : all files
    res_all = (await server.mcp.call_tool("get_project_map", {"budget_tokens": 1000})).content[0].text
    assert "deep/" in res_all
    assert "doc.md" in res_all

@pytest.mark.anyio
async def test_get_project_map_auto_indexes_uninitialized_workspace_and_lists_file_text_fallbacks(tmp_path: Path):
    from ast_graph_server.config import ServerConfig
    from ast_graph_server.server import ASTGraphServer

    # 1. Workspace avec du code et un doc mais SANS appel préalable à server.initialize()
    ws1 = tmp_path / "fresh_ws"
    ws1.mkdir()
    (ws1 / "README.md").write_text("# Fresh Project\n\n## Intro\n\nHello.\n", encoding="utf-8")
    (ws1 / "app.py").write_text("class FreshApp:\n    def start(self):\n        pass\n", encoding="utf-8")

    cfg1 = ServerConfig(workspace_root=ws1, db_path=ws1 / ".agent_graph.db")
    server1 = ASTGraphServer(cfg1)
    # Aucun appel à server1.initialize() ! Simule le 1er tour d'un agent sur un nouveau workspace
    text_map1 = (await server1.mcp.call_tool("get_project_map", {"budget_tokens": 2000})).content[0].text
    assert "README.md" in text_map1
    assert "::Intro" in text_map1
    assert "app.py" in text_map1
    assert "::class FreshApp" in text_map1

    # 2. Workspace composé uniquement de scripts/fichiers sans conteneur AST (.ps1, .toml, JS IIFE)
    ws2 = tmp_path / "scripts_ws"
    ws2.mkdir()
    (ws2 / "deploy.ps1").write_text("Write-Host 'Deploying'\nWrite-Host 'Done'\n", encoding="utf-8")
    (ws2 / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    cfg2 = ServerConfig(workspace_root=ws2, db_path=ws2 / ".agent_graph.db")
    server2 = ASTGraphServer(cfg2)
    text_map2 = (await server2.mcp.call_tool("get_project_map", {"budget_tokens": 2000})).content[0].text
    assert "deploy.ps1 (file_text, 2L)" in text_map2
    assert "pyproject.toml (file_text, 2L)" in text_map2
    assert "(none declares a container" not in text_map2

    # 3. Workspace réellement vide sur disque
    ws3 = tmp_path / "empty_ws"
    ws3.mkdir()
    cfg3 = ServerConfig(workspace_root=ws3, db_path=ws3 / ".agent_graph.db")
    server3 = ASTGraphServer(cfg3)
    text_map3 = (await server3.mcp.call_tool("get_project_map", {"budget_tokens": 2000})).content[0].text
    assert "workspace directory is empty" in text_map3
