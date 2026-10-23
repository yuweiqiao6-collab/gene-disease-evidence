"""Embedding generator for dense vector retrieval.

Uses sentence-transformers to encode text chunks into dense vectors.
Model is loaded lazily as a singleton to avoid repeated initialization.
"""
from __future__ import annotations

import os
import warnings
from typing import Any

import numpy as np

from config import get_embedding_settings

# Reduce GPU memory fragmentation for long-running encoding jobs
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# Lazy singleton instance
_model: Any = None
_model_name: str | None = None
_model_signature: str | None = None


def _load_model() -> Any:
    """Lazy-load the sentence-transformers model singleton."""
    global _model, _model_name, _model_signature
    settings = get_embedding_settings()
    target_model = settings["model_name"]
    device = settings["device"]

    from cache_manager import embedding_signature
    signature = embedding_signature()+":"+str(device)
    if _model is not None and _model_signature == signature:
        return _model

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers is not installed. "
            "Install it with: pip install sentence-transformers"
        ) from exc

    try:
        _model = SentenceTransformer(target_model, device=device, revision=str(settings["model_revision"]))
        # Clamp max_seq_length to 512; user chunks are short sentences and 8192 wastes VRAM.
        _model.max_seq_length = min(getattr(_model, "max_seq_length", 8192), 512)
        _model_name = target_model
        _model_signature = signature
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load embedding model '{target_model}'. "
            f"If you are behind a firewall, download the model manually and set EMBEDDING_MODEL to the local path. "
            f"Original error: {exc}"
        ) from exc

    return _model


def embed_texts(texts: list[str], batch_size: int | None = None, show_progress: bool = False) -> np.ndarray:
    """Encode a list of texts into dense vectors (L2-normalized).

    Args:
        texts: List of raw text strings.
        batch_size: Inference batch size. Defaults to config value (64).
        show_progress: Whether to show a progress bar.

    Returns:
        Array of shape (N, D) with dtype float32, L2-normalized.
    """
    if not texts:
        return np.array([], dtype=np.float32)

    model = _load_model()
    settings = get_embedding_settings()
    batch_size = batch_size or settings["batch_size"]

    import torch
    torch.set_num_threads(min(4, torch.get_num_threads()))

    # Use inference_mode to avoid tracking gradients and save memory
    with torch.inference_mode():
        embeddings = model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=show_progress,
            normalize_embeddings=True,  # L2 normalize so dot-product == cosine similarity
            convert_to_numpy=True,
        )

    # Explicitly clear PyTorch cache after each batch to prevent OOM on long runs
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    return embeddings.astype(np.float32)


def embed_query(text: str) -> np.ndarray:
    """Encode a single query text into a dense vector.

    Args:
        text: Query text (typically "gene disease").

    Returns:
        Array of shape (D,) with dtype float32.
    """
    return embed_texts([text])[0]


def embed_chunks(chunks: list[dict[str, Any]], field: str = "context") -> np.ndarray:
    """Batch encode chunks using the specified text field.

    Args:
        chunks: List of chunk dictionaries.
        field: Which field to encode (default "context").

    Returns:
        Array of shape (N, D) with dtype float32.
    """
    texts = [chunk.get(field, "") for chunk in chunks]
    return embed_texts(texts, show_progress=True)


def embed_chunks_incremental(
    chunks: list[dict[str, Any]],
    field: str = "context",
    shard_size: int = 100000,
) -> np.ndarray:
    """Incremental encoding for very large chunk lists to avoid OOM.

    Args:
        chunks: List of chunk dictionaries.
        field: Which field to encode.
        shard_size: Number of chunks per shard.

    Returns:
        Concatenated array of shape (N, D).
    """
    if len(chunks) <= shard_size:
        return embed_chunks(chunks, field=field)

    all_embeddings: list[np.ndarray] = []
    for i in range(0, len(chunks), shard_size):
        shard = chunks[i : i + shard_size]
        print(f"[vector] Encoding shard {i//shard_size + 1}/{(len(chunks)-1)//shard_size + 1} ({len(shard)} chunks)...")
        emb = embed_texts([c.get(field, "") for c in shard], show_progress=True)
        all_embeddings.append(emb)
    return np.concatenate(all_embeddings, axis=0)


# ---------------------------------------------------------------------------
# Convenience helpers for build / search
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """Check whether sentence-transformers can be imported and the model loads."""
    try:
        _load_model()
        return True
    except Exception:
        return False


def get_model_info() -> dict[str, Any]:
    """Return information about the loaded model."""
    model = _load_model()
    settings = get_embedding_settings()
    return {
        "model_name": settings["model_name"],
        "device": settings["device"],
        "vector_dim": model.get_sentence_embedding_dimension(),
        "max_seq_length": model.max_seq_length,
    }
