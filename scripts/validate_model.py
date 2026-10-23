"""Default is a no-network plan. Execution needs two explicit opt-in flags."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from config import get_llm_settings
from io_utils import write_json, write_jsonl
from llm_client import LLMClient
from validators import ground_result
from prompts import PROMPT_VERSION


def plan(pilot_dir,max_requests=3,*,max_output_tokens=1024):
    if not 1<=max_requests<=3: raise ValueError('This validation allows only 1..3 requests')
    if not 1<=max_output_tokens<=2048: raise ValueError('Output token limit must be 1..2048')
    rows=[json.loads(line) for line in (pilot_dir/'annotations.jsonl').read_text().splitlines() if line.strip()]
    chosen=[]
    for gene in ['IL6','ABCC8','CD36']:
        case=next((c for c in rows if c['gene']==gene and c['split']=='development'),None)
        if case: chosen.append(case)
    chosen=chosen[:max_requests]
    if any(c.get('context_sha256')!=hashlib.sha256(c['context'].encode()).hexdigest() for c in chosen):
        raise ValueError('Pilot context differs from its recorded snapshot')
    settings=get_llm_settings()
    report={'execute':False,'provider_host':urlsplit(str(settings['base_url'])).hostname,
        'model':settings['model'],'credentials_present':bool(settings['api_key']),
        'max_requests':len(chosen),'max_input_characters_per_abstract':3000,
        'max_output_tokens_per_request':max_output_tokens,
        'max_total_output_tokens':len(chosen)*max_output_tokens,
        'automatic_retries':0,'prompt_version':PROMPT_VERSION,
        'thinking_mode':'disabled' if urlsplit(str(settings['base_url'])).hostname=='api.deepseek.com' else 'provider_default',
        'samples':[{**{key:c[key] for key in ['case_id','paper_id','title','source_url','context_sha256']},
                    'sent_context_sha256':hashlib.sha256(c['context'][:3000].encode()).hexdigest(),
                    'sent_context_characters':len(c['context'][:3000]),
                    'truncated_input':len(c['context'])>3000} for c in chosen]}
    return chosen,report


def run(pilot_dir,output,max_requests=3,*,execute=False,allow_external=False,max_output_tokens=1024):
    chosen,report=plan(pilot_dir,max_requests,max_output_tokens=max_output_tokens)
    if not execute:
        output.mkdir(parents=True,exist_ok=True)
        write_json(output/'plan.json',report)
        return report
    if not allow_external: raise ValueError('Execution requires --allow-external; public abstracts will be sent and billing may apply')
    if not report['credentials_present'] or not report['model']: raise ValueError('Configure the model locally first')
    if not chosen: raise ValueError('No development cases available')
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Refuse repeating an execution, including one that crashed after consuming credits.
    with (output/'execution.started').open('x') as marker: marker.write('Explicitly authorized model validation\n')
    client=LLMClient(cache_path=output/'model_cache.sqlite',allow_fallback=False,max_output_tokens=max_output_tokens,
        thinking=False if report['thinking_mode']=='disabled' else None)
    started=time.monotonic()
    predictions=[]
    def checkpoint(stage,pending_case=None):
        # Persist known accounting before/after each request. An interrupted
        # in-flight request remains uncertain and must never be replayed.
        write_json(output/'progress.json',{
            'stage':stage,'pending_case_id':pending_case,
            'completed_case_ids':[p['case_id'] for p in predictions],
            'actual_requests_recorded':client.request_count,
            'maximum_possible_requests':min(len(chosen),client.request_count+int(stage=='before_request')),
            'failed_requests_recorded':client.failure_count,'usage_recorded':dict(client.usage),
            'response_models':sorted(getattr(client,'response_models',[])),
            'accounting_complete_for_started_requests':stage!='before_request',
            'execution_completed':stage=='complete','automatic_retries':0,
            'note':'Progress is a checkpoint, not permission to resume or a scientific accuracy result'})
    checkpoint('started')
    for c in chosen:
        context=c['context'][:3000]
        chunk={'chunk_id':c['case_id'],'paper_id':c['paper_id'],'title':c['title'],
            'source_location':c['paper_id'],'source_type':'pubmed_abstract','context':context}
        before=client.failure_count
        checkpoint('before_request',c['case_id'])
        results=client.extract(c['gene'],c['disease'],chunk)
        checkpoint('response_received',c['case_id'])
        grounded=[]
        rejected=0
        for result in results:
            row=ground_result(result.to_dict(),chunk,c['gene'],c['disease'])
            if row: grounded.append(row)
            else: rejected+=1
        predictions.append({'case_id':c['case_id'],'paper_id':c['paper_id'],
            'context_sha256':c['context_sha256'],'sent_context_sha256':hashlib.sha256(context.encode()).hexdigest(),
            'complete':client.failure_count==before and rejected==0,'relations':grounded,
            'retrieved':None,'rejected_quotes':rejected,'truncated_input':context!=c['context']})
        write_jsonl(output/'predictions.jsonl',predictions)
        checkpoint('case_saved')
    report.update(execute=True,elapsed_seconds=round(time.monotonic()-started,3),actual_requests=client.request_count,
        failed_requests=client.failure_count,usage=client.usage,failures=client.failures,
        response_models=sorted(getattr(client,'response_models',[])),
        note='This checks connectivity/extraction only; it is not a medical accuracy benchmark and does not measure retrieval recall')
    write_json(output/'report.json',report)
    checkpoint('complete')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='最多三篇公开摘要的模型验证；默认仅生成计划、不联网')
    parser.add_argument('--pilot-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--max-requests',type=int,default=3)
    parser.add_argument('--max-output-tokens',type=int,default=1024,help='每次输出词元上限，1..2048；默认 1024')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--allow-external',action='store_true')
    args=parser.parse_args()
    try:
        report=run(args.pilot_dir,args.output,args.max_requests,execute=args.execute,allow_external=args.allow_external,
                   max_output_tokens=args.max_output_tokens)
    except (ValueError,FileExistsError) as exc:
        raise SystemExit(str(exc))
    print(json.dumps(report,ensure_ascii=False,indent=2))
