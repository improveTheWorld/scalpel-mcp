import hashlib
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.patcher import Patcher
from ast_graph_server.watcher import SuppressionRegistry


# ==============================================================================
# TDD Lot 1 : Système d'Historique Delta Atomique pour Scalpel
#
# Ces tests définissent le contrat du Lot 1 selon la Spécification Technique :
# T1 : Nominal edit_block -> blobs before/after + commit 'committed'
# T2 : Déduplication des blobs dans .scalpel/history/blobs/
# T3 : Nominal insert_block (création fichier) -> before_sha=None, after_sha=sha
# T4 : Rollback création de fichier -> suppression physique + remove de l'index
# T5 : Nominal batch multi-fichiers -> 1 commit unique, N lignes dans commit_files
# T6 : Rollback batch multi-fichiers -> restauration atomique de tous les fichiers
# T7 : Crash recovery avant replace -> pending abort
# T8 : Crash recovery après replace -> pending promotion
# T9 : Refus rollback sur divergence disque
# T10: Détection écriture externe (kind='foreign')
# ==============================================================================

# Note : l'import du futur module d'historique échouera initialement tant qu'il n'est pas créé.
try:
    from ast_graph_server.history import HistoryManager
except ImportError:
    HistoryManager = None


@pytest.fixture
def history_env(tmp_path: Path):
    """Prépare un environnement complet Scalpel avec HistoryManager isolé."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Création d'un fichier source de test
    src_file = workspace / "sample.py"
    src_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8", newline="\n")

    db_path = tmp_path / ".agent_graph.db"
    db = DatabaseManager(db_path, workspace_root=workspace)
    config = ServerConfig(workspace_root=workspace, db_path=db_path)
    suppression = SuppressionRegistry()
    indexer = Indexer(config, db)
    indexer.index_workspace()

    if HistoryManager is None:
        pytest.fail("HistoryManager n'est pas encore implémenté (ImportError attendu en TDD)")

    history_dir = workspace / ".scalpel" / "history"
    history = HistoryManager(workspace_root=workspace, history_dir=history_dir, suppression_registry=suppression, indexer=indexer)

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
        "src_file": src_file,
    }


def test_t1_nominal_edit_block(history_env):
    """T1: Nominal edit_block -> blobs before/after + commit 'committed'."""
    patcher: Patcher = history_env["patcher"]
    history: HistoryManager = history_env["history"]
    src_file: Path = history_env["src_file"]

    initial_bytes = src_file.read_bytes()
    initial_sha = hashlib.sha256(initial_bytes).hexdigest()

    # Lecture du bloc pour récupérer le hash Scalpel
    read_res = patcher.read_block("sample.py::hello")
    expected_hash = read_res["content_hash"]

    # Modification atomique via edit_block
    edit_res = patcher.edit_block(
        block_id="sample.py::hello",
        ops=[{"replace": [2, 2], "content": "    return 'scalpel'"}],
        expected_hash=expected_hash,
    )
    assert not edit_res.get("error"), f"Edit failed: {edit_res}"

    new_bytes = src_file.read_bytes()
    new_sha = hashlib.sha256(new_bytes).hexdigest()
    assert new_sha != initial_sha

    # Vérification du commit dans l'historique
    head_id = history.get_head_commit_id()
    assert head_id is not None
    commit = history.get_commit(head_id)
    assert commit["status"] == "committed"
    assert commit["kind"] in ("scalpel_edit", "edit_block")

    # Vérification des commit_files
    files = history.get_commit_files(head_id)
    assert len(files) == 1
    assert files[0]["file_path"] == "sample.py"
    assert files[0]["before_sha"] == initial_sha
    assert files[0]["after_sha"] == new_sha

    # Vérification des blobs physiques
    assert history.has_blob(initial_sha)
    assert history.has_blob(new_sha)
    assert history.read_blob(initial_sha) == initial_bytes
    assert history.read_blob(new_sha) == new_bytes


def test_t2_blob_deduplication(history_env):
    """T2: Déduplication des blobs dans .scalpel/history/blobs/."""
    patcher: Patcher = history_env["patcher"]
    history: HistoryManager = history_env["history"]
    src_file: Path = history_env["src_file"]

    # Deux écritures qui remettent le même contenu
    read_res = patcher.read_block("sample.py::hello")
    h1 = read_res["content_hash"]
    patcher.edit_block("sample.py::hello", ops=[{"replace": [2, 2], "content": "    return 'v2'"}], expected_hash=h1)

    read_res2 = patcher.read_block("sample.py::hello")
    h2 = read_res2["content_hash"]
    # Re-modification vers 'world' (identique au début)
    patcher.edit_block("sample.py::hello", ops=[{"replace": [2, 2], "content": "    return 'world'"}], expected_hash=h2)

    initial_sha = hashlib.sha256(b"def hello():\n    return 'world'\n").hexdigest()
    blob_path = history.blob_path_for(initial_sha)
    assert blob_path.exists()
    # Il ne doit exister qu'un seul fichier blob pour ce sha
    assert history.count_blob_instances(initial_sha) == 1


def test_t3_nominal_insert_block_creation(history_env):
    """T3: Nominal insert_block (création fichier) -> before_sha=None, after_sha=sha."""
    patcher: Patcher = history_env["patcher"]
    history: HistoryManager = history_env["history"]
    workspace: Path = history_env["workspace"]

    res = patcher.insert_block(
        parent=".",
        text="created.py",
        content="x = 42\n",
    )
    assert not res.get("error"), f"Creation failed: {res}"

    created_file = workspace / "created.py"
    assert created_file.exists()
    created_sha = hashlib.sha256(created_file.read_bytes()).hexdigest()

    head_id = history.get_head_commit_id()
    commit = history.get_commit(head_id)
    assert commit["status"] == "committed"

    files = history.get_commit_files(head_id)
    assert len(files) == 1
    assert files[0]["file_path"] == "created.py"
    assert files[0]["before_sha"] is None
    assert files[0]["after_sha"] == created_sha


def test_t4_rollback_file_creation(history_env):
    """T4: Rollback création de fichier -> suppression physique + remove de l'index."""
    patcher: Patcher = history_env["patcher"]
    history: HistoryManager = history_env["history"]
    workspace: Path = history_env["workspace"]
    db: DatabaseManager = history_env["db"]

    patcher.insert_block(parent=".", text="temp_mod.py", content="def foo(): pass\n")
    temp_file = workspace / "temp_mod.py"
    assert temp_file.exists()

    # Rollback HEAD
    rollback_res = history.rollback_head()
    assert rollback_res["success"] is True

    # Le fichier doit être supprimé du disque
    assert not temp_file.exists()
    # Et retiré de l'index
    symbols = db.get_symbols_for_file("temp_mod.py")
    assert len(symbols) == 0


