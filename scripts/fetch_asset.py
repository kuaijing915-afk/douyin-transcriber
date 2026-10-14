"""Download an immutable runtime archive with verified ranges and SHA-256."""
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen


def fetch(asset, target):
    expected = asset['sha256']
    if target.exists():
        with target.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() == expected:
                return
    part = target.with_suffix(target.suffix + '.partial')
    with urlopen(Request(asset['url'], method='HEAD'), timeout=60) as response:
        length = int(response.headers['Content-Length'])
    step = 8 * 1024 * 1024
    with part.open('wb') as stream:
        for start in range(0, length, step):
            end = min(start + step - 1, length - 1)
            with urlopen(Request(asset['url'], headers={'Range': f'bytes={start}-{end}'}), timeout=90) as response:
                if response.status == 200 and start == 0:
                    while data := response.read(1024 * 1024):
                        stream.write(data)
                    break
                if response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {start}-'):
                    raise RuntimeError('Server returned the wrong download range')
                data = response.read()
            if len(data) != end - start + 1:
                raise RuntimeError('Runtime archive download was incomplete')
            stream.write(data)
    with part.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise RuntimeError('Runtime archive checksum mismatch')
    part.replace(target)


if __name__ == '__main__':
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    fetch(manifest[sys.argv[2]], Path(sys.argv[3]))
