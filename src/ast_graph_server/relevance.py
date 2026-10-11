"""Relevance scoring for the context projection.

Deliberately separate from indexing: the indexer fills the ontology and the
search_index table, while these backends only *rank* elements. The default BM25
backend works fully offline; the vector backend activates as soon as an embedder
is provided, and a hybrid backend fuses both with Reciprocal Rank Fusion.
"""

import math
import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Sequence


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR query of quoted tokens."""
    tokens = re.findall(r"[A-Za-z0-9_]{2,}", text or "")
    unique: List[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered not in unique:
            unique.append(lowered)
    return " OR ".join('"' + token + '"' for token in unique[:64])


class RelevanceBackend(ABC):
    """Ranks ontology elements for a natural-language query."""

    @abstractmethod
    def search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Return [{kind, ref_id, file_path, score}] best-first."""


class BM25Backend(RelevanceBackend):
    """SQLite FTS5 BM25 ranking (offline, no model required)."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        expression = fts_query(query)
        if not expression:
            return []
        return self.db.search_index(expression, limit=limit)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, 0.0 when either vector is empty or null."""
    if not a or not b:
        return 0.0
    length = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(length))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorBackend(RelevanceBackend):
    """Cosine similarity over stored embeddings.

    Returns [] unless an embedder is supplied, so the system degrades gracefully
    to BM25. Plug a local model (fastembed/ONNX) or an API call here.
    """

    def __init__(self, db: Any, embedder: Optional[Callable[[str], Optional[Sequence[float]]]] = None) -> None:
        self.db = db
        self.embedder = embedder

    def search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        if self.embedder is None or not query.strip():
            return []
        query_vector = self.embedder(query)
        if not query_vector:
            return []
        scored: List[tuple] = []
        for ref_id, vector in self.db.all_embeddings():
            scored.append((ref_id, cosine(query_vector, vector)))
        scored.sort(key=lambda item: item[1], reverse=True)
        results: List[Dict[str, Any]] = []
        for ref_id, score in scored[:limit]:
            meta = self.db.get_search_meta(ref_id) or {}
            results.append(
                {
                    "kind": meta.get("kind", ""),
                    "ref_id": ref_id,
                    "file_path": meta.get("file_path", ""),
                    "score": score,
                }
            )
        return results


class HybridBackend(RelevanceBackend):
    """Reciprocal Rank Fusion of several backends (for example BM25 + vectors)."""

    def __init__(self, backends: Sequence[RelevanceBackend], k: int = 60) -> None:
        self.backends = list(backends)
        self.k = k

    def search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        fused: Dict[str, float] = {}
        meta: Dict[str, Dict[str, Any]] = {}
        for backend in self.backends:
            for rank, item in enumerate(backend.search(query, limit=limit), start=1):
                ref_id = item["ref_id"]
                fused[ref_id] = fused.get(ref_id, 0.0) + 1.0 / (self.k + rank)
                meta.setdefault(ref_id, item)
        ordered = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)[:limit]
        results: List[Dict[str, Any]] = []
        for ref_id, score in ordered:
            item = dict(meta[ref_id])
            item["score"] = score
            results.append(item)
        return results


def build_backend(db: Any, embedder: Optional[Callable[[str], Optional[Sequence[float]]]] = None) -> RelevanceBackend:
    """BM25 only, or BM25 + vectors fused with RRF when an embedder is available."""
    bm25 = BM25Backend(db)
    if embedder is None:
        return bm25
    return HybridBackend([bm25, VectorBackend(db, embedder)])
