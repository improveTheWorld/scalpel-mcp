import hashlib
from pathlib import Path
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher
from ast_graph_server.history import HistoryManager
from ast_graph_server.watcher import SuppressionRegistry


# ==============================================================================
# TDD Lot 2 : Revert Ciblé d'un Commit Historique (Hors Sommet)
#
# Contrat de test pour le Lot 2 :
# R1 : Revert commit non-sommet sur fichier indépendant
# R2 : Revert commit non-sommet sur même fichier (lignes disjointes / clean 3-way merge)
# R3 : Refus propre de revert si conflit direct sur les mêmes lignes
# R4 : Revert d'une création de fichier historique (suppression si intact)
# R5 : Refus de revert de création si le fichier a été modifié par la suite
# R6 : Revert d'une suppression de fichier historique (recréation depuis blob)
# R7 : Traçabilité : revert_commit crée un commit 'revert' au sommet de HEAD
# ==============================================================================


@pytest.fixture
def revert_env(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    f1 = workspace / "module_a.py"
    f1.write_text("def a():\n    return 1\n", encoding="utf-8")

    f2 = workspace / "module_b.py"
    f2.write_text("def b():\n    return 10\n", encoding="utf-8")

    db_path = tmp_path / ".agent_graph.db"
    db = DatabaseManager(db_path, workspace_root=workspace)
    config = ServerConfig(workspace_root=workspace, db_path=db_path)
    suppression = SuppressionRegistry()
    indexer = Indexer(config, db)
    indexer.index_workspace()

    history_dir = workspace / ".scalpel" / "history"
    history = HistoryManager(
        workspace_root=workspace,
        history_dir=history_dir,
        suppression_registry=suppression,
        indexer=indexer,
    )

    patcher = Patcher(
        config=config,
        db=db,
        indexer=indexer,
        suppression_registry=suppression,
        history_manager=history,
    )

    return {
        "workspace": workspace,
        "history": history,
        "patcher": patcher,
        "indexer": indexer,
        "db": db,
        "f1": f1,
        "f2": f2,
    }


def test_r1_revert_independent_file(revert_env):
    """R1: Revert d'un commit non-sommet touchant un fichier indépendant."""
    patcher: Patcher = revert_env["patcher"]
    history: HistoryManager = revert_env["history"]
    f1: Path = revert_env["f1"]
    f2: Path = revert_env["f2"]

    # C1: modifie f1
    h1 = patcher.read_block("module_a.py::a")["content_hash"]
    patcher.edit_block("module_a.py::a", ops=[{"replace": [2, 2], "content": "    return 2"}], expected_hash=h1)
    c1_id = history.get_head_commit_id()

    # C2: modifie f2
    h2 = patcher.read_block("module_b.py::b")["content_hash"]
    patcher.edit_block("module_b.py::b", ops=[{"replace": [2, 2], "content": "    return 20"}], expected_hash=h2)
    c2_id = history.get_head_commit_id()
    assert c2_id != c1_id

    # Revert de C1 (alors que HEAD est sur C2)
    if not hasattr(history, "revert_commit"):
        pytest.fail("HistoryManager.revert_commit n'est pas encore implémenté (TDD RED)")

    res = history.revert_commit(c1_id)
    assert res["success"] is True

    # f1 doit être revenu à 1, f2 doit être resté à 20
    assert "return 1\n" in f1.read_text(encoding="utf-8")
    assert "return 20\n" in f2.read_text(encoding="utf-8")


def test_r2_revert_same_file_disjoint_lines(revert_env):
    """R2: Revert non-sommet sur le même fichier avec des lignes disjointes (clean 3-way merge)."""
    patcher: Patcher = revert_env["patcher"]
    history: HistoryManager = revert_env["history"]
    f1: Path = revert_env["f1"]

    # État initial : 2 fonctions dans module_a.py
    f1.write_text("def a():\n    return 'init_a'\n\ndef other():\n    return 'init_other'\n", encoding="utf-8")
    revert_env["indexer"].index_file(f1)

    # C1: modifie a()
    h_a = patcher.read_block("module_a.py::a")["content_hash"]
    patcher.edit_block("module_a.py::a", ops=[{"replace": [2, 2], "content": "    return 'mod_a'"}], expected_hash=h_a)
    c1_id = history.get_head_commit_id()

    # C2: modifie other()
    h_other = patcher.read_block("module_a.py::other")["content_hash"]
    patcher.edit_block("module_a.py::other", ops=[{"replace": [2, 2], "content": "    return 'mod_other'"}], expected_hash=h_other)

    if not hasattr(history, "revert_commit"):
        pytest.fail("HistoryManager.revert_commit n'est pas encore implémenté (TDD RED)")

    # Revert C1 : doit annuler 'mod_a' -> 'init_a' tout en gardant other() à 'mod_other'
    res = history.revert_commit(c1_id)
    assert res["success"] is True

    content = f1.read_text(encoding="utf-8")
    assert "return 'init_a'" in content
    assert "return 'mod_other'" in content


def test_r3_revert_conflict_refusal(revert_env):
    """R3: Refus propre si conflit direct sur les mêmes lignes."""
    patcher: Patcher = revert_env["patcher"]
    history: HistoryManager = revert_env["history"]
    f1: Path = revert_env["f1"]

    # C1: modifie a() vers 'val1'
    h1 = patcher.read_block("module_a.py::a")["content_hash"]
    patcher.edit_block("module_a.py::a", ops=[{"replace": [2, 2], "content": "    return 'val1'"}], expected_hash=h1)
    c1_id = history.get_head_commit_id()

    # C2: re-modifie a() vers 'val2' (sur la même ligne 2)
    h2 = patcher.read_block("module_a.py::a")["content_hash"]
    patcher.edit_block("module_a.py::a", ops=[{"replace": [2, 2], "content": "    return 'val2'"}], expected_hash=h2)

    if not hasattr(history, "revert_commit"):
        pytest.fail("HistoryManager.revert_commit n'est pas encore implémenté (TDD RED)")

    # Revert de C1 : la base est 'val1', l'inverse veut '1', mais le disque est 'val2' -> conflit !
    res = history.revert_commit(c1_id)
    assert res["success"] is False
    assert "conflict" in res["error"].lower()
    # Le fichier ne doit pas avoir été corrompu ou modifié
    assert "return 'val2'" in f1.read_text(encoding="utf-8")


def test_r4_revert_file_creation(revert_env):
    """R4: Revert d'une création de fichier historique intacte."""
    patcher: Patcher = revert_env["patcher"]
    history: HistoryManager = revert_env["history"]
    workspace: Path = revert_env["workspace"]

    # C1: création d'un fichier nouveau
    patcher.insert_block(parent=".", text="created_mod.py", content="z = 99\n")
    c1_id = history.get_head_commit_id()
    created_file = workspace / "created_mod.py"
    assert created_file.exists()

    # C2: modification d'un autre fichier
    h = patcher.read_block("module_a.py::a")["content_hash"]
    patcher.edit_block("module_a.py::a", ops=[{"replace": [2, 2], "content": "    return 999"}], expected_hash=h)

    if not hasattr(history, "revert_commit"):
        pytest.fail("HistoryManager.revert_commit n'est pas encore implémenté (TDD RED)")

    # Revert de C1 (la création) -> doit supprimer created_mod.py
    res = history.revert_commit(c1_id)
    assert res["success"] is True
    assert not created_file.exists()


def test_r5_refuse_revert_creation_if_modified(revert_env):
    """R5: Refus de revert de création si le fichier a été modifié ultérieurement."""
    patcher: Patcher = revert_env["patcher"]
    history: HistoryManager = revert_env["history"]
    workspace: Path = revert_env["workspace"]

    # C1: création
    patcher.insert_block(parent=".", text="created_mod2.py", content="def orig(): pass\n")
    c1_id = history.get_head_commit_id()
    created_file = workspace / "created_mod2.py"

    # C2: modification du fichier créé
    h = patcher.read_block("created_mod2.py::orig")["content_hash"]
    patcher.edit_block("created_mod2.py::orig", ops=[{"replace": [1, 1], "content": "def orig(): return 1\n"}], expected_hash=h)

    if not hasattr(history, "revert_commit"):
        pytest.fail("HistoryManager.revert_commit n'est pas encore implémenté (TDD RED)")

    # Revert C1 doit être refusé car le fichier a été modifié depuis sa création
    res = history.revert_commit(c1_id)
    assert res["success"] is False
    assert created_file.exists()


def test_r6_revert_file_deletion(revert_env):
    """R6: Revert d'une suppression de fichier historique -> recréation depuis blob."""
    patcher: Patcher = revert_env["patcher"]
    history: HistoryManager = revert_env["history"]
    f1: Path = revert_env["f1"]
    content_orig = f1.read_bytes()
    sha_orig = hashlib.sha256(content_orig).hexdigest()
    history.save_blob(content_orig)

    # C1: suppression de f1 enregistrée
    import uuid
    c1_id = str(uuid.uuid4())
    history.record_pending_commit(
        commit_id=c1_id,
        parent_id=history.get_head_commit_id(),
        kind="scalpel_delete",
        tool_name="delete_file",
        files=[{"file_path": "module_a.py", "before_sha": sha_orig, "after_sha": None}],
    )
    f1.unlink()
    history.commit_transaction(c1_id)
    assert not f1.exists()

    # C2: modification de f2
    h2 = patcher.read_block("module_b.py::b")["content_hash"]
    patcher.edit_block("module_b.py::b", ops=[{"replace": [2, 2], "content": "    return 55"}], expected_hash=h2)

    if not hasattr(history, "revert_commit"):
        pytest.fail("HistoryManager.revert_commit n'est pas encore implémenté (TDD RED)")

    # Revert C1 -> doit recréer module_a.py avec son contenu d'origine
    res = history.revert_commit(c1_id)
    assert res["success"] is True
    assert f1.exists()
    assert f1.read_bytes() == content_orig


def test_r7_revert_records_commit_at_head(revert_env):
    """R7: Traçabilité : revert_commit crée un commit 'revert' au sommet de HEAD."""
    patcher: Patcher = revert_env["patcher"]
    history: HistoryManager = revert_env["history"]

    h = patcher.read_block("module_a.py::a")["content_hash"]
    patcher.edit_block("module_a.py::a", ops=[{"replace": [2, 2], "content": "    return 777"}], expected_hash=h)
    c1_id = history.get_head_commit_id()

    h2 = patcher.read_block("module_b.py::b")["content_hash"]
    patcher.edit_block("module_b.py::b", ops=[{"replace": [2, 2], "content": "    return 888"}], expected_hash=h2)
    c2_id = history.get_head_commit_id()

    if not hasattr(history, "revert_commit"):
        pytest.fail("HistoryManager.revert_commit n'est pas encore implémenté (TDD RED)")

    res = history.revert_commit(c1_id)
    assert res["success"] is True

    head_id = history.get_head_commit_id()
    assert head_id not in (c1_id, c2_id)
    commit = history.get_commit(head_id)
    assert commit["kind"] == "revert"
    assert commit["status"] == "committed"
    assert commit["parent_id"] == c2_id