import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Same guarded temp-home setup as the other modules that touch the database: config caches its
# paths on first import, so whichever test module gets there first decides where the DB lives.
os.environ.setdefault("JARVIS_NO_EMBED", "1")
if "config" not in sys.modules:
    _TMP = Path(tempfile.mkdtemp())
    (_TMP / "config").mkdir()
    (_TMP / "config" / "schema.sql").write_text((REPO / "config" / "schema.sql").read_text())
    _cfg = json.loads((REPO / "config" / "jarvis.example.json").read_text())
    _cfg["memory"]["db_path"] = str(_TMP / "test.db")
    _cfg["memory"]["chroma_db_path"] = str(_TMP / "chroma")
    (_TMP / "config" / "jarvis.json").write_text(json.dumps(_cfg))
    os.environ["JARVIS_HOME"] = str(_TMP)

sys.path.insert(0, str(REPO / "src" / "orchestrator"))

import chat  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import memory  # noqa: E402


def test_sampling_defaults_fill_in_and_caller_overrides(monkeypatch):
    """Config sampling defaults apply when the caller passes None; an explicit value still wins."""
    monkeypatch.setattr(llm, "SAMPLING_DEFAULTS", {"top_k": 40, "repeat_penalty": 1.1, "max_tokens": 256})
    msgs = [{"role": "user", "content": "hi"}]

    p = llm._build_payload(msgs, None, None, None, None, None, None, None, None, None, False)
    assert p["top_k"] == 40 and p["repeat_penalty"] == 1.1 and p["max_tokens"] == 256

    p2 = llm._build_payload(msgs, None, 99, None, None, None, None, None, None, None, False)
    assert p2["top_k"] == 99  # caller wins over the config default


def test_no_sampling_defaults_keeps_payload_minimal(monkeypatch):
    """Empty defaults (the back-compat case) omit the optional keys entirely."""
    monkeypatch.setattr(llm, "SAMPLING_DEFAULTS", {})
    p = llm._build_payload([{"role": "user", "content": "hi"}], None, None, None, None, None, None, None, None, None, False)
    for k in ("top_k", "top_p", "repeat_penalty", "max_tokens", "seed"):
        assert k not in p


# --- the reasoning toggle, through the function that actually builds the prompt ---------------
#
# This was a hand-written copy of chat.build_messages' reasoning handling, asserted against
# itself. It agreed with the real code only for as long as nobody edited one of them.


@pytest.fixture(scope="module", autouse=True)
def _schema():
    db.init_db()


@pytest.fixture(autouse=True)
def _no_knowledge(monkeypatch):
    """build_messages appends any stored household/user knowledge to the system message. The whole
    suite shares one temp database, so what those tables hold depends on which modules ran first —
    stub them out and the assertions below can compare the prompt exactly."""
    monkeypatch.setattr(memory, "get_global_knowledge", lambda household_id: "")
    monkeypatch.setattr(memory, "get_user_knowledge", lambda user_id: "")


def _system_prompt(reasoning, prompt):
    """The system message build_messages produces for a given reasoning setting."""
    messages = chat.build_messages("reasoning-test-session", user_id=1, household_id=1,
                                   user_text="hello", custom_sys_prompt=prompt,
                                   reasoning=reasoning)
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


def test_reasoning_on_strips_the_no_think_token():
    assert _system_prompt(True, "You are Jarvis. /no_think") == "You are Jarvis."


def test_reasoning_off_appends_the_no_think_token():
    assert _system_prompt(False, "You are Jarvis.") == "You are Jarvis. /no_think"


def test_reasoning_off_is_idempotent():
    """Qwen takes the token once; appending a second one is at best noise in the prefix."""
    assert _system_prompt(False, "You are Jarvis. /no_think") == "You are Jarvis. /no_think"


def test_no_request_level_setting_falls_back_to_the_config(monkeypatch):
    """None means "whatever the deployment configured", which is how the UI leaves it alone."""
    monkeypatch.setattr(chat, "REASONING", None)
    assert _system_prompt(None, "You are Jarvis. /no_think") == "You are Jarvis. /no_think"
    monkeypatch.setattr(chat, "REASONING", True)
    assert _system_prompt(None, "You are Jarvis. /no_think") == "You are Jarvis."
    monkeypatch.setattr(chat, "REASONING", False)
    assert _system_prompt(None, "You are Jarvis.") == "You are Jarvis. /no_think"


def test_a_request_setting_overrides_the_config(monkeypatch):
    """The per-request flag is what the UI's toggle sends; it has to win over the config default."""
    monkeypatch.setattr(chat, "REASONING", False)
    assert _system_prompt(True, "You are Jarvis. /no_think") == "You are Jarvis."
