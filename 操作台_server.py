#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音转录系统 · 全自动操作台后端服务器
=========================================
提供 HTTP API，用于接收前端请求并执行本地脚本，通过 SSE 流返回实时输出。

技术栈: Python http.server (零依赖) + subprocess + SSE + threading

启动: python 操作台_server.py
端口: 从 18320 开始自动检测空闲端口
"""

import io
from pathlib import Path
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import socket
import subprocess
import sys
from model_paths import model_path
import threading
import time
import webbrowser
from datetime import datetime
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── 持久化日志配置 ────────────────────────────────────────────
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_server_logger = logging.getLogger("douyin_server")
_server_logger.setLevel(logging.DEBUG)

_fh = logging.handlers.RotatingFileHandler(
    os.path.join(_LOG_DIR, "server.log"),
    maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
))
_server_logger.addHandler(_fh)


def _server_log(level: str, msg: str):
    """写服务日志（不抛异常，失败静默忽略）。"""
    try:
        msg = re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]*", r"\1?[参数已隐藏]", msg)
        msg = re.sub(r"(?i)(cookie|authorization|sessionid)\s*[:=][^\r\n]*", r"\1=[已隐藏]", msg)
        getattr(_server_logger, level)(msg)
    except Exception:
        pass

# ========================================================================
# 全局常量
# ========================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = str(Path(BASE_DIR) / ".venv" / "Scripts" / "python.exe")
GLOBAL_PYTHON = VENV_PYTHON

# ========================================================================
# 全局状态（线程安全）
# ========================================================================

_current_process: subprocess.Popen | None = None
_cancel_flag = threading.Event()
_task_start_lock = threading.Lock()       # 防止并发启动任务
_task_completed = threading.Event()       # 任务完成后 set，供 SSE 断开时等待
_task_completed.set()                     # 初始为已完成状态

# ==================== FunASR 模型缓存 ====================
_funasr_model = None
_funasr_model_lock = threading.Lock()


def _get_funasr_model():
    """获取FunASR模型（懒加载，仅加载一次，缓存复用）"""
    global _funasr_model
    if _funasr_model is not None:
        return _funasr_model

    with _funasr_model_lock:
        if _funasr_model is not None:
            return _funasr_model

        import sys
        # 设置国内镜像
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

        print("正在加载 FunASR 模型（首次 ~30秒，之后秒开）...")
        sys.stdout.flush()

        from funasr import AutoModel

        _funasr_model = AutoModel(
            model=model_path("paraformer-zh"),
            vad_model=model_path("fsmn-vad"),
            punc_model=model_path("ct-punc"),
            device="cpu",
            disable_update=True,
        )
        print("FunASR 模型加载完成！")
        sys.stdout.flush()

    return _funasr_model


# ========================================================================
# 辅助函数
# ========================================================================

def get_free_port(start: int = 18320) -> int:
    """从 start 开始寻找第一个可用端口"""
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.settimeout(0.5)
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def kill_process_tree(pid: int) -> None:
    """强制终止进程树 (Windows taskkill /T)"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


class _TaskQueue(queue.Queue):
    """将实际任务结果写入日志，而不是把线程返回当作成功。"""

    def put(self, item, block=True, timeout=None):
        event, data = item
        if event == "error":
            _server_log("error", str(data))
        elif event == "done":
            state = "成功" if data == 0 else "取消" if _cancel_flag.is_set() else "失败"
            _server_log("info" if data == 0 else "warning", f"任务结束：{state}，退出码 {data}")
        super().put(item, block, timeout)


def get_output_dirs() -> list[dict]:
    """扫描 输出\\ 目录，返回 [{name, files, path}, ...]"""
    output_base = os.path.join(BASE_DIR, "输出")
    if not os.path.isdir(output_base):
        return []

    result: list[dict] = []
    for name in sorted(os.listdir(output_base)):
        dir_path = os.path.join(output_base, name)
        if os.path.isdir(dir_path):
            files = [
                f
                for f in os.listdir(dir_path)
                if os.path.isfile(os.path.join(dir_path, f))
            ]
            result.append({
                "name": name,
                "files": len(files),
                "path": os.path.join("输出", name),
            })
    return result


