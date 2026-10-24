#!/usr/bin/env python3
"""
Threads／TikTok 影片與圖片下載 Web 服務

啟動：
    pip install -r requirements.txt
    playwright install chromium        # 選用，作為備援
    python app.py                      # 開啟 http://localhost:8000
"""

import os
import re
from urllib.parse import urlparse, quote

import requests
from flask import Flask, Response, jsonify, request, abort, send_file

from threads_dl import HEADERS, extract, filenames, find_threads_url
from tiktok_dl import (TikTokError, file_mimetype, find_tiktok_url,
                       tiktok_file, tiktok_info)
from tiktok_dl import _image_ext as tiktok_image_ext

app = Flask(__name__)

# 選用：設定環境變數 APP_PASSWORD 後，開啟網頁需輸入密碼（帳號任意）
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.before_request
def require_password():
    if not APP_PASSWORD or request.path == "/healthz":
        return None
    auth = request.authorization
    if auth and auth.password == APP_PASSWORD:
        return None
    return Response("需要密碼", 401,
                    {"WWW-Authenticate": 'Basic realm="Media Downloader"'})


@app.get("/healthz")
def healthz():
    return "ok"

# 只允許代理 Meta／TikTok 的媒體 CDN，避免服務被當成開放代理（SSRF）
ALLOWED_CDN = re.compile(
    r"(^|\.)(fbcdn\.net|cdninstagram\.com|"
    r"tiktokcdn\.com|tiktokcdn-us\.com|tiktokcdn-eu\.com|ibyteimg\.com)$")
TIKTOK_CDN = re.compile(r"(^|\.)(tiktokcdn(-us|-eu)?\.com|ibyteimg\.com)$")


def is_allowed_cdn(u: str) -> bool:
    p = urlparse(u)
    return p.scheme == "https" and bool(ALLOWED_CDN.search(p.hostname or ""))


@app.post("/api/extract")
def api_extract():
    raw = (request.get_json(silent=True) or {}).get("url", "")

    tt_url = find_tiktok_url(raw)
    if tt_url:
        return extract_tiktok(tt_url)

    url = find_threads_url(raw)
    if not url:
        return jsonify(error="找不到 Threads 或 TikTok 連結，請貼上貼文或分享連結"), 400

    code, media = extract(url)
    media = [m for m in media if is_allowed_cdn(m["url"])]
    if not media:
        return jsonify(error="找不到影片或圖片（可能是純文字、私人帳號或需要登入）"), 404

    items = []
    for m, name in zip(media, filenames(code, media)):
        u = quote(m["url"], safe="")
        items.append({
            "type": m["type"],
            "name": name,
            "preview": f"/api/download?u={u}&inline=1",
            "download": f"/api/download?u={u}&name={quote(name)}",
        })
    return jsonify(items=items)


def extract_tiktok(url: str):
    try:
        info = tiktok_info(url)
    except TikTokError as e:
        return jsonify(error=str(e)), 404
    vid, src = info["id"], quote(info["url"], safe="")
    base = f"tiktok_{vid}"

    if info["kind"] == "video":
        name = f"{base}.mp4"
        return jsonify(items=[{
            "type": "video",
            "name": name,
            "title": info["title"],
            "preview": f"/api/tiktok?id={vid}&u={src}&inline=1",
            "download": f"/api/tiktok?id={vid}&u={src}&name={quote(name)}",
        }])

    # 圖片輪播：每張圖片＋背景音樂
    images = [u for u in info["images"] if is_allowed_cdn(u)]
    items = []
    for i, img in enumerate(images, 1):
        name = f"{base}_{i}{tiktok_image_ext(img)}"
        u = quote(img, safe="")
        items.append({
            "type": "image",
            "name": name,
            "preview": f"/api/download?u={u}&inline=1",
            "download": f"/api/download?u={u}&name={quote(name)}",
        })
    if info["has_audio"]:
        name = f"{base}_music"
        items.append({
            "type": "audio",
            "name": "背景音樂",
            "preview": f"/api/tiktok?id={vid}&u={src}&inline=1",
            "download": f"/api/tiktok?id={vid}&u={src}&name={quote(name)}",
        })
    if not items:
        return jsonify(error="找不到這則貼文的圖片"), 404
    items[0]["title"] = info["title"]
    return jsonify(items=items)


@app.get("/api/tiktok")
def api_tiktok():
    vid = request.args.get("id", "")
    url = find_tiktok_url(request.args.get("u", ""))
    if not url:
        abort(400)
    try:
        path = tiktok_file(vid, url)
    except TikTokError as e:
        return Response(str(e), 502, {"Content-Type": "text/plain; charset=utf-8"})
    name = re.sub(r"[^\w.-]", "_", request.args.get("name", f"tiktok_{vid}"))
    ext = os.path.splitext(path)[1]
    if not name.endswith(ext):
        name += ext
    # send_file 會處理 Range，預覽可以拖曳
    return send_file(path, mimetype=file_mimetype(path), conditional=True,
                     as_attachment=not request.args.get("inline"),
                     download_name=name)


