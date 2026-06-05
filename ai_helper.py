"""
ai_helper.py
────────────
Centralized AI module utilizing Gemini, Groq, and Mistral for generating tempting 
adult promo hooks and replying to customers in character as the actress of their chosen video.
"""

import os
import re
import logging
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# API imports
from groq import Groq
from mistralai.client import Mistral
from telegram_gemini_router import gemini_router

# Load env variables
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)

GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "").strip()
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()

PRICE_HALF_720  = int(os.getenv("PRICE_HALF_720", "0"))
PRICE_HALF_1080 = int(os.getenv("PRICE_HALF_1080", "59"))
PRICE_FULL_1080 = int(os.getenv("PRICE_FULL_1080", "149"))

log = logging.getLogger("AIHelper")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# Initialize Clients
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
mistral_client = Mistral(api_key=MISTRAL_API_KEY) if MISTRAL_API_KEY else None


def get_clean_actress_name(slug: str) -> tuple[str, str]:
    """
    Cleans a slug to get the full name and short name.
    Example: 'joslyn_james_01' -> ('Joslyn James', 'Joslyn')
    """
    # Remove trailing numbers like _01, _02, etc.
    clean = re.sub(r'_\d+$', '', slug)
    # Replace underscores with spaces and title case
    full_name = clean.replace("_", " ").strip().title()
    if not full_name:
        return "the actress", "she"
    short_name = full_name.split()[0]
    return full_name, short_name


def clean_html_for_telegram(text: str) -> str:
    """Cleans up and removes unsupported HTML tags returned by AI models for Telegram compatibility."""
    if not text:
        return ""
    
    # 1. Clean markdown code blocks
    text = re.sub(r'^```[a-zA-Z0-9]*\n', '', text)
    text = re.sub(r'\n```$', '', text)
    text = text.strip('`').strip()
    
    # 2. Replace <br> or <br/> with actual newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    
    # 3. Strip out common blocking/unsupported tags (div, p, html, body, span)
    text = re.sub(r'</?(?:div|p|html|body|span)[^>]*>', '', text, flags=re.IGNORECASE)
    
    # 4. Remove header tags h1-h6
    text = re.sub(r'</?h[1-6][^>]*>', '', text, flags=re.IGNORECASE)
    
    return text.strip()


async def generate_tempting_hook(slug: str) -> str:
    """Generates an extremely tempting, seductive adult hook for the trailer using Gemini, Groq, or Mistral."""
    full_name, short_name = get_clean_actress_name(slug)
    
    prompt = f"""
    Write a highly tempting, seductive, and teasing adult promotional hook caption for a premium private video release.
    Actress name: {full_name} (short name: {short_name}).
    
    The caption MUST be in HTML format and structure:
    1. Header: 🔥 <b>EXCLUSIVE: {full_name.upper()}</b> 🔥
    2. A short, extremely teasing, seductive body paragraph (1-2 sentences) about her private, uncut premium video being leaked or made available here. Keep it highly provocative and tempting (e.g., whispering secrets, wild action, private tape, etc.).
    3. The pricing tiers formatted exactly as:
    Watch the 720p half-duration preview above 👆
    Choose your package to unlock the full action:
    🔹 Half Video (720p) — <b>FREE</b> (sent above)
    🔹 Half Video (1080p) — <b>₹{PRICE_HALF_1080}</b>
    🔹 Full HD Video (1080p) — <b>₹{PRICE_FULL_1080}</b>
    4. Call to action footer:
    👇 Tap the button below to purchase and get instant access! 👇
    
    Return ONLY the final HTML caption. No other text, thoughts, or formatting. Do not wrap in ```html code block.
    """

    # 1. Try Groq
    if groq_client:
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: groq_client.chat.completions.create(
                    model="llama3-8b-8192",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=300,
                    temperature=0.8
                )
            )
            content = resp.choices[0].message.content.strip()
            cleaned = clean_html_for_telegram(content)
            if cleaned and "EXCLUSIVE" in cleaned.upper():
                return cleaned
        except Exception as e:
            log.warning(f"Groq failed generating hook: {e}")

    # 2. Try Mistral
    if mistral_client:
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: mistral_client.chat.complete(
                    model="open-mixtral-8x22b",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=300,
                    temperature=0.8
                )
            )
            content = resp.choices[0].message.content.strip()
            cleaned = clean_html_for_telegram(content)
            if cleaned and "EXCLUSIVE" in cleaned.upper():
                return cleaned
        except Exception as e:
            log.warning(f"Mistral failed generating hook: {e}")

    # 3. Try Gemini
    if GEMINI_API_KEY:
        try:
            resp_text = await asyncio.to_thread(
                gemini_router.generate,
                task_type="creative",
                prompt=prompt,
                module_name="sales_bot"
            )
            if resp_text:
                content = resp_text.strip()
                cleaned = clean_html_for_telegram(content)
                if cleaned and "EXCLUSIVE" in cleaned.upper():
                    return cleaned
        except Exception as e:
            log.warning(f"Gemini failed generating hook: {e}")

    # Fallback to default high-quality static template if all AI calls fail
    return (
        f"🔥 <b>EXCLUSIVE: {full_name.upper()}</b> 🔥\n\n"
        f"She thought this private video would never get out... but we've got the uncut footage! 👀\n\n"
        f"Watch the 720p half-duration preview above 👆\n"
        f"Choose your package to unlock the full action:\n"
        f"🔹 Half Video (720p) — <b>FREE</b> (sent above)\n"
        f"🔹 Half Video (1080p) — <b>₹{PRICE_HALF_1080}</b>\n"
        f"🔹 Full HD Video (1080p) — <b>₹{PRICE_FULL_1080}</b>\n\n"
        f"👇 Tap the button below to purchase and get instant access! 👇"
    )


