"""Durable research history; recovery tokens are never written to the database."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from schemas import EXPRESSION_VALUES, DISEASE_EFFECT_VALUES, RELATION_TYPE_VALUES, RELATION_SCOPE_VALUES, POPULATION_VALUES, MEASUREMENT_VALUES
from baseline import PROFILE as BASELINE_PROFILE,COMPARISON_VALUES,SIGNIFICANCE_VALUES,assess as assess_baseline,quote_segment

TERMINAL = {'completed','partial','failed','no_candidates','no_relations','interrupted','paused'}
REVIEWABLE = {'expression_direction','disease_effect','relation_type','species','tissue_or_cell','evidence_type','statistical_evidence',
    'relation_scope','population_context','measurement_type','comparison_type','disease_group','normal_group','comparison_sentence','significance'}


def owner_digest(owner):
    if not isinstance(owner,str) or not re.fullmatch(r'[a-f0-9]{64}',owner):
        raise PermissionError('研究空间恢复码无效。')
    return hashlib.sha256(owner.encode()).hexdigest()


def evidence_id(row):
    fields=['paper_id','chunk_id','gene_or_target','disease','evidence_sentence','disease_effect','relation_type',
        'expression_direction','measurement_type','population_context']
    if row.get('task_profile')==BASELINE_PROFILE:fields+=['comparison_type','comparison_sentence','disease_group','normal_group','significance','species','tissue_or_cell']
    return hashlib.sha256(json.dumps([row.get(k,'') for k in fields],ensure_ascii=False).encode()).hexdigest()[:32]


class HistoryStore:
    def __init__(self,path:Path):
        self.path=path
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner_digest TEXT NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_owner_created ON jobs(owner_digest,created DESC);
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    evidence_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    decision TEXT NOT NULL, changes TEXT NOT NULL, reason TEXT NOT NULL,
                    reviewer TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(job_id,evidence_id,revision));
            ''')
            if 'summary' not in {r[1] for r in conn.execute('PRAGMA table_info(jobs)')}:
                conn.execute("ALTER TABLE jobs ADD COLUMN summary TEXT NOT NULL DEFAULT '{}'")
        os.chmod(path,0o600)

    def connect(self):
        conn=sqlite3.connect(self.path,timeout=30)
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def save(self,job):
        payload={key:copy.deepcopy(getattr(job,key)) for key in
            ['id','state','progress','message','created','finished','rows','counts','issues','query_urls','provenance','documents']}
        payload['query']=asdict(job.query)
        payload['synthetic']=job.query.source=='demo'
        summary={key:payload[key] for key in ['id','state','created','query','synthetic','counts']}
        with self.connect() as conn:
            conn.execute('''INSERT INTO jobs(id,owner_digest,state,created,payload,summary) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                state=excluded.state,payload=excluded.payload,summary=excluded.summary''',
                (job.id,owner_digest(job.owner),job.state,job.created,json.dumps(payload,ensure_ascii=False),json.dumps(summary,ensure_ascii=False)))

    def load(self,owner,job_id):
        digest=owner_digest(owner)
        with self.connect() as conn:
            record=conn.execute('SELECT payload FROM jobs WHERE id=? AND owner_digest=?',(job_id,digest)).fetchone()
        if not record:
            raise PermissionError('无法访问该任务。')
        return json.loads(record[0])

    def list(self,owner,limit=200):
        with self.connect() as conn:
            records=conn.execute('SELECT summary FROM jobs WHERE owner_digest=? ORDER BY created DESC LIMIT ?',
                (owner_digest(owner),max(1,min(200,int(limit))))).fetchall()
        return [json.loads(r[0]) for r in records]

    def recover_interrupted(self):
        with self.connect() as conn:
            records=conn.execute("SELECT id,payload FROM jobs WHERE state IN ('queued','running')").fetchall()
            for job_id,serialized in records:
                payload=json.loads(serialized)
                payload.update(state='interrupted',finished=time.time(),progress=1,
                    message='服务重启导致任务中断；结果历史已保留，未自动重复模型请求。')
                payload.setdefault('issues',[]).append('未完成任务需要手动重新提交。')
                summary={key:payload[key] for key in ['id','state','created','query','synthetic','counts']}
                conn.execute('UPDATE jobs SET state=?,payload=?,summary=? WHERE id=?',
                    ('interrupted',json.dumps(payload,ensure_ascii=False),json.dumps(summary,ensure_ascii=False),job_id))

    def decorated_rows(self,owner,job_id):
        payload=self.load(owner,job_id)
        with self.connect() as conn:
            records=conn.execute('''SELECT evidence_id,revision,decision,changes,reason,reviewer,created
                FROM reviews WHERE job_id=? ORDER BY id''',(job_id,)).fetchall()
        latest={record[0]:record for record in records}
        rows=[]
        for original in payload['rows']:
            row=copy.deepcopy(original)
            eid=original.get('evidence_id') or evidence_id(original)
            row['evidence_id']=eid
            row['machine_result']=copy.deepcopy(original)
            row.update(review_status='unreviewed',review_revision=0,reviewer='',review_reason='',reviewed_at=None)
            if eid in latest:
                _,revision,decision,changes,reason,reviewer,created=latest[eid]
                row.update(json.loads(changes))
                row.update(review_status=decision,review_revision=revision,reviewer=reviewer,
                    review_reason=reason,reviewed_at=created,needs_review=decision not in {'accepted','corrected'})
            rows.append(assess_baseline(row))
        return rows

    def review(self,owner,job_id,eid,decision,changes=None,reason='',reviewer='',expected_revision=None):
        if decision not in {'accepted','rejected','corrected','unreviewed'}:
            raise ValueError('无效复核状态。')
        if not isinstance(reviewer,str) or not reviewer.strip() or len(reviewer)>80:
            raise ValueError('请填写复核人姓名或代号（最多 80 字符）。')
        if not isinstance(reason,str) or len(reason)>2000 or (decision in {'rejected','corrected'} and not reason.strip()):
            raise ValueError('拒绝或修正证据需要填写理由（最多 2000 字符）。')
        changes=changes or {}
        if not isinstance(changes,dict) or set(changes)-REVIEWABLE:
            raise ValueError('只能修正关系、表达方向、物种、研究类型和统计信息；来源与原句不可改写。')
        if decision!='corrected' and changes:
            raise ValueError('只有修正状态可以修改字段。')
        for key,values in [('expression_direction',EXPRESSION_VALUES),('disease_effect',DISEASE_EFFECT_VALUES),('relation_type',RELATION_TYPE_VALUES),
            ('relation_scope',RELATION_SCOPE_VALUES),('population_context',POPULATION_VALUES),('measurement_type',MEASUREMENT_VALUES)]:
            if key in changes and (not isinstance(changes[key],str) or changes[key] not in values):
                raise ValueError('修正字段的取值不受支持。')
        for key,allowed in [('comparison_type',COMPARISON_VALUES),('significance',SIGNIFICANCE_VALUES)]:
            if key in changes and (not isinstance(changes[key],str) or changes[key] not in allowed):raise ValueError('比较类型或统计显著性取值不受支持。')
        for key in ['disease_group','normal_group','comparison_sentence']:
            if key in changes and (not isinstance(changes[key],str) or not changes[key].strip() or len(changes[key])>(9000 if key=='comparison_sentence' else 500)):
                raise ValueError('比较组名称或比较原句为空或过长。')
        for key in ['species','tissue_or_cell','evidence_type']:
            if key in changes and (not isinstance(changes[key],str) or not changes[key].strip() or len(changes[key])>120):
                raise ValueError('物种、组织或研究类型必须为 1–120 字符。')
        if 'statistical_evidence' in changes and not isinstance(changes['statistical_evidence'],dict):
            raise ValueError('统计信息必须是结构化对象。')
        serialized=json.dumps(changes,ensure_ascii=False,allow_nan=False)
        if len(serialized)>8000:
            raise ValueError('修正内容过长。')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            record=conn.execute('SELECT payload FROM jobs WHERE id=? AND owner_digest=?',
                (job_id,owner_digest(owner))).fetchone()
            if not record:
                raise PermissionError('无法访问该任务。')
            payload=json.loads(record[0])
            if payload['state'] not in TERMINAL:
                raise ValueError('请等待任务结束后再复核。')
            original=next((r for r in payload['rows'] if (r.get('evidence_id') or evidence_id(r))==eid),None)
            if original is None:
                raise ValueError('证据不存在。')
            if 'comparison_sentence' in changes and quote_segment(changes['comparison_sentence'],{'context':original.get('source_context',''),'context_segments':original.get('context_segments',[])}) is None:
                raise ValueError('比较原句必须来自保留的同一原文片段，不能改写或拼接。')
            if decision=='corrected' and (not changes or all(original.get(k)==v for k,v in changes.items())):
                raise ValueError('请至少修正一个与机器原值不同的字段。')
            previous=conn.execute('SELECT MAX(revision) FROM reviews WHERE job_id=? AND evidence_id=?',(job_id,eid)).fetchone()[0] or 0
            if expected_revision is not None and previous!=expected_revision:
                raise ValueError('该证据已在其他页面更新，请刷新后再保存。')
            revision=previous+1
            conn.execute('INSERT INTO reviews(job_id,evidence_id,revision,decision,changes,reason,reviewer,created) VALUES(?,?,?,?,?,?,?,?)',
                (job_id,eid,revision,decision,serialized,reason.strip(),reviewer.strip(),time.time()))
        return revision

    def audit(self,owner,job_id):
        self.load(owner,job_id)
        with self.connect() as conn:
            conn.row_factory=sqlite3.Row
            records=conn.execute('SELECT evidence_id,revision,decision,changes,reason,reviewer,created FROM reviews WHERE job_id=? ORDER BY id',(job_id,)).fetchall()
        return [{**dict(r),'changes':json.loads(r['changes'])} for r in records]

    def delete(self,owner,job_id):
        payload=self.load(owner,job_id)
        if payload['state'] not in TERMINAL:
            raise ValueError('运行中的任务不能删除。')
        with self.connect() as conn:
            conn.execute('DELETE FROM jobs WHERE id=? AND owner_digest=?',(job_id,owner_digest(owner)))

    def recent_submission_times(self):
        with self.connect() as conn:
            records=conn.execute('SELECT owner_digest,created,payload FROM jobs WHERE created>? ORDER BY created',(time.time()-3600,)).fetchall()
        return [(digest,created,json.loads(payload)['query']['source'],json.loads(payload)['query'].get('action','extract')) for digest,created,payload in records]
