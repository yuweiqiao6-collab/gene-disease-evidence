import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from evaluation import evaluate, evaluate_retrieval, validate_labels,EVALUATION_PROFILES
from io_utils import write_json


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


if __name__=='__main__':
    p=argparse.ArgumentParser(description='只针对人工确认且预测完整的真实样本计算指标')
    p.add_argument('--task',choices=['extraction','retrieval'],default='extraction')
    p.add_argument('--split',choices=['all','development','test'],default='all')
    p.add_argument('--profile',choices=list(EVALUATION_PROFILES),default='legacy',
        help='抽取评分：legacy=旧四字段；scope_measurement=含关系范围、研究对象和测量类型的七字段')
    p.add_argument('--annotations',type=Path,required=True)
    p.add_argument('--predictions',type=Path)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.task=='retrieval' and args.profile!='legacy':
        p.error('--profile scope_measurement applies to extraction only')
    cases=read_rows(args.annotations)
    validate_labels(cases)
    if args.split!='all': cases=[c for c in cases if c['split']==args.split]
    predictions=read_rows(args.predictions) if args.predictions else []
    report=evaluate_retrieval(cases,predictions) if args.task=='retrieval' else evaluate(cases,predictions,profile=args.profile)
    report['split']=args.split
    write_json(args.output,report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
