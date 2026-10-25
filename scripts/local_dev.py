"""Local, isolated original-platform runner. No model keys or global PATH changes."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(.5)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def wait_http(url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError('Process exited; inspect .local/*.stderr.log')
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(.3)
    raise RuntimeError('Startup timeout: ' + url)


def launch(name, command, cwd, environment):
    with (LOCAL / (name + '.stdout.log')).open('ab') as out, (LOCAL / (name + '.stderr.log')).open('ab') as err:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=out, stderr=err,
                                   creationflags=NO_WINDOW)
    (LOCAL / (name + '.process.json')).write_text(json.dumps({'pid': process.pid, 'command': command}), encoding='utf-8')
    return process


def start():
    if not (LOCAL / 'connection.json').exists():
        raise RuntimeError('Missing project-local database credentials; see docs/local_setup.md')
    environment = os.environ.copy()
    # Explicitly keep this baseline free of model and embedding requests.
    for name in ('DASHSCOPE_API_KEY', 'AI_API_KEY', 'OPENAI_API_KEY'):
        environment[name] = ''
    environment.update(API_ADDR='127.0.0.1:8080',
        MYSQL_DSN=read_json(LOCAL / 'connection.json')['mysql_dsn'],
        NACOS_ADDR='http://127.0.0.1:1', BOOTSTRAP_VECTOR_INDEX='false', RUN_MIGRATIONS='true',
        APP_ENV='development', NODE_OPTIONS='--dns-result-order=ipv4first')
    if not listening(3307):
        mysql = LOCAL / 'mysql-8.4.13-winx64/bin/mysqld.exe'
        process = launch('mysql', [str(mysql), '--defaults-file=' + str(LOCAL / 'mysql.ini'), '--no-monitor', '--console'], ROOT, environment)
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('MySQL failed; inspect .local/mysql.stderr.log')
            if listening(3307):
                break
            time.sleep(.2)
        else:
            raise RuntimeError('Local MySQL startup timeout')
    if listening(8080) or listening(5173):
        raise RuntimeError('Port 8080/5173 is already in use. Run status; do not start another copy.')
    api = LOCAL / 'shop-api.exe'
    if not api.exists():
        raise RuntimeError('Build backend first with .local/go/bin/go.exe build')
    backend = launch('backend', [str(api)], ROOT / 'backend', environment)
    wait_http('http://127.0.0.1:8080/api/v1/health', backend)
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js not found')
    frontend = launch('frontend', [node, str(ROOT / 'frontend/node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', '5173', '--strictPort'], ROOT / 'frontend', environment)
    wait_http('http://127.0.0.1:5173/', frontend)
    print('Original platform ready: http://127.0.0.1:5173 (backend 8080, isolated MySQL 3307)')


def stop():
    # Compare the executable and command line before stopping recorded processes.
    for name in ('frontend', 'backend'):
        record_path = LOCAL / (name + '.process.json')
        if not record_path.exists():
            continue
        record = read_json(record_path)
        process_id = int(record['pid'])
        query = f"Get-CimInstance Win32_Process -Filter \"ProcessId={process_id}\" | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress"
        result = subprocess.run(['powershell', '-NoProfile', '-Command', query], capture_output=True, text=True, creationflags=NO_WINDOW)
        if result.stdout.strip():
            actual = json.loads(result.stdout)
            expected = str(Path(record['command'][0]).resolve())
            if os.path.normcase(actual.get('ExecutablePath') or '') != os.path.normcase(expected):
                raise RuntimeError('PID executable changed; refusing to stop ' + name)
            if name == 'frontend' and 'vite' not in (actual.get('CommandLine') or ''):
                raise RuntimeError('PID is not our Vite process; refusing to stop')
            subprocess.run(['taskkill', '/PID', str(process_id), '/F'], check=True, capture_output=True, creationflags=NO_WINDOW)
        record_path.unlink()
    mysqladmin = LOCAL / 'mysql-8.4.13-winx64/bin/mysqladmin.exe'
    if listening(3307):
        subprocess.run([str(mysqladmin), '--defaults-extra-file=' + str(LOCAL / 'mysql-admin.ini'), 'shutdown'], check=True, capture_output=True, creationflags=NO_WINDOW)
    print('Stopped project-local services; data retained.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status'), default='status', nargs='?')
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps({str(port): listening(port) for port in (3307,8080,5173)}, ensure_ascii=False))
    elif args.action == 'start':
        start()
    else:
        stop()


if __name__ == '__main__':
    main()
