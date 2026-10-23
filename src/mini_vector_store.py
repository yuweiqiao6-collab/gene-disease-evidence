"""Mini vector store: on-demand encoding of BM25-retrieved chunks.

Each (gene, disease) query gets a dedicated mini vector index.
These indexes are cached to disk and reused on repeated queries.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from cache_manager import (
    get_mini_vector_paths,
    mini_vector_exists,
    register_mini_vector,
    mini_vector_signature,
    embedding_signature,
    MINI_VECTOR_DIR,
)
from embedding_generator import embed_chunks
from vector_store import VectorStore
from embedding_cache import EmbeddingCache


class MiniVectorStore:
    """On-demand vector store for a specific (gene, disease) query."""

    def __init__(self, gene: str, disease: str, signature: str = "") -> None:
        self.signature = signature
        self.gene = gene
        self.disease = disease
        self._store: VectorStore | None = None

    def build(self, chunks: list[dict[str, Any]]) -> None:
        """Encode chunks and build the mini vector index."""
        if not chunks:
            self._store = VectorStore()
            return
        print(f"[mini-vector] Encoding {len(chunks)} chunks for {self.gene} + {self.disease} ...")
        embeddings, self.cache_stats = EmbeddingCache(get_mini_vector_paths(self.gene, self.disease, self.signature)[0].parent.parent / "chunk-vectors.sqlite").encode(
            chunks, embedding_signature(), embed_chunks)
        metadata = [dict(c) for c in chunks]
        self._store = VectorStore()
        self._store.build(embeddings, metadata)
        self._save()
        print(f"[mini-vector] Saved {self._store.count} vectors to cache.")

    def _save(self) -> None:
        if self._store is None:
            return
        npy_path, meta_path = get_mini_vector_paths(self.gene, self.disease, self.signature)
        self._store.save(npy_path=npy_path, meta_path=meta_path)
        register_mini_vector(self.gene, self.disease, self.signature)

    def _load(self) -> bool:
        npy_path, meta_path = get_mini_vector_paths(self.gene, self.disease, self.signature)
        if not npy_path.exists() or not meta_path.exists():
            return False
        try:
            self._store = VectorStore.load(npy_path=npy_path, meta_path=meta_path)
        except (ValueError, OSError):
            return False
        return (self._store.is_ready and np.isfinite(self._store.embeddings).all()
                and mini_vector_signature(self._store.metadata)==self.signature)

    def search(self, query_embedding: np.ndarray, top_k: int = 100) -> list[dict[str, Any]]:
        if self._store is None:
            if not self._load():
                return []
        return self._store.search(query_embedding, top_k=top_k)

    @property
    def is_ready(self) -> bool:
        if self._store is None:
            return self._load()
        return self._store.is_ready

    @property
    def count(self) -> int:
        return self._store.count if self._store else 0


def get_or_build_mini_store(
    gene: str,
    disease: str,
    chunks: list[dict[str, Any]],
) -> MiniVectorStore:
    """Return a ready mini store, building it only if cache miss."""
    signature = mini_vector_signature(chunks)
    store = MiniVectorStore(gene, disease, signature)
    if mini_vector_exists(gene, disease, signature) and store.is_ready:
        print(f"[mini-vector] Cache hit for {gene} + {disease} ({store.count} vectors).")
        store._store.metadata = [dict(c) for c in chunks]
        store.cache_stats = {"reused": len(chunks), "encoded": 0}
        return store
    store.build(chunks)
    return store
