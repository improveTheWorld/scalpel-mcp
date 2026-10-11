"""Le contrat a DEUX faces, et elles doivent dire la meme chose.

Erreur recurrente, notee le 30/09/2026 apres l'avoir refaite plusieurs fois : une capacite
ajoutee cote SERVEUR (nom de parametre, description, description d'outil) et oubliee cote AGENT
(le schema de benchmarks/toolsets.py, qui est ce que le modele lit vraiment). Exemples payes :
paths de read_block (la lecture de PLUSIEURS blocs en un appel) n'existait que cote serveur, et
edits (l'edition de plusieurs blocs en un appel) n'existait nulle part ; la description de
read_block.path etait vide cote harnais.

Ces tests comparent les deux surfaces outil par outil : memes outils, memes parametres, et tout
parametre decrit d'un cote doit l'etre de l'autre -- avec les mots-cles de la capacite. Ils
echouent A LA SECONDE ou une des deux faces prend de l'avance, au lieu de couter des tours en
campagne.
"""

import asyncio
from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.server import ASTGraphServer
from ast_graph_server.toolset_schemas import SCALPEL_SCHEMAS


def _both_sides(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "a.md").write_text("# T" + chr(10) + chr(10) + "corps" + chr(10), encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    server = ASTGraphServer(config)
    server.initialize(enable_watcher=False)
    server_side = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    harness_side = {spec["function"]["name"]: spec["function"] for spec in SCALPEL_SCHEMAS}
    return server_side, harness_side


def test_the_two_surfaces_expose_the_same_tools(tmp_path: Path) -> None:
    server_side, harness_side = _both_sides(tmp_path)
    assert set(server_side) == set(harness_side), (sorted(server_side), sorted(harness_side))


def test_every_parameter_the_agent_sees_exists_on_the_server(tmp_path: Path) -> None:
    """L'agent peut voir MOINS de parametres que le serveur (les niveaux cachent les nombres),
    mais aucun parametre fantome : ce que le modele lit doit exister en face."""
    server_side, harness_side = _both_sides(tmp_path)
    for name, tool in server_side.items():
        server_props = set((tool.input_schema or {}).get("properties") or {})
        harness_props = set(harness_side[name]["parameters"].get("properties") or {})
        assert harness_props <= server_props, (name, sorted(harness_props - server_props))


def test_every_described_parameter_is_described_on_both_sides(tmp_path: Path) -> None:
    """Le modele ne voit QUE le schema du harnais : une description qui n'existe que cote
    serveur est une capacite invisible, et une capacite decrite nulle part est un tour perdu."""
    server_side, harness_side = _both_sides(tmp_path)
    gaps = []
    for name, tool in server_side.items():
        server_props = (tool.input_schema or {}).get("properties") or {}
        harness_props = harness_side[name]["parameters"].get("properties") or {}
        for key, value in server_props.items():
            if key not in harness_props:
                continue  # parametre que l'agent ne voit pas : hors contrat agent
            described = (value or {}).get("description")
            if described and not (harness_props.get(key) or {}).get("description"):
                gaps.append(name + "." + key)
        for key in harness_props:
            if not (harness_props.get(key) or {}).get("description"):
                gaps.append(name + "." + key + " (expose a l'agent SANS description)")
    assert gaps == [], "decrit cote serveur, muet cote agent : " + ", ".join(gaps)


def test_the_agent_tool_texts_carry_the_rules_the_server_states(tmp_path: Path) -> None:
    """Les regles qui ont coute des tours doivent etre dans le TEXTE D'OUTIL que le modele lit.

    Mesure : la regle de titre d'edit_block ("a heading that would create a second paragraph at
    the block's level or above is refused") n'existait que cote serveur, et c'est exactement le
    refus d'un run (base f831a4e puis 0489adc). Meme chose pour le lien implementation->edition.
    """
    _server_side, harness_side = _both_sides(tmp_path)
    markers = [
        ("edit_block", "never changes the outline"),
        ("edit_block", "is REFUSED"),
        ("edit_block", "both line numbers"),
        ("edit_block", "written_outside_block"),
        ("edit_block", "BOTTOM-UP"),
        ("edit_block", "WITHOUT re-reading"),
        ("edit_block", "NO whole-block variant"),
        ("edit_block", "ONE call"),
        ("search_symbols", "content_hash"),
        ("read_block", "paths=[...]"),
        ("insert_block", "and its hash"),
    ]
    missing = []
    for tool, needle in markers:
        if needle not in str(harness_side[tool].get("description") or ""):
            missing.append(tool + " doit nommer: " + needle)
    assert missing == [], missing


def test_the_capabilities_we_paid_for_are_named_in_the_agents_schema(tmp_path: Path) -> None:
    """Les mots-cles des capacites qui ont coute des tours doivent etre dans le texte que le
    modele lit, pas seulement dans celui du serveur."""
    _server_side, harness_side = _both_sides(tmp_path)
    markers = [
        ("read_block", "paths", "ONE call"),
        ("read_block", "format", "LISTING"),
        ("edit_block", "edits", "ONE call"),
        ("edit_block", "edits", "ITS OWN path"),
        ("edit_block", "expected_hash", "version stamp"),
        ("edit_block", "expected_hash", "NAMES that block"),
        ("edit_block", "path", "code file"),
    ]
    missing = []
    for tool, parameter, needle in markers:
        described = str((harness_side[tool]["parameters"]["properties"].get(parameter) or {}).get("description") or "")
        if needle not in described:
            missing.append(tool + "." + parameter + " doit nommer " + needle)
    assert missing == [], missing


def test_the_edit_block_faces_say_the_same_rule_about_a_heading(tmp_path: Path) -> None:
    """Les DEUX faces d'edit_block portent les MEMES phrases sur l'insertion d'un titre.

    Ce que le test precedent ne voyait pas : il ne verifie que la face HARNAIS. Le 01/10/2026, le
    routeur du mur du titre a change le comportement (un titre frere de MEME niveau est CREE quand
    l'appel n'ajoute que des lignes), et les deux phrases du serveur disaient encore le contraire --
    << a heading at the block's level or above is refused >> -- pendant que la suite restait verte.
    Un modele qui lit le schema du harnais et un operateur qui lit la description du serveur
    lisaient donc deux contrats differents.

    La comparaison porte sur les PHRASES, pas sur les mots-cles : elle normalise les espaces (le
    serveur decoupe son texte en litteraux concatenes) et exige que chaque phrase de la regle soit
    presente des deux cotes, mot pour mot.
    """
    import re

    server_side, harness_side = _both_sides(tmp_path)
    harness = str(harness_side["edit_block"].get("description") or "")
    server = str(server_side["edit_block"].description or "")
    sentences = [
        "An insert_after that re-emits a line the block already carries is NOT refused (there is no "
        "range to extend): it is written and the copy is reported in warnings.",
        harness[harness.index("edit_block edits INSIDE"):harness.index("To change SEVERAL blocks")].strip(),
        "An insert_after the block's LAST line in a CODE block writes into the FILE (the code "
        "becomes a sibling): the block keeps its own hash, and the reply says written_outside_block "
        "and returns the file listing with the address of what was created -- never send it again.",
    ]
    flat = lambda text: re.sub(r"\s+", " ", text)
    missing = [
        phrase[:70]
        for phrase in sentences
        if flat(phrase) not in flat(harness) or flat(phrase) not in flat(server)
    ]
    assert missing == [], "phrase absente d'une des deux faces : " + " | ".join(missing)
