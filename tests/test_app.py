import time
from pathlib import Path
import streamlit as st
import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture(autouse=True)
def isolate_app_configuration(monkeypatch):
    monkeypatch.setenv('RAG_ORIGINAL_CONFIG_FILE','')
    for key in ['RAG_CORPUS_DB','RAG_MODEL_ENV_FILE','LLM_API_KEY','LLM_MODEL','RAG_ACCESS_PASSWORD']:
        monkeypatch.setenv(key,'')



def preview_picker(app):
    return next(widget for widget in app.selectbox if widget.key=='lit_preview_document')


def has_preview(app):
    return any(widget.key=='lit_preview_document' for widget in app.selectbox)


def test_demo_ui_finishes_and_filters_without_model(tmp_path,monkeypatch):
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE','false')
    st.cache_resource.clear()



    app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py'),default_timeout=10).run()
    assert not app.exception
    next(b for b in app.button if b.label=='IL6 · 类风湿关节炎').click().run()
    for _ in range(50):
        time.sleep(.02)
        app.run()
        if app.metric:
            break
    assert not app.exception
    assert [m.value for m in app.metric]==['3','3','0']
    result_table=next(d.value for d in app.dataframe if '复核状态' in d.value.columns)
    assert '未见显著差异' in set(result_table['表达方向'])
    assert any('人工编写' in w.value for w in app.warning)
    species=next(s for s in app.selectbox if s.label=='研究物种')
    species.select('mouse').run()
    assert not app.exception
    assert len(next(d.value for d in app.dataframe if '复核状态' in d.value.columns))==1
    # Switching to a new task must select its own output, not the old task's.
    next(b for b in app.button if b.label=='ABCC8 · 2 型糖尿病').click().run()
    for _ in range(50):
        time.sleep(.02)
        app.run()
        if any(m.value=='2' for m in app.metric): break
    assert not app.exception
    assert [m.value for m in app.metric]==['2','2','0']
    st.cache_resource.clear()


def test_review_form_and_recovery_code_restore_history(tmp_path,monkeypatch):
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE','false')
    st.cache_resource.clear()
    filename=str(Path(__file__).parents[1]/'src/app.py')
    app=AppTest.from_file(filename,default_timeout=10).run()
    recovery=app.session_state['owner']
    next(b for b in app.button if b.label=='IL6 · 类风湿关节炎').click().run()
    for _ in range(50):
        time.sleep(.02);app.run()
        if app.metric: break
    next(s for s in app.selectbox if s.label=='复核决定').select('corrected').run()
    direction=next(s for s in app.selectbox if s.label=='修正表达方向')
    direction.select('decreased' if direction.value!='decreased' else 'increased')
    next(s for s in app.selectbox if s.label=='修正测量对象').select('transcript')
    next(t for t in app.text_input if t.label=='复核人（姓名或代号）').input('test-reviewer')
    next(t for t in app.text_area if t.label=='复核理由').input('Test correction of expression interpretation')
    next(b for b in app.button if b.label=='保存复核').click().run()
    assert not app.exception
    assert not app.error, [e.value for e in app.error]
    evidence=next(d.value for d in app.dataframe if '复核状态' in d.value.columns)
    assert '已修正' in evidence['复核状态'].tolist()
    assert '转录本数量' in evidence['测量对象'].tolist()
    restored=AppTest.from_file(filename,default_timeout=10).run()
    assert restored.session_state['owner']!=recovery and not restored.metric
    next(t for t in restored.text_input if t.label=='研究空间恢复码').input(recovery)
    next(b for b in restored.button if b.label=='恢复历史').click().run()
    assert not restored.exception and restored.session_state['owner']==recovery
    evidence=next(d.value for d in restored.dataframe if '复核状态' in d.value.columns)
    assert '已修正' in evidence['复核状态'].tolist()
    assert '转录本数量' in evidence['测量对象'].tolist()
    st.cache_resource.clear()

