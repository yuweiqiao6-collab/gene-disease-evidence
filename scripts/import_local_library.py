"""Build a private, bounded-memory full-text index from existing parsed PDF pages."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from corpus_store import CorpusStore


def import_library(pages:Path,pdf_dir:Path,output:Path,report:Path):
    pages=pages.resolve();pdf_dir=pdf_dir.resolve();output=output.resolve();report=report.resolve()
    if output.exists() or report.exists():raise ValueError('输出已存在，请选择新文件名，原库保持不变。')
    if output==report or not pdf_dir.is_dir():raise ValueError('请提供不同输出路径和有效PDF目录。')
    inventory={}
    for path in sorted(pdf_dir.rglob('*')):
        if path.is_file() and path.suffix.lower()=='.pdf':
            if path.name in inventory:raise ValueError('PDF文件名重复，不能仅按文件名绑定来源。')
            inventory[path.name]=path
    counts=Counter();characters=Counter();files={};reverse_files={};source_hash=hashlib.sha256()
    def records():
        with pages.open('rb') as source:
            for number,line in enumerate(source,1):
                source_hash.update(line)
                if not line.strip():continue
                item=json.loads(line)
                pid=str(item['paper_id']);name=item['file_name'];page=item['page'];text=item['text']
                if name not in inventory or Path(name).name!=name:raise ValueError(f'第{number}行无法唯一对应原PDF文件。')
                if not isinstance(page,int) or page<1 or not isinstance(text,str):raise ValueError(f'第{number}行页码或文本无效。')
                if pid in files and files[pid]!=name:raise ValueError('一个论文标识对应多个文件。')
                if name in reverse_files and reverse_files[name]!=pid:raise ValueError('一个文件对应多个论文标识。')
                reverse_files[name]=pid
                files[pid]=name;counts[pid]+=1;characters[pid]+=len(text.strip())
                if not text.strip():continue
                for start in range(0,len(text),2600):
                    context=text[start:start+3000]
                    if not context.strip():continue
                    yield dict(chunk_id=hashlib.sha256(f'{pid}\0{page}\0{start}'.encode()).hexdigest(),
                        paper_id=pid,file_name=name,title=item.get('title') or pid,source_type='local_pdf',
                        content_level='fulltext',section='parsed_pdf_page',page=page,context=context,
                        sentence=context,source_location=f'{name} / 第{page}页 / 字符{start+1}–{start+len(context)}')
                    if start+3000>=len(text):break
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    def progress(n):
        if n%25000==0:print(f'已索引{n}个文本片段',flush=True)
    count=CorpusStore.build(output,records(),progress=progress)
    readable={files[pid] for pid,n in characters.items() if n>0}
    missing=[name for name in inventory if name not in readable]
    summary={'pdf_files':len(inventory),'parsed_paper_ids':len(files),'searchable_paper_ids':sum(n>0 for n in characters.values()),
        'pdf_files_without_readable_text':len(missing),'page_records':sum(counts.values()),'chunk_count':count,
        'pages_sha256':source_hash.hexdigest(),'unreadable_files':missing,'source':'existing_parsed_pdf_pages',
        'coverage_note':'文件数量不等于去重后的论文数量；空文本未进行文字识别；引用限于解析文本。'}
    import sqlite3
    with sqlite3.connect(output) as conn:
        conn.executemany('INSERT INTO corpus_info(key,value) VALUES(?,?)',[(k,str(summary[k])) for k in
            ('pdf_files','pdf_files_without_readable_text','page_records','pages_sha256')])
    os.chmod(output,0o600)
    report.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with report.open('x',encoding='utf-8') as handle:json.dump(summary,handle,ensure_ascii=False,indent=2);handle.write('\n')
    os.chmod(report,0o600)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='从现有逐页文本建立私有全文库，不调用模型或联网')
    parser.add_argument('--pages',type=Path,required=True)
    parser.add_argument('--pdf-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    try:result=import_library(args.pages,args.pdf_dir,args.output,args.report)
    except (ValueError,OSError,KeyError) as exc:raise SystemExit(str(exc))
    print(json.dumps({k:v for k,v in result.items() if k!='unreadable_files'},ensure_ascii=False,indent=2))
