"""Evaluate only expert-confirmed cases with complete, successful predictions."""
from __future__ import annotations
from collections import Counter
import hashlib
from schemas import (EXPRESSION_VALUES, DISEASE_EFFECT_VALUES, RELATION_TYPE_VALUES,
    RELATION_SCOPE_VALUES,POPULATION_VALUES,MEASUREMENT_VALUES)

FIELDS=['expression_direction','disease_effect','relation_type','species']
SCOPE_FIELDS=['relation_scope','population_context','measurement_type']
EVALUATION_PROFILES={'legacy':FIELDS,'scope_measurement':FIELDS+SCOPE_FIELDS}
SCOPE_ALLOWED={'relation_scope':RELATION_SCOPE_VALUES,'population_context':POPULATION_VALUES,
               'measurement_type':MEASUREMENT_VALUES}


def profile_fields(profile):
    if profile not in EVALUATION_PROFILES: raise ValueError('Unknown evaluation profile')
    return list(EVALUATION_PROFILES[profile])


def validate_labels(cases):
    seen, splits={},{}
    for case in cases:
        case_id=case['case_id']
        if case_id in seen: raise ValueError('Duplicate annotation case ID')
        seen[case_id]=True
        paper=case['paper_id']
        if paper in splits and splits[paper]!=case['split']:
            raise ValueError('One paper appears in both development and test splits')
        splits[paper]=case['split']
        if case.get('context_sha256') != hashlib.sha256(case['context'].encode()).hexdigest():
            raise ValueError('Annotation context hash does not match original text')
        if case.get('gold_status')!='reviewed': continue
        if not case.get('reviewer','').strip() or not isinstance(case.get('gold_relevant'),bool) or not isinstance(case.get('gold_relations'),list):
            raise ValueError('Reviewed cases require reviewer, relevance decision and explicit relation list')
        if case['gold_relations'] and not case['gold_relevant']:
            raise ValueError('A case with gold relations cannot be labeled irrelevant')
        for relation in case['gold_relations']:
            for field,allowed in [('expression_direction',EXPRESSION_VALUES),('disease_effect',DISEASE_EFFECT_VALUES),('relation_type',RELATION_TYPE_VALUES)]:
                if not isinstance(relation.get(field),str) or relation[field] not in allowed: raise ValueError('Unsupported gold relation value')
            for field,allowed in SCOPE_ALLOWED.items():
                if field in relation and (not isinstance(relation[field],str) or relation[field] not in allowed):
                    raise ValueError('Unsupported gold scope or measurement value')
            quote=relation.get('evidence_sentence','')
            if not isinstance(quote,str) or len(quote)<12 or quote not in case['context']:
                raise ValueError('Gold quote must exist in the original context')
            if not isinstance(relation.get('species'),str) or not relation['species'].strip(): raise ValueError('Gold species is required')