def test_independent_search_and_fulltext_ui_without_model(tmp_path,monkeypatch):
    from test_literature import result,xml
    import fulltext_gateway,literature_search,jobs
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE','false')
    monkeypatch.setenv('RAG_ENABLE_LITERATURE','true')
    monkeypatch.setattr(literature_search,'search',lambda *args,**kwargs:result())
    monkeypatch.setattr(fulltext_gateway,'fetch',lambda p,*args,**kwargs:fulltext_gateway.parse_fulltext(xml(),p,'PMC123'))
    def forbidden(**kwargs): raise AssertionError('No model call for literature actions')
    monkeypatch.setattr(jobs,'LLMClient',forbidden)
    st.cache_resource.clear()
    app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py'),default_timeout=10).run()
    next(b for b in app.button if b.label=='检索候选文献（不调用模型）').click().run()
    for _ in range(50):
        time.sleep(.02);app.run()
        if has_preview(app): break
    assert not app.exception and has_preview(app)
    selection=preview_picker(app)
    # AppTest exposes formatted options; select the underlying document ID from state.
    import sqlite3,json
    with sqlite3.connect(tmp_path/'literature.sqlite') as conn:
        document_id=json.loads(conn.execute('SELECT summary FROM papers').fetchone()[0])['document_id']
    selection.select(document_id).run()
    next(b for b in app.button if b.label=='获取所选开放全文（不调用模型）').click().run()
    for _ in range(50):
        time.sleep(.02);app.run()
        if any('已获取正文段落' in s.value for s in app.markdown): break
    assert not app.exception
    with sqlite3.connect(tmp_path/'history.sqlite') as conn:
        states=[(p['query']['action'],p['state'],p['message']) for (value,) in conn.execute('SELECT payload FROM jobs') for p in [json.loads(value)]]
    assert any('已获取正文段落' in s.value for s in app.markdown), ([e.value for e in app.error],states,preview_picker(app).value)
    assert next(b for b in app.button if b.label=='开始自动分批扫描').disabled
    next(c for c in app.checkbox if c.label=='我确认删除所选文献的私有收藏').check().run()
    next(b for b in app.button if b.label=='删除所选收藏').click().run()
    assert not app.exception
    st.cache_resource.clear()


def test_invalid_literature_query_shows_error_without_network(tmp_path,monkeypatch):
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE','false')
    monkeypatch.setenv('RAG_ENABLE_LITERATURE','true')
    st.cache_resource.clear()
    app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py'),default_timeout=10).run()
    next(t for t in app.text_input if t.label=='基因名称（单个）').input('')
    next(b for b in app.button if b.label=='检索候选文献（不调用模型）').click().run()
    assert not app.exception and app.error
    st.cache_resource.clear()


