import secrets
import time
from pathlib import Path
import pytest
import urllib.request
from jobs import JobManager, TERMINAL
from web_settings import WebSettings


@pytest.fixture(autouse=True)
def forbid_unmocked_external_requests(monkeypatch):
    monkeypatch.setenv('RAG_ORIGINAL_CONFIG_FILE','')
    def forbidden(*args,**kwargs):
        raise AssertionError('Tests must mock external requests; no real model or literature calls allowed')
    monkeypatch.setattr(urllib.request,'urlopen',forbidden)


@pytest.fixture
def owner():
    return secrets.token_hex(32)


@pytest.fixture
def manager(tmp_path):
    manager=JobManager(WebSettings(runtime_dir=tmp_path))
    yield manager
    manager.close()


def wait_for(manager, owner, task):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        snapshot=manager.snapshot(owner,task)
        if snapshot['state'] in TERMINAL:
            return snapshot
        time.sleep(.01)
    raise AssertionError('Task timed out')
