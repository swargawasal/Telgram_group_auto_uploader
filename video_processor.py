"""
video_processor.py
──────────────────
Processes high-quality (1080p) source videos into three distinct tiers:
1. HALF: Half duration, 720p quality (₹59)
2. FULL: Full duration, 720p quality (₹100)
3. HD: Full duration, 1080p quality (₹150) — original or copy.

Usage:
  from video_processor import process_video_tiers
  tiers = process_video_tiers("source.mp4")
"""

import os
import sys
import json
import subprocess
import logging
from pathlib import Path
from typing import Dict, Tuple

log = logging.getLogger("VideoProcessor")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


def get_video_metadata(path: Path) -> Tuple[float, int, int]:
    """
    Returns (duration_in_seconds, width, height) using ffprobe.
    Raises exception on error.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=width,height",
        "-of", "json",
        str(path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        data = json.loads(result.stdout)
        
        duration = float(data["format"]["duration"])
        
        # Find video stream width/height
        width = 1920
        height = 1080
        for stream in data.get("streams", []):
            if "width" in stream and "height" in stream:
                width = int(stream["width"])
                height = int(stream["height"])
                break
                
        return duration, width, height
    except Exception as e:
        log.error(f"Failed to probe video {path}: {e}")
        raise RuntimeError(f"Could not read video metadata: {e}")


def process_video_tiers(video_path: str | Path, output_dir: str | Path = None) -> Dict[str, Path]:
    """
    Takes a source video path and creates the 3 tiers.
    Returns a dictionary of paths:
      {
         "half_720": Path,
         "full_720": Path,
         "full_1080": Path
      }
    """
    source = Path(video_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source video not found: {source}")

    if output_dir is None:
        output_dir = source.parent / "output_tiers"
    else:
        output_dir = Path(output_dir).resolve()
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Get metadata
    duration, w, h = get_video_metadata(source)
    half_duration = duration / 2.0
    
    log.info(f"Processing '{source.name}' | Duration: {duration:.2f}s | Resolution: {w}x{h}")
    
    # Determine the scaling filter for 720p preserving aspect ratio
    # Landscape: scale width to fit height=720 (scale=-2:720)
    # Portrait: scale height to fit width=720 (scale=720:-2)
    if h >= w:
        scale_filter = "scale=720:-2"
        log.info("Portrait detected. Target 720p width.")
    else:
        scale_filter = "scale=-2:720"
        log.info("Landscape detected. Target 720p height.")
        
    stem = source.stem
    
    paths = {
        "half_720": output_dir / f"{stem}_half_720p.mp4",
        "half_1080": output_dir / f"{stem}_half_1080p.mp4",
        "full_1080": output_dir / f"{stem}_full_1080p.mp4"
    }

    # Common FFmpeg settings for quality/speed tradeoff
    # crf=26 is excellent for 720p mobile deliveries (small size, looks great)
    
    # --- Tier 1: Half Duration, 720p ---
    log.info(f"Generating Tier 1: HALF 720p ({half_duration:.2f}s)...")
    cmd_half = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-t", f"{half_duration:.3f}",
        "-vf", scale_filter,
        "-c:v", "libx264", "-preset", "faster", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k",
        str(paths["half_720"])
    ]
    subprocess.run(cmd_half, capture_output=True, check=True)
    log.info(f"Tier 1 created: {paths['half_720'].name} ({paths['half_720'].stat().st_size / (1024*1024):.2f} MB)")

    # --- Tier 2: Half Duration, 1080p ---
    log.info(f"Generating Tier 2: HALF 1080p ({half_duration:.2f}s)...")
    cmd_half_1080 = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-t", f"{half_duration:.3f}",
        "-c:v", "libx264", "-preset", "faster", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        str(paths["half_1080"])
    ]
    subprocess.run(cmd_half_1080, capture_output=True, check=True)
    log.info(f"Tier 2 created: {paths['half_1080'].name} ({paths['half_1080'].stat().st_size / (1024*1024):.2f} MB)")

    # --- Tier 3: Full Duration, 1080p (Original) ---
    log.info("Generating Tier 3: FULL 1080p (Original / Optimized)...")
    # We copy or slightly optimize to ensure it's compatible with all players
    # Standard fast start and format check:
    cmd_full_1080 = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-c", "copy", # direct stream copy is fast and preserves original quality
        "-movflags", "+faststart",
        str(paths["full_1080"])
    ]
    try:
        subprocess.run(cmd_full_1080, capture_output=True, check=True)
    except subprocess.CalledProcessError:
        # Fallback if copy fails (e.g. format transcode needed)
        log.warning("Direct stream copy failed, fallback to transcoding original...")
        cmd_fallback = [
            "ffmpeg", "-y",
            "-i", str(source),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(paths["full_1080"])
        ]
        subprocess.run(cmd_fallback, capture_output=True, check=True)
        
    log.info(f"Tier 3 created: {paths['full_1080'].name} ({paths['full_1080'].stat().st_size / (1024*1024):.2f} MB)")

    return paths


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python video_processor.py <path_to_video>")
        sys.exit(1)
        
    try:
        res = process_video_tiers(sys.argv[1])
        print("Successfully processed tiers:")
        for k, v in res.items():
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
