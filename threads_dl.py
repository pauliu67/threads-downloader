#!/usr/bin/env python3
"""
Threads 影片下載器

用法：
    python threads_dl.py <貼文網址> [<貼文網址> ...] [-o 輸出資料夾]

範例：
    python threads_dl.py https://www.threads.com/@someone/post/ABC123xyz
    python threads_dl.py URL1 URL2 -o videos

安裝：
    pip install requests
    # 選用（靜態解析失敗時的備援，建議安裝）：
    pip install playwright && playwright install chromium

說明：
    1. 先用一般 HTTP 請求抓頁面，從內嵌 JSON（video_versions）或 og:video 取得影片網址
    2. 若抓不到（例如頁面改版或需要 JS 渲染），改用 Playwright 開啟頁面並攔截影片請求
    3. 一則貼文有多支影片（輪播）時會全部下載
請僅下載你有權使用的內容，並遵守 Threads 服務條款。
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}

POST_RE = re.compile(r"threads\.(?:net|com)/@[^/]+/post/([A-Za-z0-9_-]+)")


# ---------- 工具函式 ----------

def post_code(url: str) -> str:
    m = POST_RE.search(url)
    return m.group(1) if m else "threads_video"


def unescape_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return s.replace("\\/", "/").replace("\\u0026", "&")


def clean_cdn_url(u: str) -> str:
    """移除 bytestart/byteend 參數，讓 CDN 回傳完整檔案。"""
    p = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k not in ("bytestart", "byteend")]
    return urlunparse(p._replace(query=urlencode(q)))


def dedupe(urls):
    seen, out = set(), []
    for u in urls:
        key = urlparse(u).path  # 同一支影片路徑相同，參數可能不同
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


# ---------- 方法一：靜態解析 ----------

def extract_static(url: str):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    html = r.text
    urls = []

    # 內嵌 JSON：每個 video_versions 陣列取第一個（通常是最高畫質）
    for block in re.finditer(r'"video_versions"\s*:\s*\[(.*?)\]', html, re.S):
        found = re.findall(r'"url"\s*:\s*"([^"]+)"', block.group(1))
        if found:
            urls.append(unescape_json_str(found[0]))

    # og:video meta
    if not urls:
        for m in re.finditer(
            r'<meta[^>]+property="og:video(?::secure_url)?"[^>]+content="([^"]+)"', html
        ):
            urls.append(m.group(1).replace("&amp;", "&"))

    return dedupe(urls)


# ---------- 方法二：Playwright 備援 ----------

def extract_browser(url: str):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ! 未安裝 Playwright，略過瀏覽器備援。"
              "（pip install playwright && playwright install chromium）")
        return []

    captured = []

    def on_response(resp):
        ct = resp.headers.get("content-type", "")
        if ".mp4" in resp.url or ct.startswith("video/"):
            captured.append(clean_cdn_url(resp.url))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)
        page.on("response", on_response)
        page.goto(url, wait_until="networkidle", timeout=45000)

        # 嘗試觸發播放，讓影片請求出現
        try:
            page.eval_on_selector_all(
                "video", "els => els.forEach(v => { v.muted = true; v.play(); })"
            )
            page.wait_for_timeout(3000)
        except Exception:
            pass

        # 直接讀 <video> 的 src（非 blob 才有用）
        srcs = page.eval_on_selector_all(
            "video", "els => els.map(v => v.currentSrc || v.src).filter(Boolean)"
        )
        captured.extend(s for s in srcs if s.startswith("http"))

        # 頁面渲染後的 HTML 也再解析一次
        html = page.content()
        for block in re.finditer(r'"video_versions"\s*:\s*\[(.*?)\]', html, re.S):
            found = re.findall(r'"url"\s*:\s*"([^"]+)"', block.group(1))
            if found:
                captured.insert(0, unescape_json_str(found[0]))

        browser.close()

    return dedupe(captured)


# ---------- 下載 ----------

def download(video_url: str, path: str):
    with requests.get(video_url, headers=HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    print(f"\r  下載中 {pct:3d}%  ({done/1e6:.1f}/{total/1e6:.1f} MB)",
                          end="", flush=True)
    print(f"\r  ✓ 已儲存：{path}" + " " * 20)


def process(url: str, outdir: str) -> bool:
    print(f"▶ {url}")
    urls = []
    try:
        urls = extract_static(url)
    except requests.RequestException as e:
        print(f"  ! 靜態抓取失敗：{e}")

    if not urls:
        print("  靜態解析找不到影片，改用瀏覽器模式…")
        urls = extract_browser(url)

    if not urls:
        print("  ✗ 找不到影片（可能是純圖文貼文、私人帳號，或需要登入）")
        return False

    code = post_code(url)
    for i, vu in enumerate(urls, 1):
        suffix = f"_{i}" if len(urls) > 1 else ""
        path = os.path.join(outdir, f"{code}{suffix}.mp4")
        try:
            download(vu, path)
        except requests.RequestException as e:
            print(f"\n  ✗ 下載失敗：{e}")
    return True


def main():
    ap = argparse.ArgumentParser(description="下載 Threads 貼文中的影片")
    ap.add_argument("urls", nargs="+", help="Threads 貼文網址")
    ap.add_argument("-o", "--output", default=".", help="輸出資料夾（預設目前目錄）")
    args = ap.parse_args()

    os.makedirs(args.output, exist_ok=True)
    ok = sum(process(u, args.output) for u in args.urls)
    print(f"\n完成：{ok}/{len(args.urls)} 則貼文成功")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
