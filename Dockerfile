FROM python:3.12-slim

WORKDIR /app

# ffmpeg：檢查 TikTok 影片編碼、把 H.265 重新封裝成 iPhone 可播放的格式，
# 以及把 TikTok 的 H.266（VVC）轉成 H.264。VVC 解碼需要 ffmpeg 7.0 以上；
# 系統套件版本不支援時，改用官方靜態版。
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl xz-utils ca-certificates \
 && if ! ffmpeg -hide_banner -decoders 2>/dev/null | grep -qw vvc; then \
      curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
        | tar -xJ -C /tmp \
      && cp /tmp/ffmpeg-*-static/ffmpeg /tmp/ffmpeg-*-static/ffprobe /usr/local/bin/ \
      && rm -rf /tmp/ffmpeg-*-static; \
    fi \
 && (ffmpeg -hide_banner -decoders | grep -w vvc || echo "WARNING: ffmpeg 沒有 VVC 解碼器") \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium

COPY . .
ENV PORT=8000
EXPOSE 8000
# 轉檔在小主機上可能需要數分鐘，放寬逾時
CMD ["sh", "-c", "gunicorn -w 2 --threads 4 -t 900 -b 0.0.0.0:${PORT} app:app"]
