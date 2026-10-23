"""Write a separate private adapter config; preserve both existing .env files."""
import argparse
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def configure(source, python, output):
    values={'RAG_ORIGINAL_PYTHON':python.resolve(),
        'RAG_ORIGINAL_INDEX_JSON':source.resolve()/'data/processed/bm25_index.json',
        'RAG_ORIGINAL_EMBEDDING_MODEL':source.resolve()/'models/bge-m3-local',
        'RAG_ORIGINAL_RERANKER_MODEL':source.resolve()/'models/cross-encoder-ms-marco-MiniLM-L-6-v2'}
    if output.exists():raise ValueError('配置已存在，请手动检查，不自动覆盖。')
    if not all(p.exists() for p in values.values()):raise ValueError('原索引、Python环境或模型路径不存在。')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as stream:
        for key,value in values.items():stream.write(f'{key}={value}\n')
    output.chmod(0o600)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='复用原项目的索引与模型，不调用模型或修改原配置')
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--python',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'.env.original')
    args=parser.parse_args();configure(args.source,args.python,args.output)
    print('原流程私有配置已写入；保存研究空间恢复码后，重启自己的网页服务即可加载。')
