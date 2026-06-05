"""
watch_and_upload.py
────────────────────
Folder watcher — drop any video into ./watch_inbox/ and it will be
automatically uploaded to the Telegram group within seconds.

Usage:
  python watch_and_upload.py
  python watch_and_upload.py --inbox /path/to/folder --caption "Auto drop 🎬"

Supported formats: .mp4  .mov  .avi  .mkv  .webm
"""

import argparse
import logging
import os
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

from telegram_uploader import upload_video, log

load_dotenv(Path(__file__).parent / ".env")

WATCH_FOLDER = os.getenv("WATCH_FOLDER", "./watch_inbox")
DONE_FOLDER  = os.getenv("DONE_FOLDER",  "./watch_done")
VIDEO_EXTS   = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


def _is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def _wait_stable(path: Path, stable_secs: float = 2.0, poll: float = 0.5) -> bool:
    """
    Wait until the file size stops changing (i.e. copy/move is complete).
    Returns False if the file disappears before stabilising.
    """
    prev_size = -1
    stable_ticks = 0
    needed = int(stable_secs / poll)

    while True:
        try:
            cur_size = path.stat().st_size
        except FileNotFoundError:
            return False

        if cur_size == prev_size:
            stable_ticks += 1
            if stable_ticks >= needed:
                return True
        else:
            stable_ticks = 0
            prev_size = cur_size

        time.sleep(poll)


class VideoHandler(FileSystemEventHandler):
    def __init__(self, caption: str, done_dir: Path):
        self.caption  = caption
        self.done_dir = done_dir

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not _is_video(path):
            return

        log.info(f"📂 Detected: {path.name}")

        # Wait for file to finish copying
        if not _wait_stable(path):
            log.warning(f"File disappeared before upload: {path.name}")
            return

        try:
            upload_video(path, caption=self.caption)
        except Exception as exc:
            log.error(f"Upload failed for '{path.name}': {exc}")
            return

        # Move to done folder
        if self.done_dir:
            self.done_dir.mkdir(parents=True, exist_ok=True)
            dest = self.done_dir / path.name
            # Handle name collision
            counter = 1
            while dest.exists():
                dest = self.done_dir / f"{path.stem}_{counter}{path.suffix}"
                counter += 1
            try:
                shutil.move(str(path), str(dest))
                log.info(f"📁 Moved to done: {dest.name}")
            except Exception as exc:
                log.warning(f"Could not move file: {exc}")
        else:
            try:
                path.unlink()
                log.info(f"🗑 Deleted: {path.name}")
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Watch folder and auto-upload videos to Telegram.")
    parser.add_argument("--inbox",  default=WATCH_FOLDER, help="Folder to watch (default: ./watch_inbox)")
    parser.add_argument("--done",   default=DONE_FOLDER,  help="Folder for uploaded files (blank = delete)")
    parser.add_argument("--caption", "-c", default="", help="Caption for every uploaded video")
    args = parser.parse_args()

    inbox   = Path(args.inbox).resolve()
    done    = Path(args.done).resolve() if args.done else None

    inbox.mkdir(parents=True, exist_ok=True)
    log.info(f"👀 Watching: {inbox}")
    log.info(f"📁 Done dir: {done or '(delete after upload)'}")
    log.info("Drop any .mp4 / .mov / .avi / .mkv video into the inbox to upload it.")
    log.info("Press Ctrl+C to stop.\n")

    handler  = VideoHandler(caption=args.caption, done_dir=done)
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping watcher …")
        observer.stop()

    observer.join()
    log.info("Watcher stopped.")


if __name__ == "__main__":
    main()
