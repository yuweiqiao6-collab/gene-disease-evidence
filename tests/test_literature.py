import copy
import hashlib
import json
import secrets
from pathlib import Path
import pytest
from conftest import wait_for
import literature_search as search
import pubmed_gateway
import fulltext_gateway as fulltext
from literature_store import LiteratureStore,from_local_chunks,paper_chunks
from jobs import JobManager,Query
from web_settings import WebSettings


def article(pmid,abstract=True):
    return f'''<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>IL6 <i>expression</i> study {pmid}</ArticleTitle>
    <Abstract>{'<AbstractText Label="RESULTS">IL6 expression was elevated in rheumatoid arthritis patients.</AbstractText>' if abstract else ''}</Abstract>
    </Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/TEST{pmid}</ArticleId><ArticleId IdType="pmc">PMC{pmid}</ArticleId></ArticleIdList></PubmedData></PubmedArticle>'''


def paper(pmid='123'):
    return search.parse_articles('<PubmedArticleSet>'+article(pmid)+'</PubmedArticleSet>',{pmid})[pmid]


def result(papers=None):
    papers=[paper()] if papers is None else papers
    return dict(papers=papers,query='IL6 AND arthritis',query_url='https://pubmed.ncbi.nlm.nih.gov/?term=IL6',
        complete=True,matched_count=200,retrieved_count=len(papers),truncated_by_limit=True,
        cache_hit=False,issues=[],status='completed',requested_limit=40)


def xml(pmid='123',pmcid='PMC123',doi='10.1234/test123'):
    return f'''<article><front><article-meta><article-id pub-id-type="pmid">{pmid}</article-id>
    <article-id pub-id-type="pmc">{pmcid}</article-id><article-id pub-id-type="doi">{doi}</article-id>
    <permissions><license>CC BY 4.0</license></permissions></article-meta></front>
    <body><sec><title>Results</title><p>IL6 expression was elevated in rheumatoid arthritis patients.</p>
    <sec><title>Subgroup</title><p>A separate result was not significant.</p></sec>
    <table-wrap><p>Unparsed table values</p></table-wrap><fig><p>Unparsed figure</p></fig></sec></body>
    <back><ref-list><ref><p>Reference is not own evidence.</p></ref></ref-list></back></article>'''.encode()


def test_discovery_broad_query_pagination_and_empty_abstract(tmp_path,monkeypatch):
    calls=[]
    def request(endpoint,params):
        calls.append((endpoint,params))
        if endpoint=='esearch.fcgi': return json.dumps(dict(esearchresult=dict(count='90',idlist=[str(i) for i in range(25)])))
        return '<PubmedArticleSet>'+''.join(article(i,abstract=i!='0') for i in params['id'].split(','))+'</PubmedArticleSet>'
    monkeypatch.setattr(pubmed_gateway,'_request',request)
    output=search.search('IL6','rheumatoid arthritis',tmp_path,max_results=25)
    assert output['complete'] and output['truncated_by_limit'] and len(output['papers'])==25
    assert [p['requested'] for p in output['pages']]==[20,5]
    assert output['papers'][0]['abstract_status']=='no_abstract'
    assert 'expression OR' not in calls[0][1]['term']
    assert output['papers'][1]['doi']=='10.1234/test1'
    assert output['papers'][1]['title']=='IL6 expression study 1'
    assert search.search('IL6','rheumatoid arthritis',tmp_path,max_results=25)['cache_hit']
    assert len(calls)==3


@pytest.mark.parametrize('problem',['missing','wrong_identity','network'])
def test_partial_pages_are_explicit_and_not_cached(tmp_path,monkeypatch,problem):
    def request(endpoint,params):
        if endpoint=='esearch.fcgi': return json.dumps(dict(esearchresult=dict(count='2',idlist=['123','456'])))
        if problem=='network': raise pubmed_gateway.PubMedError('offline')
        return '<PubmedArticleSet>'+article('123' if problem=='missing' else '999')+'</PubmedArticleSet>'
    monkeypatch.setattr(pubmed_gateway,'_request',request)
    output=search.search('IL6','arthritis',tmp_path)
    assert not output['complete'] and output['status']=='partial'
    assert output['issues'] and not list(tmp_path.glob('*.json'))
    assert all(p['pmid']!='999' for p in output['papers'])


