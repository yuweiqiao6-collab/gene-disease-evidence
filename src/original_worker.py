"""Local CPU worker running the same retrieval core as the original CLI."""
import contextlib
import json
import os
from pathlib import Path
import sys
import traceback


def main():
    if hasattr(os, 'nice'):os.nice(10)
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        torch.set_num_threads(2);torch.set_num_interop_threads(1)
        import config
        config.INDEX_JSON = Path(sys.argv[1]).resolve()
        from index_manager import get_index_manager
        from retrieval_pipeline import retrieve_evidence
        from pubmed_client import fetch_pubmed_for_query
        from alias_lookup import lookup_gene_aliases, lookup_disease_aliases
    for line in sys.stdin:
        request = {}; response = {}
        try:
            request = json.loads(line)
            gene = request['gene']; disease = request['disease']
            ga = request.get('gene_aliases', []); da = request.get('disease_aliases', [])
            with contextlib.redirect_stdout(sys.stderr):
                if request.get('auto_aliases'):
                    ga = list(dict.fromkeys([*ga, *lookup_gene_aliases(gene)[:10]]))
                    da = list(dict.fromkeys([*da, *lookup_disease_aliases(disease)[:10]]))
                ga=ga[:10];da=da[:10]
                online = []; status = 'disabled';pubmed_query='';pubmed_url=''
                if request.get('include_pubmed'):
                    online, pubmed_query, pubmed_url = fetch_pubmed_for_query(gene, disease,
                        max_results=request.get('max_papers', 50), gene_aliases=ga,
                        disease_aliases=da, use_aliases=True)
                    status = fetch_pubmed_for_query.last_status
                rows, trace = retrieve_evidence(get_index_manager().get_index(), gene, disease,
                    online_chunks=online, gene_aliases=ga, disease_aliases=da,
                    bm25_top_n=request.get('bm25_top_n',500), encode_top_n=request.get('encode_top_n',500),
                    top_k=request.get('top_k',12), use_hybrid=True, use_reranker=True,
                    per_paper_limit=request.get('per_paper_limit',0))
                trace.update(pubmed_status=status, gene_aliases=ga, disease_aliases=da,
                    pubmed_query=pubmed_query,pubmed_url=pubmed_url,pubmed_fetched_chunks=len(online),
                    device='cpu', threads=2)
            response = dict(id=request['id'], ok=True, result=dict(candidates=rows, trace=trace))
        except Exception:
            traceback.print_exc(file=sys.stderr)
            response = dict(id=request.get('id'), ok=False)
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':main()
