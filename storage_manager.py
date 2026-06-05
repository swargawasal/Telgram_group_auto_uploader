"""
storage_manager.py
──────────────────
Handles uploading of video tiers to the private Storage Group (-1003762065314)
and maintains an index database (video_index.json) mapping video slugs to
Telegram message IDs.

When a customer pays, the sales bot reads this index and forwards the
stored message ID directly to the customer's chat.
"""

import os
import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional, Any
import requests
from dotenv import load_dotenv

_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)

import sys

def get_bot_token() -> str:
    """Returns the correct bot token depending on which script is running."""
    main_module = sys.modules.get('__main__')
    if main_module and ('sales_bot' in getattr(main_module, '__file__', '') or 'run_bot' in getattr(main_module, '__file__', '')):
        cust_token = os.getenv("TELEGRAM_CUSTOMER_BOT_TOKEN", "").strip()
        if cust_token:
            return cust_token
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

def get_base_url() -> str:
    return f"https://api.telegram.org/bot{get_bot_token()}"

def get_admin_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

def get_admin_base_url() -> str:
    return f"https://api.telegram.org/bot{get_admin_token()}"

def get_customer_token() -> str:
    return os.getenv("TELEGRAM_CUSTOMER_BOT_TOKEN", "").strip()

def get_customer_base_url() -> str:
    return f"https://api.telegram.org/bot{get_customer_token()}"

STORAGE_GROUP_ID = os.getenv("TELEGRAM_STORAGE_GROUP_ID", "").strip()
DISABLE_SSL      = os.getenv("DISABLE_SSL", "false").lower() == "true"
VERIFY           = not DISABLE_SSL
INDEX_FILE       = Path(__file__).parent / "video_index.json"

log = logging.getLogger("StorageManager")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


def load_index() -> Dict[str, Any]:
    """Loads the video database from disk, after checking for updates."""
    restore_database_from_telegram()
    if not INDEX_FILE.exists():
        return {}
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Error loading index file: {e}")
        return {}


def save_index(index_data: Dict[str, Any]):
    """Saves the video database to disk, sorted by slug name."""
    try:
        sorted_index = {k: index_data[k] for k in sorted(index_data.keys())}
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted_index, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Error saving index file: {e}")


