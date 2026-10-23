"""Identity-checked Europe PMC open-access XML, paragraph-level provenance."""
from __future__ import annotations
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from io_utils import write_json,read_json
from literature_search import normalize_doi,utc_now
from text_utils import clean_text

BASE='https://www.ebi.ac.uk/europepmc/webservices/rest'
_LOCK=threading.Lock()
_LAST=0.0


class FulltextError(RuntimeError): pass


def _request(path,params=None):
    global _LAST
    # Caller constructs only fixed service paths; publisher-provided URLs are never fetched.
    if not (path=='search' or re.fullmatch(r'PMC\d+/fullTextXML',path)):
        raise ValueError('不支持的全文服务路径。')
    url=BASE+'/'+path+('?' + urllib.parse.urlencode(params) if params else '')
    with _LOCK:
        delay=max(0,.36-(time.monotonic()-_LAST))
        if delay: time.sleep(delay)
        _LAST=time.monotonic()
    try:
        request=urllib.request.Request(url,headers={'User-Agent':'GeneEvidence/0.3','Accept':'application/xml, application/json'})
        with urllib.request.urlopen(request,timeout=20) as response: data=response.read(5_000_001)
        if len(data)>5_000_000: raise FulltextError('全文响应超过 5 MB，未保存为完整正文。')
        return data
    except urllib.error.HTTPError as exc:
        if exc.code in {404,410}: raise FulltextError('全文接口未返回可获取的正文。') from exc
        raise FulltextError('全文服务请求失败，请稍后重试。') from exc
    except (urllib.error.URLError,TimeoutError,OSError) as exc:
        raise FulltextError('全文服务暂时无法访问，请稍后重试。') from exc


def _identity(paper,metadata):
    matches=0
    for field in ['pmid','pmcid','doi']:
        expected=paper.get(field,'')
        actual=metadata.get(field,'')
        if field=='doi': expected,actual=normalize_doi(expected),normalize_doi(actual)
        if expected and actual:
            if str(expected)!=str(actual): raise FulltextError('论文标识不一致，拒绝绑定全文。')
            matches+=1
    if not matches: raise FulltextError('缺少可核对的论文标识，拒绝绑定全文。')


def parse_fulltext(data,paper,pmcid):
    if b'<!ENTITY' in data.upper(): raise FulltextError('全文包含不支持的实体声明。')
    try: root=ET.fromstring(data)
    except ET.ParseError as exc: raise FulltextError('全文 XML（结构化文本）无法解析。') from exc
    # Strip namespace prefixes so standard JATS and namespaced variants behave alike.
    for node in root.iter(): node.tag=node.tag.split('}')[-1]
    if root.tag!='article': raise FulltextError('全文不是可识别的论文记录。')
    identifiers={n.get('pub-id-type'):clean_text(n.text or '') for n in root.findall('./front/article-meta/article-id')}
    actual_pmc=identifiers.get('pmc') or identifiers.get('pmcid') or ''
    if actual_pmc.isdigit(): actual_pmc='PMC'+actual_pmc
    metadata={'pmid':identifiers.get('pmid',''),'pmcid':actual_pmc,'doi':normalize_doi(identifiers.get('doi',''))}
    _identity({**paper,'pmcid':pmcid},metadata)
    if actual_pmc and actual_pmc!=pmcid: raise FulltextError('全文编号不一致。')
    body=root.find('./body')
    if body is None: raise FulltextError('未找到正文，保留原来的摘要。')
    sha=hashlib.sha256(data).hexdigest()
    url=f'https://europepmc.org/articles/{pmcid}'
    chunks=[];total=0;paragraph_count=0;omitted=set()
    def walk(node,path):
        nonlocal total,paragraph_count
        if node.tag in {'fig','table-wrap','ref-list','supplementary-material'}:
            omitted.add(node.tag);return
        if node.tag=='sec':
            title=node.find('./title')
            label=clean_text(''.join(title.itertext())) if title is not None else node.get('sec-type','未命名章节')
            path=path+[label]
        if node.tag=='p':
            text=clean_text(''.join(node.itertext()))
            if not text: return
            total+=len(text)
            if total>1_000_000 or len(chunks)>=1000: raise FulltextError('正文超过本次解析上限，未当作完整正文保存。')
            paragraph_count+=1
            number=paragraph_count
            # Bound individual model contexts, retaining overlap without cutting source provenance.
            for start in range(0,len(text),5700):
                context=text[start:start+6000]
                if len(chunks)>=1000: raise FulltextError('正文片段超过本次解析上限。')
                section=' / '.join(path) or '正文'
                chunks.append({'chunk_id':f'{pmcid}_{sha[:12]}_p{number}_{start}',
                    'paper_id':paper.get('paper_id') or pmcid,'title':paper.get('title',''),
                    'source_type':'europepmc_fulltext','content_level':'fulltext','section':section,
                    'paragraph':number,'page':None,'sentence':context,'context':context,
                    'source_location':f'{pmcid} / {section} / 第 {number} 段',
                    'source_url':url,'fulltext_sha256':sha,'pmcid':pmcid,
                    'pmid':paper.get('pmid',''),'doi':paper.get('doi','')})
            return
        for child in node: walk(child,path)
    walk(body,[])
    if not chunks: raise FulltextError('正文没有可读取段落，保留摘要。')
    permissions=root.find('./front/article-meta/permissions')
    license_text=clean_text(' '.join(permissions.itertext())) if permissions is not None else ''
    license_text=license_text or '接口标记为开放全文；原文未提供可解析的许可声明，请核对来源。'
    for chunk in chunks: chunk['fulltext_license']=license_text
    return {'fulltext_status':'available','pmcid':pmcid,'fulltext_sha256':sha,
        'fulltext_source_url':url,'fulltext_api_url':BASE+'/'+pmcid+'/fullTextXML',
        'fulltext_retrieved_at':utc_now(),'fulltext_license':license_text,
        'fulltext_chunks':chunks,'fulltext_scope':'body_paragraphs',
        'fulltext_omitted_elements':sorted(omitted),'fulltext_message':'已获取正文段落；图片、表格与补充材料不作为已解析证据。'}


