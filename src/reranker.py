"""Cross-Encoder reranker for RAG retrieval refinement.

After BM25 + dense vector hybrid retrieval returns top-k chunks,
this module re-ranks them using a Cross-Encoder model that scores
(query, chunk) pairs jointly, capturing fine-grained semantic relevance
that dual-encoder embeddings miss.

Usage:
    from reranker import CrossEncoderReranker
    reranker = CrossEncoderReranker(model_name="cross-encoder/ms-marco-MiniLM-L-6-v2")
    ranked = reranker.rerank(query="IL6 rheumatoid arthritis", chunks=candidates, top_n=8)
"""

from __future__ import annotations

from typing import Any
from functools import lru_cache

from config import get_embedding_settings

# Lazy import: sentence-transformers may not be in base env
try:
    from sentence_transformers import CrossEncoder
    _CE_AVAILABLE = True
except ImportError:
    _CE_AVAILABLE = False

_DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class CrossEncoderReranker:
    """Lightweight wrapper around sentence-transformers CrossEncoder."""

    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        if not _CE_AVAILABLE:
            raise ImportError(
                "sentence-transformers is required for CrossEncoder reranking. "
                "Install with: pip install sentence-transformers"
            )
        settings = get_embedding_settings()
        self.model_name = model_name or settings.get("reranker_model", _DEFAULT_MODEL)
        self.device = device or settings.get("device", "cpu")
        self._model: Any = None  # lazy load

    def _load(self) -> Any:
        if self._model is None:
            self._model = CrossEncoder(self.model_name, device=self.device)
        return self._model

    def rerank(
        self,
        query: str,
        chunks: list[dict[str, Any]],
        top_n: int = 8,
        batch_size: int = 8,
    ) -> list[dict[str, Any]]:
        """Re-rank chunks by CrossEncoder relevance score.

        Args:
            query: The gene-disease query text.
            chunks: Candidate chunks from hybrid retrieval.
            top_n: Number of top chunks to return after re-ranking.
            batch_size: Batch size for CrossEncoder inference.

        Returns:
            Chunks sorted by descending CrossEncoder score,
            with added key "reranker_score".
        """
        if not chunks:
            return []

        model = self._load()
        texts = [c.get("context", "") or c.get("sentence", "") for c in chunks]
        pairs = [(query, t) for t in texts]

        scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)

        scored = []
        for chunk, score in zip(chunks, scores, strict=False):
            enriched = dict(chunk)
            enriched["reranker_score"] = float(score)
            scored.append(enriched)

        scored.sort(key=lambda d: d["reranker_score"], reverse=True)
        return scored[:top_n]


def rerank_candidates(
    query: str,
    chunks: list[dict[str, Any]],
    top_n: int = 8,
) -> list[dict[str, Any]]:
    """Convenience function: instantiate default reranker and re-rank."""
    reranker = CrossEncoderReranker()
    return reranker.rerank(query, chunks, top_n=top_n)


@lru_cache(maxsize=2)
def _cached_reranker(model_name, device, stamp):
    return CrossEncoderReranker(model_name=model_name, device=device)


def get_reranker(model_name=None, device=None):
    from pathlib import Path
    settings = get_embedding_settings()
    name = model_name or settings.get('reranker_model', _DEFAULT_MODEL)
    target = Path(name)
    stamp = tuple((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(target.glob('*')) if p.is_file()) if target.is_dir() else ()
    return _cached_reranker(name,device or settings.get('device','cpu'),stamp)
