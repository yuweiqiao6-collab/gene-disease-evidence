from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from pathlib import Path
from threading import Lock

from config import get_llm_settings
from llm_cache import LLMCache, make_cache_key
from prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_COT, build_user_prompt,prompt_settings,source_text
from schemas import ExtractionResult, normalize_result
from text_utils import contains_any
from validators import extract_statistical_evidence


class LLMClient:
    def __init__(self, use_cot: bool = False, *, cache_path: Path | None = None, allow_fallback: bool = True, max_output_tokens: int = 2048, thinking: bool | None = None,task_profile: str = 'legacy') -> None:
        prompt_settings(task_profile)
        self.task_profile=task_profile
        settings = get_llm_settings()
        self.api_key = settings["api_key"]
        self.base_url = settings["base_url"]
        self.model = settings["model"]
        self.timeout = int(settings["timeout"] or 60)
        self.cache_enabled = bool(settings.get("cache_enabled"))
        self.cache = (LLMCache(cache_path) if cache_path else LLMCache()) if self.cache_enabled else None
        self.use_cot = use_cot
        self.failure_count = 0
        self.request_count = 0
        self.allow_fallback = allow_fallback
        self._failure_lock = Lock()
        if not 1 <= max_output_tokens <= 2048:
            raise ValueError('Output token limit must be 1..2048')
        self.max_output_tokens = max_output_tokens
        if thinking is not None and not isinstance(thinking,bool):
            raise ValueError('Thinking must be true, false or provider default')
        self.thinking = thinking
        self.cache_model = str(self.model) if thinking is None else f'{self.model}:thinking={thinking}'
        self.usage = {'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}
        self.failures = []
        self.response_models = set()

    def _failure(self, chunk, exc):
        with self._failure_lock:
            self.failure_count += 1
            self.failures.append({'chunk_id':chunk.get('chunk_id',''),
                'error_type':type(exc).__name__, 'http_status':getattr(exc,'code',None)})

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.model)

    def extract(self, gene: str, disease: str, chunk: dict[str, Any], use_cot: bool | None = None) -> list[ExtractionResult]:
        if use_cot is None:
            use_cot = self.use_cot
        if not self.is_configured:
            if not self.allow_fallback:
                raise ValueError("Model credentials are required for live extraction")
            return rule_based_extract(gene, disease, chunk)
        try:
            cache_key = make_cache_key(
                model=self.cache_model,
                base_url=str(self.base_url),
                gene=gene,
                disease=disease,
                chunk=chunk,
                use_cot=use_cot,
                task_profile=self.task_profile,
            )
            if self.cache is not None:
                cached_items = self.cache.get(cache_key)
                if cached_items is not None:
                    return [normalize_result(item) for item in cached_items if isinstance(item, dict)]
            raw_items = self._call_chat_completion(gene, disease, chunk, use_cot=use_cot)
            if self.cache is not None:
                self.cache.set(
                    cache_key,
                    model=str(self.model),
                    gene=gene,
                    disease=disease,
                    chunk_id=str(chunk.get("chunk_id", "")),
                    raw_items=raw_items,
                    use_cot=use_cot,
                    task_profile=self.task_profile,
                )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            self._failure(chunk, exc)
            if not self.allow_fallback:
                return []
            fallback = rule_based_extract(gene, disease, chunk)
            for item in fallback:
                item.needs_review = True
                item.notes = f"llm_call_failed_using_rule_fallback: {exc}"
            return fallback
        return [normalize_result(item) for item in raw_items if isinstance(item, dict)]

    def extract_batch(
        self,
        gene: str,
        disease: str,
        chunks: list[dict[str, Any]],
        max_workers: int = 4,
        retry: int = 2,
        use_cot: bool | None = None,
    ) -> list[tuple[dict[str, Any], list[ExtractionResult]]]:
        """Parallel LLM extraction with thread pool and retry.

        Args:
            gene: Target gene.
            disease: Target disease.
            chunks: List of candidate chunks.
            max_workers: Number of concurrent LLM calls (default 4).
                         Adjust based on API rate limits.
            retry: Number of retries on transient failure (default 2).

        Returns:
            List of (chunk, extraction_results) tuples, preserving input order.
        """
        if use_cot is None:
            use_cot = self.use_cot

        if not self.is_configured:
            if not self.allow_fallback:
                raise ValueError("Model credentials are required for live extraction")
            return [(chunk, rule_based_extract(gene, disease, chunk)) for chunk in chunks]

        results_map: dict[str, tuple[dict[str, Any], list[ExtractionResult]]] = {}

        def _call_with_retry(g: str, d: str, ch: dict[str, Any], retries: int, cot: bool) -> tuple[dict[str, Any], list[ExtractionResult]]:
            last_exc: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    cache_key = make_cache_key(
                        model=self.cache_model,
                        base_url=str(self.base_url),
                        gene=g,
                        disease=d,
                        chunk=ch,
                        use_cot=cot,
                        task_profile=self.task_profile,
                    )
                    if self.cache is not None:
                        cached_items = self.cache.get(cache_key)
                        if cached_items is not None:
                            return ch, [normalize_result(item) for item in cached_items if isinstance(item, dict)]
                    raw_items = self._call_chat_completion(g, d, ch, use_cot=cot)
                    if self.cache is not None:
                        self.cache.set(
                            cache_key,
                            model=str(self.model),
                            gene=g,
                            disease=d,
                            chunk_id=str(ch.get("chunk_id", "")),
                            raw_items=raw_items,
                            use_cot=cot,
                            task_profile=self.task_profile,
                        )
                    return ch, [normalize_result(item) for item in raw_items if isinstance(item, dict)]
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                    last_exc = exc
                    if attempt < retries:
                        time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s
                    continue
            # All retries exhausted
            self._failure(ch, last_exc)
            if not self.allow_fallback:
                return ch, []
            fallback = rule_based_extract(g, d, ch)
            for item in fallback:
                item.needs_review = True
                item.notes = f"llm_call_failed_after_{retries}_retries_using_rule_fallback: {last_exc}"
            return ch, fallback

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_call_with_retry, gene, disease, chunk, retry, use_cot): chunk.get("chunk_id", "")
                for chunk in chunks
            }
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    chunk, extracted = future.result()
                    results_map[cid] = (chunk, extracted)
                except Exception as exc:
                    # Should not happen because _call_with_retry catches internally,
                    # but defensively handle
                    fallback_chunk = next((c for c in chunks if c.get("chunk_id") == cid), {})
                    fallback = rule_based_extract(gene, disease, fallback_chunk) if self.allow_fallback else []
                    self._failure(fallback_chunk, exc)
                    for item in fallback:
                        item.needs_review = True
                        item.notes = f"parallel_executor_unexpected_error: {exc}"
                    results_map[cid] = (fallback_chunk, fallback)

        # Preserve input order
        ordered = []
        for chunk in chunks:
            cid = chunk.get("chunk_id", "")
            if cid in results_map:
                ordered.append(results_map[cid])
            else:
                fallback = rule_based_extract(gene, disease, chunk) if self.allow_fallback else []
                self._failure(chunk, RuntimeError('Missing worker result'))
                ordered.append((chunk, fallback))
        return ordered

    def _call_chat_completion(self, gene: str, disease: str, chunk: dict[str, Any], use_cot: bool = False) -> list[dict[str, Any]]:
        with self._failure_lock:
            self.request_count += 1
        endpoint = f"{self.base_url}/chat/completions"
        context = (
            f"paper_id: {chunk.get('paper_id', '')}\n"
            f"title: {chunk.get('title', '')}\n"
            f"source_location: {chunk.get('source_location', '')}\n\n"
            f"{source_text(chunk,self.task_profile)}"
        )
        _,system_prompt,_=prompt_settings(self.task_profile,use_cot)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": build_user_prompt(gene, disease, context, use_cot=use_cot,task_profile=self.task_profile)},
            ],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
        }
        if self.thinking is not None:
            payload['thinking'] = {'type':'enabled' if self.thinking else 'disabled'}
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            content_bytes = response.read()
            data = json.loads(content_bytes.decode("utf-8"))
        with self._failure_lock:
            usage = data.get('usage',{}) if isinstance(data,dict) else {}
            if not isinstance(usage,dict):
                usage = {}
            if isinstance(data,dict) and isinstance(data.get('model'),str):
                self.response_models.add(data['model'])
            for key in self.usage:
                if isinstance(usage.get(key),int) and usage[key] >= 0:
                    self.usage[key] += usage[key]
        try:
            if data['choices'][0].get('finish_reason') == 'length':
                raise ValueError('Model output truncated by token limit')
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("Missing text response")
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Invalid model response shape") from exc
        parsed = _parse_json_content(content)
        if isinstance(parsed, list):
            if not all(isinstance(item, dict) for item in parsed):
                raise ValueError("Model array contains invalid relation items")
            return parsed
        if isinstance(parsed, dict):
            for key in ("results", "relations", "items"):
                if isinstance(parsed.get(key), list):
                    if not all(isinstance(item, dict) for item in parsed[key]):
                        raise ValueError("Model array contains invalid relation items")
                    return parsed[key]
        raise ValueError("LLM response did not contain a JSON array")


