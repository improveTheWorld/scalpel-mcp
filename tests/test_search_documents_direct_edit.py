import json
from pathlib import Path
import pytest
import re

from tests.test_documents import make_env
from ast_graph_server.server import ASTGraphServer


@pytest.mark.anyio
async def test_search_documents_direct_edit_text_and_json(tmp_path: Path):
    doc_content = (
        "# Installation Guide\n\n"
        "## Prerequis\n\n"
        "Python 3.10 ou superieur est requis.\n"
        "Verifier la version avec python --version.\n\n"
        "## Configuration\n\n"
        "Configurer les variables d'environnement.\n"
    )

    ws, config, db, indexer = make_env(
        tmp_path,
        {"docs/guide.md": doc_content},
    )
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)

    # 1. Appel en format text avec include_content=True
    text_res = (
        await server.mcp.call_tool(
            "search_documents",
            {"query": "Prerequis", "include_content": True, "format": "text"},
        )
    ).content[0].text

    # Vérification de l'en-tête de bloc standard et des lignes numérotées
    assert "[section]" in text_res
    assert "[h:" in text_res
    assert re.search(r"\[h:[0-9a-f]{8}\] \(\d+ lines\)", text_res) is not None
    assert "1 |" in text_res

    # 2. Appel en format JSON
    json_res = json.loads(
        (
            await server.mcp.call_tool(
                "search_documents",
                {"query": "Prerequis", "include_content": True, "format": "json"},
            )
        ).content[0].text
    )

    assert json_res["status"] == "success"
    assert len(json_res["matches"]) >= 1
    match = json_res["matches"][0]
    assert "content_hash" in match
    assert "numbered_text" in match
    assert "line_count" in match
    assert match["line_count"] > 0
    assert match["content_hash"]
    assert "1 |" in match["numbered_text"]

    # 3. Édition directe sans read_block
    # Extraire hash et adresse du bloc
    block_id = match["displayed_id"]
    content_hash = match["content_hash"]

    # Formuler un edit_block direct
    edit_res = json.loads(
        (
            await server.mcp.call_tool(
                "edit_block",
                {
                    "path": block_id,
                    "expected_hash": content_hash,
                    "ops": [
                        {
                            "replace": [3, 3],
                            "content": "Python 3.11 ou superieur est requis.",
                        }
                    ],
                    "format": "json",
                },
            )
        ).content[0].text
    )

    assert edit_res["status"] == "success"
    assert edit_res["content_hash"] != content_hash
    # Vérifier que le fichier sur disque contient la modification
    updated_file = (ws / "docs/guide.md").read_text(encoding="utf-8")
    assert "Python 3.11 ou superieur est requis." in updated_file