async def answer_user_query_ai(user_msg: str, slug: str = None) -> str:
    """Answers customer inquiries using Gemini, Groq, or Mistral, responding as the dynamic actress persona if a session exists."""
    if slug:
        full_name, short_name = get_clean_actress_name(slug)
        sys_prompt = f"""
        You are {full_name} (calling yourself {short_name}), an extremely teasing, seductive, and playful adult model.
        A customer is messaging you directly in Telegram to purchase your premium uncut private video (slug: {slug}).
        
        Rules:
        1. Always respond in character as the teasing and seductive model {short_name}. Use playful emojis (😉, 😘, 🔥, 😈).
        2. Keep your response extremely brief (1-2 sentences max) and highly teasing.
        3. Do NOT bargain or offer discounts under any circumstances. Refuse discounts play-fully/teasingly but firmly.
        4. Prices are fixed:
           - Half video (720p): FREE
           - Half video (1080p): ₹{PRICE_HALF_1080}
           - Full HD video (1080p): ₹{PRICE_FULL_1080}
        5. Instruct them to send/upload the transaction screenshot directly here in this chat to unlock the vault.
        6. Do not mention that you are a bot, program, or AI. You are a real human girl teasing them.
        """
    else:
        sys_prompt = f"""
        You are a teasing, seductive, and playful premium video assistant.
        A customer is inquiring about premium adult videos.
        
        Rules:
        1. Keep responses very brief (1-2 sentences max) and highly teasing/seductive.
        2. Tell them to browse the trailer group, click a "Buy Now" button, or type /start to select a clip.
        3. Prices are strictly fixed:
           - Half video (720p): FREE
           - Half video (1080p): ₹{PRICE_HALF_1080}
           - Full HD video (1080p): ₹{PRICE_FULL_1080}
        4. Refuse any bargaining or discounts firmly but teasingly.
        """

    # 1. Try Gemini
    if GEMINI_API_KEY:
        try:
            resp_text = await asyncio.to_thread(
                gemini_router.generate,
                task_type="creative",
                prompt=f"System Instruction: {sys_prompt}\nUser Message: {user_msg}",
                module_name="sales_bot"
            )
            if resp_text:
                return clean_html_for_telegram(resp_text.strip())
        except Exception as e:
            log.warning(f"Gemini failed answering query: {e}")

    # 2. Try Groq
    if groq_client:
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: groq_client.chat.completions.create(
                    model="llama3-8b-8192",
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg}
                    ],
                    max_tokens=150,
                    temperature=0.8
                )
            )
            content = resp.choices[0].message.content.strip()
            return clean_html_for_telegram(content)
        except Exception as e:
            log.error(f"Groq failed answering query: {e}")

    # 3. Try Mistral
    if mistral_client:
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: mistral_client.chat.complete(
                    model="open-mixtral-8x22b",
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg}
                    ],
                    max_tokens=150,
                    temperature=0.8
                )
            )
            content = resp.choices[0].message.content.strip()
            return clean_html_for_telegram(content)
        except Exception as e:
            log.error(f"Mistral failed answering query: {e}")

    # Fallback response if all AI calls fail
    if slug:
        _, short_name = get_clean_actress_name(slug)
        return f"Mmm, don't keep me waiting baby... 😉 Send the screenshot of your payment directly here so I can unlock the vault for you! Prices are strictly fixed. 😘"
    return "Pricing is strictly fixed. Send /start to browse tiers and get purchase details. Buy it or lose it."


