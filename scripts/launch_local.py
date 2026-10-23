"""Start or reuse this checkout's loopback-only Streamlit service without killing it."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import http.client
import importlib.util
import os
from pathlib import Path
import signal
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
HOST = '127.0.0.1'


@dataclass(frozen=True)
class Service:
    pid: int
    started: str
    state: str


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return False
            raise
    return True


def process_identity(pid: int, root: Path, port: int) -> Service | None:
    """Require the same user, checkout, Python executable and explicit launch arguments."""
    proc = Path('/proc') / str(pid)
    try:
        if proc.stat().st_uid != os.getuid() or (proc / 'cwd').resolve() != root.resolve():
            return None
        if not (proc / 'exe').readlink().name.startswith('python'):
            return None
        args = (proc / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
        if args[1:4] != ['-m', 'streamlit', 'run'] or len(args) < 5:
            return None
        if (root / args[4]).resolve() != (root / 'src/app.py').resolve():
            return None
        # Only our explicit local launch command qualifies for automatic recovery.
        if args[5:] != [f'--server.address={HOST}', f'--server.port={port}']:
            return None
        stat = (proc / 'stat').read_text().rsplit(') ', 1)[1].split()
        return Service(pid, stat[19], stat[0])
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def find_services(port: int, root: Path) -> list[Service]:
    """Match the actual listening socket to an owned process, not just a saved PID."""
    if not hasattr(os, 'getuid'):
        return []
    try:
        lines = Path('/proc/net/tcp').read_text().splitlines()[1:]
    except OSError:
        return []
    inodes = set()
    for line in lines:
        fields = line.split()
        address, number = fields[1].split(':')
        if (fields[3] == '0A' and int(number, 16) == port
                and address in {'0100007F', '00000000'} and int(fields[7]) == os.getuid()):
            inodes.add(f'socket:[{fields[9]}]')
    if not inodes:
        return []
    services = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        service = process_identity(int(proc.name), root, port)
        if service is None:
            continue
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    if str(fd.readlink()) in inodes:
                        services.append(service)
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return services


def resume_service(service: Service, root: Path, port: int) -> bool:
    """Recheck identity and process start time before sending only SIGCONT."""
    descriptor = None
    try:
        if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
            descriptor = os.pidfd_open(service.pid)
        current = process_identity(service.pid, root, port)
        if current is None or current.started != service.started:
            return False
        if current.state != 'T':
            return current.state not in {'t', 'Z', 'X'}
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, signal.SIGCONT)
        else:
            os.kill(service.pid, signal.SIGCONT)
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def healthy(port: int) -> bool:
    # HTTPConnection neither uses proxy environment variables nor follows redirects.
    connection = http.client.HTTPConnection(HOST, port, timeout=1)
    try:
        connection.request('GET', '/_stcore/health')
        response = connection.getresponse()
        return response.status == 200 and response.read(32).strip() == b'ok'
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def wait_healthy(port: int) -> bool:
    deadline = time.monotonic() + 5
    while True:
        if healthy(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='启动、复用或恢复本项目的本机网页服务')
    parser.add_argument('--port', type=int, default=8501, help='网页端口，默认8501')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在1至65535之间')
    root = ROOT.resolve()
    url = f'http://{HOST}:{args.port}'
    try:
        available = port_available(args.port)
    except OSError as error:
        print(f'无法检查端口：{error}', file=sys.stderr)
        return 1
    if not available:
        services = find_services(args.port, root)
        if len(services) != 1:
            print(f'端口 {args.port} 已被占用，无法确认是本账号、本目录的服务；没有停止或修改任何进程。',
                  file=sys.stderr)
            print('可指定其他端口，例如：bash scripts/run_local.sh --port 8502', file=sys.stderr)
            return 1
        service = services[0]
        if service.state == 'T':
            if not resume_service(service, root, args.port):
                print('原服务身份已变化或无法恢复，请检查原终端；没有启动第二份服务。', file=sys.stderr)
                return 1
            print(f'已恢复本项目被暂停的网页服务（进程编号 {service.pid}）。', flush=True)
        if not wait_healthy(args.port):
            print('本项目的服务占着端口，但暂时无法响应。请在原终端检查报错；没有强制结束进程。',
                  file=sys.stderr)
            return 1
        print(f'本项目的网页服务已在运行，直接打开：\n{url}', flush=True)
        print('已复用原服务；更新代码或配置后，仍需在原终端停止旧服务再启动。')
        return 0
    if importlib.util.find_spec('streamlit') is None:
        print('当前 Python 环境缺少 Streamlit（网页框架）。请使用已配置的项目环境，或按首页安装依赖。',
              file=sys.stderr)
        return 1
    print(f'正在启动本项目网页，启动成功后打开：\n{url}', flush=True)
    print('保持终端运行；Ctrl+C 停止，Ctrl+Z 会暂停并继续占用端口。', flush=True)
    os.chdir(root)
    os.execv(sys.executable, [sys.executable, '-m', 'streamlit', 'run', 'src/app.py',
                            f'--server.address={HOST}', f'--server.port={args.port}'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
