import json
import sqlite3
import pytest
from corpus_store import CorpusStore


def record(i,context):
    return {'chunk_id':str(i),'paper_id':str(i),'context':context,'title':'Test'}


def test_gene_and_disease_hits_come_before_gene_only(tmp_path):
    path=tmp_path/'corpus.sqlite'
    CorpusStore.build(path,[record(1,'IL6 expression rheumatoid arthritis'),record(2,'IL6 expression another disease'),
                           record(3,'IL60 expression rheumatoid arthritis')])
    results=CorpusStore(path).search('IL6','rheumatoid arthritis',10)
    assert [r['chunk_id'] for r in results]==['1','2']


def test_aliases_and_query_operators_are_literal(tmp_path):
    path=tmp_path/'corpus.sqlite'
    CorpusStore.build(path,[record(1,'IL-6 expression rheumatoid arthritis')])
    assert len(CorpusStore(path).search('IL6','rheumatoid arthritis',10,['IL-6']))==1
    assert CorpusStore(path).search('IL6 OR TP53','rheumatoid arthritis',10)==[]


def test_streaming_build_and_atomic_failure(tmp_path):
    path=tmp_path/'corpus.sqlite'
    CorpusStore.build(path,(record(i,f'IL6 arthritis item {i}') for i in range(100)))
    with pytest.raises(sqlite3.IntegrityError):
        CorpusStore.build(path,[record(1,'old'),record(1,'duplicate')])
    assert len(CorpusStore(path).search('IL6','arthritis',100))==100


def test_readonly_search_does_not_create_missing_database(tmp_path):
    path=tmp_path/'absent.sqlite'
    with pytest.raises(sqlite3.OperationalError):
        CorpusStore(path).search('IL6','arthritis')
    assert not path.exists()
