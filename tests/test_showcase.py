import copy
import importlib.util
import json
from pathlib import Path
import time

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from baseline import PROFILE
from conftest import wait_for
from jobs import Query
from result_overview import overview
from showcase import scenarios


@pytest.mark.parametrize('name', ['clear', 'conditions', 'insufficient'])
def test_scenario_uses_normal_history_and_exports_without_model(manager, owner, monkeypatch, name):
    import jobs
    def forbidden(**kwargs): raise AssertionError('A showcase must never instantiate a model')
    monkeypatch.setattr(jobs, 'LLMClient', forbidden)
    item = scenarios()[name]
    task = manager.submit(owner, Query((item['gene'],), item['disease'], task_profile=PROFILE, demo_scenario=name))
    snapshot = wait_for(manager, owner, task)
    assert snapshot['state'] == item['expected_state']
    assert len(snapshot['rows']) == item['expected_evidence']
    assert snapshot['synthetic'] and snapshot['counts']['model_calls'] == 0
    assert all(r['quote_verified'] and r['comparison_quote_verified'] and r['baseline_status'] == 'eligible'
        for r in snapshot['rows'])
    assert len(json.loads(manager.download(owner, task, 'evidence.json', baseline_only=True))) == item['expected_evidence']
    with pytest.raises(PermissionError): manager.snapshot('b' * 64, task)
    if snapshot['rows']:
        row = snapshot['rows'][0]
        assert overview(snapshot)['confirmed'] == 0
        manager.review(owner, task, row['evidence_id'], 'accepted', reviewer='teaching', reason='Synthetic operation only')
        updated = manager.snapshot(owner, task)
        assert overview(updated)['confirmed'] == 1 and updated['rows'][0]['synthetic']
        assert len(json.loads(manager.download(owner, task, 'evidence.json', baseline_only=True, confirmed_only=True))) == 1


@pytest.mark.parametrize('changes', [{'demo_scenario': '../private'}, {'demo_scenario': 4},
    {'demo_scenario': 'clear', 'genes': ('CD36',)}, {'demo_scenario': 'clear', 'task_profile': 'legacy'},
    {'demo_scenario': 'clear', 'source': 'pubmed'}])
def test_showcase_cannot_override_real_sources_or_targets(manager, owner, changes):
    kwargs = dict(genes=('IL6',), disease='rheumatoid arthritis', task_profile=PROFILE)
    kwargs.update(changes)
    with pytest.raises((ValueError, PermissionError)):
        manager.submit(owner, Query(**kwargs))


def row(**changes):
    item = dict(evidence_id='e1', paper_id='p1', gene_or_target='G', disease='D', species='human',
        population_context='disease_cohort', tissue_or_cell='blood', measurement_type='transcript',
        disease_group='patients', normal_group='healthy controls', expression_direction='increased',
        significance='not_reported', baseline_status='eligible', review_status='accepted')
    return {**item, **changes}


def test_overview_excludes_unreviewed_rejected_and_unsupported_rows():
    snapshot = dict(state='completed', rows=[row(), row(evidence_id='e2', review_status='unreviewed'),
        row(evidence_id='e3', review_status='rejected'), row(evidence_id='e4', baseline_status='needs_scope_review')])
    result = overview(snapshot)
    assert (result['confirmed'], result['unreviewed'], result['scope_pending'], result['rejected']) == (1, 1, 1, 1)
    assert len(result['groups']) == 1 and result['groups'][0]['evidence_ids'] == ['e1']


def test_overview_never_merges_unknown_conditions_and_keeps_populations_separate():
    rows = [row(), row(evidence_id='e2', population_context='cell_disease_model'),
        row(evidence_id='e3', tissue_or_cell='unclear'), row(evidence_id='e4', tissue_or_cell='unclear')]
    result = overview(dict(state='completed', rows=rows))
    assert len(result['groups']) == 4
    assert sum('条件缺失' in g['status'] for g in result['groups']) == 2


def test_overview_preserves_opposite_directions_and_statistical_qualifiers():
    result = overview(dict(state='partial', rows=[row(), row(evidence_id='e2', expression_direction='decreased'),
        row(evidence_id='e3', expression_direction='unchanged', significance='not_significant')]))
    assert result['unfinished'] and '尚未完成' in result['headline']
    assert len(result['groups']) == 1 and '相反方向' in result['groups'][0]['status']
    assert ('unchanged', 'not_significant') in result['groups'][0]['observations']
    assert '多数' not in result['groups'][0]['status']


