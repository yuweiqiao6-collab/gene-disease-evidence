"""Atomic, content-addressed caches. Public PubMed data never depends on local PDFs."""
from __future__ import annotations
import hashlib
import json
import os
import time
import re
from pathlib import Path
from threading import RLock
from typing import Any
from config import CHUNKS_JSONL, RUNTIME_DIR, get_embedding_settings

CACHE_DIR = Path(os.getenv('RAG_RETRIEVAL_CACHE_DIR', str(RUNTIME_DIR / 'cache'))) 
MINI_VECTOR_DIR = CACHE_DIR / 'mini_vectors'
PUBMED_CACHE_DIR = CACHE_DIR / 'pubmed'
_CACHE_MANIFEST = CACHE_DIR / 'manifest-v2.json'
_LOCK = RLock()
_DEFAULT_PUBMED_TTL_SECONDS = 7 * 24 * 3600


def _ensure_dirs():
    MINI_VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    PUBMED_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _chunks_mtime():
    return CHUNKS_JSONL.stat().st_mtime if CHUNKS_JSONL.exists() else 0.0


def _safe_key(gene, disease, signature=''):
    return hashlib.sha256(f'{gene.strip().lower()}::{disease.strip().lower()}::{signature}'.encode()).hexdigest()[:32]


def embedding_signature():
    settings = get_embedding_settings()
    model = Path(str(settings['model_name']))
    local_files = {}
    if model.is_dir():
        for p in sorted(model.rglob('*')):
            if p.is_file() and p.suffix in {'.json','.bin','.safetensors','.model','.txt','.vocab'} and not any(part.startswith('.') for part in p.relative_to(model).parts):
                stat = p.stat()
                local_files[p.relative_to(model).as_posix()] = [stat.st_size, stat.st_mtime_ns]
    payload = {'model': settings['model_name'], 'revision': settings.get('model_revision', 'main'),
               'local_files': local_files, 'encoding_version': 'context-normalized-512-v1'}
    if not model.is_dir() and not re.fullmatch(r'[a-fA-F0-9]{40}',str(settings.get('model_revision','main'))):
        payload['unpinned_remote_session'] = os.getpid()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def mini_vector_signature(chunks):
    payload = {'model': embedding_signature(), 'chunks': [
        [c['chunk_id'], hashlib.sha256(c.get('context', '').encode()).hexdigest()] for c in chunks]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _load_manifest():
    try:
        return json.loads(_CACHE_MANIFEST.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_manifest(manifest):
    _ensure_dirs()
    from io_utils import write_json
    write_json(_CACHE_MANIFEST, manifest)


def get_mini_vector_paths(gene, disease, signature=''):
    _ensure_dirs()
    key = _safe_key(gene, disease, signature)
    return MINI_VECTOR_DIR / f'{key}.npy', MINI_VECTOR_DIR / f'{key}_meta.json'


def get_pubmed_cache_path(gene, disease, signature=''):
    _ensure_dirs()
    return PUBMED_CACHE_DIR / f'{_safe_key(gene, disease, signature)}.jsonl'


def mini_vector_exists(gene, disease, signature=''):
    with _LOCK:
        entry = _load_manifest().get(_safe_key(gene, disease, signature), {})
        valid = entry.get('signature') == signature if signature else entry.get('chunks_mtime') == _chunks_mtime()
        paths = get_mini_vector_paths(gene, disease, signature)
        return valid and all(p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==entry.get('files',{}).get(p.name) for p in paths)


def register_mini_vector(gene, disease, signature=''):
    with _LOCK:
        manifest = _load_manifest()
        manifest[_safe_key(gene, disease, signature)] = {'gene': gene, 'disease': disease,
            'signature': signature, 'chunks_mtime': _chunks_mtime(),
            'files': {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in get_mini_vector_paths(gene,disease,signature)}}
        _save_manifest(manifest)


def pubmed_cache_exists(gene, disease, max_age_seconds=_DEFAULT_PUBMED_TTL_SECONDS, signature=''):
    path = get_pubmed_cache_path(gene, disease, signature)
    return path.exists() and (max_age_seconds <= 0 or time.time() - path.stat().st_mtime <= max_age_seconds)


def register_pubmed_cache(gene, disease):
    pass  # TTL and the full query form the cache identity; local corpus changes are irrelevant.


def cache_stats():
    return {'entries': len(_load_manifest()), 'mini_vector_files': len(list(MINI_VECTOR_DIR.glob('*.npy'))),
            'pubmed_cache_files': len(list(PUBMED_CACHE_DIR.glob('*.jsonl')))}
