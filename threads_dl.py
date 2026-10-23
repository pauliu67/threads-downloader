#!/usr/bin/env python3
"""
Threads 影片／圖片下載器

支援的連結格式：
    https://www.threads.com/@帳號/post/貼文代碼
    https://www.threads.com/share/短代碼/          ← 分享短連結
    https://www.threads.com/t/貼文代碼
    （threads.net 亦可；也可以直接貼上含連結的整段分享文字）

用法：
    python threads_dl.py <連結> [<連結> ...] [-o 輸出資料夾]

安裝：
    pip install requests
    pip install playwright && playwright install chromium   # 選用，備援

做法：
    1. 先把短連結解析成實際貼文網址
    2. 從頁面內嵌的 JSON 找出「這一則」貼文的媒體（影片、圖片、輪播）
    3. 抓不到時改用 Playwright 開啟頁面再解析一次，並攔截影片請求
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

# 任何 Threads 貼文／分享連結
THREADS_URL_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:net|com)/"
    r"(?:@[^/\s?#]+/post/|t/|share/)[A-Za-z0-9_-]+/?(?:\?[^\s#]*)?",
    re.I,
)
# 標準貼文網址（可取出貼文代碼）
POST_RE = re.compile(r"threads\.(?:net|com)/(?:@[^/]+/post|t)/([A-Za-z0-9_-]+)", re.I)


# ---------- 工具函式 ----------

def find_threads_url(text: str):
    """從輸入（可能是整段分享文字）找出 Threads 連結。"""
    m = THREADS_URL_RE.search(text or "")
    return m.group(0) if m else None


def post_code(url: str):
    m = POST_RE.search(url or "")
    return m.group(1) if m else None


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


def ext_of(item) -> str:
    ext = os.path.splitext(urlparse(item["url"]).path)[1].lower()
    if ext in (".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".heic"):
        return ext
    return ".mp4" if item["type"] == "video" else ".jpg"


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = urlparse(it["url"]).path
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


# ---------- 解析頁面 ----------

def _json_blobs(html: str):
    for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
                         html, re.S):
        try:
            yield json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue


def _walk(obj):
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            yield x
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def _has_media(node: dict) -> bool:
    return any(node.get(k) for k in ("video_versions", "image_versions2", "carousel_media"))


def _media_from_node(node: dict):
    """把一個貼文（或輪播中的一項）轉成媒體清單。"""
    carousel = node.get("carousel_media")
    if carousel:
        items = []
        for child in carousel:
            if isinstance(child, dict):
                items += _media_from_node(child)
        return items

    vids = [v for v in (node.get("video_versions") or []) if v.get("url")]
    if vids:
        best = max(vids, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))
        return [{"type": "video", "url": best["url"]}]

    cands = [c for c in ((node.get("image_versions2") or {}).get("candidates") or [])
             if c.get("url")]
    if cands:
        best = max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
        return [{"type": "image", "url": best["url"]}]
    return []


def parse_media(html: str, code=None):
    """從頁面 HTML 取出指定貼文的媒體。"""
    # 1) 內嵌 JSON：找 code 相符的貼文節點，避免抓到留言或推薦貼文
    fallback = None
    for blob in _json_blobs(html):
        for node in _walk(blob):
            if not (isinstance(node.get("code"), str) and _has_media(node)):
                continue
            if code and node["code"] == code:
                items = _media_from_node(node)
                if items:
                    return dedupe(items)
            if fallback is None:
                fallback = node
    if fallback is not None and not code:
        items = _media_from_node(fallback)
        if items:
            return dedupe(items)

    # 2) 舊式寫法：直接比對 video_versions 字串
    items = []
    for block in re.finditer(r'"video_versions"\s*:\s*\[(.*?)\]', html, re.S):
        found = re.findall(r'"url"\s*:\s*"([^"]+)"', block.group(1))
        if found:
            items.append({"type": "video", "url": unescape_json_str(found[0])})
    if items:
        return dedupe(items)

    # 3) og meta（通常只有第一張圖或第一支影片）
    for prop, kind in (("og:video", "video"), ("og:image", "image")):
        m = re.search(rf'<meta[^>]+property="{prop}(?::secure_url)?"[^>]+content="([^"]+)"', html)
        if m:
            return [{"type": kind, "url": m.group(1).replace("&amp;", "&")}]
    return []


def _canonical_from_html(html: str):
    for pat in (r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"',
                r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"'):
        m = re.search(pat, html)
        if m and POST_RE.search(m.group(1)):
            return m.group(1).replace("&amp;", "&")
    return None


# ---------- 方法一：一般 HTTP ----------

def extract_static(url: str):
    """回傳 (貼文代碼, 媒體清單)。"""
    r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    r.raise_for_status()
    final, html = r.url, r.text

    code = post_code(final)
    if not code:                         # 短連結沒有直接轉址時，從頁面找正式網址
        canonical = _canonical_from_html(html)
        if canonical:
            code = post_code(canonical)
            if urlparse(canonical).path != urlparse(final).path:
                r = requests.get(canonical, headers=HEADERS, timeout=20)
                r.raise_for_status()
                html = r.text
    return code, parse_media(html, code)


# ---------- 方法二：Playwright 備援 ----------

def extract_browser(url: str):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ! 未安裝 Playwright，略過瀏覽器備援。")
        return None, []

    captured = []

    def on_response(resp):
        ct = resp.headers.get("content-type", "")
        if ".mp4" in resp.url or ct.startswith("video/"):
            captured.append({"type": "video", "url": clean_cdn_url(resp.url)})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)
        page.on("response", on_response)
        page.goto(url, wait_until="networkidle", timeout=45000)

        code = post_code(page.url)
        html = page.content()
        if not code:
            canonical = _canonical_from_html(html)
            code = post_code(canonical) if canonical else None

        items = parse_media(html, code)
        if not items:
            try:
                page.eval_on_selector_all(
                    "video", "els => els.forEach(v => { v.muted = true; v.play(); })")
                page.wait_for_timeout(3000)
            except Exception:
                pass
            items = captured
        browser.close()

    return code, dedupe(items)


def extract(url: str):
    """先靜態解析，失敗再用瀏覽器。回傳 (貼文代碼, 媒體清單)。"""
    code, items = None, []
    try:
        code, items = extract_static(url)
    except requests.RequestException as e:
        print(f"  ! 靜態抓取失敗：{e}")
    if not items:
        try:
            c2, items = extract_browser(url)
            code = code or c2
        except Exception as e:
            print(f"  ! 瀏覽器模式失敗：{e}")
            items = []
    return code or "threads", items


def filenames(code: str, items):
    multi = len(items) > 1
    return [f"{code}{f'_{i}' if multi else ''}{ext_of(it)}"
            for i, it in enumerate(items, 1)]


# ---------- 下載（命令列） ----------

def download(media_url: str, path: str):
    with requests.get(media_url, headers=HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  下載中 {done * 100 // total:3d}%", end="", flush=True)
    print(f"\r  ✓ 已儲存：{path}" + " " * 20)


def process(raw: str, outdir: str) -> bool:
    url = find_threads_url(raw)
    if not url:
        print(f"✗ 不是 Threads 連結：{raw}")
        return False
    print(f"▶ {url}")
    code, items = extract(url)
    if not items:
        print("  ✗ 找不到影片或圖片（可能是純文字、私人帳號，或需要登入）")
        return False
    for it, name in zip(items, filenames(code, items)):
        try:
            download(it["url"], os.path.join(outdir, name))
        except requests.RequestException as e:
            print(f"\n  ✗ 下載失敗：{e}")
    return True


def main():
    ap = argparse.ArgumentParser(description="下載 Threads 貼文中的影片與圖片")
    ap.add_argument("urls", nargs="+", help="Threads 貼文或分享連結")
    ap.add_argument("-o", "--output", default=".", help="輸出資料夾（預設目前目錄）")
    args = ap.parse_args()

    os.makedirs(args.output, exist_ok=True)
    ok = sum(process(u, args.output) for u in args.urls)
    print(f"\n完成：{ok}/{len(args.urls)} 則貼文成功")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
