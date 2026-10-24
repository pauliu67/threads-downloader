"""
TikTok 影片／圖片輪播下載（以 yt-dlp 處理）

支援的連結：
    https://www.tiktok.com/@帳號/video/1234567890
    https://www.tiktok.com/@帳號/photo/1234567890      （圖片輪播貼文）
    https://vm.tiktok.com/XXXXXXX/   https://vt.tiktok.com/XXXXXXX/   （分享短連結）
    https://www.tiktok.com/t/XXXXXXX/
也可以貼上整段分享文字，會自動找出連結。

影片：TikTok 的影片網址需要搭配下載時的 cookie，所以由伺服器先下載到暫存資料夾再傳給使用者；
      同一支影片在 15 分鐘內重複請求（預覽＋下載）只會下載一次。
圖片輪播：yt-dlp 只會回傳背景音樂，這裡另外從貼文資料取出每張圖片的網址，音樂也一併提供。
"""

import copy
import glob
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import requests
from urllib.parse import urlparse
from yt_dlp import YoutubeDL
from yt_dlp.extractor.tiktok import TikTokBaseIE
from yt_dlp.utils import DownloadError

TIKTOK_URL_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)?tiktok\.com/[^\s<>\"']+", re.I)
SHORT_URL_RE = re.compile(r"https?://(?:(?:vm|vt)\.tiktok\.com|(?:www\.)?tiktok\.com/t)/", re.I)
PHOTO_PATH_RE = re.compile(r"(tiktok\.com/@[^/]*)/photo/(\d+)", re.I)
VIDEO_ID_RE = re.compile(r"^\d{5,25}$")

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

CACHE_DIR = os.path.join(tempfile.gettempdir(), "tiktok_cache")
CACHE_TTL = 15 * 60
os.makedirs(CACHE_DIR, exist_ok=True)

