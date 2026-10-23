import json
import urllib.error
import pytest
from openpyxl import load_workbook
from llm_client import LLMClient
from exports import write_exports
import pubmed_gateway


def test_network_failure_has_bounded_retries_and_no_rule_fallback(tmp_path,monkeypatch):
    import llm_client
    monkeypatch.setattr(llm_client,'get_llm_settings',lambda:dict(api_key='test',base_url='https://invalid.test',
        model='test',timeout=1,cache_enabled=True))
    monkeypatch.setattr(llm_client.time,'sleep',lambda _:None)
    def fail(*args,**kwargs):
        raise urllib.error.URLError('unavailable')
    monkeypatch.setattr(llm_client.urllib.request,'urlopen',fail)
    client=LLMClient(cache_path=tmp_path/'cache.sqlite',allow_fallback=False)
    chunks=[{'chunk_id':'1','context':'IL6 expression is elevated in arthritis.','sentence':'IL6 expression is elevated in arthritis.'}]
    results=client.extract_batch('IL6','arthritis',chunks,max_workers=1,retry=1)
    assert results==[(chunks[0],[])]
    assert client.failure_count==1 and client.request_count==2


def test_spreadsheet_cells_do_not_execute_formulas(tmp_path):
    write_exports(tmp_path,[{'title':'=HYPERLINK("https://invalid.test")','notes':'  +SUM(1,1)','gene_or_target':'IL6'}])
    book=load_workbook(tmp_path/'evidence.xlsx')
    for cell in list(book.active.rows)[1]:
        assert cell.data_type!='f'
    assert "'=HYPERLINK" in (tmp_path/'evidence.csv').read_text(encoding='utf-8-sig')


def test_pubmed_cache_includes_requested_limit_and_failures_not_cached(tmp_path,monkeypatch):
    calls=[]
    def response(endpoint,params):
        calls.append(params)
        if endpoint=='esearch.fcgi':
            return json.dumps({'esearchresult':{'count':'1','idlist':['123']}})
        return '<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article><ArticleTitle>A <i>title</i></ArticleTitle><Abstract><AbstractText>IL6 expression in arthritis.</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>'
    monkeypatch.setattr(pubmed_gateway,'_request',response)
    first=pubmed_gateway.fetch('IL6','arthritis',tmp_path,max_results=5)
    assert first.status=='success' and first.chunks[0]['title']=='A title'
    pubmed_gateway.fetch('IL6','arthritis',tmp_path,max_results=5)
    assert len(calls)==2
    pubmed_gateway.fetch('IL6','arthritis',tmp_path,max_results=10)
    assert len(calls)==4
    bad=tmp_path/'bad'
    monkeypatch.setattr(pubmed_gateway,'_request',lambda *a:'{}')
    with pytest.raises(pubmed_gateway.PubMedError):
        pubmed_gateway.fetch('IL6','arthritis',bad)
    assert not list(bad.glob('*.json'))


def test_partial_pubmed_fetch_not_cached_as_complete(tmp_path,monkeypatch):
    def response(endpoint,params):
        if endpoint=='esearch.fcgi':
            return json.dumps({'esearchresult':{'count':'2','idlist':['123','456']}})
        return '<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article><Abstract><AbstractText>IL6 expression in arthritis.</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>'
    monkeypatch.setattr(pubmed_gateway,'_request',response)
    result=pubmed_gateway.fetch('IL6','arthritis',tmp_path)
    assert result.status=='partial_fetch'
    assert not list(tmp_path.glob('*.json'))


def test_non_object_model_array_is_failure(tmp_path,monkeypatch):
    import llm_client
    monkeypatch.setattr(llm_client,'get_llm_settings',lambda:dict(api_key='test',base_url='https://invalid.test',
        model='test',timeout=1,cache_enabled=False))
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return json.dumps({'choices':[{'message':{'content':'[null]'}}]}).encode()
    monkeypatch.setattr(llm_client.urllib.request,'urlopen',lambda *args,**kwargs:Response())
    client=LLMClient(allow_fallback=False)
    assert client.extract('IL6','arthritis',{'context':'IL6 expression in arthritis.'})==[]
    assert client.failure_count==1


def test_pubmed_incomplete_search_is_failure(tmp_path,monkeypatch):
    monkeypatch.setattr(pubmed_gateway,'_request',lambda *args:json.dumps({'esearchresult':{'count':'2','idlist':[]}}))
    with pytest.raises(pubmed_gateway.PubMedError):
        pubmed_gateway.fetch('IL6','arthritis',tmp_path)


@pytest.mark.parametrize('finish_reason',['stop','length'])
def test_model_usage_response_version_and_truncation_are_recorded(monkeypatch,finish_reason):
    import llm_client
    monkeypatch.setattr(llm_client,'get_llm_settings',lambda:dict(api_key='test',base_url='https://invalid.test',
        model='model-alias',timeout=1,cache_enabled=False))
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self):
            return json.dumps(dict(model='response-version',usage=dict(prompt_tokens=12,completion_tokens=3,total_tokens=15),
                choices=[dict(finish_reason=finish_reason,message=dict(content='[]'))])).encode()
    def respond(request,**kwargs):
        assert json.loads(request.data)['max_tokens']==1024
        return Response()
    monkeypatch.setattr(llm_client.urllib.request,'urlopen',respond)
    client=LLMClient(allow_fallback=False,max_output_tokens=1024)
    assert client.extract('IL6','arthritis',dict(chunk_id='c',context='IL6 expression in arthritis.'))==[]
    assert client.usage['total_tokens']==15 and client.response_models=={'response-version'}
    assert client.failure_count==(1 if finish_reason=='length' else 0)


def test_explicit_non_thinking_request_is_bounded_and_separates_cache(tmp_path,monkeypatch):
    import llm_client
    monkeypatch.setattr(llm_client,'get_llm_settings',lambda:dict(api_key='test',base_url='https://api.deepseek.com',
        model='test',timeout=1,cache_enabled=True))
    payloads=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return b'{"choices":[{"finish_reason":"stop","message":{"content":"[]"}}]}'
    def respond(request,**kwargs):
        payloads.append(json.loads(request.data));return Response()
    monkeypatch.setattr(llm_client.urllib.request,'urlopen',respond)
    chunk=dict(chunk_id='c',context='IL6 expression in rheumatoid arthritis.')
    for thinking in [None,False,False]:
        client=LLMClient(cache_path=tmp_path/'cache.sqlite',allow_fallback=False,max_output_tokens=1024,thinking=thinking)
        assert client.extract('IL6','rheumatoid arthritis',chunk)==[]
    assert len(payloads)==2
    assert 'thinking' not in payloads[0]
    assert payloads[1]['thinking']=={'type':'disabled'} and payloads[1]['max_tokens']==1024
