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
# TDD Lot 3 : Branches Internes Légères & Navigation Temporelle (Time Travel)
#
# Contrat de test pour le Lot 3 :
# B1 : create_branch(name) crée une branche pointant vers le HEAD actuel
# B2 : list_branches() liste les branches avec leur head_commit_id et 'active'
# B3 : Un nouveau commit avance la branche active sans impacter les autres
# B4 : switch_branch(name) permute les fichiers disque et réindexe l'AST
# B5 : Refus de switch_branch si modifications non commitées sur le disque (dirty)
# B6 : checkout_commit(commit_id) replace l'espace de travail à un commit passé
# B7 : delete_branch(name) supprime une branche (refuse la branche active)
# ==============================================================================


@pytest.fixture
def branch_env(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    f1 = workspace / "main_file.py"
    f1.write_text("val = 1\n", encoding="utf-8")

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
    }


def test_b1_create_branch(branch_env):
    """B1: create_branch crée un pointeur nommé vers le HEAD courant."""
    patcher: Patcher = branch_env["patcher"]
    history: HistoryManager = branch_env["history"]

    # Création d'un premier commit sur la branche par défaut ('main')
    h = patcher.read_block("main_file.py")["content_hash"]
    patcher.edit_block("main_file.py", ops=[{"replace": [1, 1], "content": "val = 2\n"}], expected_hash=h)
    initial_head = history.get_head_commit_id()

    if not hasattr(history, "create_branch"):
        pytest.fail("HistoryManager.create_branch n'est pas encore implémenté (TDD RED)")

    res = history.create_branch("feature-x")
    assert res["success"] is True
    assert res["branch"] == "feature-x"
    assert res["head_commit_id"] == initial_head


def test_b2_list_branches(branch_env):
    """B2: list_branches liste les branches avec leur tête et marque la branche active."""
    history: HistoryManager = branch_env["history"]

    if not hasattr(history, "list_branches"):
        pytest.fail("HistoryManager.list_branches n'est pas encore implémenté (TDD RED)")

    branches = history.list_branches()
    assert len(branches) >= 1
    names = {b["name"] for b in branches}
    assert "main" in names

    active = [b for b in branches if b.get("active") is True]
    assert len(active) == 1
    assert active[0]["name"] == "main"


def test_b3_branch_commit_isolation(branch_env):
    """B3: Un commit sur la branche active fait avancer sa tête sans toucher aux autres."""
    patcher: Patcher = branch_env["patcher"]
    history: HistoryManager = branch_env["history"]

    h = patcher.read_block("main_file.py")["content_hash"]
    patcher.edit_block("main_file.py", ops=[{"replace": [1, 1], "content": "val = 10\n"}], expected_hash=h)
    c1_id = history.get_head_commit_id()

    if not hasattr(history, "create_branch") or not hasattr(history, "switch_branch"):
        pytest.fail("create_branch ou switch_branch non implémenté (TDD RED)")

    # Création et bascule sur 'dev'
    history.create_branch("dev")
    history.switch_branch("dev")

    # Nouveau commit sur 'dev'
    h2 = patcher.read_block("main_file.py")["content_hash"]
    patcher.edit_block("main_file.py", ops=[{"replace": [1, 1], "content": "val = 20\n"}], expected_hash=h2)
    dev_head = history.get_head_commit_id()
    assert dev_head != c1_id

    branches = {b["name"]: b for b in history.list_branches()}
    assert branches["main"]["head_commit_id"] == c1_id
    assert branches["dev"]["head_commit_id"] == dev_head


def test_b4_switch_branch_disk_swap(branch_env):
    """B4: switch_branch permute les fichiers physiques et met à jour l'index AST."""
    patcher: Patcher = branch_env["patcher"]
    history: HistoryManager = branch_env["history"]
    workspace: Path = branch_env["workspace"]
    f1: Path = branch_env["f1"]

    # État main: val = 100
    h1 = patcher.read_block("main_file.py")["content_hash"]
    patcher.edit_block("main_file.py", ops=[{"replace": [1, 1], "content": "val = 100\n"}], expected_hash=h1)

    if not hasattr(history, "switch_branch"):
        pytest.fail("switch_branch non implémenté (TDD RED)")

    # Branche 'experiment'
    history.create_branch("experiment")
    history.switch_branch("experiment")

    # Sur experiment: création d'un nouveau fichier et modif de f1
    patcher.insert_block(parent=".", text="exp_helper.py", content="def exp(): return True\n")
    h2 = patcher.read_block("main_file.py")["content_hash"]
    patcher.edit_block("main_file.py", ops=[{"replace": [1, 1], "content": "val = 999\n"}], expected_hash=h2)

    helper_file = workspace / "exp_helper.py"
    assert helper_file.exists()
    assert "val = 999" in f1.read_text(encoding="utf-8")

    # Re-bascule sur 'main'
    res = history.switch_branch("main")
    assert res["success"] is True

    # f1 doit être revenu à 100 et exp_helper.py ne doit plus exister sur main
    assert "val = 100" in f1.read_text(encoding="utf-8")
    assert not helper_file.exists()


