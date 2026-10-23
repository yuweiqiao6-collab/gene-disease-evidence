import io
import json
import secrets
from pathlib import Path
import fitz
import pytest
from conftest import wait_for
from corpus_store import CorpusStore
from jobs import JobManager,Query
from schemas import ExtractionResult
from web_settings import WebSettings
from test_research_scripts import script


def chunk(pid,n,text='IL6 expression was elevated in rheumatoid arthritis patients.'):
    return dict(paper_id=pid,chunk_id=f'{pid}-{n}',file_name=pid+'.pdf',title=pid,
        page=n,context=text,sentence=text,source_type='local_pdf',content_level='fulltext',source_location=f'{pid}.pdf / 第{n}页')


def test_paper_ranking_returns_distinct_documents_before_snippet_limit(tmp_path):
    path=tmp_path/'corpus.sqlite'
    CorpusStore.build(path,[chunk('A',i) for i in range(20)]+[chunk('B',1)])
    papers=CorpusStore(path).search_papers('IL6','rheumatoid arthritis',2)
    assert {p['paper_id'] for p in papers}=={'A','B'}
    assert all(1<=len(p['local_chunks'])<=3 and p['local_text_available'] for p in papers)
    assert CorpusStore(path).fingerprint()['paper_count']=='2'
    scoped=CorpusStore(path).search('IL6','rheumatoid arthritis',10,paper_ids=['B'])
    assert [c['paper_id'] for c in scoped]==['B']


def test_local_search_works_without_online_switch_or_model(tmp_path,monkeypatch,owner):
    import jobs,literature_search
    corpus=tmp_path/'corpus.sqlite';CorpusStore.build(corpus,[chunk('A',1)])
    def forbidden(*a,**k):raise AssertionError('Local search cannot use a model or network')
    monkeypatch.setattr(jobs,'LLMClient',forbidden);monkeypatch.setattr(literature_search,'search',forbidden)
    manager=JobManager(WebSettings(runtime_dir=tmp_path/'runtime',corpus_db=corpus,enable_literature=False))
    try:
        task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='corpus',action='search'))
        snapshot=wait_for(manager,owner,task)
        assert snapshot['state']=='completed' and snapshot['counts']['model_calls']==0
        assert snapshot['provenance']['local_documents']==1 and 'pubmed' not in snapshot['provenance']
        with pytest.raises(PermissionError):
            manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='corpus',action='search',include_pubmed=True))
    finally:manager.close()


def test_hybrid_search_merges_only_known_identity_and_retains_local_text(tmp_path,monkeypatch,owner):
    from test_literature import result,paper
    import literature_search
    corpus=tmp_path/'corpus.sqlite';CorpusStore.build(corpus,[chunk('PMID123',1)])
    monkeypatch.setattr(literature_search,'search',lambda *a,**k:result([paper()]))
    manager=JobManager(WebSettings(runtime_dir=tmp_path/'runtime',corpus_db=corpus))
    try:
        task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='corpus',action='search',include_pubmed=True))
        snapshot=wait_for(manager,owner,task)
        assert snapshot['state']=='completed' and len(snapshot['documents'])==1
        p=manager.literature.get(owner,snapshot['documents'][0]['document_id'])
        assert p['abstract'] and p['local_chunks'] and p['local_corpus_paper_ids']==['PMID123']
        assert {s['provider'] for s in p['sources']}=={'local_corpus','pubmed'}
        assert p['local_text_available']
    finally:manager.close()


def test_selected_local_papers_retrieve_beyond_previews_and_export_page_sources(tmp_path,monkeypatch,owner):
    import jobs,config
    from openpyxl import load_workbook
    corpus=tmp_path/'corpus.sqlite';CorpusStore.build(corpus,[chunk('A',i) for i in range(1,5)]+[chunk('UNSELECTED',1)])
    monkeypatch.setattr(config,'get_llm_settings',lambda:dict(api_key='unit',model='unit'))
    received=[]
    class Client:
        failure_count=0;request_count=1
        def __init__(self,**kwargs):assert kwargs['allow_fallback'] is False
        def extract_batch(self,gene,disease,chunks,**kwargs):
            received.extend(chunks)
            return [(c,[ExtractionResult(evidence_sentence=c['context'],expression_direction='increased',species='human')]) for c in chunks]
    monkeypatch.setattr(jobs,'LLMClient',Client)
    manager=JobManager(WebSettings(runtime_dir=tmp_path/'runtime',corpus_db=corpus,enable_live=True,access_password='unit-long-password'))
    try:
        p=next(p for p in CorpusStore(corpus).search_papers('IL6','rheumatoid arthritis') if p['paper_id']=='A')
        assert len(p['local_chunks'])==3
        did=manager.literature.upsert(owner,p)['document_id']
        query=Query(('IL6',),'rheumatoid arthritis',source='library',document_ids=(did,),consent=True,top_k=4)
        task=manager.submit(owner,query,authorized=True);snapshot=wait_for(manager,owner,task)
        assert snapshot['state']=='completed' and len(received)==4
        assert {c['paper_id'] for c in received}=={'A'}
        assert all(r['content_level']=='fulltext' and r['page'] in {1,2,3,4} and r['file_name']=='A.pdf' for r in snapshot['rows'])
        workbook=load_workbook(io.BytesIO(manager.download(owner,task,'evidence.xlsx')))
        columns=[c.value for c in workbook.active[1]]
        assert all(name in columns for name in ['file_name','page','chunk_id'])
        with pytest.raises(PermissionError):manager.download(secrets.token_hex(32),task,'evidence.xlsx')
    finally:manager.close()


