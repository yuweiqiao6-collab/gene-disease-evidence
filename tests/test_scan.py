import hashlib
import json
import secrets
import shutil
import threading
import time
from dataclasses import replace
import pytest
from baseline import PROFILE
from conftest import wait_for
from corpus_store import CorpusStore
from jobs import JobManager,Query
from schemas import normalize_result
from web_settings import WebSettings
from test_baseline import TEXT,evidence
from test_literature import paper
from test_local_library import chunk


@pytest.fixture
def scan_setup(tmp_path,monkeypatch):
    import config,jobs
    monkeypatch.setattr(config,'get_llm_settings',lambda:dict(api_key='unit',model='unit',base_url='https://unit.invalid'))
    calls=[]
    class Client:
        def __init__(self,**kwargs):
            assert kwargs['allow_fallback'] is False and kwargs['task_profile']==PROFILE
            self.request_count=0;self.failure_count=0;self.usage={};self.response_models=set()
        def extract_batch(self,g,d,chunks,**kwargs):
            assert kwargs=={'max_workers':1,'retry':0} or kwargs=={'max_workers':2,'retry':0}
            result=[]
            for c in chunks:
                calls.append(c);self.request_count+=1
                self.response_models.add('unit-response-model')
                result.append((c,[normalize_result(evidence())] if TEXT in c['context'] else []))
            return result
    monkeypatch.setattr(jobs,'LLMClient',Client)
    settings=WebSettings(runtime_dir=tmp_path/'runtime',enable_live=True,access_password='unit-long-password',
        session_jobs_per_minute=100,global_live_jobs_per_hour=100)
    manager=JobManager(settings)
    yield manager,calls,Client,settings
    manager.close()


def add_papers(manager,owner,count=7,abstract=TEXT):
    ids=[]
    for number in range(count):
        p=paper(str(1000+number));p.update(abstract=abstract,abstract_sha256=hashlib.sha256(abstract.encode()).hexdigest())
        ids.append(manager.literature.upsert(owner,p)['document_id'])
    return tuple(ids)


def query(ids,**changes):
    return replace(Query(('IL6',),'rheumatoid arthritis',source='library',action='scan',document_ids=ids,
        task_profile=PROFILE,consent=True,top_k=1,scan_paper_budget=20,scan_request_budget=40),**changes)


def test_scans_more_than_five_papers_with_independent_coverage(scan_setup,owner):
    service,calls,_,_=scan_setup;ids=add_papers(service,owner,12)
    task=service.submit(owner,query(ids),authorized=True);result=wait_for(service,owner,task)
    assert result['state']=='completed' and len(calls)==12
    assert {c['paper_id'] for c in calls}=={'PMID'+str(1000+n) for n in range(12)}
    assert len(result['rows'])==12 and result['provenance']['scan']['coverage']['processed']==12
    assert result['provenance']['response_models']==['unit-response-model']
    assert all(d['successful_chunks']==1 and d['scope']=='abstract' for d in result['documents'])
    report=json.loads(service.download(owner,task,'coverage.json'))
    assert len(report['documents'])==12 and report['scan']['batch_size']==5


def test_budget_pause_survives_restart_and_temporary_cleanup(scan_setup,owner):
    service,calls,_,settings=scan_setup;ids=add_papers(service,owner,5)
    task=service.submit(owner,query(ids,scan_paper_budget=2),authorized=True)
    result=wait_for(service,owner,task);assert result['state']=='paused' and len(calls)==2
    row=result['rows'][0]
    service.review(owner,task,row['evidence_id'],'accepted',reviewer='human')
    shutil.rmtree(service.root/task);service.close()
    restored=JobManager(settings)
    try:
        restored.resume_scan(owner,task,paper_budget=2,request_budget=40,consent=True,authorized=True)
        assert wait_for(restored,owner,task)['state']=='paused' and len(calls)==4
        restored.resume_scan(owner,task,paper_budget=2,request_budget=40,consent=True,authorized=True)
        result=wait_for(restored,owner,task)
        assert result['state']=='completed' and len(calls)==5
        assert len({c['chunk_id'] for c in calls})==5
        assert result['rows'][0]['review_status']=='accepted'
        assert len(result['provenance']['scan']['runs'])==3
    finally:restored.close()


