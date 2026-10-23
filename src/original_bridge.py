"""Bounded local subprocess adapter; retrieval never imports the LLM client."""
from __future__ import annotations
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
import uuid


class OriginalRetrieval:
    def __init__(self, settings):
        self.settings = settings
        self.process = None
        self.lock = threading.RLock()
        self.log = None

    @property
    def ready(self):
        s = self.settings
        return bool(s.original_python and s.original_python.is_file() and
                    s.original_index and s.original_index.is_file() and
                    s.original_embedding and s.original_embedding.is_dir() and
                    s.original_reranker and s.original_reranker.is_dir())

    def _start(self):
        if not self.ready:
            raise ValueError('混合检索需要配置Python（程序运行环境）、BM25关键词索引和两个本地模型，见配置指南。')
        runtime = self.settings.runtime_dir/'original-retrieval'
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = dict(os.environ, RAG_RUNTIME_DIR=str(runtime), RAG_RETRIEVAL_CACHE_DIR=str(runtime/'cache'),
            EMBEDDING_MODEL=str(self.settings.original_embedding), RERANKER_MODEL=str(self.settings.original_reranker),
            EMBEDDING_DEVICE='cpu', EMBEDDING_BATCH_SIZE='8', ENABLE_HYBRID_RETRIEVAL='true',
            OMP_NUM_THREADS='2', MKL_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2',
            CUDA_VISIBLE_DEVICES='', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
        self.log = (runtime/'worker.log').open('a')
        self.process = subprocess.Popen([str(self.settings.original_python), '-u', '-B',
            str(Path(__file__).with_name('original_worker.py')), str(self.settings.original_index)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, env=env,
            cwd=runtime)

    def retrieve(self, payload):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.close(); self._start()
            request_id = uuid.uuid4().hex
            try:
                self.process.stdin.write(json.dumps(dict(id=request_id, **payload))+'\n')
                self.process.stdin.flush()
                with selectors.DefaultSelector() as selector:
                    selector.register(self.process.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=900):
                        raise TimeoutError('本地检索模型处理超时，尚未调用LLM。')
                response = json.loads(self.process.stdout.readline())
                if response.get('id') != request_id or not response.get('ok'):
                    raise RuntimeError('混合检索失败，尚未调用LLM（大语言模型）；详情保留在私有运行日志中。')
                return response['result']
            except (OSError, ValueError, RuntimeError, TimeoutError):
                self.close()
                raise

    def close(self):
        with self.lock:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:self.process.wait(timeout=8)
                    except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
                for stream in [self.process.stdin, self.process.stdout]:
                    if stream:stream.close()
                self.process = None
            if self.log:
                self.log.close(); self.log = None
