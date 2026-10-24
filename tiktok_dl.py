"""
TikTok 影片下載（以 yt-dlp 處理）

支援的連結：
    https://www.tiktok.com/@帳號/video/1234567890
    https://vm.tiktok.com/XXXXXXX/   https://vt.tiktok.com/XXXXXXX/   （分享短連結）
    https://www.tiktok.com/t/XXXXXXX/
也可以貼上整段分享文字，會自動找出連結。

TikTok 的影片網址需要搭配下載時的 cookie 才能取得，所以由伺服器先下載到暫存資料夾，
再傳給使用者；同一支影片在 15 分鐘內重複請求（預覽＋下載）只會下載一次。
"""

import glob
import os
import re
import tempfile
import threading
import time

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

TIKTOK_URL_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)?tiktok\.com/[^\s<>\"']+", re.I)
VIDEO_ID_RE = re.compile(r"^\d{5,25}$")

CACHE_DIR = os.path.join(tempfile.gettempdir(), "tiktok_cache")
CACHE_TTL = 15 * 60
os.makedirs(CACHE_DIR, exist_ok=True)

_locks: dict = {}
_locks_guard = threading.Lock()


class TikTokError(Exception):
    """可直接顯示給使用者的錯誤訊息。"""


def find_tiktok_url(text: str):
    m = TIKTOK_URL_RE.search(text or "")
    return m.group(0).rstrip(".,，。)") if m else None


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


def _friendly(err: Exception, url: str) -> TikTokError:
    msg = str(err)
    if "/photo/" in url or "photo" in msg.lower() or "Unsupported URL" in msg:
        return TikTokError("目前不支援 TikTok 圖片輪播貼文，只能下載影片")
    if "private" in msg.lower() or "login" in msg.lower():
        return TikTokError("這支影片是私人影片或需要登入才能觀看")
    if "blocked" in msg.lower() or "403" in msg or "10204" in msg:
        return TikTokError("TikTok 暫時拒絕了伺服器的請求，請稍後再試")
    return TikTokError("無法取得這支 TikTok 影片（可能已刪除或連結有誤）")


def tiktok_info(url: str) -> dict:
    """取得影片基本資料，不下載。"""
    try:
        with YoutubeDL(_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        raise _friendly(e, url) from e
    if not info or not info.get("id"):
        raise TikTokError("無法取得這支 TikTok 影片")
    if info.get("_type") == "playlist":
        raise TikTokError("請貼上單一影片的連結")
    return {
        "id": str(info["id"]),
        "title": info.get("title") or info.get("description") or "",
        "uploader": info.get("uploader") or info.get("creator") or "",
        "url": info.get("webpage_url") or url,
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
    """下載（或從暫存取得）影片檔，回傳本機路徑。"""
    if not VIDEO_ID_RE.match(video_id or ""):
        raise TikTokError("影片代碼錯誤")
    _cleanup()

    with _locks_guard:
        lock = _locks.setdefault(video_id, threading.Lock())
    with lock:
        path = _cached(video_id)
        if path:
            return path
        try:
            with YoutubeDL(_opts(outtmpl=os.path.join(CACHE_DIR, "%(id)s.%(ext)s"))) as ydl:
                ydl.download([url])
        except DownloadError as e:
            raise _friendly(e, url) from e
        path = _cached(video_id)
        if not path:
            raise TikTokError("下載失敗，請再試一次")
        return path