def _build_cookiefile():
    """
    選用：讓伺服器以 TikTok 帳號的登入狀態抓取（需要登入或有年齡限制的影片）。
      TIKTOK_COOKIES   ：Netscape 格式的完整 cookies.txt 內容
      TIKTOK_SESSIONID ：只填瀏覽器 cookie 裡的 sessionid 值
    建議使用研究專用的帳號，不要用個人主帳號。
    """
    raw = os.environ.get("TIKTOK_COOKIES", "").strip()
    sid = os.environ.get("TIKTOK_SESSIONID", "").strip()
    if not raw and not sid:
        return None
    if not raw:
        exp = int(time.time()) + 365 * 86400
        raw = "\n".join(
            f".tiktok.com\tTRUE\t/\tTRUE\t{exp}\t{name}\t{sid}"
            for name in ("sessionid", "sessionid_ss", "sid_tt"))
    if not raw.startswith("# Netscape"):
        raw = "# Netscape HTTP Cookie File\n" + raw
    path = os.path.join(tempfile.gettempdir(), "tiktok_cookies.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw + "\n")
    os.chmod(path, 0o600)
    return path


COOKIEFILE = _build_cookiefile()

# 選用：TikTok 會擋雲端機房的 IP。設定 TIKTOK_PROXY（例如家用網路代理
# http://帳號:密碼@主機:埠）後，所有連到 TikTok 的請求都改走這個代理。
PROXY = os.environ.get("TIKTOK_PROXY", "").strip() or None
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

_locks: dict = {}
_failures: dict = {}          # video_id -> (時間, 錯誤訊息)；失敗後 5 分鐘內不重抓
FAIL_TTL = 5 * 60
_locks_guard = threading.Lock()


class TikTokError(Exception):
    """可直接顯示給使用者的錯誤訊息。"""


# ---------- 讓 yt-dlp 一併回傳圖片輪播的圖片 ----------

def _images_from_aweme(aweme: dict):
    urls = []
    # 網頁資料：imagePost.images[].imageURL.urlList
    for img in ((aweme.get("imagePost") or {}).get("images") or []):
        lst = ((img or {}).get("imageURL") or {}).get("urlList") or []
        if lst:
            urls.append(lst[0])
    # App API：image_post_info.images[].display_image.url_list
    if not urls:
        for img in ((aweme.get("image_post_info") or {}).get("images") or []):
            lst = ((img or {}).get("display_image") or {}).get("url_list") or []
            if lst:
                urls.append(lst[0])
    return [u for u in urls if isinstance(u, str) and u.startswith("http")]


def _wrap(orig):
    def parse(self, aweme_detail, *args, **kwargs):
        info = orig(self, aweme_detail, *args, **kwargs)
        aweme = aweme_detail or {}
        images = _images_from_aweme(aweme)
        if isinstance(info, dict):
            if images:
                info["tt_images"] = images
            # 診斷用：TikTok 回傳的影片資料有哪些欄位
            v = aweme.get("video") or {}
            info["tt_diag"] = {
                "bitrate": len(v.get("bitrateInfo") or v.get("bit_rate") or []),
                "play": bool(v.get("playAddr") or v.get("play_addr")),
                "download": bool(v.get("downloadAddr") or v.get("download_addr")),
            }
        return info
    return parse


for _name in ("_parse_aweme_video_web", "_parse_aweme_video_app"):
    if hasattr(TikTokBaseIE, _name):
        setattr(TikTokBaseIE, _name, _wrap(getattr(TikTokBaseIE, _name)))


# ---------- 工具函式 ----------

def find_tiktok_url(text: str):
    m = TIKTOK_URL_RE.search(text or "")
    return m.group(0).rstrip(".,，。)") if m else None


def _resolve(url: str) -> str:
    """展開短連結，並把 /photo/ 換成 yt-dlp 認得的 /video/。"""
    if SHORT_URL_RE.match(url):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=15, proxies=PROXIES,
                             allow_redirects=True, stream=True)
            r.close()
            if "tiktok.com" in (r.url or ""):
                url = r.url
        except requests.RequestException:
            pass  # 交給 yt-dlp 自己處理
    return PHOTO_PATH_RE.sub(r"\1/video/\2", url.split("?")[0])


def _opts(**extra):
    opts = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        # 不要預先「試下載」再剔除格式：從雲端主機試常會失敗，導致影片版本全被丟掉。
        # 改由 _fetch 實際下載並用 ffprobe 檢查。
        "check_formats": False,
        **({"cookiefile": COOKIEFILE} if COOKIEFILE else {}),
        **({"proxy": PROXY} if PROXY else {}),
        # 單一檔案（含音訊），優先 H.264 以相容所有手機，並優先無浮水印版本
        "format": "b",
        "format_sort": ["vcodec:h264", "res:1080"],
    }
    opts.update(extra)
    return opts


def _friendly(err: Exception) -> TikTokError:
    msg = str(err).lower()
    if "private" in msg or "permission" in msg or "login" in msg:
        return TikTokError("這則貼文是私人內容或需要登入才能觀看")
    if "blocked" in msg or "403" in msg or "10204" in msg:
        return TikTokError("TikTok 拒絕了伺服器的連線（雲端主機的 IP 被擋）"
                           + ("" if PROXY else "，可設定 TIKTOK_PROXY 改走一般網路"))
    return TikTokError("無法取得這則 TikTok 貼文（可能已刪除或連結有誤）")


def _can_impersonate() -> bool:
    """yt-dlp 是否能模擬真實瀏覽器連線（需要 curl_cffi）；TikTok 沒有它常只回傳簡化資料。"""
    try:
        with YoutubeDL({"quiet": True}) as ydl:
            return bool(ydl._get_available_impersonate_targets())
    except Exception:
        return False


