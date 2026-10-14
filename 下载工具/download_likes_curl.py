"""curl_cffi: 多线程下载点赞视频 (4并发)"""
import os, sys, io, time, concurrent.futures
from pathlib import Path
import certifi

for key in list(os.environ):
    if key.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        os.environ.pop(key, None)
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'

_base_ca_bundle = Path(sys.base_prefix) / 'Lib' / 'site-packages' / 'certifi' / 'cacert.pem'
CA_BUNDLE = str(_base_ca_bundle) if _base_ca_bundle.is_file() else certifi.where()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
from curl_cffi import requests

BASE = str(Path(__file__).resolve().parents[1] / "配置")
cookie = open(Path(__file__).resolve().parents[1] / '.local' / 'credentials' / 'cookie_like.txt', encoding='utf-8').read().strip()

# 优先从 sec_uid.txt 读取（pipeline 自动获取），回退到硬编码
_sec_uid_path = os.path.join(BASE, 'sec_uid.txt')
if os.path.exists(_sec_uid_path):
    sec_user_id = open(_sec_uid_path, encoding='utf-8').read().strip()
else:
    raise SystemExit("缺少 sec_uid，请从操作台获取当前账号后再下载。")
base_path = str(Path(__file__).resolve().parents[1] / "video and audio")
MAX_WORKERS = 4  # 4线程并发下载
os.makedirs(base_path, exist_ok=True)

