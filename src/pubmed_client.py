"""PubMed client with caching, alias limiting, and timeout handling.

Wraps 05_pubmed_search logic with:
  - automatic alias truncation to avoid HTTP 414
  - timeout handling with graceful fallback
  - disk cache keyed by (gene, disease)
  - optional async/concurrent fetching
"""

from __future__ import annotations

import json
import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from alias_lookup import lookup_gene_aliases, lookup_disease_aliases
from cache_manager import get_pubmed_cache_path, pubmed_cache_exists, register_pubmed_cache
from config import ensure_directories
from io_utils import read_jsonl, write_jsonl
from schemas import ChunkRecord
from text_utils import clean_text


EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PUBMED_WEB_BASE = "https://pubmed.ncbi.nlm.nih.gov/"

# Tunable constants
_MAX_ALIASES = 5        # keep PubMed query length reasonable
_REQUEST_TIMEOUT = 30   # seconds per HTTP request
_MAX_PMIDS = 50         # default max abstracts to fetch


def _build_field_group(terms: list[str]) -> str:
    parts = [f'"{t}"[Title/Abstract]' for t in terms if t.strip()]
    return " OR ".join(parts)


def build_pubmed_query(
    gene: str,
    disease: str,
    use_aliases: bool = True,
    max_aliases: int = _MAX_ALIASES,
    gene_aliases: list[str] | None = None,
    disease_aliases: list[str] | None = None,
) -> tuple[str, str]:
    """Build PubMed query string. Returns (api_query, web_query)."""
    gene_terms = [gene]
    disease_terms = [disease]
    if use_aliases:
        # Reuse aliases already resolved by the caller. The old implementation
        # queried HGNC/disease services a second time for every PubMed search.
        resolved_gene_aliases = gene_aliases if gene_aliases is not None else lookup_gene_aliases(gene)
        resolved_disease_aliases = disease_aliases if disease_aliases is not None else lookup_disease_aliases(disease)
        gene_terms.extend(resolved_gene_aliases[:max_aliases])
        disease_terms.extend(resolved_disease_aliases[:max_aliases])

    gene_group = _build_field_group(gene_terms)
    disease_group = _build_field_group(disease_terms)
    keyword_group = "(expression OR upregulated OR overexpression OR elevated OR downregulated OR risk OR progression OR prognosis)"

    api_query = f"({gene_group}) AND ({disease_group}) AND {keyword_group}"
    web_query = api_query
    return api_query, web_query


def _fetch_json(url: str, data: bytes | None = None, timeout: int = _REQUEST_TIMEOUT) -> dict[str, Any]:
    req = urllib.request.Request(url, data=data, headers={} if data else None)
    if data:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def search_pubmed(query: str, max_results: int = _MAX_PMIDS) -> list[str]:
    params = urllib.parse.urlencode(
        {"db": "pubmed", "term": query, "retmode": "json", "retmax": str(max_results)}
    )
    url = f"{EUTILS_BASE}/esearch.fcgi"
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            payload = _fetch_json(url, data=params.encode("utf-8"))
            result = payload.get("esearchresult")
            if not isinstance(result, dict):
                raise RuntimeError("PubMed response missing esearchresult")
            backend_error = result.get("ERROR") or result.get("error")
            if backend_error:
                raise RuntimeError(f"PubMed backend error: {backend_error}")
            if "idlist" not in result or "count" not in result:
                raise RuntimeError(f"PubMed response incomplete: {result}")
            return list(result.get("idlist") or [])
        except urllib.error.HTTPError as exc:
            if exc.code == 414:
                print("ERROR: HTTP 414 (Request-URI Too Long). Reduce aliases or max_results.")
            last_error = exc
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
        if attempt < 3:
            print(f"WARNING: PubMed search attempt {attempt}/3 failed: {last_error}")
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"PubMed search failed after 3 attempts: {last_error}")


class AbstractRecords(list):
    pass


def fetch_pubmed_abstracts(pmids: list[str], batch_size: int = 200) -> list[dict[str, Any]]:
    if not pmids:
        return []

    records = AbstractRecords()
    received=set(); failed_batches=0; no_abstract=[]
    for i in range(0, len(pmids), batch_size):
        batch = pmids[i : i + batch_size]
        params = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(batch), "retmode": "xml"})
        try:
            with urllib.request.urlopen(
                f"{EUTILS_BASE}/efetch.fcgi?{params}", timeout=_REQUEST_TIMEOUT
            ) as response:
                xml_text = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"WARNING: PubMed efetch failed for batch {i//batch_size + 1}: {exc}")
            failed_batches+=1
            continue

        try:root = ET.fromstring(xml_text)
        except ET.ParseError:
            failed_batches+=1
            continue
        for article in root.findall(".//PubmedArticle"):
            pmid = article.findtext(".//PMID") or ""
            if pmid not in batch or pmid in received:continue
            received.add(pmid)
            title = clean_text(article.findtext(".//ArticleTitle") or "")
            abstract_parts = [
                clean_text("".join(node.itertext())) for node in article.findall(".//AbstractText")
            ]
            abstract = clean_text(" ".join(part for part in abstract_parts if part))
            doi = ""
            for article_id in article.findall(".//ArticleId"):
                if article_id.attrib.get("IdType") == "doi":
                    doi = article_id.text or ""
                    break
            if abstract:
                records.append({"pmid": pmid, "title": title, "abstract": abstract, "doi": doi})
            else:no_abstract.append(pmid)
    records.fetch_report={"complete":not failed_batches and set(pmids)==received, "failed_batches":failed_batches,
        "missing_ids":sorted(set(pmids)-received),"no_abstract_ids":no_abstract}
    return records


