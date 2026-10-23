import pytest
from validators import apply_relation_rules, ground_result
from schemas import ExtractionResult, normalize_result

CHUNK={'chunk_id':'real_1','paper_id':'PMID12345','title':'Trusted title',
       'source_location':'PMID 12345','source_type':'pubmed_abstract',
       'context':'IL6 expression was elevated in rheumatoid arthritis but did not predict survival.'}


def test_negation_does_not_erase_expression_or_other_endpoints():
    result=ExtractionResult(evidence_sentence=CHUNK['context'],expression_direction='increased',
        disease_effect='biomarker_only',confidence=.9)
    result=apply_relation_rules(result,'IL6','rheumatoid arthritis')
    assert result.expression_direction=='increased'
    assert result.disease_effect=='biomarker_only'
    assert result.needs_review


def test_fabricated_quote_is_rejected():
    assert ground_result({'evidence_sentence':'IL6 causes rheumatoid arthritis.'},CHUNK,'IL6','rheumatoid arthritis') is None


def test_missing_quote_is_not_replaced_with_a_convenient_sentence():
    assert ground_result({},CHUNK,'IL6','rheumatoid arthritis') is None


def test_model_cannot_forge_metadata_or_skip_review():
    row=ground_result({'evidence_sentence':CHUNK['context'],'paper_id':'PMID999','title':'Invented',
        'source_location':'page 700','needs_review':False,'confidence':.99},CHUNK,'IL6','rheumatoid arthritis')
    assert row['paper_id']=='PMID12345' and row['title']=='Trusted title'
    assert row['source_location']=='PMID 12345' and row['needs_review'] is True
    assert row['source_url']=='https://pubmed.ncbi.nlm.nih.gov/12345/'


def test_alias_accepted_without_substring_confusion():
    result=ExtractionResult(evidence_sentence='IL-6 expression was elevated in RA samples.',confidence=.9)
    checked=apply_relation_rules(result,'IL6','rheumatoid arthritis',['IL-6'],['RA'])
    assert 'gene_not_in_evidence' not in checked.notes
    assert 'disease_not_in_evidence' not in checked.notes


@pytest.mark.parametrize('value',[[],{},float('nan'),None])
def test_malformed_model_values_do_not_break_normalization(value):
    row=normalize_result({'expression_direction':value,'evidence_sentence':value,'confidence':value,'needs_review':'false'})
    assert row.expression_direction=='unclear'
    assert isinstance(row.evidence_sentence,str)
    assert 0<=row.confidence<=1
    assert row.needs_review is True