def fetch(paper,cache_dir:Path,*,refresh=False):
    pmid=str(paper.get('pmid',''))
    pmcid=str(paper.get('pmcid',''))
    doi=normalize_doi(paper.get('doi',''))
    if pmid.isdigit(): query=f'EXT_ID:{pmid} AND SRC:MED'
    elif re.fullmatch(r'PMC\d+',pmcid): query=f'PMCID:{pmcid}'
    elif doi: query='DOI:"'+doi.replace('"','')+'"'
    else: return {'fulltext_status':'unavailable','fulltext_message':'没有可用于查找开放全文的论文编号。'}
    try:
        response=json.loads(_request('search',dict(query=query,format='json',resultType='core',pageSize=2)))
        hits=response['resultList']['result']
        if not isinstance(hits,list): raise ValueError('Invalid metadata')
        if not hits:
            return {'fulltext_status':'unavailable','fulltext_message':'Europe PMC 未找到对应记录；不代表论文不存在。'}
        if len(hits)!=1: raise FulltextError('全文来源有多个候选，暂不自动绑定。')
        record=hits[0]
        _identity(paper,{'pmid':record.get('pmid') or (record.get('id') if record.get('source')=='MED' else ''),
            'pmcid':record.get('pmcid',''),'doi':record.get('doi','')})
        pmcid=record.get('pmcid','')
        if record.get('isOpenAccess')!='Y' or not re.fullmatch(r'PMC\d+',str(pmcid)):
            return {'fulltext_status':'unavailable','fulltext_message':'没有可通过本接口获取的开放全文，保留摘要；不代表论文没有全文。'}
        cache_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        cache=cache_dir/(pmcid+'.json')
        data=None
        if not refresh and cache.is_file() and time.time()-cache.stat().st_mtime<86400:
            try:
                cached=read_json(cache)
                candidate=cached['xml'].encode()
                if hashlib.sha256(candidate).hexdigest()==cached['sha256']: data=candidate
            except (KeyError,TypeError,ValueError): pass
        if data is None: data=_request(pmcid+'/fullTextXML')
        result=parse_fulltext(data,paper,pmcid)
        write_json(cache,{'xml':data.decode('utf-8'),'sha256':result['fulltext_sha256']})
        return result
    except (KeyError,TypeError,ValueError,UnicodeError) as exc:
        raise FulltextError('全文来源响应格式无效，保留摘要。') from exc
