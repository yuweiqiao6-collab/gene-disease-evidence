"""Vector store backed by a numpy matrix (no FAISS dependency).

Stores dense embeddings as a .npy file and metadata as JSON.
Retrieval uses matrix dot-product (cosine similarity because vectors are L2-normalized).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from config import VECTOR_INDEX_NPY, VECTOR_METADATA_JSON


class VectorStore:
    """In-memory dense vector index with numpy-backed persistence."""

    def __init__(self) -> None:
        self.embeddings: np.ndarray | None = None
        self.metadata: list[dict[str, Any]] = []
        self._id_to_index: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, embeddings: np.ndarray, metadata: list[dict[str, Any]]) -> None:
        """Populate the store from pre-computed embeddings.

        Args:
            embeddings: Shape (N, D), float32, L2-normalized.
            metadata: List of dicts, length N, each must contain 'chunk_id'.
        """
        if embeddings.shape[0] != len(metadata):
            raise ValueError(
                f"embeddings count ({embeddings.shape[0]}) != metadata count ({len(metadata)})"
            )
        self.embeddings = embeddings.astype(np.float32)
        self.metadata = metadata
        self._id_to_index = {
            meta["chunk_id"]: idx for idx, meta in enumerate(metadata)
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query_embedding: np.ndarray, top_k: int = 100) -> list[dict[str, Any]]:
        """Retrieve top-k most similar chunks by cosine similarity.

        Args:
            query_embedding: Shape (D,) or (1, D), float32, L2-normalized.
            top_k: Number of candidates to return.

        Returns:
            List of metadata dicts augmented with 'vector_score'.
        """
        if self.embeddings is None or self.embeddings.size == 0:
            return []

        if query_embedding.ndim == 1:
            query_embedding = query_embedding.reshape(1, -1)

        # Dot-product because vectors are L2-normalized => cosine similarity
        scores = np.dot(self.embeddings, query_embedding.T).flatten()
        # argsort descending
        top_indices = np.argpartition(scores, -min(top_k, len(scores)))[-min(top_k, len(scores)):]
        top_indices = top_indices[np.argsort(-scores[top_indices])]

        results: list[dict[str, Any]] = []
        for rank, idx in enumerate(top_indices, start=1):
            meta = dict(self.metadata[idx])
            meta["vector_score"] = round(float(scores[idx]), 6)
            meta["vector_rank"] = rank
            results.append(meta)
        return results

    def get_by_chunk_id(self, chunk_id: str) -> dict[str, Any] | None:
        """Fetch metadata for a single chunk by ID."""
        idx = self._id_to_index.get(chunk_id)
        if idx is None:
            return None
        return dict(self.metadata[idx])

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(
        self,
        npy_path: Path | None = None,
        meta_path: Path | None = None,
    ) -> None:
        """Persist embeddings to .npy and metadata to JSON."""
        npy_path = npy_path or VECTOR_INDEX_NPY
        meta_path = meta_path or VECTOR_METADATA_JSON

        if self.embeddings is not None:
            npy_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=npy_path.parent)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    np.save(handle, self.embeddings, allow_pickle=False)
                os.replace(temporary, npy_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        from io_utils import write_json
        write_json(meta_path, self.metadata)

    @classmethod
    def load(
        cls,
        npy_path: Path | None = None,
        meta_path: Path | None = None,
    ) -> VectorStore:
        """Load a previously saved store from disk."""
        npy_path = npy_path or VECTOR_INDEX_NPY
        meta_path = meta_path or VECTOR_METADATA_JSON

        store = cls()
        if npy_path.exists():
            store.embeddings = np.load(npy_path).astype(np.float32)
        if meta_path.exists():
            store.metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            store._id_to_index = {
                meta["chunk_id"]: idx for idx, meta in enumerate(store.metadata)
            }
        return store

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        """Return True if embeddings and metadata are both loaded."""
        return (self.embeddings is not None and self.embeddings.ndim == 2
                and len(self.metadata) > 0 and self.embeddings.shape[0] == len(self.metadata))

    @property
    def dim(self) -> int:
        """Return the embedding dimension."""
        return self.embeddings.shape[1] if self.embeddings is not None else 0

    @property
    def count(self) -> int:
        """Return the number of indexed chunks."""
        return len(self.metadata)
