import hashlib
import json
import secrets
import sys
from dataclasses import replace
from pathlib import Path
import pytest
from baseline import PROFILE
from conftest import wait_for
from jobs import JobManager, Query
from original_bridge import OriginalRetrieval
from web_settings import WebSettings
from test_baseline import TEXT, evidence
from schemas import normalize_result


def configuration(tmp_path):
    index=tmp_path/'index.json';index.write_text('{}')
    model=tmp_path/'model';model.mkdir()
    return WebSettings(runtime_dir=tmp_path/'runtime',enable_live=True,access_password='unit-original-password',
        original_python=Path(sys.executable),original_index=index,original_embedding=model,original_reranker=model,
        session_jobs_per_minute=100,global_live_jobs_per_hour=100)


def ranked(payload):
    rows=[dict(chunk_id=f'original-{i}',paper_id=f'P{i}',file_name=f'{i}.pdf',page=1,
        context=TEXT,sentence=TEXT,title='Original fixture',source_type='pdf',source_location=f'{i}.pdf / 1') for i in range(2)]
    return dict(candidates=rows,trace=dict(actual_method='bm25+dense+cross_encoder',final_limit=payload['top_k'],
        gene_aliases=payload.get('gene_aliases',[]),disease_aliases=[],selected_count=2,local_candidates=1,
        online_candidates=1,cache={'reused':2,'encoded':0},pubmed_status='success'))


def test_original_search_does_not_call_llm_and_extract_uses_exact_ranked_chunks(tmp_path,monkeypatch,owner):
    import jobs, config
    settings=configuration(tmp_path)
    monkeypatch.setattr(config,'get_llm_settings',lambda:dict(api_key='unit',model='unit'))
    payloads=[]
    def retrieve(self,payload):payloads.append(payload);return ranked(payload)
    monkeypatch.setattr(OriginalRetrieval,'retrieve',retrieve)
    calls=[]
    class Client:
        failure_count=0;request_count=0;usage={};response_models=set()
        def __init__(self,**kwargs):assert kwargs['allow_fallback'] is False
        def extract_batch(self,g,d,chunks,**kwargs):
            calls.extend(chunks);self.request_count+=len(chunks)
            assert kwargs=={'max_workers':2,'retry':0}
            return [(c,[normalize_result(evidence())]) for c in chunks]
    monkeypatch.setattr(jobs,'LLMClient',Client)
    manager=JobManager(settings)
    try:
        search=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='original',action='search',
            top_k=5,task_profile=PROFILE,include_pubmed=True))
        result=wait_for(manager,owner,search)
        assert result['state']=='completed' and not calls and result['counts']['model_calls']==0
        assert payloads[0]['include_pubmed'] and result['documents'][0]['source_type']=='local_pdf'
        query=Query(('IL6',),'rheumatoid arthritis',source='original',original_search_id=search,top_k=5,task_profile=PROFILE,consent=True)
        with pytest.raises(PermissionError):manager.submit(owner,query)
        with pytest.raises(PermissionError):manager.submit(secrets.token_hex(32),query,authorized=True)
        with pytest.raises(ValueError):manager.submit(owner,replace(query,top_k=15),authorized=True)
        with pytest.raises(ValueError):manager.submit(owner,replace(query,disease='another disease'),authorized=True)
        task=manager.submit(owner,query,authorized=True);output=wait_for(manager,owner,task)
        assert output['state']=='completed'
        assert [c['chunk_id'] for c in calls]==['original-0','original-1']
        assert output['provenance']['request_limit']==2
        assert output['provenance']['retrieval_method']=='original_candidate_hybrid_rerank'
    finally:manager.close()


def test_incomplete_original_retrieval_cannot_become_live_extraction(tmp_path,monkeypatch,owner):
    settings=configuration(tmp_path)
    def retrieve(self,payload):
        result=ranked(payload);result['trace']['actual_method']='bm25+cross_encoder';return result
    monkeypatch.setattr(OriginalRetrieval,'retrieve',retrieve)
    manager=JobManager(settings)
    try:
        task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='original',action='search',task_profile=PROFILE))
        assert wait_for(manager,owner,task)['state']=='partial'
        with pytest.raises(ValueError):manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='original',
            original_search_id=task,task_profile=PROFILE,consent=True),authorized=True)
    finally:manager.close()


