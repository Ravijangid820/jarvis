"""Central configuration, tunables, and logging for the Jarvis orchestrator.

Everything config-derived or constant lives here so the other modules don't each
re-read the JSON or duplicate magic numbers. No dependencies on other app modules.
"""
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List

# Project root. Defaults to the repo location (this file is at <root>/src/orchestrator/
# config.py) but can be overridden with JARVIS_HOME. Every on-disk path derives from it,
# so the app and tests can run from any checkout — not only /srv/jarvis. On the deployed
# box this resolves to /srv/jarvis, so paths are unchanged.
BASE_DIR = Path(os.environ.get("JARVIS_HOME") or Path(__file__).resolve().parents[2])

# App version. This project is an app, not an installed package (no [build-system] in pyproject —
# uv treats it as "virtual"), so package metadata usually doesn't exist; read pyproject.toml directly.
try:
    import tomllib
    APP_VERSION = tomllib.loads((BASE_DIR / "pyproject.toml").read_text())["project"]["version"]
except Exception:
    try:
        from importlib.metadata import version as _pkg_version
        APP_VERSION = _pkg_version("jarvis")
    except Exception:
        APP_VERSION = "unknown"

# --- Logging ----------------------------------------------------------------
# Rotating file handler so the log can't grow without bound (5 MB x 3 backups).
# stdout also goes to journald via systemd; rely on journald's own rotation there.
_LOG_DIR = BASE_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(_LOG_DIR / "orchestrator.log", maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("jarvis")

# --- Config file ------------------------------------------------------------
CONFIG_PATH = Path(os.environ.get("JARVIS_CONFIG") or (BASE_DIR / "config" / "jarvis.json"))
_EXAMPLE_CONFIG = BASE_DIR / "config" / "jarvis.example.json"


def load_config() -> dict:
    # Fall back to the committed example so the app/tests can load without a real config
    # (e.g. CI, a fresh checkout). The example carries every key with safe defaults.
    path = CONFIG_PATH if CONFIG_PATH.exists() else _EXAMPLE_CONFIG
    if not path.exists():
        logger.error("Config not found at %s (and no example at %s)", CONFIG_PATH, _EXAMPLE_CONFIG)
        raise SystemExit("FATAL: Config file missing. Cannot start without config.")
    if path != CONFIG_PATH:
        logger.warning("Config %s missing; using %s (example defaults)", CONFIG_PATH, path.name)
    with open(path, "r") as f:
        return json.load(f)


CONFIG = load_config()

# --- Frequently used values -------------------------------------------------
# Env override (JARVIS_FAST_BRAIN_URL) wins over the config file — used by the all-in-one container to
# point the orchestrator at a local llama-server (127.0.0.1) instead of the compose 'llama' hostname.
LLM_URL: str = os.environ.get("JARVIS_FAST_BRAIN_URL") or CONFIG["llm"]["fast_brain_url"]
REQUEST_TIMEOUT: int = CONFIG["llm"]["request_timeout_seconds"]
TEMPERATURE: float = CONFIG["llm"]["default_temperature"]
MAX_INPUT_LENGTH: int = CONFIG["orchestrator"]["max_input_length"]
RATE_LIMIT_RPM: int = CONFIG["orchestrator"]["rate_limit_requests_per_minute"]
# Opt-in: require a recognized, authorized person physically present (per the cameras) before any
# device control runs — even for an otherwise-authorized caller. Off by default.
REQUIRE_PRESENCE_FOR_CONTROL: bool = bool(CONFIG["orchestrator"].get("require_presence_for_device_control", False))
ALLOWED_ORIGINS: List[str] = CONFIG["orchestrator"].get("allowed_origins", [])
def _resolve(p: str) -> str:
    """Absolute paths pass through; relative ones resolve against BASE_DIR (so a fresh
    checkout keeps its data under the repo, while the deployed absolute paths are unchanged)."""
    path = Path(p)
    return str(path if path.is_absolute() else (BASE_DIR / path))


DB_PATH: str = _resolve(CONFIG["memory"]["db_path"])
CHROMA_DB_PATH: str = _resolve(CONFIG["memory"].get("chroma_db_path", "memory/chroma_db"))
MAX_CONTEXT_MESSAGES: int = CONFIG["memory"]["max_context_messages"]
SYSTEM_PROMPT: str = CONFIG["system_prompt"]

# --- Generation tuning (optional; edit config/jarvis.json, restart — no rebuild) -----------------
# Default sampling params forwarded to llama.cpp. Anything omitted uses the server's own default, so
# an empty/absent "sampling" block keeps current behavior. Tune generation without touching code.
_SAMPLING_KEYS = ("top_k", "top_p", "min_p", "repeat_penalty",
                  "presence_penalty", "frequency_penalty", "max_tokens", "seed")
SAMPLING_DEFAULTS: Dict[str, Any] = {
    k: v for k, v in dict(CONFIG["llm"].get("sampling") or {}).items() if k in _SAMPLING_KEYS
}
# Reasoning toggle for Qwen-style models (the "/no_think" control token). True = thinking on,
# False = off, None (key absent) = leave the system prompt exactly as written.
REASONING = CONFIG["llm"].get("reasoning", None)

# --- Prompt token budgeting -------------------------------------------------
# The llama-server is launched with a fixed context window (-c). prompt + generated
# tokens must fit inside it, or llama.cpp silently evicts the oldest prompt tokens.
MAX_CONTEXT_TOKENS: int = CONFIG["llm"].get("max_context_tokens", 4096)
COMPLETION_RESERVE_DEFAULT: int = 512   # tokens reserved for the answer if caller gives none
PROMPT_SAFETY_MARGIN: int = 96          # slack for the char-based token estimate + chat template
KNOWLEDGE_TOKEN_CAP: int = 512          # max tokens the injected user-profile block may consume
MIN_COMPLETION_TOKENS: int = 64         # never squeeze the answer below this

# --- Embeddings / RAG -------------------------------------------------------
# Configurable: set EMBED_MODEL (env) or an "embedding" block in jarvis.json to use a different model.
# Default = embeddinggemma-300m (gated; baked into the Docker image — see licenses/gemma/). Changing
# the model requires RE-INDEXING existing memories (different vector space) and usually different
# prompt prefixes below. embeddinggemma is ASYMMETRIC: documents and queries need distinct prefixes.
_EMBED_CFG = CONFIG.get("embedding") if isinstance(CONFIG.get("embedding"), dict) else {}
EMBED_MODEL_NAME = os.environ.get("EMBED_MODEL") or _EMBED_CFG.get("model") or "google/embeddinggemma-300m"
# Torch-free ONNX runtime: if this dir holds an exported model (export_embed_onnx.py), it is used
# instead of sentence-transformers/torch — provided its meta.json model matches EMBED_MODEL_NAME.
EMBED_ONNX_DIR = _resolve(os.environ.get("EMBED_ONNX_DIR") or _EMBED_CFG.get("onnx_dir") or "models/embed_onnx")

# --- Home Assistant (optional) ----------------------------------------------
# Feature is OFF unless url + token are set. Token: mint from a dedicated NON-ADMIN HA user; it stays
# server-side (config/env) and is never exposed to the LLM. allowed_entities is the hard allowlist the
# tool executor enforces — the model can only ever touch what's listed here.
_HA_CFG = CONFIG.get("home_assistant") if isinstance(CONFIG.get("home_assistant"), dict) else {}
HA_URL = (os.environ.get("HA_URL") or _HA_CFG.get("url") or "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN") or _HA_CFG.get("token") or ""
_ha_ents = os.environ.get("HA_ALLOWED_ENTITIES") or _HA_CFG.get("allowed_entities") or []
HA_ALLOWED_ENTITIES: List[str] = [e.strip() for e in (_ha_ents.split(",") if isinstance(_ha_ents, str) else _ha_ents) if e.strip()]
# When HA is set via ENVIRONMENT, the admin UI shows it read-only (env wins over the DB-stored,
# UI-managed settings). Set via jarvis.json or the UI → editable in the UI.
HA_URL_FROM_ENV = bool(os.environ.get("HA_URL"))
HA_TOKEN_FROM_ENV = bool(os.environ.get("HA_TOKEN"))
HA_ENTITIES_FROM_ENV = bool(os.environ.get("HA_ALLOWED_ENTITIES"))
EMBED_DOC_PREFIX = _EMBED_CFG.get("doc_prefix", "title: none | text: ")
EMBED_QUERY_PREFIX = _EMBED_CFG.get("query_prefix", "task: search result | query: ")
RAG_DISTANCE_THRESHOLD = 0.6  # cosine distance = 1 - similarity; discard > this
RAG_MAX_RESULTS = 5

# --- Fact extraction --------------------------------------------------------
IDLE_THRESHOLD_SECONDS = 120   # extract facts after 2 min of inactivity
IDLE_CHECK_INTERVAL = 30       # check for idle every 30 seconds
FACT_DEDUP_SIM = 0.90          # semantic-similarity merge threshold
FACT_DEDUP_WORD = 0.85         # word-overlap fallback threshold

FACT_EXTRACTION_PROMPT = """Analyze this conversation and extract any personal facts about the user.
Return a JSON array. Each fact must be a complete, self-contained sentence that would make sense on its own.

Categories: personal, family, preferences, location, work, education, interests, technical, other

Rules:
- Only extract FACTS the user explicitly stated about themselves. Do NOT infer or guess.
- Each fact must be a full sentence with context (e.g. "The user's name is Alex" not just "Alex").
- Include details, nicknames, relationships mentioned.
- If the user corrects previous info, extract the CORRECTED version.
- Skip greetings, questions, or generic statements.
- If no personal facts found, return exactly: []

Examples of good extractions:
[{"category": "personal", "content": "The user's name is Alex, also called Al by close friends"},
 {"category": "location", "content": "The user currently lives in Springfield"},
 {"category": "family", "content": "The user has a younger sibling who is studying medicine"},
 {"category": "preferences", "content": "The user's favourite car is the Tesla Model 3"},
 {"category": "work", "content": "The user works as a backend developer"},
 {"category": "technical", "content": "The user prefers Python and FastAPI for building APIs"}]

Return ONLY the JSON array, nothing else."""

VALID_FACT_CATEGORIES = {
    "personal", "family", "preferences", "location", "work",
    "education", "interests", "technical", "other",
    # household / global-knowledge categories
    "home", "household", "rooms", "devices", "people",
}

# --- Voice (Piper TTS) ------------------------------------------------------
PIPER_BIN = BASE_DIR / "piper" / "piper"
PIPER_MODEL = BASE_DIR / "piper" / "voices" / "en_GB-alan-medium.onnx"

# --- HTTP / static ----------------------------------------------------------
REACT_DIST_DIR = BASE_DIR / "frontend" / "dist"
STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = REACT_DIST_DIR / "index.html"
SCHEMA_PATH = BASE_DIR / "config" / "schema.sql"
ADMIN_MAX_INPUT = 10000
REGULAR_MAX_INPUT = 500
