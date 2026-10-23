from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from config import get_disease_mapping_dir
from query_utils import query_terms


HGNC_BASE = "https://rest.genenames.org"
OLS_SEARCH_URL = "https://www.ebi.ac.uk/ols4/api/search"
OLS_ONTOLOGIES = ("mondo", "mesh", "doid", "efo", "ncit")


def lookup_gene_aliases(symbol: str) -> list[str]:
    symbol = symbol.strip()
    if not symbol:
        return []

    aliases: list[str] = []
    try:
        docs = _hgnc_fetch_by_symbol(symbol)
        if not docs:
            docs = _hgnc_search_first(symbol)
        for doc in docs[:1]:
            aliases.extend(_as_list(doc.get("alias_symbol")))
            aliases.extend(_as_list(doc.get("alias_name")))
            aliases.extend(_as_list(doc.get("prev_symbol")))
            aliases.extend(_as_list(doc.get("prev_name")))
            if doc.get("name"):
                aliases.append(str(doc["name"]))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        pass

    aliases.extend(query_terms(symbol))
    return _unique_clean(aliases, original=symbol)


def lookup_disease_aliases(disease: str) -> list[str]:
    disease = disease.strip()
    if not disease:
        return []

    aliases: list[str] = []
    aliases.extend(query_terms(disease))
    aliases.extend(lookup_local_disease_ids(disease))

    ols_docs = lookup_ols_disease_docs(disease)
    aliases.extend(_extract_ols_aliases(ols_docs))
    for disease_id in _extract_ols_ids(ols_docs):
        aliases.extend(lookup_local_disease_ids(disease_id))

    return _unique_clean(aliases, original=disease)


def lookup_local_disease_ids(query: str) -> list[str]:
    mapping_dir = get_disease_mapping_dir()
    if mapping_dir is None or not _looks_like_disease_id(query):
        return []

    graph: dict[str, set[str]] = {}
    for path in sorted(mapping_dir.glob("mondo_to_*.tsv")):
        _load_mapping_edges(path, graph)

    normalized_query = query.strip().upper()
    neighbors = set(graph.get(normalized_query, set()))
    for node, linked in graph.items():
        if normalized_query in linked:
            neighbors.add(node)
            neighbors.update(linked)
    neighbors.discard(normalized_query)
    return sorted(neighbors)


def lookup_ols_disease_docs(disease: str, rows: int = 5) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    seen = set()
    for ontology in OLS_ONTOLOGIES:
        params = urllib.parse.urlencode(
            {
                "q": disease,
                "ontology": ontology,
                "rows": str(rows),
                "queryFields": "label,synonym",
            }
        )
        try:
            data = _fetch_json(f"{OLS_SEARCH_URL}?{params}", timeout=30)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue

        for doc in data.get("response", {}).get("docs", []):
            key = str(doc.get("iri") or doc.get("obo_id") or doc.get("short_form") or "")
            if key and key not in seen:
                seen.add(key)
                docs.append(doc)
    return docs


def _extract_ols_aliases(docs: list[dict[str, Any]]) -> list[str]:
    aliases: list[str] = []
    for doc in docs:
        if doc.get("label"):
            aliases.append(str(doc["label"]))
        aliases.extend(_as_list(doc.get("synonym")))
        aliases.extend(_as_list(doc.get("obo_synonym")))
        short_form = _normalize_ontology_id(doc.get("short_form"))
        if short_form:
            aliases.append(short_form)
        obo_id = _normalize_ontology_id(doc.get("obo_id"))
        if obo_id:
            aliases.append(obo_id)
    return aliases


def _extract_ols_ids(docs: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for doc in docs:
        short_form = _normalize_ontology_id(doc.get("short_form"))
        if short_form:
            ids.append(short_form)
        obo_id = _normalize_ontology_id(doc.get("obo_id"))
        if obo_id:
            ids.append(obo_id)
    return _unique_clean(ids)


def _hgnc_fetch_by_symbol(symbol: str) -> list[dict[str, Any]]:
    url = f"{HGNC_BASE}/fetch/symbol/{urllib.parse.quote(symbol)}"
    data = _fetch_json(url, headers={"Accept": "application/json"})
    return data.get("response", {}).get("docs", [])


def _hgnc_search_first(symbol: str) -> list[dict[str, Any]]:
    url = f"{HGNC_BASE}/search/{urllib.parse.quote(symbol)}"
    data = _fetch_json(url, headers={"Accept": "application/json"})
    hits = data.get("response", {}).get("docs", [])
    if not hits:
        return []
    hgnc_id = hits[0].get("hgnc_id")
    if not hgnc_id:
        return []
    url = f"{HGNC_BASE}/fetch/hgnc_id/{urllib.parse.quote(hgnc_id)}"
    data = _fetch_json(url, headers={"Accept": "application/json"})
    return data.get("response", {}).get("docs", [])


def _fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _normalize_ontology_id(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.replace("_", ":")


def _load_mapping_edges(path: Path, graph: dict[str, set[str]]) -> None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            handle.readline()
            for line in handle:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2:
                    continue
                left = parts[0].strip().upper()
                right = parts[1].strip().upper()
                if left and right:
                    graph.setdefault(left, set()).add(right)
                    graph.setdefault(right, set()).add(left)
    except OSError:
        return


def _looks_like_disease_id(value: str) -> bool:
    return bool(re.match(r"^(MONDO|MESH|DOID|OMIM|EFO|ORPHANET|UMLS|NCIT|ICD10)[:_][A-Z0-9.]+$", value.strip(), re.I))


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)]


# 覆盖常见的 Ontology ID 前缀（MONDO、OMIM、ORPHANET、UMLS、DOID、NCIT、MESH、EFO、ICD10、HP、SYMP 等）
_ONTOLOGY_ID_RE = re.compile(
    r"^(MONDO|OMIM|ORPHANET|UMLS|DOID|NCIT|MESH|EFO|ICD10|HP|SYMP|GO|SO|CL|CHEBI|NCBITAXON|UBERON|SNOMEDCT|BTO|PO|ENVO|OBI|IAO|PATO|RO|FMA)[:_][A-Z0-9.]",
    re.IGNORECASE,
)


_MIN_ALIAS_LEN = 3


def _is_usable_alias(value: str, original: str) -> bool:
    """判断别名是否适合用于 PubMed 检索。"""
    if not value:
        return False
    # 排除 Ontology ID（NCIT:, OMIM:, ORPHANET:, UMLS:, DOID:, MONDO: 等）
    if _ONTOLOGY_ID_RE.match(value):
        return False
    # 排除过短别名（长度 < 3 基本无法在 PubMed 中有效匹配）
    if len(value) < _MIN_ALIAS_LEN:
        return False
    # 排除与原始输入完全相同的别名（大小写不敏感）
    if value.lower() == original.strip().lower():
        return False
    return True


def _unique_clean(values: list[str], original: str = "") -> list[str]:
    original_lower = original.strip().lower()
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        value = str(value).strip()
        if not _is_usable_alias(value, original):
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    # 优先保留较长、更可能是文献常用名称的别名（长度降序，同长度按字母稳定排序）
    output.sort(key=lambda x: (-len(x), x.lower()))
    return output
