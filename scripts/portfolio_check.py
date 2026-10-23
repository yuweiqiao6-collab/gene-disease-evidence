"""Reproducible offline demo checks and optional read-only retrieval timings."""
import argparse
import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from baseline import PROFILE
from corpus_store import CorpusStore
from jobs import JobManager, Query, TERMINAL
from showcase import scenarios
from web_settings import WebSettings


def forbidden(*args, **kwargs):
    raise RuntimeError('离线验收禁止模型或网络请求。')


def check(corpus=None):
    result = dict(version=(ROOT / 'VERSION').read_text().strip(),
        recorded_at_utc=datetime.now(timezone.utc).isoformat(),
        report_kind='offline_engineering_checks', model_calls=0, external_requests=0,
        medical_accuracy=None, retrieval_recall=None, scenarios=[], retrieval=None)
    with tempfile.TemporaryDirectory(prefix='evidence-showcase-') as directory, \
            patch('jobs.LLMClient', forbidden), patch('urllib.request.urlopen', forbidden):
        manager = JobManager(WebSettings(runtime_dir=Path(directory), enable_literature=False))
        try:
            for name, item in scenarios().items():
                started = time.monotonic()
                task = manager.submit('a' * 64, Query((item['gene'],), item['disease'],
                    task_profile=PROFILE, demo_scenario=name))
                while True:
                    snapshot = manager.snapshot('a' * 64, task)
                    if snapshot['state'] in TERMINAL:
                        break
                    if time.monotonic()-started > 30:
                        raise RuntimeError('教学场景超时。')
                    time.sleep(.01)
                passed = snapshot['state'] == item['expected_state'] and len(snapshot['rows']) == item['expected_evidence'] \
                    and snapshot['counts'].get('model_calls') == 0 \
                    and all(r.get('quote_verified') and r.get('comparison_quote_verified') and r.get('synthetic')
                        and r.get('baseline_status') == 'eligible' for r in snapshot['rows'])
                result['scenarios'].append(dict(scenario=name, title=item['title'], state=snapshot['state'],
                    candidate_count=snapshot['counts'].get('candidates', 0), evidence_count=len(snapshot['rows']),
                    elapsed_seconds=round(time.monotonic()-started, 4), passed=passed))
        finally:
            manager.close()
        if corpus is not None:
            store = CorpusStore(corpus); before = store.fingerprint()
            timings = []
            for gene, disease, aliases in [('IL6', 'rheumatoid arthritis', ['IL-6', 'interleukin 6']),
                    ('ABCC8', 'type 2 diabetes', ['SUR1']), ('CD36', 'type 2 diabetes', [])]:
                samples = []; counts = []
                for _ in range(5):
                    started = time.monotonic()
                    papers = store.search_papers(gene, disease, 10, aliases, [], skip_reference_like=True)
                    samples.append(time.monotonic()-started); counts.append(len(papers))
                timings.append(dict(gene=gene, disease=disease, limit=10, repetitions=5, returned_counts=counts,
                    elapsed_seconds=[round(v, 4) for v in samples], median_seconds=round(statistics.median(samples), 4)))
            unchanged = before == store.fingerprint()
            if not unchanged:
                raise RuntimeError('验收期间索引版本发生变化。')
            result['retrieval'] = dict(corpus_snapshot=before, queries=timings, index_unchanged=unchanged,
                interpretation='同一节点顺序重复查询，受系统缓存与负载影响；只测有界关键词检索耗时，不评判相关性、完整召回或全库抽取。')
    result['passed'] = all(s['passed'] for s in result['scenarios'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='验收三类教学流程，可选只读测量本地库检索；不需要模型密钥')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--corpus', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出已存在，请指定新文件。')
    report = check(args.corpus)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'scenarios': len(report['scenarios']),
        'model_calls': 0, 'external_requests': 0}, ensure_ascii=False))
    if not report['passed']:
        raise SystemExit(1)
