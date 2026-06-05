import json
import logging
import logging.handlers
import os
import random
import re
import signal
import threading
import time
import hashlib
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Optional, Dict, List

# ── LOAD ENV EARLY ────────────────────────────────────────────────────────────
# GeminiGovernor is a singleton instantiated at import time.
# Try loading both the local directory .env and parent Credentials/.env
try:
    from dotenv import load_dotenv as _load_dotenv
    # 1. Local directory .env (highest priority)
    _local_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_local_env):
        _load_dotenv(_local_env, override=True)
    # 2. Master Credentials/.env (fallback)
    _master_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Credentials", ".env")
    if os.path.exists(_master_env):
        _load_dotenv(_master_env, override=False)
except Exception:
    pass
# ─────────────────────────────────────────────────────────────────────────────

from google import genai
from google.genai import types
import requests

try:
    from Diagnostics_Modules.gemini_trace import GeminiTrace
except ImportError:
    GeminiTrace = None

try:
    from langfuse import Langfuse
    _HAS_LANGFUSE = True
except ImportError:
    _HAS_LANGFUSE = False

logger = logging.getLogger("telegram_gemini_router")

# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL CIRCUIT BREAKER SINGLETON (Operation Beast Control v2)
# Shared across ALL calls in the entire process lifetime.
# ══════════════════════════════════════════════════════════════════════════════
_GLOBAL_GEMINI_STATE = {
    "gemini_down_until": 0.0,        # timestamp — skip all Gemini calls until this
    "consecutive_5xx": 0,            # capped at 5
    "last_fail_latency": 0.0,        # to distinguish quota (slow) vs random (fast)
}
_GLOBAL_STATE_LOCK = threading.Lock()

def is_gemini_globally_down() -> bool:
    """Check if the global circuit breaker is active."""
    with _GLOBAL_STATE_LOCK:
        return time.time() < _GLOBAL_GEMINI_STATE.get("gemini_down_until", 0)

def _record_gemini_5xx(latency: float):
    """Record a 5xx failure. Trip breaker if conditions met."""
    with _GLOBAL_STATE_LOCK:
        _GLOBAL_GEMINI_STATE["consecutive_5xx"] = min(
            _GLOBAL_GEMINI_STATE["consecutive_5xx"] + 1, 5
        )
        _GLOBAL_GEMINI_STATE["last_fail_latency"] = latency
        
        # Trip condition: 2+ consecutive failures (5xx server deaths)
        if (_GLOBAL_GEMINI_STATE["consecutive_5xx"] >= 2
                and _GLOBAL_GEMINI_STATE["last_fail_latency"] > 3.0):
            cooldown = 60 + random.uniform(0, 5)  # jitter prevents burst alignment
            _GLOBAL_GEMINI_STATE["gemini_down_until"] = time.time() + cooldown
            logger.warning(
                f"🛑 [CIRCUIT BREAKER] Gemini globally DOWN for {cooldown:.0f}s. "
                f"({_GLOBAL_GEMINI_STATE['consecutive_5xx']} consecutive 5xx, "
                f"avg latency {latency:.1f}s)"
            )

def _reset_gemini_circuit():
    """Reset the circuit breaker on a successful call."""
    with _GLOBAL_STATE_LOCK:
        _GLOBAL_GEMINI_STATE["consecutive_5xx"] = 0
        _GLOBAL_GEMINI_STATE["last_fail_latency"] = 0.0
# ══════════════════════════════════════════════════════════════════════════════

