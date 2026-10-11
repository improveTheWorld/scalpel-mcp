"""Local embedding providers for the vector relevance backend.

Kept optional and lazy: importing this module never loads a model, and a missing
or unavailable model degrades gracefully to BM25 (load_embedder returns None).
"""

from typing import Any, Callable, Dict, List, Optional, Sequence

Embedder = Callable[[str], Optional[Sequence[float]]]

# Short names -> Hugging Face model ids. minilm ships in the local HF cache on
# many machines; multilingual is worth the download for mixed FR/EN repositories.
MODELS: Dict[str, str] = {
    "minilm": "all-MiniLM-L6-v2",
    "multilingual": "intfloat/multilingual-e5-small",
}


class SentenceTransformerEmbedder:
    """Wrap a sentence-transformers model as a text -> vector callable."""

    def __init__(self, model: Any, prefix: str = "") -> None:
        self.model = model
        self.prefix = prefix

    def __call__(self, text: str) -> Optional[List[float]]:
        if not text:
            return None
        return self.model.encode(self.prefix + text, normalize_embeddings=True).tolist()

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        prepared = [self.prefix + text for text in texts]
        return self.model.encode(prepared, normalize_embeddings=True, batch_size=64).tolist()


def load_embedder(
    name: Optional[str] = None,
    cache_dir: Optional[str] = None,
    local_files_only: bool = True,
) -> Optional[SentenceTransformerEmbedder]:
    """Return an embedder, or None when disabled or unavailable.

    name: 'none', 'minilm', 'multilingual', or any Hugging Face model id.
    local_files_only keeps the process offline (no cache writes) by default.
    """
    if not name or name == "none":
        return None
    if name not in MODELS and "/" not in name:
        return None
    model_id = MODELS.get(name, name)
    try:
        from sentence_transformers import SentenceTransformer
    except Exception:
        return None
    try:
        model = SentenceTransformer(
            model_id,
            cache_folder=cache_dir,
            device="cpu",
            local_files_only=local_files_only,
        )
    except Exception:
        return None
    # E5 models are trained with "query:"/"passage:" prefixes; kept empty here so
    # the same callable works for both indexing and querying.
    return SentenceTransformerEmbedder(model, prefix="")
