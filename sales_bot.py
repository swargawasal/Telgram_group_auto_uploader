"""
sales_bot.py
────────────
The always-on sales assistant bot. Handles:
- Customer interaction, showing pricing tiers (₹59 / ₹100 / ₹150)
- Explaining the "no bargaining, buy it or lose it" policy
- Parsing payment screenshots via Gemini Vision API
- Forwarding receipts to the storage group for admin approval/rejection
- Instantly delivering the correct video tier to the buyer upon approval
- AI chat capabilities using Groq / Mistral for customer queries.
"""

import os
import time
import json
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional

from PIL import Image
from dotenv import load_dotenv

# API imports
from telegram_gemini_router import gemini_router
from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Local imports
from storage_manager import (
    load_index,
    forward_video_to_buyer,
    check_past_purchase,
    record_submission,
    update_submission_status,
    restore_database_from_telegram,
    backup_database_to_telegram
)
from ai_helper import (
    generate_tempting_hook,
    answer_user_query_ai,
    get_clean_actress_name,
    suggest_next_slug,
    clean_html_for_telegram,
    generate_bargain_response
)

# ── Load config ──────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)

BOT_TOKEN          = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()        # Admin/Storage bot
CUSTOMER_BOT_TOKEN = os.getenv("TELEGRAM_CUSTOMER_BOT_TOKEN", "").strip()  # Customer-facing bot
STORAGE_GROUP_ID   = os.getenv("TELEGRAM_STORAGE_GROUP_ID", "").strip()
TRAILER_GROUP_ID   = os.getenv("TELEGRAM_GROUP_ID", "").strip()
ADMIN_IDS          = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
UPI_ID             = os.getenv("UPI_ID", "midhunkrishna@upi").strip()

def get_bot_id_from_token(token: str) -> Optional[int]:
    try:
        return int(token.split(":")[0])
    except Exception:
        return None

ADMIN_BOT_ID = get_bot_id_from_token(BOT_TOKEN)
CUSTOMER_BOT_ID = get_bot_id_from_token(CUSTOMER_BOT_TOKEN)

GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "").strip()
MISTRAL_API_KEY    = os.getenv("MISTRAL_API_KEY", "").strip()

PRICE_HALF_720     = int(os.getenv("PRICE_HALF_720", "0"))
PRICE_HALF_1080    = int(os.getenv("PRICE_HALF_1080", "59"))
PRICE_FULL_1080    = int(os.getenv("PRICE_FULL_1080", "149"))

# Group the customer must join before getting the free 720p preview
FREE_PREVIEW_GROUP_ID = os.getenv("FREE_PREVIEW_GROUP_ID", "").strip()
# Convenience: admin bot base URL for direct requests to storage group
_ADMIN_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Customer Bot username cache and resolver
CUSTOMER_BOT_USERNAME = None

async def get_customer_bot_username(context_bot=None) -> str:
    global CUSTOMER_BOT_USERNAME
    if CUSTOMER_BOT_USERNAME:
        return CUSTOMER_BOT_USERNAME
    try:
        from telegram import Bot
        cust_bot = Bot(CUSTOMER_BOT_TOKEN)
        me = await cust_bot.get_me()
        CUSTOMER_BOT_USERNAME = me.username
        return CUSTOMER_BOT_USERNAME
    except Exception as e:
        log.error(f"Error fetching customer bot username: {e}")
        if context_bot:
            me = await context_bot.get_me()
            return me.username
        return "bot"

# Gemini configured via telegram_gemini_router

# Admin bot HTTP helper — for all Storage Group operations
import requests as _requests

def admin_send_photo(chat_id: str, photo_path: str, caption: str, reply_markup_json: str) -> int:
    """
    Sends a photo to a chat using the ADMIN bot (BOT_TOKEN) directly via HTTP.
    Returns the message_id of the sent message.
    """
    import json
    url = f"{_ADMIN_API_BASE}/sendPhoto"
    with open(photo_path, "rb") as f:
        resp = _requests.post(
            url,
            data={
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": reply_markup_json
            },
            files={"photo": f},
            timeout=30
        ).json()
    if not resp.get("ok"):
        raise RuntimeError(f"Admin bot send_photo failed: {resp.get('description')}")
    return resp["result"]["message_id"]

def admin_edit_caption(chat_id: str, message_id: int, new_caption: str) -> bool:
    """
    Edits the caption of a message in a chat using the ADMIN bot directly via HTTP.
    Used to update approval/rejection status on the Storage Group message.
    """
    url = f"{_ADMIN_API_BASE}/editMessageCaption"
    resp = _requests.post(
        url,
        data={
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": new_caption,
            "parse_mode": "HTML"
        },
        timeout=15
    ).json()
    return resp.get("ok", False)

def admin_send_message(chat_id: str, text: str) -> bool:
    """Sends a text message to a chat using the ADMIN bot directly via HTTP."""
    url = f"{_ADMIN_API_BASE}/sendMessage"
    resp = _requests.post(
        url,
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15
    ).json()
    return resp.get("ok", False)

# Configure Groq
groq_client = None
if GROQ_API_KEY:
    groq_client = Groq(api_key=GROQ_API_KEY)

# Logging
log = logging.getLogger("SalesBot")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# In-memory session tracking: user_id -> { "slug": str, "tier": str, "price": int }
USER_SESSIONS: Dict[int, Dict[str, Any]] = {}

# Seductive Upgrade Bargaining sessions
BARGAIN_SESSIONS: Dict[int, Dict[str, Any]] = {}

UPGRADE_PRICING = {
    ("half_720", "half_1080"): {
        "target": 40,
        "standard_diff": 59,
        "steps": {1: 55, 2: 50, 3: 46, 4: 42, 5: 40}
    },
    ("half_720", "full_1080"): {
        "target": 100,
        "standard_diff": 149,
        "steps": {1: 139, 2: 129, 3: 119, 4: 109, 5: 100}
    },
    ("half_1080", "full_1080"): {
        "target": 60,
        "standard_diff": 90,
        "steps": {1: 85, 2: 78, 3: 72, 4: 66, 5: 60}
    }
}


# ── AI Processing Helpers ─────────────────────────────────────────────────────