def extract_base_actress_name(file_name: str, caption: str) -> str:
    """Extracts a clean base name/slug (without suffixes) from either a filename or caption."""
    # 1. Look at caption first
    text_to_parse = ""
    if caption:
        # Split by newline and take first line
        first_line = caption.strip().split("\n")[0]
        # Remove EXCLUSIVE, EXCL, etc.
        first_line = re.sub(r'(?i)\bexclusive\b:?', '', first_line)
        first_line = re.sub(r'(?i)\bexcl\b:?', '', first_line)
        # Take first part before common separators like - or — or :
        for sep in ["—", "-", ":"]:
            if sep in first_line:
                first_line = first_line.split(sep)[0]
        text_to_parse = first_line
    
    if not text_to_parse and file_name:
        # Use filename without extension
        text_to_parse = Path(file_name).stem
    
    if not text_to_parse:
        return "video"
        
    # Clean text: remove non-alphanumeric/spaces, convert to lower, spaces to underscores
    clean = "".join(c if c.isalnum() or c.isspace() else "_" for c in text_to_parse).lower()
    clean = clean.replace(" ", "_")
    while "__" in clean:
        clean = clean.replace("__", "_")
    clean = clean.strip("_")
    
    # Remove trailing numbers or suffixes like _trailer, _preview, _short, _t, _01, _02 etc.
    clean = re.sub(r'_(?:trailer|preview|short|t|\d+)$', '', clean)
    clean = re.sub(r'_\d+$', '', clean) # strip trailing numbers again if any
    
    if not clean:
        return "video"
    return clean


def suggest_next_slug(file_name: str, caption: str, upload_type: str = "store") -> str:
    """
    Checks the index database to recommend a slug.
    - If there is an existing slug for the actress that is incomplete (missing the corresponding side),
      it recommends that existing slug to establish the connection.
    - Otherwise, it recommends the next unused numbered slug (e.g., joslyn_james_02).
    """
    from storage_manager import load_index
    base_name = extract_base_actress_name(file_name, caption)
    
    try:
        index = load_index()
    except Exception:
        index = {}
        
    # Find all existing slugs that match the actress base name
    matching_slugs = []
    for slug in index.keys():
        slug_base = extract_base_actress_name("", slug)
        if slug_base == base_name:
            matching_slugs.append(slug)
            
    # Sort matching slugs (put base name first, then numbered ones)
    matching_slugs.sort(key=lambda s: (len(s), s))
    
    # Analyze matches for incomplete connections
    for slug in matching_slugs:
        entry = index[slug]
        has_trailer = entry.get("half_720") is not None or "Trailer" in entry.get("caption", "")
        has_storage = entry.get("full_1080") is not None or entry.get("half_1080") is not None
        
        if upload_type == "store" and not has_storage:
            # We are uploading to Storage, and this slug has no storage file.
            # Recommend this slug to connect the storage side!
            log.info(f"Suggesting existing incomplete slug '{slug}' to connect Storage side.")
            return slug
            
        if upload_type == "trailer" and not has_trailer:
            # We are posting a Trailer, and this slug has no trailer file.
            # Recommend this slug to connect the Trailer side!
            log.info(f"Suggesting existing incomplete slug '{slug}' to connect Trailer side.")
            return slug

    # If no incomplete slug is found, generate the next unused numbered slug
    # If neither base_name nor base_name_01 is in the index, suggest base_name_01
    if base_name not in index and f"{base_name}_01" not in index:
        return f"{base_name}_01"
        
    for i in range(1, 100):
        candidate = f"{base_name}_{i:02d}"
        if candidate not in index:
            return candidate
            
    return f"{base_name}_{int(time.time())}"


