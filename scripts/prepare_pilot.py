"""Create a private, deterministic pilot from existing PubMed caches; no network calls."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from corpus_store import CorpusStore
from io_utils import write_json, write_jsonl
from query_utils import contains_any_term

QUERIES=[('IL6','rheumatoid arthritis',['IL6','IL-6','interleukin 6']),
         ('ABCC8','type 2 diabetes',['ABCC8','SUR1','sulfonylurea receptor 1']),
         ('CD36','type 2 diabetes',['CD36','fatty acid translocase'])]


def split_for(paper_id):
    # A paper always stays in one split, regardless of target gene or sentence.
    return 'test' if int(hashlib.sha256(('pilot-v1:'+paper_id).encode()).hexdigest()[:8],16)%5==0 else 'development'


def stratum(context):
    lower=context.lower()
    if any(t in lower for t in ['not associated','no association','not significant','no significant']): return 'negation_hint'
    if any(t in lower for t in ['expression','upregulat','downregulat','knockdown','overexpression']): return 'expression_hint'
    if any(t in lower for t in ['variant','polymorphism','allele','mutation']): return 'variant_hint'
    return 'other_hint'


def prepare(cache_root:Path,output:Path,per_query=10):
    if not 1<=per_query<=15: raise ValueError('per-query must be 1..15')
    if output.exists() and any(output.iterdir()): raise ValueError('Output directory is not empty; choose a new snapshot')
    manifest=json.loads((cache_root/'manifest.json').read_text())
    selected,cases,seen,query_counts=[],[],set(),{}
    for gene,disease,aliases in QUERIES:
        buckets={name:[] for name in ['expression_hint','negation_hint','variant_hint','other_hint']}
        for key,entry in manifest.items():
            if entry.get('gene')!=gene or entry.get('disease')!=disease or not re.fullmatch('[a-f0-9]{16,64}',key): continue
            path=cache_root/'pubmed'/f'{key}.jsonl'
            if not path.is_file(): continue
            with path.open() as handle:
                for line in handle:
                    if not line.strip(): continue
                    row=json.loads(line)
                    if not re.fullmatch(r'PMID\d+',str(row.get('paper_id',''))) or not contains_any_term(row.get('context',''),aliases): continue
                    if len(row['context'])>12000: continue
                    buckets[stratum(row['context'])].append(row)
        for values in buckets.values():
            values.sort(key=lambda r:hashlib.sha256(r['paper_id'].encode()).hexdigest())
        count=0
        while count<per_query and any(buckets.values()):
            for hint,values in buckets.items():
                while values:
                    row=values.pop(0)
                    if row['paper_id'] in seen: continue
                    seen.add(row['paper_id'])
                    context_hash=hashlib.sha256(row['context'].encode()).hexdigest()
                    selected.append(row)
                    cases.append({'case_id':f'{gene}-{row["paper_id"]}','paper_id':row['paper_id'],
                        'gene':gene,'disease':disease,'split':split_for(row['paper_id']),
                        'source_url':f'https://pubmed.ncbi.nlm.nih.gov/{row["paper_id"][4:]}/',
                        'title':row.get('title',''),'context':row['context'],'context_sha256':context_hash,
                        'sampling_hint':hint,'gold_status':'unreviewed','gold_relevant':None,
                        'gold_relations':None,'reviewer':'','annotation_notes':''})
                    count+=1
                    break
                if count==per_query: break
        query_counts[gene+' / '+disease]=count
    if not selected: raise ValueError('No matching real PubMed abstracts found')
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    write_jsonl(output/'chunks.jsonl',selected)
    write_jsonl(output/'annotations.jsonl',cases)
    CorpusStore.build(output/'corpus.sqlite',selected)
    report={'schema_version':'pilot-v1','prepared_at_utc':datetime.now(timezone.utc).isoformat(),
        'paper_count':len(selected),'query_counts':query_counts,
        'source':'Existing PubMed abstract cache; not revalidated against current PubMed metadata',
        'selection':'Deterministic round-robin lexical hints, distinct papers; hints are not expert labels',
        'split_policy':'Paper-level deterministic hash; never split sentences from one paper across groups',
        'gold_status':'No expert-confirmed labels yet; medical accuracy cannot be calculated',
        'snapshot':CorpusStore(output/'corpus.sqlite').fingerprint(),
        'cases':[{k:c[k] for k in ['case_id','paper_id','title','source_url','split','context_sha256','sampling_hint']} for c in cases]}
    write_json(output/'manifest.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='从已有缓存准备私有的真实摘要试点数据')
    parser.add_argument('--cache-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--per-query',type=int,default=10)
    args=parser.parse_args()
    report=prepare(args.cache_root,args.output,args.per_query)
    print(json.dumps({'paper_count':report['paper_count'],'query_counts':report['query_counts'],
                      'gold_status':report['gold_status']},ensure_ascii=False))
