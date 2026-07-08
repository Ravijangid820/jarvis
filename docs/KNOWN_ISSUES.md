# Known issues & limitations

The living tracker: quirks, accepted trade-offs, and open limitations — with status and the
workaround/fix for each. Add new ones at the top of the relevant section; move items to *Resolved*
rather than deleting them. (Security-audit findings live in [AUDIT.md](AUDIT.md); future *features*
in [FUTURE_IDEAS.md](FUTURE_IDEAS.md) — this file is for things a user or operator can bump into
today.)

## Open

| # | Issue | Impact | Workaround / planned fix |
|---|---|---|---|
| 1 | **Box can't *initiate* connections to off-LAN / tailnet-only devices.** The Tailscale subnet router makes the box reachable *inbound* (SNAT return path), but the box has no tailnet interface of its own — so it cannot reach a device on another network (found 2026-07-07: HA on a hotspot-connected laptop was unreachable). | Integrations (HA, future off-LAN agents) must be on the box's LAN. | Run the peer on the box's LAN (HA as a Proxmox container — done, and the production shape anyway). Fix: install Tailscale in the LXC (`/dev/net/tun` or userspace mode). See FUTURE_IDEAS → Networking. |
| 2 | **LLM tool-calling is wired into `/inbox` only — the streaming web chat (`/chat/stream`) sends NO tools to the model**, and the 2B model's tool-calling is not fully reliable anyway (may answer in prose, or hallucinate). | In web chat, device commands the fast-paths don't recognize get a made-up text answer instead of an action. | Deterministic fast-paths (`intents.py`: volume, reminders, **home on/off/toggle/status** since 2026-07-08) run before the LLM on BOTH endpoints and cover the common phrasings instantly. Planned: streaming tool-call support + GBNF grammar-constrained output (FUTURE_IDEAS). |
| 3 | **Reminders fire only while a client is polling** (web UI open). No push channel / spoken announcement on the box yet. | A reminder due while no UI is open surfaces late. | Keep a UI open, or wait for the announce-on-box / push work (FUTURE_IDEAS). |
| 4 | **Presence is a 180 s window and arrival state is in-memory.** Someone who leaves can read "present" for ~3 min; an orchestrator restart re-greets on next sighting. | Presence-gated control can lag reality; duplicate greetings after deploys. | Accepted for a household; tighten the window in `config.py` if it bites. |
| 5 | **Single LLM slot** (`--parallel 1`). | Concurrent chats queue behind each other. | Accepted on 8 GB RAM (a second slot costs another KV cache). Split deployment on a bigger host can raise `--parallel`. |
| 6 | **Images are `linux/amd64` only.** | No ARM (Pi/Apple-silicon-native) images. | Native install works on ARM; ARM image builds are future work. |
| 7 | **Voice capture + whisper STT run on the server box** — the mic must be attached to the server. | No multi-room / remote mics. | Edge-voice roadmap item: transcribe on the device with the mic, POST text to `/inbox`. |
| 8 | **HA control is on/off/toggle only** (generic `homeassistant.*` services). | No brightness/color/temperature parameters yet. | Planned extension (FUTURE_IDEAS): `light.turn_on` with parameters, sensors as context, MQTT events. |
| 9 | **int8 quantization of the ONNX embedder fails** in onnxruntime's quantization preprocessor (shape-inference `AssertionError`). | Embedding bundle ships fp32 (1.2 GB) instead of ~330 MB. | fp32 works and is verified; retry when onnxruntime's quantizer handles the graph. |
| 10 | **Combined-image cosmetics**: llama prints `warn: LLAMA_ARG_HOST … overwritten by --host` at startup, and the banner's "LLM backend" line shows the split-config URL (`http://llama:8081`) although all-in-one actually talks to loopback. | Confusing log lines; no functional effect. | Cosmetic cleanup queued for a future image release. |

## Accepted defaults (by design — documented, warned at runtime)

| Issue | Why it's accepted | What to do when it matters |
|---|---|---|
| Default login **`admin`/`admin`** on fresh installs | Zero-config first-run; a warning is printed at startup and shown in docs | Set `ADMIN_PASS` (env/UI) on anything reachable by others |
| **Containers serve HTTP** (no TLS) by default | Containers commonly sit behind a reverse proxy; the native install gets local-CA HTTPS out of the box | Mount `tls/` (entrypoint enables HTTPS) or front with a proxy |
| The HA token grants whatever its HA user can do | HA tokens aren't scopeable per-entity | Mint it from a **dedicated non-admin HA user**; Jarvis adds its own entity allowlist on top |

## Resolved (kept for operators on older versions)

| Issue | Affected | Fixed in |
|---|---|---|
| **HA tools invisible to the LLM when configured via the Admin UI** — the tool menu was built at import time, so runtime (UI/DB) config never exposed `home_control`/`home_status`; devices showed in the UI but chat couldn't act | v2.5.0 | **v2.5.1** (menu computed per request via `_active_tools()`) |
| Admin header shows "Jarvis **vunknown**" (`APP_VERSION` unresolvable — app isn't an installed package) | v2.3.x–v2.4.0 images | **v2.5.0** (reads `pyproject.toml` directly) |
| Overriding `EMBED_MODEL` + own `HF_TOKEN` at runtime silently failed (offline switch triggered on *any* cached model) | ≤ v2.3.0 images | v2.3.1 |
| Gated-embedding 401s / `HF_TOKEN` needed for memory | ≤ v2.3.1 | **v2.4.0** (public SHA-pinned ONNX bundle — no token, ever) |
| Native build OOM-killed (`cc1plus: Killed`) — unbounded `make -j` | pre-2026-07-03 checkouts | RAM-aware `BUILD_JOBS` in `build_native.sh` |
| `.env` file ignored by the repo scripts (only Docker read it) | pre-v2.3.1 | v2.3.1 (`load_env.sh`, shell > `.env` precedence) |
