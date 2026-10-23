"""Bounded PubMed fetch with query-specific cache and explicit failure states."""
from __future__ import annotations
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from io_utils import read_json, write_json
from query_utils import query_terms
from schemas import ChunkRecord
from text_utils import clean_text

_LOCK = threading.Lock()
_LAST_REQUEST = 0.0
EUTILS_BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'


def build_pubmed_query(gene, disease, gene_aliases=None, disease_aliases=None, *, focused=True):
    def group(terms):
        return '(' + ' OR '.join('"' + term.replace('"', ' ') + '"[Title/Abstract]' for term in terms) + ')'
    query = group(query_terms(gene, gene_aliases)) + ' AND ' + group(query_terms(disease, disease_aliases))
    if focused:
        query += ' AND (expression OR upregulated OR overexpression OR elevated OR downregulated OR risk OR progression OR prognosis)'
    return query, query


def abstracts_to_chunks(records):
    return [ChunkRecord(f"PMID{r['pmid']}_abstract",f"PMID{r['pmid']}",f"PMID{r['pmid']}",
        'pubmed_abstract',None,'abstract',r['abstract'],r['abstract'],f"PMID {r['pmid']}",r['title']).to_dict()
        for r in records]


class PubMedError(RuntimeError):
    pass


@dataclass
class PubMedResult:
    chunks: list[dict]
    status: str
    query_url: str
    matched_count: int
    without_abstract: int = 0


def _request(endpoint, params):
    global _LAST_REQUEST
    body = urllib.parse.urlencode(params).encode()
    for attempt in range(2):
        try:
            with _LOCK:
                delay = max(0, .36 - (time.monotonic() - _LAST_REQUEST))
                if delay:
                    time.sleep(delay)
                _LAST_REQUEST = time.monotonic()
            request = urllib.request.Request(f'{EUTILS_BASE}/{endpoint}', data=body,
                headers={'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'GeneEvidence/0.3'})
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read(8_000_001)
                if len(data) > 8_000_000:
                    raise PubMedError('PubMed 返回内容超限。')
                return data.decode('utf-8')
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError) as exc:
            if attempt == 1:
                raise PubMedError('PubMed 暂时无法访问，请稍后重试。') from exc
            time.sleep(1)
    raise PubMedError('PubMed 请求失败。')


def fetch(gene, disease, cache_dir: Path, max_results=20, gene_aliases=None, disease_aliases=None):
    if not 1 <= max_results <= 20:
        raise ValueError('PubMed result limit must be 1..20')
    query, _ = build_pubmed_query(gene, disease, gene_aliases=gene_aliases or [], disease_aliases=disease_aliases or [])
    query_url = 'https://pubmed.ncbi.nlm.nih.gov/?term=' + urllib.parse.quote(query)
    key = hashlib.sha256(json.dumps([query, max_results, 'v2']).encode()).hexdigest()
    path = cache_dir / f'{key}.json'
    if path.exists() and time.time() - path.stat().st_mtime < 86400:
        try:
            cached = read_json(path)
            return PubMedResult(cached['chunks'], cached['status'], query_url,
                cached['matched_count'], cached.get('without_abstract', 0))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    try:
        result = json.loads(_request('esearch.fcgi', {'db':'pubmed','term':query,'retmode':'json','retmax':max_results}))['esearchresult']
        if result.get('ERROR') or 'count' not in result or not isinstance(result.get('idlist'), list):
            raise ValueError('Invalid search response')
        count = int(result['count'])
        ids = result['idlist'][:max_results]
        if not ids:
            if count:
                raise ValueError('Search reported matches without identifiers')
            return PubMedResult([], 'no_results', query_url, count)
        root = ET.fromstring(_request('efetch.fcgi', {'db':'pubmed','id':','.join(ids),'retmode':'xml'}))
        if root.find('.//ERROR') is not None:
            raise ValueError('PubMed fetch error')
    except (KeyError, ValueError, TypeError, ET.ParseError) as exc:
        raise PubMedError('PubMed 返回了无法解析的数据，请重试。') from exc
    records, returned_ids, without_abstract = [], set(), 0
    for article in root.findall('.//PubmedArticle'):
        pmid = article.findtext('.//MedlineCitation/PMID') or ''
        returned_ids.add(pmid)
        title_node = article.find('.//ArticleTitle')
        title = clean_text(''.join(title_node.itertext())) if title_node is not None else ''
        abstract = clean_text(' '.join(''.join(n.itertext()) for n in article.findall('.//AbstractText')))
        if not abstract:
            without_abstract += 1
            continue
        if pmid.isdigit():
            records.append({'pmid':pmid, 'title':title, 'abstract':abstract})
    partial = not set(ids).issubset(returned_ids)
    if partial and not records:
        raise PubMedError('PubMed 未返回请求的文献，请重试。')
    status = 'partial_fetch' if partial else ('success' if records else 'no_abstracts')
    chunks = abstracts_to_chunks(records)
    if not partial:
        write_json(path, {'chunks':chunks, 'status':status, 'matched_count':count, 'without_abstract':without_abstract})
    return PubMedResult(chunks, status, query_url, count, without_abstract)