headers = {
    'Cookie': cookie,
    'Referer': 'https://www.douyin.com/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}

params_base = {
    'device_platform': 'webapp', 'aid': '6383', 'channel': 'channel_pc_web',
    'pc_client_type': '1', 'version_code': '290100', 'version_name': '29.1.0',
    'cookie_enabled': 'true', 'screen_width': '1920', 'screen_height': '1080',
    'browser_language': 'zh-CN', 'browser_platform': 'Win32',
    'browser_name': 'Edge', 'browser_version': '130.0.0.0',
    'browser_online': 'true', 'engine_name': 'Blink', 'engine_version': '130.0.0.0',
    'os_name': 'Windows', 'os_version': '10', 'cpu_core_num': '12',
    'device_memory': '8', 'platform': 'PC', 'downlink': '10',
    'effective_type': '4g', 'round_trip_time': '100',
}

LIKE_LIST_URL = 'https://www.douyin.com/user/self?from_tab_name=main&showTab=like'
PLAYWRIGHT_SITE_PACKAGES = str(Path(sys.prefix) / "Lib" / "site-packages")


def parse_browser_cookies(cookie_header):
    cookie_map = {}
    for item in cookie_header.split(';'):
        item = item.strip()
        if not item or '=' not in item:
            continue
        name, value = item.split('=', 1)
        name = name.strip()
        if name:
            cookie_map[name] = value.strip()
    return [
        {'name': name, 'value': value, 'domain': '.douyin.com', 'path': '/'}
        for name, value in cookie_map.items()
    ]


def get_aweme_list_with_browser():
    if PLAYWRIGHT_SITE_PACKAGES not in sys.path:
        sys.path.append(PLAYWRIGHT_SITE_PACKAGES)
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception as exc:
        fail(f"无法加载 Playwright: {exc}")

    captured = {}
    deadline = time.monotonic() + 20
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
            try:
                context = browser.new_context(
                    user_agent=headers['User-Agent'],
                    viewport={'width': 1920, 'height': 1080},
                    locale='zh-CN',
                )
                context.add_cookies(parse_browser_cookies(cookie))
                page = context.new_page()

                def on_response(response):
                    if captured.get('seen'):
                        return
                    if '/aweme/v1/web/aweme/favorite/' not in response.url:
                        return
                    try:
                        data = response.json()
                    except Exception:
                        captured['last_error'] = (
                            f'HTTP {response.status}: '
                            + response.text()[:160].replace('\n', ' ').strip()
                        )
                        return
                    if response.status != 200:
                        captured['last_error'] = f'HTTP {response.status}'
                        return
                    if not isinstance(data, dict):
                        captured['last_error'] = '接口返回的不是 JSON 对象'
                        return
                    if data.get('status_code') not in (None, 0):
                        captured['last_error'] = (
                            f"status_code={data.get('status_code')}: "
                            f"{data.get('status_msg', '')}"
                        )
                        return
                    if not isinstance(data.get('aweme_list'), list):
                        captured['last_error'] = '接口缺少 aweme_list'
                        return
                    captured['status'] = response.status
                    captured['data'] = data
                    captured['seen'] = True

                page.on('response', on_response)
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                try:
                    page.goto(LIKE_LIST_URL, wait_until='domcontentloaded', timeout=remaining_ms)
                except PlaywrightTimeoutError:
                    pass
                while not captured.get('seen') and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
            finally:
                browser.close()
    except PlaywrightTimeoutError:
        fail('喜欢列表页面等待超时（20秒）')
    except Exception as exc:
        fail(f'喜欢列表页面请求失败: {exc}')

    if not captured.get('seen'):
        detail = captured.get('last_error', '')
        suffix = f'，最后一次响应: {detail}' if detail else ''
        fail(f'20秒内未监听到有效的喜欢列表接口响应{suffix}')
    if captured.get('status') != 200:
        detail = captured.get('error', '')
        fail(f"喜欢列表接口返回 HTTP {captured.get('status')}: {detail}")
    data = captured.get('data')
    if not isinstance(data, dict):
        fail('喜欢列表接口返回的不是 JSON')
    return data

def get_play_url(aweme):
    """从视频数据中提取 douyin play URL"""
    video_data = aweme.get('video', {})
    url_list = video_data.get('play_addr', {}).get('url_list', [])
    for url in reversed(url_list):
        if 'douyin.com/aweme/v1/play/' in url:
            return url
    if url_list:
        return url_list[0]
    bit_rate = video_data.get('bit_rate', [])
    if bit_rate:
        for br in bit_rate:
            for url in reversed(br.get('play_addr', {}).get('url_list', [])):
                if 'douyin.com/aweme/v1/play/' in url:
                    return url
    return None

def download_one(video_id, play_url, filename):
    """下载单个视频"""
    try:
        vresp = requests.get(
            play_url,
            headers={'Cookie': cookie, 'Referer': 'https://www.douyin.com/'},
            impersonate='edge101',
            timeout=300,
            proxy='',
            verify=CA_BUNDLE,
        )
        if not 200 <= vresp.status_code < 300:
            return f"err:HTTP {vresp.status_code}"
        if len(vresp.content) > 50000:
            with open(filename, 'wb') as f:
                f.write(vresp.content)
            return len(vresp.content)
        return 0
    except Exception as e:
        return f"err:{e}"

def fail(message: str, code: int = 1) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(code)


print("获取点赞列表...")
data = get_aweme_list_with_browser()

api_status = data.get('status_code')
if api_status not in (None, 0):
    fail(f"喜欢列表接口返回错误 status_code={api_status}: {data.get('status_msg', '')}")

aweme_list = data.get('aweme_list')
if not isinstance(aweme_list, list):
    fail("喜欢列表接口缺少 aweme_list")
if not aweme_list:
    fail("喜欢列表返回 0 条视频，已停止；请检查 Cookie 是否仍有效")

# 支持环境变量限制下载数量
max_count = int(os.environ.get("DOWNLOAD_MAX_COUNT", "0"))
if max_count > 0:
    aweme_list = aweme_list[:max_count]
print(f"找到 {len(aweme_list)} 个点赞视频{'（限' + str(max_count) + '个）' if max_count > 0 else ''}\n")

# 构建下载任务列表
tasks = []
existing_count = 0
for i, aweme in enumerate(aweme_list):
    video_id = aweme.get('aweme_id', 'unknown')
    desc = (aweme.get('desc', '') or '')[:60]
    filename = os.path.join(base_path, f"like_{video_id}.mp4")

    if os.path.exists(filename) and os.path.getsize(filename) > 50000:
        size_mb = os.path.getsize(filename) / 1024 / 1024
        print(f"[{i+1}/{len(aweme_list)}] [OK] 已存在 ({size_mb:.0f}MB): {desc}")
        existing_count += 1
        continue

    play_url = get_play_url(aweme)
    if not play_url:
        print(f"[{i+1}/{len(aweme_list)}] [FAIL] 无视频URL: {desc}")
        continue

    tasks.append((i+1, video_id, play_url, filename, desc))

if not tasks:
    if existing_count != len(aweme_list):
        fail(f"喜欢视频下载未完成：可用 {existing_count}/{len(aweme_list)}，其余条目无视频URL")
    print("全部已下载，无需操作")
    sys.exit(0)

print(f"需要下载: {len(tasks)} 个 (并发 {MAX_WORKERS} 线程)\n")

# 多线程下载
success = 0
t0 = time.time()
with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    fut_map = {}
    for idx, vid, url, fpath, desc in tasks:
        fut = executor.submit(download_one, vid, url, fpath)
        fut_map[fut] = (idx, desc)

    done = 0
    for fut in concurrent.futures.as_completed(fut_map):
        idx, desc = fut_map[fut]
        result = fut.result()
        done += 1
        if isinstance(result, int) and result > 50000:
            size_mb = result / 1024 / 1024
            print(f"[{idx}/{len(aweme_list)}] OK {size_mb:.1f}MB: {desc}")
            success += 1
        elif isinstance(result, int):
            print(f"[{idx}/{len(aweme_list)}] [WARN] 太小 ({result}b): {desc}")
        else:
            print(f"[{idx}/{len(aweme_list)}] [FAIL] {result}: {desc}")

elapsed = time.time() - t0
print(f"\n下载结果: 已存在 {existing_count}，新下载 {success}/{len(tasks)}，耗时: {elapsed:.0f}s")
if existing_count + success != len(aweme_list):
    fail(f"喜欢视频下载未完成：可用 {existing_count + success}/{len(aweme_list)}")
