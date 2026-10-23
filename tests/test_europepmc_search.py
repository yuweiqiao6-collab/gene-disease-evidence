import io
import json
import urllib.parse
import pytest
from europepmc_search import parse,search,ENDPOINT
from pubmed_gateway import PubMedError
from conftest import wait_for
from jobs import JobManager,Query
from web_settings import WebSettings


def response():
    return {'hitCount':1,'resultList':{'result':[{'source':'MED','id':'123','pmcid':'PMC456',
        'title':'IL6 in rheumatoid arthritis','abstractText':'<p>Cases &amp; controls.</p><p>Results.</p>',
        'doi':'10.1000/unit','pubTypeList':{'pubType':['Review']}}]}}


def test_html_metadata_and_non_pubmed_identities():
    count,papers=parse(response());p=papers[0]
    assert count==1 and p['abstract']=='Cases & controls. Results.'
    assert p['pmid']=='123' and p['paper_id']=='PMID123' and p['publication_types']==['Review']
    data=response();data['resultList']['result'][0].update(source='PPR',id='PPR123',pmid='')
    preprint=parse(data)[1][0]
    assert preprint['paper_id']=='EPMC_PPR_PPR123' and preprint['publication_status']=='preprint'
    from literature_store import paper_chunks
    from source_quality import source_flags
    assert 'preprint' in source_flags(paper_chunks(preprint)[0])


def test_pubmed_abstract_retains_public_url_when_merged_with_local_identity():
    from literature_store import paper_chunks
    from validators import ground_result
    from test_literature import paper
    p=paper();p['paper_id']='LOCAL-PAPER'
    c=paper_chunks(p)[0]
    row=ground_result({'evidence_sentence':c['context']},c,'IL6','rheumatoid arthritis')
    assert row['paper_id']=='LOCAL-PAPER' and row['source_url']=='https://pubmed.ncbi.nlm.nih.gov/123/'


@pytest.mark.parametrize('invalid',[
    {},{'hitCount':-1,'resultList':{'result':[]}},
    {'hitCount':1,'resultList':{'result':[None]}},
    {'hitCount':1,'resultList':{'result':[{'source':'MED','id':'123','title':'x','pmcid':'bad'}]}},
    {'hitCount':1,'resultList':{'result':[{'source':'../escape','id':'123','title':'x'}]}}])
def test_invalid_response_is_not_no_evidence(invalid):
    with pytest.raises(PubMedError):parse(invalid)


def test_fixed_endpoint_bounded_query_and_cache(tmp_path,monkeypatch):
    calls=[]
    def request(req,timeout):
        calls.append(req.full_url)
        assert req.full_url.startswith(ENDPOINT+'?') and timeout==25
        params=urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        assert params['pageSize']==['3'] and params['resultType']==['core']
        return io.BytesIO(json.dumps(response()).encode())
    monkeypatch.setattr('urllib.request.urlopen',request)
    first=search('IL6','rheumatoid arthritis',tmp_path,max_results=3)
    second=search('IL6','rheumatoid arthritis',tmp_path,max_results=3)
    assert not first['cache_hit'] and second['cache_hit'] and len(calls)==1
    with pytest.raises(ValueError):search('IL6','arthritis',tmp_path,max_results=101)


def test_incomplete_response_or_timeout_is_never_cached_as_success(tmp_path,monkeypatch):
    data=response();data['hitCount']=4
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k:io.BytesIO(json.dumps(data).encode()))
    with pytest.raises(PubMedError,match='完整'):search('IL6','arthritis',tmp_path,max_results=3)
    assert not list(tmp_path.glob('*.json'))
    def fail(*a,**k):raise TimeoutError('unit timeout')
    monkeypatch.setattr('urllib.request.urlopen',fail)
    with pytest.raises(PubMedError,match='失败'):search('IL6','arthritis',tmp_path,max_results=3)


def test_pubmed_and_europepmc_merge_and_record_partial_source_failure(tmp_path,monkeypatch,owner):
    import jobs,literature_search
    from test_literature import result,paper
    monkeypatch.setattr(literature_search,'search',lambda *a,**k:result([paper()]))
    data=response();data['resultList']['result'][0].update(pmcid='PMC123',doi='10.1234/test123')
    monkeypatch.setattr(jobs.europepmc_search,'search',lambda *a,**k:dict(result(parse(data)[1]),provider='europepmc'))
    manager=JobManager(WebSettings(runtime_dir=tmp_path))
    try:
        query=Query(('IL6',),'rheumatoid arthritis',source='pubmed',action='search',include_europepmc=True)
        task=manager.submit(owner,query);snapshot=wait_for(manager,owner,task)
        assert snapshot['state']=='completed' and len(snapshot['documents'])==1
        document=manager.literature.get(owner,snapshot['documents'][0]['document_id'])
        assert {s['provider'] for s in document['sources']}=={'pubmed','europepmc'}
        def fail(*a,**k):raise PubMedError('Europe PMC暂不可用')
        monkeypatch.setattr(jobs.europepmc_search,'search',fail)
        task=manager.submit(owner,query);snapshot=wait_for(manager,owner,task)
        assert snapshot['state']=='partial' and snapshot['provenance']['europepmc']['complete'] is False
        assert snapshot['counts']['model_calls']==0 and snapshot['documents']
    finally:manager.close()
