"""Local video upload -> original first audio stream, without re-encoding."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, quote, urlparse


def extract_upload(handler, root):
    size = int(handler.headers.get('Content-Length', '0'))
    if size <= 0 or not handler.headers.get('Content-Type', '').startswith('application/octet-stream'):
        raise ValueError('请选择本地视频文件。')
    root = Path(root)
    work = root / '.local' / 'audio-extract'
    work.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(work).free < size * 2 + 5 * 1024 * 1024:
        raise ValueError('项目所在磁盘空间不足，请先释放空间。')
    name = parse_qs(urlparse(handler.path).query).get('filename', ['视频'])[0]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(name).stem)[:100].rstrip(' .') or '视频'
    tools = root / '.runtime' / 'ffmpeg' / 'bin'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    handler.connection.settimeout(120)
    with tempfile.TemporaryDirectory(dir=work, prefix='extract-') as directory:
        source = Path(directory) / 'input.media'
        with source.open('wb') as stream:
            remaining = size
            while remaining:
                chunk = handler.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('文件上传中断，请重试。')
                stream.write(chunk)
                remaining -= len(chunk)
        probe = subprocess.run([str(tools / 'ffprobe.exe'), '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'a:0',
                                '-show_entries', 'stream=codec_name,sample_rate,channels', '-of', 'json', str(source)],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60, creationflags=flags)
        if probe.returncode:
            raise ValueError('无法读取这个视频，请检查文件是否完整。')
        streams = json.loads(probe.stdout).get('streams', [])
        if not streams:
            raise ValueError('这个视频没有音轨。')
        original = streams[0]
        codec = original['codec_name']
        extension = {'aac': 'm4a', 'alac': 'm4a', 'mp3': 'mp3', 'opus': 'mka', 'vorbis': 'mka',
                     'flac': 'flac', 'ac3': 'ac3', 'eac3': 'eac3'}.get(codec, 'mka')
        if codec.startswith('pcm_') and codec.endswith('le'):
            extension = 'wav'
        output = Path(directory) / ('audio.' + extension)
        result = subprocess.run([str(tools / 'ffmpeg.exe'), '-nostdin', '-v', 'error', '-n', '-protocol_whitelist', 'file,pipe', '-i', str(source),
                                 '-map', '0:a:0', '-vn', '-c:a', 'copy', '-map_metadata', '0', str(output)],
                                capture_output=True, timeout=600, creationflags=flags)
        if result.returncode or not output.is_file() or not output.stat().st_size:
            raise ValueError('此音轨无法直接封装为音频文件；未进行任何有损转码。')
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/octet-stream')
        handler.send_header('Content-Length', str(output.stat().st_size))
        handler.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(stem + '_音频.' + extension, safe=''))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.end_headers()
        with output.open('rb') as stream:
            shutil.copyfileobj(stream, handler.wfile, length=1024 * 1024)
