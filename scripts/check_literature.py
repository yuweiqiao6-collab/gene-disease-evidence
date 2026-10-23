"""Bounded public-interface verification; never imports or invokes a model client."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import literature_search,fulltext_gateway,pubmed_gateway
from io_utils import write_json


def run(output,execute=False,search_report=None):
    plan={'execute':execute,'query':{'gene':'IL6','disease':'rheumatoid arthritis'},
        'max_papers':3,'max_fulltexts':1,'max_fulltext_metadata_lookups':3,'model_calls':0,
        'providers':['PubMed','Europe PMC']}
    if not execute: return plan
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    try:
        result=json.loads(search_report.read_text()) if search_report else literature_search.search('IL6','rheumatoid arthritis',output/'search-cache',max_results=3)
        if len(result['papers'])>3: raise ValueError('验证最多使用三篇论文。')
        write_json(output/'search.json',result)
        plan['pubmed']={k:result[k] for k in ['complete','matched_count','retrieved_count','truncated_by_limit']}
        papers=result['papers']
        for paper in sorted(papers,key=lambda p:not bool(p.get('pmcid'))):
            full=fulltext_gateway.fetch(paper,output/'fulltext-cache')
            write_json(output/'fulltext.json',full)
            plan.setdefault('fulltext_attempts',[]).append({'paper_id':paper['paper_id'],'status':full['fulltext_status']})
            plan['fulltext']={k:v for k,v in full.items() if k!='fulltext_chunks'}
            plan['fulltext_paragraph_chunks']=len(full.get('fulltext_chunks',[]))
            if full['fulltext_status']=='available': break
        plan['pubmed_verified']=result['complete'] and bool(papers)
        plan['fulltext_verified']=plan.get('fulltext',{}).get('fulltext_status')=='available'
    except (pubmed_gateway.PubMedError,fulltext_gateway.FulltextError) as exc:
        plan.update(error=str(exc),error_type=type(exc).__name__,cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None)
    write_json(output/'report.json',plan)
    return plan


if __name__=='__main__':
    p=argparse.ArgumentParser(description='公开文献接口小批检查；默认只显示计划，不调用模型')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--search-report',type=Path,help='复用已保存的最多三篇检索记录，避免再次搜索')
    args=p.parse_args()
    report=run(args.output,args.execute,args.search_report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if args.execute and not (report.get('pubmed_verified') and report.get('fulltext_verified')): raise SystemExit(1)
