FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium

COPY . .
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "gunicorn -w 2 --threads 4 -t 120 -b 0.0.0.0:${PORT} app:app"]