def upload_single_tier(file_path: Path, slug: str, tier_name: str) -> int:
    """
    Uploads a single file to the Storage Group.
    Returns the message_id of the uploaded video.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found for upload: {file_path}")
        
    url = f"{get_customer_base_url()}/sendVideo"
    caption = f"📦 <b>TIER STORAGE</b>\n<b>Slug:</b> <code>{slug}</code>\n<b>Tier:</b> <code>{tier_name}</code>\n<b>Size:</b> {file_path.stat().st_size / (1024*1024):.2f} MB"
    
    log.info(f"Uploading {tier_name} for '{slug}' to Storage Group...")
    
    # Standard POST request with file
    with open(file_path, "rb") as f:
        resp = requests.post(
            url,
            data={
                "chat_id": STORAGE_GROUP_ID,
                "caption": caption,
                "supports_streaming": "true",
                "parse_mode": "HTML",
            },
            files={"video": (file_path.name, f, "video/mp4")},
            timeout=300,
            verify=VERIFY
        )
        
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram upload failed: {data.get('description')}")
        
    msg_id = data["result"]["message_id"]
    log.info(f"Stored {tier_name} successfully. message_id: {msg_id}")
    return msg_id


def register_video_tiers(slug: str, tier_paths: Dict[str, Path], caption: str = "") -> Dict[str, int]:
    """
    Uploads all 3 tiers of a video to the Storage Group and registers them in the index.
    Returns a dict mapping tier name -> message_id.
    """
    index = load_index()
    
    # Prepare entry structure
    if slug in index:
        log.warning(f"Slug '{slug}' already exists in index. Will overwrite.")
        
    index[slug] = {
        "caption": caption,
        "half_720": None,
        "half_1080": None,
        "full_1080": None,
        "timestamp": os.path.getmtime(list(tier_paths.values())[0]) if tier_paths else 0
    }
    
    msg_ids = {}
    for tier, path in tier_paths.items():
        if tier not in ["half_720", "half_1080", "full_1080"]:
            continue
        try:
            msg_id = upload_single_tier(path, slug, tier)
            index[slug][tier] = msg_id
            msg_ids[tier] = msg_id
        except Exception as e:
            log.error(f"Failed to upload tier {tier} for {slug}: {e}")
            # Save whatever we have so far
            save_index(index)
            raise e
            
    save_index(index)
    log.info(f"Successfully registered all tiers for '{slug}' in index.")
    return msg_ids


def forward_video_to_buyer(slug: str, tier: str, buyer_chat_id: int | str) -> bool:
    """
    Delivers the stored video to the customer's chat using copyMessage.
    Falls back to forwardMessage if copyMessage fails.
    """
    index = load_index()
    if slug not in index:
        log.error(f"Slug '{slug}' not found in index database.")
        return False
        
    msg_id = index[slug].get(tier)
    if not msg_id:
        log.error(f"Tier '{tier}' for slug '{slug}' is missing from index.")
        return False
        
    # 1. Try copyMessage (clean, no forward header)
    url = f"{get_base_url()}/copyMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": buyer_chat_id,
                "from_chat_id": STORAGE_GROUP_ID,
                "message_id": msg_id
            },
            timeout=15,
            verify=VERIFY
        )
        data = resp.json()
        if data.get("ok"):
            log.info(f"Copied {tier} for '{slug}' to buyer {buyer_chat_id} via Bot")
            return True
        else:
            log.warning(f"Copy failed: {data.get('description')}. Trying forward fallback.")
    except Exception as e:
        log.warning(f"Error copying video: {e}")

    # 2. Try forwardMessage (has forward header, but works as a fallback)
    url = f"{get_base_url()}/forwardMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": buyer_chat_id,
                "from_chat_id": STORAGE_GROUP_ID,
                "message_id": msg_id
            },
            timeout=15,
            verify=VERIFY
        )
        data = resp.json()
        if data.get("ok"):
            log.info(f"Forwarded {tier} for '{slug}' to buyer {buyer_chat_id} via Bot")
            return True
        else:
            log.error(f"Forward failed: {data.get('description')}")
    except Exception as e:
        log.error(f"Error forwarding video: {e}")

    return False


HISTORY_FILE = Path(__file__).parent / "purchase_history.json"

def load_history() -> list:
    """Loads the purchase history from disk, after checking for updates."""
    restore_database_from_telegram()
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Error loading history file: {e}")
        return []

def save_history(history: list):
    """Saves the purchase history to disk, sorted by timestamp."""
    try:
        sorted_history = sorted(history, key=lambda x: x.get("timestamp", 0))
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted_history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Error saving history file: {e}")

def record_submission(user_id: int, username: str, slug: str, tier: str, price: int, txn_id: str) -> None:
    """Inserts a new customer receipt verification record with 'pending' status."""
    history = load_history()
    # Check if there is already a pending submission with the same transaction ID to avoid duplicates
    for record in history:
        if (record.get("user_id") == user_id and 
            record.get("slug") == slug and 
            record.get("tier") == tier and 
            record.get("txn_id") == txn_id and 
            record.get("status") == "pending"):
            log.info(f"Pending submission already exists for user {user_id}, slug {slug}, tier {tier}.")
            return
            
    record = {
        "user_id": user_id,
        "username": username,
        "slug": slug,
        "tier": tier,
        "status": "pending",
        "price": price,
        "txn_id": txn_id,
        "timestamp": int(time.time())
    }
    history.append(record)
    save_history(history)
    log.info(f"Recorded pending submission for user {user_id}, slug {slug}, tier {tier}")

def update_submission_status(user_id: int, slug: str, tier: str, status: str) -> None:
    """Updates a pending submission to approved or rejected."""
    history = load_history()
    updated = False
    # Find the latest pending matching submission and update it
    for record in reversed(history):
        if (record.get("user_id") == user_id and 
            record.get("slug") == slug and 
            record.get("tier") == tier and 
            record.get("status") == "pending"):
            record["status"] = status
            record["timestamp"] = int(time.time())
            updated = True
            break
            
    if updated:
        save_history(history)
        log.info(f"Updated submission status to {status} for user {user_id}, slug {slug}, tier {tier}")
    else:
        log.warning(f"No pending submission found to update for user {user_id}, slug {slug}, tier {tier}")

def check_past_purchase(user_id: int, slug: str) -> Optional[str]:
    """
    Finds if a customer has an existing approved purchase for the requested video.
    Returns the highest tier name (half_720, half_1080, full_1080) purchased, or None.
    """
    history = load_history()
    highest_tier = None
    tier_rank = {"half_720": 1, "half_1080": 2, "full_1080": 3}
    
    for record in history:
        if record.get("user_id") == user_id and record.get("slug") == slug and record.get("status") == "approved":
            t = record.get("tier")
            if t in tier_rank:
                if highest_tier is None or tier_rank[t] > tier_rank[highest_tier]:
                    highest_tier = t
    return highest_tier

LAST_RESTORED_MSG_ID_FILE = Path(__file__).parent / "last_restored_msg_id.txt"

def get_last_restored_msg_id() -> int:
    if not LAST_RESTORED_MSG_ID_FILE.exists():
        return 0
    try:
        with open(LAST_RESTORED_MSG_ID_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0

def set_last_restored_msg_id(msg_id: int):
    try:
        with open(LAST_RESTORED_MSG_ID_FILE, "w") as f:
            f.write(str(msg_id))
    except Exception as e:
        log.error(f"Error saving last restored msg id: {e}")

def _load_index_raw() -> Dict[str, Any]:
    if not INDEX_FILE.exists():
        return {}
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _load_history_raw() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def restore_database_from_telegram(force: bool = False) -> bool:
    """
    Checks the STORAGE_GROUP_ID pinned message.
    If it is a database backup file, downloads it and restores video_index.json and purchase_history.json.
    """
    token = get_admin_token()
    if not token or not STORAGE_GROUP_ID:
        return False
        
    url = f"{get_admin_base_url()}/getChat"
    try:
        resp = requests.post(url, data={"chat_id": STORAGE_GROUP_ID}, timeout=15, verify=VERIFY).json()
        if not resp.get("ok"):
            return False
            
        chat = resp.get("result", {})
        pinned_msg = chat.get("pinned_message")
        if not pinned_msg:
            return False
            
        document = pinned_msg.get("document")
        if not document or document.get("file_name") != "database_backup.json":
            return False
            
        msg_id = pinned_msg.get("message_id")
        if not force and msg_id == get_last_restored_msg_id():
            # Already up to date!
            return True
            
        file_id = document.get("file_id")
        file_info_url = f"{get_admin_base_url()}/getFile"
        file_info_resp = requests.post(file_info_url, data={"file_id": file_id}, timeout=15, verify=VERIFY).json()
        if not file_info_resp.get("ok"):
            return False
            
        file_path = file_info_resp["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        
        download_resp = requests.get(download_url, timeout=30, verify=VERIFY)
        if download_resp.status_code != 200:
            return False
            
        backup_data = download_resp.json()
        
        # Save files locally without calling save_index/save_history to avoid recursion
        video_index = backup_data.get("video_index", {})
        sorted_index = {k: video_index[k] for k in sorted(video_index.keys())}
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted_index, f, indent=2, ensure_ascii=False)
            
        purchase_history = backup_data.get("purchase_history", [])
        sorted_history = sorted(purchase_history, key=lambda x: x.get("timestamp", 0))
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted_history, f, indent=2, ensure_ascii=False)
            
        set_last_restored_msg_id(msg_id)
        log.info(f"Successfully sync-restored database from pinned message {msg_id}")
        return True
    except Exception as e:
        log.error(f"Error sync-restoring database: {e}")
        return False


def backup_database_to_telegram() -> bool:
    """
    Loads video_index.json and purchase_history.json, combines them,
    uploads database_backup.json to Storage Group, and pins the message.
    """
    token = get_admin_token()
    if not token or not STORAGE_GROUP_ID:
        log.warning("Admin Bot token or STORAGE_GROUP_ID is missing. Cannot backup database.")
        return False
        
    try:
        log.info("Preparing database backup...")
        video_index = _load_index_raw()
        purchase_history = _load_history_raw()
        
        backup_data = {
            "video_index": video_index,
            "purchase_history": purchase_history
        }
        
        backup_file = Path(__file__).parent / "database_backup.json"
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, indent=2, ensure_ascii=False)
            
        # Upload file to Telegram
        url = f"{get_admin_base_url()}/sendDocument"
        caption = f"💾 <b>DATABASE BACKUP</b>\n🕒 Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
        
        log.info("Uploading database_backup.json to Storage Group...")
        with open(backup_file, "rb") as f:
            resp = requests.post(
                url,
                data={
                    "chat_id": STORAGE_GROUP_ID,
                    "caption": caption,
                    "parse_mode": "HTML"
                },
                files={"document": ("database_backup.json", f, "application/json")},
                timeout=60,
                verify=VERIFY
            ).json()
            
        # Clean up local backup file
        try:
            backup_file.unlink()
        except Exception:
            pass
            
        if not resp.get("ok"):
            log.error(f"Failed to upload backup: {resp.get('description')}")
            return False
            
        msg_id = resp["result"]["message_id"]
        log.info(f"Backup uploaded. message_id: {msg_id}. Pinning message...")
        
        # Pin the uploaded message
        pin_url = f"{get_admin_base_url()}/pinChatMessage"
        pin_resp = requests.post(
            pin_url,
            data={
                "chat_id": STORAGE_GROUP_ID,
                "message_id": msg_id,
                "disable_notification": "true"
            },
            timeout=15,
            verify=VERIFY
        ).json()
        
        if pin_resp.get("ok"):
            log.info("Successfully pinned the latest database backup!")
            set_last_restored_msg_id(msg_id)
            return True
        else:
            log.error(f"Failed to pin backup message: {pin_resp.get('description')}")
            return False
            
    except Exception as e:
        log.error(f"Error backing up database to Telegram: {e}")
        return False


if __name__ == "__main__":
    # Test connection and database loading
    idx = load_index()
    print(f"Loaded index database. Registered videos: {list(idx.keys())}")
    
    # Try testing storage group accessibility
    print("Testing storage group access...")
    try:
        resp = requests.post(
            f"{get_base_url()}/sendMessage",
            data={"chat_id": STORAGE_GROUP_ID, "text": "🤖 Storage Manager initialized and connected."},
            timeout=10,
            verify=VERIFY
        ).json()
        if resp.get("ok"):
            print("OK! Storage group reachable.")
        else:
            print(f"FAIL: {resp.get('description')}")
    except Exception as e:
        print(f"Error: {e}")