class GeminiGovernor:
    """
    V3.5 World-Class Intelligent Router.
    Handles multi-tier scoring, adaptive cooldowns, rate limiting, and persistence.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(GeminiGovernor, cls).__new__(cls)
                cls._instance._init_governor()
            return cls._instance

    def _init_governor(self):
        # Granular Locks for high-performance concurrency
        self.state_lock = threading.Lock()
        self.rate_limit_lock = threading.Lock()
        self.memory_lock = threading.Lock()

        # Langfuse Observability
        self.langfuse = None
        if _HAS_LANGFUSE:
            try:
                pk = os.getenv("LANGFUSE_PUBLIC_KEY")
                sk = os.getenv("LANGFUSE_SECRET_KEY")
                host = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
                if pk and sk:
                    self.langfuse = Langfuse(public_key=pk, secret_key=sk, host=host)
                    logger.info("🔭 [LANGFUSE] Observability Engine ACTIVE.")
            except Exception as _lf_err:
                logger.warning(f"⚠️ [LANGFUSE] Initialization failed: {_lf_err}")

        # Config from Environment
        self.MAX_RPM = int(os.getenv("GEMINI_MAX_REQUESTS_PER_MIN", 15))
        self.MAX_FPM = int(os.getenv("GEMINI_MAX_FALLBACKS_PER_MIN", 2))
        self.JITTER = float(os.getenv("GEMINI_JITTER_RANGE", 0.03))
        self.DECAY = float(os.getenv("GEMINI_DECAY_RATE", 0.99))
        self.MEMORY_TTL = int(os.getenv("GEMINI_MEMORY_TTL", 120))
        self.STATE_FILE = os.getenv("GEMINI_STATE_FILE", "Credentials/gemini_states.json")
        self.LOG_FILE = os.getenv("GEMINI_LOG_FILE", "logs/gemini_routing.json")
        self.REQUEST_DEADLINE = 300  # seconds — heavy vision payloads take ~40-60s
        self.global_cooldown_until = 0
        self.last_successful_model = None

        # Validation
        assert 0 <= self.JITTER < 0.1, "JITTER_RANGE must be between 0 and 0.1"
        assert self.MAX_RPM > 0, "MAX_REQUESTS_PER_MIN must be positive"

        # Model State & Metrics
        self.model_states = {}
        self.cache = {}
        self.stats = {
            "logical_requests": 0,
            "api_calls": 0,
            "cache_hits": 0,
            "blocked_calls": 0,
            "multi_task_calls": 0,
            "failures": 0,
            "calls_per_module": {},
            "payload_sizes": [],
        }

        # Sliding Window Queues
        self.request_timestamps = deque()
        self.session_locks = {} # {session_id: {task_type: model_name}}
        self.session_costs = {} # {session_id: total_calls}
        self.MAX_BUDGET = 20    # Threshold calls before force-lite
        self.fallback_timestamps = deque()
        self.recent_task_failures = {}  # { (model, task): timestamp }

        # ── VIDEO BUDGET SYSTEM ─────────────────────────────────────────────
        self._video_session_id: Optional[str] = None
        self._video_call_count: int = 0
        self._video_call_budget: int = 5
        self._video_budget_log: list = []

        self.TASK_PRIORITY = {
            "watermark":  1,
            "caption":    2,
            "narrative":  3,
            "price":      4,
            "reasoning":  4,
            "master":     4,
            "vision":     5,
            "analysis":   5,
        }

        self._setup_structured_logging()
        self.current_key_hash = self._get_key_hash()
        self._initialize_models()
        self._load_states()

    def _get_key_hash(self):
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            env_path = os.path.join("Credentials", ".env")
            if os.path.exists(env_path):
                try:
                    with open(env_path, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.startswith("GEMINI_API_KEY="):
                                key = line.split("=", 1)[1].strip().strip("'").strip('"')
                                break
                except Exception:
                    pass
        key = key or ""
        return hashlib.sha256(key.encode()).hexdigest()

    def _tick_ban_timers_unlocked(self):
        now = time.monotonic()
        elapsed = now - getattr(self, "last_time_check", now)
        self.last_time_check = now
        for m, state in self.model_states.items():
            if state.get("status") == "BANNED":
                rem = state.get("ban_remaining_seconds", 0) - elapsed
                if rem <= 0:
                    state.update({
                        "status": "ACTIVE",
                        "ban_remaining_seconds": 0,
                        "warmup_calls": 5
                    })
                else:
                    state["ban_remaining_seconds"] = rem

    def _setup_structured_logging(self):
        try:
            os.makedirs(os.path.dirname(self.LOG_FILE), exist_ok=True)
            self.routing_logger = logging.getLogger("gemini_routing")
            self.routing_logger.setLevel(logging.INFO)
            if not self.routing_logger.handlers:
                handler = logging.handlers.RotatingFileHandler(
                    self.LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                self.routing_logger.addHandler(handler)
        except Exception:
            pass

    def _initialize_models(self):
        models = [
            "gemini-2.5-pro",
            "gemini-pro-latest",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-2.0-flash-001",
            "gemini-flash-latest",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash-lite",
            "gemini-2.0-flash-lite-001",
            "gemini-flash-lite-latest",
        ]
        with self.state_lock:
            for m in models:
                if m not in self.model_states:
                    self.model_states[m] = {
                        "status": "ACTIVE",
                        "ban_remaining_seconds": 0,
                        "success_count": 5.0,
                        "total_calls": 5.0,
                        "429_count": 0,
                        "fail_count": 0,
                        "avg_latency": 0.5,
                        "last_used_at": 0,
                        "last_success_at": 0,
                        "warmup_calls": 0
                    }

    def begin_video_session(self, video_id: str, video_duration: float = 0.0):
        self._video_session_id = video_id
        self._video_call_count = 0
        self._video_budget_log = []
        if video_duration < 15:
            self._video_call_budget = 12
        elif video_duration < 60:
            self._video_call_budget = 18
        else:
            self._video_call_budget = 25
        logger.info(f"🎬 [BUDGET] New video session: {video_id} | budget={self._video_call_budget}")

    def _load_states(self):
        self.last_time_check = time.monotonic()
        if not os.path.exists(self.STATE_FILE):
            return
        try:
            with open(self.STATE_FILE, "r") as f:
                data = json.load(f)
                stored_hash = data.get("_key_hash")
                if stored_hash and stored_hash != self.current_key_hash:
                    logger.info("🔑 New API Key detected! Wiping states.")
                    return
                with self.state_lock:
                    for m, state in data.items():
                        if m.startswith("_"): continue
                        if m in self.model_states:
                            if "banned_until" in state:
                                state.pop("banned_until", None)
                                state["ban_remaining_seconds"] = 0
                            self.model_states[m].update(state)
        except Exception as e:
            logger.warning(f"Failed to load states: {e}")

    def _save_states(self):
        try:
            with self.state_lock:
                self._tick_ban_timers_unlocked()
                serializable = {"_key_hash": self.current_key_hash}
                for m, state in self.model_states.items():
                    s = state.copy()
                    serializable[m] = s
            temp_file = self.STATE_FILE + ".tmp"
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            with open(temp_file, "w") as f:
                json.dump(serializable, f, indent=2)
            os.replace(temp_file, self.STATE_FILE)
        except Exception as e:
            logger.warning(f"Failed to save states: {e}")

    def is_model_available(self, model_name):
        with self.state_lock:
            self._tick_ban_timers_unlocked()
            state = self.model_states.get(model_name)
            if not state: return True
            return state["status"] != "BANNED"

    def _clean_window(self, queue, window=60):
        now = time.monotonic()
        while queue and now - queue[0] > window:
            queue.popleft()

    def can_make_request(self):
        with self.rate_limit_lock:
            self._clean_window(self.request_timestamps)
            return len(self.request_timestamps) < self.MAX_RPM

    def can_make_fallback(self):
        with self.rate_limit_lock:
            self._clean_window(self.fallback_timestamps)
            return len(self.fallback_timestamps) < self.MAX_FPM

    def _add_request_event(self, is_fallback=False):
        now = time.monotonic()
        with self.rate_limit_lock:
            self.request_timestamps.append(now)
            if is_fallback:
                self.fallback_timestamps.append(now)

    def mark_model_banned(self, model_name, error_type="429", seconds: Optional[int] = None):
        with self.state_lock:
            self._tick_ban_timers_unlocked()
            state = self.model_states.get(model_name)
            if not state: return
            if seconds is not None:
                state["status"] = "BANNED"
                state["ban_remaining_seconds"] = seconds
                return
            if error_type == "429":
                state["429_count"] += 1
                duration_sec = 45 if state["429_count"] == 1 else (90 if state["429_count"] == 2 else 240)
                state["status"] = "BANNED"
                state["ban_remaining_seconds"] = duration_sec
                logger.warning(f"🚫 Model {model_name} BANNED for {duration_sec}s due to 429.")
            elif error_type == "timeout":
                state["status"] = "BANNED"
                state["ban_remaining_seconds"] = 30
                logger.warning(f"⏳ Model {model_name} isolated for 30s due to Timeout.")
            elif error_type == "5xx":
                state["status"] = "BANNED"
                state["ban_remaining_seconds"] = 90
                logger.warning(f"🔥 Model {model_name} isolated for 90s due to Server Error.")
            elif error_type == "safety":
                state["status"] = "BANNED"
                state["ban_remaining_seconds"] = 300
                logger.warning(f"🛡️ Model {model_name} BANNED for 300s due to Safety Block.")

    def _get_cache_key(self, prompt: Any, metadata: Dict[str, Any]) -> str:
        p_str = str(prompt)
        m_str = json.dumps(metadata, sort_keys=True)
        raw = f"{p_str}_{m_str}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get_available_model(self, task_type, session_id=None, exclude_models=None):
        exclude_set = set(exclude_models) if exclude_models else set()
        if session_id and session_id in self.session_locks:
            locked_model = self.session_locks[session_id].get(task_type)
            if locked_model and locked_model not in exclude_set and self.model_states.get(locked_model, {}).get("status") != "BANNED":
                return locked_model

        force_lite = False
        if session_id and self.session_costs.get(session_id, 0) > self.MAX_BUDGET:
            if task_type not in ["watermark", "master", "vision"]:
                force_lite = True

        with self.state_lock:
            self._tick_ban_timers_unlocked()
            boosts = {
                "creative": {
                    "gemini-2.5-flash-lite": 3.0, "gemini-flash-lite-latest": 2.9,
                    "gemini-2.0-flash-lite": 2.8, "gemini-2.5-flash": 2.7,
                    "gemini-2.0-flash": 2.6, "gemini-flash-latest": 2.5,
                    "gemini-2.5-pro": 1.5, "gemini-pro-latest": 1.1,
                },
                "reasoning": {
                    "gemini-2.5-flash-lite": 3.0, "gemini-2.0-flash-lite": 2.9,
                    "gemini-2.5-flash": 2.8, "gemini-2.0-flash": 2.7,
                    "gemini-flash-latest": 2.6, "gemini-2.5-pro": 1.7,
                    "gemini-pro-latest": 1.1,
                },
                "cheap": {
                    "gemini-2.5-flash-lite": 3.9, "gemini-flash-lite-latest": 3.8,
                    "gemini-2.0-flash-lite": 3.7, "gemini-2.5-flash": 2.0,
                    "gemini-2.0-flash": 1.8,
                },
                "master": {
                    "gemini-2.5-flash": 3.2, "gemini-2.0-flash": 3.0,
                    "gemini-2.5-flash-lite": 2.7, "gemini-2.5-pro": 1.7,
                },
                "watermark": {
                    "gemini-2.5-flash": 4.0, "gemini-2.0-flash": 3.8,
                    "gemini-2.5-flash-lite": 2.5, "gemini-2.5-pro": 1.5,
                },
                "vision": {
                    "gemini-2.5-flash-lite": 3.8, "gemini-2.5-flash": 3.5,
                    "gemini-2.5-pro": 1.5,
                },
                "caption": {
                    "gemini-2.5-flash": 3.5, "gemini-2.0-flash": 3.3,
                    "gemini-2.5-flash-lite": 3.0,
                },
                "narrative": {
                    "gemini-2.5-flash": 3.3, "gemini-2.5-flash-lite": 2.8,
                },
                "price": {
                    "gemini-2.5-flash-lite": 3.8, "gemini-2.5-flash": 3.3,
                },
                "analysis": {
                    "gemini-2.5-flash-lite": 3.7, "gemini-2.5-flash": 3.4,
                }
            }
            task_boost = boosts.get(task_type, {})
            cost_weights = {"pro": 1.0, "flash": 0.7, "lite": 0.3}
            best_model = None
            max_score = -float('inf')
            now = time.monotonic()

            for name, state in self.model_states.items():
                if state["status"] == "BANNED": continue
                if name in exclude_set: continue
                if "1.5" in name: continue

                with self.memory_lock:
                    fail_time = self.recent_task_failures.get((name, task_type))
                    if fail_time and now - fail_time < self.MEMORY_TTL:
                        continue

                total = state["total_calls"]
                success_rate = (state["success_count"] / total) if total > 0 else 1.0
                state["total_calls"] *= self.DECAY
                state["success_count"] *= self.DECAY
                state.setdefault("fail_count", 0)
                state["fail_count"] *= 0.9

                reliability_penalty = (state["fail_count"] * 10.0)
                latency_penalty = (state["avg_latency"] * 0.4)
                freshness = min(1.0, (now - state["last_used_at"]) / 300) if state["last_used_at"] > 0 else 1.0
                cold_start_penalty = 5.0 if total < 5 else 0.0

                if force_lite and "lite" not in name: continue

                base_score = (success_rate * 50) - reliability_penalty - latency_penalty - cold_start_penalty + (freshness * 5)
                boost_val = task_boost.get(name, 1.0)
                c_type = "pro" if "pro" in name else ("lite" if "lite" in name else "flash")
                cost_inv = 1.0 / cost_weights.get(c_type, 0.7)
                jitter = random.uniform(-self.JITTER, self.JITTER)

                final_score = (base_score * boost_val * cost_inv) + jitter
                if state.get("warmup_calls", 0) > 0:
                    final_score *= 0.5

                if final_score > max_score:
                    max_score = final_score
                    best_model = name

            if best_model:
                if session_id:
                    if session_id not in self.session_locks:
                        self.session_locks[session_id] = {}
                    self.session_locks[session_id][task_type] = best_model
                    self.session_costs[session_id] = self.session_costs.get(session_id, 0) + 1
                return best_model
            return None

    def simplify_prompt(self, prompt: Any, tier: str = "high") -> Any:
        if tier == "high": return prompt
        if isinstance(prompt, list):
            new_prompt = []
            for item in prompt:
                if isinstance(item, str):
                    mid_limit = 8000
                    if tier == "mid" and len(item) > mid_limit:
                        new_prompt.append(item[:mid_limit] + "\n[TRUNCATED]")
                    elif tier == "low":
                        new_prompt.append(f"Simplify decision:\n{item[:500]}")
                    else:
                        new_prompt.append(item)
                else:
                    new_prompt.append(item)
            return new_prompt

        p_str = str(prompt)
        if tier == "mid":
            return p_str[:2000] + "\n[TRUNCATED]" if len(p_str) > 2000 else prompt
        return f"Simplify decision:\n{p_str[:500]}"

    def _call_ollama(self, prompt: Any) -> Optional[str]:
        if isinstance(prompt, list):
            prompt = "\n".join([str(item) for item in prompt if isinstance(item, str)])
        else:
            prompt = str(prompt)
        url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
        model = os.getenv("OLLAMA_MODEL", "phi3")
        try:
            payload = {"model": model, "prompt": prompt, "stream": False}
            response = requests.post(url, json=payload, timeout=15)
            if response.status_code == 200:
                return response.json().get("response")
            return None
        except Exception:
            return None

    def generate(
        self,
        task_type,
        prompt,
        metadata=None,
        module_name="unknown",
        gen_config=None,
        safety_settings=None,
        existing_confidence=None,
        model_name: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        self.stats["logical_requests"] += 1
        self.stats["calls_per_module"][module_name] = self.stats["calls_per_module"].get(module_name, 0) + 1

        if is_gemini_globally_down():
            logger.warning(f"🛑 [CIRCUIT] Gemini globally down. Skipping '{task_type}'")
            return None

        metadata = metadata or {}
        cache_key = self._get_cache_key(prompt, metadata)
        if cache_key in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[cache_key]

        # ── ORCHESTRA FAST LANE (Groq + Mistral fallback) ─────────────────────
        TEXT_TASKS = {"reasoning", "narrative", "price", "analysis", "caption", "master", "creative"}
        is_escalated = False
        if task_type in TEXT_TASKS and isinstance(prompt, str):
            try:
                import sys
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from Intelligence_Modules.router_orchestra import orchestra
                orch_result = orchestra.route(
                    prompt=prompt,
                    task_type=task_type,
                    visual_context=metadata.get("visual_context")
                )
                if orch_result:
                    self.cache[cache_key] = orch_result
                    return orch_result
                is_escalated = True
            except Exception:
                pass

        # ── PRIORITY BUDGET GATE ─────────────────────────────────────────────
        if self._video_session_id is not None:
            task_priority = self.TASK_PRIORITY.get(task_type, 5)
            remaining = self._video_call_budget - self._video_call_count
            if not is_escalated:
                if task_priority >= 5 and remaining <= 1:
                    return None
                if self._video_call_count >= self._video_call_budget:
                    return None
            self._video_call_count += 1

        trace = None
        if self.langfuse:
            try:
                trace = self.langfuse.trace(
                    name=f"amtce_{task_type}",
                    tags=[module_name, "production"],
                    metadata={"module": module_name}
                )
            except: pass

        start_time = time.time()
        attempts = 0
        MAX_ATTEMPTS = 8
        tried_models = set()
        last_error = "Unknown"

        while attempts < MAX_ATTEMPTS:
            elapsed = time.time() - start_time
            if elapsed > self.REQUEST_DEADLINE:
                break

            current_model = None
            if model_name and attempts == 0:
                if self.is_model_available(model_name):
                    current_model = model_name
            if not current_model:
                current_model = self.get_available_model(task_type, session_id=session_id, exclude_models=tried_models)

            if not current_model:
                if attempts > 0:
                    with _GLOBAL_STATE_LOCK:
                         _GLOBAL_GEMINI_STATE["gemini_down_until"] = time.time() + 60.0
                break

            prompt_tier = "high" if attempts < 4 else ("mid" if attempts < 6 else "low")
            active_prompt = self.simplify_prompt(prompt, prompt_tier)
            self.stats["api_calls"] += 1
            call_start = time.monotonic()

            generation = None
            if trace:
                try:
                    generation = trace.generation(name=f"attempt_{attempts+1}", model=current_model)
                except: pass

            try:
                api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
                if not api_key: raise Exception("Missing Gemini API Key")
                client = genai.Client(api_key=api_key)

                config_params = {}
                if gen_config:
                    config_params.update(gen_config)
                if safety_settings:
                    config_params["safety_settings"] = safety_settings

                response = client.models.generate_content(
                    model=current_model,
                    contents=active_prompt,
                    config=types.GenerateContentConfig(**config_params) if config_params else None
                )

                duration_ms = int((time.monotonic() - call_start) * 1000)
                if not response.candidates:
                     raise Exception("GEMINI_SAFETY_BLOCK")
                result = response.text

                _reset_gemini_circuit()
                with self.state_lock:
                    state = self.model_states[current_model]
                    state["success_count"] += 1
                    state["total_calls"] += 1
                    state["avg_latency"] = (state["avg_latency"] * 0.7) + ((duration_ms/1000.0) * 0.3)
                    state["last_used_at"] = time.monotonic()

                self.cache[cache_key] = result
                if generation:
                    try: generation.end(output=result)
                    except: pass
                return result

            except Exception as e:
                err_msg = str(e).lower()
                call_latency = time.monotonic() - call_start

                if "gemini_safety_block" in err_msg:
                    self.mark_model_banned(current_model, "safety")
                    tried_models.add(current_model)
                    attempts += 1
                    continue

                if "429" in err_msg or "quota" in err_msg or "resource_exhausted" in err_msg:
                    self.mark_model_banned(current_model, "429")
                    tried_models.add(current_model)
                    attempts += 1
                    continue

                if "api key expired" in err_msg or "api_key_invalid" in err_msg:
                    break

                error_type = "5xx" if "500" in err_msg else "timeout"
                self.mark_model_banned(current_model, error_type=error_type)
                tried_models.add(current_model)
                attempts += 1
                self.stats["failures"] += 1

                if error_type == "5xx":
                    _record_gemini_5xx(call_latency)
                    if is_gemini_globally_down(): break

                backoff_sec = min(2 ** attempts, 10)
                time.sleep(backoff_sec + random.uniform(0, 1.0))

        # Fallback to local Ollama
        result = self._call_ollama(self.simplify_prompt(prompt, "low"))
        if result:
            self.cache[cache_key] = result
            return result

        if task_type in ("caption", "creative", "narrative"):
             return "Trending Fashion Style"

        return None

    def embed(self, text, model_name="text-embedding-004", module_name="unknown"):
        try:
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not api_key: return []
            client = genai.Client(api_key=api_key)
            try:
                result = client.models.embed_content(model=model_name, contents=text)
                if hasattr(result, 'embeddings') and result.embeddings:
                    emb = result.embeddings[0]
                    return emb.values if hasattr(emb, 'values') else emb
                return result.get('embedding', [])
            except Exception:
                result = client.models.embed_content(model="models/embedding-001", contents=text)
                if hasattr(result, 'embeddings') and result.embeddings:
                    emb = result.embeddings[0]
                    return emb.values if hasattr(emb, 'values') else emb
                return []
        except Exception:
            return []

    def print_usage_report(self):
        print(f"📊 [TELEGRAM_GEMINI_ROUTER] total_calls={self.stats['api_calls']} | failures={self.stats['failures']}")

gemini_router = GeminiGovernor()
