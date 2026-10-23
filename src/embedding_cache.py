"""Content-addressed vectors shared across queries; never stores source text."""
from __future__ import annotations
import hashlib
import sqlite3
from pathlib import Path
import numpy as np


class EmbeddingCache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, dimension INTEGER NOT NULL, digest TEXT NOT NULL, vector BLOB NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def key(model_signature, text):
        return hashlib.sha256((model_signature+'\0'+text).encode()).hexdigest()

    def get(self, key):
        with self.connect() as conn:
            row = conn.execute('SELECT dimension,digest,vector FROM vectors WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        dimension, digest, data = row
        if dimension < 1 or len(data) != dimension*4 or hashlib.sha256(data).hexdigest() != digest:
            return None
        vector = np.frombuffer(data, dtype='<f4').copy()
        return vector if np.isfinite(vector).all() and np.linalg.norm(vector) > 0 else None

    def put_many(self, rows):
        values = []
        for key, value in rows:
            vector = np.asarray(value, dtype='<f4')
            if vector.ndim != 1 or not len(vector) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
                raise ValueError('Invalid embedding vector')
            data = vector.tobytes()
            values.append((key, len(vector), hashlib.sha256(data).hexdigest(), data))
        with self.connect() as conn:
            conn.executemany('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)', values)

    def encode(self, chunks, model_signature, encoder):
        keys = [self.key(model_signature, c.get('context', '')) for c in chunks]
        vectors = {key: self.get(key) for key in dict.fromkeys(keys)}
        missing = dict.fromkeys(key for key in keys if vectors[key] is None)
        representative = {key: chunk for key, chunk in zip(keys, chunks)}
        if missing:
            encoded = np.asarray(encoder([representative[key] for key in missing], field='context'), dtype=np.float32)
            if encoded.ndim != 2 or encoded.shape[0] != len(missing):
                raise ValueError('Embedding response does not match requested chunks')
            self.put_many(zip(missing, encoded))
            vectors.update(zip(missing, encoded))
        if not chunks:
            return np.empty((0, 0), dtype=np.float32), dict(reused=0, encoded=0)
        dimensions = {len(vector) for vector in vectors.values()}
        if len(dimensions) != 1:
            raise ValueError('Embedding cache dimensions disagree; model identity must change')
        return np.stack([vectors[key] for key in keys]), dict(reused=len(chunks)-len(missing), encoded=len(missing))
