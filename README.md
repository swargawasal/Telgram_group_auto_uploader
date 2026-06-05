# Telegram Video Uploader

Upload any video to your private Telegram group using a bot.

## 📁 Folder Structure

```
Telegram_Video_Uploader/
├── .env                  ← Bot token + group ID (already configured)
├── telegram_uploader.py  ← Core upload module (importable)
├── upload_video.py       ← CLI: upload one or more files manually
├── watch_and_upload.py   ← Watcher: auto-upload anything dropped in watch_inbox/
├── requirements.txt
├── watch_inbox/          ← Drop videos here (auto-created)
└── watch_done/           ← Uploaded videos moved here (auto-created)
```

## ⚡ Quick Start

```bash
cd d:\AMTCE\Telegram_Video_Uploader

# 1. Install deps (first time only)
pip install -r requirements.txt

# 2. Test bot connection
python upload_video.py --test

# 3. Upload a video
python upload_video.py path\to\video.mp4

# 4. Upload with a caption
python upload_video.py video.mp4 --caption "New clip 🔥"

# 5. Upload multiple files
python upload_video.py clip1.mp4 clip2.mp4 clip3.mp4

# 6. Auto-watch folder (leave running, drop files to upload)
python watch_and_upload.py
```

## 🤖 Using as a Module in Other Scripts

```python
from Telegram_Video_Uploader.telegram_uploader import upload_video

upload_video("path/to/video.mp4", caption="Auto-uploaded by AMTCE 🎬")
```

## ⚙️ .env Options

| Key | Default | Description |
|-----|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | *(set)* | Bot token from @BotFather |
| `TELEGRAM_GROUP_ID` | *(set)* | Target group ID (negative number) |
| `TELEGRAM_MAX_UPLOAD_MB` | `50` | Max size before auto-split |
| `DEFAULT_CAPTION` | *(blank)* | Default caption for all uploads |
| `WATCH_FOLDER` | `./watch_inbox` | Folder watched by watcher script |
| `DONE_FOLDER` | `./watch_done` | Where uploaded files are moved |

## 📝 Notes

- Files **> 50 MB** are auto-split via `ffmpeg` into parts and sent sequentially.
- The bot **must be an admin** in the group (or at least have "Send Messages" permission).
- To add bot: open group → Add Member → search your bot username.