def test_b5_refuse_switch_if_dirty_disk(branch_env):
    """B5: Refus de switch_branch si le disque contient des modifications non commitées."""
    patcher: Patcher = branch_env["patcher"]
    history: HistoryManager = branch_env["history"]
    f1: Path = branch_env["f1"]

    h = patcher.read_block("main_file.py")["content_hash"]
    patcher.edit_block("main_file.py", ops=[{"replace": [1, 1], "content": "val = 42\n"}], expected_hash=h)

    if not hasattr(history, "switch_branch"):
        pytest.fail("switch_branch non implémenté (TDD RED)")

    history.create_branch("backup")

    # Modification externe pirate sur le disque
    f1.write_text("pirate dirty edit\n", encoding="utf-8")

    # Tentative de switch vers backup -> doit être refusée
    res = history.switch_branch("backup")
    assert res["success"] is False
    assert "dirty" in res["error"].lower() or "modified" in res["error"].lower()
    # Le fichier pirate ne doit pas avoir été écrasé
    assert "pirate dirty edit" in f1.read_text(encoding="utf-8")


def test_b6_checkout_commit_time_travel(branch_env):
    """B6: checkout_commit replace l'espace de travail à un commit passé quelconque."""
    patcher: Patcher = branch_env["patcher"]
    history: HistoryManager = branch_env["history"]
    f1: Path = branch_env["f1"]

    # C1: val = 1
    h1 = patcher.read_block("main_file.py")["content_hash"]
    patcher.edit_block("main_file.py", ops=[{"replace": [1, 1], "content": "val = 111\n"}], expected_hash=h1)
    c1_id = history.get_head_commit_id()

    # C2: val = 222
    h2 = patcher.read_block("main_file.py")["content_hash"]
    patcher.edit_block("main_file.py", ops=[{"replace": [1, 1], "content": "val = 222\n"}], expected_hash=h2)
    c2_id = history.get_head_commit_id()

    # C3: val = 333
    h3 = patcher.read_block("main_file.py")["content_hash"]
    patcher.edit_block("main_file.py", ops=[{"replace": [1, 1], "content": "val = 333\n"}], expected_hash=h3)
    c3_id = history.get_head_commit_id()

    if not hasattr(history, "checkout_commit"):
        pytest.fail("checkout_commit non implémenté (TDD RED)")

    # Voyage dans le temps vers C1
    res = history.checkout_commit(c1_id)
    assert res["success"] is True
    assert "val = 111" in f1.read_text(encoding="utf-8")
    assert history.get_head_commit_id() == c1_id

    # Voyage dans le temps vers C2
    res2 = history.checkout_commit(c2_id)
    assert res2["success"] is True
    assert "val = 222" in f1.read_text(encoding="utf-8")
    assert history.get_head_commit_id() == c2_id


def test_b7_delete_branch(branch_env):
    """B7: delete_branch supprime une branche mais refuse la branche active."""
    history: HistoryManager = branch_env["history"]

    if not hasattr(history, "delete_branch"):
        pytest.fail("delete_branch non implémenté (TDD RED)")

    history.create_branch("temp-br")
    branches = {b["name"] for b in history.list_branches()}
    assert "temp-br" in branches

    # Suppression de temp-br
    res = history.delete_branch("temp-br")
    assert res["success"] is True
    branches_after = {b["name"] for b in history.list_branches()}
    assert "temp-br" not in branches_after

    # Refus de supprimer la branche active 'main'
    res_main = history.delete_branch("main")
    assert res_main["success"] is False
    assert "active" in res_main["error"].lower()