def test_request_budget_can_pause_inside_one_paper(scan_setup,tmp_path,owner):
    service,calls,_,settings=scan_setup
    corpus=tmp_path/'corpus.sqlite';CorpusStore.build(corpus,[chunk('A',n,TEXT) for n in range(1,4)])
    service.settings=replace(settings,corpus_db=corpus)
    p=CorpusStore(corpus).search_papers('IL6','rheumatoid arthritis')[0]
    did=service.literature.upsert(owner,p)['document_id']
    task=service.submit(owner,query((did,),top_k=3,scan_request_budget=1),authorized=True)
    result=wait_for(service,owner,task)
    assert result['state']=='paused' and len(calls)==1
    assert result['documents'][0]['remaining_chunks']==2
    service.resume_scan(owner,task,paper_budget=1,request_budget=2,consent=True,authorized=True)
    result=wait_for(service,owner,task)
    assert result['state']=='completed' and len(calls)==3
    assert len({c['chunk_id'] for c in calls})==3
    assert result['provenance']['scan']['request_slots_used']==3


def test_insufficient_body_can_continue_to_another_source_chunk(scan_setup,tmp_path,monkeypatch,owner):
    import jobs
    service,calls,Client,settings=scan_setup
    corpus=tmp_path/'corpus.sqlite';CorpusStore.build(corpus,[chunk('A',1,TEXT),chunk('A',8,TEXT)])
    service.settings=replace(settings,corpus_db=corpus)
    did=service.literature.upsert(owner,CorpusStore(corpus).search_papers('IL6','rheumatoid arthritis')[0])['document_id']
    class EmptyFirst(Client):
        def extract_batch(self,g,d,chunks,**kwargs):
            if not calls:
                calls.extend(chunks);self.request_count+=1;return [(chunks[0],[])]
            return super().extract_batch(g,d,chunks,**kwargs)
    monkeypatch.setattr(jobs,'LLMClient',EmptyFirst)
    task=service.submit(owner,query((did,)),authorized=True)
    first=wait_for(service,owner,task)
    assert first['state']=='partial' and first['documents'][0]['status']=='needs_context'
    service.resume_scan(owner,task,consent=True,authorized=True,retry_unresolved=True)
    result=wait_for(service,owner,task)
    assert result['state']=='completed' and len(calls)==2 and len({c['chunk_id'] for c in calls})==2


def test_user_pause_finishes_current_call_and_does_not_start_next(scan_setup,monkeypatch,owner):
    import jobs
    service,calls,Client,_=scan_setup;entered=threading.Event();release=threading.Event()
    class Slow(Client):
        def extract_batch(self,*args,**kwargs):
            entered.set();release.wait(4)
            return super().extract_batch(*args,**kwargs)
    monkeypatch.setattr(jobs,'LLMClient',Slow);ids=add_papers(service,owner,3)
    task=service.submit(owner,query(ids),authorized=True)
    try:
        assert entered.wait(3)
        service.pause_scan(owner,task);release.set()
        assert wait_for(service,owner,task)['state']=='paused' and len(calls)==1
    finally:release.set()
    service.resume_scan(owner,task,consent=True,authorized=True)
    assert wait_for(service,owner,task)['state']=='completed' and len(calls)==3


def test_failed_call_is_not_negative_and_requires_explicit_retry(scan_setup,monkeypatch,owner):
    import jobs
    service,calls,Client,_=scan_setup;failed=[]
    class FailOnce(Client):
        def extract_batch(self,g,d,chunks,**kwargs):
            if not failed:
                failed.append(True);self.failure_count+=1;self.request_count+=1
                return [(chunks[0],[])]
            return super().extract_batch(g,d,chunks,**kwargs)
    monkeypatch.setattr(jobs,'LLMClient',FailOnce)
    task=service.submit(owner,query(add_papers(service,owner,3)),authorized=True)
    result=wait_for(service,owner,task)
    assert result['state']=='partial' and len(calls)==2 and result['counts']['failed_requests']==1
    with pytest.raises(ValueError,match='没有未处理'):service.resume_scan(owner,task,consent=True,authorized=True)
    service.resume_scan(owner,task,consent=True,authorized=True,retry_unresolved=True)
    result=wait_for(service,owner,task)
    assert result['state']=='completed' and len(calls)==3 and result['counts']['model_calls']==4
    assert result['counts']['failed_requests']==1


