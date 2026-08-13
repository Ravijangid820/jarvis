"""What Jarvis can truthfully say about itself.

Asked "how are you?", a 2B model answers from whatever state-like data is nearest in the prompt.
Measured on this box, 8 samples each: with the live device block attached — the normal case — it
drifted into reporting the house 6 times out of 8 ("I am functioning normally, sir. The lights
remain on, and the fan continues spinning."). Given its OWN status instead, 0 out of 8. It is not
ignoring instructions; it is answering with the only status it was handed.

Prompt wording does not fix this. Four system-prompt variants were measured, and the most promising
("answer about yourself and stop — the state of the home is not part of that answer") scored 0/5 on
the first run and 5/8 on a larger one: noise, not an effect. Adding a rule and *also* removing the
device nouns it named scored WORSE than the rule alone. At this size the reliable lever is what you
put in front of the model, not what you ask of it — the same conclusion the greeting fast path
reached.

Deliberately QUALITATIVE. An earlier version passed real figures and the model rendered "4 cores"
as "a single CPU core" in six replies out of eight — it had nothing to gain from the precision and
misread it anyway. "Load is light" cannot be garbled into a false number, and a person asking how
you are wants the qualitative answer regardless.
"""
import os
import time
from typing import Dict


def _read_meminfo() -> Dict[str, int]:
    try:
        with open("/proc/meminfo") as f:
            return {line.split(":")[0]: int(line.split()[1]) for line in f if ":" in line}
    except OSError:
        return {}


def _load_band(load1: float, cpus: int) -> str:
    """Load per core, as a word. Thresholds are the usual rule of thumb, not a measurement."""
    per_core = load1 / max(cpus, 1)
    if per_core < 0.7:
        return "light"
    return "moderate" if per_core < 1.5 else "heavy"


def _memory_band(used_pct: float) -> str:
    if used_pct < 70:
        return "comfortable"
    return "tight" if used_pct < 90 else "nearly full"


def self_status() -> Dict[str, str]:
    """A few true, unnumbered facts about this process and the machine under it."""
    cpus = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    mem = _read_meminfo()
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    used_pct = ((total - avail) / total * 100) if total else 0.0
    try:
        uptime_h = time.clock_gettime(time.CLOCK_BOOTTIME) / 3600
    except (AttributeError, OSError):
        uptime_h = 0.0
    if uptime_h < 1:
        up = "less than an hour"
    elif uptime_h < 48:
        up = f"about {uptime_h:.0f} hours"
    else:
        up = f"about {uptime_h / 24:.0f} days"
    return {"load": _load_band(load1, cpus), "memory": _memory_band(used_pct), "uptime": up}


def self_status_block() -> str:
    """The reference block attached when the message asks how JARVIS is, in place of the device
    block. Same shape as the device block so it lands in the prompt the same way."""
    s = self_status()
    return ("--- YOUR OWN STATUS (live, right now) ---\n"
            f"  You are running on this machine, locally: a small model on CPU, no GPU.\n"
            f"  Up {s['uptime']}. Load is {s['load']}. Memory is {s['memory']}.\n"
            f"  Language model, memory and speech are all responding.\n"
            "This block is about YOU, not about the home. Answer in one short sentence.\n"
            "---")
