"""Hybrid retrieval: BM25 sparse + dense vector with fusion strategies.

Supports three fusion modes:
  - rrf     : Reciprocal Rank Fusion (default, parameter-free)
  - weighted: Linear score combination
  - cascade : Vector re-rank of BM25 results

Gracefully falls back to pure BM25 if vector components are unavailable.
"""
from __future__ import annotations

from typing import Any

from config import EXPRESSION_TERMS, RELATION_TERMS
from config import get_embedding_settings
from embedding_generator import embed_query
from original_query_utils import contains_any_term, query_terms
from retrieval import retrieve_candidates
from text_utils import contains_any
from vector_store import VectorStore


def _enrich_chunk(chunk: dict[str, Any], gene: str, disease: str,
                  gene_aliases: list[str] | None = None,
                  disease_aliases: list[str] | None = None) -> dict[str, Any]:
    """Ensure chunk has gene_hit / disease_hit / expression_hit / relation_hit / retrieval_score."""
    enriched = dict(chunk)
    gene_terms = query_terms(gene, gene_aliases)
    disease_terms = query_terms(disease, disease_aliases)
    context = enriched.get("context", "")
    enriched.setdefault("gene_hit", contains_any_term(context, gene_terms))
    enriched.setdefault("disease_hit", contains_any_term(context, disease_terms))
    enriched.setdefault("expression_hit", contains_any(context, EXPRESSION_TERMS))
    enriched.setdefault("relation_hit", contains_any(context, RELATION_TERMS))
    enriched.setdefault("retrieval_score", enriched.get("hybrid_score", enriched.get("vector_score", 0.0)))
    enriched.setdefault("gene_terms_checked", gene_terms)
    enriched.setdefault("disease_terms_checked", disease_terms)
    return enriched


def _filter_require_gene(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in chunks if c.get("gene_hit")]


def _normalize_scores(results: list[dict[str, Any]], score_key: str) -> dict[str, float]:
    """Min-max normalize scores to [0, 1]."""
    scores = {r["chunk_id"]: r.get(score_key, 0.0) for r in results}
    vals = list(scores.values())
    if not vals:
        return scores
    min_v, max_v = min(vals), max(vals)
    span = max_v - min_v if max_v != min_v else 1.0
    return {cid: (s - min_v) / span for cid, s in scores.items()}