def _parse_json_content(content: str) -> Any:
    content = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", content, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        content = fenced.group(1).strip()
    return json.loads(content)


def rule_based_extract(gene: str, disease: str, chunk: dict[str, Any]) -> list[ExtractionResult]:
    context = chunk.get("context", "")
    sentence = chunk.get("sentence") or context
    lower = context.lower()
    if gene.lower() not in lower and disease.lower() not in lower:
        return []

    result = ExtractionResult(
        paper_id=chunk.get("paper_id", ""),
        title=chunk.get("title", ""),
        gene_or_target=gene,
        disease=disease,
        expression_direction=_guess_expression_direction(lower),
        disease_effect=_guess_disease_effect(lower),
        relation_type=_guess_relation_type(lower),
        evidence_type=_guess_evidence_type(lower),
        statistical_evidence=extract_statistical_evidence(context),
        evidence_sentence=sentence,
        source_location=chunk.get("source_location", ""),
        confidence=0.55,
        needs_review=True,
        notes="rule_based_fallback_review_required",
    )
    return [result]


def _guess_expression_direction(lower_text: str) -> str:
    increased_terms = [
        "upregulated",
        "up-regulated",
        "increased expression",
        "overexpression",
        "elevated",
        "high expression",
        "higher expression",
    ]
    decreased_terms = [
        "downregulated",
        "down-regulated",
        "decreased expression",
        "reduced",
        "low expression",
        "lower expression",
        "knockdown",
        "silencing",
    ]
    if contains_any(lower_text, increased_terms):
        return "increased"
    if contains_any(lower_text, decreased_terms):
        return "decreased"
    if "no significant" in lower_text:
        return "unchanged"
    return "unclear"


