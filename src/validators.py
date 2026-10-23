from __future__ import annotations

import re

from config import CAUSAL_TERMS, NEGATION_TERMS, RELATION_TERMS
from schemas import ExtractionResult
from text_utils import contains_any
from query_utils import contains_any_term, query_terms
from typing import Any


def apply_relation_rules(result: ExtractionResult, gene: str, disease: str,
                         gene_aliases: list[str] | None = None,
                         disease_aliases: list[str] | None = None) -> ExtractionResult:
    evidence = result.evidence_sentence or ""
    lower_evidence = evidence.lower()
    notes: list[str] = []

    if gene and not contains_any_term(evidence, query_terms(gene, gene_aliases)):
        result.needs_review = True
        notes.append("gene_not_in_evidence")
    if disease and not contains_any_term(evidence, query_terms(disease, disease_aliases)):
        result.needs_review = True
        notes.append("disease_not_in_evidence")

    if contains_any(evidence, NEGATION_TERMS):
        # A negated clause may concern a different endpoint. Flag it without
        # rewriting the model's entire relationship or expression finding.
        result.needs_review = True
        notes.append("negative_or_non_significant_evidence")

    if result.relation_type == "causal" and not contains_any(evidence, CAUSAL_TERMS):
        result.relation_type = "associated"
        result.needs_review = True
        notes.append("causal_downgraded_without_intervention_term")

    if (
        result.expression_direction in {"increased", "decreased"}
        and result.disease_effect == "unclear"
        and not contains_any(evidence, RELATION_TERMS)
    ):
        result.disease_effect = "biomarker_only"
        result.needs_review = True
        notes.append("expression_only_without_relation_direction")

    if result.confidence < 0.75:
        result.needs_review = True
        notes.append("low_confidence")

    result.notes = "; ".join([part for part in [result.notes, *notes] if part])
    return result


def ground_result(raw: dict[str, Any], chunk: dict[str, Any], gene: str, disease: str,
                  gene_aliases: list[str] | None = None,
                  disease_aliases: list[str] | None = None) -> dict[str, Any] | None:
    """Reject absent quotes; bind source identity to trusted input, never model text."""
    from schemas import normalize_result
    result = normalize_result(raw)
    quote = re.sub(r"\s+", " ", str(result.evidence_sentence or "")).strip()
    context = re.sub(r"\s+", " ", str(chunk.get("context", ""))).strip()
    if len(quote) < 12 or quote not in context:
        return None
    result.paper_id = str(chunk.get("paper_id", ""))
    result.title = str(chunk.get("title", ""))
    result.source_location = str(chunk.get("source_location", ""))
    result.gene_or_target, result.disease = gene, disease
    result.evidence_sentence = quote
    result = apply_relation_rules(result, gene, disease, gene_aliases, disease_aliases)
    # A model's self-reported confidence is not an empirically calibrated probability.
    result.needs_review = True
    row = result.to_dict()
    row.update(chunk_id=chunk.get("chunk_id", ""), source_type=chunk.get("source_type", ""),
               page=chunk.get("page"), file_name=chunk.get('file_name',''), quote_verified=True,
               retrieval_score=chunk.get("retrieval_score", 0.0))
    from source_quality import source_flags
    row['source_quality_flags']=source_flags(chunk)
    paper_id = row["paper_id"]
    row["source_url"] = (f"https://pubmed.ncbi.nlm.nih.gov/{paper_id[4:]}/"
                         if re.fullmatch(r"PMID\d+", paper_id) else "")
    if chunk.get('source_type')=='pubmed_abstract' and re.fullmatch(r'\d+',str(chunk.get('pmid',''))):
        row['source_url']='https://pubmed.ncbi.nlm.nih.gov/'+str(chunk['pmid'])+'/'
    # Source-level evidence and locations are inherited from the trusted retrieved chunk.
    row['content_level']=chunk.get('content_level') or ('abstract' if chunk.get('source_type')=='pubmed_abstract' else 'snippet')
    for key in ['section','paragraph','pmid','pmcid','doi','abstract_sha256','fulltext_sha256','fulltext_license']:
        if key in chunk: row[key]=chunk[key]
    if chunk.get('source_type')=='europepmc_fulltext' and re.fullmatch(r'PMC\d+',str(chunk.get('pmcid',''))):
        row['source_url']='https://europepmc.org/articles/'+chunk['pmcid']
    if chunk.get('source_type')=='europepmc_abstract' and str(chunk.get('source_url','')).startswith('https://europepmc.org/article/'):
        row['source_url']=chunk['source_url']
    from support_checks import check_support
    row.update(check_support(row,context,gene,disease,gene_aliases,disease_aliases))
    return row


def extract_statistical_evidence(text: str) -> dict[str, str | None]:
    p_value = _first_match(text, r"\bp\s*[=<]\s*0?\.\d+")
    ci = _first_match(text, r"95%\s*CI[^,.;)]*")
    ratio = _first_match(text, r"\b(?:OR|HR|RR|odds ratio|hazard ratio)\s*[=:]?\s*\d+(?:\.\d+)?")
    sample_size = _first_match(text, r"\b(?:n|N)\s*=\s*\d+|\b\d+\s+(?:patients|participants|subjects|cases|controls)\b")
    return {
        "p_value": p_value,
        "or_hr_rr": ratio,
        "confidence_interval": ci,
        "sample_size": sample_size,
    }


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(0) if match else None