async def check_screenshot_with_gemini(image_path: Path, expected_amount: int) -> Dict[str, Any]:
    """
    Asks Gemini Vision if the screenshot is a successful UPI transaction of the expected amount.
    Returns parsed JSON result.
    """
    if not GEMINI_API_KEY:
        log.warning("No GEMINI_API_KEY. Skipping Gemini screenshot check.")
        return {"is_successful": True, "amount": expected_amount, "txn_id": "GEMINI_DISABLED"}

    try:
        img = Image.open(image_path)
        
        prompt = f"""
        Analyze this payment screenshot.
        We are expecting a UPI payment of exactly ₹{expected_amount} INR.
        
        Check carefully:
        1. Is it a successful payment transaction (from apps like GPay, Paytm, PhonePe, YONO, BHIM)?
        2. What is the transaction/ref/UPI transaction ID if visible?
        3. What is the paid amount in INR?
        
        Respond ONLY with a valid JSON block containing:
        - "is_successful": true or false
        - "amount": the number paid (or null if not found)
        - "txn_id": "the transaction string" (or null if not found)
        
        Do not wrap the response in markdown blocks like ```json, just output raw JSON text.
        """
        
        # Run in thread pool to prevent blocking asyncio loop
        loop = asyncio.get_running_loop()
        response_text = await loop.run_in_executor(
            None, 
            lambda: gemini_router.generate(
                task_type="vision",
                prompt=[prompt, img],
                module_name="sales_bot",
                model_name="gemini-2.5-flash"
            )
        )
        
        if not response_text:
            log.error("Gemini Governor returned empty or None response.")
            return {"is_successful": False, "amount": None, "txn_id": "Error parsing"}
            
        text = response_text.strip()
        # Clean potential markdown wrapping if present
        if text.startswith("```"):
            text = text.replace("```json", "", 1).replace("```", "", 1).strip()
            
        data = json.loads(text)
        log.info(f"Gemini verification result: {data}")
        return data
    except Exception as e:
        log.error(f"Error calling Gemini Vision: {e}")
        # Default safe fallback: let manual admin confirm
        return {"is_successful": False, "amount": None, "txn_id": "Error parsing"}


import re

def extract_price_from_text(text: str) -> Optional[int]:
    """Finds the first integer in the text."""
    numbers = re.findall(r'\b\d+\b', text)
    if numbers:
        return int(numbers[0])
    return None

def is_acceptance(text: str) -> bool:
    """Checks if the user types an acceptance word like deal, ok, done etc."""
    text_lower = text.lower()
    keywords = ["deal", "ok", "okay", "accept", "fine", "yes", "agree", "sure", "done", "perfect", "pay"]
    for word in keywords:
        if re.search(r'\b' + re.escape(word) + r'\b', text_lower):
            return True
    return False