def test_corrupt_cache_refetched_and_focused_separate(tmp_path,monkeypatch):
    calls=[]
    def request(endpoint,params):
        calls.append((endpoint,params))
        if endpoint=='esearch.fcgi': return json.dumps(dict(esearchresult=dict(count='1',idlist=['123'])))
        return '<PubmedArticleSet>'+article('123')+'</PubmedArticleSet>'
    monkeypatch.setattr(pubmed_gateway,'_request',request)
    search.search('IL6','arthritis',tmp_path)
    cache=next(tmp_path.glob('*.json'));payload=json.loads(cache.read_text());payload['papers'][0]['abstract']='tampered'
    cache.write_text(json.dumps(payload))
    assert not search.search('IL6','arthritis',tmp_path)['cache_hit']
    search.search('IL6','arthritis',tmp_path,focused=True)
    assert 'expression OR' in calls[-2][1]['term']
    assert len(calls)==6


def test_store_deduplicates_identifiers_preserves_sources_and_isolates(tmp_path,owner):
    store=LiteratureStore(tmp_path/'library.sqlite')
    local=from_local_chunks([dict(chunk_id='c',paper_id='PMID123',source_type='pubmed_abstract',
        context='Older abstract.',title='Older title')])[0]
    first=store.upsert(owner,local)
    merged=store.upsert(owner,paper())
    assert first['document_id']==merged['document_id'] and len(store.list(owner))==1
    assert len(merged['sources'])==2 and merged['abstract']==paper()['abstract']
    doi_only=copy.deepcopy(paper());doi_only.pop('pmid');doi_only.pop('pmcid');doi_only['paper_id']='doi-record'
    assert store.upsert(owner,doi_only)['document_id']==first['document_id']
    conflict=paper('456');conflict['doi']=paper()['doi']
    with pytest.raises(ValueError,match='冲突'): store.upsert(owner,conflict)
    other=secrets.token_hex(32)
    assert store.list(other)==[]
    with pytest.raises(PermissionError): store.get(other,first['document_id'])
    with pytest.raises(PermissionError): store.delete(other,first['document_id'])
    assert owner.encode() not in store.path.read_bytes()
    assert LiteratureStore(store.path).list(owner)[0]['document_id']==first['document_id']


def test_fulltext_paragraphs_sections_license_and_identity():
    parsed=fulltext.parse_fulltext(xml(),paper(),'PMC123')
    assert parsed['fulltext_status']=='available' and len(parsed['fulltext_chunks'])==2
    assert parsed['fulltext_chunks'][1]['section']=='Results / Subgroup'
    assert parsed['fulltext_chunks'][0]['content_level']=='fulltext'
    assert parsed['fulltext_license']=='CC BY 4.0'
    assert 'table-wrap' in parsed['fulltext_omitted_elements']
    assert parsed['fulltext_sha256']==hashlib.sha256(xml()).hexdigest()


@pytest.mark.parametrize('data',[xml(pmid='999'),xml(pmcid='PMC999'),xml(doi='10.1234/wrong'),
    b'<article><body><p>No article identity</p></body></article>',b'<!DOCTYPE article [<!ENTITY x "bomb">]><article/>'])
def test_fulltext_identity_missing_or_mismatch_is_rejected(data):
    with pytest.raises(fulltext.FulltextError): fulltext.parse_fulltext(data,paper(),'PMC123')


def test_fulltext_lookup_unavailable_is_not_network_failure(tmp_path,monkeypatch):
    calls=[]
    def request(path,params=None):
        calls.append(path)
        return json.dumps(dict(resultList=dict(result=[dict(id='123',source='MED',pmcid='PMC123',doi='10.1234/test123',isOpenAccess='N')]))).encode()
    monkeypatch.setattr(fulltext,'_request',request)
    assert fulltext.fetch(paper(),tmp_path)['fulltext_status']=='unavailable'
    assert calls==['search']


def test_fulltext_gateway_forbids_arbitrary_urls():
    with pytest.raises(ValueError): fulltext_gateway_bad_url()


