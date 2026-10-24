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

import glob
import mimetypes
import os
import re
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
                os.remove(f)
        except OSError:
            pass


def _cached(video_id: str):
    for f in glob.glob(os.path.join(CACHE_DIR, f"{video_id}.*")):
        if not f.endswith((".part", ".ytdl", ".tmp")):
            return f
    return None


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
        try:
            with YoutubeDL(_opts(outtmpl=os.path.join(CACHE_DIR, "%(id)s.%(ext)s"))) as ydl:
                ydl.download([_resolve(url)])
        except DownloadError as e:
            raise _friendly(e) from e
        path = _cached(video_id)
        if not path:
            raise TikTokError("下載失敗，請再試一次")
        return path


def file_mimetype(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"
