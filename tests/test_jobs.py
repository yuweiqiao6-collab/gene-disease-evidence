import io
import secrets
import threading
from pathlib import Path
import pytest
import fitz
from conftest import wait_for
from jobs import JobManager, Query
from web_settings import WebSettings
from uploads import Upload


def pdf_bytes(text='IL6 expression was elevated in rheumatoid arthritis patients.'):
    doc=fitz.open()
    page=doc.new_page()
    page.insert_text((50,60),text)
    data=doc.tobytes()
    doc.close()
    return data


def credentials(monkeypatch):
    import config
    monkeypatch.setattr(config,'get_llm_settings',lambda:dict(api_key='test',model='test'))


def live_manager(tmp_path,monkeypatch):
    credentials(monkeypatch)
    return JobManager(WebSettings(runtime_dir=tmp_path,enable_live=True,access_password='a-strong-test-password'))


def test_concurrent_sessions_cannot_read_or_download_each_other(manager,owner):
    other=secrets.token_hex(32)
    first=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis'))
    second=manager.submit(other,Query(('ABCC8',),'type 2 diabetes'))
    assert first!=second
    assert wait_for(manager,owner,first)['counts']['evidence']==3
    assert wait_for(manager,other,second)['counts']['evidence']==2
    with pytest.raises(PermissionError):
        manager.snapshot(owner,second)
    with pytest.raises(PermissionError):
        manager.download(other,first,'evidence.xlsx')
    with pytest.raises(PermissionError):
        manager.download(owner,first,'../'+second+'/evidence.json')
    assert b'ABCC8' not in manager.download(owner,first,'evidence.json')


def test_same_query_gets_separate_artifacts(manager,owner):
    other=secrets.token_hex(32)
    a=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis'))
    b=manager.submit(other,Query(('IL6',),'rheumatoid arthritis'))
    wait_for(manager,owner,a)
    wait_for(manager,other,b)
    assert a!=b and (manager.root/a/'evidence.xlsx').exists() and (manager.root/b/'evidence.xlsx').exists()


def test_no_match_is_distinct_from_failure(manager,owner):
    job=manager.submit(owner,Query(('UNKNOWN',),'rheumatoid arthritis'))
    assert wait_for(manager,owner,job)['state']=='no_candidates'


def test_demo_cannot_enable_live_or_upload(manager,owner):
    with pytest.raises(PermissionError):
        manager.submit(owner,Query(('IL6',),'arthritis',source='pubmed',consent=True),authorized=True)
    with pytest.raises(ValueError):
        manager.submit(owner,Query(('IL6',),'arthritis'),(Upload('paper.pdf',b'%PDF-x'),))


@pytest.mark.parametrize('query',[
    Query(('IL6',)*4,'arthritis'), Query(('IL6',),'arthritis',top_k=13),
    Query(('IL6','ABCC8','TP53'),'arthritis',top_k=12),Query(('../escape',),'arthritis'),
    Query(('IL6',),' '),Query(('IL6','TP53'),'arthritis',gene_aliases=('IL-6',))])
def test_query_budgets_are_enforced(manager,owner,query):
    with pytest.raises(ValueError):
        manager.submit(owner,query)


def test_one_active_task_per_session_and_global_queue(tmp_path,monkeypatch,owner):
    manager=JobManager(WebSettings(runtime_dir=tmp_path,max_workers=1,max_queue=0))
    release=threading.Event()
    original=manager._run
    def delayed(job,uploads):
        release.wait(3)
        original(job,uploads)
    monkeypatch.setattr(manager,'_run',delayed)
    try:
        job=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis'))
        with pytest.raises(ValueError,match='等待'):
            manager.submit(owner,Query(('IL6',),'rheumatoid arthritis'))
        with pytest.raises(ValueError,match='队列'):
            manager.submit(secrets.token_hex(32),Query(('IL6',),'rheumatoid arthritis'))
    finally:
        release.set()
        manager.close()


