"""Bounded sequential scanning with durable, per-request checkpoints."""
import hashlib
import json
import time
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit
from baseline import PROFILE,assess,bind_primary_source
from history_store import evidence_id,owner_digest
from prompts import prompt_settings
from scan_sources import candidates_for_paper,paper_revision,ScanSourceError
from scan_state import document_summary,counts as coverage_counts
from validators import ground_result


def signature():
    from config import get_llm_settings
    settings=get_llm_settings()
    version,system,template=prompt_settings(PROFILE)
    payload={k:settings.get(k) for k in ['model','base_url','thinking','max_output_tokens']}
    payload['prompt']=hashlib.sha256((version+system+template).encode()).hexdigest()
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()


def new_record(paper):
    return {'document_id':paper['document_id'],'paper_id':paper['paper_id'],'title':paper['title'],
        'source_revision':paper_revision(paper),'fulltext_status':paper.get('fulltext_status','not_requested'),
        'status':'pending','prepared':False,'messages':[],'candidates':[]}


def deduplicate(rows):
    unique={}
    fields=['paper_id','gene_or_target','disease','evidence_sentence','expression_direction','measurement_type',
            'population_context','comparison_type','comparison_sentence','disease_group','normal_group','significance','species','tissue_or_cell']
    for row in rows:unique.setdefault(tuple(row.get(k,'') for k in fields),row)
    return list(unique.values())


def refresh(manager,job,*,state=None,message=None):
    records=manager.scans.records(job.id)
    rows=deduplicate([r for d in records for c in d['candidates'] if c['state']=='succeeded' for r in c['rows']])
    documents=[document_summary(d) for d in records]
    manifest=manager.scans.manifest(job.id)
    attempts=[a for d in records for c in d['candidates'] for a in c.get('attempts',[])]
    usage={k:sum(a.get('usage',{}).get(k,0) for a in attempts) for k in ('prompt_tokens','completion_tokens','total_tokens')}
    provenance={**job.provenance,'scan':{'version':1,'batch_size':5,'coverage':coverage_counts(documents),
        'runs':manifest['runs'],'configuration_signature':manifest['configuration_signature'],
        'request_slots_used':sum(r['slots_used'] for r in manifest['runs']),
        'unknown_request_outcomes':sum(a['state'] in {'in_flight','interrupted'} for a in attempts)},'model_usage':usage,
        'response_models':sorted({m for a in attempts for m in a.get('response_models_seen',[])}),
        'selected_document_coverage':[{'document_id':d['document_id'],'title':d['title'],
            'candidate_count':d['attempted_chunks']} for d in documents]}
    counts={'documents':len(documents),'candidates':sum(d['candidate_count'] for d in documents),
        'evidence':len(rows),'model_calls':sum(a.get('model_calls') or 0 for a in attempts),
        'failed_requests':sum(a.get('request_failed',False) for a in attempts),
        'rejected_quotes':sum(c.get('rejected_quotes',0) for d in records for c in d['candidates']),
        'baseline_eligible':sum(r['baseline_status']=='eligible' for r in rows),
        'baseline_pending':sum(r['baseline_status']=='needs_scope_review' for r in rows),
        'baseline_out_of_scope':sum(r['baseline_status']=='out_of_scope' for r in rows),
        'support_flagged':sum(r.get('support_status')=='flagged' for r in rows)}
    changes=dict(rows=rows,documents=documents,counts=counts,provenance=provenance,
        progress=sum(d['status'] not in {'pending','running'} for d in records)/max(1,len(records)))
    if state:changes.update(state=state,finished=time.time())
    if message:changes['message']=message
    manager._update(job,**changes)


def finish_document(record):
    states={c['state'] for c in record['candidates']}
    if 'interrupted' in states:record['status']='interrupted'
    elif 'failed' in states:record['status']='failed'
    elif 'pending' in states:record['status']='pending'
    else:
        summary=document_summary(record)
        if summary['scope']=='abstract' and not summary['eligible']:
            record['status']='needs_fulltext'
            record['messages'].append('摘要未给出通过检查的比较，不能据此排除正文证据。')
        elif not summary['eligible']:
            record['status']='needs_context'
            record['messages'].append('当前片段没有通过范围检查的候选；可按新一轮上限补查其他命中，不能判定全文无证据。')
        else:record['status']='processed'


