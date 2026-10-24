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

_locks: dict = {}
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
        images = _images_from_aweme(aweme_detail or {})
        if images and isinstance(info, dict):
            info["tt_images"] = images
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
            r = requests.get(url, headers={"User-Agent": UA}, timeout=15,
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
        return TikTokError("TikTok 暫時拒絕了伺服器的請求，請稍後再試")
    return TikTokError("無法取得這則 TikTok 貼文（可能已刪除或連結有誤）")


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
    has_video = (info.get("vcodec") or "none") != "none"
    if not images and not has_video:
        raise TikTokError("這則貼文只抓得到聲音，沒有影片或圖片")

    return {
        "id": str(info["id"]),
        "title": info.get("description") or info.get("title") or "",
        "url": info.get("webpage_url") or url,
        "kind": "video" if has_video and not images else "photo",
        "images": images,
        "has_audio": (info.get("acodec") or "none") != "none",
    }


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
    v = next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), None)
    a = next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None)
    return v, a


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
    排除只有聲音、只有畫面，以及 TikTok 自家 H.266（bytevc2）格式。
    """
    fmts = [f for f in (info.get("formats") or [])
            if f.get("url") and f.get("vcodec") not in (None, "none")
            and f.get("acodec") not in (None, "none")
            and f.get("vcodec") != "bytevc2" and f.get("format_note") != "UNPLAYABLE"]

    def rank(f):
        fid, vc = f.get("format_id") or "", f.get("vcodec") or ""
        if fid == "download":
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
    one["formats"] = [fmt]
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
        tmp_dir = tempfile.mkdtemp(dir=CACHE_DIR, prefix=f"dl_{video_id}_")
        try:
            return _fetch(video_id, url, tmp_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _fetch(video_id: str, url: str, tmp_dir: str) -> str:
    try:
        with YoutubeDL(_opts()) as ydl:
            info = ydl.extract_info(_resolve(url), download=False)
            candidates = _candidate_formats(info)

            # 圖片輪播：沒有影片格式，只下載背景音樂
            if not candidates:
                audio = [f for f in (info.get("formats") or [])
                         if f.get("url") and f.get("acodec") not in (None, "none")]
                if not audio:
                    raise TikTokError("這則貼文沒有可下載的影片或聲音")
                src = _download_format(ydl, info, audio[-1], tmp_dir)
                if not src:
                    raise TikTokError("下載失敗，請再試一次")
                dst = os.path.join(CACHE_DIR, f"{video_id}{os.path.splitext(src)[1]}")
                shutil.move(src, dst)
                return dst

            for fmt in candidates[:5]:
                try:
                    src = _download_format(ydl, info, fmt, tmp_dir)
                except DownloadError:
                    continue
                if not src:
                    continue
                vcodec, _ = _probe(src)
                dst = os.path.join(CACHE_DIR, f"{video_id}.mp4")
                if vcodec in ("h264", "unknown"):
                    shutil.move(src, dst)
                    return dst
                if vcodec == "hevc" and _remux_for_iphone(src, dst):
                    return dst
                # 沒有畫面，或是手機播不了的編碼（例如 H.266）→ 換下一個格式
    except DownloadError as e:
        raise _friendly(e) from e
    raise TikTokError("TikTok 只提供了手機無法播放的影片格式，請稍後再試")


def file_mimetype(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"
