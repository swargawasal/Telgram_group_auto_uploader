import os
import subprocess
import sys
from pathlib import Path

# Add parent dir to path so we can import telegram_uploader
sys.path.append(str(Path(__file__).parent))
import telegram_uploader

INPUT_VIDEO = r"C:\Users\midhunkrishnapv\Downloads\joslyn james.mp4"
OUTPUT_VIDEO = str(Path(__file__).parent / "joslyn_james_720p_hook.mp4")
CHAT_ID = "-1003925595316"

def get_duration(video_path: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except Exception as e:
        print(f"Error getting duration: {e}")
        return 0.0

def main():
    print(f"Checking if input video exists: {INPUT_VIDEO}")
    if not os.path.exists(INPUT_VIDEO):
        print(f"Error: Input video not found at {INPUT_VIDEO}")
        sys.exit(1)

    duration = get_duration(INPUT_VIDEO)
    if duration <= 0:
        print("Error: Could not retrieve video duration.")
        sys.exit(1)

    half_duration = duration / 2
    print(f"Original duration: {duration:.2f}s | Half duration: {half_duration:.2f}s")

    print("Processing video with FFmpeg: cutting to half duration and scaling to 720p...")
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-ss", "0",
        "-t", f"{half_duration:.3f}",
        "-i", INPUT_VIDEO,
        "-vf", "scale=-2:720",
        "-c:v", "libx264",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        OUTPUT_VIDEO
    ]
    
    try:
        subprocess.run(ffmpeg_cmd, check=True)
        print(f"Successfully created preview video: {OUTPUT_VIDEO}")
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg error: {e}")
        sys.exit(1)

    caption = (
        "🔥 <b>EXCLUSIVE: Joslyn James — Leaked Action Tape!</b> 🔥\n\n"
        "She thought this private video would never get out... but we've got the uncut footage! 👀\n\n"
        "Watch the 720p half-duration preview above 👆\n"
        "Choose your package to unlock the full action:\n"
        "🔹 <b>720p Half Duration Video</b> — ₹59 (sent above)\n"
        "🔹 <b>720p Full Duration Video</b> — ₹100\n"
        "🔹 <b>1080p Full Duration (Ultra HD) Video</b> — ₹150\n\n"
        "👇 <b>DM Admin to purchase and get instant access!</b> 👇"
    )

    print(f"Uploading preview to Telegram chat: {CHAT_ID}...")
    try:
        msg_ids = telegram_uploader.upload_video(
            video_path=OUTPUT_VIDEO,
            caption=caption,
            chat_id=CHAT_ID,
            auto_split=False
        )
        print(f"Success! Uploaded successfully. Message IDs: {msg_ids}")
    except Exception as e:
        print(f"Failed to upload: {e}")
    finally:
        # Clean up temporary output video file
        if os.path.exists(OUTPUT_VIDEO):
            try:
                os.remove(OUTPUT_VIDEO)
                print(f"Cleaned up temporary file: {OUTPUT_VIDEO}")
            except Exception as e:
                print(f"Failed to delete temporary file: {e}")

if __name__ == "__main__":
    main()