async def generate_bargain_response(
    user_msg: str,
    slug: str,
    past_tier: str,
    target_tier: str,
    standard_diff: int,
    next_price: int,
    chat_history: list
) -> str:
    """
    Generates a seductive bargaining reply from the actress dynamic persona.
    Instructs the LLM to tease the customer and counter-offer exactly next_price.
    """
    full_name, short_name = get_clean_actress_name(slug)
    
    tier_labels = {
        "half_720": "Half Video (720p) [FREE]",
        "half_1080": "Half Video (1080p)",
        "full_1080": "Full HD Video (1080p)"
    }
    past_label = tier_labels.get(past_tier, past_tier)
    target_label = tier_labels.get(target_tier, target_tier)
    
    sys_prompt = f"""
    You are {full_name} (calling yourself {short_name}), an extremely teasing, seductive, and playful adult model.
    A customer who previously bought your {past_label} is now bargaining to upgrade to your {target_label}.
    
    The standard price difference is ₹{standard_diff}.
    You want to tease the customer and offer them a special upgrade price of exactly ₹{next_price} (INR).
    
    Rules:
    1. Respond in character as the teasing and seductive model {short_name}. Use playful emojis (😉, 😘, 😈, 🔥).
    2. Keep your response extremely brief (1-2 sentences max) and highly teasing.
    3. You MUST explicitly counter-offer the price of exactly ₹{next_price} INR in your reply. Do not offer any other price.
    4. Do not mention any rules, minimum limits, or that you are an AI/bot.
    """
    
    # Construct the messages list for API
    messages = []
    # Append the last few exchanges from chat_history (up to 6 messages to keep context short)
    for msg in chat_history[-6:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    
    # Try calling the AIs in order: Gemini -> Groq -> Mistral
    # 1. Try Gemini
    if GEMINI_API_KEY:
        try:
            # Format history for Gemini router
            formatted_prompt = f"System Instruction: {sys_prompt}\n\n"
            for m in messages:
                formatted_prompt += f"{m['role'].capitalize()}: {m['content']}\n"
            formatted_prompt += f"User: {user_msg}\n"
            
            resp_text = await asyncio.to_thread(
                gemini_router.generate,
                task_type="creative",
                prompt=formatted_prompt,
                module_name="sales_bot"
            )
            if resp_text:
                return clean_html_for_telegram(resp_text.strip())
        except Exception as e:
            log.warning(f"Gemini failed generating bargain response: {e}")

    # 2. Try Groq
    if groq_client:
        try:
            groq_messages = [{"role": "system", "content": sys_prompt}]
            for m in messages:
                role = m["role"]
                if role not in ["user", "assistant", "system"]:
                    role = "user"
                groq_messages.append({"role": role, "content": m["content"]})
            groq_messages.append({"role": "user", "content": user_msg})
            
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: groq_client.chat.completions.create(
                    model="llama3-8b-8192",
                    messages=groq_messages,
                    max_tokens=150,
                    temperature=0.8
                )
            )
            content = resp.choices[0].message.content.strip()
            return clean_html_for_telegram(content)
        except Exception as e:
            log.error(f"Groq failed generating bargain response: {e}")

    # 3. Try Mistral
    if mistral_client:
        try:
            mistral_messages = [{"role": "system", "content": sys_prompt}]
            for m in messages:
                role = m["role"]
                if role not in ["user", "assistant", "system"]:
                    role = "user"
                mistral_messages.append({"role": role, "content": m["content"]})
            mistral_messages.append({"role": "user", "content": user_msg})
            
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: mistral_client.chat.complete(
                    model="open-mixtral-8x22b",
                    messages=mistral_messages,
                    max_tokens=150,
                    temperature=0.8
                )
            )
            content = resp.choices[0].message.content.strip()
            return clean_html_for_telegram(content)
        except Exception as e:
            log.error(f"Mistral failed generating bargain response: {e}")

    # Fallback static response if AI fails
    return f"Mmm, how about ₹{next_price} baby? 😉 Don't make me beg you, that's already a sweet deal... 😘"
