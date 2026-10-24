# Threads／TikTok 下載

貼上連結，預覽並下載：

- **Threads**：影片與圖片（含輪播），支援 `threads.com/share/…` 分享短連結
- **TikTok**：影片，支援 `vt.tiktok.com`、`vm.tiktok.com` 分享短連結（使用 yt-dlp；圖片輪播貼文暫不支援）

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/pauliu67/threads-downloader)

## 設定

- `APP_PASSWORD`（選用）：設定後開啟網頁需輸入密碼，帳號欄可填任意文字。留空則不需密碼。

## 本機執行

```
pip install -r requirements.txt
playwright install chromium
python app.py
```

請僅下載你有權使用的內容，並遵守 Threads 服務條款。
