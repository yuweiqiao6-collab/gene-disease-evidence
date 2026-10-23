"""Single-process bounded workers. Ownership is checked for every result/download."""
from __future__ import annotations
import copy
import hmac
import json
import re
import shutil
import threading
import time
import sqlite3
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4
from config import PROJECT_ROOT
from history_store import HistoryStore, owner_digest, evidence_id
from instance_lock import InstanceLock
from urllib.parse import urlsplit
import hashlib
from prompts import PROMPT_VERSION, SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from prompts import prompt_settings
from baseline import PROFILE as BASELINE_PROFILE,assess as assess_baseline,bind_primary_source
from scan_state import ScanStore
from scan_sources import candidates_for_paper
from corpus_store import CorpusStore
from original_bridge import OriginalRetrieval
from exports import write_exports, export_bytes
from io_utils import write_json
from llm_client import LLMClient
from query_utils import contains_any_term, query_terms
from uploads import Upload, validate_uploads, save_and_parse
from validators import ground_result
from support_checks import CHECK_VERSION
from web_settings import WebSettings
import pubmed_gateway
import literature_search
import europepmc_search
import fulltext_gateway
from literature_store import LiteratureStore,from_local_chunks,paper_chunks,summary as paper_summary

EXAMPLES = PROJECT_ROOT / 'examples' / 'synthetic_evidence.json'
BASELINE_EXAMPLES = PROJECT_ROOT / 'examples' / 'baseline_evidence.json'
TERMINAL = {'completed','partial','failed','no_candidates','no_relations','interrupted','paused'}
ALIASES = {'IL6':['IL-6','interleukin 6'], 'ABCC8':['SUR1'], 'TP53':['p53']}


class TaskSourceError(ValueError):
    """A fixed, user-readable source error that contains no server details."""


@dataclass(frozen=True)
class Query:
    genes: tuple[str, ...]
    disease: str
    source: str = 'demo'
    top_k: int = 6
    gene_aliases: tuple[str, ...] = ()
    disease_aliases: tuple[str, ...] = ()
    consent: bool = False
    action: str = 'extract'
    document_ids: tuple[str,...] = ()
    max_papers: int = 40
    focused: bool = False
    include_local: bool = True
    refresh: bool = False
    evidence_scope: str = 'auto'
    include_pubmed: bool = False
    model_retries: int = 0
    skip_reference_like: bool = False
    task_profile: str = 'legacy'
    include_europepmc: bool = False
    scan_paper_budget: int = 20
    scan_request_budget: int = 40
    scan_fetch_fulltext: bool = False
    demo_scenario: str = ''
    original_search_id: str = ''
    bm25_top_n: int = 500
    encode_top_n: int = 500
    auto_lookup_aliases: bool = False
    per_paper_limit: int = 0


@dataclass
class Job:
    id: str
    owner: str
    query: Query
    directory: Path
    state: str = 'queued'
    progress: float = 0
    message: str = '正在排队'
    created: float = field(default_factory=time.time)
    finished: float | None = None
    rows: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    query_urls: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    documents: list[dict] = field(default_factory=list)


