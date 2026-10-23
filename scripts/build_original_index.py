"""Build the original BM25 format from explicitly supplied, unchanged chunks."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from retrieval import build_index
from io_utils import write_json


if __name__=='__main__':
    p=argparse.ArgumentParser(description='从已有原文片段建立原格式BM25索引，不重新分片、不生成向量、不调用模型')
    p.add_argument('--chunks',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():p.error('输出已存在，不覆盖旧索引。')
    rows=[json.loads(line) for line in args.chunks.read_text().splitlines() if line.strip()]
    if any(not isinstance(c.get('context'),str) or not c.get('chunk_id') for c in rows):p.error('每条片段需要context原文和chunk_id编号。')
    write_json(args.output,build_index(rows))
    print(f'已建立{len(rows)}组原片段的BM25索引；没有生成向量。')