def test_batch_ui_scans_unselected_candidates_with_budget_and_resume_consent(tmp_path,monkeypatch):
    import hashlib,jobs,literature_search
    from schemas import normalize_result
    from test_baseline import TEXT,evidence
    from test_literature import paper,result
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE','true');monkeypatch.setenv('RAG_ENABLE_LITERATURE','true')
    monkeypatch.setenv('RAG_ACCESS_PASSWORD','unit-access-password')
    monkeypatch.setenv('LLM_API_KEY','unit-key');monkeypatch.setenv('LLM_MODEL','unit-model')
    monkeypatch.setenv('LLM_BASE_URL','https://unit.invalid')
    papers=[]
    for n in range(8):
        p=paper(str(1000+n));p.update(abstract=TEXT,abstract_sha256=hashlib.sha256(TEXT.encode()).hexdigest());papers.append(p)
    monkeypatch.setattr(literature_search,'search',lambda *a,**k:result(papers))
    calls=[]
    class Client:
        def __init__(self,**kwargs):self.request_count=0;self.failure_count=0;self.usage={}
        def extract_batch(self,g,d,chunks,**kwargs):
            calls.extend(c['paper_id'] for c in chunks);self.request_count+=len(chunks)
            return [(c,[normalize_result(evidence())]) for c in chunks]
    monkeypatch.setattr(jobs,'LLMClient',Client);st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py'),default_timeout=10).run()
        next(b for b in app.button if b.label=='检索候选文献（不调用模型）').click().run()
        for _ in range(50):
            time.sleep(.02);app.run()
            if has_preview(app):break
        assert not app.exception and not app.multiselect and not calls
        assert preview_picker(app).value is None
        assert len(preview_picker(app).options)==8
        assert next(b for b in app.button if b.label=='开始自动分批扫描').disabled
        next(t for t in app.text_input if t.label=='真实抽取访问口令').input('unit-access-password')
        next(b for b in app.button if b.label=='验证口令').click().run()
        next(n for n in app.number_input if n.label=='本轮最多处理篇数').set_value(1).run()
        consent='我同意按上述上限扫描候选并外发文字，了解可能产生模型费用。'
        next(c for c in app.checkbox if c.label==consent).check().run()
        # Previewing one paper must leave all eight candidates in the scan queue.
        import sqlite3,json
        with sqlite3.connect(tmp_path/'literature.sqlite') as conn:
            ids=sorted(json.loads(row[0])['document_id'] for row in conn.execute('SELECT summary FROM papers'))
        preview_picker(app).select(ids[-1]).run()
        assert not app.multiselect and next(c for c in app.checkbox if c.label==consent).value
        next(s for s in app.slider if s.label=='扫描每篇最多新候选片段组').set_value(3).run()
        assert not next(c for c in app.checkbox if c.label==consent).value
        next(c for c in app.checkbox if c.label==consent).check().run()
        next(b for b in app.button if b.label=='开始自动分批扫描').click().run()
        for _ in range(50):
            time.sleep(.02);app.run()
            if any(b.label=='按本轮上限继续扫描' for b in app.button):break
        assert not app.exception and not app.error and len(calls)==1
        coverage=next(d.value for d in app.dataframe if '处理状态' in d.value.columns)
        assert len(coverage)==8 and (coverage['处理状态']=='未处理').sum()==7
        next(n for n in app.number_input if n.label=='继续这轮最多处理篇数').set_value(2).run()
        resume_consent='我同意本次继续扫描的上限、文字外发及可能产生的新费用。'
        next(c for c in app.checkbox if c.label==resume_consent).check().run()
        next(n for n in app.number_input if n.label=='继续这轮最多请求名额').set_value(2).run()
        assert not next(c for c in app.checkbox if c.label==resume_consent).value
        next(c for c in app.checkbox if c.label==resume_consent).check().run()
        next(b for b in app.button if b.label=='按本轮上限继续扫描').click().run()
        for _ in range(50):
            time.sleep(.02);app.run()
            if len(calls)==3 and any(b.label=='按本轮上限继续扫描' for b in app.button):break
        assert not app.exception and not app.error and len(calls)==len(set(calls))==3
    finally:st.cache_resource.clear()


def test_offline_workspace_explains_disabled_search_and_keeps_demo_available(tmp_path,monkeypatch):
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE','false')
    monkeypatch.setenv('RAG_ENABLE_LITERATURE','false')
    st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py')).run()
        assert not app.exception
        assert [tab.label for tab in app.tabs]==['人工示例体验','真实文献与模型抽取','上传论文与本地库']
        assert next(b for b in app.button if b.label=='检索候选文献（不调用模型）').disabled
        assert next(b for b in app.button if b.label=='开始自动分批扫描').disabled
        assert not next(b for b in app.button if b.label=='IL6 · 类风湿关节炎').disabled
        assert any('RAG_ENABLE_LITERATURE' in message.value for message in app.info)
    finally:
        st.cache_resource.clear()


def test_model_status_does_not_display_deployment_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE','false')
    monkeypatch.setenv('LLM_API_KEY','unit-private-value-not-for-display')
    monkeypatch.setenv('LLM_MODEL','unit-model')
    monkeypatch.setenv('LLM_BASE_URL','https://private-provider.invalid/v1')
    st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py')).run()
        assert not app.exception
        visible='\n'.join(item.value for kind in ('markdown','caption','info','code') for item in getattr(app,kind))
        assert '已配置' in visible
        assert 'unit-private-value-not-for-display' not in visible
        assert 'private-provider.invalid' not in visible
        assert '访问口令不是模型密钥' in visible
        assert next(b for b in app.button if b.label=='开始自动分批扫描').disabled
    finally:
        st.cache_resource.clear()


