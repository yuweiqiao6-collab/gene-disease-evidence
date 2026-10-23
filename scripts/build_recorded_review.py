"""Produce a minimal public historical-run review, without source text or names."""
import argparse
import hashlib
import json
from pathlib import Path


def build(run, assessment, comparisons):
    cases = {c['case_id']: c for c in comparisons}
    if run['actual_requests'] != assessment['executed_requests'] or run['usage'] != assessment['usage']:
        raise ValueError('运行与复盘记录不一致。')
    if set(cases) != {s['case_id'] for s in run['samples']}:
        raise ValueError('案例集合不一致。')
    descriptions = {
        'IL6-PMID42041559': ('历史提示返回了细胞模型中的表达候选。', '关系支持段落仍不完整；细胞干预结果不能直接作为疾病与正常对照的表达结论。'),
        'ABCC8-PMID33338546': ('历史提示保留了治疗相关的表达升高，并将测量类型保留为不明确。', '目标疾病映射与关系支持仍需核对；治疗比较不代替疾病与正常对照比较。'),
        'CD36-PMID20966915': ('历史提示未把遗传变异研究转换成表达量证据。', '空输出只描述本次抽取范围，不能据此判断CD36与疾病无关。')}
    if set(cases) != set(descriptions):
        raise ValueError('本脚本只整理已核对身份的三篇历史开发材料。')
    emitted = sum(len(c['new_relations']) for c in cases.values())
    if emitted != assessment['emitted_relations']:
        raise ValueError('候选数量不一致。')
    result = dict(schema_version=1, report_kind='historical_development_run',
        record_date=assessment['record_date_local'], prompt_version=run['prompt_version'],
        configured_model=run['model'], returned_models=run['response_models'],
        task_scope='历史范围与测量类型任务，非当前疾病与正常对照任务',
        current_baseline_accuracy=None, medical_accuracy=None, independent_test=False,
        requests=run['actual_requests'], failed_requests=run['failed_requests'],
        emitted_relations=emitted, usage=run['usage'], elapsed_seconds=run['elapsed_seconds'],
        unsupported_scope_passages=assessment['support_scope_passages_still_incomplete'],
        quote_existence_only=assessment['all_emitted_quotes_contiguous'],
        cases=[])
    for sample in run['samples']:
        case = cases[sample['case_id']]
        result['cases'].append(dict(case_id=sample['case_id'], gene=case['gene'],
            pmid=sample['paper_id'].removeprefix('PMID'), source_url=sample['source_url'],
            previous_relation_count=len(case['previous_relations']), new_relation_count=len(case['new_relations']),
            finding=descriptions[sample['case_id']][0], limitation=descriptions[sample['case_id']][1]))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='从指定历史记录生成最小公开复盘，不联网、不调用模型')
    parser.add_argument('--run-report', type=Path, required=True)
    parser.add_argument('--assessment', type=Path, required=True)
    parser.add_argument('--comparisons', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出已存在，请指定新文件。')
    result = build(json.loads(args.run_report.read_text()), json.loads(args.assessment.read_text()),
        [json.loads(line) for line in args.comparisons.read_text().splitlines() if line.strip()])
    result['source_sha256'] = {key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in [('run_report', args.run_report), ('assessment', args.assessment), ('comparisons', args.comparisons)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('已生成去除原文、人工姓名和私有路径的历史复盘。')
