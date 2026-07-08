# Jarvis Future Add-Ons & Ideas

This is a living document to track upcoming features, architectural shifts, and cool ideas for the Jarvis project.

## Planned Features
- **Home Assistant / MQTT Integration** (DONE — REST control, 2026-07-07; MQTT events still future): Jarvis controls HA devices via narrow allowlisted tools over HA's REST API (see docs/setup/home-assistant.md). Remaining ideas: MQTT/event push (HA → Jarvis announcements), brightness/color params, sensors as prompt context.
- **Dynamic intent understanding — de-hardcode device commands** (filed 2026-07-09, after the HA
  hardening round): today device control rests on hand-written regex fast-paths (`intents.py`) —
  reliable and instant, but *scripted*: "turn on the fan" works, "it's boiling in here" doesn't, and
  every new phrasing is a code edit. The plan is a **layered router**, each layer smarter and slower,
  all feeding the SAME executor (allowlist + `_can_control_devices` + presence + audit — proposal is
  never authorization, no matter which layer proposed):
  1. **Regex fast-path** (exists) — exact common phrasings, ~0 ms. Keep as layer 1.
  2. **Semantic router — reuse the ONNX embedder we already run** (~175 ms/query): embed the user
     utterance and compare (cosine) against pre-embedded exemplar phrases per intent+entity
     ("turn on the fan" ≈ "i'm melting in here" ≈ "some air please"). High similarity → act;
     medium → **confirm** ("Should I turn on the fan?"); low → fall through. No LLM call, no new
     dependency, generalizes to paraphrases the regexes can't enumerate. Exemplars can even be
     auto-generated per allowlisted entity ("cool the room" for a fan, "make it dark" for lights).
  3. **GBNF-constrained tool call** (llama.cpp grammars): a short router call where the 2B model
     MUST emit schema-valid JSON ({"tool":"none"} | a valid call) — structural validity solves the
     small-model tool-calling flakiness; `cache_prompt` keeps it ~1–2 s. Run it only when layer 2 is
     unsure, so the cost is paid rarely.
  4. **Streaming tool-call support in `/chat/stream`** so the main conversation model can also act
     mid-chat (today the web chat path is toolless by design — see KNOWN_ISSUES #2).
  Success criteria: indirect requests act (or ask one good question), zero regressions on the
  105-test suite, and the anti-bluff guarantee stays (no invented acks, ever).
- **Custom JARVIS Community Voice**: Currently using the high-quality British male voice (`en_GB-alan`), but a community-trained JARVIS model (`jgkawell/jarvis`) exists. We should eventually train or download a bespoke Marvel JARVIS voice.
- **Wake-Word Optimization**: Enhance `run_listener.sh` to use a more robust VAD (Voice Activity Detection) pipeline to prevent false positives when listening for the wake word.
- **Edge / distributed voice (mic + STT on the device)**: Today the mic and whisper STT run **on the server box** (single-box design); the edge devices do vision only. Move audio capture + whisper transcription onto the device that actually has the mic (the Pi/laptop already running the camera agent) — transcribe locally, gate on the wake word, and POST text to `/inbox`, so the **server drops whisper entirely**. Benefits: mic near the user, STT offloaded from the 2011 server, and support for multiple / multi-room mics. Groundwork already in place: `build_native.sh` supports `SKIP_WHISPER=1` so a server without the mic skips the whisper build.
- **Speculative decoding (perf experiment — likely the biggest same-hardware win)**: run a tiny draft
  model (e.g. a 0.5B from the same Qwen family) alongside the 2B — the draft proposes tokens, the 2B
  verifies in one batch pass. Typical 1.5–2.5× token-generation speedup on CPU. Already plumbed:
  `LLAMA_EXTRA_ARGS="--model-draft <draft.gguf> ..."` (compose/all-in-one) or the same flags on the
  native `llama-server`. Experiment on the laptop first; verify quality is unchanged (spec decoding is
  lossless). Rejected alternative for the record: rewriting the orchestrator in Rust/Go/C++ — the hot
  path is already C++ (llama.cpp); Python glue is ~15 ms of a 30–60 s request (<0.1%, Amdahl).
- **ONNX embeddings (drop torch — the biggest resource win)**: torch exists in the stack ONLY to run
  the 300M embedder (~1.5–2 GB of image, hundreds of MB RAM), and `onnxruntime` is already a dependency
  (via chromadb). Export/pull embeddinggemma as ONNX (optionally int8) and run it on onnxruntime:
  much smaller images, lower RAM, faster embeds on the no-AVX2 box. Requires re-indexing memories
  (`src/scripts/reembed_memory.py`) only if the vectors change; same model → same vector space.
- **Multi-User Profiles**: Add proper user accounts so the frontend can store separate histories for different household members.
- **Vector-based Semantic Search**: Replace SQLite FTS5 Keyword RAG with a dedicated vector database (like Chroma or FAISS) and a local embedding model (e.g. `all-MiniLM-L6-v2`). This provides true semantic understanding of memories rather than exact keyword matches. (Note: Embedding models require slightly more CPU/RAM resources per message to compute cosine similarities).
- **Real-Time Voice Streaming** (DONE): Stream Piper TTS audio bytes instantly to the browser as the LLM generates text, instead of waiting for the full generation to complete.
- **Function Calling & Tools** (DONE — voice path; lights pending): Grant JARVIS the ability to execute local server commands, control IoT devices, or search the web by giving the LLM function definitions.

## DevOps / Deployment
- **Dockerfile + Compose** (DONE — v2.2.0 → v2.3.0): Stack is containerized and published to GHCR — `jarvis-combined` (single self-contained container, built ON the official `llama.cpp:server` image) + `jarvis-orchestrator` (slim, for the two-service split). We no longer compile llama.cpp in-image; a new upstream release is a one-line `LLAMA_IMAGE` bump. Embedding baked in, zero-config defaults, GitHub Actions build. Deployed on Proxmox VE 9.1 as a native OCI container. See [setup/docker.md](setup/docker.md) + [setup/image-releases.md](setup/image-releases.md).

## Networking / Infrastructure
*(operator-facing symptoms of these live in [KNOWN_ISSUES.md](KNOWN_ISSUES.md))*
- **Box outbound reachability to off-LAN devices — put the box on Tailscale** (KNOWN LIMITATION, hit 2026-07-07 during HA testing): the box is reachable *inbound* from anywhere via a Tailscale **subnet router** (the LAN gateway `192.168.0.100` advertises `192.168.0.0/24` into the tailnet and SNATs, so a remote laptop can open the Jarvis UI). But the box itself is **not a tailnet node** — it only has its LAN `eth0` — so it can *reply* to inbound connections yet **cannot initiate outbound** to a device on another network (a Tailscale-only laptop, or HA/camera agents on a different subnet/hotspot). Concretely: box `192.168.0.101` could not reach HA on a laptop that was on a mobile hotspot (`172.28.29.0/24`); **resolved by running HA on the box's LAN instead** (the production topology anyway). Future fix so the box can reach edge devices wherever they live (HA, cameras, volume/voice agents on other networks — also relevant to the edge-voice roadmap): install Tailscale in the LXC (`/dev/net/tun`, or userspace mode) so the box gets a real tailnet interface; then target the device's `100.x` tailnet IP. Alternative without making the box a node: a static `100.64.0.0/10 via <subnet-router-LAN-IP>` route on the box **plus** the subnet router configured to forward LAN→tailnet and Tailscale ACLs allowing it (fiddlier). The subnet-router path is inbound-only *by design* (return traffic rides NAT state), so one of these is required for box-initiated outbound.
