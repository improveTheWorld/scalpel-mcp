"""Les deux faces du contrat, mot pour mot — le controle que le 01/10/2026 a rendu permanent.

Quand le mode d'emploi d'edit_block a change (le verrou par fichier et la traduction des numeros
remplacent la « reparation » de lot), le paragraphe a ete recopie a la main dans DEUX fichiers :
la description de l'outil MCP (src/ast_graph_server/server.py) et le schema que le modele lit
(benchmarks/toolsets.py). Le controle de ce jour-la vivait dans un script jetable, hors du depot.

Ce test le fixe. Il compare LE PARAGRAPHE, pas les descriptions entieres : les deux faces ont des
phrases que l'autre n'a pas (le serveur explique la syntaxe de `ops` et la relecture recommandee),
et exiger l'egalite totale ferait echouer un etat que personne n'a mis en cause.
"""

from pathlib import Path

from ast_graph_server.toolset_schemas import SCALPEL_SCHEMAS

from .test_contract_parity import _both_sides

#: Le paragraphe est encadre par ces deux phrases, presentes des deux cotes et non modifiees.
START = "In a batch, every entry is expressed against the version you READ"
END = "In BOTH cases the stamp you sent is NOT reusable"


def _paragraph(text: str) -> str:
    """Le paragraphe publie sur le lot, extrait par ses propres bornes."""
    began = text.index(START)
    ended = text.index(END, began) + len(END)
    return text[began:ended]


def test_the_batch_paragraph_is_one_text_on_both_faces(tmp_path: Path) -> None:
    """Le meme texte, caractere pour caractere — la regle qui a le plus coute a ce depot."""
    server_side, harness_side = _both_sides(tmp_path)
    harness = str(harness_side["edit_block"].get("description") or "")
    server = str(server_side["edit_block"].description or "")
    assert START in harness and START in server, "le paragraphe a disparu d'une des deux faces"
    assert END in harness and END in server, "la fin du paragraphe a disparu d'une des deux faces"
    assert _paragraph(harness) == _paragraph(server), (
        "les deux faces ne portent plus le meme paragraphe de lot",
        len(_paragraph(harness)),
        len(_paragraph(server)),
    )


def test_the_removed_repair_is_not_published_any_more(tmp_path: Path) -> None:
    """Le mode d'emploi ne decrit plus un mecanisme qui n'existe plus dans le code."""
    server_side, harness_side = _both_sides(tmp_path)
    for face, text in (
        ("harnais", str(harness_side["edit_block"].get("description") or "")),
        ("serveur", str(server_side["edit_block"].description or "")),
    ):
        for ghost in (
            "not automatically stale",
            "expected_hash_refreshed",
            "says expected_hash_refreshed",
            "POSITIONAL and needs both halves",
        ):
            assert ghost not in text, (face, ghost)