def test_original_core_published_copy_matches_canonical_modules():
    root=Path(__file__).parents[1]
    manifest=json.loads((root/'src/original_core_manifest.json').read_text())
    original=root.parent/'rag_gene_disease_extractor/src'
    for name,digest in manifest['files'].items():
        assert hashlib.sha256((root/'src'/name).read_bytes()).hexdigest()==digest
        if original.is_dir():assert (root/'src'/name).read_bytes()==(original/name).read_bytes()


def test_original_configuration_is_separate_and_can_be_disabled(tmp_path,monkeypatch):
    config=tmp_path/'private.env'
    config.write_text('RAG_ORIGINAL_PYTHON='+sys.executable+'\nRAG_ORIGINAL_INDEX_JSON='+str(tmp_path/'original.json'))
    monkeypatch.setenv('RAG_ORIGINAL_CONFIG_FILE',str(config))
    assert WebSettings.from_env().original_python==Path(sys.executable).resolve()
    monkeypatch.setenv('RAG_ORIGINAL_CONFIG_FILE','')
    assert WebSettings.from_env().original_python is None


def test_original_ui_search_shows_ranking_and_keeps_paid_action_disabled(tmp_path,monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    settings=configuration(tmp_path)
    monkeypatch.setenv('RAG_ORIGINAL_CONFIG_FILE','')
    for key,value in {'RAG_RUNTIME_DIR':str(settings.runtime_dir),'RAG_ENABLE_LIVE':'false',
        'RAG_ENABLE_LITERATURE':'true','RAG_CORPUS_DB':'','RAG_MODEL_ENV_FILE':'','LLM_API_KEY':'','LLM_MODEL':'',
        'RAG_ORIGINAL_PYTHON':str(settings.original_python),'RAG_ORIGINAL_INDEX_JSON':str(settings.original_index),
        'RAG_ORIGINAL_EMBEDDING_MODEL':str(settings.original_embedding),'RAG_ORIGINAL_RERANKER_MODEL':str(settings.original_reranker)}.items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(OriginalRetrieval,'retrieve',lambda self,payload:ranked(payload))
    st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py')).run()
        assert not app.exception
        assert next(r for r in app.radio if r.key=='literature_engine').value=='混合检索与精排'
        next(b for b in app.button if b.label=='检索并精排（不调用LLM）').click().run()
        import time
        for _ in range(30):
            time.sleep(.02);app.run()
            if any('精排完成' in x.value for x in app.success):break
        assert not app.exception
        assert next(b for b in app.button if b.label=='调用LLM抽取这批精排片段').disabled
        assert any('新计算0组' in x.value for x in app.caption)
    finally:st.cache_resource.clear()


def test_budget_comparison_reports_limits_without_inventing_recall(monkeypatch):
    import importlib.util
    import numpy as np
    import retrieval_pipeline as pipeline, hybrid_retrieval as hybrid
    from retrieval import build_index
    pool=[dict(chunk_id=str(i),paper_id=str(i),context=f'IL6 rheumatoid arthritis passage {i}') for i in range(24)]
    class Store:
        is_ready=True;cache_stats={'reused':24,'encoded':0}
        def search(self,query,top_k):return [{**c,'vector_score':1/(i+1)} for i,c in enumerate(pool)]
    class Ranker:
        def rerank(self,query,chunks,top_n):return chunks[:top_n]
    monkeypatch.setattr(pipeline,'get_or_build_mini_store',lambda *a:Store())
    monkeypatch.setattr(pipeline,'get_reranker',lambda:Ranker())
    monkeypatch.setattr(hybrid,'embed_query',lambda *a:np.array([1.,0.]))
    monkeypatch.setattr(hybrid,'get_embedding_settings',lambda:dict(enable_hybrid=True,fusion_strategy='rrf',vector_top_k=100,hybrid_weight_alpha=.5,rrf_k=60))
    path=Path(__file__).parents[1]/'scripts/compare_original_retrieval.py'
    spec=importlib.util.spec_from_file_location('compare_original',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    report,rows=module.compare(build_index(pool),'IL6','rheumatoid arthritis')
    assert [c['trace']['selected_count'] for c in report['comparisons']]==[5,10,15]
    assert all(c['recall'] is None and c['input_tokens'] is None for c in report['comparisons'])
    assert report['model_api_calls']==0 and report['online_requests']==0 and rows
