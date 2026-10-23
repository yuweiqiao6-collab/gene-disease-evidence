import json
import secrets
import shutil
import sqlite3
import time
import pytest
from conftest import wait_for
from history_store import HistoryStore
from jobs import Job, JobManager, Query
from web_settings import WebSettings


def completed(manager,owner):
    task=manager.submit(owner,Query(('IL6',),'rheumatoid arthritis'))
    return task,wait_for(manager,owner,task)


def test_history_and_reviews_survive_restart_and_temp_cleanup(tmp_path,owner):
    settings=WebSettings(runtime_dir=tmp_path)
    manager=JobManager(settings)
    task,snapshot=completed(manager,owner)
    row=snapshot['rows'][0]
    manager.review(owner,task,row['evidence_id'],'corrected',changes={'species':'rat'},
        reason='Independent source check',reviewer='reviewer-a',expected_revision=0)
    shutil.rmtree(manager.root/task)
    manager.close()
    manager=JobManager(settings)
    try:
        restored=manager.snapshot(owner,task)
        assert restored['rows'][0]['species']=='rat'
        assert restored['rows'][0]['machine_result']['species']==row['species']
        assert restored['provenance']['application_version']==(__import__('pathlib').Path(__file__).parents[1]/'VERSION').read_text().strip()
        assert restored['provenance']['corpus_snapshot']
        assert manager.download(owner,task,'evidence.xlsx').startswith(b'PK')
        assert len(json.loads(manager.download(owner,task,'evidence.json',confirmed_only=True)))==1
        assert manager.list_history(secrets.token_hex(32))==[]
        with pytest.raises(PermissionError): manager.snapshot(secrets.token_hex(32),task)
        with pytest.raises(PermissionError): manager.review(secrets.token_hex(32),task,row['evidence_id'],'accepted',reviewer='x')
        # Repeated access to a history whose files expired must remain usable.
        manager.jobs[task].finished=time.time()-90000
        manager.cleanup()
        manager.snapshot(owner,task)
        manager.cleanup()
        manager.snapshot(owner,task)
        assert owner.encode() not in (tmp_path/'history.sqlite').read_bytes()
    finally:
        manager.close()


def test_review_audit_conflicts_and_deletion(manager,owner):
    task,snapshot=completed(manager,owner)
    eid=snapshot['rows'][0]['evidence_id']
    assert manager.review(owner,task,eid,'accepted',reviewer='a',expected_revision=0)==1
    with pytest.raises(ValueError,match='更新'):
        manager.review(owner,task,eid,'rejected',reason='bad',reviewer='b',expected_revision=0)
    with pytest.raises(ValueError): manager.review(owner,task,eid,'rejected',reviewer='a')
    with pytest.raises(ValueError): manager.review(owner,task,eid,'corrected',changes={'evidence_sentence':'fake'},reason='bad',reviewer='a')
    with pytest.raises(ValueError): manager.review(owner,task,eid,'corrected',changes={'species':snapshot['rows'][0]['species']},reason='same',reviewer='a')
    manager.review(owner,task,eid,'rejected',reason='Unsupported interpretation',reviewer='b',expected_revision=1)
    assert json.loads(manager.download(owner,task,'evidence.json',confirmed_only=True))==[]
    assert [r['revision'] for r in manager.review_audit(owner,task)]==[1,2]
    assert manager.snapshot(owner,task)['rows'][0]['machine_result']==snapshot['rows'][0]['machine_result']
    with pytest.raises(PermissionError): manager.delete_history(secrets.token_hex(32),task)
    manager.delete_history(owner,task)
    assert manager.list_history(owner)==[]
    with sqlite3.connect(manager.history.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]==0
    with pytest.raises(PermissionError): manager.download(owner,task,'evidence.json')


def test_restart_marks_incomplete_without_retrying(tmp_path,owner,monkeypatch):
    history=HistoryStore(tmp_path/'history.sqlite')
    job=Job('a'*32,owner,Query(('IL6',),'arthritis'),tmp_path/'tasks'/('a'*32),state='running')
    history.save(job)
    def forbidden(*args,**kwargs): raise AssertionError('No network or replay on restart')
    monkeypatch.setattr('jobs.LLMClient',forbidden)
    manager=JobManager(WebSettings(runtime_dir=tmp_path))
    try:
        assert manager.snapshot(owner,job.id)['state']=='interrupted'
        assert manager.snapshot(owner,job.id)['rows']==[]
        assert manager.list_history(owner)[0]['state']=='interrupted'
    finally: manager.close()


def test_single_instance_and_rate_budget_survive_restart(tmp_path,owner):
    settings=WebSettings(runtime_dir=tmp_path)
    manager=JobManager(settings)
    with pytest.raises(RuntimeError,match='已有程序'): JobManager(settings)
    for _ in range(3): completed(manager,owner)
    manager.close()
    manager=JobManager(settings)
    try:
        with pytest.raises(ValueError,match='频繁'): manager.submit(owner,Query(('IL6',),'arthritis'))
    finally: manager.close()