def test_abstract_insufficiency_keeps_body_followup_and_never_replays_success(scan_setup,owner):
    service,calls,_,_=scan_setup
    ids=add_papers(service,owner,1,abstract='This IL6 rheumatoid arthritis abstract omits expression comparison results.')
    task=service.submit(owner,query(ids),authorized=True);result=wait_for(service,owner,task)
    assert result['state']=='partial' and result['documents'][0]['status']=='needs_fulltext' and len(calls)==1
    body={**chunk('PMID1000',1,TEXT),'source_type':'europepmc_fulltext','content_level':'fulltext','pmcid':'PMC1000'}
    service.literature.update_fulltext(owner,ids[0],{'fulltext_status':'available','fulltext_sha256':'unit-sha','fulltext_chunks':[body]})
    service.resume_scan(owner,task,consent=True,authorized=True,retry_unresolved=True)
    result=wait_for(service,owner,task)
    assert result['state']=='completed' and len(calls)==2
    assert calls[0]['source_type']=='pubmed_abstract' and calls[1]['source_type']=='europepmc_fulltext'
    assert result['documents'][0]['scope']=='mixed' and result['counts']['baseline_eligible']==1


def test_abstract_only_scan_can_explicitly_upgrade_to_body(scan_setup,owner):
    service,calls,_,_=scan_setup
    ids=add_papers(service,owner,1,abstract='This abstract does not provide the requested comparison.')
    task=service.submit(owner,query(ids,evidence_scope='abstract'),authorized=True)
    assert wait_for(service,owner,task)['documents'][0]['status']=='needs_fulltext'
    service.literature.update_fulltext(owner,ids[0],{'fulltext_status':'available','fulltext_sha256':'new-body',
        'fulltext_chunks':[{**chunk('PMID1000',1,TEXT),'source_type':'europepmc_fulltext','content_level':'fulltext'}]})
    service.resume_scan(owner,task,consent=True,authorized=True,retry_unresolved=True,evidence_scope='auto')
    result=wait_for(service,owner,task)
    assert result['state']=='completed' and len(calls)==2
    assert calls[-1]['content_level']=='fulltext' and result['provenance']['evidence_scope']=='auto'


def test_resume_frequency_budget_survives_process_restart(scan_setup,owner):
    service,_,_,settings=scan_setup
    task=service.submit(owner,query(add_papers(service,owner,4),scan_paper_budget=1),authorized=True)
    wait_for(service,owner,task)
    service.resume_scan(owner,task,paper_budget=1,consent=True,authorized=True)
    wait_for(service,owner,task);service.close()
    restored=JobManager(replace(settings,session_jobs_per_minute=2))
    try:
        with pytest.raises(ValueError,match='频率'):restored.resume_scan(owner,task,consent=True,authorized=True)
    finally:restored.close()


def test_optional_open_body_fetch_precedes_extraction(scan_setup,monkeypatch,owner):
    import fulltext_gateway
    service,calls,_,_=scan_setup;ids=add_papers(service,owner,1,abstract='IL6 and rheumatoid arthritis were discussed.')
    fetched=[]
    def fetch(p,*args,**kwargs):
        fetched.append(p['paper_id'])
        return {'fulltext_status':'available','fulltext_sha256':'unit',
            'fulltext_chunks':[{**chunk(p['paper_id'],1,TEXT),'source_type':'europepmc_fulltext','content_level':'fulltext'}]}
    monkeypatch.setattr(fulltext_gateway,'fetch',fetch)
    task=service.submit(owner,query(ids,scan_fetch_fulltext=True),authorized=True)
    result=wait_for(service,owner,task)
    assert result['state']=='completed' and len(fetched)==1 and len(calls)==1
    assert calls[0]['content_level']=='fulltext'


def test_missing_abstract_does_not_become_a_negative_result(scan_setup,owner):
    service,calls,_,_=scan_setup
    task=service.submit(owner,query(add_papers(service,owner,1,abstract='')),authorized=True)
    result=wait_for(service,owner,task)
    assert result['state']=='partial' and result['documents'][0]['status']=='no_text' and not calls


def test_pending_source_change_is_not_silently_sent(scan_setup,owner):
    service,calls,_,_=scan_setup;ids=add_papers(service,owner,2)
    task=service.submit(owner,query(ids,scan_paper_budget=1),authorized=True)
    assert wait_for(service,owner,task)['state']=='paused'
    p=service.literature.get(owner,ids[1]);p['abstract']='A changed private source.';p['abstract_sha256']='changed'
    service.literature.upsert(owner,p)
    service.resume_scan(owner,task,consent=True,authorized=True)
    result=wait_for(service,owner,task)
    assert result['state']=='partial' and len(calls)==1
    assert result['documents'][1]['status']=='source_changed'


