"""Bounded Europe PMC article discovery; no arbitrary URLs or model requests."""
import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from html.parser import HTMLParser
from pathlib import Path
from io_utils import read_json,write_json
from query_utils import query_terms
from literature_search import normalize_doi,utc_now
from pubmed_gateway import PubMedError

ENDPOINT='https://www.ebi.ac.uk/europepmc/webservices/rest/search'

class PlainText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[]
    def handle_data(self,data):self.parts.append(data)
    def handle_starttag(self,tag,attrs):
        if tag in {'p','br','div'}:self.parts.append(' ')

def plain(value):
    parser=PlainText();parser.feed(str(value or ''))
    return re.sub(r'\s+',' ',''.join(parser.parts)).strip()

def parse(data):
    try:
        count=int(data['hitCount']);records=data['resultList']['result']
        if count<0 or not isinstance(records,list):raise ValueError()
    except (TypeError,ValueError,KeyError) as exc:raise PubMedError('Europe PMC搜索响应无效。') from exc
    papers=[];seen=set()
    for record in records:
        if not isinstance(record,dict):raise PubMedError('Europe PMC文献记录格式无效。')
        source=str(record.get('source',''));identity=str(record.get('id',''))
        if not re.fullmatch(r'[A-Z]{2,12}',source) or not identity or len(identity)>150:
            raise PubMedError('Europe PMC文献缺少可核对编号。')
        key=(source,identity)
        if key in seen:raise PubMedError('Europe PMC返回重复编号。')
        seen.add(key)
        pmid=identity if source=='MED' and identity.isdigit() else str(record.get('pmid',''))
        if pmid and not pmid.isdigit():raise PubMedError('Europe PMC返回无效PubMed编号。')
        pmcid=str(record.get('pmcid',''))
        if pmcid and not re.fullmatch(r'PMC\d+',pmcid):raise PubMedError('Europe PMC返回无效全文编号。')
        title=plain(record.get('title'));abstract=plain(record.get('abstractText'))
        if not title:raise PubMedError('Europe PMC文献缺少标题。')
        url='https://europepmc.org/article/'+source+'/'+urllib.parse.quote(identity,safe='')
        types=record.get('pubTypeList') or {}
        if not isinstance(types,dict) or not isinstance(types.get('pubType',[]),list):raise PubMedError('Europe PMC出版类型字段无效。')
        papers.append({'paper_id':'PMID'+pmid if pmid else 'EPMC_'+source+'_'+identity,
            'pmid':pmid,'pmcid':pmcid,'doi':normalize_doi(record.get('doi','')),'title':title,'abstract':abstract,
            'abstract_sha256':hashlib.sha256(abstract.encode()).hexdigest(),'abstract_status':'available' if abstract else 'no_abstract',
            'source_type':'europepmc_abstract','content_level':'abstract','source_url':url,'fulltext_status':'not_requested',
            'retrieved_at':utc_now(),'publication_types':types.get('pubType',[]),
            'publication_status':'preprint' if source=='PPR' else 'not_assessed',
            'sources':[{'provider':'europepmc','source_url':url,'source':source,'id':identity}]})
    return count,papers

def search(gene,disease,cache_dir:Path,*,max_results=40,gene_aliases=None,disease_aliases=None,focused=False,refresh=False):
    if not 1<=max_results<=100:raise ValueError('Europe PMC每次最多100篇。')
    def group(terms):return '('+' OR '.join('TITLE_ABS:"'+v.replace('"',' ')+'"' for v in terms)+')'
    query=group(query_terms(gene,gene_aliases))+' AND '+group(query_terms(disease,disease_aliases))
    if focused:query+=' AND (expression OR abundance OR upregulated OR downregulated)'
    params={'query':query,'format':'json','resultType':'core','pageSize':max_results,'cursorMark':'*'}
    path=cache_dir/(hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()+'.json')
    if not refresh and path.is_file() and time.time()-path.stat().st_mtime<86400:
        try:
            data=read_json(path);count,papers=parse(data)
            if len(papers)!=min(count,max_results):raise ValueError('Incomplete cached result')
            return report(query,count,papers,max_results,True)
        except (ValueError,KeyError,TypeError,PubMedError):pass
    request=urllib.request.Request(ENDPOINT+'?'+urllib.parse.urlencode(params),headers={'User-Agent':'GeneEvidence/0.8'})
    try:
        with urllib.request.urlopen(request,timeout=25) as response:
            raw=response.read(8_000_001)
            if len(raw)>8_000_000:raise PubMedError('Europe PMC响应超出大小上限。')
        data=json.loads(raw.decode());count,papers=parse(data)
    except (OSError,TimeoutError,ValueError,UnicodeError) as exc:raise PubMedError('Europe PMC检索失败，没有将失败解释为无文献。') from exc
    if len(papers)!=min(count,max_results):raise PubMedError('Europe PMC未返回完整的本批候选，未缓存为成功。')
    write_json(path,data)
    return report(query,count,papers,max_results,False)

def report(query,count,papers,maximum,cache_hit):
    return {'provider':'europepmc','query':query,'query_url':'https://europepmc.org/search?query='+urllib.parse.quote(query),
        'matched_count':count,'retrieved_count':len(papers),'requested_limit':maximum,'truncated_by_limit':count>maximum,
        'complete':True,'cache_hit':cache_hit,'papers':papers,'issues':[],'searched_at':utc_now()}
