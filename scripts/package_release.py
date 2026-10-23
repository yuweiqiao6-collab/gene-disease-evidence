"""Package only reviewed source roots; runtime and credentials are never candidates."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DIRECTORIES={'src','scripts','tests','docs','examples','.streamlit','.github'}
FILES={'AGENTS.md','README.md','README.zh-CN.md','LICENSE_POLICY.md','VERSION','requirements.txt','requirements-dev.txt',
       'requirements-lock.txt','requirements-research.txt','.env.example','.gitignore','.dockerignore',
       'Dockerfile','compose.yaml','pytest.ini'}
EXTENSIONS={'.py','.md','.json','.toml','.yml','.yaml','.txt','.sh'}
BINARY_ASSETS={f'docs/assets/{name}.png' for name in ('home','evidence','conditions','insufficient','workspace')}
PUBLIC_DOCS={f'docs/{name}.md' for name in (
    'USER_GUIDE','MODEL_SETUP','FULL_LIBRARY','BASELINE_GUIDE','BATCH_SCAN_GUIDE',
    'LITERATURE_GUIDE','ARCHITECTURE','DEPLOYMENT','SHOWCASE','PORTFOLIO_REPORT',
    'VALIDATION','GITHUB_UPLOAD')} | BINARY_ASSETS
SECRET=re.compile(r'(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


def public_files(root=ROOT):
    paths=[]
    candidates=[root/name for name in FILES if (root/name).exists()]
    for directory in sorted(DIRECTORIES):
        folder=root/directory
        if folder.is_symlink():
            raise ValueError(f'Publication symlink forbidden: {directory}')
        if folder.is_dir():
            if directory=='docs':
                for name in sorted(PUBLIC_DOCS):
                    relative=Path(name)
                    for parent in relative.parents:
                        if (root/parent).is_symlink():
                            raise ValueError(f'Publication symlink forbidden: {parent}')
                    candidates.append(root/relative)
            else:
                candidates.extend(folder.rglob('*'))
    for path in candidates:
        relative=path.relative_to(root)
        if relative.parts[0] not in DIRECTORIES and relative.as_posix() not in FILES:
            continue
        if any(part in {'__pycache__','.pytest_cache'} for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f'Publication symlink forbidden: {relative}')
        if not path.is_file():
            continue
        if path.name in {'secrets.toml','secrets.json'} or (path.name.startswith('.env') and path.name!='.env.example'):
            raise ValueError(f'Credential file forbidden: {relative}')
        if relative.as_posix() not in FILES and path.suffix not in EXTENSIONS and relative.as_posix() not in BINARY_ASSETS:
            raise ValueError(f'Unexpected publication file: {relative}')
        data=path.read_bytes()
        if len(data)>1_000_000:
            raise ValueError(f'Unexpected large publication file: {relative}')
        if relative.as_posix() in BINARY_ASSETS:
            if not (data.startswith(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR') and data.endswith(b'\x00\x00\x00\x00IEND\xaeB`\x82')):
                raise ValueError(f'Invalid PNG publication asset: {relative}')
            paths.append(path)
            continue
        text=data.decode('utf-8')
        if SECRET.search(text):
            raise ValueError(f'Possible secret in {relative}; value withheld')
        if ('/data1/' + 'home/') in text:
            raise ValueError(f'Private machine/account detail in {relative}')
        if path.name=='.env.example':
            for line in text.splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    key,value=line.split('=',1)
                    if any(marker in key.upper() for marker in ('KEY','PASSWORD','TOKEN','SECRET')) and value.strip():
                        raise ValueError('Example credentials must be empty')
        paths.append(path)
    return sorted(paths)


def package(root, output):
    paths=public_files(root)
    version=(root/'VERSION').read_text().strip()
    manifest={'version':version,'files':{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            info=zipfile.ZipInfo(f'gene-disease-evidence-app/{path.relative_to(root).as_posix()}')
            info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16
            archive.writestr(info,path.read_bytes())
        archive.writestr('gene-disease-evidence-app/RELEASE_MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(f'{digest}  {output.name}\n')
    return len(paths),digest


def github_upload(root, output):
    paths=public_files(root)
    version=(root/'VERSION').read_text().strip()
    manifest={'version':version,'files':{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    sections={'app-folders':{},'docs-folder':{},'root-files':{}}
    for path in paths:
        relative=path.relative_to(root)
        group='docs-folder' if relative.parts[0]=='docs' else ('root-files' if len(relative.parts)==1 else 'app-folders')
        sections[group][relative.as_posix()]=path.read_bytes()
    sections['root-files']['RELEASE_MANIFEST.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    batches={}
    for label,files in sections.items():
        items=list(files.items())
        for offset in range(0,len(items),100):
            suffix=f'-{offset//100+1}' if len(items)>100 else ''
            batches[f'{len(batches)+1:02d}-{label}{suffix}']=dict(items[offset:offset+100])
    instructions=('解压后，按编号逐批将批次文件夹里面的内容上传至仓库根目录。\n'
        '不要上传外层目录、批次目录或压缩包本身。\n'
        '显示隐藏文件，以便上传.github、.streamlit及根目录点号文件。\n'
        '应用可能拆为多批；不同批次中相同src/或tests/目录的内容依次合并，不删除前一批。\n'
        +'\n'.join(f'{name}: {len(files)}个文件' for name,files in batches.items())+'\n'
        'docs只保留下列12份指南和assets中的5张截图；开发历史不随包发布。\n'
        +'\n'.join(sorted(PUBLIC_DOCS))+'\n'
        '同名上传会替换文件；不会自动删除远端多余文件。先另存自己增加的内容，再核对旧文件。\n'
        '请保留自己增加的许可证。原文献、索引、模型与私有配置不在公开包内。\n')
    output.parent.mkdir(parents=True,exist_ok=True)
    prefix=f'gene-disease-evidence-app-{version}-github-upload'
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for group,files in batches.items():
            for name,data in files.items():
                info=zipfile.ZipInfo(f'{prefix}/{group}/{name}')
                info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
                archive.writestr(info,data)
        archive.writestr(prefix+'/UPLOAD_INSTRUCTIONS.txt',instructions)
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(f'{digest}  {output.name}\n')
    return {name:len(files) for name,files in batches.items()},digest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='检查并打包可公开发布的源代码')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--github-upload',action='store_true',help='生成保持仓库路径的三个网页上传批次')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    suffix='-github-upload' if args.github_upload else ''
    output=args.output or ROOT/'artifacts'/f'gene-disease-evidence-app-{(ROOT/"VERSION").read_text().strip()}{suffix}.zip'
    if args.check:
        print(f'Publication checks passed: {len(public_files())} source files; no runtime data packaged.')
    else:
        count,digest=github_upload(ROOT,output) if args.github_upload else package(ROOT,output)
        print(f'Packaged {count}: {output}\nSHA256: {digest}')