def test_t5_nominal_multiline_batch(history_env):
    """T5: Nominal batch multi-fichiers -> 1 commit unique, N lignes dans commit_files."""
    patcher: Patcher = history_env["patcher"]
    history: HistoryManager = history_env["history"]
    workspace: Path = history_env["workspace"]

    f2 = workspace / "file2.py"
    f2.write_text("b = 1\n", encoding="utf-8")
    history_env["indexer"].index_file(f2)

    h1 = patcher.read_block("sample.py::hello")["content_hash"]
    h2 = patcher.read_block("file2.py")["content_hash"]

    batch_res = patcher.edit_block(
        edits=[
            {"path": "sample.py::hello", "ops": [{"replace": [2, 2], "content": "    return 'batch'"}], "expected_hash": h1},
            {"path": "file2.py", "ops": [{"replace": [1, 1], "content": "b = 2\n"}], "expected_hash": h2},
        ]
    )
    assert not batch_res.get("error"), f"Batch failed: {batch_res}"

    head_id = history.get_head_commit_id()
    commit = history.get_commit(head_id)
    assert commit["status"] == "committed"

    files = history.get_commit_files(head_id)
    assert len(files) == 2
    paths = {f["file_path"] for f in files}
    assert paths == {"sample.py", "file2.py"}


def test_t6_rollback_batch(history_env):
    """T6: Rollback batch multi-fichiers -> restauration atomique de tous les fichiers."""
    patcher: Patcher = history_env["patcher"]
    history: HistoryManager = history_env["history"]
    workspace: Path = history_env["workspace"]

    f2 = workspace / "file2.py"
    f2.write_text("b = 100\n", encoding="utf-8")
    history_env["indexer"].index_file(f2)

    s1_orig = (workspace / "sample.py").read_bytes()
    s2_orig = f2.read_bytes()

    h1 = patcher.read_block("sample.py::hello")["content_hash"]
    h2 = patcher.read_block("file2.py")["content_hash"]

    patcher.edit_block(
        edits=[
            {"path": "sample.py::hello", "ops": [{"replace": [2, 2], "content": "    return 'batch_rb'"}], "expected_hash": h1},
            {"path": "file2.py", "ops": [{"replace": [1, 1], "content": "b = 200\n"}], "expected_hash": h2},
        ]
    )

    # Rollback HEAD
    rollback_res = history.rollback_head()
    assert rollback_res["success"] is True

    # Vérification que les deux fichiers sont restaurés à l'octet près
    assert (workspace / "sample.py").read_bytes() == s1_orig
    assert f2.read_bytes() == s2_orig


