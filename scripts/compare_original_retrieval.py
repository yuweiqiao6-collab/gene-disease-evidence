"""Compare final budgets with the original retrieval core; never calls an LLM.

Input is an explicit frozen BM25 index and optional recorded online chunks.
No online fetching; medical relevance stays unknown without reviewed labels.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))


def compare(index, gene, disease, online=(), *, coarse=500, encode=500, gene_aliases=None, disease_aliases=None):
    from retrieval_pipeline import retrieve_evidence
    reports=[]; rows={}
    for k in [5,10,15]:
        candidates,trace=retrieve_evidence(index,gene,disease,online_chunks=online,
            top_k=k,bm25_top_n=coarse,encode_top_n=encode,gene_aliases=gene_aliases,disease_aliases=disease_aliases)
        reports.append(dict(k=k, trace=trace,
            candidate_ids=[c['chunk_id'] for c in candidates],
            relevance=None, recall=None, input_tokens=None,
            note='正文字符不等于服务商词元；无人工完整参考时不报告相关性或召回率。'))
        for c in candidates:
            rows.setdefault(c['chunk_id'],c)
    return dict(model_api_calls=0,online_requests=0,comparisons=reports),list(rows.values())


if __name__=='__main__':
    p=argparse.ArgumentParser(description='固定语料比较最终5/10/15组，仅检索、不调用付费LLM或文献接口')
    p.add_argument('--index',type=Path,required=True);p.add_argument('--gene',required=True);p.add_argument('--disease',required=True)
    p.add_argument('--recorded-online',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--coarse',type=int,default=500);p.add_argument('--encode',type=int,default=500)
    p.add_argument('--gene-alias',action='append',default=[]);p.add_argument('--disease-alias',action='append',default=[])
    args=p.parse_args()
    if args.output.exists():p.error('输出目录已存在。')
    if not 15<=args.encode<=2000 or not 1<=args.coarse<=2000:p.error('候选上限无效。')
    # Models must already be local/cached. This script never downloads weights.
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    import urllib.request
    def forbidden(*a,**kw):raise RuntimeError('比较脚本禁止文献或模型接口请求。')
    urllib.request.urlopen=forbidden
    index=json.loads(args.index.read_text())
    online=[json.loads(s) for s in args.recorded_online.read_text().splitlines() if s.strip()] if args.recorded_online else []
    report,rows=compare(index,args.gene,args.disease,online,coarse=args.coarse,encode=args.encode,
        gene_aliases=args.gene_alias,disease_aliases=args.disease_alias)
    args.output.mkdir(parents=True,mode=0o700)
    (args.output/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (args.output/'selected-candidates.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    with (args.output/'review.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['chunk_id','paper_id','context','human_relevant','reason'])
        for row in rows:writer.writerow([row['chunk_id'],row.get('paper_id',''),row.get('context',''),'',''])
    print(json.dumps({'compared_k':[5,10,15],'model_calls':0,'network_calls':0}))
