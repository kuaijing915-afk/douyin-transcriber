"""Export tracked source only; fail closed on private/runtime files."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    files = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z']).decode().split('\0')
    files = [name for name in files if name]
    if not files:
        raise RuntimeError('No tracked source files')
    prohibited = {'.local', '.runtime', 'node_modules', '__pycache__', 'dist', 'logs', '输出', '配置', 'video and audio', '操作台数据'}
    for name in files:
        path = Path(name)
        if (any(part in prohibited or part.startswith('.venv') for part in path.parts)
                or path.name.startswith('.env') or path.name == 'cookie.txt'
                or path.suffix in {'.db', '.sqlite', '.log'}):
            raise RuntimeError('Private file unexpectedly tracked: ' + name)
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(r'E:\独立运行的工具项目压缩包'))
    destination = parser.parse_args().output
    destination.mkdir(parents=True, exist_ok=True)
    app = json.loads((ROOT / 'app.json').read_text(encoding='utf-8'))
    slug = app['slug']
    archive = destination / (app['name'] + '_独立安装包.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        for name in files:
            package.write(ROOT / name, slug + '/' + name)
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None or len(package.namelist()) != len(files):
            raise RuntimeError('Release archive verification failed')
    digest = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest + '  ' + archive.name + '\n', encoding='utf-8')
    print(json.dumps({'archive': str(archive), 'files': len(files), 'sha256': digest}, ensure_ascii=False))


if __name__ == '__main__':
    main()
