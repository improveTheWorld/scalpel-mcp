"""A delete event for a file that is back on disk is a REPLACE, not a deletion.

Measured: after one edit_block on README.md, the root README.md vanished from the index
(392 files instead of 393; read_block then answered 'not indexed ... extension is neither'
and the next edit on it failed). A writer that replaces a file by rename emits `deleted`
for the path while the new file is already in place; on_deleted removed it from the index
and nothing put it back until the next server start.
"""

from pathlib import Path

from watchdog.events import FileDeletedEvent

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.indexer import Indexer
from ast_graph_server.watcher import WorkspaceEventHandler


def _handler(tmp_path: Path):
    config = ServerConfig(workspace_root=tmp_path, db_path=tmp_path / "graph.db")
    db = DatabaseManager(config.db_path, workspace_root=tmp_path)
    indexer = Indexer(config, db)
    return WorkspaceEventHandler(config, indexer), indexer, db


def test_delete_event_for_a_file_that_still_exists_keeps_it_indexed(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\nbody\n", encoding="utf-8")
    handler, indexer, db = _handler(tmp_path)
    indexer.index_file(doc)
    assert "README.md" in db.get_file_hashes()

    handler.on_deleted(FileDeletedEvent(str(doc)))  # file is still there: a replace

    assert "README.md" in db.get_file_hashes()


def test_delete_event_for_a_file_really_gone_removes_it(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\nbody\n", encoding="utf-8")
    handler, indexer, db = _handler(tmp_path)
    indexer.index_file(doc)
    doc.unlink()

    handler.on_deleted(FileDeletedEvent(str(doc)))

    assert "README.md" not in db.get_file_hashes()