def fulltext_gateway_bad_url():
    return fulltext._request('https://127.0.0.1/private')


def test_fulltext_cached_content_rechecked_and_failure_preserves_previous(tmp_path,monkeypatch,owner):
    calls=[]
    def request(path,params=None):
        calls.append(path)
        if path=='search': return json.dumps(dict(resultList=dict(result=[dict(id='123',source='MED',pmcid='PMC123',doi='10.1234/test123',isOpenAccess='Y')]))).encode()
        return xml()
    monkeypatch.setattr(fulltext,'_request',request)
    full=fulltext.fetch(paper(),tmp_path/'cache')
    fulltext_again=fulltext.fetch(paper(),tmp_path/'cache')
    assert calls==['search','PMC123/fullTextXML','search']
    assert fulltext_again['fulltext_sha256']==full['fulltext_sha256']
    store=LiteratureStore(tmp_path/'library.sqlite');saved=store.upsert(owner,paper());did=saved['document_id']
    store.update_fulltext(owner,did,full)
    store.update_fulltext(owner,did,dict(fulltext_status='failed',fulltext_message='offline'))
    assert store.get(owner,did)['fulltext_status']=='available'
    assert len(paper_chunks(store.get(owner,did)))==3
    assert len(paper_chunks(store.get(owner,did),'abstract'))==1
    assert store.get(owner,did)['fulltext_last_attempt']['fulltext_status']=='failed'


def test_search_and_fulltext_jobs_need_no_model_and_survive_restart(tmp_path,owner,monkeypatch):
    import jobs
    def forbidden(**kwargs): raise AssertionError('Model must not be constructed')
    monkeypatch.setattr(jobs,'LLMClient',forbidden)
    monkeypatch.setattr(search,'search',lambda *args,**kwargs:result())
    monkeypatch.setattr(fulltext,'fetch',lambda p,*args,**kwargs:fulltext.parse_fulltext(xml(),p,'PMC123'))
    settings=WebSettings(runtime_dir=tmp_path,enable_live=False)
    manager=JobManager(settings)
    task=manager.submit(owner,Query(('IL6',),'arthritis',source='pubmed',action='search'))
    snapshot=wait_for(manager,owner,task)
    assert snapshot['state']=='completed' and snapshot['counts']['model_calls']==0
    did=snapshot['documents'][0]['document_id']
    with pytest.raises(PermissionError): manager.submit(secrets.token_hex(32),Query(('IL6',),'arthritis',source='library',action='fulltext',document_ids=(did,)))
    fulltask=manager.submit(owner,Query(('IL6',),'arthritis',source='library',action='fulltext',document_ids=(did,)))
    assert wait_for(manager,owner,fulltask)['counts']['fulltexts']==1
    with pytest.raises(PermissionError): manager.submit(owner,Query(('IL6',),'arthritis',source='library',document_ids=(did,),consent=True),authorized=True)
    manager.close();manager=JobManager(settings)
    try:
        assert manager.snapshot(owner,fulltask)['documents'][0]['fulltext_status']=='available'
        assert len(json.loads(manager.download(owner,task,'literature.json'))['documents'])==1
        assert manager.literature.get(owner,did)['fulltext_chunks']
        with pytest.raises(PermissionError): manager.download(secrets.token_hex(32),task,'literature.json')
    finally: manager.close()


def test_grounded_evidence_uses_chunk_scope_and_location():
    from validators import ground_result
    c=fulltext.parse_fulltext(xml(),paper(),'PMC123')['fulltext_chunks'][0]
    row=ground_result(dict(evidence_sentence=c['context'],paper_id='FORGED',source_url='https://evil.test',content_level='abstract'),c,'IL6','rheumatoid arthritis')
    assert row['content_level']=='fulltext' and row['paper_id']=='PMID123'
    assert row['source_url']=='https://europepmc.org/articles/PMC123' and row['paragraph']==1