def test_selected_papers_require_access_and_consent_and_keep_submitted_target(tmp_path,monkeypatch):
    import json,sqlite3
    import literature_search,jobs
    from schemas import ExtractionResult
    from test_literature import result,paper
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE','true')
    monkeypatch.setenv('RAG_ENABLE_LITERATURE','true')
    monkeypatch.setenv('RAG_ACCESS_PASSWORD','unit-access-password')
    monkeypatch.setenv('LLM_API_KEY','unit-api-key')
    monkeypatch.setenv('LLM_MODEL','unit-model')
    monkeypatch.setenv('LLM_BASE_URL','https://unit-provider.invalid/v1')
    monkeypatch.setattr(literature_search,'search',lambda *a,**k:result([paper('123'),paper('124')]))
    calls=[]
    class Client:
        failure_count=0
        request_count=1
        def __init__(self,**kwargs):
            assert kwargs['allow_fallback'] is False
        def extract_batch(self,gene,disease,chunks,**kwargs):
            calls.append((gene,disease,chunks))
            return [(c,[ExtractionResult(evidence_sentence=c['context'],expression_direction='increased',species='human')]) for c in chunks]
    monkeypatch.setattr(jobs,'LLMClient',Client)
    st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py'),default_timeout=10).run()
        next(b for b in app.button if b.label=='检索候选文献（不调用模型）').click().run()
        for _ in range(50):
            time.sleep(.02);app.run()
            if has_preview(app):break
        assert not app.exception and has_preview(app) and not app.multiselect and not calls
        next(r for r in app.radio if r.label=='处理方式').set_value('手动抽取少量文献').run()
        assert app.multiselect
        with sqlite3.connect(tmp_path/'literature.sqlite') as conn:
            ids=sorted(json.loads(row[0])['document_id'] for row in conn.execute('SELECT summary FROM papers'))
        app.multiselect[0].select(ids[0]).run()
        assert next(b for b in app.button if b.label=='调用模型整理所选文献').disabled
        next(t for t in app.text_input if t.label=='真实抽取访问口令').input('unit-access-password')
        next(b for b in app.button if b.label=='验证口令').click().run()
        assert next(b for b in app.button if b.label=='调用模型整理所选文献').disabled
        next(c for c in app.checkbox if c.label=='我允许将所选候选文本发送到配置的模型服务。').check().run()
        assert not next(b for b in app.button if b.label=='调用模型整理所选文献').disabled
        next(s for s in app.slider if s.label=='每篇最多抽取的候选片段数').set_value(1).run()
        assert not next(c for c in app.checkbox if c.label=='我允许将所选候选文本发送到配置的模型服务。').value
        next(c for c in app.checkbox if c.label=='我允许将所选候选文本发送到配置的模型服务。').check().run()
        next(c for c in app.checkbox if c.label=='模型请求失败后自动重试1次（可能增加费用）').check().run()
        assert not next(c for c in app.checkbox if c.label=='我允许将所选候选文本发送到配置的模型服务。').value
        next(c for c in app.checkbox if c.label=='模型请求失败后自动重试1次（可能增加费用）').uncheck().run()
        next(c for c in app.checkbox if c.label=='我允许将所选候选文本发送到配置的模型服务。').check().run()
        # Editing an unsubmitted search form must not silently change the extraction target.
        next(t for t in app.text_input if t.label=='基因名称（单个）').input('TP53').run()
        next(b for b in app.button if b.label=='调用模型整理所选文献').click().run()
        for _ in range(50):
            time.sleep(.02);app.run()
            if app.metric:break
        assert not app.exception and app.metric and calls
        assert calls[0][0:2]==('IL6','rheumatoid arthritis')
        with sqlite3.connect(tmp_path/'history.sqlite') as conn:
            queries=[json.loads(row[0])['query'] for row in conn.execute('SELECT payload FROM jobs')]
        extracted=next(q for q in queries if q['action']=='extract')
        assert extracted['genes']==['IL6'] and extracted['document_ids']==[ids[0]]
        assert extracted['model_retries']==0
        assert not any('人工编写' in w.value for w in app.warning)
        # A new set of selected papers needs a new explicit outbound confirmation.
        app.multiselect[0].select(ids[1]).run()
        assert not next(c for c in app.checkbox if c.label=='我允许将所选候选文本发送到配置的模型服务。').value
        assert next(b for b in app.button if b.label=='调用模型整理所选文献').disabled
        next(c for c in app.checkbox if c.label=='我允许将所选候选文本发送到配置的模型服务。').check().run()
        next(r for r in app.radio if r.label=='处理方式').set_value('自动分批扫描全部当前候选').run()
        assert not app.multiselect
        assert not next(c for c in app.checkbox if c.key=='scan_start_consent').value
        next(c for c in app.checkbox if c.key=='scan_start_consent').check().run()
        next(r for r in app.radio if r.label=='处理方式').set_value('手动抽取少量文献').run()
        assert not next(c for c in app.checkbox if c.key=='lit_model_consent').value
    finally:
        st.cache_resource.clear()


