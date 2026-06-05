"""
telegram_uploader.py
────────────────────
Core Telegram video upload module.

Features:
  • Uploads any video file to a configured group/channel
  • Auto-splits files > TELEGRAM_MAX_UPLOAD_MB using ffmpeg (optional)
  • Progress bar in terminal via tqdm
  • Retry with exponential back-off on network errors
  • Returns message_id of the sent message
"""

import os
import time
import logging
import subprocess
import math
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from tqdm import tqdm

# ── Load env ────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)

BOT_TOKEN   = os.getenv("TELEGRAM_CUSTOMER_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GROUP_ID    = os.getenv("TELEGRAM_GROUP_ID", "").strip()
MAX_MB      = int(os.getenv("TELEGRAM_MAX_UPLOAD_MB", "50"))
DEF_CAPTION = os.getenv("DEFAULT_CAPTION", "").strip()
DISABLE_SSL = os.getenv("DISABLE_SSL", "false").lower() == "true"

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
VERIFY   = not DISABLE_SSL

log = logging.getLogger("TelegramUploader")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _get_video_duration(path: Path) -> float:
    """Return video duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _split_video(path: Path, max_mb: int = 45) -> list[Path]:
    """
    Split a large video into parts of ≤ max_mb MB using ffmpeg.
    Returns list of part paths.
    """
    duration = _get_video_duration(path)
    if duration <= 0:
        log.warning("Cannot get duration — uploading as-is (may fail).")
        return [path]

    size_mb = _file_size_mb(path)
    n_parts = math.ceil(size_mb / max_mb)
    part_dur = duration / n_parts

    parts = []
    stem = path.stem
    out_dir = path.parent / "_split_parts"
    out_dir.mkdir(exist_ok=True)

    for i in range(n_parts):
        out = out_dir / f"{stem}_part{i+1:02d}.mp4"
        start = i * part_dur
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(path),
                "-t", str(part_dur),
                "-c", "copy",
                str(out),
            ],
            capture_output=True, timeout=600,
        )
        if out.exists():
            parts.append(out)
            log.info(f"  Part {i+1}/{n_parts}: {out.name}  ({_file_size_mb(out):.1f} MB)")

    return parts


class _ProgressFileWrapper:
    """Wraps a file object to show tqdm upload progress."""

    def __init__(self, path: Path):
        self._f = open(path, "rb")
        self._bar = tqdm(
            total=path.stat().st_size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=f"⬆ {path.name}",
            colour="cyan",
        )

    def read(self, size=-1):
        chunk = self._f.read(size)
        self._bar.update(len(chunk))
        return chunk

    def close(self):
        self._bar.close()
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── Core API calls ────────────────────────────────────────────────────────────

def _send_video_file(
    video_path: Path,
    chat_id: str,
    caption: str = "",
    retries: int = 3,
) -> dict:
    """
    POST a single video file to Telegram.
    Returns the JSON response dict.
    Raises on permanent failure.
    """
    url = f"{BASE_URL}/sendVideo"

    for attempt in range(1, retries + 1):
        try:
            with _ProgressFileWrapper(video_path) as wrapped:
                resp = requests.post(
                    url,
                    data={
                        "chat_id": chat_id,
                        "caption": caption[:1024] if caption else "",
                        "supports_streaming": "true",
                        "parse_mode": "HTML",
                    },
                    files={"video": (video_path.name, wrapped, "video/mp4")},
                    timeout=300,
                    verify=VERIFY,
                )
            data = resp.json()

            if data.get("ok"):
                msg_id = data["result"]["message_id"]
                log.info(f"✅ Sent '{video_path.name}' → message_id {msg_id}")
                return data

            # Telegram-level error
            err = data.get("description", "Unknown error")
            log.error(f"Telegram API error (attempt {attempt}): {err}")

            # Non-retryable errors
            if "file is too big" in err.lower():
                raise ValueError(f"File too large for Telegram: {video_path.name}")
            if "chat not found" in err.lower():
                raise ValueError(f"Chat ID not found: {chat_id}. Is the bot in the group?")

        except (requests.ConnectionError, requests.Timeout) as exc:
            log.warning(f"Network error (attempt {attempt}/{retries}): {exc}")

        if attempt < retries:
            wait = 2 ** attempt
            log.info(f"Retrying in {wait}s …")
            time.sleep(wait)

    raise RuntimeError(f"Failed to upload '{video_path.name}' after {retries} attempts.")


def _send_text(chat_id: str, text: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
        verify=VERIFY,
    )
    return resp.json()


# ── Public API ────────────────────────────────────────────────────────────────

def upload_video(
    video_path: str | Path,
    caption: str = "",
    chat_id: Optional[str] = None,
    auto_split: bool = True,
) -> list[int]:
    """
    Upload a video (or multiple parts if large) to the Telegram group.

    Args:
        video_path : Path to the .mp4 / .mov / .avi file.
        caption    : Caption text (HTML allowed). Falls back to DEFAULT_CAPTION.
        chat_id    : Override the GROUP_ID from .env.
        auto_split : If True and file > MAX_MB, split into parts via ffmpeg.

    Returns:
        List of message_ids for each sent message.
    """
    if not BOT_TOKEN:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN is not set in .env")
    if not GROUP_ID and not chat_id:
        raise EnvironmentError("TELEGRAM_GROUP_ID is not set in .env")

    target_chat = chat_id or GROUP_ID
    path = Path(video_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"Video not found: {path}")

    caption = (caption or DEF_CAPTION).strip()

    size_mb = _file_size_mb(path)
    log.info(f"📦 File: {path.name}  ({size_mb:.1f} MB)")

    # Decide whether to split
    if size_mb > MAX_MB and auto_split:
        log.info(f"🔪 File exceeds {MAX_MB} MB — splitting into parts …")
        parts = _split_video(path, max_mb=int(MAX_MB * 0.9))
    else:
        parts = [path]

    message_ids = []
    for idx, part in enumerate(parts, 1):
        part_caption = caption
        if len(parts) > 1:
            part_caption = f"[Part {idx}/{len(parts)}] {caption}".strip()
        result = _send_video_file(part, target_chat, caption=part_caption)
        message_ids.append(result["result"]["message_id"])

    log.info(f"🎉 Done — {len(message_ids)} message(s) sent to {target_chat}")
    return message_ids


def send_text_message(text: str, chat_id: Optional[str] = None) -> dict:
    """Send a plain text message to the group."""
    target_chat = chat_id or GROUP_ID
    return _send_text(target_chat, text)


def test_connection() -> bool:
    """Verify bot token and group access by sending a test message."""
    log.info("🔍 Testing bot connection …")
    resp = _send_text(GROUP_ID, "🤖 <b>AMTCE Telegram Uploader</b> — connection test ✅")
    ok = resp.get("ok", False)
    if ok:
        log.info("✅ Connection test passed!")
    else:
        log.error(f"❌ Connection test failed: {resp.get('description')}")
    return ok


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_connection()
