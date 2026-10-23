import json
import pytest
from baseline import PROFILE,assess,bind_primary_source,quote_segment,summarize
from corpus_store import CorpusStore
from conftest import wait_for
from jobs import Job,Query
from llm_cache import make_cache_key
from prompts import build_user_prompt,prompt_settings
from test_local_library import chunk


TEXT='IL6 transcript was higher in rheumatoid arthritis patients than in healthy controls.'


def evidence(**changes):
    return dict(task_profile=PROFILE,paper_id='A',gene_or_target='IL6',disease='rheumatoid arthritis',
        evidence_sentence=TEXT,source_context=TEXT,comparison_sentence=TEXT,
        comparison_type='disease_vs_normal',disease_group='rheumatoid arthritis patients',
        normal_group='healthy controls',expression_direction='increased',measurement_type='transcript',
        species='human',tissue_or_cell='blood',**changes)


def test_scope_requires_contiguous_source_and_explicit_target_groups():
    row=assess(evidence())
    assert row['baseline_status']=='eligible' and row['comparison_quote_verified']
    for field,value,flag in [
        ('normal_group','vehicle','normal_control_not_explicit'),
        ('disease_group','other disease patients','disease_group_not_supported_by_comparison_quote'),
        ('comparison_type','unclear','comparison_type_unconfirmed'),
        ('comparison_sentence','Fabricated comparison of cases and healthy controls.','comparison_quote_missing_or_not_contiguous'),
        ('evidence_sentence','Another gene was upregulated.','target_gene_not_explicit_in_expression_quote')]:
        changed=evidence();changed[field]=value
        result=assess(changed)
        assert result['baseline_status']=='needs_scope_review' and flag in result['baseline_flags']
    missing=evidence();missing['disease']='type 2 diabetes'
    assert 'target_disease_not_explicit_in_comparison' in assess(missing)['baseline_flags']


@pytest.mark.parametrize('changes',[
    {'comparison_type':'intervention'},{'comparison_type':'within_disease'},
    {'measurement_type':'activity'},{'measurement_type':'localization'},
    {'source_quality_flags':['reference_like']}])
def test_intervention_subgroups_activity_and_references_are_not_primary(changes):
    row=evidence();row.update(changes)
    assert assess(row)['baseline_status']=='out_of_scope'


def test_review_is_a_lead_not_original_study_validation():
    row=evidence();row['source_quality_flags']=['review_title']
    assert assess(row)['baseline_status']=='needs_scope_review'
    legacy=evidence();legacy.pop('task_profile')
    assert assess(legacy)==legacy and 'baseline_status' not in legacy


def test_source_binding_cannot_stitch_quotes_across_page_joins():
    left=chunk('A',1,'The study enrolled rheumatoid arthritis patients and healthy controls.')
    right=chunk('A',2,TEXT)
    combined={**left,'context':left['context']+'\n\n'+right['context'],'context_segments':[left,right]}
    bound=bind_primary_source({'evidence_sentence':TEXT},combined)
    assert bound['page']==2 and bound['chunk_id']=='A-2'
    stitched=left['context']+' '+right['context']
    assert quote_segment(stitched,combined) is None
    assert bind_primary_source({'evidence_sentence':'A fabricated quotation.'},combined) is None
    row=evidence();row.update(source_context=combined['context'],context_segments=[left,right],comparison_sentence=left['context'],
        comparison_type='disease_vs_normal')
    result=assess(row)
    assert result['baseline_status']=='eligible' and result['comparison_page']==1


def test_neighbor_context_is_bounded_and_stays_in_same_paper(tmp_path):
    records=[chunk('OTHER',1,'Must never enter this context.'),chunk('A',1,'Disease group definitions. '*8),
        chunk('A',2,TEXT),chunk('A',3,'Methods and comparisons. '*15),chunk('A',8,'Distant page. '*10)]
    database=tmp_path/'corpus.sqlite';CorpusStore.build(database,records)
    expanded=CorpusStore(database).with_neighbors(records[2],max_characters=500)
    assert len(expanded['context'])<=500 and expanded['expanded_context']
    assert all(s['paper_id']=='A' and s['page'] in {1,2,3} for s in expanded['context_segments'])
    assert 'Must never' not in expanded['context'] and 'Distant' not in expanded['context']
    abstract={**records[2],'source_type':'pubmed_abstract'}
    assert len(CorpusStore(database).with_neighbors(abstract)['context_segments'])==1


def test_condition_summary_preserves_differences_and_rejected_rows():
    a=assess(evidence());b={**a,'paper_id':'B','expression_direction':'decreased'}
    c={**a,'paper_id':'C','species':'mouse'};d={**a,'paper_id':'D','review_status':'rejected'}
    groups=summarize([a,b,c,d,{**a,'baseline_status':'needs_scope_review'}])
    assert len(groups)==2
    human=next(g for g in groups if g['species']=='human')
    assert human['papers']==['A','B'] and human['mixed_directions']
    assert human['directions']==['decreased','increased']