def evaluate(cases,predictions,*,profile='legacy'):
    fields=profile_fields(profile)
    validate_labels(cases)
    by_id={}
    for p in predictions:
        if p['case_id'] in by_id: raise ValueError('Duplicate prediction case ID')
        by_id[p['case_id']]=p
    reviewed=[c for c in cases if c.get('gold_status')=='reviewed']
    report={'reviewed_cases':len(reviewed),'unreviewed_cases':len(cases)-len(reviewed),
        'missing_predictions':0,'failed_predictions':0,'content_mismatches':0,
        'incomplete_reference_cases':0,'reference_missing_fields':[],
        'evaluation_ready':False,'metrics':None,
        'evaluation_profile':profile,
        'metric_definition':'Exact '+('/'.join(fields))+' tuple matching per paper and target; primary quotes checked separately'}
    report['evaluated_fields']=fields
    report['not_evaluated_fields']=[k for k in FIELDS+SCOPE_FIELDS if k not in fields]
    report['quote_check_scope']='Primary quote existence only; relation-support and measurement-quote semantics are not scored'
    if not reviewed:
        report['reason']='尚无人工确认的参考答案，不能报告医学准确率。'
        return report
    for c in reviewed:
        gaps=[{'case_id':c['case_id'],'relation_index':i,'fields':[k for k in fields if k not in r]}
              for i,r in enumerate(c['gold_relations']) if any(k not in r for k in fields)]
        if gaps:
            report['incomplete_reference_cases']+=1
            report['reference_missing_fields'].extend(gaps)
    if report['incomplete_reference_cases']:
        report['reason']='人工参考缺少当前评测要求的字段；不从模型预测补答案，也不把缺失当成“不明确”。'
        return report
    for c in reviewed:
        p=by_id.get(c['case_id'])
        if p is None: report['missing_predictions']+=1; continue
        if p.get('complete') is not True or p.get('extraction_performed') is False: report['failed_predictions']+=1
        if p.get('context_sha256')!=c.get('context_sha256') or p.get('paper_id')!=c['paper_id'] or p.get('truncated_input') is True:
            report['content_mismatches']+=1
    if any(report[k] for k in ['missing_predictions','failed_predictions','content_mismatches']):
        report['reason']='预测不完整、失败或原文版本不一致；质量指标暂停计算，避免只统计成功案例。'
        return report
    tp=fp=fn=0
    positives=retrieved_positives=0
    all_retrieval_known=True
    for c in reviewed:
        p=by_id[c['case_id']]
        predicted=p.get('relations')
        if not isinstance(predicted,list): raise ValueError('Predictions require an explicit relation list')
        gold=Counter(tuple(r.get(k,'') for k in fields) for r in c['gold_relations'])
        pred=Counter()
        for r in predicted:
            if not isinstance(r,dict) or any(not isinstance(r.get(k,''),str) for k in fields):
                fp+=1
                continue
            quote=r.get('evidence_sentence','')
            if not isinstance(quote,str) or len(quote)<12 or quote not in c['context']:
                fp+=1
                continue
            pred[tuple(r.get(k,'') for k in fields)]+=1
        matches=sum((gold & pred).values())
        tp+=matches;fp+=sum(pred.values())-matches;fn+=sum(gold.values())-matches
        if c['gold_relevant']:
            positives+=1
            if not isinstance(p.get('retrieved'),bool): all_retrieval_known=False
            elif p['retrieved']: retrieved_positives+=1
    report.update(evaluation_ready=True,metrics={
        'true_positive_relations':tp,'false_positive_relations':fp,'false_negative_relations':fn,
        'precision':tp/(tp+fp) if tp+fp else None,
        'recall':tp/(tp+fn) if tp+fn else None,
        'f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None,
        'retrieval_recall':retrieved_positives/positives if positives and all_retrieval_known else None,
        'relevant_papers':positives})
    report['reason']='仅用于当前人工确认样本；小样本和抽样偏差限制了可推广性。'
    return report


def evaluate_retrieval(cases,predictions):
    validate_labels(cases)
    reviewed=[c for c in cases if c.get('gold_status')=='reviewed']
    mapping={p['case_id']:p for p in predictions}
    if len(mapping)!=len(predictions): raise ValueError('Duplicate prediction case ID')
    report={'task':'retrieval','reviewed_cases':len(reviewed),'unreviewed_cases':len(cases)-len(reviewed),
        'evaluation_ready':False,'metrics':None,
        'scope':'Recall on labeled pilot papers only; not full-corpus recall or retrieval precision'}
    if not reviewed:
        report['reason']='尚无人工确认的相关性标注。';return report
    if any(c['case_id'] not in mapping or mapping[c['case_id']].get('complete') is not True
           or not isinstance(mapping[c['case_id']].get('retrieved'),bool)
           or mapping[c['case_id']].get('paper_id')!=c['paper_id']
           or mapping[c['case_id']].get('context_sha256')!=c['context_sha256'] for c in reviewed):
        report['reason']='检索记录缺失、失败或原文版本不一致。';return report
    positives=[c for c in reviewed if c['gold_relevant']]
    found=sum(mapping[c['case_id']]['retrieved'] for c in positives)
    report.update(evaluation_ready=True,metrics={'relevant_papers':len(positives),'retrieved_relevant_papers':found,
        'retrieval_recall':found/len(positives) if positives else None})
    return report
