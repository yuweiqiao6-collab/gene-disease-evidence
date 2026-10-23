import importlib.util
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import time

import pytest

spec = importlib.util.spec_from_file_location('launch_local', Path(__file__).parents[1] / 'scripts/launch_local.py')
launcher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = launcher
spec.loader.exec_module(launcher)


def test_unknown_port_owner_is_never_contacted_or_signalled(monkeypatch, capsys):
    monkeypatch.setattr(launcher, 'port_available', lambda port: False)
    monkeypatch.setattr(launcher, 'find_services', lambda *args: [])
    def unexpected(*args):
        pytest.fail('An unrecognised service must not be contacted or modified')
    monkeypatch.setattr(launcher, 'wait_healthy', unexpected)
    monkeypatch.setattr(launcher, 'resume_service', unexpected)
    monkeypatch.setattr(os, 'execv', unexpected)
    assert launcher.main([]) == 1
    assert '没有停止或修改任何进程' in capsys.readouterr().err


def test_owned_unresponsive_service_is_not_killed_or_duplicated(monkeypatch, capsys):
    monkeypatch.setattr(launcher, 'port_available', lambda port: False)
    monkeypatch.setattr(launcher, 'find_services', lambda *args: [launcher.Service(123, '1', 'S')])
    monkeypatch.setattr(launcher, 'wait_healthy', lambda port: False)
    def unexpected(*args):
        pytest.fail('Do not kill or duplicate an unresponsive service')
    monkeypatch.setattr(os, 'kill', unexpected)
    monkeypatch.setattr(os, 'execv', unexpected)
    assert launcher.main([]) == 1
    assert '没有强制结束进程' in capsys.readouterr().err


def test_changed_process_identity_cannot_receive_resume_signal(monkeypatch):
    monkeypatch.setattr(os, 'pidfd_open', lambda pid: 321, raising=False)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda *args: pytest.fail('Identity changed'), raising=False)
    monkeypatch.setattr(os, 'close', lambda fd: None)
    monkeypatch.setattr(launcher, 'process_identity', lambda *args: launcher.Service(123, 'new-start', 'T'))
    assert not launcher.resume_service(launcher.Service(123, 'old-start', 'T'), launcher.ROOT, 8501)


def test_fresh_launch_keeps_loopback_and_explicit_port(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.setattr(launcher, 'port_available', lambda port: True)
    monkeypatch.setattr(launcher.importlib.util, 'find_spec', lambda name: object())
    monkeypatch.chdir(tmp_path)
    commands = []
    monkeypatch.setattr(os, 'execv', lambda executable, args: commands.append((executable, args)))
    assert launcher.main(['--port', '8502']) == 0
    assert commands == [(sys.executable, [sys.executable, '-m', 'streamlit', 'run', 'src/app.py',
                                        '--server.address=127.0.0.1', '--server.port=8502'])]
    assert 'http://127.0.0.1:8502' in capsys.readouterr().out


def test_missing_dependencies_gives_actionable_message(monkeypatch, capsys):
    monkeypatch.setattr(launcher, 'port_available', lambda port: True)
    monkeypatch.setattr(launcher.importlib.util, 'find_spec', lambda name: None)
    assert launcher.main([]) == 1
    assert '已配置的项目环境' in capsys.readouterr().err


@pytest.mark.parametrize('activate_custom_environment', [False, True])
def test_shell_uses_existing_environment_from_another_working_directory(tmp_path, activate_custom_environment):
    root = tmp_path / 'project with spaces'
    (root / 'scripts').mkdir(parents=True)
    shutil.copyfile(Path(__file__).parents[1] / 'scripts/run_local.sh', root / 'scripts/run_local.sh')
    release_python = root / '.venv-release/bin/python'
    active_python = tmp_path / 'custom environment/bin/python'
    for executable in [release_python, active_python]:
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text('#!/bin/sh\nprintf "%s\\n" "$0" "$@" > "$LAUNCH_RECORD"\n')
        executable.chmod(0o700)
    record = tmp_path / 'invocation.txt'
    env = dict(os.environ, LAUNCH_RECORD=str(record))
    env.pop('VIRTUAL_ENV', None)
    if activate_custom_environment:
        env['VIRTUAL_ENV'] = str(active_python.parent.parent)
    subprocess.run(['bash', str(root / 'scripts/run_local.sh'), '--port', '8502'],
                   cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    invocation = record.read_text().splitlines()
    assert (root / invocation[0]).resolve() == (active_python if activate_custom_environment else release_python)
    assert invocation[1:] == ['scripts/launch_local.py', '--port', '8502']


FAKE_STREAMLIT = '''
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
port = int(next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--server.port=')))
HTTPServer(('127.0.0.1', port), Handler).serve_forever()
'''


@pytest.fixture
def local_service(tmp_path):
    if sys.platform != 'linux' or not Path('/proc/net/tcp').exists():
        pytest.skip('Process ownership recovery is Linux-specific')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/app.py').write_text('')
    (tmp_path / 'streamlit.py').write_text(FAKE_STREAMLIT)
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    process = subprocess.Popen([sys.executable, '-m', 'streamlit', 'run', 'src/app.py',
                                '--server.address=127.0.0.1', f'--server.port={port}'],
                               cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        assert launcher.wait_healthy(port), 'Temporary local service did not start'
        yield process, port, tmp_path
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGCONT)
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        process.stderr.close()


def test_real_paused_process_recovers_then_repeated_launch_reuses_it(local_service, monkeypatch, capsys):
    process, port, root = local_service
    monkeypatch.setattr(launcher, 'ROOT', root)
    services = launcher.find_services(port, root)
    assert len(services) == 1 and services[0].pid == process.pid
    os.kill(process.pid, signal.SIGSTOP)
    deadline = time.monotonic() + 3
    while launcher.process_identity(process.pid, root, port).state != 'T':
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert launcher.main(['--port', str(port)]) == 0
    assert '已恢复本项目被暂停' in capsys.readouterr().out
    assert launcher.healthy(port)
    assert process.poll() is None
    assert launcher.main(['--port', str(port)]) == 0
    output = capsys.readouterr().out
    assert '已在运行' in output and '已恢复' not in output
    assert launcher.find_services(port, root)[0].pid == process.pid


def test_real_other_checkout_is_left_running(local_service, monkeypatch, capsys):
    process, port, root = local_service
    other = root / 'another-checkout'
    other.mkdir()
    monkeypatch.setattr(launcher, 'ROOT', other)
    assert launcher.main(['--port', str(port)]) == 1
    assert '无法确认' in capsys.readouterr().err
    assert process.poll() is None and launcher.healthy(port)
