"""No model/API calls: record bounded SQLite retrieval on a private pilot."""
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from corpus_store import CorpusStore
from io_utils import write_json,write_jsonl
from evaluation import evaluate_retrieval
from jobs import ALIASES


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='小型真实语料的本地检索记录；不会调用模型')
    parser.add_argument('--annotations',type=Path,required=True)
    parser.add_argument('--corpus',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--top-k',type=int,default=6)
    args=parser.parse_args()
    if not 1<=args.top_k<=50: parser.error('top-k must be 1..50')
    if args.output.exists(): parser.error('Output already exists; choose a new experiment directory')
    cases=[json.loads(line) for line in args.annotations.read_text().splitlines() if line.strip()]
    store=CorpusStore(args.corpus)
    queries={}
    for c in cases:
        key=(c['gene'],c['disease'])
        if key not in queries:
            start=time.monotonic()
            results=store.search(*key,args.top_k,ALIASES.get(c['gene'],[]))
            queries[key]={'paper_ids':sorted({r['paper_id'] for r in results}),'elapsed_seconds':time.monotonic()-start}
    predictions=[{'case_id':c['case_id'],'paper_id':c['paper_id'],'context_sha256':c['context_sha256'],
        'complete':True,'retrieved':c['paper_id'] in queries[(c['gene'],c['disease'])]['paper_ids'],
        'extraction_performed':False} for c in cases]
    args.output.mkdir(parents=True,mode=0o700)
    write_jsonl(args.output/'predictions.jsonl',predictions)
    report=evaluate_retrieval(cases,predictions)
    report['queries']=[{'gene':g,'disease':d,**result} for (g,d),result in queries.items()]
    report['corpus_snapshot']=store.fingerprint();report['top_k']=args.top_k
    write_json(args.output/'report.json',report)
    print(json.dumps({k:report[k] for k in ['task','evaluation_ready','metrics','reason'] if k in report},ensure_ascii=False))
