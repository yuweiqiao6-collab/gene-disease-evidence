from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.getenv("RAG_RUNTIME_DIR", str(PROJECT_ROOT / ".runtime"))).resolve()
DATA_DIR = RUNTIME_DIR / "data"
RAW_PDF_DIR = DATA_DIR / "raw_pdfs"
USER_UPLOAD_PDF_DIR = RAW_PDF_DIR / "user_upload"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUT_DIR = RUNTIME_DIR / "outputs"

PAGES_JSONL = PROCESSED_DIR / "pages.jsonl"
CHUNKS_JSONL = PROCESSED_DIR / "chunks.jsonl"
PUBMED_JSONL = PROCESSED_DIR / "pubmed_abstracts.jsonl"
INDEX_JSON = PROCESSED_DIR / "bm25_index.json"
LLM_CACHE_DB = PROCESSED_DIR / "llm_cache.sqlite"
RESULTS_JSON = OUTPUT_DIR / "extraction_results.json"
RESULTS_XLSX = OUTPUT_DIR / "extraction_results.xlsx"

DEFAULT_DISEASE_MAPPING_DIR = PROJECT_ROOT / "reference" / "disease_id_mapping"

DEFAULT_TOP_K = 12

EXPRESSION_TERMS = [
    "upregulated",
    "up-regulated",
    "increased expression",
    "increased levels",
    "overexpression",
    "over-expressed",
    "elevated",
    "high expression",
    "higher expression",
    "downregulated",
    "down-regulated",
    "decreased expression",
    "decreased levels",
    "reduced",
    "low expression",
    "lower expression",
    "knockdown",
    "silencing",
    "inhibition",
    "activation",
]

RELATION_TERMS = [
    "risk",
    "susceptibility",
    "incidence",
    "progression",
    "severity",
    "prognosis",
    "survival",
    "mortality",
    "associated with",
    "correlated with",
    "promotes",
    "protects against",
    "attenuates",
    "worsens",
    "alleviates",
    "poor prognosis",
    "better prognosis",
    "hazard ratio",
    "odds ratio",
]

NEGATION_TERMS = [
    "not associated",
    "no association",
    "no significant",
    "not significant",
    "failed to",
    "did not",
    "without significant",
]

CAUSAL_TERMS = [
    "knockdown",
    "silencing",
    "overexpression",
    "over-expression",
    "inhibition",
    "inhibitor",
    "activation",
    "deficiency",
    "deletion",
    "deleted",
    "restored",
    "rescued",
]


def ensure_directories() -> None:
    RAW_PDF_DIR.mkdir(parents=True, exist_ok=True)
    USER_UPLOAD_PDF_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def read_env_file(env_path: Path) -> dict[str, str]:
    values = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"\'')
    return values


def load_env() -> None:
    for key, value in read_env_file(PROJECT_ROOT / '.env').items():
        os.environ.setdefault(key, value)


def get_llm_settings() -> dict[str, str | int | None]:
    load_env()
    linked = read_env_file(Path(os.environ['RAG_MODEL_ENV_FILE']).expanduser()) if os.getenv('RAG_MODEL_ENV_FILE') else {}
    def setting(key, default=''):
        return os.getenv(key) or linked.get(key) or default
    timeout_raw = setting("LLM_TIMEOUT_SECONDS", "60")
    try:
        timeout = max(1, min(120, int(timeout_raw)))
    except ValueError:
        timeout = 60
    return {
        "api_key": setting("LLM_API_KEY") or None,
        "base_url": setting("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "model": setting("LLM_MODEL") or None,
        "timeout": timeout,
        "cache_enabled": setting("LLM_CACHE_ENABLED", "true").lower() not in {"0", "false", "no"},
    }


def get_disease_mapping_dir() -> Path | None:
    load_env()
    raw = os.getenv("DISEASE_MAPPING_DIR")
    path = Path(raw) if raw else DEFAULT_DISEASE_MAPPING_DIR
    return path if path.exists() else None


# ========== Hybrid Retrieval / Vector Search Config ==========

VECTOR_INDEX_NPY = PROCESSED_DIR / "chunk_embeddings.npy"
VECTOR_METADATA_JSON = PROCESSED_DIR / "vector_metadata.json"


def get_embedding_settings() -> dict[str, str | int | bool | float]:
    """Return embedding model and hybrid search settings from .env."""
    load_env()
    return {
        # Model
        "model_name": os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
        "model_revision": os.getenv("EMBEDDING_MODEL_REVISION", "main"),
        "reranker_model": os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
        "device": os.getenv("EMBEDDING_DEVICE", "cpu"),
        "batch_size": int(os.getenv("EMBEDDING_BATCH_SIZE", "64")),
        # Hybrid search
        "enable_hybrid": os.getenv("ENABLE_HYBRID_RETRIEVAL", "true").lower()
        not in {"0", "false", "no"},
        "fusion_strategy": os.getenv("HYBRID_FUSION_STRATEGY", "rrf"),
        "hybrid_weight_alpha": float(os.getenv("HYBRID_WEIGHT_ALPHA", "0.5")),
        "rrf_k": int(os.getenv("RRF_K", "60")),
        "vector_top_k": int(os.getenv("VECTOR_TOP_K", "100")),
    }
