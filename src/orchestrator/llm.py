"""LLM HTTP client (blocking + streaming) and Piper text-to-speech."""
import base64
import json
import subprocess
import urllib.request
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from config import LLM_URL, PIPER_BIN, PIPER_MODEL, REQUEST_TIMEOUT, TEMPERATURE, logger


def _build_payload(messages, temperature, top_k, top_p, min_p, repeat_penalty,
                   presence_penalty, frequency_penalty, n_predict, seed, stream):
    data: Dict[str, Any] = {
        "messages": messages,
        "temperature": temperature if temperature is not None else TEMPERATURE,
        "stream": stream,
    }
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
    """Render text to speech via Piper, returning base64 WAV (or None if unavailable/failed)."""
    if not text or not (PIPER_BIN.exists() and PIPER_MODEL.exists()):
        return None
    try:
        proc = subprocess.run(
            [str(PIPER_BIN), "--model", str(PIPER_MODEL), "--output_file", "-"],
            input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout:
            return base64.b64encode(proc.stdout).decode("utf-8")
    except Exception as e:
        logger.warning("Piper TTS failed: %s", e)
    return None