def test_model_extraction_from_selected_library_is_explicit_and_grounded(tmp_path,owner,monkeypatch):
    import jobs,config
    from schemas import ExtractionResult
    monkeypatch.setattr(config,'get_llm_settings',lambda:dict(api_key='test',model='test'))
    calls=[]
    class Client:
        failure_count=0;request_count=1
        def __init__(self,**kwargs):
            assert kwargs['allow_fallback'] is False
        def extract_batch(self,gene,disease,chunks,**kwargs):
            calls.extend(chunks)
            return [(c,[ExtractionResult(evidence_sentence=c['context'],paper_id='FORGED',expression_direction='increased',species='human')]) for c in chunks]
    monkeypatch.setattr(jobs,'LLMClient',Client)
    manager=JobManager(WebSettings(runtime_dir=tmp_path,enable_live=True,access_password='long-test-password'))
    try:
        p=manager.literature.upsert(owner,paper());did=p['document_id']
        manager.literature.update_fulltext(owner,did,fulltext.parse_fulltext(xml(),paper(),'PMC123'))
        q=Query(('IL6',),'rheumatoid arthritis',source='library',document_ids=(did,),consent=True)
        with pytest.raises(PermissionError): manager.submit(owner,q)
        task=manager.submit(owner,q,authorized=True);s=wait_for(manager,owner,task)
        assert s['state']=='completed' and calls
        assert any(c['content_level']=='fulltext' for c in calls)
        body_row=next(r for r in s['rows'] if r['content_level']=='fulltext')
        assert body_row['fulltext_license']=='CC BY 4.0'
        assert s['rows'][0]['paper_id']=='PMID123'
        assert s['provenance']['selected_documents']==[did]
        assert b'fulltext_sha256' in manager.download(owner,task,'evidence.csv')
    finally: manager.close()


def test_local_online_merge_and_network_failure_is_not_zero_evidence(tmp_path,owner,monkeypatch):
    from corpus_store import CorpusStore
    corpus=tmp_path/'corpus.sqlite'
    CorpusStore.build(corpus,[dict(chunk_id='c',paper_id='PMID123',title='Local paper',source_type='pubmed_abstract',
        context='IL6 expression in rheumatoid arthritis.')])
    manager=JobManager(WebSettings(runtime_dir=tmp_path/'runtime',corpus_db=corpus))
    monkeypatch.setattr(search,'search',lambda *args,**kwargs:result())
    try:
        task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='pubmed',action='search'))
        snapshot=wait_for(manager,owner,task)
        assert len(snapshot['documents'])==1 and len(snapshot['documents'][0]['sources'])==2
        def fail(*args,**kwargs): raise pubmed_gateway.PubMedError('network failure')
        monkeypatch.setattr(search,'search',fail)
        task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='pubmed',action='search'))
        snapshot=wait_for(manager,owner,task)
        assert snapshot['state']=='partial' and snapshot['documents'] and snapshot['issues']
        assert snapshot['provenance']['pubmed']['status']=='failed'
    finally: manager.close()


def test_old_history_without_new_fields_still_restores(tmp_path,owner):
    import sqlite3
    manager=JobManager(WebSettings(runtime_dir=tmp_path))
    task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis'));wait_for(manager,owner,task)
    manager.close()
    with sqlite3.connect(tmp_path/'history.sqlite') as conn:
        saved=json.loads(conn.execute('SELECT payload FROM jobs WHERE id=?',(task,)).fetchone()[0])
        saved.pop('documents')
        for key in ['action','document_ids','max_papers','focused','include_local','refresh','evidence_scope']: saved['query'].pop(key)
        conn.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(saved),task))
    manager=JobManager(WebSettings(runtime_dir=tmp_path))
    try:
        snapshot=manager.snapshot(owner,task)
        assert snapshot['action']=='extract' and snapshot['documents']==[] and snapshot['rows']
    finally: manager.close()


def test_literature_disable_switch_also_blocks_legacy_online_extraction(tmp_path,owner):
    manager=JobManager(WebSettings(runtime_dir=tmp_path,enable_literature=False,enable_live=True,access_password='long-test-password'))
    try:
        for action in ['search','extract']:
            with pytest.raises(PermissionError,match='关闭在线'):
                manager.submit(owner,Query(('IL6',),'arthritis',source='pubmed',action=action,consent=True),authorized=True)
    finally: manager.close()
