"""
upload_video.py
───────────────
CLI tool — upload one or more videos to the Telegram group.

Usage:
  python upload_video.py video.mp4
  python upload_video.py clip1.mp4 clip2.mp4 --caption "New drops 🔥"
  python upload_video.py video.mp4 --chat-id -100123456789
  python upload_video.py --test          (connection test only)
"""

import argparse
import sys
from pathlib import Path

from telegram_uploader import upload_video, test_connection, log


def main():
    parser = argparse.ArgumentParser(
        description="Upload video(s) to your Telegram group via bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "videos",
        nargs="*",
        help="Path(s) to video file(s) to upload",
    )
    parser.add_argument(
        "--caption", "-c",
        default="",
        help="Caption text (HTML supported). Overrides DEFAULT_CAPTION in .env",
    )
    parser.add_argument(
        "--chat-id",
        default=None,
        help="Override the TELEGRAM_GROUP_ID from .env",
    )
    parser.add_argument(
        "--no-split",
        action="store_true",
        help="Disable auto-splitting for large files",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run a connection test (sends a text message to the group)",
    )

    args = parser.parse_args()

    if args.test:
        ok = test_connection()
        sys.exit(0 if ok else 1)

    if not args.videos:
        parser.print_help()
        sys.exit(1)

    all_ok = True
    for vid in args.videos:
        path = Path(vid)
        if not path.exists():
            log.error(f"File not found: {vid}")
            all_ok = False
            continue
        try:
            ids = upload_video(
                video_path=path,
                caption=args.caption,
                chat_id=args.chat_id,
                auto_split=not args.no_split,
            )
            log.info(f"Uploaded '{path.name}' → message IDs: {ids}")
        except Exception as exc:
            log.error(f"Failed: {exc}")
            all_ok = False

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
