from __future__ import annotations

import math
from collections import Counter
from typing import Any

from config import EXPRESSION_TERMS, RELATION_TERMS
from original_query_utils import contains_any_term, query_terms
from text_utils import contains_any, tokenize


def build_index(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    tokenized = [tokenize(chunk.get("context", "")) for chunk in chunks]
    doc_freq: Counter[str] = Counter()
    for tokens in tokenized:
        doc_freq.update(set(tokens))
    return {
        "doc_count": len(chunks),
        "avg_doc_len": sum(len(tokens) for tokens in tokenized) / max(1, len(tokenized)),
        "doc_freq": dict(doc_freq),
        "tokenized_chunks": tokenized,
        "chunks": chunks,
    }


def bm25_score(
    query_tokens: list[str],
    document_tokens: list[str],
    doc_freq: dict[str, int],
    doc_count: int,
    avg_doc_len: float,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    if not document_tokens:
        return 0.0
    tf = Counter(document_tokens)
    score = 0.0
    doc_len = len(document_tokens)
    for token in query_tokens:
        freq = tf.get(token, 0)
        if freq == 0:
            continue
        df = doc_freq.get(token, 0)
        idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
        denominator = freq + k1 * (1 - b + b * doc_len / max(avg_doc_len, 1e-9))
        score += idf * (freq * (k1 + 1) / denominator)
    return score


def score_chunk_bm25(
    chunk: dict[str, Any],
    index: dict[str, Any],
    gene: str,
    disease: str,
    gene_aliases: list[str] | None = None,
    disease_aliases: list[str] | None = None,
    require_gene: bool = True,
) -> dict[str, Any] | None:
    """Compute BM25 score and hit flags for a single chunk using global index stats."""
    gene_terms = query_terms(gene, gene_aliases)
    disease_terms = query_terms(disease, disease_aliases)
    query = " ".join([*gene_terms, *disease_terms, *EXPRESSION_TERMS, *RELATION_TERMS])
    query_tokens = tokenize(query)

    context = chunk.get("context", "")
    tokens = tokenize(context)

    doc_freq = index.get("doc_freq", {})
    doc_count = int(index.get("doc_count", 1))
    avg_doc_len = float(index.get("avg_doc_len", 1.0))

    gene_hit = contains_any_term(context, gene_terms)
    disease_hit = contains_any_term(context, disease_terms)
    expression_hit = contains_any(context, EXPRESSION_TERMS)
    relation_hit = contains_any(context, RELATION_TERMS)

    if require_gene and not gene_hit:
        return None

    score = bm25_score(query_tokens, tokens, doc_freq, doc_count, avg_doc_len)
    if gene_hit:
        score += 8.0
    if disease_hit:
        score += 8.0
    if gene_hit and disease_hit:
        score += 12.0
    if expression_hit:
        score += 3.0
    if relation_hit:
        score += 3.0

    enriched = dict(chunk)
    enriched["retrieval_score"] = round(score, 4)
    enriched["gene_hit"] = gene_hit
    enriched["disease_hit"] = disease_hit
    enriched["gene_terms_checked"] = gene_terms
    enriched["disease_terms_checked"] = disease_terms
    enriched["expression_hit"] = expression_hit
    enriched["relation_hit"] = relation_hit
    return enriched


def retrieve_candidates(
    index: dict[str, Any],
    gene: str,
    disease: str,
    top_k: int = 12,
    gene_aliases: list[str] | None = None,
    disease_aliases: list[str] | None = None,
    require_gene: bool = False,
) -> list[dict[str, Any]]:
    gene_terms = query_terms(gene, gene_aliases)
    disease_terms = query_terms(disease, disease_aliases)
    query = " ".join([*gene_terms, *disease_terms, *EXPRESSION_TERMS, *RELATION_TERMS])
    query_tokens = tokenize(query)
    chunks = index.get("chunks", [])
    tokenized_chunks = index.get("tokenized_chunks", [])
    doc_freq = index.get("doc_freq", {})
    doc_count = int(index.get("doc_count", len(chunks)))
    avg_doc_len = float(index.get("avg_doc_len", 1.0))

    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk, tokens in zip(chunks, tokenized_chunks, strict=False):
        context = chunk.get("context", "")
        gene_hit = contains_any_term(context, gene_terms)
        # In target-specific mode almost every corpus chunk is irrelevant.
        # Reject it before disease/expression scans and BM25 term-frequency
        # construction; the previous order spent most runtime scoring chunks
        # that were discarded immediately afterwards.
        if require_gene and not gene_hit:
            continue
        disease_hit = contains_any_term(context, disease_terms)
        expression_hit = contains_any(context, EXPRESSION_TERMS)
        relation_hit = contains_any(context, RELATION_TERMS)
        score = bm25_score(query_tokens, tokens, doc_freq, doc_count, avg_doc_len)
        if gene_hit:
            score += 8.0
        if disease_hit:
            score += 8.0
        if gene_hit and disease_hit:
            score += 12.0
        if expression_hit:
            score += 3.0
        if relation_hit:
            score += 3.0
        if gene_hit or disease_hit:
            enriched = dict(chunk)
            enriched["retrieval_score"] = round(score, 4)
            enriched["gene_hit"] = gene_hit
            enriched["disease_hit"] = disease_hit
            enriched["gene_terms_checked"] = gene_terms
            enriched["disease_terms_checked"] = disease_terms
            enriched["expression_hit"] = expression_hit
            enriched["relation_hit"] = relation_hit
            scored.append((score, enriched))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]]
