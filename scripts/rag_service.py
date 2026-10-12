"""Start/stop only the RAG process recorded by this launcher."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'data/rag/run'
STATE = RUN / 'service.json'


def running(state: dict) -> bool:
    try:
        command = Path(f'/proc/{state["pid"]}/cmdline').read_bytes()
        started = Path(f'/proc/{state["pid"]}/stat').read_text().split()[21]
        return b'semi_rag' in command and started == state['started']
    except (OSError, KeyError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['start', 'stop', 'status'])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8090)
    parser.add_argument('--trusted-network', action='store_true',
                        help='allow unauthenticated binding for an approved private network')
    args = parser.parse_args()
    RUN.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if args.command == 'stop':
        if state and running(state):
            os.kill(state['pid'], signal.SIGTERM)
            for _ in range(30):
                if not running(state):
                    print('RAG stopped')
                    return
                time.sleep(1)
            raise SystemExit('Graceful shutdown still pending; no force kill performed')
        print('RAG is not running')
        return
    if args.command == 'status':
        print(json.dumps({**state, 'running': bool(state and running(state))}, indent=2))
        return
    if state and running(state):
        print(f'Already running: {state["url"]}')
        return
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1' if args.host == '0.0.0.0' else args.host, args.port)) == 0:
            raise SystemExit(f'Port {args.port} already in use; choose another --port')
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not (
        os.environ.get('RAG_API_KEY') or args.trusted_network
    ):
        raise SystemExit('Set RAG_API_KEY or use --trusted-network before binding beyond localhost')
    env = os.environ.copy()
    env['PYTHONPATH'] = str(ROOT / 'src')
    env['TOKENIZERS_PARALLELISM'] = 'false'
    with (RUN / 'server.log').open('a') as log:
        command = [str(ROOT / '.venv-rag/bin/python'), '-m', 'semi_rag',
                   'serve', '--host', args.host, '--port', str(args.port)]
        if args.trusted_network:
            command.append('--trusted-network')
        process = subprocess.Popen(command, cwd=ROOT, env=env,
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    state = {'pid': process.pid, 'started': Path(f'/proc/{process.pid}/stat').read_text().split()[21],
             'url': f'http://{args.host}:{args.port}', 'trusted_network': args.trusted_network,
             'log': str(RUN / 'server.log')}
    STATE.write_text(json.dumps(state, indent=2))
    print(json.dumps(state), flush=True)
    with httpx.Client(trust_env=False, timeout=6) as client:
        for _ in range(90):
            if process.poll() is not None:
                raise SystemExit(f'Service exited; inspect {state["log"]}')
            try:
                response = client.get(f'http://127.0.0.1:{args.port}/health')
                if response.status_code == 200:
                    print(f'Ready: {state["url"]}', flush=True)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
    raise SystemExit(f'Service not yet healthy; inspect {state["log"]}')


if __name__ == '__main__':
    main()