def _auto_get_sec_uid() -> str | None:
    """从 cookie 自动获取用户 sec_uid，用户无感知"""
    cookie_path = os.path.join(BASE_DIR, ".local", "credentials", "cookie_like.txt")
    if not os.path.isfile(cookie_path):
        return None
    try:
        with open(cookie_path, "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except Exception:
        return None
    if not cookie:
        return None

    # 内联 Python 脚本：调抖音 profile API 获取 sec_uid
    test_code = """import json, sys
from pathlib import Path
import certifi
from curl_cffi import requests
cookie = COOKIE_PLACEHOLDER
bundle = Path(sys.base_prefix) / "Lib/site-packages/certifi/cacert.pem"
ca_bundle = str(bundle) if bundle.is_file() else certifi.where()
resp = requests.get("https://www.douyin.com/aweme/v1/web/user/profile/self/",
    headers={"Cookie": cookie, "Referer": "https://www.douyin.com/"},
    params={"device_platform":"webapp","aid":"6383"},
    impersonate="edge101", timeout=15, proxy="", verify=ca_bundle)
if resp.status_code != 200:
    print(f"用户资料接口 HTTP {resp.status_code}", file=sys.stderr)
    raise SystemExit(1)
data = resp.json()
user = data.get("user") or data.get("data",{}).get("user") or {}
if not user.get("sec_uid"):
    print(f"用户资料接口未返回登录用户，status_code={data.get('status_code')}", file=sys.stderr)
    raise SystemExit(1)
print(user.get("sec_uid",""))
"""
    test_code = test_code.replace("COOKIE_PLACEHOLDER", json.dumps(cookie))

    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    try:
        tmp.write(test_code)
        tmp.close()
        result = subprocess.run(
            [VENV_PYTHON, "-u", tmp.name],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            _server_log("warning", result.stderr.strip()[:500] or "用户资料验证失败")
            return None
        output = result.stdout.strip()
        return output if output else None
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _auto_create_batch_dir() -> tuple[str, str]:
    """自动创建批次目录 → 返回 (目录名, 绝对路径)"""
    from datetime import datetime
    now = datetime.now()
    dir_name = f"batch_{now.strftime('%Y%m%d_%H%M')}"
    dir_path = os.path.join(BASE_DIR, "输出", dir_name)
    os.makedirs(dir_path, exist_ok=True)
    return dir_name, dir_path


def _validate_creator_url(value: str) -> str | None:
    """只接受抖音主页或抖音分享短链接。"""
    value = value.strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        return None
    if host != "douyin.com" and not host.endswith(".douyin.com"):
        return None
    if host == "v.douyin.com" or re.match(r"^/user/[^/]+", parsed.path):
        return value
    return None


def _drain_queue(q: queue.Queue, timeout: float = 30) -> None:
    """耗尽队列直到收到 'done' 事件（用于 SSE 断开后的 cleanup）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ev_type, _ = q.get(timeout=1)
            if ev_type == "done":
                return
        except queue.Empty:
            continue


def run_script(
    cmd: list[str],
    cwd: str,
    output_queue: queue.Queue,
) -> None:
    """执行命令并通过队列发送实时输出行。

    发送的事件类型: log / error / result / done
    """
    global _current_process

    process: subprocess.Popen | None = None
    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        _current_process = process

        # 逐行读取输出
        for line in iter(process.stdout.readline, ""):
            if _cancel_flag.is_set():
                kill_process_tree(process.pid)
                break
            output_queue.put(("log", line.rstrip()))

        process.wait()
        exit_code = process.returncode

        if _cancel_flag.is_set():
            output_queue.put(("log", "[已取消] 任务被用户中止"))
            output_queue.put(("done", -1))
        else:
            output_queue.put(("done", exit_code))

    except FileNotFoundError as e:
        output_queue.put(("error", f"找不到可执行文件: {e}"))
        output_queue.put(("done", -1))
    except Exception as e:
        output_queue.put(("error", f"执行失败: {e}"))
        output_queue.put(("done", -1))
    finally:
        if _current_process is process:
            _current_process = None


# ========================================================================
# HTTP 请求处理器
# ========================================================================

class RequestHandler(BaseHTTPRequestHandler):

    # ---- 基础 ----

    def log_message(self, fmt: str, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")

    # ---- CORS ----

    def _set_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "http://" + self.headers.get("Host", "localhost"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Cache-Control")

    def _local_request(self):
        host = self.headers.get("Host", "")
        allowed = {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        return host in allowed and (not origin or origin in {"http://" + h for h in allowed})

    def do_OPTIONS(self):
        if not self._local_request():
            self._send_error(403, "只允许本机操作台请求")
            return
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    # ---- 路由: GET / POST ----

    def do_GET(self):
        if not self._local_request():
            self._send_error(403, "只允许本机操作台请求")
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        try:
            # 服务静态文件：操作台 HTML
            if path == "" or path == "/" or path == "/操作台.html":
                self._serve_html()
                return

            if path == "/api/status":
                self._handle_status()
            elif path == "/api/output":
                self._handle_list_outputs()
            elif path.startswith("/api/output/"):
                dir_name = path.split("/api/output/")[1]
                self._handle_list_output_files(dir_name)
            elif path == "/api/output_file":
                file_path = params.get("path", [None])[0]
                self._handle_output_file(file_path)
            elif path == "/api/results":
                self._handle_results()
            elif path == "/api/cookie/check":
                self._handle_cookie_check()
            else:
                self._send_json(404, {"error": "Not Found", "path": path})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def do_POST(self):
        if not self._local_request():
            self._send_error(403, "只允许本机操作台请求")
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            if path == "/api/audio-extract":
                from audio_extract import extract_upload
                try:
                    extract_upload(self, BASE_DIR)
                except ValueError as exc:
                    self._send_error(400, str(exc))
            elif path == "/api/cancel":
                self._handle_cancel()
            elif path == "/api/pipeline":
                body = self._read_body_json()
                self._handle_pipeline(body)
            elif path == "/api/run/download":
                body = self._read_body_json()
                self._handle_run_download(body)
            elif path == "/api/run/transcribe":
                body = self._read_body_json()
                self._handle_run_transcribe(body)
            elif path.startswith("/api/run/"):
                tool_name = path.split("/api/run/")[1]
                body = self._read_body_json()
                self._handle_run_tool(tool_name, body)
            elif path == "/api/cookie/save":
                body = self._read_body_json()
                self._handle_cookie_save(body)
            elif path == "/api/cookie/test":
                self._handle_cookie_test()
            else:
                self._send_json(404, {"error": "Not Found", "path": path})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    # ---- JSON 响应辅助 ----

    def _send_json(self, status_code: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status_code: int, message: str) -> None:
        self._send_json(status_code, {"error": message})

    def _read_body_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        # 兼容 Windows BOM (utf-8-sig)
        text = raw.decode("utf-8-sig")
        return json.loads(text)

    # ---- 服务 HTML ----

    def _serve_html(self):
        """提供操作台前端页面。"""
        html_path = os.path.join(BASE_DIR, "操作台.html")
        if not os.path.isfile(html_path):
            self._send_error(404, "操作台.html 不存在")
            return
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                content = f.read()
            body = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._send_error(500, f"读取 HTML 失败: {e}")

    # ---- SSE 流 ----

    def _start_sse(self):
        self.send_response(200)
        self._set_cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _send_sse_event(self, event_type: str, text: str):
        payload = json.dumps({"type": event_type, "text": text}, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_sse_done(self, exit_code: int):
        payload = json.dumps({"type": "done", "exit_code": exit_code}, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream_sse_from_queue(self, q: queue.Queue):
        """从队列消费事件并通过 SSE 发送给客户端。"""
        self._start_sse()
        while True:
            try:
                ev_type, data = q.get(timeout=30)
            except queue.Empty:
                # 心跳保活
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                continue

            try:
                if ev_type == "done":
                    self._send_sse_done(data)
                    return
                elif ev_type == "progress":
                    payload = json.dumps(
                        {"type": "progress", "current": data["current"], "total": data["total"]},
                        ensure_ascii=False,
                    )
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                elif ev_type == "error":
                    self._send_sse_event("error", data)
                elif ev_type == "result":
                    self._send_sse_event("result", data)
                else:  # log
                    self._send_sse_event("log", data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    # ================================================================
    # API: GET /api/status
    # ================================================================

    def _handle_status(self):
        # Python 可用性
        python_global = os.path.isfile(GLOBAL_PYTHON)
        python_venv = os.path.isfile(VENV_PYTHON)
        python_ok = python_global or python_venv

        # ffmpeg 可用性
        ffmpeg_ok = False
        try:
            r = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            ffmpeg_ok = r.returncode == 0
        except Exception:
            pass

        # Cookie 检查
        cookie_path = os.path.join(BASE_DIR, ".local", "credentials", "cookie_like.txt")
        cookie_ok = False
        cookie_size = 0
        cookie_time = ""
        if os.path.isfile(cookie_path):
            try:
                st = os.stat(cookie_path)
                cookie_size = st.st_size
                cookie_ok = st.st_size > 0
                cookie_time = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        # 视频文件计数
        video_dir = os.path.join(BASE_DIR, "video and audio")
        video_count = 0
        if os.path.isdir(video_dir):
            for f in os.listdir(video_dir):
                fp = os.path.join(video_dir, f)
                if os.path.isfile(fp) and not f.endswith(
                    (".wav", ".txt", ".json", ".log", ".md")
                ):
                    video_count += 1

        # 运行状态
        running = not _task_start_lock.acquire(blocking=False)
        if not running:
            _task_start_lock.release()

        # 最近的输出批次
        output_dirs = get_output_dirs()
        results = [
            {"name": d["name"], "files": d["files"], "time": ""}
            for d in output_dirs[-5:]
        ]

        # 尝试获取批次时间（取目录内最新文件的 mtime 的时间部分）
        for r_item in results:
            dir_path = os.path.join(BASE_DIR, "输出", r_item["name"])
            if os.path.isdir(dir_path):
                latest_mtime = 0
                for f_name in os.listdir(dir_path):
                    fp = os.path.join(dir_path, f_name)
                    if os.path.isfile(fp):
                        m = os.stat(fp).st_mtime
                        if m > latest_mtime:
                            latest_mtime = m
                if latest_mtime:
                    r_item["time"] = datetime.fromtimestamp(latest_mtime).strftime("%H:%M")

        ready = python_ok and ffmpeg_ok

        self._send_json(200, {
            "ready": ready,
            "model_loaded": _funasr_model is not None,
            "suite_id": hashlib.sha256(BASE_DIR.encode()).hexdigest()[:16],
            "cookie_ok": cookie_ok,
            "cookie_size": cookie_size,
            "cookie_time": cookie_time,
            "video_count": video_count,
            "running": running,
            "results": results,
        })

    # ================================================================
    # API: GET /api/results
    # ================================================================

    def _handle_results(self):
        """返回详细的结果信息（喜欢列表批次和博主主页批次）。"""
        output_base = os.path.join(BASE_DIR, "输出")
        batches = []

        if os.path.isdir(output_base):
            for name in sorted(os.listdir(output_base)):
                dir_path = os.path.join(output_base, name)
                if os.path.isdir(dir_path) and name.startswith(("batch_", "creator_")):
                    files = []
                    dir_mtime = 0
                    for f_name in sorted(os.listdir(dir_path)):
                        fp = os.path.join(dir_path, f_name)
                        if os.path.isfile(fp):
                            st = os.stat(fp)
                            if st.st_mtime > dir_mtime:
                                dir_mtime = st.st_mtime
                            files.append({
                                "name": f_name,
                                "size": st.st_size,
                                "path": os.path.join("输出", name, f_name),
                            })
                    batches.append({
                        "name": name,
                        "dir": os.path.join("输出", name),
                        "time": datetime.fromtimestamp(dir_mtime).strftime("%Y-%m-%d %H:%M") if dir_mtime else "",
                        "files": files,
                    })

        # 按时间倒序（最新的在前）
        batches.reverse()
        self._send_json(200, {"batches": batches})

    # ================================================================
    # API: POST /api/cookie/save
    # ================================================================

    def _handle_cookie_save(self, body: dict):
        cookie = body.get("cookie", "").strip()
        if not cookie:
            self._send_error(400, "Cookie 不能为空")
            return

        config_dir = os.path.join(BASE_DIR, ".local", "credentials")
        os.makedirs(config_dir, exist_ok=True)

        file1 = os.path.join(config_dir, "cookie_like.txt")
        file2 = os.path.join(config_dir, "douyin_cookie.txt")

        self._start_sse()
        try:
            with open(file1, "w", encoding="utf-8") as f:
                f.write(cookie)
            with open(file2, "w", encoding="utf-8") as f:
                f.write(cookie)
            self._send_sse_event("log", "Cookie 已保存")
            self._send_sse_done(0)
        except Exception as e:
            self._send_sse_event("error", f"保存失败: {e}")
            self._send_sse_done(-1)

    # ================================================================
    # API: GET /api/cookie/check
    # ================================================================

    def _handle_cookie_check(self):
        cookie_path = os.path.join(BASE_DIR, ".local", "credentials", "cookie_like.txt")
        if not os.path.isfile(cookie_path):
            self._send_json(200, {"exists": False, "size": 0, "modified": "", "preview": ""})
            return

        st = os.stat(cookie_path)
        preview = ""
        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                preview = "已保存（内容不显示）"
        except Exception:
            pass

        modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        self._send_json(200, {
            "exists": True,
            "size": st.st_size,
            "modified": modified,
            "preview": preview,
        })

    # ================================================================
    # API: POST /api/cookie/test
    # ================================================================

    def _handle_cookie_test(self):
        self._start_sse()

        cookie_path = os.path.join(BASE_DIR, ".local", "credentials", "cookie_like.txt")
        if not os.path.isfile(cookie_path):
            self._send_sse_event("error", "Cookie 文件不存在")
            self._send_sse_done(-1)
            return

        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                cookie = f.read().strip()
            if not cookie:
                self._send_sse_event("error", "Cookie 文件为空")
                self._send_sse_done(-1)
                return

            self._send_sse_event("log", "正在测试 Cookie 有效性...")

            # 内联测试脚本：使用 curl_cffi 调用抖音 profile API
            test_code = """import json, sys
from pathlib import Path
import certifi
from curl_cffi import requests
cookie = COOKIE_PLACEHOLDER
bundle = Path(sys.base_prefix) / "Lib/site-packages/certifi/cacert.pem"
ca_bundle = str(bundle) if bundle.is_file() else certifi.where()
resp = requests.get(
    "https://www.douyin.com/aweme/v1/web/user/profile/self/",
    headers={"Cookie": cookie, "Referer": "https://www.douyin.com/"},
    params={"aid": "6383", "device_platform": "webapp", "channel": "channel_pc_web"},
    impersonate="edge101",
    timeout=15,
    proxy="",
    verify=ca_bundle,
)
data = resp.json()
out = {"http_status": resp.status_code, "api_status": data.get("status_code"), "ok": resp.status_code == 200 and data.get("status_code") == 0}
print(json.dumps(out, ensure_ascii=False))
"""
            test_code = test_code.replace("COOKIE_PLACEHOLDER", json.dumps(cookie))

            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
            try:
                tmp.write(test_code)
                tmp.close()

                result = subprocess.run(
                    [VENV_PYTHON, "-u", tmp.name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )

                if result.returncode != 0:
                    err_msg = result.stderr.strip()[:300] or "未知错误"
                    self._send_sse_event("error", f"测试执行失败: {err_msg}")
                    self._send_sse_done(-1)
                    return

                output = result.stdout.strip()
                if not output:
                    self._send_sse_event("error", "测试无输出，可能请求被拦截")
                    self._send_sse_done(-1)
                    return

                data = json.loads(output.split("\n")[-1])

                if data.get("ok"):
                    self._send_sse_event("log", f"Cookie 有效！(HTTP {data['http_status']}, status_code={data['api_status']})")
                    self._send_sse_done(0)
                else:
                    self._send_sse_event("error", f"Cookie 可能过期 (HTTP {data['http_status']}, API status_code={data.get('api_status', 'N/A')})")
                    self._send_sse_done(-1)
            finally:
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
        except subprocess.TimeoutExpired:
            self._send_sse_event("error", "测试超时（30秒），请求抖音 API 无响应")
            self._send_sse_done(-1)
        except Exception as e:
            self._send_sse_event("error", f"测试失败: {e}")
            self._send_sse_done(-1)

    # ================================================================
    # API: GET /api/output
    # ================================================================

    def _handle_list_outputs(self):
        self._send_json(200, {"dirs": get_output_dirs()})

    # ================================================================
    # API: GET /api/output/{dir_name}
    # ================================================================

    def _handle_list_output_files(self, dir_name: str):
        output_root = Path(BASE_DIR, "输出").resolve()
        dir_path = str((output_root / dir_name).resolve())
        if not Path(dir_path).is_relative_to(output_root):
            self._send_error(403, "仅允许查看本项目输出目录")
            return
        if not os.path.isdir(dir_path):
            self._send_error(404, f"目录不存在: {dir_name}")
            return

        files = []
        for f in sorted(os.listdir(dir_path)):
            fp = os.path.join(dir_path, f)
            if os.path.isfile(fp):
                st = os.stat(fp)
                files.append({
                    "name": f,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    "path": os.path.join("输出", dir_name, f),
                })
        self._send_json(200, {"files": files, "dir": dir_name})

    # ================================================================
    # API: GET /api/output_file?path=xxx
    # ================================================================

    def _handle_output_file(self, file_path: str | None):
        if not file_path:
            self._send_error(400, "缺少 path 参数")
            return

        abs_path = os.path.realpath(os.path.join(BASE_DIR, file_path))
        output_root = os.path.realpath(os.path.join(BASE_DIR, "输出"))
        if not Path(abs_path).is_relative_to(Path(output_root)) or not abs_path.lower().endswith(".txt"):
            self._send_error(403, "仅允许读取输出目录中的逐字稿")
            return
        if not os.path.isfile(abs_path):
            self._send_error(404, f"文件不存在: {file_path}")
            return

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
            self._send_json(200, {"content": content, "path": file_path})
        except Exception as e:
            self._send_error(500, f"读取失败: {e}")

    # ================================================================
    # API: POST /api/cancel
    # ================================================================

    def _handle_cancel(self):
        if _task_start_lock.locked():
            _cancel_flag.set()
            process = _current_process
            if process is not None:
                kill_process_tree(process.pid)
            self._send_json(200, {"status": "cancelling", "message": "已请求停止，等待当前处理退出"})
        else:
            self._send_json(200, {"status": "no_task", "message": "当前没有运行中的任务"})

    # ================================================================
    # API: POST /api/run/{tool_name}
    # ================================================================

    def _handle_run_tool(self, tool_name: str, body: dict):
        if not _task_start_lock.acquire(blocking=False):
            self._send_error(429, "已有任务在运行，请等待当前任务完成")
            return

        _cancel_flag.clear()
        _task_completed.clear()
        q: queue.Queue = _TaskQueue()

        t = threading.Thread(
            target=self._execute_tool_wrapper,
            args=(tool_name, body, q),
            daemon=True,
        )
        t.start()

        try:
            self._stream_sse_from_queue(q)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端断开 → 后台任务继续，等待它完成
            _drain_queue(q)
        finally:
            _task_completed.wait()
            _task_start_lock.release()

    def _execute_tool_wrapper(self, tool_name: str, body: dict, q: queue.Queue):
        """包装 _execute_tool，确保 _task_completed 被 set"""
        try:
            self._execute_tool(tool_name, body, q)
        except Exception as e:
            try:
                q.put(("error", f"执行器异常: {e}"))
                q.put(("done", -1))
            except Exception:
                pass
        finally:
            _task_completed.set()

    def _execute_tool(self, tool_name: str, body: dict, q: queue.Queue):
        """工具分发"""
        if tool_name == "get_sec_uid":
            self._run_get_sec_uid(q)
        elif tool_name == "download_likes":
            self._run_download_likes(q)
        elif tool_name == "download_post":
            creator_url = _validate_creator_url(str(body.get("url", "")))
            if not creator_url:
                q.put(("error", "请输入有效的抖音博主主页链接"))
                q.put(("done", -1))
                return
            try:
                count = int(body.get("count", 0) or 0)
            except (TypeError, ValueError):
                count = -1
            if count < 0 or count > 999:
                q.put(("error", "下载数量必须是 1-999，留空表示全部"))
                q.put(("done", -1))
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            download_dir = os.path.join(
                BASE_DIR, "video and audio", f"creator_{stamp}"
            )
            self._run_download_post(creator_url, download_dir, count, q)
        elif tool_name == "batch_transcribe":
            self._run_batch_transcribe(body, q)
        elif tool_name == "watch_transcribe":
            self._run_watch_transcribe(q)
        elif tool_name == "open_output":
            os.startfile(os.path.join(BASE_DIR, "输出"))
            q.put(("result", "已打开输出目录"))
            q.put(("done", 0))
        elif tool_name == "open_video":
            os.startfile(os.path.join(BASE_DIR, "video and audio"))
            q.put(("result", "已打开视频目录"))
            q.put(("done", 0))
        elif tool_name == "open_config":
            os.startfile(os.path.join(BASE_DIR, "配置"))
            q.put(("result", "已打开配置目录"))
            q.put(("done", 0))
        elif tool_name == "cleanup":
            self._run_cleanup(q)
        else:
            q.put(("error", f"未知工具: {tool_name}"))
            q.put(("done", -1))

    # ---- 各工具执行 ----

    @staticmethod
    def _run_get_sec_uid(q: queue.Queue):
        script = os.path.join(BASE_DIR, "辅助工具", "get_sec_uid.py")
        q.put(("log", "执行: get_sec_uid.py\n"))
        run_script([VENV_PYTHON, "-u", script], BASE_DIR, q)

    @staticmethod
    def _run_download_likes(q: queue.Queue):
        script = os.path.join(BASE_DIR, "下载工具", "download_likes_curl.py")
        q.put(("log", "执行下载脚本\n"))
        run_script([VENV_PYTHON, "-u", script], BASE_DIR, q)

    @staticmethod
    def _run_download_post(
        creator_url: str,
        download_dir: str,
        count: int,
        q: queue.Queue,
    ):
        script = os.path.join(BASE_DIR, "下载工具", "download_creator.py")
        q.put(("log", "执行博主主页下载\n"))
        run_script(
            [
                VENV_PYTHON, "-u", script,
                "--url", creator_url,
                "--output", download_dir,
                "--count", str(count),
            ],
            BASE_DIR,
            q,
        )

    @staticmethod
    def _run_batch_transcribe(body: dict, q: queue.Queue):
        script_path = os.path.join(BASE_DIR, "转录工具", "batch_transcribe.py")
        output_dir_name = body.get("output_dir", "")

        if output_dir_name:
            custom_output = os.path.join(BASE_DIR, "输出", output_dir_name)
            os.makedirs(custom_output, exist_ok=True)
            q.put(("log", f"输出目录: {custom_output}\n"))

            with open(script_path, "r", encoding="utf-8") as f:
                src = f.read()

            # 替换 OUTPUT_DIR 为自定义目录
            # 注意: re.sub 替换字符串中 \ 是转义符，需要双写
            src = re.sub(
                r'^OUTPUT_DIR\s*=.*$',
                lambda _: f'OUTPUT_DIR = {custom_output!r}',
                src,
                flags=re.MULTILINE,
            )
            # 可选的 FILE_PREFIX 覆盖
            if "file_prefix" in body:
                safe_prefix = body["file_prefix"].replace("\\", "\\\\")
                src = re.sub(
                    r'^FILE_PREFIX\s*=\s*"[^"]*"',
                    f'FILE_PREFIX = "{safe_prefix}"',
                    src,
                    flags=re.MULTILINE,
                )

            # 写入临时文件 → 执行 → 清理
            temp_path = script_path + ".tmp_run.py"
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    f.write(src)
                q.put(("log", f"执行: batch_transcribe.py (输出 → {output_dir_name})\n"))
                run_script([GLOBAL_PYTHON, "-u", temp_path], BASE_DIR, q)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
        else:
            q.put(("log", "执行: batch_transcribe.py\n"))
            run_script([GLOBAL_PYTHON, "-u", script_path], BASE_DIR, q)

    @staticmethod
    def _run_watch_transcribe(q: queue.Queue):
        script_path = os.path.join(BASE_DIR, "转录工具", "watch_transcribe.py")
        q.put(("log", "执行: watch_transcribe.py\n"))
        q.put(("log", "提示: 请在服务器终端按 Ctrl+C 停止监听\n"))
        run_script([GLOBAL_PYTHON, "-u", script_path], BASE_DIR, q)

    @staticmethod
    def _run_cleanup(q: queue.Queue):
        video_dir = os.path.join(BASE_DIR, "video and audio")
        wav_count = 0
        mp4_count = 0
        # 临时音频子目录
        audio_dir = os.path.join(video_dir, "临时音频")
        if os.path.isdir(video_dir):
            for f in os.listdir(video_dir):
                fp = os.path.join(video_dir, f)
                try:
                    if f.endswith(".wav"):
                        os.remove(fp)
                        wav_count += 1
                        q.put(("log", f"已删除临时音频: {f}"))
                    elif f.endswith(".mp4"):
                        os.remove(fp)
                        mp4_count += 1
                        q.put(("log", f"已删除视频: {f}"))
                except Exception as e:
                    q.put(("log", f"删除失败 {f}: {e}"))
        # 清理独立音频子目录
        if os.path.isdir(audio_dir):
            for f in os.listdir(audio_dir):
                fp = os.path.join(audio_dir, f)
                try:
                    if os.path.isfile(fp):
                        os.remove(fp)
                        wav_count += 1
                        q.put(("log", f"已删除临时音频: {f}"))
                except Exception as e:
                    q.put(("log", f"删除失败 {f}: {e}"))
        q.put(("result", f"清理完成，删除 {mp4_count} 个视频 + {wav_count} 个临时音频"))
        q.put(("done", 0))

    # ================================================================
    # API: POST /api/run/download — 单独下载（自动获取 sec_uid）
    # ================================================================

    def _handle_run_download(self, body: dict):
        if not _task_start_lock.acquire(blocking=False):
            self._send_error(429, "已有任务在运行，请等待当前任务完成")
            return

        _cancel_flag.clear()
        _task_completed.clear()
        q: queue.Queue = _TaskQueue()

        t = threading.Thread(target=self._execute_download, args=(q, body), daemon=True)
        t.start()

        try:
            self._stream_sse_from_queue(q)
        except (BrokenPipeError, ConnectionResetError, OSError):
            _drain_queue(q)
        finally:
            _task_completed.wait()
            _task_start_lock.release()

    def _execute_download(self, q: queue.Queue, body: dict = {}):
        try:
            # 0. 下载数量限制
            count = body.get("count", 0)
            if count > 0:
                os.environ["DOWNLOAD_MAX_COUNT"] = str(count)
                q.put(("log", f"限下载 {count} 个视频\n"))

            # 1. 检查 Cookie
            cookie_path = os.path.join(BASE_DIR, ".local", "credentials", "cookie_like.txt")
            if not os.path.isfile(cookie_path):
                q.put(("error", "Cookie 文件不存在"))
                q.put(("done", -1))
                return

            # 2. 自动获取 sec_uid
            q.put(("log", "正在自动获取用户信息...\n"))
            sec_uid = _auto_get_sec_uid()
            if not sec_uid:
                q.put(("error", "无法获取 sec_uid，Cookie 可能已过期"))
                q.put(("done", -1))
                return
            q.put(("log", "已获取 sec_uid\n"))

            # 3. 保存 sec_uid 供下载脚本使用
            sec_uid_path = os.path.join(BASE_DIR, "配置", "sec_uid.txt")
            with open(sec_uid_path, "w", encoding="utf-8") as f:
                f.write(sec_uid)
            q.put(("log", "sec_uid 已保存到配置目录\n"))

            # 4. 下载点赞视频
            q.put(("log", "开始下载点赞视频...\n"))
            self._run_download_likes(q)

        except Exception as e:
            try:
                q.put(("error", f"下载失败: {e}"))
                q.put(("done", -1))
            except Exception:
                pass
        finally:
            _task_completed.set()

    # ================================================================
    # API: POST /api/run/transcribe — 单独转录
    # ================================================================

    def _handle_run_transcribe(self, body: dict):
        if not _task_start_lock.acquire(blocking=False):
            self._send_error(429, "已有任务在运行，请等待当前任务完成")
            return

        _cancel_flag.clear()
        _task_completed.clear()
        q: queue.Queue = _TaskQueue()

        t = threading.Thread(
            target=self._execute_transcribe,
            args=(body, q),
            daemon=True,
        )
        t.start()

        try:
            self._stream_sse_from_queue(q)
        except (BrokenPipeError, ConnectionResetError, OSError):
            _drain_queue(q)
        finally:
            _task_completed.wait()
            _task_start_lock.release()

    def _execute_transcribe(self, body: dict, q: queue.Queue):
        try:
            self._run_transcribe_with_model(body, q)
        except Exception as e:
            try:
                q.put(("error", f"转录失败: {e}"))
                q.put(("done", -1))
            except Exception:
                pass
        finally:
            _task_completed.set()

    # ================================================================
    # API: POST /api/pipeline  — 一键全流程
    # ================================================================

    def _handle_pipeline(self, body: dict):
        if not _task_start_lock.acquire(blocking=False):
            self._send_error(429, "已有任务在运行，请等待当前任务完成")
            _server_log("warning", "流水线请求被拒绝：任务正在运行中")
            return

        _cancel_flag.clear()
        _task_completed.clear()
        q: queue.Queue = _TaskQueue()

        t = threading.Thread(
            target=self._execute_pipeline_wrapper,
            args=(body, q),
            daemon=True,
        )
        t.start()

        try:
            self._stream_sse_from_queue(q)
        except (BrokenPipeError, ConnectionResetError, OSError):
            _drain_queue(q)
        finally:
            _task_completed.wait()
            _task_start_lock.release()

    def _execute_pipeline_wrapper(self, body: dict, q: queue.Queue):
        try:
            _server_log("info", "流水线任务开始")
            self._execute_pipeline(body, q)
        except Exception as e:
            _server_log("error", f"流水线异常: {e}")
            try:
                q.put(("error", f"流水线异常: {e}"))
                q.put(("done", -1))
            except Exception:
                pass
        finally:
            _task_completed.set()

    def _run_pipeline_step(self, step_func, q: queue.Queue, *args):
        """执行流水线中的一个步骤，拦截其 done 事件以防中断 SSE 流。

        每个步骤使用内部队列，只向主队列转发 log/error/result 事件，
        步骤自身的 done 事件被转换为日志消息，
        流水线结束后由 _execute_pipeline 统一发送最终的 done。
        """
        inner_q: queue.Queue = queue.Queue()
        step_exit_code = [-1]

        def forward():
            while True:
                ev_type, data = inner_q.get()
                if ev_type == "done":
                    step_exit_code[0] = data if isinstance(data, int) else -1
                    q.put(("log", f"[步骤完成] 退出码: {data}"))
                    break
                q.put((ev_type, data))

        fwd = threading.Thread(target=forward, daemon=True)
        fwd.start()
        step_func(*args, inner_q)
        fwd.join(timeout=30)
        return step_exit_code[0]

    def _execute_pipeline(self, body: dict, q: queue.Queue):
        if body.get("source") == "creator":
            self._execute_creator_pipeline(body, q)
            return

        # 1. 检查 Cookie
        cookie_path = os.path.join(BASE_DIR, ".local", "credentials", "cookie_like.txt")
        if not os.path.isfile(cookie_path):
            q.put(("error", "Cookie 文件不存在"))
            q.put(("done", -1))
            return

        # 2. 自动获取 sec_uid
        q.put(("log", "\n步骤 1/3: 获取用户信息\n"))
        q.put(("log", "正在自动获取用户信息...\n"))
        sec_uid = _auto_get_sec_uid()
        if not sec_uid:
            q.put(("error", "无法获取 sec_uid，Cookie 可能已过期"))
            q.put(("done", -1))
            return
        q.put(("log", "已获取 sec_uid\n"))

        # 保存 sec_uid 供下载脚本使用
        sec_uid_path = os.path.join(BASE_DIR, "配置", "sec_uid.txt")
        with open(sec_uid_path, "w", encoding="utf-8") as f:
            f.write(sec_uid)
        q.put(("log", "sec_uid 已保存\n"))

        if _cancel_flag.is_set():
            q.put(("log", "\n流程已取消\n"))
            q.put(("done", -1))
            return

        # 下载数量限制
        dl_count = body.get("count", 0)
        if dl_count > 0:
            os.environ["DOWNLOAD_MAX_COUNT"] = str(dl_count)
            q.put(("log", f"限下载 {dl_count} 个\n"))
        else:
            os.environ.pop("DOWNLOAD_MAX_COUNT", None)

        # 3. 下载点赞视频
        q.put(("log", f"\n{'=' * 50}\n"))
        q.put(("log", "步骤 2/3: 下载点赞视频\n"))
        q.put(("log", f"{'=' * 50}\n"))
        download_exit_code = self._run_pipeline_step(self._run_download_likes, q)

        if _cancel_flag.is_set():
            q.put(("log", "\n流程已取消\n"))
            q.put(("done", -1))
            return
        if download_exit_code != 0:
            q.put(("error", "喜欢列表下载失败，已停止转录"))
            q.put(("done", -1))
            return

        # 4. 自动创建批次目录
        dir_name, dir_path = _auto_create_batch_dir()
        q.put(("log", f"批次目录: {dir_name}\n"))

        # 5. 转录
        q.put(("log", f"\n{'=' * 50}\n"))
        q.put(("log", "步骤 3/3: 转录视频\n"))
        q.put(("log", f"{'=' * 50}\n"))
        transcribe_exit_code = self._run_pipeline_step(
            self._run_pipeline_transcribe, q, dir_name
        )
        if _cancel_flag.is_set():
            q.put(("log", "\n流程已取消\n"))
            q.put(("done", -1))
            return
        if transcribe_exit_code != 0:
            q.put(("error", "喜欢列表转录失败，流程未完成"))
            q.put(("done", -1))
            return

        q.put(("log", f"\n{'=' * 50}\n"))
        q.put(("log", "全流程执行完成!\n"))
        q.put(("result", "全流程执行完成"))
        q.put(("done", 0))

    def _execute_creator_pipeline(self, body: dict, q: queue.Queue):
        creator_url = _validate_creator_url(str(body.get("url", "")))
        if not creator_url:
            q.put(("error", "请输入有效的抖音博主主页链接"))
            q.put(("done", -1))
            return

        try:
            count = int(body.get("count", 0) or 0)
        except (TypeError, ValueError):
            count = -1
        if count < 0 or count > 999:
            q.put(("error", "下载数量必须是 1-999，留空表示全部"))
            q.put(("done", -1))
            return

        cookie_path = os.path.join(BASE_DIR, ".local", "credentials", "cookie_like.txt")
        if not os.path.isfile(cookie_path):
            q.put(("error", "Cookie 文件不存在"))
            q.put(("done", -1))
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        video_dir = os.path.join(BASE_DIR, "video and audio", f"creator_{stamp}")
        output_name = f"creator_{stamp}"
        os.makedirs(video_dir, exist_ok=True)

        q.put(("log", "\n步骤 1/2: 下载博主视频\n"))
        download_exit_code = self._run_pipeline_step(
            self._run_download_post,
            q,
            creator_url,
            video_dir,
            count,
        )
        if _cancel_flag.is_set():
            q.put(("log", "\n流程已取消\n"))
            q.put(("done", -1))
            return
        if download_exit_code != 0:
            downloaded = [
                name for name in os.listdir(video_dir)
                if name.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm"))
                and os.path.isfile(os.path.join(video_dir, name))
                and os.path.getsize(os.path.join(video_dir, name)) > 50000
            ]
            if not downloaded:
                q.put(("error", "博主视频下载失败，未获得可转录视频；请检查登录状态及下载日志"))
                q.put(("done", -1))
                return
            q.put(("error", f"博主下载中断，已保留 {len(downloaded)} 个视频；继续转录已下载部分，整批下载尚未完成"))

        q.put(("log", "\n步骤 2/2: 转录博主视频\n"))
        transcribe_exit_code = self._run_pipeline_step(
            self._run_creator_transcribe,
            q,
            output_name,
            video_dir,
        )
        if _cancel_flag.is_set():
            q.put(("log", "\n流程已取消\n"))
            q.put(("done", -1))
            return
        if transcribe_exit_code != 0:
            q.put(("error", "博主视频转录失败，流程未完成"))
            q.put(("done", -1))
            return

        if download_exit_code != 0:
            q.put(("result", f"已下载部分已转录：{output_name}；下载中断，整批未完成"))
            q.put(("done", -1))
        else:
            q.put(("result", f"博主主页批量转录完成：{output_name}"))
            q.put(("done", 0))

    def _run_transcribe_with_model(
        self,
        body: dict,
        q: queue.Queue,
        video_dir: str | None = None,
        source_label: str = "喜欢列表",
    ):
        """使用预加载的FunASR模型直接转录（不启动子进程）"""
        creator_mode = video_dir is not None
        video_dir = video_dir or os.path.join(BASE_DIR, "video and audio")
        if not os.path.isdir(video_dir):
            q.put(("error", "视频目录不存在"))
            q.put(("done", -1))
            return

        # 创建批次目录
        import time
        dir_name = body.get("dir", f"batch_{datetime.now().strftime('%Y%m%d_%H%M')}")
        if not isinstance(dir_name, str) or not dir_name or Path(dir_name).name != dir_name or dir_name in {'.', '..'}:
            q.put(("error", "批次名必须是本项目中的单个文件夹名"))
            q.put(("done", -1))
            return
        output_dir = os.path.join(BASE_DIR, "输出", dir_name)

        # 错误日志文件
        error_log_path = os.path.join(output_dir, "_errors.log")

        def _log_error(msg: str):
            """同时发送到SSE和写入持久化错误日志"""
            q.put(("error", msg))
            with open(error_log_path, "a", encoding="utf-8") as ef:
                ef.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")

        # 扫描视频文件
        videos = sorted([
            f for f in os.listdir(video_dir)
            if os.path.isfile(os.path.join(video_dir, f))
            and (creator_mode or f.startswith("like_"))
            and not f.endswith(('.wav', '.txt', '.json', '.log', '.md'))
        ])

        if not videos:
            q.put(("error", f"没有找到{source_label}视频"))
            q.put(("done", -1))
            return

        os.makedirs(output_dir, exist_ok=True)
        q.put(("log", f"输出目录: {dir_name}\n"))

        # 独立音频临时目录
        audio_dir = os.path.join(video_dir, "临时音频")
        os.makedirs(audio_dir, exist_ok=True)

        q.put(("log", f"找到 {len(videos)} 个{source_label}视频\n"))

        # 获取预加载的模型
        q.put(("log", "加载 FunASR 模型中...\n"))
        try:
            model = _get_funasr_model()
        except Exception as e:
            q.put(("error", f"模型加载失败: {e}"))
            q.put(("done", -1))
            return
        q.put(("log", "模型已就绪\n"))

        # ffmpeg 路径
        ffmpeg_bin = str(Path(BASE_DIR) / ".runtime" / "ffmpeg" / "bin")
        os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")

        success = 0
        for i, video_name in enumerate(videos, 1):
            if _cancel_flag.is_set():
                q.put(("log", "[已取消]\n"))
                q.put(("done", -1))
                return

            video_path = os.path.join(video_dir, video_name)
            q.put(("log", f"\n[{i}/{len(videos)}] {video_name[:50]}...\n"))

            # 生成输出文件名
            base_name = video_name
            for ext in (".mp4", ".MP4", ".mkv", ".avi", ".mov", ".flv"):
                if base_name.endswith(ext):
                    base_name = base_name[:-len(ext)]
                    break

            short_name = base_name[:80]
            audio_path = os.path.join(audio_dir, f"_tmp_{i:04d}.wav")
            output_path = os.path.join(output_dir, f"{i:04d}_{short_name}.txt")

            t_start = time.time()

            # 跳过已存在的
            if os.path.exists(output_path):
                with open(output_path, encoding="utf-8") as existing:
                    valid_output = re.search(r"^\[\d+:\d+\]\s*\S", existing.read(), re.MULTILINE)
                if valid_output:
                    q.put(("log", "  跳过（已有有效转录）\n"))
                    success += 1
                    continue

            # ffmpeg 提取音频
            q.put(("log", "  提取音频...\n"))
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
                 "-ar", "16000", "-ac", "1", audio_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=120,  # 2分钟超时，防止损坏视频永久卡死
            )
            if result.returncode != 0:
                _log_error(f"  ffmpeg失败 [{video_name[:50]}]: {result.stderr[:200]}\n")
                continue

            if _cancel_flag.is_set():
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                q.put(("done", -1))
                return

            # 转录
            q.put(("log", "  转文字中...\n"))
            try:
                rec_result = model.generate(
                    input=audio_path,
                    language="zh",
                    batch_size_s=300,
                )
            except Exception as e:
                _log_error(f"  转录异常 [{video_name[:50]}]: {e}\n")
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                continue

            if _cancel_flag.is_set():
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                q.put(("log", "[已取消]\n"))
                q.put(("done", -1))
                return

            # 写入结果
            text_segments = [seg for seg in (rec_result or []) if (seg.get("text") or "").strip()]
            if text_segments:
                last_ts = text_segments[-1].get("timestamp") or []
                total_dur = last_ts[-1][1] / 1000 if last_ts else 0
                mm, ss = int(total_dur // 60), int(total_dur % 60)

                with open(output_path, 'w', encoding='utf-8') as f:
                    title = short_name[20:] if len(short_name) > 20 else short_name
                    f.write(f'{title}\n\n')
                    f.write(f'总时长: {mm:02d}:{ss:02d}\n')
                    f.write(f'{"=" * 60}\n\n')
                    for seg in text_segments:
                        timestamps = seg.get("timestamp") or [[0, 0]]
                        ts = timestamps[0][0] / 1000
                        m, s = int(ts // 60), int(ts % 60)
                        text = seg.get("text", "").strip()
                        if text:
                            f.write(f'[{m:02d}:{s:02d}] {text}\n\n')

                elapsed = time.time() - t_start
                q.put(("log", f"  \u2705 完成! ({mm:02d}:{ss:02d}, {elapsed:.0f}s)\n"))
                success += 1
            else:
                _log_error(f"  转录结果为空（模型未识别到语音） [{video_name[:50]}]\n")

            # 清理临时音频
            if os.path.exists(audio_path):
                os.remove(audio_path)

            # 发送进度
            q.put(("progress", {"current": i, "total": len(videos), "message": f"正在转录第{i}/{len(videos)}个视频..."}))

        q.put(("log", f"\n处理结束，成功: {success}/{len(videos)}\n"))
        if success != len(videos):
            q.put(("error", f"有 {len(videos) - success} 个视频转录失败，详情见批次内 _errors.log"))
        q.put(("done", 0 if success == len(videos) else -1))

    def _run_pipeline_transcribe(self, transcribe_dir: str, q: queue.Queue):
        """使用预加载的FunASR模型直接转录（流水线中使用，不启动子进程）"""
        body = {"dir": transcribe_dir}
        self._run_transcribe_with_model(body, q)

    def _run_creator_transcribe(
        self,
        transcribe_dir: str,
        video_dir: str,
        q: queue.Queue,
    ):
        """只转录本次博主主页下载目录。"""
        body = {"dir": transcribe_dir}
        self._run_transcribe_with_model(body, q, video_dir, "博主主页")


# ========================================================================
# 主函数
# ========================================================================

def main():
    # 确保控制台输出 UTF-8
    if sys.stdout.encoding not in ("utf-8", "UTF-8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    port = get_free_port(int(os.environ.get("SUITE_DOUYIN_PORT", "18420")))
    server_address = ("127.0.0.1", port)

    httpd = ThreadingHTTPServer(server_address, RequestHandler)

    url = f"http://localhost:{port}"

    print("=" * 55)
    print("  抖音转录系统 · 操作台后端服务器")
    print("=" * 55)
    print(f"  服务地址: http://localhost:{port}")
    print(f"  前端页面: {url}")
    print(f"  API 端点:")
    print(f"    GET  /api/status          — 系统状态 (一站式)")
    print(f"    GET  /api/results         — 输出结果详情")
    print(f"    POST /api/run/download    — 单独下载 (自动 sec_uid, SSE)")
    print(f"    POST /api/run/transcribe  — 单独转录 (自动建批次, SSE)")
    print(f"    POST /api/run/{{tool}}     — 其他工具 (SSE)")
    print(f"    GET  /api/output          — 输出目录列表")
    print(f"    GET  /api/output/{{dir}}   — 目录文件列表")
    print(f"    GET  /api/output_file     — 预览转录文件")
    print(f"    POST /api/pipeline        — 一键全流程 (自动 sec_uid, SSE)")
    print(f"    POST /api/cancel          — 取消当前任务")
    print(f"  支持的工具:")
    print(f"    download_likes / download_post / batch_transcribe")
    print(f"    get_sec_uid / watch_transcribe / cleanup")
    print(f"    open_output / open_video / open_config")
    print("=" * 55)
    print("  Ctrl+C 停止服务器")
    print("=" * 55)

    # 自动打开浏览器
    try:
        if not os.environ.get("SUITE_NO_BROWSER"):
            webbrowser.open(url)
        print(f"  [OK] 已自动打开浏览器")
    except Exception as e:
        print(f"  [!] 无法自动打开浏览器: {e}")
        print(f"  [!] 请手动访问: {url}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在关闭服务器...")
        # 终止正在运行的任务
        if _current_process:
            kill_process_tree(_current_process.pid)
        httpd.server_close()
        print("服务器已停止")


if __name__ == "__main__":
    main()
