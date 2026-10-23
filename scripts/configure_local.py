"""Link an existing model configuration; never copy or print its API credential."""
from __future__ import annotations
import argparse
import os
import secrets
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from config import PROJECT_ROOT, read_env_file


def configure(model_env:Path,corpus:Path):
    model_env=model_env.resolve()
    corpus=corpus.resolve()
    settings=read_env_file(model_env)
    if not settings.get('LLM_API_KEY') or not settings.get('LLM_MODEL'):
        raise ValueError('原配置缺少模型名称或密钥。')
    if not corpus.is_file(): raise ValueError('语料数据库不存在。')
    destination=PROJECT_ROOT/'.env'
    data='\n'.join(['# Private local configuration; excluded from publication.',
        'RAG_ENABLE_LIVE=false','RAG_ACCESS_PASSWORD='+secrets.token_urlsafe(24),
        'RAG_MODEL_ENV_FILE='+str(model_env),'RAG_CORPUS_DB='+str(corpus),'LLM_TIMEOUT_SECONDS=45',''])
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as handle: handle.write(data)
    return destination


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='只读引用原模型配置，并设置本地私有访问口令')
    parser.add_argument('--model-env',type=Path,required=True)
    parser.add_argument('--corpus',type=Path,required=True)
    args=parser.parse_args()
    try:
        destination=configure(args.model_env,args.corpus)
    except (ValueError,FileExistsError) as exc:
        raise SystemExit(str(exc))
    print('本地配置已建立；真实模式默认关闭。访问口令保存在本地 .env，未输出到终端。')