@app.get("/api/download")
def api_download():
    u = request.args.get("u", "")
    if not is_allowed_cdn(u):
        abort(400)
    name = re.sub(r"[^\w.-]", "_", request.args.get("name", "threads_media"))

    fwd = dict(HEADERS)
    if TIKTOK_CDN.search(urlparse(u).hostname or ""):
        fwd["Referer"] = "https://www.tiktok.com/"
    if "Range" in request.headers:          # 讓預覽播放器可以拖曳
        fwd["Range"] = request.headers["Range"]
    upstream = requests.get(u, headers=fwd, stream=True, timeout=60)

    headers = {"Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
               "Accept-Ranges": "bytes"}
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            headers[h] = upstream.headers[h]
    if not request.args.get("inline"):
        headers["Content-Disposition"] = f'attachment; filename="{name}"'

    return Response(upstream.iter_content(1 << 16),
                    status=upstream.status_code, headers=headers)


@app.get("/")
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Threads／TikTok 下載</title>
<style>
  :root { --bg:#fafafa; --fg:#111; --muted:#777; --card:#fff; --line:#e5e5e5; --accent:#111; --accent-fg:#fff; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#101010; --fg:#f3f3f3; --muted:#999; --card:#1a1a1a; --line:#2c2c2c; --accent:#f3f3f3; --accent-fg:#111; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:16px/1.5 -apple-system, "PingFang TC", "Noto Sans TC", sans-serif; }
  main { max-width:640px; margin:0 auto; padding:48px 16px; }
  h1 { font-size:26px; margin:0 0 4px; }
  p.sub { color:var(--muted); margin:0 0 24px; }
  form { display:flex; gap:8px; }
  input { flex:1; min-width:0; padding:12px 14px; font-size:16px; border:1px solid var(--line);
          border-radius:12px; background:var(--card); color:var(--fg); }
  button, a.btn { padding:12px 18px; font-size:15px; border:0; border-radius:12px; cursor:pointer;
          background:var(--accent); color:var(--accent-fg); text-decoration:none; white-space:nowrap; }
  button:disabled { opacity:.5; cursor:wait; }
  #msg { margin:16px 0; color:var(--muted); min-height:1.5em; white-space:pre-line; word-break:break-all; }
  #msg.err { color:#d33; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px;
          padding:12px; margin-bottom:16px; }
  audio { width:100%; display:block; }
  video, img.m { width:100%; display:block; object-fit:contain; border-radius:10px; background:#000; max-height:70vh; }
  .row { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:10px; }
  .title { font-size:14px; margin:10px 0 0; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
  .name { color:var(--muted); font-size:14px; overflow:hidden; text-overflow:ellipsis; }
</style>
</head>
<body>
<main>
  <h1>Threads／TikTok 下載</h1>
  <p class="sub">貼上 Threads 或 TikTok 的貼文／分享連結，下載影片與圖片。</p>
  <form id="f">
    <input id="u" type="text" required autocomplete="off" placeholder="貼上 Threads 或 TikTok 連結">
    <button id="b">抓取</button>
  </form>
  <div id="msg"></div>
  <div id="out"></div>
</main>
<script>
const f = document.getElementById('f'), u = document.getElementById('u'),
      b = document.getElementById('b'), msg = document.getElementById('msg'),
      out = document.getElementById('out');

f.addEventListener('submit', async e => {
  e.preventDefault();
  b.disabled = true; out.innerHTML = '';
  msg.className = ''; msg.textContent = '解析中，可能需要十幾秒…';
  try {
    const r = await fetch('/api/extract', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: u.value})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '發生錯誤');
    const cnt = t => d.items.filter(x => x.type === t).length;
    const nv = cnt('video'), ni = cnt('image'), na = cnt('audio');
    msg.textContent = '找到 ' + [nv && `${nv} 支影片`, ni && `${ni} 張圖片`, na && '背景音樂']
      .filter(Boolean).join('、');
    for (const v of d.items) {
      const c = document.createElement('div'); c.className = 'card';
      const media = v.type === 'video'
        ? '<video controls playsinline preload="metadata"></video>'
        : v.type === 'audio'
        ? '<audio controls preload="none"></audio>'
        : '<img class="m" alt="" loading="lazy">';
      c.innerHTML = media + `<div class="row"><span class="name"></span><a class="btn">下載</a></div>`;
      const el = c.querySelector('video, audio, img');
      el.src = v.preview;
      // 伺服器下載或轉檔失敗時，把錯誤訊息（含診斷）顯示出來
      el.addEventListener('error', async () => {
        try {
          const r = await fetch(v.preview, {headers: {Range: 'bytes=0-0'}});
          if (!r.ok) { msg.className = 'err'; msg.textContent = await r.text(); }
        } catch (_) {}
      });
      if (v.type === 'video' && v.preview.startsWith('/api/tiktok')) {
        msg.textContent += '（TikTok 影片需要伺服器先下載處理，預覽可能要等一下）';
      }
      if (v.title) {
        const t = document.createElement('p'); t.className = 'title'; t.textContent = v.title;
        c.insertBefore(t, c.querySelector('.row'));
      }
      c.querySelector('.name').textContent = v.name;
      c.querySelector('a').href = v.download;
      out.appendChild(c);
    }
  } catch (err) {
    msg.className = 'err'; msg.textContent = err.message;
  } finally { b.disabled = false; }
});
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
