"""Desktop launch entry: no system Python or working-directory assumptions."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import socket
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
PYTHON_MEDIA = ROOT / '.venv' / 'Scripts' / 'python.exe'
PYTHON_CONSOLE = ROOT / '.venv' / 'Scripts' / 'python.exe'


def get_json(url):
    with urlopen(url, timeout=2) as response:
        return json.load(response)


def launch(kind, no_browser=False):
    RUNTIME.mkdir(exist_ok=True)
    lock = (RUNTIME / 'launch.lock').open('a+b')
    import msvcrt
    lock.seek(0)
    lock.write(b'0')
    lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
    try:
        registry_path = RUNTIME / 'services.json'
        registry = json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.exists() else {}
        previous = registry.get(kind)
        if previous:
            import psutil
            try:
                command = psutil.Process(previous['pid']).cmdline()
                owned = any(str(ROOT).lower() in item.lower() for item in command)
                if owned:
                    get_json(previous['url'] + previous['probe'])
                    if not no_browser:
                        webbrowser.open(previous['url'])
                    print(previous['url'])
                    return previous['url']
            except (OSError, ValueError, psutil.Error):
                pass
        def free_port(start):
            for candidate in range(start, start + 100):
                with socket.socket() as listener:
                    try:
                        listener.bind(("127.0.0.1", candidate))
                        return candidate
                    except OSError:
                        pass
            raise RuntimeError("No available local port")
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', SUITE_NO_BROWSER='1',
                   PLAYWRIGHT_BROWSERS_PATH=str(RUNTIME / 'browsers'),
                   MODELSCOPE_CACHE=str(RUNTIME / 'models'), HF_HOME=str(RUNTIME / 'huggingface'))
        env['PATH'] = str(RUNTIME / 'python') + os.pathsep + str(RUNTIME / 'ffmpeg' / 'bin') + os.pathsep + env.get('PATH', '')
        if kind == 'douyin':
            port = free_port(18420)
            command = [str(PYTHON_MEDIA), '-u', str(ROOT / '操作台_server.py')]
            env['SUITE_DOUYIN_PORT'] = str(port)
            probe = '/api/status'
        elif kind == 'bilibili':
            port = free_port(18440)
            command = [str(PYTHON_MEDIA), '-u', str(ROOT / 'bili_console.py'), '--no-browser', '--port', str(port)]
            probe = '/api/status'
        elif kind == 'console':
            state = json.loads((ROOT / '.local' / 'config' / 'console-state.json').read_text(encoding='utf-8'))
            port = free_port(state['api_port'])
            state['api_port'] = port
            (ROOT / '.local' / 'config' / 'console-state.json').write_text(json.dumps(state), encoding='utf-8')
            command = [str(PYTHON_CONSOLE), '-u', str(ROOT / 'scripts' / 'console_bootstrap.py'), '--serve']
            probe = '/health/ready'
        else:
            raise ValueError('Unknown console')
        url = f'http://127.0.0.1:{port}'
        with (RUNTIME / (kind + '.log')).open('ab') as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(180):
            if process.poll() is not None:
                raise RuntimeError('Startup failed. See ' + str(RUNTIME / (kind + '.log')))
            try:
                get_json(url + probe)
                registry[kind] = {'url': url, 'pid': process.pid, 'probe': probe}
                registry_path.write_text(json.dumps(registry), encoding='utf-8')
                if not no_browser:
                    webbrowser.open(url)
                print(url)
                return url
            except (OSError, ValueError):
                time.sleep(1)
        raise RuntimeError('Startup timed out; check the local log before retrying.')
    finally:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


if __name__ == '__main__':
    try:
        launch(json.loads((ROOT / 'app.json').read_text(encoding='utf-8'))['kind'], '--no-browser' in sys.argv)
    except Exception as error:
        (RUNTIME / 'launch-error.txt').write_text(str(error), encoding='utf-8')
        import ctypes
        if '--no-browser' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(0, str(error), '工具箱启动失败', 0x10)
        else:
            print(str(error), file=sys.stderr)
        raise SystemExit(1)