def test_baseline_prompt_and_cache_do_not_reinterpret_legacy():
    assert prompt_settings(PROFILE)[0]!=prompt_settings('legacy')[0]
    with pytest.raises(ValueError):prompt_settings('unknown')
    assert 'normal' in build_user_prompt('IL6','arthritis',{'context':TEXT},task_profile=PROFILE).lower()
    # Use the real cache signature: profile separation must survive identical model/text.
    kwargs={'gene':'IL6','disease':'rheumatoid arthritis','model':'test','base_url':'https://unit.invalid',
        'chunk':chunk('A',1,TEXT)}
    assert make_cache_key(**kwargs)!=make_cache_key(**kwargs,task_profile=PROFILE)
    split={**kwargs['chunk'],'context_segments':[chunk('A',1,TEXT[:30]),chunk('A',2,TEXT[30:])]}
    assert make_cache_key(**kwargs,task_profile=PROFILE)!=make_cache_key(**{**kwargs,'chunk':split},task_profile=PROFILE)


def test_demo_records_comparison_fields_and_default_primary_export(manager,owner):
    task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',task_profile=PROFILE))
    snapshot=wait_for(manager,owner,task)
    assert snapshot['state']=='completed' and snapshot['counts']['model_calls']==0
    assert snapshot['counts']['baseline_eligible']==3
    assert all(r['comparison_quote_verified'] and r['baseline_status']=='eligible' for r in snapshot['rows'])
    assert len(json.loads(manager.download(owner,task,'evidence.json',baseline_only=True)))==3
    assert snapshot['provenance']['task_profile']==PROFILE


def test_human_acceptance_cannot_bypass_missing_comparison_and_correction_is_audited(manager,owner):
    row=evidence();row.update(comparison_type='unclear',normal_group='',comparison_sentence='')
    job=Job('b'*32,owner,Query(('IL6',),'rheumatoid arthritis',task_profile=PROFILE),manager.root/('b'*32),
        state='completed',rows=[row])
    manager.history.save(job)
    original=manager.snapshot(owner,job.id)['rows'][0];eid=original['evidence_id']
    manager.review(owner,job.id,eid,'accepted',reviewer='human')
    assert json.loads(manager.download(owner,job.id,'evidence.json',baseline_only=True,confirmed_only=True))==[]
    with pytest.raises(ValueError,match='原句'):
        manager.review(owner,job.id,eid,'corrected',changes={'comparison_sentence':'Invented healthy control quote.'},reason='x',reviewer='human')
    manager.review(owner,job.id,eid,'corrected',changes={'comparison_type':'disease_vs_normal',
        'normal_group':'healthy controls','comparison_sentence':TEXT},reason='Checked retained source',reviewer='human')
    exported=json.loads(manager.download(owner,job.id,'evidence.json',baseline_only=True,confirmed_only=True))
    assert len(exported)==1 and exported[0]['baseline_status']=='eligible'
    assert manager.snapshot(owner,job.id)['rows'][0]['machine_result']['normal_group']==''
    assert len(manager.review_audit(owner,job.id))==2
    manager.review(owner,job.id,eid,'rejected',reason='Does not support the requested result',reviewer='human')
    assert json.loads(manager.download(owner,job.id,'evidence.json',baseline_only=True))==[]


def test_real_workflow_routes_model_output_into_three_queues(tmp_path,monkeypatch,owner):
    import jobs,config
    from jobs import JobManager
    from web_settings import WebSettings
    from schemas import normalize_result
    database=tmp_path/'corpus.sqlite';CorpusStore.build(database,[chunk('A',1,TEXT)])
    monkeypatch.setattr(config,'get_llm_settings',lambda:dict(api_key='unit',model='unit'))
    class Client:
        request_count=1;failure_count=0
        def __init__(self,**kwargs):assert kwargs['task_profile']==PROFILE and kwargs['allow_fallback'] is False
        def extract_batch(self,g,d,chunks,**kwargs):
            assert kwargs['retry']==0
            outputs=[evidence(),evidence(),evidence()]
            outputs[1].update(normal_group='',comparison_type='unclear')
            outputs[2]['comparison_type']='intervention'
            return [(c,[normalize_result(o) for o in outputs]) for c in chunks]
    monkeypatch.setattr(jobs,'LLMClient',Client)
    manager=JobManager(WebSettings(runtime_dir=tmp_path/'runtime',corpus_db=database,enable_live=True,access_password='unit-long-password'))
    try:
        task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis',source='corpus',task_profile=PROFILE,consent=True),authorized=True)
        snapshot=wait_for(manager,owner,task)
        assert snapshot['state']=='completed'
        assert snapshot['counts']['baseline_eligible']==1 and snapshot['counts']['baseline_pending']==1
        assert snapshot['counts']['baseline_out_of_scope']==1
        assert len(json.loads(manager.download(owner,task,'evidence.json',baseline_only=True)))==1
        assert len(json.loads(manager.download(owner,task,'evidence.json')))==3
    finally:manager.close()
