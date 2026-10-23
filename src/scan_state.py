"""Private atomic scan checkpoints, retained independently of temporary job files."""
import copy
import hashlib
import re
import shutil
from pathlib import Path
from io_utils import read_json,write_json
from prompts import source_text

PAPER_LABELS={'pending':'未处理','running':'处理中','processed':'已处理候选文字','needs_fulltext':'摘要不足，待查正文',
    'no_text':'暂无可读文字','no_candidates':'正文未命中，待人工检查','failed':'处理失败','interrupted':'调用中断，需确认后重试',
    'source_changed':'来源已变更，需重新检索','needs_context':'当前片段未检出，待补查','needs_review':'无新增命中，待人工核对'}
SCOPE_LABELS={'none':'未处理文字','abstract':'仅摘要','fulltext':'正文候选片段','mixed':'正文片段与摘要'}


class ScanStore:
    def __init__(self,root):self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    def directory(self,job_id):
        if not re.fullmatch(r'[a-f0-9]{32}',job_id):raise ValueError('扫描编号无效。')
        path=self.root/job_id
        if path.is_symlink():raise ValueError('扫描目录不可为链接。')
        return path
    def path(self,job_id,document_id):
        if not re.fullmatch(r'[a-f0-9]{32}',document_id):raise ValueError('文献编号无效。')
        return self.directory(job_id)/(document_id+'.json')
    def create(self,job_id,records,manifest):
        directory=self.directory(job_id);directory.mkdir(mode=0o700)
        for record in records:self.save(job_id,record)
        self.save_manifest(job_id,{**manifest,'document_ids':[r['document_id'] for r in records],'runs':[]})
    def save(self,job_id,record):write_json(self.path(job_id,record['document_id']),record)
    def load(self,job_id,document_id):
        value=read_json(self.path(job_id,document_id))
        if not value:raise ValueError('扫描检查点缺失，不能自动重发。')
        return value
    def manifest(self,job_id):
        value=read_json(self.directory(job_id)/'manifest.json')
        if not value:raise ValueError('扫描计划缺失，不能自动重发。')
        return value
    def save_manifest(self,job_id,value):write_json(self.directory(job_id)/'manifest.json',value)
    def records(self,job_id):return [self.load(job_id,d) for d in self.manifest(job_id)['document_ids']]
    def recover(self,job_id):
        for record in self.records(job_id):
            changed=False
            for candidate in record['candidates']:
                if candidate['state']=='in_flight':
                    candidate['state']='interrupted';changed=True
                    for attempt in candidate.get('attempts',[]):
                        if attempt['state']=='in_flight':attempt['state']='interrupted'
            if record['status']=='running' or changed:
                record['status']='interrupted';record['messages'].append('服务中断；未自动重发，已有调用可能计费。')
                self.save(job_id,record)
    def delete(self,job_id):
        directory=self.directory(job_id)
        if directory.is_dir():shutil.rmtree(directory)


def document_summary(record):
    candidates=record['candidates'];successful=[c for c in candidates if c['state']=='succeeded']
    levels={c['chunk'].get('content_level','snippet') for c in successful}
    scope='mixed' if len(levels)>1 else ('fulltext' if levels=={'fulltext'} else 'abstract' if levels=={'abstract'} else 'none')
    rows=[r for c in successful for r in c['rows']]
    return {k:copy.deepcopy(record.get(k)) for k in ['document_id','paper_id','title','status','messages','fulltext_status']}|{
        'source_revision':record['source_revision'],
        'scope':scope,'candidate_count':len(candidates),'successful_chunks':len(successful),
        'attempted_chunks':sum(bool(c.get('attempts')) for c in candidates),
        'remaining_chunks':sum(c['state']=='pending' for c in candidates),
        'interrupted_chunks':sum(c['state']=='interrupted' for c in candidates),
        'failed_chunks':sum(c['state']=='failed' for c in candidates),
        'model_calls':sum(a.get('model_calls') or 0 for c in candidates for a in c.get('attempts',[])),
        'eligible':sum(r.get('baseline_status')=='eligible' for r in rows),
        'pending_evidence':sum(r.get('baseline_status')=='needs_scope_review' for r in rows),
        'processed_characters':sum(len(c['chunk']['context']) for c in successful),
        'candidate_versions':[{'chunk_id':c['chunk']['chunk_id'],'source_location':c['chunk'].get('source_location',''),
            'content_sha256':hashlib.sha256(source_text(c['chunk'],'disease_vs_normal_v1').encode()).hexdigest(),
            'state':c['state'],'attempts':copy.deepcopy(c.get('attempts',[]))} for c in candidates],
        'context_truncated':any(c['chunk'].get('context_truncated') or any(s.get('context_truncated') for s in c['chunk'].get('context_segments',[])) for c in successful)}


def counts(documents):
    return {'total':len(documents),'pending':sum(d['status'] in {'pending','running'} for d in documents),
        'processed':sum(d['status']=='processed' for d in documents),
        'needs_fulltext':sum(d['status']=='needs_fulltext' for d in documents),
        'needs_context':sum(d['status'] in {'needs_context','needs_review'} for d in documents),
        'failed':sum(d['status'] in {'failed','interrupted','source_changed'} for d in documents),
        'no_text':sum(d['status'] in {'no_text','no_candidates'} for d in documents),
        'abstract_only':sum(d['scope']=='abstract' for d in documents),
        'body_processed':sum(d['scope'] in {'fulltext','mixed'} for d in documents)}
