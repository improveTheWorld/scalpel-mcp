"""Tests for the optional local embedding pass and the vector backend."""

from pathlib import Path

from ast_graph_server.config import ServerConfig
from ast_graph_server.database import DatabaseManager
from ast_graph_server.embedding import load_embedder
from ast_graph_server.indexer import Indexer
from ast_graph_server.relevance import BM25Backend, build_backend


class FakeEmbedder:
    """Deterministic 2-dim embedder: dimension 0 = oauth, dimension 1 = gardening."""

    def _vector(self, text):
        low = text.lower()
        return [1.0 if "oauth" in low else 0.0, 1.0 if ("garden" in low or "plant" in low) else 0.0]

    def __call__(self, text):
        return self._vector(text)

    def embed_batch(self, texts):
        return [self._vector(text) for text in texts]


def make_env(tmp_path: Path, embedder=None):
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "guide.md").write_text(
        "# Guide\n\n## OAuth setup\n\nConfigure the client secret.\n\n## Gardening\n\nWater the plants.\n",
        encoding="utf-8",
    )
    config = ServerConfig(workspace_root=ws, db_path=ws / ".agent_graph.db")
    db = DatabaseManager(config.db_path)
    Indexer(config, db, embedder=embedder).index_workspace()
    return ws, config, db


def test_load_embedder_disabled_or_unknown() -> None:
    assert load_embedder(None) is None
    assert load_embedder("none") is None
    assert load_embedder("not-a-real-model") is None


def test_indexer_stores_embeddings_when_configured(tmp_path: Path) -> None:
    ws, config, db = make_env(tmp_path, embedder=FakeEmbedder())
    stored = db.all_embeddings()
    assert stored
    refs = {ref for ref, _ in stored}
    assert "docs/guide.md#oauth-setup" in refs


def test_no_embeddings_without_embedder(tmp_path: Path) -> None:
    ws, config, db = make_env(tmp_path)
    assert db.all_embeddings() == []