def test_changed_corpus_rejects_stale_candidates_before_constructing_model(tmp_path,monkeypatch,owner):
    import config,jobs
    corpus=tmp_path/'corpus.sqlite';CorpusStore.build(corpus,[chunk('A',1)])
    p=CorpusStore(corpus).search_papers('IL6','rheumatoid arthritis')[0]
    CorpusStore.build(corpus,[chunk('A',2)])
    monkeypatch.setattr(config,'get_llm_settings',lambda:dict(api_key='unit',model='unit'))
    def forbidden(**kwargs):raise AssertionError('Stale source must fail before model construction')
    monkeypatch.setattr(jobs,'LLMClient',forbidden)
    manager=JobManager(WebSettings(runtime_dir=tmp_path/'runtime',corpus_db=corpus,enable_live=True,access_password='unit-long-password'))
    try:
        did=manager.literature.upsert(owner,p)['document_id']
        task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='library',document_ids=(did,),consent=True),authorized=True)
        snapshot=wait_for(manager,owner,task)
        assert snapshot['state']=='failed' and '全文库已更换' in snapshot['message']
    finally:manager.close()


def test_page_import_counts_empty_files_and_retains_source_locations(tmp_path):
    pdfs=tmp_path/'pdfs';pdfs.mkdir()
    for name in ('readable.pdf','empty.pdf'):
        with fitz.open() as pdf:pdf.new_page();pdf.save(pdfs/name)
    rows=[dict(paper_id='readable',file_name='readable.pdf',title='Test',page=1,text='IL6 expression in rheumatoid arthritis. '*100),
          dict(paper_id='empty',file_name='empty.pdf',title='Empty',page=1,text='')]
    pages=tmp_path/'pages.jsonl';pages.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    original=pages.read_bytes();module=script('import_local_library')
    result=module.import_library(pages,pdfs,tmp_path/'corpus.sqlite',tmp_path/'report.json')
    assert result['searchable_paper_ids']==1 and result['pdf_files_without_readable_text']==1
    assert result['unreadable_files']==['empty.pdf'] and pages.read_bytes()==original
    paper=CorpusStore(tmp_path/'corpus.sqlite').search_papers('IL6','rheumatoid arthritis')[0]
    assert all(c['page']==1 and c['content_level']=='fulltext' for c in paper['local_chunks'])
    with pytest.raises(ValueError):module.import_library(pages,pdfs,tmp_path/'corpus.sqlite',tmp_path/'other.json')


def test_page_import_rejects_ambiguous_filenames_without_publishing_database(tmp_path):
    pdfs=tmp_path/'pdfs';(pdfs/'one').mkdir(parents=True);(pdfs/'two').mkdir()
    (pdfs/'one/a.pdf').write_bytes(b'fixture');(pdfs/'two/a.pdf').write_bytes(b'fixture')
    pages=tmp_path/'pages.jsonl';pages.write_text('')
    with pytest.raises(ValueError,match='文件名重复'):
        script('import_local_library').import_library(pages,pdfs,tmp_path/'corpus.sqlite',tmp_path/'report.json')
    assert not (tmp_path/'corpus.sqlite').exists()


def test_prepare_own_pdfs_then_import_without_legacy_project(tmp_path):
    pdfs=tmp_path/'pdfs';pdfs.mkdir()
    with fitz.open() as document:
        page=document.new_page();page.insert_text((50,60),'IL6 expression was elevated in rheumatoid arthritis patients.')
        document.save(pdfs/'own.pdf')
    pages=tmp_path/'pages.jsonl'
    prepared=script('prepare_local_pages').prepare(pdfs,pages,tmp_path/'prepare.json')
    assert prepared['parsed_files']==1 and not prepared['failed_files']
    imported=script('import_local_library').import_library(pages,pdfs,tmp_path/'corpus.sqlite',tmp_path/'import.json')
    assert imported['searchable_paper_ids']==1
    assert CorpusStore(tmp_path/'corpus.sqlite').search_papers('IL6','rheumatoid arthritis')
