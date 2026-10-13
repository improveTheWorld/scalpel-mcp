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
    assert "intro.md :: Guide de démarrage" in result

    # 3. Vérification de l'arborescence des fichiers de code
    src_dir_lines = [l for l in lines if l.strip() == "src/"]
    assert len(src_dir_lines) == 1, f"Expected exactly 1 'src/' line, got: {src_dir_lines}"
    
    parser_dir_lines = [l for l in lines if l.strip() == "parser/"]
    assert len(parser_dir_lines) == 1, f"Expected exactly 1 'parser/' line, got: {parser_dir_lines}"
    
    assert "src/parser/a.py" not in result
    assert "a.py :: class A, class Token" in result
    assert "b.py :: class B" in result


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
    assert any("intro.md" in l and "Prerequis" in l for l in lines)

    # Le dossier 'src/' puis 'parser/'
    assert any(l.strip() == "src/" for l in lines)
    assert any(l.strip() == "parser/" for l in lines)
    assert any("a.py :: class ParserA" in l for l in lines)


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