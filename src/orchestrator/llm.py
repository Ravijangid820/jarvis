"""LLM HTTP client (blocking + streaming) and Piper text-to-speech."""
import base64
import hashlib
import json
import os
import subprocess
import urllib.request
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from config import (BASE_DIR, LLM_URL, PIPER_BIN, PIPER_MODEL, REQUEST_TIMEOUT, SAMPLING_DEFAULTS,
                    TEMPERATURE, logger)

# --- TTS cache: synthesized audio is deterministic for (voice model, text), so cache it on disk and
# replay on a hit instead of re-running Piper. Lossless (identical bytes); survives restarts. -------
_TTS_CACHE_DIR = BASE_DIR / ".cache" / "tts"
_TTS_CACHE_MAX = 500                      # keep the newest N phrases; prune the rest


def _tts_cache_key(text: str) -> str:
    h = hashlib.sha256()
    h.update(str(PIPER_MODEL).encode("utf-8"))   # a voice change invalidates old audio
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _tts_cache_get(key: str) -> Optional[str]:
    p = _TTS_CACHE_DIR / key
    try:
        b64 = p.read_text()
        os.utime(p, None)                 # mark as recently used (mtime = LRU)
        return b64
    except OSError:
        return None


def _tts_cache_put(key: str, b64: str) -> None:
    try:
        _TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _TTS_CACHE_DIR / f".{key}.{os.getpid()}.tmp"
        tmp.write_text(b64)
        os.replace(tmp, _TTS_CACHE_DIR / key)        # atomic publish
        files = [f for f in _TTS_CACHE_DIR.iterdir() if not f.name.startswith(".")]
        if len(files) > _TTS_CACHE_MAX:              # evict oldest beyond the cap
            for f in sorted(files, key=lambda f: f.stat().st_mtime)[:len(files) - _TTS_CACHE_MAX]:
                f.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("TTS cache write skipped: %s", e)   # caching is best-effort, never fatal


def _build_payload(messages, temperature, top_k, top_p, min_p, repeat_penalty,
                   presence_penalty, frequency_penalty, n_predict, seed, stream):
    data: Dict[str, Any] = {
        "messages": messages,
        "temperature": temperature if temperature is not None else TEMPERATURE,
        "stream": stream,
        # Reuse the server's KV cache for the common prefix across turns — only the new tokens get
        # processed instead of re-evaluating the whole context every turn (huge on a slow CPU).
        # Effective only because build_messages keeps the leading system message + history stable.
        "cache_prompt": True,
    }
    # Where the caller didn't override a param, fall back to the config "sampling" defaults (absent
    # key -> None -> the value is omitted and llama.cpp uses its own default). Back-compat: an empty
    # SAMPLING_DEFAULTS leaves the request identical to before.
    g = SAMPLING_DEFAULTS.get
    if top_k is None: top_k = g("top_k")
    if top_p is None: top_p = g("top_p")
    if min_p is None: min_p = g("min_p")
    if repeat_penalty is None: repeat_penalty = g("repeat_penalty")
    if presence_penalty is None: presence_penalty = g("presence_penalty")
    if frequency_penalty is None: frequency_penalty = g("frequency_penalty")
    if n_predict is None: n_predict = g("max_tokens")
    if seed is None: seed = g("seed")
    if top_k is not None: data["top_k"] = top_k
    if top_p is not None: data["top_p"] = top_p
    if min_p is not None: data["min_p"] = min_p
    if repeat_penalty is not None: data["repeat_penalty"] = repeat_penalty
    if presence_penalty is not None: data["presence_penalty"] = presence_penalty
    if frequency_penalty is not None: data["frequency_penalty"] = frequency_penalty
    if n_predict is not None: data["max_tokens"] = n_predict
    if seed is not None: data["seed"] = seed
    return data


def request_llm(messages: List[Dict[str, str]], temperature=None, top_k=None, top_p=None, min_p=None,
                repeat_penalty=None, presence_penalty=None, frequency_penalty=None, n_predict=None,
                seed=None) -> Dict[str, Any]:
    data = _build_payload(messages, temperature, top_k, top_p, min_p, repeat_penalty,
                          presence_penalty, frequency_penalty, n_predict, seed, stream=False)
    req = urllib.request.Request(LLM_URL, data=json.dumps(data).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.error("LLM error: %s", e)
        raise HTTPException(status_code=503, detail="AI backend error")


def request_llm_tools(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]],
                      temperature=None) -> Dict[str, Any]:
    """One non-streaming call that offers the model `tools`. It either returns tool_calls (a command)
    or plain content (a normal answer) — a single round-trip, so no extra latency vs a normal reply."""
    data: Dict[str, Any] = {
        "messages": messages,
        "temperature": temperature if temperature is not None else TEMPERATURE,
        "stream": False, "cache_prompt": True,
        "tools": tools, "tool_choice": "auto",
    }
    req = urllib.request.Request(LLM_URL, data=json.dumps(data).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.error("LLM tools error: %s", e)
        raise HTTPException(status_code=503, detail="AI backend error")


def request_llm_stream(messages: List[Dict[str, str]], temperature=None, top_k=None, top_p=None, min_p=None,
                       repeat_penalty=None, presence_penalty=None, frequency_penalty=None, n_predict=None,
                       seed=None):
    data = _build_payload(messages, temperature, top_k, top_p, min_p, repeat_penalty,
                          presence_penalty, frequency_penalty, n_predict, seed, stream=True)
    req = urllib.request.Request(LLM_URL, data=json.dumps(data).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            for line in response:
                line = line.decode("utf-8").strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                        if "choices" in chunk and chunk["choices"]:
                            content = chunk["choices"][0]["delta"].get("content", "")
                            if content:
                                yield content
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        logger.error("LLM streaming error: %s", e)
        raise


def llm_content(resp: Dict[str, Any]) -> str:
    """Safely pull the assistant text out of an OpenAI-style response.

    llama-server can return an error object or an empty choices list; indexing it
    positionally would surface as an unhandled 500. We validate and raise 503 instead.
    """
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.error("Unexpected LLM response shape: %s", str(resp)[:300])
        raise HTTPException(status_code=503, detail="AI backend returned an unexpected response")


def synthesize_tts(text: str) -> Optional[str]:
    """Render text to speech via Piper, returning base64 WAV (or None if unavailable/failed).
    Cached on disk per (voice model, text) — a repeated phrase replays without re-running Piper."""
    if not text or not (PIPER_BIN.exists() and PIPER_MODEL.exists()):
        return None
    key = _tts_cache_key(text)
    hit = _tts_cache_get(key)
    if hit is not None:
        return hit
    try:
        proc = subprocess.run(
            [str(PIPER_BIN), "--model", str(PIPER_MODEL), "--output_file", "-"],
            input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout:
            b64 = base64.b64encode(proc.stdout).decode("utf-8")
            _tts_cache_put(key, b64)
            return b64
    except Exception as e:
        logger.warning("Piper TTS failed: %s", e)
    return None
