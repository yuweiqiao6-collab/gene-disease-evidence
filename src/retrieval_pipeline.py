"""The original candidate-only retrieval chain, shared by CLI and web adapter.

No LLM calls, no full-corpus vector build. Default ranking/quotas preserve the
original pipeline; optional diversity is explicit and defaults to off.
"""
from __future__ import annotations
import time
from retrieval import retrieve_candidates, score_chunk_bm25
from mini_vector_store import get_or_build_mini_store
from hybrid_retrieval import retrieve_candidates_hybrid
from reranker import get_reranker


def rank_candidates(merged, gene, disease, *, index=None, top_k=12,
                    encode_top_n=500, use_hybrid=True, use_reranker=True,
                    fusion_strategy=None, gene_aliases=None, disease_aliases=None,
                    require_gene=True, reranker_top_n=None, per_paper_limit=0):
    if not 1 <= top_k <= 50 or not top_k <= encode_top_n <= 2000:
        raise ValueError('候选编码上限需不少于最终片段数，且不超过2000。')
    if not isinstance(per_paper_limit, int) or per_paper_limit < 0:
        raise ValueError('每篇名额不能为负数。')
    unique = {}
    for c in merged:
        cid = c['chunk_id']
        if cid in unique and unique[cid].get('context') != c.get('context'):
            raise ValueError('相同片段编号对应不同原文，停止混合。')
        unique.setdefault(cid, dict(c))
    merged = sorted(unique.values(), key=lambda c: c.get('retrieval_score', 0), reverse=True)
    trace = dict(engine='original-candidate-pipeline-v2', coarse_candidates=len(merged),
                 encoded_pool_limit=encode_top_n, final_limit=top_k,
                 hybrid_requested=use_hybrid, reranker_requested=use_reranker,
                 actual_method='bm25', cache={'reused':0, 'encoded':0}, timings={})
    if not merged:
        return [], trace
    candidates = merged
    if use_hybrid:
        started = time.monotonic()
        store = get_or_build_mini_store(gene, disease, merged[:encode_top_n])
        trace['cache'] = dict(getattr(store, 'cache_stats', {}))
        trace['timings']['vector_prepare_seconds'] = round(time.monotonic()-started, 4)
        started = time.monotonic()
        candidates = retrieve_candidates_hybrid(index or {}, gene, disease,
            top_k=max(top_k, int(top_k*2.5)), gene_aliases=gene_aliases,
            disease_aliases=disease_aliases, require_gene=require_gene,
            vector_store=store, fusion_strategy=fusion_strategy,
            precomputed_bm25_results=merged)
        trace['actual_method'] = getattr(store, 'last_method', 'bm25')
        trace['timings']['fusion_seconds'] = round(time.monotonic()-started, 4)
    trace['before_rerank'] = len(candidates)
    if use_reranker and len(candidates) > 1:
        started = time.monotonic()
        candidates = get_reranker().rerank(query=f'{gene} {disease}', chunks=candidates,
            top_n=len(candidates) if per_paper_limit else (reranker_top_n or top_k))
        trace['timings']['rerank_seconds'] = round(time.monotonic()-started, 4)
        trace['actual_method'] += '+cross_encoder'
    selected = []; per_paper = {}
    for c in candidates:
        pid = c.get('paper_id', c['chunk_id'])
        if per_paper_limit and per_paper.get(pid, 0) >= per_paper_limit:
            continue
        selected.append(c); per_paper[pid] = per_paper.get(pid, 0)+1
        if len(selected) >= top_k:
            break
    trace.update(selected_count=len(selected), unique_papers=len(per_paper),
                 selected_characters=sum(len(c.get('context', '')) for c in selected),
                 per_paper_limit=per_paper_limit,
                 ranked=[{k:c[k] for k in ('chunk_id','paper_id','retrieval_score','vector_score','hybrid_score','reranker_score') if k in c} for c in selected])
    return selected, trace


def retrieve_evidence(index, gene, disease, *, online_chunks=(), local_candidates=None,
                      bm25_top_n=500, **options):
    started = time.monotonic()
    ga = options.get('gene_aliases'); da = options.get('disease_aliases')
    require_gene = options.get('require_gene', True)
    local = local_candidates if local_candidates is not None else retrieve_candidates(
        index, gene, disease, top_k=bm25_top_n, gene_aliases=ga,
        disease_aliases=da, require_gene=require_gene)
    online = [c for chunk in online_chunks if (c := score_chunk_bm25(
        chunk, index, gene, disease, ga, da, require_gene)) is not None]
    coarse_seconds = time.monotonic()-started
    selected, trace = rank_candidates([*local, *online], gene, disease, index=index, **options)
    trace.update(local_candidates=len(local), online_candidates=len(online))
    trace['timings']['coarse_seconds'] = round(coarse_seconds, 4)
    return selected, trace
