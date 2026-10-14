"""Collect comments visible in a Douyin post's web comment list.

Usage: python collect_comments.py URL OUTPUT.jsonl [--max-comments 1000]
The first run opens Chrome so the user can sign in if Douyin asks.
"""

import argparse
import json
import os
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def read_comments(page):
    return page.evaluate("""() => Array.from(document.querySelectorAll('[data-e2e="comment-item"]')).map(item => ({
        id: item.querySelector('[data-e2e="video-comment-more"]')?.firstElementChild?.id?.replace(/^tooltip_/, '') || '',
        author: item.querySelector('a[href*="/user/"]')?.textContent?.trim() || '',
        text: item.querySelector('.FduGc_lz')?.innerText?.trim() || '',
        likes: item.querySelector('.comment-item-stats-container p span')?.textContent?.trim() || '',
        time: item.querySelector('.VAQA49VP')?.textContent?.trim() || ''
    }))""")


def open_comments(page):
    tab = page.get_by_text(re.compile(r"^评论\(\d+\)$")).first
    while True:
        tab.wait_for(state="visible", timeout=30000)
        blocked = False
        try:
            tab.click(timeout=5000)
        except PlaywrightTimeoutError:
            blocked = True
        if not blocked:
            try:
                page.locator('[data-e2e="comment-item"]:visible').first.wait_for(timeout=8000)
                blocked = page.get_by_text("登录后可查看更多评论").is_visible()
            except PlaywrightTimeoutError:
                blocked = True
        if not blocked:
            return
        print("评论尚不可见。请在 Chrome 窗口完成登录，完成后回到终端按回车；窗口会保持打开。", flush=True)
        input()
        page.reload(wait_until="domcontentloaded")


def scroll_comments(page):
    pane = page.locator('[data-e2e="comment-list"]').filter(visible=True).last
    box = pane.bounding_box()
    if box is None:
        return None
    before = pane.evaluate("item => item.scrollTop")
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 1600)
    return pane.evaluate("item => ({after: item.scrollTop, height: item.scrollHeight, view: item.clientHeight})") | {"before": before}


def main():
    parser = argparse.ArgumentParser(description="采集抖音作品网页评论")
    parser.add_argument("url", help="抖音作品或分享链接")
    parser.add_argument("output", type=Path, help="保存为 UTF-8 JSONL 的路径")
    parser.add_argument("--max-comments", type=int, default=0, help="最多采集多少条；0 表示翻到末尾")
    args = parser.parse_args()
    if args.max_comments < 0:
        parser.error("--max-comments 不能为负数")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    profile = Path(os.environ["LOCALAPPDATA"]) / "douyin-comment-crawler-profile"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False, locale="zh-CN"
        )
        try:
            page = browser.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            open_comments(page)
            seen = set()
            if args.output.exists():
                for line in args.output.read_text(encoding="utf-8").splitlines():
                    comment = json.loads(line)
                    seen.add(comment["id"] or comment["author"] + "|" + comment["text"])
                print(f"从已有的 {len(seen)} 条评论继续", flush=True)
            stalled = 0
            with args.output.open("a", encoding="utf-8") as output:
                while not args.max_comments or len(seen) < args.max_comments:
                    added = 0
                    for comment in read_comments(page):
                        if not comment["text"]:
                            continue
                        key = comment["id"] or comment["author"] + "|" + comment["text"]
                        if key in seen:
                            continue
                        seen.add(key)
                        output.write(json.dumps(comment, ensure_ascii=False) + "\n")
                        added += 1
                        if args.max_comments and len(seen) >= args.max_comments:
                            break
                    output.flush()
                    if args.max_comments and len(seen) >= args.max_comments:
                        break
                    before_count = page.locator('[data-e2e="comment-item"]').count()
                    position = scroll_comments(page)
                    if position is None:
                        raise RuntimeError("没有找到可滚动的评论列表。")
                    at_end = position["after"] + position["view"] >= position["height"] - 2
                    stalled = stalled + 1 if at_end and not added else 0
                    if stalled >= 5:
                        print("连续 5 次滚动没有加载新评论，停止采集。", flush=True)
                        break
                    if at_end:
                        try:
                            page.wait_for_function(
                                "previous => document.querySelectorAll('[data-e2e=\"comment-item\"]').length > previous",
                                arg=before_count, timeout=5000,
                            )
                        except PlaywrightTimeoutError:
                            pass
                    if len(seen) and len(seen) % 100 < added:
                        print(f"已保存 {len(seen)} 条评论")
            print(f"本次已保存 {len(seen)} 条评论：{args.output}")
        except Exception as error:
            print(f"采集遇到 {type(error).__name__}，Chrome 保持打开供检查。", flush=True)
            try:
                input("检查完页面后按回车关闭 Chrome。")
            except EOFError:
                pass
            raise
        else:
            try:
                input("采集已停止，Chrome 保持打开；按回车关闭。")
            except EOFError:
                pass
        finally:
            browser.close()


if __name__ == "__main__":
    main()
