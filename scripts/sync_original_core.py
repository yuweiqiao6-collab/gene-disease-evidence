"""Publish identical original retrieval modules, with a verifiable source manifest."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODULES=('retrieval.py','text_utils.py','original_query_utils.py','hybrid_retrieval.py',
         'vector_store.py','cache_manager.py','embedding_cache.py','embedding_generator.py',
         'mini_vector_store.py','reranker.py','retrieval_pipeline.py','pubmed_client.py',
         'index_manager.py','alias_lookup.py')


def sync(source=None, check=False):
    manifest=ROOT/'src/original_core_manifest.json'
    if check:
        data=json.loads(manifest.read_text())
        for name,value in data['files'].items():
            assert hashlib.sha256((ROOT/'src'/name).read_bytes()).hexdigest()==value, name
            if source is not None:
                assert (source/name).read_bytes()==(ROOT/'src'/name).read_bytes(), name
        return len(data['files'])
    if source is None:
        raise ValueError('Specify original source directory')
    data={'canonical_project':'rag_gene_disease_extractor/src','engine':'original-candidate-pipeline-v2','files':{}}
    for name in MODULES:
        raw=(source/name).read_bytes()
        (ROOT/'src'/name).write_bytes(raw)
        data['files'][name]=hashlib.sha256(raw).hexdigest()
    manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    return len(MODULES)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    print(f'{sync(args.source,args.check)} original core modules verified or synchronized.')
