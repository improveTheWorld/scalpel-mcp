"""Tests for persisted index state: index once, skip unchanged files on restart."""

from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer


def test_index_is_persisted_across_restart(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (ws / "docs" / "spec.md").write_text("# Title\n\nBody.\n", encoding="utf-8")

    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)

    assert Indexer(config, db).index_workspace() == 2

    # Simulate a server restart: brand new indexer, same database.
    restarted = Indexer(config, db)
    assert restarted.index_workspace() == 0
    assert db.get_file_hash("src/a.py") is not None

    # Only the modified file is re-parsed.
    (ws / "src" / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    assert restarted.index_workspace() == 1

def test_a_document_schema_change_reparses_documents_only(tmp_path: Path) -> None:
    """Section identifiers come from headings, so they can change with no file change.

    A scheme change must not cost a re-parse of the source files: the document-only
    version drops the hashes of the documents, and re-parsing a document replaces
    all of its sections, its search entries and its embeddings.
    """
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (ws / "docs" / "spec.md").write_text("# Title\n\n## A\n\nBody.\n", encoding="utf-8")

    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)

    assert Indexer(config, db).index_workspace() == 2
    assert Indexer(config, db).index_workspace() == 0

    # An index built before the identifier scheme changed.
    db.set_meta("document_schema_version", "1")

    restarted = Indexer(config, db)
    assert restarted.index_workspace() == 1  # the document, not the source file
    assert restarted.index_workspace() == 0


    # A deleted file is pruned from the persisted index.
    (ws / "docs" / "spec.md").unlink()
    restarted.index_workspace()
    assert db.get_file_hash("docs/spec.md") is None

    # force=True re-parses everything that remains.
    assert restarted.index_workspace(force=True) == 1

def test_index_schema_version_forces_a_full_reparse(tmp_path: Path) -> None:
    """A stored index built under a different schema must be rebuilt.

    The hash-only incremental pass cannot know that *what* should be indexed
    changed, so it silently kept a partial index (Dossiers built on 11 files
    instead of 284). A schema mismatch must force a full re-parse.
    """
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "spec.md").write_text("# Title\n\nBody.\n", encoding="utf-8")
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    indexer = Indexer(config, db)

    assert indexer.index_workspace() == 1
    assert db.get_meta("index_schema_version") == str(Indexer.INDEX_SCHEMA_VERSION)
    # Same schema, unchanged content: nothing to re-parse.
    assert indexer.index_workspace() == 0

    # Simulate an index built by an older schema.
    db.set_meta("index_schema_version", "1")
    assert indexer.index_workspace() == 1
    assert db.get_meta("index_schema_version") == str(Indexer.INDEX_SCHEMA_VERSION)


def test_ignore_pattern_is_retroactive_on_the_next_pass(tmp_path: Path) -> None:
    """Adding an ignore pattern drops the file already in the index, no --reindex.

    The walk stops seeing it, but the file is still on disk. The prune used to fire
    only for files that had disappeared, so a newly ignored file -- a build
    directory, or a secret filename -- stayed in the index, and therefore in the
    Dossier, until a full re-index.
    """
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (ws / "scratch_notes.md").write_text("# Scratch\n\nNotes.\n", encoding="utf-8")

    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    assert Indexer(config, db).index_workspace() == 2
    assert "scratch_notes.md" in db.get_indexed_paths()

    # The pattern arrives afterwards: the file is ignored, but still present.
    config.ignore_patterns = list(config.ignore_patterns) + ["scratch_*"]
    Indexer(config, db).index_workspace()

    assert "scratch_notes.md" not in db.get_indexed_paths()
    assert "src/a.py" in db.get_indexed_paths()
    assert db.get_file_hash("scratch_notes.md") is None
    assert db.get_document("scratch_notes.md") is None


def test_default_ignore_list_covers_secret_material(tmp_path: Path) -> None:
    """Secret filenames are ignored out of the box, not only when a repo says so."""
    ws = tmp_path / "ws"
    ws.mkdir()
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    indexer = Indexer(config, DatabaseManager(config.db_path))

    for name in (
        ".env",
        ".env.local",
        ".secrets.md",
        "secret.md",
        "server.pem",
        "server.key",
        "bundle.pfx",
        "id_rsa",
        "credentials.yml",
        "credentials.prod.json",
    ):
        assert indexer.should_ignore(ws / name), name
    # A normal source file is untouched.
    assert not indexer.should_ignore(ws / "src" / "a.py")
