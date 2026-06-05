"""
trailer_uploader.py
───────────────────
Processes and uploads a new video into the sales system.

Workflow:
1. Slices and transcodes the source 1080p video into 3 tiers using video_processor.py.
2. Uploads the 3 tiers to the private Storage Group and registers them in video_index.json.
3. Obtains or generates a short trailer clip (first 10 seconds by default).
4. Queries Telegram to get the bot's username for deep linking.
5. Uploads the trailer to the public Trailer Group with a "Buy Now" button linking to the Sales Bot.

Usage:
  python trailer_uploader.py --video path/to/video.mp4 --caption "New Hot Drop 🔥" --slug "actress_name_01"
"""

import os
import sys
import argparse
import logging
import subprocess
import requests
from pathlib import Path
from dotenv import load_dotenv

# Local imports
from video_processor import process_video_tiers
from storage_manager import register_video_tiers
from ai_helper import generate_tempting_hook

_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)

BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TRAILER_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID", "").strip()
DISABLE_SSL      = os.getenv("DISABLE_SSL", "false").lower() == "true"

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
VERIFY   = not DISABLE_SSL

log = logging.getLogger("TrailerUploader")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


def get_bot_username() -> str:
    """Gets the bot's username dynamically using /getMe."""
    url = f"{BASE_URL}/getMe"
    try:
        resp = requests.get(url, timeout=10, verify=VERIFY).json()
        if resp.get("ok"):
            return resp["result"]["username"]
        else:
            raise RuntimeError(f"Could not get bot details: {resp.get('description')}")
    except Exception as e:
        log.error(f"Error fetching bot username: {e}")
        raise e


def generate_trailer(video_path: Path, output_path: Path, seconds: int = 10):
    """Generates a 10 second trailer from the start of the video."""
    log.info(f"Generating temporary {seconds}s trailer from {video_path.name}...")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-t", str(seconds),
        "-c:v", "libx264", "-preset", "fast", "-crf", "24",
        "-c:a", "aac", "-b:a", "128k",
        str(output_path)
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=120)
        log.info(f"Trailer generated: {output_path.name}")
    except subprocess.CalledProcessError as e:
        log.error(f"FFmpeg error generating trailer: {e.stderr.decode('utf-8', errors='ignore')}")
        raise e


def upload_trailer_to_public(
    trailer_path: Path,
    slug: str,
    caption: str,
    bot_username: str
) -> int:
    """
    Uploads the trailer to the public group with a Buy button.
    Returns the message_id of the public post.
    """
    url = f"{BASE_URL}/sendVideo"
    
    # Deep link to open the bot and pass the slug parameter
    buy_link = f"https://t.me/{bot_username}?start={slug}"
    
    # Inline keyboard
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "🛒 Unlock Quality Tiers (FREE / ₹59 / ₹149)", "url": buy_link}
            ]
        ]
    }
    
    import asyncio
    formatted_caption = asyncio.run(generate_tempting_hook(slug))
    
    log.info(f"Uploading trailer to public group {TRAILER_GROUP_ID}...")
    
    with open(trailer_path, "rb") as f:
        resp = requests.post(
            url,
            data={
                "chat_id": TRAILER_GROUP_ID,
                "caption": formatted_caption,
                "supports_streaming": "true",
                "parse_mode": "HTML",
                "reply_markup": requests.utils.quote(str(reply_markup).replace("'", '"')) 
                # Converting dict to JSON string format safely
            },
            files={"video": (trailer_path.name, f, "video/mp4")},
            timeout=300,
            verify=VERIFY
        )
        
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Failed to post public trailer: {data.get('description')}")
        
    msg_id = data["result"]["message_id"]
    log.info(f"Trailer posted successfully to public group! msg_id: {msg_id}")
    return msg_id


def process_and_publish(
    video_path: str | Path,
    caption: str,
    slug: str = None,
    trailer_path: str | Path = None
):
    """Core workflow coordinator."""
    video = Path(video_path).resolve()
    if not video.exists():
        raise FileNotFoundError(f"Video file not found: {video}")
        
    if not slug:
        # Generate slug from filename
        slug = video.stem.lower().replace(" ", "_").replace("-", "_")
        log.info(f"Auto-generated slug: {slug}")
        
    bot_username = get_bot_username()
    log.info(f"Connected as Bot: @{bot_username}")
    
    # 1. Process tiers
    log.info("--- Step 1: Processing quality tiers ---")
    tier_paths = process_video_tiers(video)
    
    # 2. Upload tiers & register in storage index
    log.info("--- Step 2: Uploading tiers to storage ---")
    register_video_tiers(slug, tier_paths, caption=caption)
    
    # 3. Generate trailer if not provided
    temp_trailer = None
    if not trailer_path:
        temp_trailer = video.parent / f"temp_{slug}_trailer.mp4"
        generate_trailer(video, temp_trailer, seconds=10)
        trailer_file = temp_trailer
    else:
        trailer_file = Path(trailer_path).resolve()
        
    # 4. Upload trailer with Buy button
    log.info("--- Step 3: Posting trailer to public group ---")
    try:
        upload_trailer_to_public(trailer_file, slug, caption, bot_username)
    finally:
        # Clean up temp trailer file if we created it
        if temp_trailer and temp_trailer.exists():
            try:
                temp_trailer.unlink()
                log.info("Temporary trailer file cleaned up.")
            except Exception as e:
                log.warning(f"Could not delete temp trailer file: {e}")
                
    log.info(f"🎉 Fully completed! Video '{slug}' is now live for purchase.")


def main():
    parser = argparse.ArgumentParser(description="Upload source video, process tiers and post trailer to group.")
    parser.add_argument("--video", "-v", required=True, help="Path to the source 1080p video")
    parser.add_argument("--caption", "-c", default="New video drop!", help="Public caption description")
    parser.add_argument("--slug", "-s", default=None, help="Unique identifier for indexing this video (auto-generated if omitted)")
    parser.add_argument("--trailer", "-t", default=None, help="Optional pre-cut trailer file. Auto-generated from start if omitted.")
    
    args = parser.parse_args()
    
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is missing in .env")
        sys.exit(1)
    if not TRAILER_GROUP_ID:
        log.error("TELEGRAM_GROUP_ID is missing in .env")
        sys.exit(1)
        
    try:
        process_and_publish(args.video, args.caption, args.slug, args.trailer)
    except Exception as e:
        log.error(f"Process failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
