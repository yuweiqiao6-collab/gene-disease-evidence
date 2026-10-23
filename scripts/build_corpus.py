"""Administrator-only migration. Never loads the legacy giant JSON index."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from corpus_store import CorpusStore


def iter_rows(path):
    with path.open(encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f'Invalid JSON at line {line_number}') from exc


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='按行迁移语料到全文检索数据库')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; choose a new filename to preserve the current database')
    if args.input.resolve() == args.output.resolve():
        parser.error('Input and output must differ')
    count = CorpusStore.build(args.output, iter_rows(args.input))
    print(f'Indexed {count} chunks into {args.output}')
