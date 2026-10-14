# Runtime components

The ZIPs here contain public runtime binaries only. No account, browser profile,
installed third-party Python package, or business data is bundled.

- Python 3.11.15: [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases/tag/20260623). Its licenses remain inside `python-runtime.zip`.
- uv 0.11.24: [Astral uv](https://github.com/astral-sh/uv/releases/tag/0.11.24), MIT license included here.
- FFmpeg 9.0.2 essentials: [Gyan builds](https://www.gyan.dev/ffmpeg/builds/). The archive preserves its license and readme; only ffmpeg/ffprobe executables are needed. Source is available through the upstream build page and [FFmpeg](https://ffmpeg.org/download.html).

Exact archive hashes are recorded in `scripts/runtime-assets.json`.