def _guess_disease_effect(lower_text: str) -> str:
    if contains_any(lower_text, ["not associated", "no association", "no significant"]):
        return "no_relation"
    if contains_any(lower_text, ["increased risk", "higher risk", "susceptibility", "incidence"]):
        return "risk_increase"
    if contains_any(lower_text, ["decreased risk", "lower risk", "protects against", "protective"]):
        return "risk_decrease"
    if contains_any(lower_text, ["progression", "severity", "disease activity", "increased disease activity", "promotes", "worsens", "exacerbates"]):
        return "progression_increase"
    if contains_any(lower_text, ["attenuates", "alleviates", "reduced disease activity", "reduced tumor growth"]):
        return "progression_decrease"
    if contains_any(lower_text, ["poor prognosis", "poor survival", "shorter survival"]):
        return "poor_prognosis"
    if contains_any(lower_text, ["better prognosis", "improved survival", "longer survival"]):
        return "better_prognosis"
    if contains_any(lower_text, ["expression", "upregulated", "downregulated", "elevated"]):
        return "biomarker_only"
    return "unclear"


def _guess_relation_type(lower_text: str) -> str:
    if contains_any(lower_text, ["knockdown", "silencing", "overexpression", "inhibition", "activation", "deletion", "rescued"]):
        return "causal"
    if contains_any(lower_text, ["pathway", "mechanism", "mediated", "regulates"]):
        return "mechanistic"
    if contains_any(lower_text, ["predict", "predictor", "prognostic", "survival"]):
        return "predictive"
    if contains_any(lower_text, ["target", "therapeutic"]):
        return "therapeutic_target"
    if contains_any(lower_text, ["associated", "correlated", "correlation"]):
        return "associated"
    return "unclear"


def _guess_evidence_type(lower_text: str) -> str:
    if contains_any(lower_text, ["patients", "cohort", "case-control", "clinical"]):
        return "human_cohort"
    if contains_any(lower_text, ["mouse", "mice", "rat", "animal model"]):
        return "animal_model"
    if contains_any(lower_text, ["cell line", "cells", "in vitro"]):
        return "cell_experiment"
    if contains_any(lower_text, ["meta-analysis", "systematic review"]):
        return "meta_analysis"
    return "unclear"