def _diag(info: dict, attempts=None) -> str:
    """把 TikTok 回傳的格式整理成一行，附在錯誤訊息後面方便排查。"""
    parts = []
    for f in (info.get("formats") or []):
        v = f.get("vcodec") or "?"
        a = "有聲" if f.get("acodec") not in (None, "none") else "無聲"
        parts.append(f"{f.get('format_id')}({'純音訊' if v == 'none' else v},{a})")
    d = info.get("tt_diag") or {}
    s = (f"\n［診斷］{'已' if COOKIEFILE else '未'}登入；"
         f"瀏覽器模擬{'可用' if _can_impersonate() else '不可用'}；"
         f"{'經由代理' if PROXY else '直連'}；"
         f"格式：{'、'.join(parts) or '無'}")
    if d:
        s += (f"；bitrateInfo {d.get('bitrate', 0)} 筆、playAddr {'有' if d.get('play') else '無'}、"
              f"downloadAddr {'有' if d.get('download') else '無'}")
    if attempts:
        s += "；嘗試結果：" + "、".join(attempts)
    return s


def _image_ext(u: str) -> str:
    path = u.split("?")[0].lower()
    for key, ext in ((".webp", ".webp"), (".png", ".png"), (".heic", ".heic")):
        if key in path:
            return ext
    return ".jpg"


# ---------- 對外介面 ----------

