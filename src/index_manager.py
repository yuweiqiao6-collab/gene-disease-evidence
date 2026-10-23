"""Pre-loaded index manager: singleton that loads BM25 index once into memory.

The BM25 index (bm25_index.json) is ~5 GB and takes 10-30 seconds to parse
from JSON.  In batch mode (e.g. 317 genes) this cost is paid *per gene*,
adding 50+ minutes of pure I/O.  This module loads the index once and
serves it to all subsequent callers.

Usage:
    from index_manager import get_index_manager
    mgr = get_index_manager()
    index = mgr.get_index()          # returns dict, loads from disk on first call
    index = mgr.get_index(force=True) # force reload (e.g. after rebuild)
"""
from __future__ import annotations

import gc
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from config import INDEX_JSON


class IndexManager:
    """Thread-safe singleton that holds the BM25 index in memory.

    The index is loaded lazily on first access.  Subsequent calls return
    the same object without any I/O.
    """

    _instance: IndexManager | None = None

    def __new__(cls) -> IndexManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._index: dict[str, Any] | None = None
            cls._instance._load_time: float = 0.0
            cls._instance._last_mtime: float = 0.0
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_index(self, force: bool = False) -> dict[str, Any]:
        """Return the BM25 index, loading from disk if needed.

        Args:
            force: If True, reload even if already cached.

        Returns:
            The BM25 index dict with keys: doc_count, avg_doc_len,
            doc_freq, tokenized_chunks, chunks.
        """
        if not force and self._index is not None:
            # Validate file hasn't changed (quick stat)
            current_mtime = self._file_mtime()
            if current_mtime == self._last_mtime:
                return self._index

        self._load()
        return self._index  # type: ignore[return-value]

    def is_loaded(self) -> bool:
        """Return True if the index is already in memory."""
        return self._index is not None

    def unload(self) -> None:
        """Release the index from memory."""
        self._index = None
        self._last_mtime = 0.0
        self._load_time = 0.0
        gc.collect()

    @property
    def load_time(self) -> float:
        """Seconds spent loading the index on the last load."""
        return self._load_time

    @property
    def chunk_count(self) -> int:
        """Number of chunks in the loaded index."""
        if self._index is None:
            return 0
        return len(self._index.get("chunks", []))

    @property
    def memory_usage_mb(self) -> float:
        """Approximate memory usage of the loaded index in MB."""
        if self._index is None:
            return 0.0
        try:
            size_bytes = sys.getsizeof(self._index)
            # Rough estimate: chunks + tokenized_chunks + doc_freq
            chunks = self._index.get("chunks", [])
            tokenized = self._index.get("tokenized_chunks", [])
            doc_freq = self._index.get("doc_freq", {})
            size_bytes += sys.getsizeof(chunks) + sys.getsizeof(tokenized) + sys.getsizeof(doc_freq)
            # Add per-element overhead (rough)
            size_bytes += len(chunks) * 200  # ~200 bytes per chunk dict
            size_bytes += len(tokenized) * 100  # ~100 bytes per token list
            size_bytes += len(doc_freq) * 80  # ~80 bytes per doc_freq entry
            return size_bytes / (1024 * 1024)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _file_mtime(self) -> float:
        return INDEX_JSON.stat().st_mtime if INDEX_JSON.exists() else 0.0

    def _load(self) -> None:
        if not INDEX_JSON.exists():
            raise FileNotFoundError(
                f"BM25 index not found at {INDEX_JSON}. "
                "Run: python src/02_build_index.py"
            )

        t0 = time.time()
        with open(INDEX_JSON, "r", encoding="utf-8") as f:
            self._index = json.load(f)
        self._last_mtime = self._file_mtime()
        self._load_time = time.time() - t0

        chunk_count = self.chunk_count
        print(
            f"[index_manager] Loaded BM25 index: {chunk_count:,} chunks, "
            f"{self.memory_usage_mb:.1f} MB, {self._load_time:.1f}s"
        )


def get_index_manager() -> IndexManager:
    """Return the global IndexManager singleton."""
    return IndexManager()
