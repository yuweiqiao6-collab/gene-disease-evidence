"""SQLite FTS5 corpus with streaming builds and read-only, bounded queries."""
from __future__ import annotations
import json
import os
import sqlite3
import tempfile
import hashlib
from itertools import groupby
from pathlib import Path
from typing import Iterable, Any
from query_utils import query_terms, contains_any_term
from source_quality import annotate_source


class CorpusStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def build(cls, path: Path, records: Iterable[dict[str, Any]], *, metadata=None, progress=None) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.corpus-', suffix='.sqlite', dir=path.parent)
        os.close(fd)
        count = 0
        digest = hashlib.sha256()
        try:
            with sqlite3.connect(temporary) as conn:
                conn.execute('PRAGMA cache_size=-32768')
                conn.execute('PRAGMA threads=1')
                conn.execute('CREATE TABLE chunks (id INTEGER PRIMARY KEY, chunk_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, paper_id TEXT NOT NULL)')
                conn.execute('CREATE INDEX chunks_paper ON chunks(paper_id)')
                conn.execute("CREATE VIRTUAL TABLE text_index USING fts5(title, context)")
                conn.execute('CREATE TABLE papers (id INTEGER PRIMARY KEY, paper_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)')
                conn.execute('CREATE VIRTUAL TABLE paper_index USING fts5(title, body)')
                conn.execute('CREATE TABLE corpus_info (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                for record in records:
                    if not isinstance(record, dict) or not record.get('chunk_id') or not isinstance(record.get('context'), str):
                        raise ValueError('Each corpus row needs chunk_id and text context')
                    record=dict(record)
                    record['paper_id']=str(record.get('paper_id') or record['chunk_id'])
                    payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
                    digest.update((payload+'\n').encode())
                    cursor = conn.execute('INSERT INTO chunks(chunk_id,payload,paper_id) VALUES (?,?,?)',
                        (record['chunk_id'], payload,record['paper_id']))
                    conn.execute('INSERT INTO text_index(rowid,title,context) VALUES (?,?,?)',
                        (cursor.lastrowid, str(record.get('title', '')), record['context']))
                    count += 1
                    if count % 5000 == 0:
                        conn.commit()
                        if progress: progress(count)
                paper_count=0
                source=conn.execute('SELECT paper_id,payload FROM chunks ORDER BY paper_id,id')
                for pid,group in groupby(source,key=lambda row:row[0]):
                    body=[];first=None
                    for _,payload in group:
                        item=json.loads(payload)
                        if first is None:first=item
                        body.append(item['context'])
                    info={k:first[k] for k in ('paper_id','title','file_name','source_type','content_level','pmid','pmcid','doi') if k in first}
                    cursor=conn.execute('INSERT INTO papers(paper_id,payload) VALUES(?,?)',(pid,json.dumps(info,ensure_ascii=False)))
                    conn.execute('INSERT INTO paper_index(rowid,title,body) VALUES(?,?,?)',(cursor.lastrowid,str(first.get('title','')),'\n'.join(body)))
                    paper_count+=1
                conn.execute("INSERT INTO text_index(text_index) VALUES('optimize')")
                conn.execute("INSERT INTO paper_index(paper_index) VALUES('optimize')")
                conn.executemany('INSERT INTO corpus_info VALUES(?,?)',
                    [('snapshot_sha256',digest.hexdigest()),('chunk_count',str(count)),('paper_count',str(paper_count)),('schema_version','2')])
                if metadata:
                    conn.executemany('INSERT INTO corpus_info VALUES(?,?)',[(k,str(v)) for k,v in metadata.items()])
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return count

    def fingerprint(self):
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as conn:
            try:
                return dict(conn.execute('SELECT key,value FROM corpus_info'))
            except sqlite3.OperationalError:
                stat = self.path.stat()
                return {'legacy_file_size':stat.st_size,'legacy_file_mtime_ns':stat.st_mtime_ns}

    @staticmethod
    def _group(terms):
        # Quotes neutralize operators; bound parameters protect the SQL layer.
        return '(' + ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms if t.strip()) + ')'

    def search(self, gene, disease, limit=100, gene_aliases=None, disease_aliases=None, *, paper_ids=None,skip_reference_like=False):
        if not 1 <= limit <= 500:
            raise ValueError('Search limit must be 1..500')
        gene_terms = query_terms(gene, gene_aliases)
        disease_terms = query_terms(disease, disease_aliases)
        gene_query = self._group(gene_terms)
        combined = gene_query + ' AND ' + self._group(disease_terms)
        rows, seen = [], set()
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
            restriction=''
            parameters=()
            if paper_ids is not None:
                if not paper_ids:return []
                if len(paper_ids)>5:raise ValueError('Select at most 5 local papers')
                column='c.paper_id' if 'paper_id' in {r[1] for r in conn.execute('PRAGMA table_info(chunks)')} else "json_extract(c.payload,'$.paper_id')"
                restriction=' AND '+column+' IN ('+','.join('?' for _ in paper_ids)+')'
                parameters=tuple(paper_ids)
            for query in [combined, gene_query]:
                scan_limit=min(500,max(40,limit*8)) if skip_reference_like else limit
                found = conn.execute('''SELECT c.payload, bm25(text_index) AS rank
                    FROM text_index JOIN chunks c ON c.id=text_index.rowid
                    WHERE text_index MATCH ?'''+restriction+' ORDER BY rank LIMIT ?', (query,*parameters,scan_limit)).fetchall()
                for payload, rank in found:
                    record = annotate_source(json.loads(payload))
                    if skip_reference_like and 'reference_like' in record['source_quality_flags']:continue
                    if record['chunk_id'] in seen or not contains_any_term(record['context'], gene_terms):
                        continue
                    seen.add(record['chunk_id'])
                    record['retrieval_score'] = -rank
                    record['retrieval_match']='gene_and_disease' if contains_any_term(record['context'],disease_terms) else 'gene_only'
                    rows.append(record)
                    if len(rows) == limit:
                        return rows
        return rows

    def search_papers(self,gene,disease,limit=40,gene_aliases=None,disease_aliases=None,*,skip_reference_like=False):
        """Rank distinct papers first, then return bounded previews for each paper."""
        from literature_store import from_local_chunks
        if not 1<=limit<=100:raise ValueError('Paper limit must be 1..100')
        with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as conn:
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'paper_index' not in tables:
                return from_local_chunks(self.search(gene,disease,min(500,limit*5),gene_aliases,disease_aliases,skip_reference_like=skip_reference_like))[:limit]
            g=self._group(query_terms(gene,gene_aliases));d=self._group(query_terms(disease,disease_aliases))
            found=[];seen=set()
            for query,match in [(g+' AND '+d,'gene_and_disease'),(g,'gene_only')]:
                for pid,payload in conn.execute('SELECT p.paper_id,p.payload FROM paper_index JOIN papers p ON p.id=paper_index.rowid WHERE paper_index MATCH ? ORDER BY bm25(paper_index) LIMIT ?',(query,limit)):
                    if pid in seen:continue
                    seen.add(pid);found.append((pid,json.loads(payload),match))
                    if len(found)>=limit:break
                if len(found)>=limit:break
        snapshot=self.fingerprint()['snapshot_sha256']
        result=[]
        for pid,info,match in found:
            chunks=self.search(gene,disease,3,gene_aliases,disease_aliases,paper_ids=[pid],skip_reference_like=skip_reference_like)
            if not chunks:continue
            paper=from_local_chunks(chunks)[0]
            paper.update(local_corpus_paper_ids=[pid],local_snapshot_sha256=snapshot,local_document_match=match)
            result.append(paper)
        return result

    def with_neighbors(self,record,*,max_characters=9000,skip_reference_like=True):
        """Bounded, same-paper context with individual quote locations preserved."""
        if record.get('context_segments'):return record
        if not 100<=max_characters<=12000:raise ValueError('Context character cap must be 100..12000')
        ordered=[record];index=0
        if not record.get('source_type','').endswith('_abstract'):
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as conn:
                column='paper_id' if 'paper_id' in {r[1] for r in conn.execute('PRAGMA table_info(chunks)')} else "json_extract(payload,'$.paper_id')"
                found=conn.execute('SELECT payload FROM chunks WHERE '+column+'=? ORDER BY id',(record['paper_id'],))
                ordered=[json.loads(r[0]) for r in found]
            ordered=[r for r in ordered if r.get('source_type')==record.get('source_type')]
            index=next((i for i,c in enumerate(ordered) if c['chunk_id']==record['chunk_id']),-1)
            if index<0:ordered=[record];index=0
        selected={};used=0
        for offset in [0,-1,1,-2,2]:
            position=index+offset
            if not 0<=position<len(ordered):continue
            item=annotate_source(ordered[position])
            if record.get('page') is not None and item.get('page') is not None and abs(item['page']-record['page'])>1:continue
            if skip_reference_like and 'reference_like' in item['source_quality_flags']:continue
            remaining=max_characters-used-(2 if selected else 0)
            if remaining<100:continue
            text=item['context'][:remaining]
            if not text:continue
            selected[position]={**item,'context':text,'context_truncated':len(text)<len(item['context'])}
            used+=len(text)+(2 if len(selected)>1 else 0)
        parts=[selected[k] for k in sorted(selected)]
        if not parts:return {**record,'context':record['context'][:max_characters]}
        return {**record,'context':'\n\n'.join(p['context'] for p in parts),'context_segments':parts,
                'expanded_context':len(parts)>1}
