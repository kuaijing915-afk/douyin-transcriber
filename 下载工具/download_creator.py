"""从浏览器自然返回的博主作品列表下载视频。"""

import argparse
from collections import deque
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, unquote, urlsplit

import certifi
from curl_cffi import requests


BASE_DIR = Path(__file__).resolve().parents[1]
COOKIE_PATH = BASE_DIR / ".local" / "credentials" / "cookie_like.txt"
PLAYWRIGHT_SITE_PACKAGES = str(Path(sys.prefix) / "Lib" / "site-packages")
PAGE_TIMEOUT_SECONDS = 30


def configure_direct_network():
    for name in list(os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def ca_bundle():
    candidate = Path(sys.base_prefix) / "Lib" / "site-packages" / "certifi" / "cacert.pem"
    return str(candidate) if candidate.is_file() else certifi.where()


def creator_uid(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (host == "douyin.com" or host.endswith(".douyin.com")):
        return None
    match = re.match(r"^/user/([^/]+)", parsed.path)
    return unquote(match.group(1)) if match else None


def parse_creator_page(data, target_uid):
    if not isinstance(data, dict) or data.get("status_code") not in (None, 0):
        raise ValueError("作品列表接口未返回成功数据")
    items = data.get("aweme_list")
    if not isinstance(items, list):
        raise ValueError("作品列表缺少 aweme_list")
    videos = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("author"), dict) or item["author"].get("sec_uid") != target_uid:
            raise ValueError("作品作者与目标博主不匹配")
        video_id = str(item.get("aweme_id", ""))
        if not re.fullmatch(r"[0-9]{1,32}", video_id):
            raise ValueError("作品 ID 无效")
        videos.append({"id": video_id, "title": str(item.get("desc") or ""), "video": item.get("video") or {}})
    return {"videos": videos, "has_more": data.get("has_more"), "cursor": data.get("max_cursor")}


def video_filename(video):
    if not re.fullmatch(r"[0-9]{1,32}", str(video["id"])):
        raise ValueError("作品 ID 无效")
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", video["title"])
    title = re.sub(r"\s+", " ", title).strip(" ._")[:60].rstrip(" .") or "视频"
    return f"{video['id']}_{title}.mp4"


def play_url(video):
    data = video["video"]
    addresses = [data.get("play_addr") or {}]
    addresses += [entry.get("play_addr") or {} for entry in data.get("bit_rate") or []]
    for address in addresses:
        for url in address.get("url_list") or []:
            if not isinstance(url, str):
                continue
            parsed = urlsplit(url)
            if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
                return url
    raise ValueError(f"作品 {video['id']} 没有有效视频地址")


def download_video(video, output_dir):
    target = output_dir / video_filename(video)
    if target.is_file() and target.stat().st_size > 50000:
        return target
    try:
        response = requests.get(
            play_url(video), headers={"Referer": "https://www.douyin.com/"},
            impersonate="edge101", proxy="", verify=ca_bundle(), timeout=120,
        )
    except Exception as exc:
        raise RuntimeError(f"作品 {video['id']} 请求失败（{type(exc).__name__}）") from None
    partial = target.with_suffix(".mp4.part")
    try:
        if response.status_code not in (200, 206):
            raise RuntimeError(f"作品 {video['id']} 下载 HTTP {response.status_code}")
        if len(response.content) <= 50000:
            raise RuntimeError(f"作品 {video['id']} 返回的视频数据过小")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type and not (content_type.startswith("video/") or content_type == "application/octet-stream"):
            raise RuntimeError(f"作品 {video['id']} 返回的不是视频内容")
        partial.write_bytes(response.content)
        partial.replace(target)
        return target
    finally:
        response.close()
        partial.unlink(missing_ok=True)


def download_pages(pages, output_dir, max_count):
    seen = set()
    completed = 0
    for page in pages:
        fresh = []
        for video in page["videos"]:
            if video["id"] not in seen:
                seen.add(video["id"])
                fresh.append(video)
        if max_count > 0:
            fresh = fresh[:max_count - completed]
        for video in fresh:
            path = download_video(video, output_dir)
            completed += 1
            print(f"[OK] 已获得 {completed} 个视频：{path.name}", flush=True)
        if max_count > 0 and completed >= max_count:
            return completed
        if page["has_more"] in (False, 0):
            return completed
        if not fresh:
            raise RuntimeError("作品分页没有新增视频，整批尚未完成")
    raise RuntimeError("作品列表提前结束，尚未收到完整结束标志")


def scroll_next_page(page):
    container = page.locator("div.parent-route-container.route-scroll-container")
    container.hover(timeout=5000)
    page.mouse.wheel(0, 2160)


def browser_pages(url, cookie):
    if PLAYWRIGHT_SITE_PACKAGES not in sys.path:
        sys.path.append(PLAYWRIGHT_SITE_PACKAGES)
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

    target_uid = creator_uid(url)
    parsed = urlsplit(url)
    if not target_uid and not (parsed.scheme in ("http", "https") and parsed.hostname == "v.douyin.com"):
        raise ValueError("请输入有效的抖音博主主页链接")
    cookie_map = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
    pending = deque()
    seen_pages = set()
    last_error = "尚未收到目标博主作品列表"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
        try:
            context = browser.new_context(locale="zh-CN")
            context.add_cookies([{"name": name, "value": value, "domain": ".douyin.com", "path": "/"} for name, value in cookie_map.items()])
            page = context.new_page()

            def on_response(response):
                nonlocal target_uid, last_error
                endpoint = urlsplit(response.url)
                host = (endpoint.hostname or "").lower()
                if endpoint.path != "/aweme/v1/web/aweme/post/" or not (host == "douyin.com" or host.endswith(".douyin.com")):
                    return
                target_uid = target_uid or creator_uid(page.url)
                if not target_uid or parse_qs(endpoint.query).get("sec_user_id") != [target_uid]:
                    return
                if response.status != 200:
                    last_error = f"作品列表 HTTP {response.status}"
                    return
                try:
                    body = response.body()
                    if not body:
                        last_error = "作品列表 HTTP 200，但正文为空"
                        return
                    result = parse_creator_page(json.loads(body), target_uid)
                    key = (str(result["cursor"]), str(result["has_more"]), tuple(video["id"] for video in result["videos"]))
                    if key not in seen_pages:
                        seen_pages.add(key)
                        pending.append(result)
                except ValueError as exc:
                    last_error = str(exc) if not isinstance(exc, json.JSONDecodeError) else "作品列表不是有效 JSON"
                except Exception as exc:
                    last_error = f"读取作品列表失败（{type(exc).__name__}）"

            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_SECONDS * 1000)
            except PlaywrightTimeoutError:
                pass
            while True:
                deadline = time.monotonic() + PAGE_TIMEOUT_SECONDS
                while not pending and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                if not pending:
                    raise RuntimeError(f"等待作品列表超时：{last_error}")
                result = pending.popleft()
                yield result
                if result["has_more"] in (False, 0):
                    return
                if not pending:
                    scroll_next_page(page)
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=0)
    args = parser.parse_args()
    if args.count < 0:
        parser.error("--count 不能为负数")
    configure_direct_network()
    try:
        cookie = COOKIE_PATH.read_text(encoding="utf-8").strip()
        if not cookie:
            raise ValueError("Cookie 为空")
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        print("下载网络：不使用 HTTP 代理；系统 TUN 按现有分流规则处理", flush=True)
        with closing(browser_pages(args.url, cookie)) as pages:
            count = download_pages(pages, output_dir, args.count)
        if count == 0:
            raise RuntimeError("目标博主没有可下载视频")
        print(f"[OK] 博主视频下载完成，共 {count} 个", flush=True)
        return 0
    except Exception as exc:
        # 库异常可能包含签名 URL；只输出本脚本生成的错误或异常类型。
        detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print(f"[FAIL] {detail}；已下载文件已保留", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