def test_interrupted_request_is_never_automatically_replayed(scan_setup,monkeypatch,owner):
    import jobs
    service,calls,Client,settings=scan_setup;entered=threading.Event();crashed=[]
    class CrashOnce(Client):
        def extract_batch(self,*args,**kwargs):
            if not crashed:
                crashed.append(True);entered.set();raise SystemExit('Simulated abrupt process loss')
            return super().extract_batch(*args,**kwargs)
    monkeypatch.setattr(jobs,'LLMClient',CrashOnce)
    task=service.submit(owner,query(add_papers(service,owner,2)),authorized=True)
    assert entered.wait(3);service.close()
    restored=JobManager(settings)
    try:
        result=restored.snapshot(owner,task)
        assert result['state']=='interrupted' and result['documents'][0]['status']=='interrupted'
        assert result['provenance']['scan']['unknown_request_outcomes']==1 and not calls
        restored.resume_scan(owner,task,consent=True,authorized=True)
        result=wait_for(restored,owner,task)
        assert result['state']=='partial' and len(calls)==1 and calls[0]['paper_id']=='PMID1001'
        restored.resume_scan(owner,task,consent=True,authorized=True,retry_unresolved=True)
        result=wait_for(restored,owner,task)
        assert result['state']=='completed' and len(calls)==2 and result['provenance']['scan']['unknown_request_outcomes']==1
    finally:restored.close()


def test_scan_is_owned_and_resumption_needs_consent_and_unchanged_model(scan_setup,monkeypatch,owner):
    import config
    service,calls,_,_=scan_setup
    task=service.submit(owner,query(add_papers(service,owner,2),scan_paper_budget=1),authorized=True)
    wait_for(service,owner,task);other=secrets.token_hex(32)
    with pytest.raises(PermissionError):service.download(other,task,'coverage.json')
    with pytest.raises(PermissionError):service.pause_scan(other,task)
    with pytest.raises(PermissionError):service.resume_scan(other,task,consent=True,authorized=True)
    with pytest.raises(ValueError,match='确认'):service.resume_scan(owner,task,authorized=True)
    with pytest.raises(PermissionError):service.resume_scan(owner,task,consent=True)
    monkeypatch.setattr(config,'get_llm_settings',lambda:dict(api_key='unit',model='changed',base_url='https://unit.invalid'))
    with pytest.raises(ValueError,match='模型设置'):service.resume_scan(owner,task,consent=True,authorized=True)
    assert len(calls)==1


@pytest.mark.parametrize('changes',[{'scan_request_budget':121},{'scan_paper_budget':51},{'scan_request_budget':True},
    {'model_retries':1},{'top_k':7},{'task_profile':'legacy'}])
def test_scan_request_limits_are_enforced_before_calls(scan_setup,owner,changes):
    service,calls,_,_=scan_setup
    with pytest.raises(ValueError):service.submit(owner,query(add_papers(service,owner,1),**changes),authorized=True)
    assert not calls


def test_scan_checkpoint_cleanup_is_explicit_not_temporary(scan_setup,owner):
    service,_,_,_=scan_setup
    task=service.submit(owner,query(add_papers(service,owner,1)),authorized=True);wait_for(service,owner,task)
    service.jobs[task].finished=time.time()-90000;service.cleanup()
    assert service.scans.directory(task).exists()
    service.snapshot(owner,task);service.delete_history(owner,task)
    assert not service.scans.directory(task).exists()


def test_manual_baseline_also_gives_each_paper_its_own_quota(scan_setup,tmp_path,owner):
    service,calls,_,settings=scan_setup
    database=tmp_path/'corpus.sqlite';CorpusStore.build(database,[chunk('A',n,TEXT) for n in range(1,10)]+[chunk('B',1,TEXT)])
    service.settings=replace(settings,corpus_db=database)
    ids=tuple(service.literature.upsert(owner,p)['document_id'] for p in CorpusStore(database).search_papers('IL6','rheumatoid arthritis'))
    task=service.submit(owner,query(ids,action='extract',top_k=2),authorized=True)
    result=wait_for(service,owner,task)
    assert result['state']=='completed' and {c['paper_id'] for c in calls}=={'A','B'}
    assert {d['candidate_count'] for d in result['provenance']['selected_document_coverage']}=={1,2}
    assert result['provenance']['request_limit']==4