async def check_group_membership(bot, user_id: int) -> bool:
    """
    Returns True if the user is a member (or admin/owner) of FREE_PREVIEW_GROUP_ID.
    Returns False if not a member, or if the group ID is not configured.
    """
    if not FREE_PREVIEW_GROUP_ID:
        # No gate configured — allow everyone
        return True
    try:
        member = await bot.get_chat_member(chat_id=int(FREE_PREVIEW_GROUP_ID), user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning(f"Could not check group membership for user {user_id}: {e}")
        # If we can't check (bot not in group, etc.) — block until fixed
        return False

async def process_bargain_message(user_id: int, user_text: str) -> str:
    """
    Manages active bargaining session state for user_id.
    Validates user offers, queries AI for actress response, and handles early/final deal locks.
    """
    session = BARGAIN_SESSIONS.get(user_id)
    if not session:
        return "Mmm, start fresh by choosing a tier from a link baby... 😉"
        
    session["message_count"] += 1
    count = session["message_count"]
    slug = session["slug"]
    past_tier = session["past_tier"]
    target_tier = session["target_tier"]
    target_price = session["target_price"]
    standard_diff = session["standard_diff"]
    label = session["label"]
    
    full_name, short_name = get_clean_actress_name(slug)
    
    # 1. Parse user input
    offered_price = extract_price_from_text(user_text)
    accepted = is_acceptance(user_text)
    
    # Check if they offered or accepted early
    if offered_price is not None:
        if offered_price >= target_price:
            # Seductive acceptance of their offer
            final_price = offered_price
            # Lock the deal
            USER_SESSIONS[user_id] = {
                "slug": slug,
                "tier": target_tier,
                "price": final_price,
                "label": label,
                "is_negotiated": True
            }
            BARGAIN_SESSIONS.pop(user_id, None)
            return (
                f"Mmm, I like your style, baby... ₹{final_price} it is! 😘 Deal closed.\n\n"
                f"💳 <b>Payment Details:</b>\n"
                f"Send exactly <b>₹{final_price}</b> to UPI ID:\n"
                f"👉 <code>{UPI_ID}</code>\n\n"
                f"Upload the screenshot here so I can unlock the vault and send your premium video! 😈🔥"
            )
        else:
            # Offered price is lower than target price. Reject it and continue.
            pass
            
    elif accepted:
        # User accepted the deal. They pay the last counter price.
        final_price = session["current_counter"]
        USER_SESSIONS[user_id] = {
            "slug": slug,
            "tier": target_tier,
            "price": final_price,
            "label": label,
            "is_negotiated": True
        }
        BARGAIN_SESSIONS.pop(user_id, None)
        return (
            f"Mmm, deal! 😘 Let's lock it at ₹{final_price}.\n\n"
            f"💳 <b>Payment Details:</b>\n"
            f"Send exactly <b>₹{final_price}</b> to UPI ID:\n"
            f"👉 <code>{UPI_ID}</code>\n\n"
            f"Upload the screenshot here so I can unlock the vault and send your premium video! 😈🔥"
        )
        
    # 2. Check if we reached message 5 (final lock)
    if count >= 5:
        final_price = target_price
        USER_SESSIONS[user_id] = {
            "slug": slug,
            "tier": target_tier,
            "price": final_price,
            "label": label,
            "is_negotiated": True
        }
        BARGAIN_SESSIONS.pop(user_id, None)
        return (
            f"Alright baby, no more games. Let's make this happen. My final price is ₹{final_price} — "
            f"you're getting an absolute steal! 😘\n\n"
            f"💳 <b>Payment Details:</b>\n"
            f"Send exactly <b>₹{final_price}</b> to UPI ID:\n"
            f"👉 <code>{UPI_ID}</code>\n\n"
            f"Upload the screenshot here so I can unlock the vault and send your premium video! 😈🔥"
        )
        
    # 3. Message 1-4: Generate counter offer using AI
    # Find next price to counter with from step plan
    upgrade_key = (past_tier, target_tier)
    pricing = UPGRADE_PRICING.get(upgrade_key, {})
    steps = pricing.get("steps", {})
    next_price = steps.get(count, target_price)
    session["current_counter"] = next_price
    
    # Query AI for dynamic response in character
    response = await generate_bargain_response(
        user_msg=user_text,
        slug=slug,
        past_tier=past_tier,
        target_tier=target_tier,
        standard_diff=standard_diff,
        next_price=next_price,
        chat_history=session["messages"]
    )
    
    # Update chat history
    session["messages"].append({"role": "user", "content": user_text})
    session["messages"].append({"role": "assistant", "content": response})
    
    return response


async def answer_user_query(user_msg: str, user_id: int) -> str:
    """Answers customer inquiries using Groq Llama 3, Mistral, or Gemini, adopting actress persona if session is active."""
    session = USER_SESSIONS.get(user_id)
    slug = session.get("slug") if session else None
    return await answer_user_query_ai(user_msg, slug)


# ── Telegram Handlers ────────────────────────────────────────────────────────

async def admin_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command for the Admin Bot. Only responds to configured admins."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
        
    await update.message.reply_text(
        "🛠️ <b>Admin Bot Panel Active</b>\n\n"
        "• <b>Approve/Reject buttons</b> in the storage group are handled here automatically.\n"
        "• <b>To index new videos/documents:</b> Upload/forward the video or document file here (or in the Customer Bot DM). The system will automatically handle uploading and registering it via the Customer Bot.\n"
        "• <b>To post trailers:</b> Upload/forward the trailer here (or in the Customer Bot DM).",
        parse_mode="HTML"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Triggered when user opens bot.
    If deep linked (e.g. /start slug), it sets up active session for that video.
    """
    args = context.args
    slug = args[0] if args else None
    
    index = load_index()
    
    if not slug:
        await update.message.reply_text(
            "👋 Welcome to the Premium Video Bot!\n\n"
            "Browse our trailer group, click a **Buy Now** button, and you will be redirected here automatically."
        )
        return

    if slug not in index:
        await update.message.reply_text(
            "⚠️ Video not found or expired. Please check the link and try again."
        )
        return
        
    # Setup session
    USER_SESSIONS[update.effective_user.id] = {
        "slug": slug,
        "caption": index[slug].get("caption", "Video Clip"),
        "tier": None,
        "price": None
    }
    
    keyboard = [
        [InlineKeyboardButton("🎥 Preview Video (720p) — FREE", callback_data=f"buy_half_{slug}")],
        [InlineKeyboardButton(f"🎬 Half Video (1080p) — ₹{PRICE_HALF_1080}", callback_data=f"buy_half1080_{slug}")],
        [InlineKeyboardButton(f"🌟 Full HD Video (1080p) — ₹{PRICE_FULL_1080}", callback_data=f"buy_full1080_{slug}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    video_caption = index[slug].get('caption', slug)
    await update.message.reply_text(
        f"🛒 <b>Choose Your Quality Tier:</b>\n"
        f"📝 <i>{video_caption}</i>\n\n"
        f"Select the version you'd like to purchase below:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def tier_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles tier button click."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = update.effective_user.id
    
    if not data.startswith("buy_"):
        return
        
    parts = data.split("_")
    # Format: buy_{tier}_{slug}
    tier_code = parts[1]
    slug = "_".join(parts[2:])
    
    # Handle legacy buy_full720_{slug} callback data gracefully
    if tier_code == "full720":
        await query.edit_message_text(
            "⚠️ <b>Notice:</b> The 720p Full video tier has been discontinued.\n"
            "Please select one of our active tiers instead:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎥 Preview Video (720p) — FREE", callback_data=f"buy_half_{slug}")],
                [InlineKeyboardButton("🎬 Half Video (1080p) — ₹59", callback_data=f"buy_half1080_{slug}")],
                [InlineKeyboardButton("🌟 Full HD Video (1080p) — ₹149", callback_data=f"buy_full1080_{slug}")],
            ]),
            parse_mode="HTML"
        )
        return

    tier_map = {
        "half": ("half_720", PRICE_HALF_720, "Half Video (720p) [FREE]"),
        "half1080": ("half_1080", PRICE_HALF_1080, "Half Video (1080p)"),
        "full1080": ("full_1080", PRICE_FULL_1080, "Full HD Video (1080p)")
    }
    
    if tier_code not in tier_map:
        return
        
    db_tier, price, label = tier_map[tier_code]
    
    # FREE Tier (half_720) — Gate: must be a member of FREE_PREVIEW_GROUP_ID
    if db_tier == "half_720":
        username = update.effective_user.username or update.effective_user.first_name or "Unknown"

        # Check if customer has already received the free preview for this slug
        past_free = check_past_purchase(user_id, slug)
        if past_free is not None:
            # Already got it — re-deliver the highest tier they own
            tier_label = "free preview" if past_free == "half_720" else "purchased video"
            await query.edit_message_text(
                f"🎁 <b>You already have this!</b>\nRe-sending your {tier_label} now... 😉",
                parse_mode="HTML"
            )
            forward_video_to_buyer(slug, past_free, user_id)
            return

        # Membership gate: must join the group first
        is_member = await check_group_membership(context.bot, user_id)
        if not is_member:
            join_link = f"https://t.me/c/{str(FREE_PREVIEW_GROUP_ID).lstrip('-100')}"
            try:
                await query.edit_message_text(
                    "🔒 <b>Join to Unlock the FREE Preview!</b>\n\n"
                    "To get your free 720p preview clip, you need to join our exclusive members group first.\n\n"
                    "1️⃣ Tap <b>Join Group</b> below\n"
                    "2️⃣ Then tap <b>✅ I Joined — Give Me the Preview</b>\n\n"
                    "It's free, baby... just one little tap away 😉🔥",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔗 Join Group", url=join_link)],
                        [InlineKeyboardButton("✅ I Joined — Give Me the Preview", callback_data=f"buy_half_{slug}")]
                    ]),
                    parse_mode="HTML"
                )
            except Exception as e:
                from telegram.error import BadRequest
                if isinstance(e, BadRequest) and "Message is not modified" in str(e):
                    # Show alert toast popup to user
                    await query.answer("⚠️ You haven't joined the group yet! Please join first.", show_alert=True)
                else:
                    raise
            return

        # Membership confirmed — deliver the free preview!
        await query.edit_message_text(
            "🎁 <b>Welcome to the club!</b>\nDelivering your FREE Half Video (720p) now... 😈🔥",
            parse_mode="HTML"
        )
        record_submission(user_id, username, slug, "half_720", 0, "FREE")
        update_submission_status(user_id, slug, "half_720", "approved")
        success = forward_video_to_buyer(slug, "half_720", user_id)
        if not success:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ There was an issue retrieving the free preview. An administrator has been notified."
            )
        return

    # Check if the user has a past approved purchase for the same slug
    past_tier = check_past_purchase(user_id, slug)
    if past_tier is not None:
        tier_rank = {"half_720": 1, "half_1080": 2, "full_1080": 3}
        if tier_rank.get(db_tier, 0) <= tier_rank.get(past_tier, 0):
            # Already purchased equal or higher tier
            await query.edit_message_text(
                f"✨ <b>You already purchased this quality (or a higher one)!</b>\n"
                f"Forwarding the file to you now...",
                parse_mode="HTML"
            )
            # Forward the purchased video
            success = forward_video_to_buyer(slug, past_tier, user_id)
            if not success:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ There was an issue retrieving the file. An administrator has been notified."
                )
            return
            
        # Eligible for upgrade bargaining!
        upgrade_key = (past_tier, db_tier)
        if upgrade_key in UPGRADE_PRICING:
            pricing = UPGRADE_PRICING[upgrade_key]
            target_price = pricing["target"]
            standard_diff = pricing["standard_diff"]
            
            # Start bargaining session
            BARGAIN_SESSIONS[user_id] = {
                "slug": slug,
                "past_tier": past_tier,
                "target_tier": db_tier,
                "target_price": target_price,
                "standard_diff": standard_diff,
                "message_count": 0,
                "messages": [],
                "current_counter": pricing["steps"][1],
                "label": label
            }
            
            full_name, short_name = get_clean_actress_name(slug)
            opening_msg = (
                f"Mmm, hey there... 😉 I see you already have my preview clip, but now you want the full uncut action? "
                f"I love a man who can't get enough of me... 😈\n\n"
                f"The standard price difference to upgrade to my {label} is ₹{standard_diff}. "
                f"But if you tease me nicely, maybe we can make a deal... 😘 What's your offer, baby?"
            )
            
            # Save the opening message in character context for AI to follow
            BARGAIN_SESSIONS[user_id]["messages"].append({
                "role": "assistant",
                "content": opening_msg
            })
            
            await query.edit_message_text(opening_msg)
            return

    # Store or update session
    USER_SESSIONS[user_id] = {
        "slug": slug,
        "tier": db_tier,
        "price": price,
        "label": label
    }
    
    instructions = (
        f"💎 <b>Order details:</b> {label}\n"
        f"💰 <b>Total Price:</b> ₹{price} (Fixed. No Bargains)\n\n"
        f"💳 <b>Payment Instructions:</b>\n"
        f"1. Send exactly <b>₹{price}</b> to UPI ID:\n"
        f"   👉 <code>{UPI_ID}</code>\n"
        f"2. Take a screenshot showing <b>SUCCESSFUL</b> status.\n"
        f"3. <b>Send/Upload the screenshot directly in this chat.</b>\n\n"
        f"⚠️ <i>Please do not send cropped or edited screenshots. The bot uses Gemini AI to verify.</i>"
    )
    
    await query.edit_message_text(instructions, parse_mode="HTML")



async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming photos (payment screenshots)."""
    user_id = update.effective_user.id
    session = USER_SESSIONS.get(user_id)
    
    if not session or not session.get("tier"):
        await update.message.reply_text(
            "⚠️ Please select a video and tier first by clicking a Buy link in the group."
        )
        return
        
    # User has sent a screenshot for an active session
    expected_price = session["price"]
    slug = session["slug"]
    tier = session["tier"]
    
    wait_msg = await update.message.reply_text(
        "⏳ <b>Payment screenshot received!</b>\n"
        "Verifying transaction with Gemini AI...",
        parse_mode="HTML"
    )
    
    try:
        # Download screenshot
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        temp_dir = Path(__file__).parent / "temp_receipts"
        temp_dir.mkdir(exist_ok=True)
        img_path = temp_dir / f"{user_id}_{int(time.time())}.jpg"
        
        await file.download_to_drive(str(img_path))
        
        # Verify via Gemini Vision
        verification = await check_screenshot_with_gemini(img_path, expected_price)
        
        is_ok = verification.get("is_successful", False)
        detected_amount = verification.get("amount")
        txn_id = verification.get("txn_id", "Not found")
        
        # Check amount match (if detected)
        amount_match = True
        if detected_amount is not None:
            # Tolerant matching: sometimes Gemini misreads numbers slightly or user pays different
            if int(detected_amount) != expected_price:
                amount_match = False
                
        # Forward details to Storage Group for Owner confirmation
        username = f"@{update.effective_user.username}" if update.effective_user.username else f"ID {user_id}"
        
        # Record the submission in history
        record_submission(user_id, username, slug, tier, expected_price, txn_id)
        
        is_negotiated = session.get("is_negotiated", False)
        admin_caption = (
            f"🔔 <b>NEW ORDER SUBMISSION</b>\n\n"
            f"👤 <b>Customer:</b> {username} (<code>{user_id}</code>)\n"
            f"📦 <b>Video Slug:</b> <code>{slug}</code>\n"
            f"💎 <b>Requested Tier:</b> <code>{tier}</code> (Price: ₹{expected_price})\n"
            f"⚡ <b>Bargained Upgrade:</b> {'✅ Yes' if is_negotiated else '❌ No'}\n\n"
            f"🤖 <b>Gemini Vision Report:</b>\n"
            f"• Verified Success: {'✅ Yes' if is_ok else '❌ No'}\n"
            f"• Detected Amount: ₹{detected_amount if detected_amount else 'Unknown'}\n"
            f"• Txn Ref ID: <code>{txn_id}</code>\n"
            f"• Price Match: {'✅ Yes' if amount_match else '❌ NO MATCH'}\n"
        )
        
        # Inline buttons for Admin approval in the storage channel
        # These callback buttons will be answered by the ADMIN bot (BOT_TOKEN)
        import json as _json
        approve_cb = f"adm_approve_{user_id}_{slug}_{tier}"
        reject_cb  = f"adm_reject_{user_id}_{slug}_{tier}"
        reply_markup_json = _json.dumps({
            "inline_keyboard": [[
                {"text": "✅ Approve & Deliver", "callback_data": approve_cb},
                {"text": "❌ Reject", "callback_data": reject_cb}
            ]]
        })
        
        # Send receipt to Storage Group via ADMIN bot (admin bot is member of storage group)
        try:
            admin_send_photo(STORAGE_GROUP_ID, str(img_path), admin_caption, reply_markup_json)
        except Exception as e:
            log.error(f"Admin bot failed to send approval photo: {e}")
            # Fallback: try via customer bot (may fail if not in storage group)
            with open(img_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=STORAGE_GROUP_ID,
                    photo=f,
                    caption=admin_caption,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Approve & Deliver", callback_data=approve_cb),
                        InlineKeyboardButton("❌ Reject", callback_data=reject_cb)
                    ]]),
                    parse_mode="HTML"
                )
            
        # Clean up temp file
        try:
            img_path.unlink()
        except Exception:
            pass
            
        # Reconstruct clean full and short names for the actress from the slug
        full_name, short_name = get_clean_actress_name(slug)
        
        # Notify customer
        if is_ok and amount_match:
            await wait_msg.edit_text(
                f"🔥 <b>AI check complete!</b>\n\n"
                f"Your payment matches. {short_name} is just whispering to the boss to unlock the vault. "
                f"Hold your breath, your uncut premium footage is being prepared for delivery right now... 😉✨",
                parse_mode="HTML"
            )
        else:
            await wait_msg.edit_text(
                f"⏳ <b>{short_name} is confirming with the boss...</b>\n\n"
                f"Keep your eyes on this screen — your exclusive action tape is being wrapped up for delivery! "
                f"Just a few hot seconds... 😉🔥",
                parse_mode="HTML"
            )
            
    except Exception as e:
        log.error(f"Error handling receipt: {e}")
        await wait_msg.edit_text(
            "❌ <b>Error processing receipt.</b>\n"
            "An error occurred. Don't worry, your submission has been saved. "
            "Please notify support if your delivery is delayed.",
            parse_mode="HTML"
        )


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes Approval/Rejection buttons pressed by admin in the Storage Group."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data.startswith("adm_"):
        return
        
    parts = data.split("_")
    # Format: adm_{action}_{user_id}_{slug}_{tier}
    action = parts[1]
    buyer_id = int(parts[2])
    # Slugs and tiers can contain underscores (e.g. joslyn_james_01, half_1080)
    slug = "_".join(parts[3:-2])
    tier = "_".join(parts[-2:])
    
    admin_user = update.effective_user.username or f"ID {update.effective_user.id}"
    original_caption = query.message.caption or ""
    
    # Customer bot instance to send DM notifications to customer in Customer Bot chat
    from telegram import Bot
    customer_bot = Bot(token=CUSTOMER_BOT_TOKEN)
    
    if action == "approve":
        from storage_manager import load_index
        index_data = load_index()
        video_exists = slug in index_data and index_data[slug].get(tier) is not None
        
        if video_exists:
            # Deliver video via Admin Bot (videos live in Admin Bot's storage group)
            success = forward_video_to_buyer(slug, tier, buyer_id)
            if success:
                update_submission_status(buyer_id, slug, tier, "approved")
                
                # Notify buyer via CUSTOMER bot
                try:
                    await customer_bot.send_message(
                        chat_id=buyer_id,
                        text="🎉 <b>Payment Confirmed!</b>\n"
                             "Here is your purchased video file below. Enjoy!",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    log.error(f"Could not notify buyer via Customer Bot: {e}")
                    # Fallback to Admin Bot
                    try:
                        await context.bot.send_message(
                            chat_id=buyer_id,
                            text="🎉 <b>Payment Confirmed!</b>\n"
                                 "Here is your purchased video file below. Enjoy!",
                            parse_mode="HTML"
                        )
                    except Exception as e2:
                        log.error(f"Could not notify buyer via Admin Bot fallback: {e2}")
                    
                # Update admin message via ADMIN bot
                new_caption = f"{original_caption}\n\n✅ <b>APPROVED & DELIVERED</b> by @{admin_user}"
                if not admin_edit_caption(STORAGE_GROUP_ID, query.message.message_id, new_caption):
                    log.warning("admin_edit_caption failed (approval ok)")
                USER_SESSIONS.pop(buyer_id, None)
            else:
                new_caption = f"{original_caption}\n\n⚠️ <b>ERROR: Delivery failed during forwarding.</b>"
                admin_edit_caption(STORAGE_GROUP_ID, query.message.message_id, new_caption)
        else:
            # Payment approved but video not in index yet
            update_submission_status(buyer_id, slug, tier, "approved")
            
            try:
                await customer_bot.send_message(
                    chat_id=buyer_id,
                    text="🎉 <b>Payment Verified!</b>\n\n"
                         "Your premium video package is currently being retrieved and processed by our delivery system. "
                         "This usually takes just a few minutes. Please hold on, it will be delivered to this chat shortly! "
                         "Thank you for your patience! 🙏",
                    parse_mode="HTML"
                )
            except Exception as e:
                log.error(f"Could not notify buyer via Customer Bot: {e}")
                # Fallback to Admin Bot
                try:
                    await context.bot.send_message(
                        chat_id=buyer_id,
                        text="🎉 <b>Payment Verified!</b>\n\n"
                             "Your premium video package is currently being retrieved and processed by our delivery system. "
                             "This usually takes just a few minutes. Please hold on, it will be delivered to this chat shortly! "
                             "Thank you for your patience! 🙏",
                        parse_mode="HTML"
                    )
                except Exception as e2:
                    log.error(f"Could not notify buyer via Admin Bot fallback: {e2}")
                
            new_caption = (
                f"{original_caption}\n\n"
                f"✅ <b>APPROVED (PENDING DELIVERY)</b> by @{admin_user}\n\n"
                f"⚠️ <b>ALERT:</b> The tier <code>{tier}</code> for slug <code>{slug}</code> is missing from the database index!\n"
                f"Please upload/forward the file to the customer manually once ready."
            )
            admin_edit_caption(STORAGE_GROUP_ID, query.message.message_id, new_caption)
            USER_SESSIONS.pop(buyer_id, None)
            
    elif action == "reject":
        update_submission_status(buyer_id, slug, tier, "rejected")
        
        # Notify buyer via CUSTOMER bot
        try:
            await customer_bot.send_message(
                chat_id=buyer_id,
                text="❌ <b>Payment Screenshot Rejected</b>\n"
                     "The administrator rejected the transaction receipt. "
                     "Please ensure you transferred the exact amount and sent the correct, unmodified receipt screenshot.",
                parse_mode="HTML"
            )
        except Exception as e:
            log.error(f"Could not notify buyer of rejection via Customer Bot: {e}")
            # Fallback to Admin Bot
            try:
                await context.bot.send_message(
                    chat_id=buyer_id,
                    text="❌ <b>Payment Screenshot Rejected</b>\n"
                         "The administrator rejected the transaction receipt. "
                         "Please ensure you transferred the exact amount and sent the correct, unmodified receipt screenshot.",
                    parse_mode="HTML"
                )
            except Exception as e2:
                log.error(f"Could not notify buyer of rejection via Admin Bot fallback: {e2}")
            
        # Update admin message via ADMIN bot
        new_caption = f"{original_caption}\n\n❌ <b>REJECTED</b> by @{admin_user}"
        admin_edit_caption(STORAGE_GROUP_ID, query.message.message_id, new_caption)


async def handle_admin_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles standard text messages sent to the Admin Bot. Only processes admin upload states."""
    if update.effective_chat.type != "private":
        return
        
    user_id = update.effective_user.id
    user_text = update.message.text.strip()
    
    if user_id not in ADMIN_IDS:
        return
        
    session = ADMIN_MEDIA_SESSIONS.get(user_id)
    if not session or session["status"] not in ["waiting_slug_tier", "waiting_slug_trailer"]:
        await update.message.reply_text("🛠️ <b>Send a video or document to start indexing.</b>", parse_mode="HTML")
        return
        
    if session["status"] == "waiting_slug_tier":
        parts = user_text.split()
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ <b>Invalid format.</b>\n"
                "Please provide both slug and tier separated by a space.\n"
                "Example: <code>sakshitha_02 full_1080</code>",
                parse_mode="HTML"
            )
            return
        slug = parts[0].strip().lower()
        tier = parts[1].strip().lower()
        
        if tier not in ["half_720", "half_1080", "full_1080"]:
            await update.message.reply_text(
                f"❌ <b>Invalid tier name '{tier}'.</b>\n"
                f"Supported tiers: <code>half_720</code>, <code>half_1080</code>, <code>full_1080</code>",
                parse_mode="HTML"
            )
            return
            
        try:
            await update.message.reply_text("⏳ Uploading to Storage Group and indexing...")
            
            # Since both bots are merged, we use the file_id directly to send the video/document.
            # No download/re-upload is needed on the server side.
            if session["media_type"] == "document":
                sent_msg = await context.bot.send_document(
                    chat_id=STORAGE_GROUP_ID,
                    document=session["file_id"],
                    caption=f"Slug: {slug}\nTier: {tier}"
                )
            else:
                sent_msg = await context.bot.send_video(
                    chat_id=STORAGE_GROUP_ID,
                    video=session["file_id"],
                    caption=f"Slug: {slug}\nTier: {tier}"
                )
            
            from storage_manager import load_index, save_index
            index = load_index()
            if slug not in index:
                index[slug] = {
                    "caption": f"Manual Upload: {slug}",
                    "half_720": None,
                    "half_1080": None,
                    "full_1080": None,
                    "timestamp": int(time.time())
                }
            index[slug][tier] = sent_msg.message_id
            save_index(index)
            
            await update.message.reply_text(
                f"✅ <b>Successfully uploaded and indexed!</b>\n"
                f"• <b>Slug:</b> <code>{slug}</code>\n"
                f"• <b>Tier:</b> <code>{tier}</code>\n"
                f"• <b>Message ID:</b> <code>{sent_msg.message_id}</code>",
                parse_mode="HTML"
            )
        except Exception as e:
            log.error(f"Manual storage upload failed: {e}")
            await update.message.reply_text(f"❌ Upload failed: {e}")
            
    elif session["status"] == "waiting_slug_trailer":
        slug = user_text.lower().replace(" ", "_")
        
        try:
            await update.message.reply_text("⏳ Posting trailer to Public Group...")
            bot_username = await get_customer_bot_username(context.bot)
            buy_link = f"https://t.me/{bot_username}?start={slug}"
            
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Unlock Quality Tiers (FREE / ₹59 / ₹149)", url=buy_link)]
            ])
            
            caption = await generate_tempting_hook(slug)
            
            if session["media_type"] == "document":
                sent_msg = await context.bot.send_document(
                    chat_id=TRAILER_GROUP_ID,
                    document=session["file_id"],
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                )
            else:
                sent_msg = await context.bot.send_video(
                    chat_id=TRAILER_GROUP_ID,
                    video=session["file_id"],
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                )
                
            from storage_manager import load_index, save_index
            index = load_index()
            if slug not in index:
                index[slug] = {
                    "caption": f"Trailer Video: {slug}",
                    "half_720": None,
                    "half_1080": None,
                    "full_1080": None,
                    "timestamp": int(time.time())
                }
                save_index(index)
                
            await update.message.reply_text(
                f"✅ <b>Successfully posted trailer!</b>\n"
                f"• <b>Slug:</b> <code>{slug}</code>\n"
                f"• <b>Trailer message ID:</b> <code>{sent_msg.message_id}</code>",
                parse_mode="HTML"
            )
        except Exception as e:
            log.error(f"Manual trailer post failed: {e}")
            await update.message.reply_text(f"❌ Post failed: {e}")
            
    ADMIN_MEDIA_SESSIONS.pop(user_id, None)


async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles standard text messages from users (inquires, negotiations) using Groq/Mistral AI."""
    # Ignore group messages
    if update.effective_chat.type != "private":
        return
        
    user_id = update.effective_user.id
    user_text = update.message.text.strip()
    
    # Check if this admin has an active media session
    if user_id in ADMIN_IDS and user_id in ADMIN_MEDIA_SESSIONS:
        await handle_admin_chat_message(update, context)
        return
        
    # Check if user is in an active bargaining session
    if user_id in BARGAIN_SESSIONS:
        response = await process_bargain_message(user_id, user_text)
        await update.message.reply_text(response, parse_mode="HTML")
        return
        
    # Normal customer text message
    log.info(f"User Message: {user_text}")
    response = await answer_user_query(user_text, user_id)
    await update.message.reply_text(response)


async def handle_private_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes private text messages based on whether the sender is an admin in a media session or a customer."""
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS and user_id in ADMIN_MEDIA_SESSIONS:
        await handle_admin_chat_message(update, context)
    else:
        await handle_chat_message(update, context)


async def handle_storage_group_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listens for manual video uploads inside the private Storage Group and auto-indexes them."""
    msg = update.message or update.channel_post
    if not msg or not msg.video:
        return
        
    caption = msg.caption or ""
    if not caption:
        return
        
    slug = None
    tier = None
    for line in caption.split("\n"):
        line_lower = line.lower().strip()
        if "slug:" in line_lower:
            slug = line.split(":", 1)[1].strip()
        elif "tier:" in line_lower:
            tier = line.split(":", 1)[1].strip()
            
    if slug and tier:
        tier = tier.lower()
        if tier not in ["half_720", "half_1080", "full_1080"]:
            log.warning(f"Ignored manual upload: Invalid tier '{tier}' specified.")
            return
            
        from storage_manager import load_index, save_index
        index = load_index()
        
        if slug not in index:
            index[slug] = {
                "caption": f"Manual Upload: {slug}",
                "half_720": None,
                "half_1080": None,
                "full_1080": None,
                "timestamp": int(time.time())
            }
            
        index[slug][tier] = msg.message_id
        save_index(index)
        
        log.info(f"💾 Automatically indexed manual upload: slug='{slug}', tier='{tier}', message_id={msg.message_id}")
        try:
            await msg.reply_text(
                f"✅ <b>Auto-Indexed Video:</b>\n"
                f"• <b>Slug:</b> <code>{slug}</code>\n"
                f"• <b>Tier:</b> <code>{tier}</code>\n"
                f"• <b>Message ID:</b> <code>{msg.message_id}</code>\n"
                f"Ready for customer purchases!",
                parse_mode="HTML"
            )
        except Exception as e:
            log.warning(f"Could not reply to group message: {e}")


async def handle_trailer_group_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listens for trailer uploads in the public Trailer Group, generates AI hooks and attaches Buy buttons."""
    msg = update.message or update.channel_post
    if not msg or not msg.video:
        return
        
    # Ignore if the message already has the Buy button to avoid infinite loops!
    if msg.reply_markup and msg.reply_markup.inline_keyboard:
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if btn.url and "/start=" in btn.url:
                    return # Already processed!
                    
    caption = msg.caption or ""
    file_name = msg.video.file_name or ""
    
    # Determine slug from caption or filename
    slug = None
    if caption:
        # Check if they specified a slug explicitly, e.g. "Slug: joslyn_james_01"
        for line in caption.split("\n"):
            if "slug:" in line.lower():
                slug = line.split(":", 1)[1].strip()
                break
        if not slug:
            # Clean caption first line to make a slug
            first_line = caption.split("\n")[0].strip()
            slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in first_line).lower()
            
    if not slug and file_name:
        file_name_clean = Path(file_name).stem
        for suffix in ["_trailer", "_preview", "_short", "_t", "-trailer", "-preview"]:
            if file_name_clean.lower().endswith(suffix):
                file_name_clean = file_name_clean[:-len(suffix)]
                break
        slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in file_name_clean).lower()
        
    if not slug or slug.startswith("video_"):
        slug = f"trailer_{int(time.time())}"
        
    slug = slug.strip().lower()
    while "__" in slug:
        slug = slug.replace("__", "_")
    slug = slug.strip("_")
    
    # Generate AI hook
    ai_caption = await generate_tempting_hook(slug)
    
    bot_username = await get_customer_bot_username(context.bot)
    buy_link = f"https://t.me/{bot_username}?start={slug}"
    
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Unlock Quality Tiers (FREE / ₹59 / ₹149)", url=buy_link)]
    ])
    
    # Try to edit in place (works for channel posts or if bot sent it)
    try:
        if update.channel_post:
            await context.bot.edit_message_caption(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=ai_caption,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            log.info(f"Edited channel trailer post message_id={msg.message_id} with AI hook.")
            return
    except Exception as e:
        log.warning(f"Could not edit trailer caption directly: {e}. Falling back to reposting.")
        
    # Fallback: Delete original and post new one (if group admin permissions allow)
    try:
        sent_msg = await context.bot.send_video(
            chat_id=msg.chat_id,
            video=msg.video.file_id,
            caption=ai_caption,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        log.info(f"Reposted trailer with AI hook. New message_id={sent_msg.message_id}")
        try:
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
        except Exception as delete_err:
            log.warning(f"Could not delete original admin trailer message: {delete_err}")
    except Exception as e:
        log.error(f"Failed to post trailer in trailer group: {e}")


# ── Admin Direct Media Upload Helpers ─────────────────────────────────────────

ADMIN_MEDIA_SESSIONS: Dict[int, Dict[str, Any]] = {}

async def handle_admin_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when an admin uploads a document or video directly to the bot in DM."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
        
    msg = update.message
    is_video = msg.video is not None
    is_doc = msg.document is not None
    
    if not is_video and not is_doc:
        return
        
    file_id = msg.video.file_id if is_video else msg.document.file_id
    file_name = msg.video.file_name if is_video else msg.document.file_name
    if not file_name:
        file_name = f"video_{int(time.time())}.mp4"
        
    media_type = "video" if is_video else "document"
    
    # Pre-calculate suggested slug for timeout fallback
    suggested_slug = suggest_next_slug(file_name, msg.caption, "store" if media_type == "document" else "trailer")
        
    # Store session details
    ADMIN_MEDIA_SESSIONS[user_id] = {
        "msg_id": msg.message_id,
        "file_id": file_id,
        "file_name": file_name,
        "media_type": media_type,
        "timestamp": time.time(),
        "status": "pending_action",
        "prompt_msg_id": None,
        "caption": msg.caption,
        "suggested_slug": suggested_slug
    }
    
    # Prompt keyboard
    keyboard = [
        [
            InlineKeyboardButton("📥 Storage Group", callback_data=f"media_store_{user_id}_{msg.message_id}"),
            InlineKeyboardButton("📺 Post as Trailer", callback_data=f"media_trailer_{user_id}_{msg.message_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    prompt_text = (
        f"⚡ <b>Admin Media Received ({media_type.upper()})</b>\n\n"
    )
    if suggested_slug:
        prompt_text += f"💡 <b>Auto-detected Slug:</b> <code>{suggested_slug}</code>\n\n"
    prompt_text += (
        f"Select upload destination within <b>30 seconds</b>.\n"
        f"If no option is selected, the bot automatically defaults:\n"
        f"• 📄 Documents go to <b>Storage Group</b> (full_1080)\n"
        f"• 🎥 Videos go to <b>Trailer Group</b> (Public)"
    )
    
    prompt = await msg.reply_text(
        prompt_text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )
    
    ADMIN_MEDIA_SESSIONS[user_id]["prompt_msg_id"] = prompt.message_id
    
    # Run 30-second timeout task
    asyncio.create_task(run_admin_media_timeout(context, user_id, msg.message_id))


async def run_admin_media_timeout(context: ContextTypes.DEFAULT_TYPE, admin_id: int, message_id: int):
    """Timer thread that executes fallback configuration after 30 seconds."""
    await asyncio.sleep(30)
    session = ADMIN_MEDIA_SESSIONS.get(admin_id)
    if session and session["msg_id"] == message_id and session["status"] == "pending_action":
        session["status"] = "timeout"
        try:
            await context.bot.edit_message_text(
                chat_id=admin_id,
                message_id=session["prompt_msg_id"],
                text="⏳ <b>30 seconds elapsed.</b> Processing automatic fallback routing...",
                parse_mode="HTML"
            )
        except Exception:
            pass
        await execute_media_fallback(context, admin_id, session)


async def execute_media_fallback(context: ContextTypes.DEFAULT_TYPE, admin_id: int, session: Dict[str, Any]):
    """Default fallback routing based on media format."""
    file_id = session["file_id"]
    media_type = session["media_type"]
    file_name = session["file_name"]
    
    slug = session.get("suggested_slug")
    if not slug:
        base_name = Path(file_name).stem
        slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in base_name).lower()
        
    if slug:
        slug = slug.strip().lower()
        while "__" in slug:
            slug = slug.replace("__", "_")
        slug = slug.strip("_")
        
    if not slug:
        slug = f"auto_{int(time.time())}"
        
    try:
        if media_type == "document":
            # Fallback A: Document -> Private Storage Group as full_1080
            caption = f"Slug: {slug}\nTier: full_1080"
            # Since both bots are merged, we use the file_id directly to send the document fallback.
            sent_msg = await context.bot.send_document(
                chat_id=STORAGE_GROUP_ID,
                document=file_id,
                caption=caption
            )
            
            from storage_manager import load_index, save_index
            index = load_index()
            if slug not in index:
                index[slug] = {
                    "caption": f"Auto Upload: {slug}",
                    "half_720": None,
                    "full_720": None,
                    "full_1080": None,
                    "timestamp": int(time.time())
                }
            index[slug]["full_1080"] = sent_msg.message_id
            save_index(index)
            
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"✅ <b>Auto-Fallback Applied:</b> Uploaded Document to Storage Group.\n"
                     f"• <b>Slug:</b> <code>{slug}</code>\n"
                     f"• <b>Tier:</b> <code>full_1080</code>\n"
                     f"• <b>Message ID:</b> <code>{sent_msg.message_id}</code>",
                parse_mode="HTML"
            )
        else:
            # Fallback B: Video -> Public Trailer Group with Buy Button
            slug = session.get("suggested_slug")
            if not slug:
                file_name_clean = Path(file_name).stem
                # Strip common trailer/preview suffixes
                for suffix in ["_trailer", "_preview", "_short", "_t", "-trailer", "-preview"]:
                    if file_name_clean.lower().endswith(suffix):
                        file_name_clean = file_name_clean[:-len(suffix)]
                        break
                slug = "".join(c if c.isalnum() or c in "_-" else "_" for c in file_name_clean).lower()
                
            if slug:
                slug = slug.strip().lower()
                while "__" in slug:
                    slug = slug.replace("__", "_")
                slug = slug.strip("_")
                
            if not slug or slug.startswith("video_"):
                slug = f"trailer_{int(time.time())}"
                
            bot_username = await get_customer_bot_username(context.bot)
            buy_link = f"https://t.me/{bot_username}?start={slug}"
            
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Unlock Quality Tiers (FREE / ₹59 / ₹149)", url=buy_link)]
            ])
            
            caption = await generate_tempting_hook(slug)
            
            sent_msg = await context.bot.send_video(
                chat_id=TRAILER_GROUP_ID,
                video=file_id,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            
            from storage_manager import load_index, save_index
            index = load_index()
            if slug not in index:
                index[slug] = {
                    "caption": f"Trailer Video: {slug}",
                    "half_720": None,
                    "full_720": None,
                    "full_1080": None,
                    "timestamp": int(time.time())
                }
                save_index(index)
                
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"✅ <b>Auto-Fallback Applied:</b> Posted Video to Trailer Group.\n"
                     f"• <b>Slug:</b> <code>{slug}</code>\n"
                     f"• <b>Trailer message ID:</b> <code>{sent_msg.message_id}</code>\n\n"
                     f"<i>Note: Remember to upload/index storage tiers for this slug!</i>",
                parse_mode="HTML"
            )
    except Exception as e:
        log.error(f"Fallback routing failed: {e}")
        await context.bot.send_message(chat_id=admin_id, text=f"❌ Fallback processing failed: {e}")
        
    ADMIN_MEDIA_SESSIONS.pop(admin_id, None)


async def admin_media_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes admin buttons clicked within the 6-second window."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    parts = data.split("_")
    action = parts[1]
    admin_id = int(parts[2])
    msg_id = int(parts[3])
    
    session = ADMIN_MEDIA_SESSIONS.get(admin_id)
    if not session or session["msg_id"] != msg_id:
        await query.edit_message_text("❌ This session has expired or is invalid.")
        return
        
    if session["status"] != "pending_action":
        await query.edit_message_text("❌ Action already taken or session timed out.")
        return
        
    if action == "store":
        session["status"] = "waiting_slug_tier"
        caption = session.get("caption") or ""
        file_name = session["file_name"]
        suggested_slug = suggest_next_slug(file_name, caption, "store")
        
        text = (
            "📥 <b>Upload to Storage selected.</b>\n\n"
            "Please send the <b>slug</b> and <b>tier</b> (separated by space) in your next message:\n"
            "👉 <code>slug_name tier_name</code>\n\n"
        )
        if suggested_slug:
            text += f"💡 <b>Suggested:</b> Send <code>{suggested_slug} [tier]</code> (e.g., <code>{suggested_slug} full_1080</code>)\n\n"
        text += (
            "Example:\n"
            "<code>sakshitha_02 full_1080</code>"
        )
        await query.edit_message_text(text, parse_mode="HTML")
    elif action == "trailer":
        session["status"] = "waiting_slug_trailer"
        caption = session.get("caption") or ""
        file_name = session["file_name"]
        suggested_slug = suggest_next_slug(file_name, caption, "trailer")
        
        text = (
            "📺 <b>Post as Trailer selected.</b>\n\n"
            "Please send the <b>slug</b> for the trailer in your next message:\n"
            "👉 <code>slug_name</code>\n\n"
        )
        if suggested_slug:
            text += f"💡 <b>Suggested:</b> Send <code>{suggested_slug}</code>\n\n"
        text += (
            "Example:\n"
            "<code>sakshitha_02</code>"
        )
        await query.edit_message_text(text, parse_mode="HTML")


async def periodic_backup_loop():
    """Periodically backs up the database to Telegram every 5 hours and 45 minutes (20700 seconds)."""
    interval = 5 * 3600 + 45 * 60  # 20,700 seconds
    log.info(f"Periodic database backup loop started. Will backup every 5 hours and 45 minutes.")
    while True:
        try:
            await asyncio.sleep(interval)
            log.info("Triggering scheduled periodic database backup...")
            success = await asyncio.to_thread(backup_database_to_telegram)
            if success:
                log.info("Periodic backup successfully uploaded and pinned.")
            else:
                log.error("Periodic backup failed.")
        except asyncio.CancelledError:
            log.info("Periodic backup loop cancelled.")
            break
        except Exception as e:
            log.error(f"Error in periodic backup loop: {e}")
            await asyncio.sleep(60)


async def post_init_callback(application: Application):
    """Callback run on application startup to spawn background tasks."""
    asyncio.create_task(periodic_backup_loop())
    log.info("Started background periodic backup task.")


async def post_shutdown_callback(application: Application):
    """Callback run on application shutdown to save final database state."""
    log.info("Bot is shutting down. Triggering final database backup...")
    success = await asyncio.to_thread(backup_database_to_telegram)
    if success:
        log.info("Final database backup completed successfully.")
    else:
        log.error("Final database backup failed.")


# ── Main Builder ─────────────────────────────────────────────────────────────

def _build_unified_app() -> Application:
    """
    Unified Bot Application — runs as BOT_TOKEN (8629240240).
    Handles all Admin and Customer operations in a single application instance.
    """
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init_callback)
        .post_shutdown(post_shutdown_callback)
        .build()
    )

    # Command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(admin_callback_handler,       pattern=r"^adm_"))
    app.add_handler(CallbackQueryHandler(admin_media_callback_handler, pattern=r"^media_"))
    app.add_handler(CallbackQueryHandler(tier_selection_callback,      pattern=r"^buy_"))

    # Private DMs
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_screenshot))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.VIDEO | filters.Document.ALL),
        handle_admin_media_upload
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private_text_message
    ))

    # Storage Group auto-indexer
    if STORAGE_GROUP_ID:
        try:
            storage_chat_id = int(STORAGE_GROUP_ID)
            app.add_handler(MessageHandler(
                filters.Chat(chat_id=storage_chat_id) & filters.VIDEO,
                handle_storage_group_upload
            ))
            log.info(f"🔍 [Bot] Auto-Indexer active for Storage Group: {STORAGE_GROUP_ID}")
        except ValueError:
            log.error(f"Invalid STORAGE_GROUP_ID format: {STORAGE_GROUP_ID}")

    # Trailer Group auto-indexer
    if TRAILER_GROUP_ID:
        try:
            trailer_chat_id = int(TRAILER_GROUP_ID)
            app.add_handler(MessageHandler(
                filters.Chat(chat_id=trailer_chat_id) & filters.VIDEO,
                handle_trailer_group_upload
            ))
            log.info(f"🔍 [Bot] Auto-Indexer active for Trailer Group: {TRAILER_GROUP_ID}")
        except ValueError:
            log.error(f"Invalid TRAILER_GROUP_ID format: {TRAILER_GROUP_ID}")

    return app


async def _run_unified(app: Application):
    """Initialises and runs the unified Application polling."""
    async with app:
        await app.start()

        # Resolve Bot username at startup
        global CUSTOMER_BOT_USERNAME
        try:
            me = await app.bot.get_me()
            CUSTOMER_BOT_USERNAME = me.username
            log.info(f"Loaded Bot username: @{CUSTOMER_BOT_USERNAME}")
        except Exception as e:
            log.error(f"Failed to fetch Bot username at startup: {e}")

        log.info("✅ [UnifiedBot] polling started — handles all Admin & Customer operations")

        updater = app.updater
        await updater.start_polling()

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await updater.stop()
            await app.stop()


def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is missing from .env!")
        return

    # Restore database from Telegram Storage Group pinned message on startup
    try:
        restore_database_from_telegram()
    except Exception as e:
        log.error(f"Failed to restore database on startup: {e}")

    log.info("=" * 60)
    log.info("🚀 Starting Unified Sales & Admin System")
    log.info("   Bot Token: 8629****")
    log.info("=" * 60)

    app = _build_unified_app()
    asyncio.run(_run_unified(app))


if __name__ == "__main__":
    main()
