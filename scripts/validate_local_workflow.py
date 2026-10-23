"""Freeze up to three local excerpts, then execute once only after explicit consent."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from config import get_llm_settings
from corpus_store import CorpusStore
from exports import write_exports
from io_utils import write_json
from jobs import ALIASES
from llm_client import LLMClient
from prompts import PROMPT_VERSION,SYSTEM_PROMPT,USER_PROMPT_TEMPLATE
from validators import ground_result

TARGETS=[('IL6','rheumatoid arthritis'),('ABCC8','type 2 diabetes'),('CD36','type 2 diabetes')]

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def prompt_digest():return hashlib.sha256((SYSTEM_PROMPT+USER_PROMPT_TEMPLATE).encode()).hexdigest()

def prepare(corpus,output):
    if output.exists():raise ValueError('计划目录已存在，请使用新目录，保留原计划。')
    settings=get_llm_settings();store=CorpusStore(corpus);samples=[]
    snapshot=store.fingerprint()
    for gene,disease in TARGETS:
        chunks=store.search(gene,disease,1,ALIASES.get(gene,[]),skip_reference_like=True)
        if not chunks:raise ValueError('某个目标没有可冻结的候选，未生成不完整计划。')
        chunk=chunks[0]
        if chunk.get('synthetic') or chunk.get('source_type')!='local_pdf':raise ValueError('只允许真实本地PDF文字。')
        original=chunk['context'];chunk['context']=original[:3000]
        samples.append({'gene':gene,'disease':disease,'chunk':chunk,'chunk_sha256':digest(chunk),
                        'truncated_input':chunk['context']!=original})
    if snapshot!=store.fingerprint():raise ValueError('文献库在准备期间发生变化。')
    plan={'execute':False,'provider_host':urlsplit(str(settings['base_url'])).hostname,'model':settings['model'],
          'credentials_present':bool(settings['api_key']),'prompt_version':PROMPT_VERSION,'prompt_sha256':prompt_digest(),
          'corpus_snapshot':snapshot,'max_requests':len(samples),'automatic_retries':0,
          'skip_reference_like':True,
          'max_input_characters_per_excerpt':3000,'max_output_tokens_per_request':2048,
          'max_total_output_tokens':len(samples)*2048,'samples':samples,
          'scope':'Up to three retrieval-selected local full-text excerpts; no human answers; not a medical accuracy benchmark'}
    output.mkdir(parents=True,mode=0o700)
    write_json(output/'plan.json',plan)
    (output/'plan.sha256').write_text(digest(plan)+'\n')
    from openpyxl import Workbook
    from openpyxl.styles import Alignment
    book=Workbook();sheet=book.active;sheet.title='调用前核对'
    sheet.append(['基因','疾病英文','论文记录','标题','文件名','页码','拟发送原文','输入字符数'])
    for sample in samples:
        c=sample['chunk']
        sheet.append([sample['gene'],sample['disease'],c['paper_id'],c.get('title',''),c.get('file_name',''),c.get('page'),c['context'],len(c['context'])])
    for row in sheet:
        for cell in row:
            cell.alignment=Alignment(wrap_text=True,vertical='top')
            if isinstance(cell.value,str):cell.data_type='s'
    sheet.column_dimensions['G'].width=110;sheet.column_dimensions['D'].width=55;sheet.freeze_panes='G2'
    book.save(output/'三段全文调用前核对.xlsx')
    return {k:v for k,v in plan.items() if k!='samples'}

def execute(plan_path,corpus,output,*,allow_external=False):
    if not allow_external:raise ValueError('执行需要 --allow-external，并先确认原文外发与模型费用。')
    plan=json.loads(plan_path.read_text());samples=plan.get('samples',[])
    if (plan_path.parent/'plan.sha256').read_text().strip()!=digest(plan):raise ValueError('计划内容已改变。')
    if not 1<=len(samples)<=3 or plan.get('automatic_retries')!=0:raise ValueError('计划超过3次或包含重试。')
    if plan.get('max_output_tokens_per_request')!=2048 or plan.get('prompt_sha256')!=prompt_digest():raise ValueError('提示或输出上限与计划不同。')
    if CorpusStore(corpus).fingerprint()!=plan['corpus_snapshot']:raise ValueError('原文索引与计划不一致，请重新准备。')
    settings=get_llm_settings()
    if not settings['api_key'] or not settings['model']:raise ValueError('请在私有配置中设置模型。')
    if settings['model']!=plan['model'] or urlsplit(str(settings['base_url'])).hostname!=plan['provider_host']:
        raise ValueError('模型或服务商与核对计划不一致。')
    for sample in samples:
        c=sample['chunk']
        if digest(c)!=sample['chunk_sha256'] or not 1<=len(c['context'])<=3000 or c.get('synthetic') or c.get('source_type')!='local_pdf':
            raise ValueError('拟发送片段与计划不一致。')
    if output.exists():raise ValueError('执行目录已存在，不能自动重复可能已计费的调用。')
    output.mkdir(parents=True,mode=0o700)
    with (output/'execution.started').open('x') as marker:marker.write('Explicit external-text and billing consent required\n')
    client=LLMClient(cache_path=output/'model_cache.sqlite',allow_fallback=False,max_output_tokens=2048,
                     thinking=False if plan['provider_host']=='api.deepseek.com' else None)
    rows=[];results=[]
    def checkpoint(stage,index=None):
        write_json(output/'progress.json',{'stage':stage,'pending_sample_index':index,'completed_samples':len(results),
            'requests_recorded':client.request_count,'maximum_planned_requests':len(samples),
            'usage_recorded':dict(client.usage),'accounting_complete_for_started_requests':stage!='before_request',
            'automatic_retries':0,'note':'Interrupted requests may have been billed; never automatically repeat.'})
    checkpoint('started')
    for index,sample in enumerate(samples):
        checkpoint('before_request',index);before=client.failure_count
        raw=client.extract(sample['gene'],sample['disease'],sample['chunk'])
        checkpoint('response_received',index);accepted=[];rejected=0
        for relation in raw:
            row=ground_result(relation.to_dict(),sample['chunk'],sample['gene'],sample['disease'],ALIASES.get(sample['gene'],[]))
            if row:
                row.update(synthetic=False,source_context=sample['chunk']['context']);accepted.append(row)
            else:rejected+=1
        rows.extend(accepted)
        results.append({'sample_index':index,'gene':sample['gene'],'disease':sample['disease'],
            'chunk_sha256':sample['chunk_sha256'],'complete':client.failure_count==before and rejected==0,
            'rejected_quotes':rejected,'relations':accepted,'truncated_input':sample.get('truncated_input',False)})
        write_json(output/'results.json',results);write_exports(output,rows);checkpoint('sample_saved')
    report={'execute':True,'actual_requests':client.request_count,'failed_requests':client.failure_count,
        'usage':client.usage,'failures':client.failures,'response_models':sorted(client.response_models),
        'automatic_retries':0,'plan_sha256':digest(plan),'scientific_accuracy_measured':False,
        'note':'Checks retrieval-selected excerpt extraction and traceability; not full-paper coverage or independent accuracy.'}
    write_json(output/'report.json',report);checkpoint('complete');return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='先离线冻结三段全文，再经确认执行单次有限验证；不自动重试')
    parser.add_argument('action',choices=['prepare','execute'])
    parser.add_argument('--corpus',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--allow-external',action='store_true')
    args=parser.parse_args()
    if args.action=='prepare':report=prepare(args.corpus,args.output)
    elif args.plan is None:parser.error('执行需要 --plan 指定已核对计划')
    else:report=execute(args.plan,args.corpus,args.output,allow_external=args.allow_external)
    print(json.dumps(report,ensure_ascii=False,indent=2))