def test_local_ui_works_offline_and_new_query_has_its_own_candidates(tmp_path,monkeypatch):
    from corpus_store import CorpusStore
    from test_local_library import chunk
    import literature_search
    corpus=tmp_path/'corpus.sqlite'
    CorpusStore.build(corpus,[chunk('A',1),chunk('B',1,'ABCC8 expression was reduced in type 2 diabetes.')])
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path/'runtime'))
    monkeypatch.setenv('RAG_CORPUS_DB',str(corpus))
    monkeypatch.setenv('RAG_ENABLE_LIVE','false')
    monkeypatch.setenv('RAG_ENABLE_LITERATURE','false')
    def forbidden(*a,**k):raise AssertionError('Offline local UI cannot call PubMed')
    monkeypatch.setattr(literature_search,'search',forbidden)
    st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py'),default_timeout=10).run()
        assert next(r for r in app.radio if r.label=='检索范围').value=='本地全文库（不联网）'
        assert not next(b for b in app.button if b.label=='检索候选文献（不调用模型）').disabled
        next(b for b in app.button if b.label=='检索候选文献（不调用模型）').click().run()
        for _ in range(50):
            time.sleep(.02);app.run()
            if has_preview(app):break
        assert not app.exception and len(preview_picker(app).options)==1
        assert any('A' in value for value in preview_picker(app).options)
        assert next(r for r in app.radio if r.label=='处理方式').value=='自动分批扫描全部当前候选'
        assert not app.multiselect
        next(t for t in app.text_input if t.label=='基因名称（单个）').input('ABCC8')
        next(t for t in app.text_input if t.label=='疾病英文名称').input('type 2 diabetes')
        next(b for b in app.button if b.label=='检索候选文献（不调用模型）').click().run()
        for _ in range(50):
            time.sleep(.02);app.run()
            if has_preview(app) and any('B' in value for value in preview_picker(app).options):break
        assert not app.exception and len(preview_picker(app).options)==1
        assert preview_picker(app).value is None
        next(r for r in app.radio if r.label=='候选文献范围').set_value('已保存候选（最近200篇）').run()
        assert len(preview_picker(app).options)==2
    finally:
        st.cache_resource.clear()


def test_current_candidates_preserve_search_order_instead_of_collection_recency(tmp_path,monkeypatch):
    import literature_search
    from test_literature import result,paper
    first=paper('123');first['title']='First search result'
    second=paper('124');second['title']='Second search result'
    monkeypatch.setenv('RAG_RUNTIME_DIR',str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE','false')
    monkeypatch.setenv('RAG_ENABLE_LITERATURE','true')
    monkeypatch.setattr(literature_search,'search',lambda *a,**k:result([first,second]))
    st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(Path(__file__).parents[1]/'src/app.py'),default_timeout=10).run()
        next(b for b in app.button if b.label=='检索候选文献（不调用模型）').click().run()
        for _ in range(50):
            time.sleep(.02);app.run()
            if has_preview(app):break
        assert not app.exception
        assert [s.split(' · ',1)[1] for s in preview_picker(app).options]==['First search result','Second search result']
    finally:st.cache_resource.clear()

