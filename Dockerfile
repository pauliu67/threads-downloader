FROM python:3.12-slim

WORKDIR /app
# ffmpeg：檢查 TikTok 影片編碼，並把 H.265 重新封裝成 iPhone 可播放的格式
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium

COPY . .
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "gunicorn -w 2 --threads 4 -t 120 -b 0.0.0.0:${PORT} app:app"]
