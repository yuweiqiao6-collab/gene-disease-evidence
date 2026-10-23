"""Independent, bounded PubMed discovery. No model import or model calls."""
from __future__ import annotations
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from io_utils import read_json, write_json
import pubmed_gateway as gateway
from text_utils import clean_text


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_doi(value):
    value=str(value or '').strip().lower()
    value=re.sub(r'^https?://(?:dx\.)?doi\.org/','',value)
    value=re.sub(r'^doi:\s*','',value)
    return value if re.fullmatch(r'10\.\d{4,9}/\S+',value) else ''


def parse_articles(xml,requested):
    if '<!ENTITY' in xml.upper(): raise gateway.PubMedError('文献响应包含不支持的实体声明。')
    try: root=ET.fromstring(xml)
    except ET.ParseError as exc: raise gateway.PubMedError('PubMed 文献响应无法解析。') from exc
    if root.tag!='PubmedArticleSet' or root.find('.//ERROR') is not None:
        raise gateway.PubMedError('PubMed 文献响应无效。')
    papers={}
    for article in root.findall('./PubmedArticle'):
        pmid=(article.findtext('./MedlineCitation/PMID') or '').strip()
        if pmid not in requested or not pmid.isdigit():
            raise gateway.PubMedError('PubMed 返回了不属于本次请求的论文，已拒绝绑定。')
        def text(path):
            node=article.find(path)
            return clean_text(''.join(node.itertext())) if node is not None else ''
        identifiers={n.get('IdType'):clean_text(n.text or '') for n in article.findall('./PubmedData/ArticleIdList/ArticleId')}
        pmcid=identifiers.get('pmc','').upper()
        if pmcid and not re.fullmatch(r'PMC\d+',pmcid): pmcid=''
        parts=[]
        for node in article.findall('./MedlineCitation/Article/Abstract/AbstractText'):
            content=clean_text(''.join(node.itertext()))
            if content:
                label=node.get('Label','')
                parts.append((label+': ' if label else '')+content)
        abstract='\n'.join(parts)
        title=text('./MedlineCitation/Article/ArticleTitle')
        if not title: raise gateway.PubMedError('PubMed 论文缺少标题。')
        authors=[]
        for author in article.findall('./MedlineCitation/Article/AuthorList/Author'):
            authors.append(clean_text(' '.join(filter(None,[author.findtext('ForeName'),author.findtext('LastName'),author.findtext('CollectiveName')]))))
        papers[pmid]={'paper_id':'PMID'+pmid,'pmid':pmid,'pmcid':pmcid,
            'doi':normalize_doi(identifiers.get('doi','')),'title':title,'abstract':abstract,
            'authors':authors,'journal':text('./MedlineCitation/Article/Journal/Title'),
            'publication_types':[clean_text(n.text or '') for n in article.findall('./MedlineCitation/Article/PublicationTypeList/PublicationType')],
            'publication_date':text('./MedlineCitation/Article/Journal/JournalIssue/PubDate'),
            'source_url':f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
            'abstract_status':'available' if abstract else 'no_abstract',
            'source_type':'pubmed_abstract','content_level':'abstract',
            'abstract_sha256':hashlib.sha256(abstract.encode()).hexdigest(),
            'fulltext_status':'not_requested','retrieved_at':utc_now(),
            'sources':[{'provider':'pubmed','source_url':f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/'}]}
    return papers


def search(gene,disease,cache_dir:Path,*,max_results=40,gene_aliases=None,disease_aliases=None,focused=False,refresh=False):
    if not 1<=max_results<=100: raise ValueError('在线检索每次支持 1–100 篇。')
    query,_=gateway.build_pubmed_query(gene,disease,gene_aliases,disease_aliases,focused=focused)
    query_url='https://pubmed.ncbi.nlm.nih.gov/?term='+gateway.urllib.parse.quote(query)
    key=hashlib.sha256(json.dumps([query,max_results,'discovery-v1']).encode()).hexdigest()
    path=cache_dir/f'{key}.json'
    if not refresh and path.is_file() and time.time()-path.stat().st_mtime<86400:
        try:
            report=read_json(path)
            if report['complete'] is True and isinstance(report['papers'],list) and report['query']==query:
                if {p['pmid'] for p in report['papers']}!=set(report['requested_ids']) or len(report['papers'])!=len(report['requested_ids']):
                    raise ValueError('Cached identifier mismatch')
                # Revalidate cached content, not just the JSON shape.
                for p in report['papers']:
                    if not str(p['pmid']).isdigit() or p['paper_id']!='PMID'+p['pmid'] or p['abstract_sha256']!=hashlib.sha256(p['abstract'].encode()).hexdigest():
                        raise ValueError('Invalid cached record')
                return {**report,'cache_hit':True}
        except (KeyError,TypeError,ValueError): pass
    try:
        result=json.loads(gateway._request('esearch.fcgi',dict(db='pubmed',term=query,retmode='json',retmax=max_results,sort='relevance')))['esearchresult']
        if result.get('ERROR') or not isinstance(result.get('idlist'),list): raise ValueError('Invalid search response')
        count=int(result['count'])
        ids=result['idlist'][:max_results]
        if count<0 or any(not isinstance(i,str) or not i.isdigit() for i in ids) or len(ids)!=len(set(ids)) or len(ids)!=min(count,max_results):
            raise ValueError('Invalid identifier list')
    except (KeyError,TypeError,ValueError) as exc:
        raise gateway.PubMedError('PubMed 搜索响应不完整或无效。') from exc
    papers={};issues=[];page_reports=[]
    # Freeze the identifier set, then fetch in pages; no changing query rankings between pages.
    for offset in range(0,len(ids),20):
        page=ids[offset:offset+20]
        try:
            records=parse_articles(gateway._request('efetch.fcgi',dict(db='pubmed',id=','.join(page),retmode='xml')),set(page))
            missing=set(page)-set(records)
            papers.update(records)
            if missing: issues.append(f'第 {offset//20+1} 批缺少 {len(missing)} 篇记录。')
            page_reports.append({'offset':offset,'requested':len(page),'returned':len(records),'complete':not missing})
        except gateway.PubMedError:
            issues.append(f'第 {offset//20+1} 批获取失败；没有将失败解释为无文献。')
            page_reports.append({'offset':offset,'requested':len(page),'returned':0,'complete':False})
    complete=len(papers)==len(ids)
    report={'provider':'pubmed','query':query,'query_url':query_url,'searched_at':utc_now(),
        'matched_count':count,'requested_limit':max_results,'requested_ids':ids,
        'retrieved_count':len(papers),'truncated_by_limit':count>max_results,'complete':complete,
        'status':'no_results' if count==0 else ('completed' if complete else 'partial'),
        'focused':focused,'cache_hit':False,'pages':page_reports,'issues':issues,
        'papers':[papers[i] for i in ids if i in papers]}
    if complete: write_json(path,report)
    return report
