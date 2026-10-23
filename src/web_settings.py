from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from config import PROJECT_ROOT, load_env, read_env_file


@dataclass(frozen=True)
class WebSettings:
    runtime_dir: Path = PROJECT_ROOT / '.runtime'
    enable_live: bool = False
    enable_literature: bool = True
    access_password: str = ''
    corpus_db: Path | None = None
    max_workers: int = 2
    max_queue: int = 4
    max_genes: int = 3
    max_top_k: int = 12
    max_uploads: int = 5
    max_upload_mb: int = 10
    max_pages: int = 100
    max_model_chunks: int = 24
    session_jobs_per_minute: int = 3
    global_live_jobs_per_hour: int = 20
    retention_hours: int = 24
    original_python: Path | None = None
    original_index: Path | None = None
    original_embedding: Path | None = None
    original_reranker: Path | None = None

    @classmethod
    def from_env(cls):
        load_env()
        corpus = os.getenv('RAG_CORPUS_DB', '').strip()
        file = os.getenv('RAG_ORIGINAL_CONFIG_FILE', str(PROJECT_ROOT/'.env.original'))
        private = read_env_file(Path(file)) if file else {}
        def original_path(key):
            value = os.environ.get(key, private.get(key, '')).strip()
            return Path(value).expanduser().resolve() if value else None
        return cls(runtime_dir=Path(os.getenv('RAG_RUNTIME_DIR', str(PROJECT_ROOT / '.runtime'))).resolve(),
                   enable_live=os.getenv('RAG_ENABLE_LIVE', 'false').lower() == 'true',
                   enable_literature=os.getenv('RAG_ENABLE_LITERATURE','true').lower()=='true',
                   access_password=os.getenv('RAG_ACCESS_PASSWORD', ''),
                   corpus_db=Path(corpus).resolve() if corpus else None,
                   original_python=original_path('RAG_ORIGINAL_PYTHON'),
                   original_index=original_path('RAG_ORIGINAL_INDEX_JSON'),
                   original_embedding=original_path('RAG_ORIGINAL_EMBEDDING_MODEL'),
                   original_reranker=original_path('RAG_ORIGINAL_RERANKER_MODEL'))

    @property
    def live_ready(self):
        return self.enable_live and len(self.access_password) >= 12