def test_uploaded_filename_cannot_escape_task_and_source_is_trusted(tmp_path,monkeypatch,owner):
    import jobs
    manager=live_manager(tmp_path,monkeypatch)
    class Client:
        failure_count=0
        request_count=1
        def __init__(self,**kwargs): pass
        def extract_batch(self,g,d,chunks,**kwargs):
            from schemas import ExtractionResult
            return [(c,[ExtractionResult(evidence_sentence=c['sentence'],paper_id='FORGED',
                source_location='FORGED',expression_direction='increased',disease_effect='biomarker_only')]) for c in chunks]
    monkeypatch.setattr(jobs,'LLMClient',Client)
    try:
        job=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='upload',consent=True),
            (Upload('../../escape.pdf',pdf_bytes()),),authorized=True)
        snapshot=wait_for(manager,owner,job)
        assert snapshot['state']=='completed'
        assert snapshot['rows'][0]['paper_id'].startswith('UPLOAD-')
        assert snapshot['rows'][0]['source_location']=='escape.pdf，第 1 页'
        assert not (tmp_path/'escape.pdf').exists()
        assert len(list((manager.root/job/'uploads').glob('UPLOAD-*.pdf')))==1
    finally:
        manager.close()


def test_live_model_failure_is_not_zero_results_or_fake_success(tmp_path,monkeypatch,owner):
    import jobs
    manager=live_manager(tmp_path,monkeypatch)
    class FailedClient:
        failure_count=1
        request_count=2
        def __init__(self,**kwargs):
            assert kwargs['allow_fallback'] is False
        def extract_batch(self,g,d,chunks,**kwargs):
            return [(c,[]) for c in chunks]
    monkeypatch.setattr(jobs,'LLMClient',FailedClient)
    try:
        job=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='upload',consent=True),
            (Upload('paper.pdf',pdf_bytes()),),authorized=True)
        snapshot=wait_for(manager,owner,job)
        assert snapshot['state']=='failed' and snapshot['rows']==[]
        assert snapshot['counts']['failed_requests']==1
        assert b'"complete": false' in manager.download(owner,job,'status.json')
    finally:
        manager.close()


def test_pubmed_failure_does_not_mean_no_evidence(tmp_path,monkeypatch,owner):
    import jobs
    import pubmed_gateway
    manager=live_manager(tmp_path,monkeypatch)
    monkeypatch.setattr(jobs,'LLMClient',lambda **kwargs:type('Client',(),{'failure_count':0,'request_count':0})())
    def fail(*args,**kwargs):
        raise pubmed_gateway.PubMedError('PubMed 暂时无法访问，请稍后重试。')
    monkeypatch.setattr(pubmed_gateway,'fetch',fail)
    try:
        job=manager.submit(owner,Query(('IL6',),'arthritis',source='pubmed',consent=True),authorized=True)
        assert wait_for(manager,owner,job)['state']=='failed'
    finally:
        manager.close()


def test_upload_size_magic_and_page_limits(tmp_path,monkeypatch,owner):
    from uploads import validate_uploads, save_and_parse
    settings=WebSettings(runtime_dir=tmp_path,max_upload_mb=1,max_pages=1)
    with pytest.raises(ValueError): validate_uploads((Upload('fake.pdf',b'not pdf'),),settings)
    with pytest.raises(ValueError): validate_uploads((Upload('large.pdf',b'%PDF-'+b'x'*1024*1024),),settings)
    doc=fitz.open()
    doc.new_page()
    doc.new_page()
    upload=Upload('pages.pdf',doc.tobytes())
    doc.close()
    with pytest.raises(ValueError,match='1 页'):
        save_and_parse((upload,),tmp_path/'uploads',settings)


def test_live_requires_consent_and_rate_limits(tmp_path,monkeypatch,owner):
    manager=live_manager(tmp_path,monkeypatch)
    try:
        with pytest.raises(ValueError,match='确认'):
            manager.submit(owner,Query(('IL6',),'arthritis',source='pubmed'),authorized=True)
        for _ in range(3):
            task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis'))
            wait_for(manager,owner,task)
        with pytest.raises(ValueError,match='频繁'):
            manager.submit(owner,Query(('IL6',),'arthritis'))
    finally:
        manager.close()


def test_chinese_access_password_and_attempt_limit(tmp_path,owner):
    password='测试访问口令足够长并包含中文'
    manager=JobManager(WebSettings(runtime_dir=tmp_path,enable_live=True,access_password=password))
    try:
        assert manager.authenticate(owner,password)
        for _ in range(5):
            assert not manager.authenticate(owner,'wrong')
        assert not manager.authenticate(owner,password)
    finally:
        manager.close()
