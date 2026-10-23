import copy
import hashlib
import pytest
from evaluation import evaluate,evaluate_retrieval,validate_labels


def case(case_id='a',reviewed=True,positive=True):
    context='IL6 expression was elevated in rheumatoid arthritis patients.'
    relation=dict(expression_direction='increased',disease_effect='biomarker_only',relation_type='associated',
        species='human',evidence_sentence=context)
    return dict(case_id=case_id,paper_id='PMID'+case_id,context=context,
        context_sha256=hashlib.sha256(context.encode()).hexdigest(),split='development',
        gold_status='reviewed' if reviewed else 'unreviewed',reviewer='expert' if reviewed else '',
        gold_relevant=positive if reviewed else None,gold_relations=([relation] if positive else []) if reviewed else None)


def prediction(c):
    return dict(case_id=c['case_id'],paper_id=c['paper_id'],context_sha256=c['context_sha256'],
        complete=True,relations=copy.deepcopy(c['gold_relations']),retrieved=True)


def test_unreviewed_has_no_medical_metrics():
    report=evaluate([case(reviewed=False)],[])
    assert report['metrics'] is None and not report['evaluation_ready']


@pytest.mark.parametrize('change',[{'complete':False},{'context_sha256':'different'},
    {'paper_id':'other'},{'extraction_performed':False},{'truncated_input':True}])
def test_incomplete_or_mismatched_predictions_do_not_enter_metrics(change):
    c=case();p=prediction(c);p.update(change)
    assert evaluate([c],[p])['metrics'] is None
    assert evaluate([c],[])['metrics'] is None


def test_exact_matching_and_false_positive_on_negative_paper():
    a,b=case('a'),case('b',positive=False)
    pa,pb=prediction(a),prediction(b)
    pb['relations']=copy.deepcopy(pa['relations'])
    metrics=evaluate([a,b],[pa,pb])['metrics']
    assert metrics['precision']==.5 and metrics['recall']==1
    assert metrics['f1']==pytest.approx(2/3)
    pa['relations'][0]['evidence_sentence']='Invented sentence not in the source'
    metrics=evaluate([a,b],[pa,pb])['metrics']
    assert metrics['true_positive_relations']==0 and metrics['false_negative_relations']==1
    assert metrics['false_positive_relations']==2


def test_paper_split_leakage_hash_and_bad_labels_rejected():
    a=case();b=copy.deepcopy(a);b.update(case_id='b',split='test')
    with pytest.raises(ValueError): validate_labels([a,b])
    a['context']='Changed source'
    with pytest.raises(ValueError): validate_labels([a])
    a=case();a['gold_relations'][0]['relation_type']=[]
    with pytest.raises(ValueError): validate_labels([a])


def test_retrieval_metrics_independent_of_extraction():
    c=case();p=prediction(c);p.update(extraction_performed=False,retrieved=False);p.pop('relations')
    assert evaluate_retrieval([c],[p])['metrics']['retrieval_recall']==0
    assert evaluate([c],[p])['metrics'] is None
    p['paper_id']='other'
    assert evaluate_retrieval([c],[p])['metrics'] is None


def scoped_case():
    c=case()
    c['gold_relations'][0].update(relation_scope='direct',population_context='disease_cohort',measurement_type='unclear')
    return c


@pytest.mark.parametrize(('field','wrong'),[
    ('relation_scope','background'),('population_context','cell_disease_model'),('measurement_type','transcript')])
def test_seven_field_profile_counts_scope_and_measurement_errors_legacy_does_not(field,wrong):
    c=scoped_case();p=prediction(c);p['relations'][0][field]=wrong
    assert evaluate([c],[p])['metrics']['true_positive_relations']==1
    report=evaluate([c],[p],profile='scope_measurement')
    assert report['evaluation_ready'] and report['metrics']['true_positive_relations']==0
    assert report['metrics']['false_positive_relations']==report['metrics']['false_negative_relations']==1
    assert report['metrics']['f1']==0
    assert report['not_evaluated_fields']==[] and field in report['evaluated_fields']


def test_seven_field_profile_never_fills_missing_reference_from_prediction():
    c=case();p=prediction(c)
    p['relations'][0].update(relation_scope='direct',population_context='disease_cohort',measurement_type='unclear')
    before=copy.deepcopy(c)
    report=evaluate([c],[p],profile='scope_measurement')
    assert not report['evaluation_ready'] and report['metrics'] is None
    assert report['incomplete_reference_cases']==1 and c==before
    assert set(report['reference_missing_fields'][0]['fields'])=={'relation_scope','population_context','measurement_type'}


def test_explicit_unclear_is_a_reference_value_but_absent_prediction_does_not_match_it():
    c=scoped_case();p=prediction(c)
    assert evaluate([c],[p],profile='scope_measurement')['metrics']['true_positive_relations']==1
    p['relations'][0].pop('measurement_type')
    assert evaluate([c],[p],profile='scope_measurement')['metrics']['true_positive_relations']==0


@pytest.mark.parametrize('field',['relation_scope','population_context','measurement_type'])
def test_invalid_optional_scope_reference_rejected(field):
    c=scoped_case();c['gold_relations'][0][field]=''
    with pytest.raises(ValueError,match='scope or measurement'): evaluate([c],[prediction(c)],profile='scope_measurement')


def test_seven_field_negative_reference_and_unreviewed_cases_remain_distinct():
    c=case(positive=False)
    report=evaluate([c],[prediction(c)],profile='scope_measurement')
    assert report['evaluation_ready'] and report['metrics']['false_positive_relations']==0
    assert report['metrics']['precision'] is None
    assert evaluate([case(reviewed=False)],[],profile='scope_measurement')['metrics'] is None


def test_malformed_prediction_fields_count_as_false_positive_instead_of_crashing():
    c=scoped_case();p=prediction(c);p['relations'][0]['measurement_type']=[]
    result=evaluate([c],[p],profile='scope_measurement')['metrics']
    assert result['false_positive_relations']==result['false_negative_relations']==1