def abstracts_to_chunks(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunks = []
    for record in records:
        pmid = record.get("pmid", "")
        title = record.get("title", "")
        context = record.get("abstract", "")
        chunks.append(
            ChunkRecord(
                chunk_id=f"PMID{pmid}_abstract",
                paper_id=f"PMID{pmid}",
                file_name=f"PMID{pmid}",
                source_type="pubmed_abstract",
                page=None,
                section="abstract",
                sentence=context,
                context=context,
                source_location=f"PMID {pmid}",
                title=title,
            ).to_dict()
        )
    return chunks


def fetch_pubmed_for_query(
    gene: str,
    disease: str,
    max_results: int = _MAX_PMIDS,
    use_aliases: bool = True,
    max_aliases: int = _MAX_ALIASES,
    force_refresh: bool = False,
    gene_aliases: list[str] | None = None,
    disease_aliases: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str, str]:
    """Fetch PubMed abstracts for a gene-disease query, using cache when available.

    Returns:
        (chunks, web_query, web_url)
    """
    ensure_directories()

    api_query, web_query = build_pubmed_query(
        gene, disease, use_aliases, max_aliases, gene_aliases, disease_aliases)
    cache_signature = hashlib.sha256(json.dumps([api_query, max_results]).encode()).hexdigest()
    # Complete query identity; legacy files remain untouched.
    cache_path = get_pubmed_cache_path(gene, disease, cache_signature)
    if not force_refresh and pubmed_cache_exists(gene, disease, signature=cache_signature):
        fetch_pubmed_for_query.last_status = "cache_hit"
        print(f"[pubmed] Cache hit for {gene} + {disease}.")
        chunks = read_jsonl(cache_path)
        api_query, web_query = build_pubmed_query(
            gene, disease, use_aliases, max_aliases, gene_aliases, disease_aliases
        )
        web_url = f"{PUBMED_WEB_BASE}?term={urllib.parse.quote(web_query)}"
        return chunks, web_query, web_url

    api_query, web_query = build_pubmed_query(
        gene, disease, use_aliases, max_aliases, gene_aliases, disease_aliases
    )
    print(f"[pubmed] Searching: {web_query[:120]}...")

    try:
        pmids = search_pubmed(api_query, max_results=max_results)
    except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        fetch_pubmed_for_query.last_status = "query_failed"
        print(f"WARNING: PubMed search failed: {exc}. Returning empty.")
        return [], web_query, ""

    if not pmids:
        fetch_pubmed_for_query.last_status = "no_results"
        print("[pubmed] No results found.")
        return [], web_query, ""

    records = fetch_pubmed_abstracts(pmids)
    chunks = abstracts_to_chunks(records)

    report=getattr(records,"fetch_report",{"complete":set(str(r.get("pmid","")) for r in records)==set(pmids)})
    # Failed or partial fetches are never cached as complete success.
    if report["complete"]:
        write_jsonl(cache_path, chunks)
        register_pubmed_cache(gene, disease)

    web_url = f"{PUBMED_WEB_BASE}?term={urllib.parse.quote(web_query)}"
    print(f"[pubmed] Fetched {len(chunks)} abstracts; complete={bool(report['complete'])}.")
    fetch_pubmed_for_query.last_status = ("success" if chunks else "no_abstracts") if report["complete"] else "partial_fetch"
    return chunks, web_query, web_url


fetch_pubmed_for_query.last_status = "not_run"


# ---------------------------------------------------------------------------
# Batch / concurrent fetching (for advanced use cases)
# ---------------------------------------------------------------------------

def fetch_multiple_pubmed(
    queries: list[tuple[str, str]],
    max_results: int = _MAX_PMIDS,
    max_workers: int = 3,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Fetch PubMed for multiple gene-disease pairs in parallel.

    Args:
        queries: List of (gene, disease) tuples.
        max_results: Max abstracts per query.
        max_workers: Concurrent HTTP workers.

    Returns:
        Dict mapping (gene, disease) -> chunks.
    """
    results: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def _fetch(g: str, d: str) -> tuple[tuple[str, str], list[dict[str, Any]]]:
        chunks, _, _ = fetch_pubmed_for_query(g, d, max_results=max_results)
        return (g, d), chunks

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch, g, d): (g, d) for g, d in queries}
        for future in as_completed(futures):
            key, chunks = future.result()
            results[key] = chunks

    return results
