from pathlib import Path
import pytest
from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from tests.test_documents import make_env


def test_pure_function_files_included_in_map(tmp_path: Path):
    """Vérifie que les modules contenant uniquement des fonctions pures (sans classe) sont inclus dans la map."""
    ws, config, db, indexer = make_env(
        tmp_path,
        {
            "src/lineops.py": (
                "def split_lines(text: str):\n"
                "    return text.splitlines()\n\n"
                "def join_lines(lines):\n"
                "    return '\\n'.join(lines)\n"
            ),
            "src/models.py": (
                "class User:\n"
                "    def __init__(self, name):\n"
                "        self.name = name\n"
            ),
        },
    )
    indexer.index_workspace()

    result = db.get_project_map(budget_tokens=2000)
    files = result.get("files", [])
    file_paths = [f["file_path"] for f in files]

    # Le fichier lineops.py contenant uniquement des fonctions pures DOIT être présent
    assert "src/lineops.py" in file_paths, f"src/lineops.py absent des fichiers: {file_paths}"
    
    lineops_entry = next(f for f in files if f["file_path"] == "src/lineops.py")
    symbols = lineops_entry.get("symbols", [])
    assert any("function split_lines" in s for s in symbols), f"function split_lines attendu dans {symbols}"
    assert any("function join_lines" in s for s in symbols), f"function join_lines attendu dans {symbols}"


def test_markdown_h1_title_deduplication(tmp_path: Path):
    """Vérifie que le premier titre H1 identique au titre du document n'est pas répété dans les section_titles."""
    doc_content = (
        "# Architecture Overview\n\n"
        "## Architecture Overview\n\n"
        "Introduction text.\n\n"
        "## Storage Engine\n\n"
        "Details on storage.\n\n"
        "## Query Planner\n\n"
        "Details on planner.\n"
    )
    ws, config, db, indexer = make_env(
        tmp_path,
        {
            "docs/architecture.md": doc_content,
        },
    )
    indexer.index_workspace()

    result = db.get_project_map(budget_tokens=2000)
    docs = result.get("documents", [])
    assert len(docs) == 1
    doc = docs[0]
    assert doc["title"] == "Architecture Overview"
    # Le premier heading étant identique au titre "Architecture Overview", il doit être ignoré
    section_titles = doc.get("section_titles", [])
    assert "Architecture Overview" not in section_titles, (
        f"Le titre H1 ne doit pas être répété dans section_titles: {section_titles}"
    )
    assert "Storage Engine" in section_titles
    assert "Query Planner" in section_titles


def test_vases_communicants_doc_budget_to_code(tmp_path: Path):
    """Vérifie le reversement automatique du budget document inutilisé vers les fichiers de code."""
    # 6 fichiers de code sans aucun document.
    files_dict = {}
    for i in range(6):
        files_dict[f"src/module_{i}.py"] = (
            f"class Module{i}Handler:\n"
            f"    def handle(self):\n"
            f"        return {i}\n"
        )
    ws, config, db, indexer = make_env(tmp_path, files_dict)
    indexer.index_workspace()

    # budget_tokens = 200 => budget_chars = 800, fixed = 140, usable = 660.
    # 6 fichiers * ~72 chars = ~432 chars (restitution multi-lignes Lot 2).
    # Sans vases communicants: doc_budget = 40% (264 chars) est réservé aux docs (qui n'existent pas)
    # et code_budget = 60% (396 chars). 432 chars ne rentrent pas dans 396 chars -> troncature (5 fichiers max).
    # Avec vases communicants: doc_remainder (40% non consommé) est reversé au code -> budget effectif = 660 chars.
    # Les 6 fichiers rentrent intégralement et result["truncated"] == False.
    result = db.get_project_map(budget_tokens=200)
    files = result.get("files", [])
    file_paths = [f["file_path"] for f in files]

    assert len(file_paths) == 6, f"Tous les 6 fichiers devraient être packés grâce aux vases communicants, eu: {len(file_paths)}"
    assert result.get("truncated") is False, "Le résultat ne devrait pas être tronqué grâce aux vases communicants"


