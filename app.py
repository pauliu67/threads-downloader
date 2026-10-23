#!/usr/bin/env python3
"""
Threads 影片／圖片下載 Web 服務

啟動：
    pip install -r requirements.txt
    playwright install chromium        # 選用，作為備援
    python app.py                      # 開啟 http://localhost:8000
"""

import os
import re
from urllib.parse import urlparse, quote

import requests
from flask import Flask, Response, jsonify, request, abort

from threads_dl import HEADERS, extract, filenames, find_threads_url

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
                    {"WWW-Authenticate": 'Basic realm="Threads Downloader"'})


@app.get("/healthz")
def healthz():
    return "ok"

# 只允許代理 Meta 的影片 CDN，避免服務被當成開放代理（SSRF）
ALLOWED_CDN = re.compile(r"(^|\.)(fbcdn\.net|cdninstagram\.com)$")


def is_allowed_cdn(u: str) -> bool:
    p = urlparse(u)
    return p.scheme == "https" and bool(ALLOWED_CDN.search(p.hostname or ""))


@app.post("/api/extract")
def api_extract():
    raw = (request.get_json(silent=True) or {}).get("url", "")
    url = find_threads_url(raw)
    if not url:
        return jsonify(error="找不到 Threads 連結，請貼上貼文或分享連結"), 400

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


@app.get("/api/download")
def api_download():
    u = request.args.get("u", "")
    if not is_allowed_cdn(u):
        abort(400)
    name = re.sub(r"[^\w.-]", "_", request.args.get("name", "threads_media"))

    fwd = dict(HEADERS)
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
<title>Threads 媒體下載</title>
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
  #msg { margin:16px 0; color:var(--muted); min-height:1.5em; }
  #msg.err { color:#d33; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px;
          padding:12px; margin-bottom:16px; }
  video, img.m { width:100%; display:block; object-fit:contain; border-radius:10px; background:#000; max-height:70vh; }
  .row { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:10px; }
  .name { color:var(--muted); font-size:14px; overflow:hidden; text-overflow:ellipsis; }
</style>
</head>
<body>
<main>
  <h1>Threads 媒體下載</h1>
  <p class="sub">貼上貼文或分享連結（例如 threads.com/share/…），下載影片與圖片。</p>
  <form id="f">
    <input id="u" type="text" required autocomplete="off" placeholder="https://www.threads.com/share/…">
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
    const nv = d.items.filter(x => x.type === 'video').length, ni = d.items.length - nv;
    msg.textContent = '找到 ' + [nv && `${nv} 支影片`, ni && `${ni} 張圖片`].filter(Boolean).join('、');
    for (const v of d.items) {
      const c = document.createElement('div'); c.className = 'card';
      const media = v.type === 'video'
        ? '<video controls playsinline preload="metadata"></video>'
        : '<img class="m" alt="">';
      c.innerHTML = media + `<div class="row"><span class="name"></span><a class="btn">下載</a></div>`;
      c.querySelector('video, img').src = v.preview;
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