def test_t7_crash_recovery_before_replace(history_env):
    """T7: Crash recovery avant replace (pending abort)."""
    history: HistoryManager = history_env["history"]
    workspace: Path = history_env["workspace"]
    src_file: Path = history_env["src_file"]

    before_bytes = src_file.read_bytes()
    before_sha = hashlib.sha256(before_bytes).hexdigest()
    fake_after_sha = hashlib.sha256(b"never_written").hexdigest()

    # Création manuelle d'un commit 'pending' simulant un crash avant os.replace
    pending_id = str(uuid.uuid4())
    history.record_pending_commit(
        commit_id=pending_id,
        parent_id=history.get_head_commit_id(),
        kind="scalpel_edit",
        tool_name="edit_block",
        files=[{"file_path": "sample.py", "before_sha": before_sha, "after_sha": fake_after_sha}],
    )

    commit = history.get_commit(pending_id)
    assert commit["status"] == "pending"

    # Exécution de la récupération au démarrage
    recovered = history.recover_pending()
    assert pending_id in recovered["aborted"]

    commit_after = history.get_commit(pending_id)
    assert commit_after["status"] == "aborted"


def test_t8_crash_recovery_after_replace(history_env):
    """T8: Crash recovery après replace (pending promotion)."""
    history: HistoryManager = history_env["history"]
    workspace: Path = history_env["workspace"]
    src_file: Path = history_env["src_file"]

    before_sha = hashlib.sha256(src_file.read_bytes()).hexdigest()
    new_bytes = b"def hello():\n    return 'recovered'\n"
    new_sha = hashlib.sha256(new_bytes).hexdigest()

    # On écrit le blob
    history.save_blob(new_bytes)

    # On simule le commit pending
    pending_id = str(uuid.uuid4())
    history.record_pending_commit(
        commit_id=pending_id,
        parent_id=history.get_head_commit_id(),
        kind="scalpel_edit",
        tool_name="edit_block",
        files=[{"file_path": "sample.py", "before_sha": before_sha, "after_sha": new_sha}],
    )

    # On simule que le crash est intervenu APRÈS l'écriture disque
    src_file.write_bytes(new_bytes)

    recovered = history.recover_pending()
    assert pending_id in recovered["committed"]

    commit_after = history.get_commit(pending_id)
    assert commit_after["status"] == "committed"
    assert history.get_head_commit_id() == pending_id


def test_t9_refuse_rollback_on_disk_divergence(history_env):
    """T9: Refus rollback si modification extérieure entre-temps."""
    patcher: Patcher = history_env["patcher"]
    history: HistoryManager = history_env["history"]
    src_file: Path = history_env["src_file"]

    h = patcher.read_block("sample.py::hello")["content_hash"]
    patcher.edit_block("sample.py::hello", ops=[{"replace": [2, 2], "content": "    return 'diverge'"}], expected_hash=h)

    # Modification externe pirate sur le disque
    src_file.write_text("pirate edit\n", encoding="utf-8")

    # Tentative de rollback HEAD -> doit être refusée
    res = history.rollback_head()
    assert res["success"] is False
    assert "modified since HEAD" in res["error"]
    assert src_file.read_text(encoding="utf-8") == "pirate edit\n"


def test_t10_foreign_mutation_detection(history_env):
    """T10: Détection écriture externe (kind='foreign')."""
    history: HistoryManager = history_env["history"]
    workspace: Path = history_env["workspace"]
    src_file: Path = history_env["src_file"]

    initial_head = history.get_head_commit_id()

    # Mutation externe directe
    external_content = b"def hello():\n    return 'external_world'\n"
    src_file.write_bytes(external_content)

    # Notification / Sync du watcher ou méthode de détection
    detected_commit_id = history.record_external_modification("sample.py")
    assert detected_commit_id is not None

    commit = history.get_commit(detected_commit_id)
    assert commit["kind"] == "foreign"
    assert commit["status"] == "committed"
    assert commit["parent_id"] == initial_head
    assert history.get_head_commit_id() == detected_commit_id

    files = history.get_commit_files(detected_commit_id)
    assert len(files) == 1
    assert files[0]["file_path"] == "sample.py"
    assert files[0]["after_sha"] == hashlib.sha256(external_content).hexdigest()