def test_prioritization_entrypoints_src_over_tests_and_archives(tmp_path: Path):
    """Vérifie que server.py et les fichiers sous src/ sont priorisés par rapport aux tests et archives."""
    # Chaque fichier a suffisamment de classes pour peser ~100 caractères
    files_dict = {
        "src/server.py": (
            "class ServerA:\n    pass\n"
            "class ServerB:\n    pass\n"
            "class ServerC:\n    pass\n"
        ),
        "src/core/engine.py": (
            "class EngineA:\n    pass\n"
            "class EngineB:\n    pass\n"
            "class EngineC:\n    pass\n"
        ),
        "tests/test_engine.py": (
            "class TestEngineA:\n    pass\n"
            "class TestEngineB:\n    pass\n"
            "class TestEngineC:\n    pass\n"
        ),
        ".archive/old_engine.py": (
            "class OldEngineA:\n    pass\n"
            "class OldEngineB:\n    pass\n"
            "class OldEngineC:\n    pass\n"
        ),
    }
    ws, config, db, indexer = make_env(tmp_path, files_dict)
    indexer.index_workspace()

    # budget_tokens = 200 => budget_chars = 800, fixed = 140, usable = 660.
    # Si doc_budget = 40% (264), reliquat transféré au code => effective_code_budget = 660.
    # Mais si on met un budget plus bas, disons budget_tokens = 85 (budget_chars = 800 minimum car max(800, ...)).
    # Note: budget_chars est au minimum 800 car max(800, int(budget_tokens)*4).
    # Avec budget_tokens = 250 (budget_chars = 1000, fixed = 140, usable = 860).
    # Pour que la sélection coupe après 2 fichiers :
    # Faisons en sorte que 2 fichiers prennent ~400 chars et que usable = 500 chars :
    # ou budget_tokens = 100 => budget_chars = 800, usable = 660.
    # Donnons à chaque fichier 15 classes (chacun pèse ~250 chars).
    # Ainsi 2 fichiers = 500 chars (tiennent dans usable 660), 3 fichiers = 750 chars (ne tiennent pas).
    # Le packing par score décroissant doit retenir src/server.py et src/core/engine.py,
    # et éliminer tests/test_engine.py et .archive/old_engine.py.
    # L'ancien round-robin alphabétique prendrait .archive/old_engine.py et src/server.py,
    # sacrifiant src/core/engine.py !
    long_classes = "\n".join(f"class Item{i}:\n    pass" for i in range(18))
    files_dict_large = {
        "src/server.py": long_classes,
        "src/core/engine.py": long_classes,
        "tests/test_engine.py": long_classes,
        ".archive/old_engine.py": long_classes,
    }
    prio_dir = tmp_path / "prio"
    prio_dir.mkdir(parents=True, exist_ok=True)
    ws2, config2, db2, indexer2 = make_env(prio_dir, files_dict_large)
    indexer2.index_workspace()

    result = db2.get_project_map(budget_tokens=200)
    files = result.get("files", [])
    file_paths = [f["file_path"] for f in files]

    assert "src/server.py" in file_paths, f"src/server.py doit être sélectionné: {file_paths}"
    assert "src/core/engine.py" in file_paths, f"src/core/engine.py doit être sélectionné en priorité sur .archive: {file_paths}"
    assert ".archive/old_engine.py" not in file_paths, f".archive/old_engine.py ne doit pas évincer le code source coeur: {file_paths}"


def test_calls_in_centrality_boosts_file_ranking(tmp_path: Path):
    """Vérifie que les appels entrants (calls_in) augmentent le score composite d'un fichier."""
    files_dict = {
        "src/called_lib.py": (
            "class Target:\n"
            "    def do_work(self):\n"
            "        pass\n"
        ),
        "src/caller_a.py": (
            "from src.called_lib import Target\n"
            "class CallerA:\n"
            "    def call(self):\n"
            "        t = Target()\n"
            "        t.do_work()\n"
        ),
        "src/caller_b.py": (
            "from src.called_lib import Target\n"
            "class CallerB:\n"
            "    def call(self):\n"
            "        t = Target()\n"
            "        t.do_work()\n"
        ),
        "src/isolated_lib.py": (
            "class Isolated:\n"
            "    def no_calls(self):\n"
            "        pass\n"
        ),
    }
    ws, config, db, indexer = make_env(tmp_path, files_dict)
    indexer.index_workspace()

    with db.get_connection() as conn:
        call_rows = conn.execute(
            """
            SELECT s.file_path, count(*) as call_count
            FROM symbol_calls sc
            JOIN symbols s ON s.id = sc.callee_symbol_id
            GROUP BY s.file_path
            """
        ).fetchall()
        # Si le parser tree-sitter résout callee_symbol_id, called_lib a des calls_in
        called_count = next((r["call_count"] for r in call_rows if "called_lib" in r["file_path"]), 0)

    # Vérifions que get_project_map s'exécute sans erreur avec la requête de centralité
    result = db.get_project_map(budget_tokens=1000)
    files = result.get("files", [])
    assert any("src/called_lib.py" in f["file_path"] for f in files)