def tiktok_info(url: str) -> dict:
    """
    取得貼文資料，不下載影片。回傳：
        {"id", "title", "url", "kind": "video" | "photo",
         "images": [圖片網址...], "has_audio": bool}
    """
    url = _resolve(url)
    try:
        with YoutubeDL(_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        raise _friendly(e) from e
    if not info or not info.get("id"):
        raise TikTokError("無法取得這則 TikTok 貼文")
    if info.get("_type") == "playlist":
        raise TikTokError("請貼上單一貼文的連結")

    images = info.get("tt_images") or []
    # 看完整格式清單（不只 yt-dlp 選中的那一個），有任何影片版本就交給下載階段逐一嘗試
    has_video = any(f.get("vcodec") not in (None, "none") for f in (info.get("formats") or []))
    embed_notes = []
    if not images and not has_video:
        _, embed_urls, embed_notes = _embed_video_urls(str(info["id"]))
        has_video = bool(embed_urls)
    if not images and not has_video:
        hint = ("" if COOKIEFILE else
                "\n這類影片通常需要登入才能看（例如年齡限制），可在伺服器設定 TIKTOK_SESSIONID。")
        raise TikTokError("這則貼文只抓得到聲音，沒有影片或圖片" + hint
                          + _diag(info, embed_notes))

    return {
        "id": str(info["id"]),
        "title": info.get("description") or info.get("title") or "",
        "url": info.get("webpage_url") or url,
        "kind": "video" if has_video and not images else "photo",
        "images": images,
        "has_audio": (info.get("acodec") or "none") != "none",
    }


# ---------- 備援：TikTok 嵌入播放器頁面 ----------
# 一般網頁對未登入訪客有時只給背景音樂；嵌入播放器（網站嵌入 TikTok 影片用的頁面）
# 提供資料的方式不同，常常仍有影片網址。

EMBED_PAGES = (("player", "https://www.tiktok.com/player/v1/{id}"),
               ("embed", "https://www.tiktok.com/embed/v2/{id}"))
_URL_IN_JSON = re.compile(r'"(https?:(?:\\/|\\u002[Ff]|/)[^"\s]+)"')
_VIDEO_HOST = re.compile(r"tiktok|byteoversea|ibyteimg|ibytedtos|muscdn", re.I)


def _unescape(u: str) -> str:
    try:
        return json.loads(f'"{u}"')
    except ValueError:
        return u.replace("\\/", "/").replace("\\u002F", "/").replace("\\u0026", "&")


def _looks_like_video(u: str) -> bool:
    if not _VIDEO_HOST.search(urlparse(u).hostname or ""):
        return False
    low = u.lower()
    if any(k in low for k in ("mime_type=audio", ".mp3", "/music/", "audio_mpeg", "ies-music")):
        return False                       # 背景音樂，不是影片
    return any(k in low for k in ("mime_type=video", "/video/tos/", "video_mp4"))


def _embed_session():
    from curl_cffi import requests as creq        # yt-dlp 瀏覽器模擬用的同一個套件
    return creq.Session(impersonate="chrome", proxies=PROXIES)


def _embed_video_urls(video_id: str):
    """回傳 (session, 影片網址清單, 診斷說明)。"""
    notes, urls = [], []
    try:
        sess = _embed_session()
    except Exception:
        return None, [], ["嵌入頁:無法建立連線"]
    for name, tpl in EMBED_PAGES:
        try:
            r = sess.get(tpl.format(id=video_id), timeout=20)
        except Exception:
            notes.append(f"{name}:連線失敗")
            continue
        found = [_unescape(m.group(1)) for m in _URL_IN_JSON.finditer(r.text or "")]
        vids = [u for u in found if _looks_like_video(u)]
        notes.append(f"{name}:HTTP{r.status_code},影片網址{len(vids)}個")
        for u in vids:
            if u not in urls:
                urls.append(u)
        if urls:
            break
    return sess, urls, notes


def _download_with_session(sess, u: str, path: str, referer: str):
    """下載並回傳 (成功與否, 說明)。說明含 HTTP 狀態、類型、大小，方便排查。"""
    headers = {"Referer": referer, "Origin": "https://www.tiktok.com",
               "Range": "bytes=0-", "Accept": "*/*"}
    try:
        r = sess.get(u, headers=headers, timeout=120, stream=True)
        ctype = (r.headers.get("content-type") or "?").split(";")[0]
        if r.status_code >= 400:
            return False, f"HTTP{r.status_code}"
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
        size = os.path.getsize(path)
        return size > 0, f"HTTP{r.status_code},{ctype},{size // 1024}KB"
    except Exception as e:
        return False, f"錯誤{type(e).__name__}"


def _streams_summary(path: str) -> str:
    """列出檔案裡的串流（類型/編碼/標記）；不是影音檔時顯示開頭內容。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,codec_tag_string", "-of", "json", path],
            capture_output=True, text=True, timeout=30)
        streams = json.loads(out.stdout or "{}").get("streams", [])
        if streams:
            return "+".join(f"{x.get('codec_type')}/{x.get('codec_name') or '?'}"
                            f"/{x.get('codec_tag_string') or '?'}" for x in streams)
        err = (out.stderr or "").strip().splitlines()
        with open(path, "rb") as f:
            head = f.read(24)
        last = err[-1].replace(path, "").strip(": ") if err else ""
        return f"無串流({last[:60]}) 開頭:{head[:24]!r}"
    except Exception as e:
        return f"無法檢查({type(e).__name__})"


def _cleanup():
    now = time.time()
    for f in glob.glob(os.path.join(CACHE_DIR, "*")):
        try:
            if now - os.path.getmtime(f) > CACHE_TTL:
                if os.path.isdir(f):
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    os.remove(f)
        except OSError:
            pass


def _cached(video_id: str):
    for f in glob.glob(os.path.join(CACHE_DIR, f"{video_id}.*")):
        if os.path.isfile(f) and not f.endswith((".part", ".ytdl", ".tmp")):
            return f
    return None


def _probe(path: str):
    """回傳 (影片編碼, 音訊編碼)，沒有則為 None。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30).stdout
        streams = json.loads(out or "{}").get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return "unknown", "unknown"          # 沒有 ffprobe 時不做檢查
    # 有影片串流但 ffprobe 認不出編碼時回傳 "unrecognized"，與「沒有畫面」(None) 區分
    v = next((s.get("codec_name") or "unrecognized"
              for s in streams if s.get("codec_type") == "video"), None)
    a = next((s.get("codec_name") or "unrecognized"
              for s in streams if s.get("codec_type") == "audio"), None)
    return v, a


