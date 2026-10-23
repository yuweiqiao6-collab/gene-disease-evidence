from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from config import LLM_CACHE_DB
from prompts import PROMPT_VERSION, PROMPT_VERSION_COT, SYSTEM_PROMPT, SYSTEM_PROMPT_COT, build_user_prompt,prompt_settings,source_text


def make_cache_key(
    *,
    model: str,
    base_url: str,
    gene: str,
    disease: str,
    chunk: dict[str, Any],
    use_cot: bool = False,
    task_profile: str = 'legacy',
) -> str:
    context = source_text(chunk,task_profile)
    version,system,_=prompt_settings(task_profile,use_cot)
    payload = {
        "prompt_version":version,
        "prompt_sha256": hashlib.sha256((system+build_user_prompt(gene,disease,'',use_cot,task_profile=task_profile)).encode()).hexdigest(),
        "model": model,
        "base_url": base_url,
        "gene": gene.strip().lower(),
        "disease": disease.strip().lower(),
        "chunk_id": chunk.get("chunk_id", ""),
        "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "source": {key: chunk.get(key, "") for key in ("paper_id", "title", "source_location")},
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LLMCache:
    def __init__(self, path: Path = LLM_CACHE_DB) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_cache (
                    cache_key TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    gene TEXT NOT NULL,
                    disease TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                )
                """
            )

    def get(self, cache_key: str) -> list[dict[str, Any]] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT raw_json FROM llm_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        return data if isinstance(data, list) else None

    def set(
        self,
        cache_key: str,
        *,
        model: str,
        gene: str,
        disease: str,
        chunk_id: str,
        raw_items: list[dict[str, Any]],
        use_cot: bool = False,
        task_profile: str = 'legacy',
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO llm_cache
                (cache_key, created_at, model, gene, disease, chunk_id, prompt_version, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    int(time.time()),
                    model,
                    gene,
                    disease,
                    chunk_id,
                    prompt_settings(task_profile,use_cot)[0],
                    json.dumps(raw_items, ensure_ascii=False),
                ),
            )

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
            by_model = conn.execute(
                "SELECT model, COUNT(*) FROM llm_cache GROUP BY model ORDER BY COUNT(*) DESC"
            ).fetchall()
        return {"path": str(self.path), "total_entries": total, "by_model": by_model}