@pytest.mark.parametrize('state', ['failed', 'no_relations', 'no_candidates'])
def test_empty_or_failed_overview_does_not_claim_biological_absence(state):
    result = overview(dict(state=state, rows=[]))
    assert not result['groups'] and result['confirmed'] == 0
    assert '无关' not in result['headline'] and '下调' not in result['headline']


def test_historical_review_is_minimal_and_keeps_current_accuracy_unmeasured():
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / 'examples/recorded_run_review.json').read_text())
    assert (data['requests'], data['failed_requests'], data['emitted_relations']) == (3, 0, 2)
    assert data['usage']['total_tokens'] == 7038
    assert data['medical_accuracy'] is None and data['current_baseline_accuracy'] is None
    assert not data['independent_test'] and len(data['cases']) == 3
    assert data['unsupported_scope_passages'] == 2
    forbidden = {'context', 'source_context', 'human_screening', 'reviewer', 'api_key', 'raw_response'}
    assert not (set(data) & forbidden)
    assert all(not (set(c) & forbidden) for c in data['cases'])


def test_offline_check_is_reproducible_and_does_not_score_medical_accuracy():
    path = Path(__file__).resolve().parents[1] / 'scripts/portfolio_check.py'
    spec = importlib.util.spec_from_file_location('portfolio_check', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    result = module.check()
    assert result['passed'] and len(result['scenarios']) == 3
    assert result['model_calls'] == result['external_requests'] == 0
    assert result['medical_accuracy'] is None and result['retrieval_recall'] is None


def test_presentation_ui_runs_three_scenarios_and_updates_reviewed_overview(tmp_path, monkeypatch):
    for key in ['RAG_CORPUS_DB', 'RAG_MODEL_ENV_FILE', 'LLM_API_KEY', 'LLM_MODEL', 'RAG_ACCESS_PASSWORD']:
        monkeypatch.setenv(key, '')
    monkeypatch.setenv('RAG_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setenv('RAG_ENABLE_LIVE', 'false'); monkeypatch.setenv('RAG_ENABLE_LITERATURE', 'false')
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).parents[1] / 'src/app.py'), default_timeout=10).run()
        assert not app.exception and app.tabs[0].label == '人工示例体验'
        assert not any(e.label in {'先了解示例与真实抽取的区别','使用说明与部署状态'} for e in app.expander)
        for name in ['clear', 'conditions', 'insufficient']:
            item = scenarios()[name]
            next(b for b in app.button if b.label == item['button']).click().run()
            for _ in range(50):
                time.sleep(.02); app.run()
                if app.metric: break
            assert not app.exception and not app.error
            assert [m.value for m in app.metric][1] == str(item['expected_evidence'])
            if name == 'clear':
                next(s for s in app.selectbox if s.label == '复核决定').select('accepted').run()
                next(t for t in app.text_input if t.label == '复核人（姓名或代号）').input('teaching')
                next(b for b in app.button if b.label == '保存复核').click().run()
                assert any('已有 1 条人工核对' in m.value for m in app.markdown)
            if name == 'insufficient':
                assert any('当前材料不足' in m.value for m in app.markdown)
    finally:
        st.cache_resource.clear()


def test_scientific_case_cards_escape_source_text_before_rendering_html():
    from ui_theme import case_html
    item=copy.deepcopy(scenarios()['clear'])
    item.update(title='01 · <script>alert(1)</script>',description='<img src=x onerror=alert(1)>',
        gene='GENE_<unsafe>',learn='Some <source> text')
    html=case_html('clear',item,1)
    assert '<script>' not in html and '<img src=x' not in html and 'GENE_<unsafe>' not in html
    assert '&lt;script&gt;' in html and 'GENE_&lt;unsafe&gt;' in html
    assert '人工预设' in html and '预设 1 条证据' in html


def test_scientific_header_uses_actual_configuration_without_claiming_connectivity(monkeypatch):
    from ui_theme import header
    rendered=[]
    monkeypatch.setattr(st,'markdown',lambda text,**kwargs:rendered.append(text))
    header({},online=False,configured=False,authorized=False,live_ready=False)
    assert '未接入' in rendered[-1] and '未配置' in rendered[-1] and '未启用真实抽取' in rendered[-1]
    header({'paper_count':'17297'},online=True,configured=True,authorized=False,live_ready=True)
    assert '17,297 份可检索' in rendered[-1] and '待验证口令' in rendered[-1]
    assert '未验证服务可用性' in rendered[-1] and '非接口连通性验证' in rendered[-1]
    assert '已连接' not in rendered[-1]