def run(manager,job):
    from jobs import ALIASES,LLMClient
    import fulltext_gateway
    query=job.query;pause=manager.scan_pauses[job.id]
    directory=manager.scans.directory(job.id)
    if not (directory/'manifest.json').exists():
        records=[new_record(manager.literature.get(job.owner,d)) for d in query.document_ids]
        manager.scans.create(job.id,records,{'configuration_signature':signature(),
            'task_profile':PROFILE,'created':time.time(),'owner_digest':owner_digest(job.owner)})
    manifest=manager.scans.manifest(job.id)
    if manifest['configuration_signature']!=signature():raise ScanSourceError('模型设置或任务规则已变更，请保留原记录并新建扫描。')
    version,system,template=prompt_settings(PROFILE)
    run_record={'number':len(manifest['runs'])+1,'started':time.time(),'paper_budget':query.scan_paper_budget,
        'request_budget':query.scan_request_budget,'papers_started':0,'slots_used':0,'finished':None,'stop_reason':''}
    manifest['runs'].append(run_record);manager.scans.save_manifest(job.id,manifest)
    client=LLMClient(cache_path=directory/'model_cache.sqlite',allow_fallback=False,task_profile=PROFILE)
    manager._update(job,provenance={**job.provenance,'application_version':(Path(__file__).parents[1]/'VERSION').read_text().strip(),
        'task_profile':PROFILE,'prompt_version':version,'prompt_sha256':hashlib.sha256((system+template).encode()).hexdigest(),
        'model_name':getattr(client,'model',None),'provider_host':urlsplit(str(getattr(client,'base_url',''))).hostname,
        'input_character_limit':9000,'max_output_tokens':2048,'max_attempts_per_chunk':1,
        'request_limit':query.scan_request_budget,'retrieval_method':'per_paper_sqlite_fts5',
        'evidence_scope':query.evidence_scope,'fetch_open_fulltext':query.scan_fetch_fulltext,'per_paper_candidate_limit':query.top_k})
    refresh(manager,job)
    ga=list(query.gene_aliases) or ALIASES.get(query.genes[0].upper(),[]);da=list(query.disease_aliases)
    stop=''
    for index,document_id in enumerate(manifest['document_ids']):
        record=manager.scans.load(job.id,document_id)
        if record['status']!='pending':continue
        if pause.is_set():stop='user_pause';break
        if run_record['papers_started']>=query.scan_paper_budget:stop='paper_budget';break
        if run_record['slots_used']>=query.scan_request_budget:stop='request_budget';break
        run_record['papers_started']+=1;manager.scans.save_manifest(job.id,manifest)
        manager._update(job,message=f'第{index//5+1}批，处理候选{index+1}/{len(manifest["document_ids"])}；本轮最多{query.scan_request_budget}次请求')
        record['status']='running';manager.scans.save(job.id,record)
        if not record['prepared']:
            try:
                paper=manager.literature.get(job.owner,document_id)
                if paper_revision(paper)!=record['source_revision']:
                    record['status']='source_changed';record['messages'].append('候选文字或来源已改变，未自动发送，请重新检索并新建扫描。')
                    manager.scans.save(job.id,record);refresh(manager,job);continue
                has_body=paper.get('local_text_available') or paper.get('fulltext_status')=='available'
                if query.scan_fetch_fulltext and query.evidence_scope=='auto' and not has_body:
                    try:
                        result=fulltext_gateway.fetch(paper,manager.settings.runtime_dir/'fulltext-cache')
                    except fulltext_gateway.FulltextError as exc:
                        result={'fulltext_status':'failed','fulltext_message':str(exc)}
                    manager.literature.update_fulltext(job.owner,document_id,result)
                    paper=manager.literature.get(job.owner,document_id)
                    record['source_revision']=paper_revision(paper);record['fulltext_status']=paper.get('fulltext_status')
                    has_body=paper.get('local_text_available') or paper.get('fulltext_status')=='available'
                    record['messages'].append(paper.get('fulltext_message','已尝试获取开放正文。'))
                temporary=job.directory/document_id;temporary.mkdir(parents=True,exist_ok=True,mode=0o700)
                excluded={part['chunk_id'] for c in record['candidates'] if c['state']=='succeeded'
                    for part in (c['chunk'].get('context_segments') or [c['chunk']])}
                candidates=candidates_for_paper(paper,query,manager.settings,temporary,gene_aliases=ga,exclude_chunk_ids=excluded)
                if any(c.get('synthetic') or c.get('source_type')=='synthetic_demo' for c in candidates):raise ScanSourceError('教学材料不能进入真实扫描。')
                retained={c['chunk']['chunk_id']:c for c in record['candidates']}
                for c in candidates:
                    retained.setdefault(c['chunk_id'],{'chunk':c,'state':'pending','rows':[],'rejected_quotes':0,'attempts':[]})
                record['candidates']=list(retained.values())
                record['prepared']=True
                if not candidates and record['candidates'] and all(c['state']=='succeeded' for c in record['candidates']):
                    record['status']='needs_review'
                    record['messages'].append('当前检索范围没有新增命中，不重发成功候选；仍需人工核对，不代表全文没有证据。')
                if not record['candidates']:
                    record['status']='no_candidates' if has_body else 'no_text'
                    record['messages'].append('本次没有可发送的候选文字，保留为未解决；不能判定无关系。')
            except (ScanSourceError,ValueError,PermissionError,OSError,RuntimeError,sqlite3.Error) as exc:
                record['status']='failed'
                record['messages'].append(str(exc) if isinstance(exc,ScanSourceError) else '读取候选文字失败；没有判为无证据。')
            manager.scans.save(job.id,record)
            if record['status']!='running':refresh(manager,job);continue
        processed_in_document=0
        for candidate in record['candidates']:
            if candidate['state']!='pending':continue
            if processed_in_document>=query.top_k:break
            if pause.is_set():stop='user_pause';break
            if run_record['slots_used']>=query.scan_request_budget:stop='request_budget';break
            chunk=candidate['chunk'];candidate['state']='in_flight'
            attempt={'state':'in_flight','started':time.time(),'model_calls':None,'usage':{}}
            candidate['attempts'].append(attempt)
            # Save before the network call; uncertainty after a crash must never
            # trigger automatic replay, even if the provider already charged.
            manager.scans.save(job.id,record)
            run_record['slots_used']+=1;manager.scans.save_manifest(job.id,manifest)
            processed_in_document+=1
            before_calls=getattr(client,'request_count',0);before_failures=getattr(client,'failure_count',0)
            before_usage=dict(getattr(client,'usage',{}));failed=False;request_failed=False
            try:
                returned=client.extract_batch(query.genes[0],query.disease,[chunk],max_workers=1,retry=0)
                if len(returned)!=1 or returned[0][0].get('chunk_id')!=chunk['chunk_id']:raise ValueError('Missing chunk response')
                annotations=returned[0][1]
                failed=getattr(client,'failure_count',0)>before_failures
                request_failed=failed
                grounded=[];rejected=0
                if not failed:
                    for value in annotations:
                        raw=value.to_dict() if hasattr(value,'to_dict') else value
                        bound=bind_primary_source(raw,chunk)
                        row=ground_result(raw,bound,query.genes[0],query.disease,ga,da) if bound else None
                        if row is None:rejected+=1;continue
                        row.update(synthetic=False,task_profile=PROFILE,source_context=chunk['context'],
                            context_segments=chunk.get('context_segments',[]),target_gene_aliases=ga,target_disease_aliases=da)
                        row=assess(row);row['evidence_id']=evidence_id(row);grounded.append(row)
                    candidate.update(rows=deduplicate(grounded),rejected_quotes=rejected)
                    # Entirely invalid quotations cannot masquerade as no evidence.
                    if rejected and not grounded:failed=True
            except Exception:
                failed=True;request_failed=True
            candidate['state']='failed' if failed else 'succeeded'
            attempt.update(state=candidate['state'],finished=time.time(),request_failed=request_failed,
                response_models_seen=sorted(getattr(client,'response_models',[])),
                model_calls=max(0,getattr(client,'request_count',0)-before_calls),
                usage={k:max(0,v-before_usage.get(k,0)) for k,v in getattr(client,'usage',{}).items()})
            manager.scans.save(job.id,record)
        finish_document(record);manager.scans.save(job.id,record);refresh(manager,job)
        if stop:break
    records=manager.scans.records(job.id)
    pending=any(d['status']=='pending' for d in records)
    unresolved=any(d['status'] not in {'pending','processed'} for d in records)
    unresolved=unresolved or any(c.get('rejected_quotes') for d in records for c in d['candidates'])
    state='paused' if pending else 'partial' if unresolved else 'completed'
    run_record.update(finished=time.time(),stop_reason=stop or ('unresolved_items' if unresolved else 'candidate_list_finished'))
    manager.scans.save_manifest(job.id,manifest)
    message=('已暂停并保存进度；继续时跳过已成功处理的片段。' if state=='paused' else
        '候选队列已处理，仍有摘要不足、待补查、缺少文字或失败项需要核对。' if state=='partial' else
        '本次候选队列处理结束；仅检查有限文字，不能代表全部文献或医学结论。')
    refresh(manager,job,state=state,message=message)
