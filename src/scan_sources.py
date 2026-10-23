"""Per-paper retrieval. An empty abstract is never a reason to exclude its body."""
import hashlib
import json
from corpus_store import CorpusStore
from literature_store import paper_chunks


class ScanSourceError(ValueError):pass


def paper_revision(paper):
    fields=['paper_id','pmid','pmcid','doi','title','publication_types','publication_status','abstract_sha256','fulltext_sha256',
            'local_snapshot_sha256','local_corpus_paper_ids']
    # Also hash the text to detect stale/tampered summaries without trusting their hash fields.
    payload={k:paper.get(k) for k in fields}
    payload['abstract']=paper.get('abstract','')
    payload['fulltext_chunks']=paper.get('fulltext_chunks',[])
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def candidates_for_paper(paper,query,settings,temporary,*,gene_aliases=(),exclude_chunk_ids=()):
    """Select this paper's own quota, favoring body evidence when available."""
    k=query.top_k;gene=query.genes[0];disease=query.disease;body=[];excluded=set(exclude_chunk_ids)
    limit=min(500,k+len(excluded))
    if query.evidence_scope=='auto' and paper.get('local_corpus_paper_ids'):
        if not settings.corpus_db or not settings.corpus_db.is_file():raise ScanSourceError('本地全文库当前不可用，请重新配置后检索。')
        local=CorpusStore(settings.corpus_db);snapshot=local.fingerprint().get('snapshot_sha256')
        if snapshot!=paper.get('local_snapshot_sha256'):raise ScanSourceError('本地全文库已更换，请重新检索，旧候选不能直接抽取。')
        hits=[]
        for pid in paper['local_corpus_paper_ids']:
            hits+=local.search(gene,disease,limit,list(gene_aliases),list(query.disease_aliases),paper_ids=[pid],skip_reference_like=query.skip_reference_like)
        hits=sorted({c['chunk_id']:c for c in hits if c['chunk_id'] not in excluded}.values(),key=lambda c:c.get('retrieval_score',0),reverse=True)[:k]
        body=[local.with_neighbors(c,skip_reference_like=query.skip_reference_like) for c in hits]
        if local.fingerprint().get('snapshot_sha256')!=snapshot:raise ScanSourceError('检索期间本地全文库发生变化，请重新检索。')
    chunks=paper_chunks(paper,query.evidence_scope)
    online_body=[c for c in chunks if c.get('content_level')=='fulltext']
    abstracts=[c for c in chunks if c.get('content_level')=='abstract']
    # Prefer available body hits, then use abstract as a separate evidence level.
    if online_body:
        path=temporary/'online.sqlite';CorpusStore.build(path,online_body)
        store=CorpusStore(path)
        hits=[c for c in store.search(gene,disease,limit,list(gene_aliases),list(query.disease_aliases),skip_reference_like=query.skip_reference_like) if c['chunk_id'] not in excluded][:k]
        body.extend(store.with_neighbors(c,skip_reference_like=query.skip_reference_like) for c in hits)
    chosen=[];seen=set()
    for c in body:
        if c['chunk_id'] not in seen:chosen.append(c);seen.add(c['chunk_id'])
        if len(chosen)>=k:break
    if len(chosen)<k and abstracts:
        # The target may only be named in the title or a body definition. Send the
        # whole bounded abstract for assessment rather than hard-excluding it.
        for c in abstracts:
            if c['chunk_id'] in seen or c['chunk_id'] in excluded:continue
            chosen.append({**c,'context':c['context'][:9000],
                'context_truncated':len(c['context'])>9000});seen.add(c['chunk_id'])
            if len(chosen)>=k:break
    return chosen
