"""Owner-scoped document collection with identifier-based deduplication."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from uuid import uuid4
from history_store import owner_digest
from literature_search import normalize_doi,utc_now


def identifiers(paper):
    result={}
    for field,pattern in [('pmid',r'\d+'),('pmcid',r'PMC\d+')]:
        value=str(paper.get(field,'') or '')
        if value:
            if not re.fullmatch(pattern,value): raise ValueError('论文编号格式不正确。')
            result[field]=value
    doi=normalize_doi(paper.get('doi',''))
    if doi: result['doi']=doi
    if not result and paper.get('paper_id'): result['local_id']=str(paper['paper_id'])
    if not result: raise ValueError('文献缺少可追溯的编号。')
    return result


def summary(paper):
    return {k:copy.deepcopy(v) for k,v in paper.items() if k not in {'fulltext_chunks','local_chunks'}}


class LiteratureStore:
    def __init__(self,path:Path):
        self.path=path
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS papers (
                id TEXT PRIMARY KEY,owner_digest TEXT NOT NULL,updated TEXT NOT NULL,payload TEXT NOT NULL,summary TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS papers_owner ON papers(owner_digest,updated DESC);
                CREATE TABLE IF NOT EXISTS identifiers (
                    owner_digest TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    PRIMARY KEY(owner_digest,kind,value));''')
        os.chmod(path,0o600)

    def connect(self):
        conn=sqlite3.connect(self.path,timeout=30)
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def upsert(self,owner,paper):
        digest=owner_digest(owner)
        ids=identifiers(paper)
        incoming=copy.deepcopy(paper)
        incoming['doi']=normalize_doi(incoming.get('doi',''))
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            matches=set()
            for kind,value in ids.items():
                record=conn.execute('SELECT paper_id FROM identifiers WHERE owner_digest=? AND kind=? AND value=?',(digest,kind,value)).fetchone()
                if record: matches.add(record[0])
            if len(matches)>1: raise ValueError('论文标识指向不同收藏，请人工核对后合并。')
            document_id=next(iter(matches)) if matches else uuid4().hex
            if matches:
                old=json.loads(conn.execute('SELECT payload FROM papers WHERE id=? AND owner_digest=?',(document_id,digest)).fetchone()[0])
                old_ids=identifiers(old)
                if any(kind in old_ids and old_ids[kind]!=value for kind,value in ids.items()):
                    raise ValueError('同一文献的编号相互冲突，未覆盖已有收藏。')
                merged=copy.deepcopy(old)
                for key,value in incoming.items():
                    if key in {'sources','local_chunks','local_corpus_paper_ids','document_id','saved_at','fulltext_status','paper_id'}: continue
                    if value not in (None,'',[]):
                        if key in {'abstract','abstract_sha256','abstract_status','title','source_type','content_level'} and old.get(key) and incoming.get('source_type') not in {'pubmed_abstract','europepmc_abstract'}: continue
                        merged[key]=value
                merged['sources']=old.get('sources',[])+[s for s in incoming.get('sources',[]) if s not in old.get('sources',[])]
                local_changed=bool(incoming.get('local_snapshot_sha256') and incoming.get('local_snapshot_sha256')!=old.get('local_snapshot_sha256'))
                old_chunks=[] if local_changed else old.get('local_chunks',[])
                chunks={hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest():c for c in old_chunks+incoming.get('local_chunks',[])}
                merged['local_chunks']=list(chunks.values())
                old_ids=[] if local_changed else old.get('local_corpus_paper_ids',[])
                merged['local_corpus_paper_ids']=list(dict.fromkeys(old_ids+incoming.get('local_corpus_paper_ids',[])))
                merged['fulltext_status']=old.get('fulltext_status','not_requested')
                incoming=merged
            elif conn.execute('SELECT COUNT(*) FROM papers WHERE owner_digest=?',(digest,)).fetchone()[0]>=1000:
                raise ValueError('本研究空间已保存 1000 篇文献，请先整理或删除旧文献。')
            incoming.update(document_id=document_id,saved_at=utc_now())
            incoming.setdefault('fulltext_status','not_requested')
            conn.execute('''INSERT INTO papers VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                updated=excluded.updated,payload=excluded.payload,summary=excluded.summary''',
                (document_id,digest,incoming['saved_at'],json.dumps(incoming,ensure_ascii=False),json.dumps(summary(incoming),ensure_ascii=False)))
            for kind,value in identifiers(incoming).items():
                conn.execute('INSERT OR IGNORE INTO identifiers VALUES(?,?,?,?)',(digest,kind,value,document_id))
        return summary(incoming)

    def get(self,owner,document_id):
        with self.connect() as conn:
            record=conn.execute('SELECT payload FROM papers WHERE id=? AND owner_digest=?',(document_id,owner_digest(owner))).fetchone()
        if record is None: raise PermissionError('无法访问该文献。')
        return json.loads(record[0])

    def list(self,owner,limit=200):
        with self.connect() as conn:
            records=conn.execute('SELECT summary FROM papers WHERE owner_digest=? ORDER BY updated DESC LIMIT ?',
                (owner_digest(owner),max(1,min(1000,int(limit))))).fetchall()
        return [json.loads(r[0]) for r in records]

    def update_fulltext(self,owner,document_id,result):
        if result.get('fulltext_status') not in {'available','unavailable','failed'}:
            raise ValueError('无效全文状态。')
        if set(result)-{'fulltext_status','fulltext_message','pmcid','fulltext_sha256','fulltext_source_url',
            'fulltext_api_url','fulltext_retrieved_at','fulltext_license','fulltext_chunks','fulltext_scope','fulltext_omitted_elements'}:
            raise ValueError('不允许通过全文结果修改摘要或来源身份。')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            digest=owner_digest(owner)
            record=conn.execute('SELECT payload FROM papers WHERE id=? AND owner_digest=?',(document_id,digest)).fetchone()
            if not record: raise PermissionError('无法访问该文献。')
            paper=json.loads(record[0])
            if result.get('pmcid'):
                old=paper.get('pmcid')
                if old and old!=result['pmcid']: raise ValueError('全文编号冲突。')
                collision=conn.execute('SELECT paper_id FROM identifiers WHERE owner_digest=? AND kind=? AND value=?',(digest,'pmcid',result['pmcid'])).fetchone()
                if collision and collision[0]!=document_id: raise ValueError('该全文已对应另一份收藏，请核对身份。')
                conn.execute('INSERT OR IGNORE INTO identifiers VALUES(?,?,?,?)',(digest,'pmcid',result['pmcid'],document_id))
            # A transient refetch failure must not erase previously verified content.
            if paper.get('fulltext_status')=='available' and result['fulltext_status']!='available':
                paper['fulltext_last_attempt']=result
            else:
                paper.update(copy.deepcopy(result))
                if result['fulltext_status']=='available': paper.pop('fulltext_last_attempt',None)
            paper['saved_at']=utc_now()
            conn.execute('UPDATE papers SET updated=?,payload=?,summary=? WHERE id=? AND owner_digest=?',
                (paper['saved_at'],json.dumps(paper,ensure_ascii=False),json.dumps(summary(paper),ensure_ascii=False),document_id,digest))
        return summary(paper)

    def delete(self,owner,document_id):
        self.get(owner,document_id)
        with self.connect() as conn: conn.execute('DELETE FROM papers WHERE id=? AND owner_digest=?',(document_id,owner_digest(owner)))


def from_local_chunks(chunks):
    grouped={}
    for c in chunks:
        if c.get('synthetic') or c.get('source_type')=='synthetic_demo': continue
        pid=c.get('paper_id','')
        if not pid: continue
        if pid not in grouped:
            pmid=pid[4:] if re.fullmatch(r'PMID\d+',pid) else ''
            abstract=c['context'] if c.get('source_type')=='pubmed_abstract' else ''
            grouped[pid]={'paper_id':pid,'pmid':pmid,'pmcid':c.get('pmcid',''),'doi':c.get('doi',''),
                'title':c.get('title','') or pid,'abstract':abstract,
                'abstract_sha256':hashlib.sha256(abstract.encode()).hexdigest(),
                'abstract_status':'available' if abstract else 'not_imported',
                'source_url':f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/' if pmid else '',
                'source_type':c.get('source_type','local_snippet'),'content_level':'abstract' if abstract else 'snippet',
                'sources':[{'provider':'local_corpus','paper_id':pid}],'local_chunks':[],
                'fulltext_status':'not_requested','retrieved_at':utc_now()}
        grouped[pid]['local_chunks'].append(c)
        if c.get('source_type')=='local_pdf':
            grouped[pid]['local_text_available']=True
            grouped[pid]['file_name']=c.get('file_name','')
    return list(grouped.values())


def paper_chunks(paper,mode='auto'):
    if mode not in {'auto','abstract'}: raise ValueError('不支持的证据范围。')
    chunks=copy.deepcopy(paper['fulltext_chunks']) if mode=='auto' and paper.get('fulltext_status')=='available' else []
    for c in chunks:c.update(publication_types=paper.get('publication_types',[]),publication_status=paper.get('publication_status','not_assessed'))
    abstract=paper.get('abstract','')
    if abstract:
        sha=hashlib.sha256(abstract.encode()).hexdigest()
        return chunks+[{'chunk_id':paper['paper_id']+'_abstract_'+sha[:12],'paper_id':paper['paper_id'],
            'title':paper['title'],'source_type':'europepmc_abstract' if paper.get('source_type')=='europepmc_abstract' else 'pubmed_abstract','content_level':'abstract',
            'publication_types':paper.get('publication_types',[]),'publication_status':paper.get('publication_status','not_assessed'),
            'pmid':paper.get('pmid',''),'pmcid':paper.get('pmcid',''),'doi':paper.get('doi',''),
            'section':'abstract','context':abstract,'sentence':abstract,'source_location':paper['paper_id']+' / 摘要',
            'source_url':paper.get('source_url',''),'page':None,'abstract_sha256':sha}]
    return chunks or (copy.deepcopy(paper.get('local_chunks',[])) if mode=='auto' else [])
