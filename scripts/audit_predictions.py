"""Recheck previously saved predictions offline; never create new model predictions."""
import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from support_checks import check_support,normalized,CHECK_VERSION
from io_utils import write_json,write_jsonl
from exports import safe_cell
from openpyxl import Workbook


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def review_workbook(audited,cases,path):
    book=Workbook();sheet=book.active;sheet.title='待人工核对'
    sheet.append(['案例编号','论文编号','查询基因','查询疾病','论文标题','来源链接','实际发送原文',
        '模型引用','模型表达方向','模型疾病关系','模型物种','自动核对提示',
        '人工：是否直接支持目标关系','人工：实际研究人群','人工：实际测量对象','人工：建议修正','复核人'])
    for p in audited:
        c=cases[p['case_id']]
        for row in p['relations']:
            context=c['context'][:3000] if p.get('truncated_input') else c['context']
            values=[p['case_id'],p['paper_id'],c['gene'],c['disease'],c.get('title',''),c.get('source_url',''),context,
                row['evidence_sentence'],row.get('expression_direction',''),row.get('disease_effect',''),row.get('species',''),
                '；'.join(row['support_messages'])]+['']*5
            sheet.append([safe_cell(value) for value in values])
    sheet.freeze_panes='A2'
    for column in ['G','H','L','P']: sheet.column_dimensions[column].width=85
    note=book.create_sheet('说明')
    for line in [
        '此表用于诊断已有模型结果，不是独立盲评的专家标准答案；不得直接作为医学准确率依据。',
        '所有人工栏默认空白。阅读原文与来源后再判断直接关系、实际研究对象及测量范围。',
        '英文原文和模型取值保留原样；increased=升高，decreased=降低，unchanged=未变化，unclear=不明确。',
        'biomarker_only=仅标志物或表达差异，associated=相关性，human=人。',
        '没有足够信息时写信息不足，不要自动改成没有关系。',
        '总表达与膜表面丰度、定位或活性不同；健康人用药不是疾病患者的直接表达证据。',
        '自动提示基于词汇检查，可能漏报或误报；不得代替人工原文核对。',
        '此表不会自动导入参考答案，也不会触发任何模型调用。',
    ]: note.append([line])
    note.column_dimensions['A'].width=120
    book.save(path)


def audit(annotations,predictions,output):
    if output.exists(): raise ValueError('输出目录已存在，避免覆盖先前复查。')
    cases=read_rows(annotations);previous=read_rows(predictions)
    mapping={c['case_id']:c for c in cases}
    if len(mapping)!=len(cases): raise ValueError('重复案例编号。')
    audited=[]
    for p in previous:
        c=mapping[p['case_id']]
        if p['paper_id']!=c['paper_id'] or p['context_sha256']!=hashlib.sha256(c['context'].encode()).hexdigest():
            raise ValueError('预测与原文身份或内容摘要不匹配。')
        context=c['context'][:3000] if p.get('truncated_input') else c['context']
        expected=p.get('sent_context_sha256',p['context_sha256'])
        if hashlib.sha256(context.encode()).hexdigest()!=expected: raise ValueError('实际发送文本无法核对，暂停复查。')
        current=copy.deepcopy(p)
        current['offline_recheck']=True
        for row in current['relations']:
            if len(normalized(row.get('evidence_sentence')))<12 or normalized(row['evidence_sentence']) not in normalized(context):
                raise ValueError('旧引用无法匹配发送原文。')
            row.update(check_support(row,context,c['gene'],c['disease']))
        audited.append(current)
    report={'operation':'offline_recheck','support_check_version':CHECK_VERSION,'new_model_requests':0,
        'cases':len(audited),'relations':sum(len(p['relations']) for p in audited),
        'flagged_relations':sum(r['support_status']=='flagged' for p in audited for r in p['relations']),
        'medical_accuracy':None,'expert_gold_created':False,
        'note':'This rechecks existing outputs with lexical hints; it does not run the revised prompt or establish medical correctness',
        'findings':[{'paper_id':p['paper_id'],'flags':[r['support_flags'] for r in p['relations']]} for p in audited]}
    output.mkdir(parents=True,mode=0o700)
    write_jsonl(output/'rechecked_predictions.jsonl',audited)
    write_json(output/'report.json',report)
    review_workbook(audited,mapping,output/'review-workbook.xlsx')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description='对已有模型输出离线复查，不调用模型、不修改原报告')
    p.add_argument('--annotations',type=Path,required=True)
    p.add_argument('--predictions',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    print(json.dumps(audit(args.annotations,args.predictions,args.output),ensure_ascii=False,indent=2))