def _fuse_rrf(
    bm25_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    k: int = 60,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion.

    score(doc) = sum(1 / (k + rank_i)) across both result lists.
    """
    rrf: dict[str, float] = {}
    metas: dict[str, dict[str, Any]] = {}

    for rank, doc in enumerate(bm25_results, start=1):
        cid = doc["chunk_id"]
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (k + rank)
        metas.setdefault(cid, {}).update(doc)

    for rank, doc in enumerate(vector_results, start=1):
        cid = doc["chunk_id"]
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (k + rank)
        metas.setdefault(cid, {}).update(doc)

    sorted_cids = sorted(rrf, key=lambda c: rrf[c], reverse=True)
    return [{**metas[cid], "hybrid_score": round(rrf[cid], 6)} for cid in sorted_cids]


def _fuse_weighted(
    bm25_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    alpha: float = 0.5,
) -> list[dict[str, Any]]:
    """Linear weighted fusion of normalized scores.

    final = alpha * norm(bm25) + (1 - alpha) * norm(vector)
    """
    bm25_norm = _normalize_scores(bm25_results, "retrieval_score")
    vec_norm = _normalize_scores(vector_results, "vector_score")

    all_cids = set(bm25_norm) | set(vec_norm)
    scores: dict[str, float] = {}
    metas: dict[str, dict[str, Any]] = {}

    for doc in bm25_results:
        cid = doc["chunk_id"]
        scores[cid] = alpha * bm25_norm.get(cid, 0.0)
        metas[cid] = dict(doc)
    for doc in vector_results:
        cid = doc["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + (1 - alpha) * vec_norm.get(cid, 0.0)
        if cid not in metas:
            metas[cid] = dict(doc)
        else:
            metas[cid].update(doc)

    sorted_cids = sorted(all_cids, key=lambda c: scores[c], reverse=True)
    return [{**metas[cid], "hybrid_score": round(scores[cid], 6)} for cid in sorted_cids]


def _fuse_cascade(
    bm25_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Re-rank BM25 results by vector scores.

    Only BM25 candidates are kept; they are re-ordered by vector similarity.
    If a BM25 candidate has no vector score, it is ranked last.
    """
    vec_scores = {r["chunk_id"]: r.get("vector_score", 0.0) for r in vector_results}
    bm25_cids = {r["chunk_id"] for r in bm25_results}

    # Intersection: candidates present in both BM25 and vector results
    fused = []
    for doc in bm25_results:
        cid = doc["chunk_id"]
        enriched = dict(doc)
        enriched["vector_score"] = vec_scores.get(cid, 0.0)
        enriched["hybrid_score"] = vec_scores.get(cid, 0.0)
        fused.append(enriched)

    fused.sort(key=lambda d: d["hybrid_score"], reverse=True)
    return fused


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def retrieve_candidates_hybrid(
    bm25_index: dict[str, Any],
    gene: str,
    disease: str,
    top_k: int = 12,
    gene_aliases: list[str] | None = None,
    disease_aliases: list[str] | None = None,
    require_gene: bool = True,
    vector_store: VectorStore | None = None,
    fusion_strategy: str | None = None,
    precomputed_bm25_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve candidates using BM25 + dense vector hybrid search.

    Falls back to pure BM25 if:
      - ENABLE_HYBRID_RETRIEVAL is False
      - vector_store is None or not ready
      - embedding model failed to load

    Args:
        bm25_index: The existing BM25 index dict.
        gene: Target gene name.
        disease: Target disease name.
        top_k: Final number of candidates to return.
        gene_aliases: Additional gene aliases.
        disease_aliases: Additional disease aliases.
        require_gene: If True, filter out chunks without gene mention.
        vector_store: Pre-loaded VectorStore. If None, one is auto-loaded.
        fusion_strategy: "rrf" | "weighted" | "cascade". Defaults to config.

    Returns:
        List of enriched chunk dicts sorted by fused score.
    """
    settings = get_embedding_settings()
    enable_hybrid = settings["enable_hybrid"]
    strategy = (fusion_strategy or settings["fusion_strategy"]).lower()
    vector_top_k = settings["vector_top_k"]
    alpha = settings["hybrid_weight_alpha"]
    rrf_k = settings["rrf_k"]

    # ----- 1. BM25 retrieval (always run) -----
    if precomputed_bm25_results is None:
        bm25_results = retrieve_candidates(
            bm25_index,
            gene=gene,
            disease=disease,
            top_k=vector_top_k,
            gene_aliases=gene_aliases,
            disease_aliases=disease_aliases,
            require_gene=require_gene,
        )
    else:
        # The caller already performed the same BM25 retrieval. Reuse it to
        # avoid a second full scan of the multi-million-chunk corpus.
        bm25_results = precomputed_bm25_results[:vector_top_k]

    # ----- 2. Check if hybrid is possible -----
    if vector_store is not None: vector_store.last_method = "bm25"
    if not enable_hybrid:
        return bm25_results[:top_k]

    store = vector_store
    if store is None:
        store = VectorStore.load()
    if not store.is_ready:
        return bm25_results[:top_k]

    # ----- 3. Dense vector retrieval -----
    query_text = f"{gene} {disease}"
    try:
        query_vec = embed_query(query_text)
    except Exception:
        # Embedding model unavailable → fallback to BM25
        return bm25_results[:top_k]

    vector_results = store.search(query_vec, top_k=vector_top_k)
    store.last_method = "bm25+dense"

    # ----- 4. Fuse -----
    if strategy == "rrf":
        fused = _fuse_rrf(bm25_results, vector_results, k=rrf_k)
    elif strategy == "weighted":
        fused = _fuse_weighted(bm25_results, vector_results, alpha=alpha)
    elif strategy == "cascade":
        fused = _fuse_cascade(bm25_results, vector_results)
    else:
        # Unknown strategy → safest fallback to RRF
        fused = _fuse_rrf(bm25_results, vector_results, k=rrf_k)

    # Enrich all results with hit flags and apply require_gene filter
    enriched = [_enrich_chunk(c, gene, disease, gene_aliases, disease_aliases) for c in fused]
    if require_gene:
        enriched = _filter_require_gene(enriched)

    return enriched[:top_k]


def is_hybrid_available() -> bool:
    """Quick check whether hybrid retrieval can be used on this system."""
    settings = get_embedding_settings()
    if not settings["enable_hybrid"]:
        return False
    store = VectorStore.load()
    if not store.is_ready:
        return False
    from embedding_generator import is_available
    return is_available()
