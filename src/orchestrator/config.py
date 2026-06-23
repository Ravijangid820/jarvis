"""Central configuration, tunables, and logging for the Jarvis orchestrator.

Everything config-derived or constant lives here so the other modules don't each
re-read the JSON or duplicate magic numbers. No dependencies on other app modules.
"""
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List

# Project root. Defaults to the repo location (this file is at <root>/src/orchestrator/
# config.py) but can be overridden with JARVIS_HOME. Every on-disk path derives from it,
# so the app and tests can run from any checkout — not only /srv/jarvis. On the deployed
# box this resolves to /srv/jarvis, so paths are unchanged.
BASE_DIR = Path(os.environ.get("JARVIS_HOME") or Path(__file__).resolve().parents[2])

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
LLM_URL: str = CONFIG["llm"]["fast_brain_url"]
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

# --- Prompt token budgeting -------------------------------------------------
# The llama-server is launched with a fixed context window (-c). prompt + generated
# tokens must fit inside it, or llama.cpp silently evicts the oldest prompt tokens.
MAX_CONTEXT_TOKENS: int = CONFIG["llm"].get("max_context_tokens", 4096)
COMPLETION_RESERVE_DEFAULT: int = 512   # tokens reserved for the answer if caller gives none
PROMPT_SAFETY_MARGIN: int = 96          # slack for the char-based token estimate + chat template
KNOWLEDGE_TOKEN_CAP: int = 512          # max tokens the injected user-profile block may consume
MIN_COMPLETION_TOKENS: int = 64         # never squeeze the answer below this

# --- Embeddings / RAG -------------------------------------------------------
# embeddinggemma-300m is ASYMMETRIC: documents and queries need different prompt prefixes.
EMBED_MODEL_NAME = "google/embeddinggemma-300m"
EMBED_DOC_PREFIX = "title: none | text: "
EMBED_QUERY_PREFIX = "task: search result | query: "
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
- Each fact must be a full sentence with context (e.g. "The user's name is Ravi" not just "Ravi").
- Include details, nicknames, relationships mentioned.
- If the user corrects previous info, extract the CORRECTED version.
- Skip greetings, questions, or generic statements.
- If no personal facts found, return exactly: []

Examples of good extractions:
[{"category": "personal", "content": "The user's name is Ravi, also called Ravi bhai by friends"},
 {"category": "location", "content": "The user currently lives in Pune, Maharashtra"},
 {"category": "family", "content": "The user has a younger sister named Priya who is studying medicine"},
 {"category": "preferences", "content": "The user's favourite car is the Tesla Model 3"},
 {"category": "work", "content": "The user works as a backend developer at Infosys"},
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
