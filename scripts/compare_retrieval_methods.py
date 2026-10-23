"""冻结候选的三方法对比与中文盲核对；不调用文献或大语言模型接口。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))


def forbid_network():
    def guard(event,args):
        if event in {'socket.connect','socket.getaddrinfo'}:
            raise RuntimeError('本评测禁止联网、下载模型或访问付费接口。')
    sys.addaudithook(guard)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    run=sub.add_parser('run',help='执行本地模型对比')
    run.add_argument('--dataset',type=Path,required=True)
    run.add_argument('--output',type=Path,required=True)
    run.add_argument('--embedding-model',type=Path,required=True)
    run.add_argument('--reranker-model',type=Path,required=True)
    run.add_argument('--ks',type=int,nargs='+',default=[5,10,15])
    run.add_argument('--repeats',type=int,default=3)
    run.add_argument('--coarse',type=int,default=500)
    run.add_argument('--encode',type=int,default=500)
    scoring=sub.add_parser('score',help='只导入人工表与计算已具备条件的指标')
    scoring.add_argument('--run-dir',type=Path,required=True)
    scoring.add_argument('--workbook',type=Path,required=True)
    scoring.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('输出已存在；请使用新路径，不能覆盖结果或人工工作。')
    forbid_network()
    from retrieval_benchmark import compare,save_json,read_judgments,score
    if args.action=='score':
        manifest=json.loads((args.run_dir/'review-manifest.json').read_text())
        runs=[json.loads(s) for s in (args.run_dir/'runs.jsonl').read_text().splitlines() if s.strip()]
        labels=read_judgments(manifest,args.workbook)
        report=score(manifest,runs,labels)
        save_json(args.output,report)
        print(json.dumps({k:report[k] for k in ['judged','insufficient','unfilled']},ensure_ascii=False))
        return
    for path in (args.embedding_model,args.reranker_model):
        if not path.is_dir():parser.error('需要已有本地模型目录，不能自动下载。')
    os.environ.update(EMBEDDING_MODEL=str(args.embedding_model.resolve()),
        RERANKER_MODEL=str(args.reranker_model.resolve()),EMBEDDING_DEVICE='cpu',
        EMBEDDING_BATCH_SIZE='4',ENABLE_HYBRID_RETRIEVAL='true',HYBRID_FUSION_STRATEGY='rrf',RRF_K='60',
        VECTOR_TOP_K='100',RAG_RETRIEVAL_CACHE_DIR=str(args.output.resolve()/'cache'),
        HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',CUDA_VISIBLE_DEVICES='',
        OMP_NUM_THREADS='2',MKL_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2',TOKENIZERS_PARALLELISM='false')
    os.nice(10)
    import torch
    torch.set_num_threads(2);torch.set_num_interop_threads(1)
    dataset=json.loads(args.dataset.read_text())
    try:
        report=compare(dataset,args.output,ks=args.ks,repeats=args.repeats,coarse=args.coarse,encode=args.encode)
    except Exception as exc:
        if args.output.is_dir():
            save_json(args.output/'status.json',{'state':'failed','error_type':type(exc).__name__,
                'message':str(exc),'automatic_retry':False})
        raise
    from importlib.metadata import version
    from cache_manager import embedding_signature
    save_json(args.output/'environment.json',{'python':sys.version,'torch':torch.__version__,
        'sentence_transformers':version('sentence-transformers'),'numpy':version('numpy'),
        'cpu_threads':torch.get_num_threads(),'device':'cpu','embedding_signature':embedding_signature(),
        'embedding_model':args.embedding_model.name,'reranker_model':args.reranker_model.name,
        'fusion_strategy':'rrf','rrf_k':60,'vector_top_k':100,'embedding_max_tokens':512})
    print(json.dumps({'queries':len(report['queries']),'stable_rankings':report['stable_rankings'],
        'model_api_calls':0,'quality_status':report['quality_status']},ensure_ascii=False))


if __name__=='__main__':main()