def _transcode_to_h264(src: str, dst: str) -> bool:
    """
    把手機無法播放的編碼（例如 TikTok 的 H.266／bytevc2）轉成 H.264。
    需要支援 VVC 解碼的 ffmpeg（7.0 以上）；為了在小主機上跑得動，限制寬度 720 並使用快速設定。
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src,
             "-map", "0:v:0", "-map", "0:a:0?",
             "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", dst],
            capture_output=True, timeout=900)
        return r.returncode == 0 and os.path.getsize(dst) > 0
    except (OSError, subprocess.SubprocessError):
        return False


def _remux_for_iphone(src: str, dst: str) -> bool:
    """HEVC 重新封裝成 hvc1 標記（iPhone／Safari 才能顯示畫面），並把索引移到檔頭方便預覽。"""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src, "-c", "copy", "-tag:v", "hvc1",
             "-movflags", "+faststart", dst],
            capture_output=True, timeout=120)
        return r.returncode == 0 and os.path.getsize(dst) > 0
    except (OSError, subprocess.SubprocessError):
        return False


def _candidate_formats(info: dict):
    """
    依「最可能在手機上正常播放」排序候選格式：
    明確標示為 H.264 的 > H.265 > 未標示編碼的 play > 有浮水印的 download（通常是 H.264）
    > TikTok 自家 H.266（bytevc2，需轉檔，最後才用）。
    排除只有聲音或只有畫面的格式。
    關閉下載的影片通常沒有 download 版本，只剩播放用的 H.265／H.266 串流。
    """
    fmts = [f for f in (info.get("formats") or [])
            if f.get("url") and f.get("vcodec") not in (None, "none")
            and f.get("acodec") not in (None, "none")]

    def rank(f):
        fid, vc = f.get("format_id") or "", f.get("vcodec") or ""
        if vc == "bytevc2" or f.get("format_note") == "UNPLAYABLE":
            group = 4
        elif fid == "download":
            group = 3
        elif fid == "play":
            group = 2
        elif vc.startswith(("h264", "avc")):
            group = 0
        elif vc.startswith(("h265", "hevc", "hvc", "bytevc1")):
            group = 1
        else:
            group = 2
        size = (f.get("width") or 0) * (f.get("height") or 0)
        return (group, -size, -(f.get("tbr") or 0))

    return sorted(fmts, key=rank)


def _download_format(ydl, info: dict, fmt: dict, tmp_dir: str):
    """用同一個 YoutubeDL（保留 cookie）下載指定格式，回傳檔案路徑。"""
    for f in glob.glob(os.path.join(tmp_dir, "*")):
        os.remove(f)
    one = copy.deepcopy(info)
    for k in ("requested_formats", "requested_downloads", "format_id", "url", "_filename",
              "filepath"):
        one.pop(k, None)
    one["formats"] = [{k: v for k, v in fmt.items() if k != "__needs_testing"}]
    ydl.params["format"] = fmt["format_id"]
    ydl.format_selector = ydl.build_format_selector(fmt["format_id"])
    ydl.params["outtmpl"] = {"default": os.path.join(tmp_dir, "media.%(ext)s")}
    ydl.process_ie_result(one, download=True)
    files = [f for f in glob.glob(os.path.join(tmp_dir, "media.*"))
             if not f.endswith((".part", ".ytdl"))]
    return files[0] if files else None


def tiktok_file(video_id: str, url: str) -> str:
    """下載（或從暫存取得）影片或背景音樂，回傳本機路徑。"""
    if not VIDEO_ID_RE.match(video_id or ""):
        raise TikTokError("貼文代碼錯誤")
    _cleanup()

    with _locks_guard:
        lock = _locks.setdefault(video_id, threading.Lock())
    with lock:
        path = _cached(video_id)
        if path:
            return path
        failed = _failures.get(video_id)
        if failed and time.time() - failed[0] < FAIL_TTL:
            raise TikTokError(failed[1])
        tmp_dir = tempfile.mkdtemp(dir=CACHE_DIR, prefix=f"dl_{video_id}_")
        try:
            return _fetch(video_id, url, tmp_dir)
        except TikTokError as e:
            _failures[video_id] = (time.time(), str(e))
            raise
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _accept(src: str, dst: str, state: dict, attempts: list, label: str) -> bool:
    """檢查下載的檔案；可直接用或已重新封裝就回傳 True，需轉檔的先留著。"""
    vcodec, _ = _probe(src)
    attempts.append(f"{label}:{vcodec or '無畫面'}")
    if vcodec in ("h264", "unknown"):
        shutil.move(src, dst)
        return True
    if vcodec == "hevc" and _remux_for_iphone(src, dst):
        return True
    if vcodec and state.get("transcode") is None:
        keep = os.path.join(os.path.dirname(src), "transcode_src" + os.path.splitext(src)[1])
        shutil.move(src, keep)
        state["transcode"] = keep
    return False


def _fetch(video_id: str, url: str, tmp_dir: str) -> str:
    dst = os.path.join(CACHE_DIR, f"{video_id}.mp4")
    state, attempts = {}, []
    try:
        with YoutubeDL(_opts()) as ydl:
            info = ydl.extract_info(_resolve(url), download=False)
            candidates = _candidate_formats(info)

            # 圖片輪播：沒有影片格式，只下載背景音樂
            if not candidates and info.get("tt_images"):
                audio = [f for f in (info.get("formats") or [])
                         if f.get("url") and f.get("acodec") not in (None, "none")]
                if not audio:
                    raise TikTokError("這則貼文沒有可下載的影片或聲音")
                src = _download_format(ydl, info, audio[-1], tmp_dir)
                if not src:
                    raise TikTokError("下載失敗，請再試一次")
                adst = os.path.join(CACHE_DIR, f"{video_id}{os.path.splitext(src)[1]}")
                shutil.move(src, adst)
                return adst

            # 1) yt-dlp 從一般網頁取得的格式
            for fmt in candidates[:6]:
                fid = fmt.get("format_id")
                try:
                    src = _download_format(ydl, info, fmt, tmp_dir)
                except DownloadError as e:
                    m = re.search(r"HTTP Error (\d+)", str(e))
                    attempts.append(f"{fid}:下載失敗{m.group(1) if m else ''}")
                    continue
                if not src:
                    attempts.append(f"{fid}:沒有檔案")
                    continue
                if _accept(src, dst, state, attempts, fid):
                    return dst
    except DownloadError as e:
        raise _friendly(e) from e

    # 2) 備援：嵌入播放器頁面
    sess, urls, notes = _embed_video_urls(video_id)
    attempts += notes
    referers = (f"https://www.tiktok.com/embed/v2/{video_id}", "https://www.tiktok.com/")
    for i, u in enumerate(urls[:4], 1):
        for j, ref in enumerate(referers, 1):
            label = f"嵌入{i}-{j}"
            src = os.path.join(tmp_dir, f"embed_{i}_{j}.mp4")
            ok, how = _download_with_session(sess, u, src, ref)
            if not ok:
                attempts.append(f"{label}:下載失敗({how})")
                continue
            detail = _streams_summary(src)
            if _accept(src, dst, state, attempts, label):
                return dst
            attempts.append(f"{label}細節:{how};{detail}")

    # 3) 只有手機播不了的編碼（例如 H.266）→ 轉成 H.264
    if state.get("transcode"):
        if _transcode_to_h264(state["transcode"], dst):
            return dst
        raise TikTokError("這支影片使用手機無法播放的編碼，伺服器轉檔失敗"
                          + _diag(info, attempts))
    raise TikTokError("這支影片抓不到畫面" + _diag(info, attempts))


def file_mimetype(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"
