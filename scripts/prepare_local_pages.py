"""Prepare private page text from a PDF folder, one file at a time, without APIs."""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text_utils import clean_text


def prepare(pdf_dir:Path,output:Path,report:Path):
    import fitz
    if output.exists() or report.exists() or output.resolve()==report.resolve():
        raise ValueError('请为逐页文本和报告选择不同的新路径，已有文件不覆盖。')
    files=sorted(p for p in pdf_dir.rglob('*') if p.is_file() and p.suffix.lower()=='.pdf')
    if not files:raise ValueError('目录中没有PDF文件。')
    if len({p.name for p in files})!=len(files):raise ValueError('PDF文件名重复，先整理成可唯一识别的文件名。')
    result={'pdf_files':len(files),'parsed_files':0,'empty_files':[],'failed_files':[],'page_records':0}
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with output.open('x',encoding='utf-8') as handle:
        for path in files:
            rows=[]
            try:
                with fitz.open(path) as document:
                    if document.is_encrypted:raise ValueError('文件加密')
                    for number,page in enumerate(document,1):
                        rows.append(dict(paper_id=path.stem,file_name=path.name,title=document.metadata.get('title') or path.stem,
                            page=number,text=clean_text(page.get_text('text'))))
            except Exception:
                result['failed_files'].append(path.name);continue
            for row in rows:handle.write(json.dumps(row,ensure_ascii=False)+'\n')
            result['parsed_files']+=1;result['page_records']+=len(rows)
            if not any(r['text'] for r in rows):result['empty_files'].append(path.name)
    os.chmod(output,0o600)
    report.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with report.open('x',encoding='utf-8') as handle:json.dump(result,handle,ensure_ascii=False,indent=2);handle.write('\n')
    os.chmod(report,0o600)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='单进程提取本地PDF的可读页文本，不识别扫描图片')
    parser.add_argument('--pdf-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    try:result=prepare(args.pdf_dir,args.output,args.report)
    except (ValueError,OSError) as exc:raise SystemExit(str(exc))
    print(json.dumps(result,ensure_ascii=False,indent=2))