class JobManager:
    def __init__(self, settings: WebSettings):
        self.settings = settings
        self.original = OriginalRetrieval(settings)
        self.root = settings.runtime_dir / 'tasks'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.instance_lock = InstanceLock(settings.runtime_dir/'service.lock')
        self.history = HistoryStore(settings.runtime_dir/'history.sqlite')
        self.literature = LiteratureStore(settings.runtime_dir/'literature.sqlite')
        self.scans = ScanStore(settings.runtime_dir/'scans')
        self.scan_pauses = {}
        self.history.recover_interrupted()
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=settings.max_workers, thread_name_prefix='evidence')
        self.slots = threading.BoundedSemaphore(settings.max_workers + settings.max_queue)
        self.jobs: dict[str, Job] = {}
        self.submissions: dict[str, deque] = {}
        self.live_submissions = deque()
        self.global_submissions = deque()
        self.login_failures: dict[str, deque] = {}
        for digest,created,source,action in self.history.recent_submission_times():
            self.global_submissions.append(created)
            if source != 'demo' and action in {'extract','scan'}:
                self.live_submissions.append(created)
            if time.time()-created < 60:
                self.submissions.setdefault(digest,deque()).append(created)
        # Resuming is a new budgeted run even though it keeps the same job ID.
        for directory in self.scans.root.iterdir():
            if not re.fullmatch(r'[a-f0-9]{32}',directory.name):continue
            try:manifest=self.scans.manifest(directory.name)
            except (ValueError,OSError):continue
            digest=manifest.get('owner_digest')
            for run in manifest.get('runs',[])[1:]:
                created=run['started']
                if time.time()-created<3600:
                    self.global_submissions.append(created);self.live_submissions.append(created)
                    if digest and time.time()-created<60:self.submissions.setdefault(digest,deque()).append(created)
        self.global_submissions=deque(sorted(self.global_submissions));self.live_submissions=deque(sorted(self.live_submissions))
        self.submissions={key:deque(sorted(value)) for key,value in self.submissions.items()}
        self.cleanup()

    def authenticate(self, owner, password):
        with self.lock:
            now = time.time()
            attempts = self.login_failures.setdefault(owner, deque())
            while attempts and now-attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= 5:
                return False
            valid = self.settings.live_ready and hmac.compare_digest(password.encode('utf-8'), self.settings.access_password.encode('utf-8'))
            if not valid:
                attempts.append(now)
            return valid

    def _validate(self, query, uploads, authorized, owner,*,resume=False):
        if query.source not in {'demo','pubmed','upload','corpus','library','original'}:
            raise ValueError('不支持的数据来源。')
        if query.action not in {'extract','search','fulltext','scan'}:
            raise ValueError('不支持的操作。')
        if query.source=='pubmed' and not self.settings.enable_literature:
            raise PermissionError('管理员已关闭在线文献服务。')
        if query.action in {'search','fulltext'}:
            online=query.action=='fulltext' or query.source=='pubmed' or query.include_pubmed or query.include_europepmc
            if online and not self.settings.enable_literature: raise PermissionError('管理员已关闭在线文献服务。')
            if (query.action=='search' and query.source not in {'pubmed','corpus','original'}) or (query.action=='fulltext' and query.source!='library'):
                raise ValueError('操作与文献来源不匹配。')
            if len(query.genes)!=1: raise ValueError('独立文献操作每次使用一个基因，便于核对检索范围。')
            if not 1<=query.max_papers<=100: raise ValueError('每次最多检索 100 篇文献。')
        if query.include_pubmed and not (query.action=='search' and query.source in {'corpus','original'}):
            raise ValueError('PubMed补充仅适用于本地文献检索。')
        if query.include_europepmc and not (query.action=='search' and query.source in {'corpus','pubmed'}):raise ValueError('Europe PMC补充仅用于独立文献检索。')
        if query.source=='original':
            if len(query.gene_aliases)>10 or len(query.disease_aliases)>10:
                raise ValueError('混合检索每类最多10条别名。')
            if query.action not in {'search','extract'} or len(query.genes)!=1 or query.task_profile!=BASELINE_PROFILE:
                raise ValueError('混合检索入口仅用于单个基因与疾病的检索或表达证据抽取。')
            if not self.original.ready:raise ValueError('混合检索尚未配置本地模型环境与BM25关键词索引。')
            if not 1<=query.top_k<=15 or not query.top_k<=query.encode_top_n<=2000 or not 1<=query.bm25_top_n<=2000:
                raise ValueError('混合检索最多15组最终片段，粗筛和编码上限最多2000。')
            if type(query.auto_lookup_aliases) is not bool or type(query.per_paper_limit) is not int or not 0<=query.per_paper_limit<=15:
                raise ValueError('混合检索的别名或每篇数量限制设置无效。')
            if query.auto_lookup_aliases and not self.settings.enable_literature:
                raise PermissionError('在线文献关闭时不能查询自动别名。')
            if query.action=='extract':
                parent=self._get(owner,query.original_search_id)
                if (parent.query.source,parent.query.action,parent.state,parent.query.genes,parent.query.disease)!=(
                        'original','search','completed',query.genes,query.disease):
                    raise ValueError('需要同一研究空间已经完成且目标一致的混合检索精排结果。')
                if query.top_k!=parent.query.top_k:raise ValueError('最终片段数已改变，请重新精排。')
                trace=parent.provenance.get('retrieval_trace',{})
                if (query.gene_aliases,query.disease_aliases)!=(tuple(trace.get('gene_aliases',[])[:10]),tuple(trace.get('disease_aliases',[])[:10])):
                    raise ValueError('别名与已确认的精排结果不一致，请重新精排。')
            elif query.original_search_id:raise ValueError('新检索不能引用旧精排任务。')
        elif query.original_search_id:
            raise ValueError('精排任务编号仅用于混合检索入口。')
        if query.evidence_scope not in {'auto','abstract'}: raise ValueError('不支持的证据范围。')
        if type(query.model_retries) is not int or query.model_retries not in {0,1}:
            raise ValueError('模型失败重试次数只能为0或1。')
        if type(query.skip_reference_like) is not bool:raise ValueError('参考文献筛选选项需要明确开启或关闭。')
        if query.task_profile not in {'legacy',BASELINE_PROFILE}:raise ValueError('不支持的提取任务范围。')
        if not isinstance(query.demo_scenario,str):raise ValueError('教学场景名称必须为文字。')
        if query.demo_scenario:
            from showcase import scenario
            item=scenario(query.demo_scenario)
            if (query.source,query.action,query.task_profile,query.genes,query.disease)!=(
                    'demo','extract',BASELINE_PROFILE,(item['gene'],),item['disease']):
                raise ValueError('教学场景只能用于其固定目标的离线演示。')
        if query.task_profile==BASELINE_PROFILE and len(query.genes)!=1:raise ValueError('疾病与正常对照任务每次使用一个疾病和一个基因。')
        if query.source=='library':
            maximum=300 if query.action=='scan' else 5
            if not 1<=len(query.document_ids)<=maximum or len(query.document_ids)!=len(set(query.document_ids)):
                raise ValueError(f'每次选择1–{maximum}篇不同文献。')
            if not resume:
                for document_id in query.document_ids: self.literature.get(owner,document_id)
        elif query.document_ids:
            raise ValueError('文献选择仅适用于个人收藏。')
        if query.action=='scan':
            if query.source!='library' or query.task_profile!=BASELINE_PROFILE:raise ValueError('批量扫描仅支持个人候选中的疾病与正常对照任务。')
            if not 1<=query.top_k<=6 or query.model_retries!=0:raise ValueError('扫描每篇最多6组片段，不自动重试。')
            self._scan_budgets(query.scan_paper_budget,query.scan_request_budget)
            if type(query.scan_fetch_fulltext) is not bool:raise ValueError('需要明确是否获取开放正文。')
            if query.scan_fetch_fulltext and (not self.settings.enable_literature or query.evidence_scope!='auto'):
                raise ValueError('自动获取正文需要开启在线文献并选择包含正文的范围。')
        if query.action=='extract' and query.source=='library' and query.task_profile==BASELINE_PROFILE and len(query.document_ids)*query.top_k>self.settings.max_model_chunks:
            raise ValueError(f'每篇独立分配片段；篇数乘以每篇片段数不能超过{self.settings.max_model_chunks}组。')
        if not 1 <= len(query.genes) <= self.settings.max_genes:
            raise ValueError(f'每次查询需要 1–{self.settings.max_genes} 个基因。')
        if any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', g) for g in query.genes):
            raise ValueError('基因名仅支持字母、数字、点、短横线和下划线。')
        if not query.disease.strip() or len(query.disease) > 200:
            raise ValueError('请输入 1–200 字符的疾病名称。')
        if not 1 <= query.top_k <= (15 if query.source=='original' else self.settings.max_top_k) or len(query.genes)*query.top_k > self.settings.max_model_chunks:
            raise ValueError(f'每个基因最多 {self.settings.max_top_k} 条候选，整次任务最多 {self.settings.max_model_chunks} 条。')
        aliases = query.gene_aliases + query.disease_aliases
        if len(aliases) > 20 or any(not a.strip() or len(a)>100 for a in aliases):
            raise ValueError('别名总数最多 20 个，每个最多 100 字符。')
        if query.gene_aliases and len(query.genes) > 1:
            raise ValueError('自定义基因别名仅支持单基因查询。')
        if query.source != 'demo' and query.action in {'extract','scan'}:
            if not self.settings.live_ready or not authorized:
                raise PermissionError('真实抽取需要管理员启用，并通过访问口令验证。')
            if not query.consent:
                raise ValueError('请确认允许将候选文献文本发送到配置的模型服务。')
            from config import get_llm_settings
            credentials = get_llm_settings()
            if not credentials['api_key'] or not credentials['model']:
                raise ValueError('管理员尚未配置模型服务，当前可以使用示例体验。')
        if uploads and query.source != 'upload':
            raise ValueError('只有上传模式可以接收文件。')
        if query.source == 'upload' and not uploads:
            raise ValueError('请先上传论文。')
        validate_uploads(uploads, self.settings)
        if query.source == 'corpus' and (not self.settings.corpus_db or not self.settings.corpus_db.is_file()):
            raise ValueError('管理员尚未配置公共语料库。')

    def submit(self, owner: str, query: Query, uploads: tuple[Upload, ...] = (), *, authorized=False):
        if not isinstance(owner, str) or not re.fullmatch(r'[a-f0-9]{64}', owner):
            raise ValueError('无效会话。')
        self._validate(query, uploads, authorized, owner)
        with self.lock:
            self.cleanup()
            now = time.time()
            history = self.submissions.setdefault(owner_digest(owner), deque())
            for records, span in [(history, 60),(self.live_submissions,3600),(self.global_submissions,3600)]:
                while records and now-records[0] > span:
                    records.popleft()
            if len(history) >= self.settings.session_jobs_per_minute:
                raise ValueError('查询过于频繁，请一分钟后重试。')
            if len(self.global_submissions) >= 60:
                raise ValueError('演示服务本小时任务额度已用完，请稍后再试。')
            if query.source != 'demo' and query.action in {'extract','scan'} and len(self.live_submissions) >= self.settings.global_live_jobs_per_hour:
                raise ValueError('真实抽取本小时任务额度已用完。')
            if any(j.owner == owner and j.state not in TERMINAL for j in self.jobs.values()):
                raise ValueError('请等待当前任务完成后再提交。')
            if not self.slots.acquire(blocking=False):
                raise ValueError('服务繁忙，队列已满，请稍后重试。')
            job_id = uuid4().hex
            directory = self.root/job_id
            try:
                directory.mkdir(mode=0o700)
                self.jobs[job_id] = Job(job_id, owner, query, directory)
                self.history.save(self.jobs[job_id])
                if query.action=='scan':self.scan_pauses[job_id]=threading.Event()
                history.append(now)
                self.global_submissions.append(now)
                if query.source != 'demo' and query.action in {'extract','scan'}:
                    self.live_submissions.append(now)
                self.executor.submit(self._run, job_id, uploads)
            except Exception:
                self.jobs.pop(job_id, None)
                self.slots.release()
                raise
            return job_id

    def _get(self, owner, job_id):
        owner_digest(owner)
        if not isinstance(job_id,str) or not re.fullmatch('[a-f0-9]{32}',job_id):
            raise PermissionError('无法访问该任务。')
        job = self.jobs.get(job_id)
        if job is None:
            saved = self.history.load(owner,job_id)
            query_data = saved.pop('query')
            for key in ['genes','gene_aliases','disease_aliases','document_ids']:
                query_data[key] = tuple(query_data.get(key,()))
            saved.pop('synthetic',None)
            job = Job(owner=owner,query=Query(**query_data),directory=self.root/job_id,**saved)
            self.jobs[job_id] = job
            if job.query.action=='scan' and job.state=='interrupted':
                from scan_runner import refresh
                if (self.scans.directory(job_id)/'manifest.json').exists():
                    self.scans.recover(job_id);refresh(self,job)
        if not hmac.compare_digest(job.owner, owner):
            raise PermissionError('无法访问该任务。')
        return job

    def list_history(self, owner):
        with self.lock:
            return [{key:record[key] for key in ['id','created','state','query','synthetic','counts']}
                    for record in self.history.list(owner)]

    def snapshot(self, owner, job_id):
        with self.lock:
            job = self._get(owner, job_id)
            rows = self.history.decorated_rows(owner,job_id) if job.state in TERMINAL else job.rows
            return copy.deepcopy({'id':job.id, 'state':job.state, 'progress':job.progress,
                'message':job.message, 'rows':rows, 'counts':job.counts,
                'issues':job.issues, 'query_urls':job.query_urls, 'synthetic':job.query.source=='demo',
                'genes':job.query.genes, 'disease':job.query.disease, 'created':job.created,
                'provenance':job.provenance,'action':job.query.action,'documents':job.documents})

    def download(self, owner, job_id, filename, *, confirmed_only=False,baseline_only=False):
        with self.lock:
            job = self._get(owner, job_id)
            if job.state not in TERMINAL or filename not in {'evidence.json','evidence.csv','evidence.xlsx','status.json','reviews.json','literature.json','coverage.json'}:
                raise PermissionError('下载文件不可用。')
            if filename=='coverage.json':
                if job.query.action!='scan':raise PermissionError('本任务没有扫描覆盖记录。')
                return json.dumps({'documents':job.documents,'scan':job.provenance.get('scan',{}),
                    'state':job.state,'model_usage':job.provenance.get('model_usage',{})},ensure_ascii=False,indent=2).encode()
            if filename=='literature.json':
                return json.dumps({'documents':job.documents,'provenance':job.provenance,'state':job.state,'issues':job.issues},ensure_ascii=False,indent=2).encode()
            if filename=='status.json':
                return json.dumps({'state':job.state,'complete':job.state in {'completed','no_candidates','no_relations'},
                    'synthetic':job.query.source=='demo','counts':job.counts,'issues':job.issues,
                    'query_urls':job.query_urls,'provenance':job.provenance},ensure_ascii=False,indent=2).encode()
            if filename=='reviews.json':
                return json.dumps(self.history.audit(owner,job_id),ensure_ascii=False,indent=2).encode()
            rows = self.history.decorated_rows(owner,job_id)
            if confirmed_only:
                rows = [r for r in rows if r['review_status'] in {'accepted','corrected'}]
            if baseline_only:rows=[r for r in rows if r.get('baseline_status')=='eligible' and r.get('review_status')!='rejected']
            return export_bytes(rows,filename.split('.')[-1])

    def review(self, owner, job_id, eid, decision, **kwargs):
        with self.lock:
            self._get(owner,job_id)
            return self.history.review(owner,job_id,eid,decision,**kwargs)

    def review_audit(self, owner, job_id):
        with self.lock:
            self._get(owner,job_id)
            return self.history.audit(owner,job_id)

    @staticmethod
    def _scan_budgets(papers,requests):
        if type(papers) is not int or not 1<=papers<=50 or type(requests) is not int or not 1<=requests<=120:
            raise ValueError('每轮扫描上限为1–50篇、1–120次请求名额。')

    def pause_scan(self,owner,job_id):
        with self.lock:
            job=self._get(owner,job_id)
            if job.query.action!='scan' or job.state in TERMINAL:raise ValueError('扫描当前不在运行。')
            self.scan_pauses[job_id].set()
            self._update(job,message='已请求暂停，当前调用结束后保存进度；不会强制取消已发出的请求。')

    def resume_scan(self,owner,job_id,*,paper_budget=20,request_budget=40,consent=False,authorized=False,retry_unresolved=False,evidence_scope=None,fetch_fulltext=None):
        from scan_runner import signature,new_record
        with self.lock:
            job=self._get(owner,job_id)
            if job.query.action!='scan' or job.state not in TERMINAL:raise ValueError('请等待扫描暂停或结束后再继续。')
            if type(retry_unresolved) is not bool:raise ValueError('需要明确是否重试未解决项。')
            query=replace(job.query,scan_paper_budget=paper_budget,scan_request_budget=request_budget,consent=consent,
                evidence_scope=job.query.evidence_scope if evidence_scope is None else evidence_scope,
                scan_fetch_fulltext=job.query.scan_fetch_fulltext if fetch_fulltext is None else fetch_fulltext)
            self._validate(query,(),authorized,owner,resume=True)
            manifest=self.scans.manifest(job_id)
            if manifest['configuration_signature']!=signature():raise ValueError('模型设置或任务规则已变更，请新建扫描。')
            self.scans.recover(job_id)
            records=self.scans.records(job_id)
            retry_states={'failed','interrupted','needs_fulltext','needs_context','needs_review','no_text','no_candidates'}
            if not any(d['status']=='pending' or (retry_unresolved and d['status'] in retry_states) for d in records):
                raise ValueError('没有未处理候选；需重试失败或摘要不足项时请明确勾选，否则应补充检索或人工核对。')
            now=time.time();digest=owner_digest(owner)
            history=self.submissions.setdefault(digest,deque())
            for values,span in [(history,60),(self.live_submissions,3600),(self.global_submissions,3600)]:
                while values and now-values[0]>span:values.popleft()
            if len(history)>=self.settings.session_jobs_per_minute or len(self.live_submissions)>=self.settings.global_live_jobs_per_hour or len(self.global_submissions)>=60:
                raise ValueError('继续扫描已达到任务频率上限，请稍后重试。')
            if any(j.owner==owner and j.state not in TERMINAL for j in self.jobs.values()):raise ValueError('请等待当前任务完成后再继续。')
            if not self.slots.acquire(blocking=False):raise ValueError('服务繁忙，队列已满。')
            try:
                scope_changed=(query.evidence_scope,query.scan_fetch_fulltext)!=(job.query.evidence_scope,job.query.scan_fetch_fulltext)
                if retry_unresolved:
                    for record in records:
                        if record['status'] not in retry_states:continue
                        for c in record['candidates']:
                            if c['state'] in {'failed','interrupted'}:c['state']='pending'
                        # Already successful chunks stay successful. New text can
                        # supplement an abstract only after explicit resume consent.
                        if record['status'] in {'needs_fulltext','needs_context','needs_review','no_text','no_candidates'}:
                            try:
                                paper=self.literature.get(owner,record['document_id'])
                                if paper['paper_id']!=record['paper_id']:raise ValueError('Identity changed')
                                record['source_revision']=new_record(paper)['source_revision'];record['prepared']=False
                            except (PermissionError,ValueError):
                                record['status']='source_changed';self.scans.save(job_id,record);continue
                        record['status']='pending';self.scans.save(job_id,record)
                if scope_changed:
                    for record in self.scans.records(job_id):
                        if record['status']!='pending':continue
                        try:
                            paper=self.literature.get(owner,record['document_id'])
                            if paper['paper_id']!=record['paper_id']:raise ValueError('Identity changed')
                            record['source_revision']=new_record(paper)['source_revision'];record['prepared']=False
                        except (PermissionError,ValueError):record['status']='source_changed'
                        self.scans.save(job_id,record)
                job.query=query;job.finished=None
                self.scan_pauses[job_id]=threading.Event()
                self._update(job,state='queued',message='已确认新一轮上限，等待继续扫描。')
                history.append(now);self.live_submissions.append(now);self.global_submissions.append(now)
                self.executor.submit(self._run,job_id,())
            except Exception:
                self.slots.release();raise
            return job_id

    def delete_history(self, owner, job_id):
        with self.lock:
            job=self._get(owner,job_id)
            self.history.delete(owner,job_id)
            if job.query.action=='scan':self.scans.delete(job_id)
            self.scan_pauses.pop(job_id,None)
            if job.directory.is_dir() and not job.directory.is_symlink():
                shutil.rmtree(job.directory)
            self.jobs.pop(job_id,None)

    def _update(self, job, **changes):
        with self.lock:
            for key, value in changes.items():
                setattr(job, key, value)
            self.history.save(job)

    def _run_original_search(self,job):
        q=job.query
        self._update(job,message='混合检索：合并本地关键词与PubMed候选，读取或补算向量，随后重排序。')
        result=self.original.retrieve(dict(gene=q.genes[0],disease=q.disease,
            gene_aliases=list(q.gene_aliases),disease_aliases=list(q.disease_aliases),
            auto_aliases=q.auto_lookup_aliases,include_pubmed=q.include_pubmed,max_papers=q.max_papers,
            top_k=q.top_k,bm25_top_n=q.bm25_top_n,encode_top_n=q.encode_top_n,per_paper_limit=q.per_paper_limit))
        rows=[]
        for c in result['candidates']:
            if c.get('synthetic') or c.get('source_type')=='synthetic_demo':raise ValueError('教学材料不能进入真实精排。')
            c=copy.deepcopy(c)
            c['source_type']='local_pdf' if c.get('source_type')=='pdf' else c.get('source_type','local_pdf')
            c.setdefault('content_level','abstract' if 'abstract' in c['source_type'] else 'fulltext')
            rows.append(c)
        trace=result['trace']
        degraded=trace['actual_method']!='bm25+dense+cross_encoder' and len(rows)>1
        online_failed=trace.get('pubmed_status') not in {'disabled','success','cache_hit','no_results','no_abstracts'}
        state='partial' if degraded or online_failed else 'completed' if rows else 'no_candidates'
        self._update(job,state=state,progress=1,documents=rows,
            provenance={'engine':'original','retrieval_trace':trace,'model_calls':0},
            counts={'candidates':len(rows),'documents':len({c['paper_id'] for c in rows}),'model_calls':0},
            message='混合检索精排完成；尚未调用LLM。' if state=='completed' else '混合检索未完整成功或候选为空，请核对实际方法与来源状态。',finished=time.time())

    def _run_literature(self,job):
        query=job.query
        provenance={'application_version':(PROJECT_ROOT/'VERSION').read_text().strip(),
            'operation':query.action,'model_calls':0,'model_used':False}
        issues=[];documents=[];urls=[]
        if query.action=='search':
            gene=query.genes[0]
            ga=list(query.gene_aliases) or ALIASES.get(gene.upper(),[])
            use_local=query.source=='corpus' or query.include_local
            use_online=query.source=='pubmed' or query.include_pubmed
            if use_local and self.settings.corpus_db and self.settings.corpus_db.is_file():
                try:
                    store=CorpusStore(self.settings.corpus_db)
                    local=store.search_papers(gene,query.disease,query.max_papers,ga,list(query.disease_aliases),skip_reference_like=query.skip_reference_like)
                    for paper in local:
                        documents.append(self.literature.upsert(job.owner,paper))
                    provenance['local_snapshot']=store.fingerprint()
                    provenance['local_documents']=len(local)
                except (ValueError,RuntimeError,OSError,sqlite3.Error):
                    issues.append('本地语料检索失败，未视为无文献。')
            if use_online:
                try:
                    result=literature_search.search(gene,query.disease,self.settings.runtime_dir/'literature-cache',
                        max_results=query.max_papers,gene_aliases=ga,disease_aliases=list(query.disease_aliases),
                        focused=query.focused,refresh=query.refresh)
                    provenance['pubmed']={k:v for k,v in result.items() if k!='papers'}
                    urls=[result['query_url']]
                    issues.extend(result['issues'])
                    for paper in result['papers']:
                        try: documents.append(self.literature.upsert(job.owner,paper))
                        except ValueError: issues.append('一篇文献的身份冲突或候选记录已达上限，未自动合并或保存。')
                    if result['truncated_by_limit']:
                        provenance['coverage_note']='达到本次篇数上限；不是完整检索结果。'
                except pubmed_gateway.PubMedError as exc:
                    issues.append(str(exc))
                    provenance['pubmed']={'status':'failed','complete':False}
            if query.include_europepmc:
                try:
                    result=europepmc_search.search(gene,query.disease,self.settings.runtime_dir/'europepmc-search-cache',
                        max_results=query.max_papers,gene_aliases=ga,disease_aliases=list(query.disease_aliases),focused=query.focused,refresh=query.refresh)
                    provenance['europepmc']={k:v for k,v in result.items() if k!='papers'};urls.append(result['query_url'])
                    for paper in result['papers']:
                        try:documents.append(self.literature.upsert(job.owner,paper))
                        except ValueError:issues.append('Europe PMC一篇文献的编号冲突或记录已达上限，未自动保存。')
                except pubmed_gateway.PubMedError as exc:
                    issues.append(str(exc));provenance['europepmc']={'status':'failed','complete':False}
            # Last observation includes metadata merged from both sources.
            documents=list({d['document_id']:d for d in documents}.values())
        else:
            for i,document_id in enumerate(query.document_ids):
                self._update(job,message=f'核对并获取开放全文 {i+1}/{len(query.document_ids)}',progress=.1+.8*i/len(query.document_ids))
                paper=self.literature.get(job.owner,document_id)
                if paper.get('fulltext_status')=='available' and not query.refresh:
                    result={'fulltext_status':'available'}
                else:
                    try: result=fulltext_gateway.fetch(paper,self.settings.runtime_dir/'fulltext-cache',refresh=query.refresh)
                    except fulltext_gateway.FulltextError as exc:
                        result={'fulltext_status':'failed','fulltext_message':str(exc)}
                    self.literature.update_fulltext(job.owner,document_id,result)
                if result['fulltext_status']=='failed': issues.append(paper['paper_id']+'：'+result['fulltext_message'])
                documents.append(paper_summary(self.literature.get(job.owner,document_id)))
        state=('partial' if documents else 'failed') if issues else ('completed' if documents else 'no_candidates')
        counts={'documents':len(documents),'abstracts':sum(bool(d.get('abstract')) for d in documents),
            'fulltexts':sum(d.get('fulltext_status')=='available' for d in documents),'model_calls':0}
        message='文献操作完成；尚未调用模型或判断基因—疾病关系。' if state=='completed' else (
            '本次没有找到文献；不代表医学上没有关系。' if state=='no_candidates' else '文献操作未完整成功，请查看说明。')
        self._update(job,state=state,progress=1,message=message,documents=documents,
            provenance=provenance,counts=counts,issues=issues,query_urls=urls,finished=time.time())

    def _run(self, job_id, uploads):
        job = self.jobs[job_id]
        rows, candidates_count, rejected, failures, issues, urls = [], 0, 0, 0, [], []
        try:
            self._update(job, state='running', message='准备文献', progress=.05)
            query = job.query
            if query.action=='scan':
                from scan_runner import run
                run(self,job)
                return
            if query.action!='extract':
                if query.source=='original':
                    self._run_original_search(job)
                    return
                self._run_literature(job)
                return
            store = None
            library_candidates = None
            selected = []
            sent_candidates = []
            demo_rows = []
            if query.source == 'demo':
                if query.demo_scenario:
                    from showcase import scenario
                    demo_rows=scenario(query.demo_scenario)['documents']
                else:
                    demo_rows = json.loads((BASELINE_EXAMPLES if query.task_profile==BASELINE_PROFILE else EXAMPLES).read_text(encoding='utf-8'))
                CorpusStore.build(job.directory/'corpus.sqlite', demo_rows)
                store = CorpusStore(job.directory/'corpus.sqlite')
            elif query.source == 'original':
                parent=self._get(job.owner,query.original_search_id)
                if parent.state!='completed':raise TaskSourceError('精排结果未完整完成。')
                library_candidates=copy.deepcopy(parent.documents)
                if not library_candidates:raise TaskSourceError('没有可抽取的精排片段。')
            elif query.source == 'upload':
                chunks = save_and_parse(uploads, job.directory/'uploads', self.settings)
                CorpusStore.build(job.directory/'corpus.sqlite', chunks)
                store = CorpusStore(job.directory/'corpus.sqlite')
            elif query.source == 'corpus':
                store = CorpusStore(self.settings.corpus_db)
            elif query.source == 'library':
                selected=[self.literature.get(job.owner,d) for d in query.document_ids]
                if query.task_profile==BASELINE_PROFILE:
                    library_candidates=[]
                    for paper in selected:
                        temporary=job.directory/paper['document_id'];temporary.mkdir(mode=0o700)
                        library_candidates+=candidates_for_paper(paper,query,self.settings,temporary,
                            gene_aliases=list(query.gene_aliases) or ALIASES.get(query.genes[0].upper(),[]))
                chunks=library_candidates if library_candidates is not None else [c for paper in selected for c in paper_chunks(paper,query.evidence_scope)]
                local_papers=[p for p in selected if p.get('local_corpus_paper_ids')] if query.evidence_scope=='auto' and library_candidates is None else []
                if local_papers:
                    if not self.settings.corpus_db or not self.settings.corpus_db.is_file():
                        raise TaskSourceError('选中文献来自本地全文库，但该库当前不可用，请重新配置后检索。')
                    local_store=CorpusStore(self.settings.corpus_db)
                    snapshot=local_store.fingerprint().get('snapshot_sha256')
                    if any(p.get('local_snapshot_sha256')!=snapshot for p in local_papers):
                        raise TaskSourceError('本地全文库已更换，旧候选不能直接用于抽取，请重新检索。')
                    ids=list(dict.fromkeys(pid for p in local_papers for pid in p['local_corpus_paper_ids']))
                    chunks=[c for c in chunks if c.get('source_type')!='local_pdf']
                    for gene in query.genes:
                        local_hits=local_store.search(gene,query.disease,query.top_k,
                            list(query.gene_aliases) or ALIASES.get(gene.upper(),[]),list(query.disease_aliases),paper_ids=ids,skip_reference_like=query.skip_reference_like)
                        chunks+=[local_store.with_neighbors(c,skip_reference_like=query.skip_reference_like) for c in local_hits] if query.task_profile==BASELINE_PROFILE else local_hits
                    chunks=list({c['chunk_id']:c for c in chunks}.values())
                    if local_store.fingerprint().get('snapshot_sha256')!=snapshot:
                        raise TaskSourceError('检索期间本地全文库发生变化，请重新检索后再抽取。')
                if not chunks: raise TaskSourceError('所选文献没有当前范围内可读取的证据文本。')
                CorpusStore.build(job.directory/'corpus.sqlite',chunks)
                store=CorpusStore(job.directory/'corpus.sqlite')
            client = None if query.source=='demo' else LLMClient(cache_path=job.directory/'model_cache.sqlite', allow_fallback=False,task_profile=query.task_profile)
            prompt_version,system_prompt,user_template=prompt_settings(query.task_profile)
            provenance = {
                'application_version': (PROJECT_ROOT/'VERSION').read_text().strip(),
                'demo_scenario':query.demo_scenario,
                'model_name': getattr(client,'model',None) if client else 'synthetic_annotations',
                'provider_host': urlsplit(str(getattr(client,'base_url',''))).hostname if client else None,
                'prompt_version':prompt_version,'task_profile':query.task_profile,
                'support_check_version': CHECK_VERSION,
                'prompt_sha256': hashlib.sha256((system_prompt+user_template).encode()).hexdigest(),
                'retrieval_method': 'original_candidate_hybrid_rerank' if query.source=='original' else 'bm25_pubmed' if query.source=='pubmed' else 'sqlite_fts5',
                'corpus_snapshot': store.fingerprint() if store else None,
                'candidate_snapshots': [],
                'selected_documents':list(query.document_ids),'evidence_scope':query.evidence_scope,
                'input_character_limit': 9000 if query.task_profile==BASELINE_PROFILE else 12000, 'max_output_tokens': 2048,
                'max_attempts_per_chunk': 1+query.model_retries,
                'request_limit':(len(selected) if library_candidates is not None else len(query.genes))*query.top_k*(1+query.model_retries) if client else 0,
                'skip_reference_like':query.skip_reference_like,
            }
            if query.source=='original':
                provenance['retrieval_trace']=copy.deepcopy(parent.provenance['retrieval_trace'])
                provenance['request_limit']=len(library_candidates)*(1+query.model_retries)
                provenance['original_search_id']=parent.id
            self._update(job,provenance=provenance)
            for index, gene in enumerate(query.genes):
                ga = list(query.gene_aliases) or ALIASES.get(gene.upper(), [])
                da = list(query.disease_aliases)
                self._update(job, progress=.1+.75*index/len(query.genes), message=f'检索 {gene} 的证据')
                if library_candidates is not None:
                    candidates=library_candidates
                elif query.source == 'pubmed':
                    try:
                        fetched = pubmed_gateway.fetch(gene, query.disease, self.settings.runtime_dir/'pubmed',
                            gene_aliases=ga, disease_aliases=da)
                    except pubmed_gateway.PubMedError as exc:
                        issues.append(f'{gene}：{exc}')
                        failures += 1
                        continue
                    urls.append(fetched.query_url)
                    if fetched.status == 'partial_fetch':
                        issues.append(f'{gene}：PubMed 只返回了部分文献，本次检索不完整。')
                    if fetched.status == 'no_abstracts':
                        issues.append(f'{gene}：匹配文献没有可读取的摘要。')
                    chunks = [c for c in fetched.chunks if contains_any_term(c['context'], query_terms(gene, ga))]
                    if fetched.chunks and not chunks:
                        issues.append(f'{gene}：摘要未明确提及目标基因，无法确认实体。')
                    from retrieval import build_index, retrieve_candidates
                    candidates = retrieve_candidates(build_index(chunks), gene, query.disease, query.top_k,
                        gene_aliases=ga, disease_aliases=da, require_gene=True)
                else:
                    candidates = store.search(gene, query.disease, query.top_k, ga, da,skip_reference_like=query.skip_reference_like)
                if query.source != 'demo' and any(c.get('synthetic') or c.get('source_type')=='synthetic_demo' for c in candidates):
                    raise ValueError('人工教学案例不能进入真实抽取，请检查语料来源。')
                candidates_count += len(candidates)
                # Bound input size as well as number of paid calls.
                input_limit=9000 if query.task_profile==BASELINE_PROFILE else 12000
                candidates = [{**c, 'context':c['context'][:input_limit]} for c in candidates]
                if query.task_profile==BASELINE_PROFILE and store:
                    candidates=[store.with_neighbors(c,skip_reference_like=query.skip_reference_like) for c in candidates]
                sent_candidates.extend(candidates)
                provenance['candidate_snapshots'].append({'gene':gene,
                    'count':len(candidates),'sha256':hashlib.sha256(json.dumps(candidates,sort_keys=True,ensure_ascii=False).encode()).hexdigest()})
                write_json(job.directory/f'candidates_{index}.json',candidates)
                self._update(job, message=f'核对 {gene} 的原文与关系',provenance=provenance)
                if query.source=='demo':
                    extracted = [(c,[c['annotation']] if c.get('annotation') else []) for c in candidates]
                else:
                    extracted = [(c,[r.to_dict() for r in results]) for c,results in
                        client.extract_batch(gene, query.disease, candidates, max_workers=2, retry=query.model_retries)]
                for chunk, annotations in extracted:
                    for raw in annotations:
                        bound=bind_primary_source(raw,chunk) if query.task_profile==BASELINE_PROFILE else chunk
                        row = ground_result(raw,bound,gene,query.disease,ga,da) if bound else None
                        if row is None:
                            rejected += 1
                            continue
                        row['synthetic'] = query.source=='demo'
                        row['source_context'] = chunk['context']
                        if query.task_profile==BASELINE_PROFILE:
                            row.update(task_profile=BASELINE_PROFILE,context_segments=chunk.get('context_segments',[]),
                                target_gene_aliases=ga,target_disease_aliases=da)
                            row=assess_baseline(row)
                        if row['synthetic']:
                            row['notes'] = '人工编写的教学案例，不是真实论文；' + row['notes']
                        rows.append(row)
            failures += client.failure_count if client else 0
            if failures:
                issues.append('部分检索或模型请求失败；未以规则结果替代。')
            if rejected:
                issues.append(f'{rejected} 条输出的引用无法与原文匹配，已剔除。')
            # Deduplicate overlapping snippets while preserving separate endpoints.
            unique = {}
            for row in rows:
                key = tuple(row[k] for k in ['paper_id','gene_or_target','disease','evidence_sentence','disease_effect',
                    'expression_direction','measurement_type','population_context'])
                if query.task_profile==BASELINE_PROFILE:key+=tuple(row.get(k,'') for k in ['comparison_type','comparison_sentence','disease_group','normal_group','significance','species','tissue_or_cell'])
                unique.setdefault(key, row)
            rows = list(unique.values())
            for row in rows:
                row['evidence_id'] = evidence_id(row)
            provenance['model_usage'] = getattr(client,'usage',{}) if client else {}
            provenance['model_failures'] = getattr(client,'failures',[]) if client else []
            provenance['response_models'] = sorted(getattr(client,'response_models',[])) if client else []
            if selected:
                coverage=[]
                for paper in selected:
                    paper_ids={paper['paper_id'],*paper.get('local_corpus_paper_ids',[])}
                    coverage.append({'document_id':paper['document_id'],'title':paper['title'],
                        'candidate_count':sum(c.get('paper_id') in paper_ids for c in sent_candidates)})
                provenance['selected_document_coverage']=coverage
            state = ('partial' if rows else 'failed') if (failures or rejected or any('不完整' in i for i in issues)) else (
                'completed' if rows else ('no_candidates' if not candidates_count else 'no_relations'))
            messages = {'completed':'处理完成，证据仍需人工复核','partial':'部分完成，请查看说明',
                'failed':'本次处理未完整成功，请查看说明','no_candidates':'没有找到可抽取的文献片段',
                'no_relations':'候选文献中未抽取到符合要求的表达关系'}
            counts = {'candidates':candidates_count,'evidence':len(rows),'rejected_quotes':rejected,
                      'failed_requests':failures,'model_calls':getattr(client,'request_count',0),
                      'support_flagged':sum(r.get('support_status')=='flagged' for r in rows)}
            if query.task_profile==BASELINE_PROFILE:
                counts.update(baseline_eligible=sum(r['baseline_status']=='eligible' for r in rows),
                    baseline_pending=sum(r['baseline_status']=='needs_scope_review' for r in rows),
                    baseline_out_of_scope=sum(r['baseline_status']=='out_of_scope' for r in rows))
            write_exports(job.directory, rows)
            write_json(job.directory/'status.json', {'state':state,'complete':state in {'completed','no_candidates','no_relations'},
                'synthetic':query.source=='demo','counts':counts,'issues':issues,'query_urls':urls})
            self._update(job, state=state, progress=1, message=messages[state], rows=rows,
                counts=counts, issues=issues, query_urls=urls, provenance=provenance, finished=time.time())
        except Exception as exc:
            # Never echo exception text containing paths, provider responses or credentials.
            message = str(exc) if isinstance(exc,TaskSourceError) or (isinstance(exc, ValueError) and job.query.source=='upload') else '处理失败，请检查服务配置或稍后重试。'
            issues = [message]
            write_json(job.directory/'status.json', {'state':'failed','complete':False,'issues':issues})
            self._update(job, state='failed', progress=1, message=message, issues=issues, finished=time.time())
        finally:
            self.slots.release()

    def cleanup(self):
        with self.lock:
            now = time.time()
            expired = [j for j in self.jobs.values() if j.finished and now-j.finished > self.settings.retention_hours*3600]
            completed = sorted((j for j in self.jobs.values() if j.finished and j not in expired), key=lambda j:j.finished)
            expired += completed[:max(0,len(self.jobs)-len(expired)-200)]
            for job in expired:
                if job.directory.parent == self.root and job.directory.is_dir() and not job.directory.is_symlink():
                    shutil.rmtree(job.directory)
                self.jobs.pop(job.id,None)
            # Purge stale task directories after process restarts. IDs alone confer no access.
            for directory in self.root.iterdir():
                if (re.fullmatch(r'[a-f0-9]{32}',directory.name) and directory.name not in self.jobs
                    and directory.is_dir() and not directory.is_symlink()
                    and now-directory.stat().st_mtime > self.settings.retention_hours*3600):
                    shutil.rmtree(directory)
            for histories in [self.submissions,self.login_failures]:
                for owner in list(histories):
                    history = histories[owner]
                    while history and now-history[0]>60:
                        history.popleft()
                    if not history:
                        del histories[owner]

    def close(self):
        self.executor.shutdown(wait=True)
        self.original.close()
        self.instance_lock.close()
