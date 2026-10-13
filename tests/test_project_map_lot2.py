import json
from pathlib import Path
import pytest
from ast_graph_server.rendering import (
    render_project_map,
    _format_doc_map_entry,
    _format_file_map_entry,
)
from ast_graph_server.server import ASTGraphServer
from tests.test_documents import make_env


def test_format_doc_map_entry_multiline_and_no_commas():
    doc = {
        "file_path": "docs/guide.md",
        "title": "Guide Utilisateur",
        "sections": 4,
        "section_titles": ["Introduction", "Installation", "Configuration", "Avance"],
    }
    rendered = _format_doc_map_entry("guide.md", doc, depth=1)
    lines = rendered.splitlines()

    assert lines[0] == "  guide.md (4 sections)"
    assert lines[1] == "    ::Introduction"
    assert lines[2] == "    ::Installation"
    assert lines[3] == "    ::Configuration"
    assert lines[4] == "    ::Avance"
    # Vérifier qu'aucune virgule n'est utilisée pour séparer les sections
    assert "," not in rendered


def test_format_doc_map_entry_truncation_marker():
    doc = {
        "file_path": "docs/big_doc.md",
        "title": "Big Doc",
        "sections": 12,
        "section_titles": [
            "Section 1",
            "Section 2",
            "Section 3",
            "Section 4",
            "Section 5",
            "Section 6",
        ],
    }
    rendered = _format_doc_map_entry("big_doc.md", doc, depth=1)
    lines = rendered.splitlines()

    assert lines[0] == "  big_doc.md (12 sections)"
    # Affiche au maximum 6 (ou 5) sections puis le marqueur
    assert "::Section 1" in rendered
    assert "::Section 2" in rendered
    assert any(line.strip().startswith("::... (+") and "autres sections)" in line for line in lines)


def test_format_file_map_entry_multiline_with_loc():
    file_entry = {
        "file_path": "src/parser.py",
        "sym_count": 2,
        "loc": 150,
        "symbol_details": [
            {"kind": "class", "name": "ParserA", "start_line": 10, "end_line": 90},
            {"kind": "def", "name": "parse_item", "start_line": 95, "end_line": 150},
        ],
    }
    rendered = _format_file_map_entry("parser.py", file_entry, depth=1)
    lines = rendered.splitlines()

    assert lines[0] == "  parser.py (2 symboles, 150L)"
    assert lines[1] == "    ::class ParserA (L10-L90)"
    assert lines[2] == "    ::def parse_item (L95-L150)"


def test_format_file_map_entry_truncation_marker_over_6_symbols():
    symbols_details = [
        {"kind": "def", "name": f"func_{i}", "start_line": i * 10, "end_line": i * 10 + 5}
        for i in range(1, 11)
    ]
    file_entry = {
        "file_path": "src/many_funcs.py",
        "sym_count": 10,
        "loc": 100,
        "symbol_details": symbols_details,
    }
    rendered = _format_file_map_entry("many_funcs.py", file_entry, depth=1)
    lines = rendered.splitlines()

    assert lines[0] == "  many_funcs.py (10 symboles, 100L)"
    # Max 6 affichés
    displayed_symbols = [l for l in lines if l.strip().startswith("::def func_")]
    assert len(displayed_symbols) == 6
    # Le marqueur avec les 4 restants (+4 autres symboles)
    assert any(l.strip() == "::... (+4 autres symboles)" for l in lines)


@pytest.mark.anyio
async def test_get_project_map_lot2_integration(tmp_path: Path):
    doc_content = "# Architecture\n\n## Composants\n\nDetail 1\n\n## Flux\n\nDetail 2\n"
    # Créer un fichier python avec 8 fonctions
    py_lines = []
    for i in range(1, 9):
        py_lines.append(f"def helper_{i}():\n    pass\n")
    py_content = "\n".join(py_lines)

    ws, config, db, indexer = make_env(
        tmp_path,
        {
            "docs/archi.md": doc_content,
            "src/helpers.py": py_content,
        },
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    # 1. Test du format texte
    text_map = (await server.mcp.call_tool("get_project_map", {"budget_tokens": 2000})).content[0].text
    lines = text_map.splitlines()

    # Documents : sous-lignes dédiées avec ::, sans virgule
    assert any("archi.md" in l and "sections" in l for l in lines)
    assert any(l.strip() == "::Composants" for l in lines)
    assert any(l.strip() == "::Flux" for l in lines)
    doc_archi_lines = [l for l in lines if "archi.md" in l]
    assert "," not in doc_archi_lines[0]

    # Files : sous-lignes dédiées avec ::, tronqué à 6 symboles
    assert any("helpers.py" in l and "8 symboles" in l for l in lines)
    assert any(l.strip().startswith("::function helper_1") for l in lines)
    assert any(l.strip().startswith("::function helper_6") for l in lines)
    assert not any(l.strip().startswith("::function helper_7") for l in lines)
    assert any("::... (+2 autres symboles)" in l for l in lines)

    # 2. Test de la compatibilité JSON
    json_res = json.loads(
        (await server.mcp.call_tool("get_project_map", {"budget_tokens": 2000, "format": "json"})).content[0].text
    )
    assert "documents" in json_res
    assert "files" in json_res
    doc_entry = next(d for d in json_res["documents"] if "archi.md" in d["file_path"])
    assert doc_entry["sections"] >= 2
    assert "Composants" in doc_entry["section_titles"]

    file_entry = next(f for f in json_res["files"] if "helpers.py" in f["file_path"])
    assert file_entry["sym_count"] == 8
    assert "symbol_details" in file_entry
    assert len(file_entry["symbol_details"]) == 6
    assert file_entry["symbol_details"][0]["name"] == "helper_1"