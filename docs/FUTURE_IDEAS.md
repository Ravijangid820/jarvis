# Jarvis Future Add-Ons & Ideas

This is a living document to track upcoming features, architectural shifts, and cool ideas for the Jarvis project.

## Planned Features
- **Home Assistant / MQTT Integration**: Connect Jarvis to the smart home network. Allow the LLM to trigger lights, smart plugs, and routines by outputting specific JSON commands that the orchestrator translates into MQTT messages.
- **Custom JARVIS Community Voice**: Currently using the high-quality British male voice (`en_GB-alan`), but a community-trained JARVIS model (`jgkawell/jarvis`) exists. We should eventually train or download a bespoke Marvel JARVIS voice.
- **Wake-Word Optimization**: Enhance `run_listener.sh` to use a more robust VAD (Voice Activity Detection) pipeline to prevent false positives when listening for the wake word.
- **Edge / distributed voice (mic + STT on the device)**: Today the mic and whisper STT run **on the server box** (single-box design); the edge devices do vision only. Move audio capture + whisper transcription onto the device that actually has the mic (the Pi/laptop already running the camera agent) — transcribe locally, gate on the wake word, and POST text to `/inbox`, so the **server drops whisper entirely**. Benefits: mic near the user, STT offloaded from the 2011 server, and support for multiple / multi-room mics. Groundwork already in place: `build_native.sh` supports `SKIP_WHISPER=1` so a server without the mic skips the whisper build.
- **Multi-User Profiles**: Add proper user accounts so the frontend can store separate histories for different household members.
- **Vector-based Semantic Search**: Replace SQLite FTS5 Keyword RAG with a dedicated vector database (like Chroma or FAISS) and a local embedding model (e.g. `all-MiniLM-L6-v2`). This provides true semantic understanding of memories rather than exact keyword matches. (Note: Embedding models require slightly more CPU/RAM resources per message to compute cosine similarities).
- **Real-Time Voice Streaming** (DONE): Stream Piper TTS audio bytes instantly to the browser as the LLM generates text, instead of waiting for the full generation to complete.
- **Function Calling & Tools** (DONE — voice path; lights pending): Grant JARVIS the ability to execute local server commands, control IoT devices, or search the web by giving the LLM function definitions.

## DevOps / Deployment
- **Dockerfile + Compose** (DONE — v2.2.0 → v2.3.0): Stack is containerized and published to GHCR — `jarvis-combined` (single self-contained container, built ON the official `llama.cpp:server` image) + `jarvis-orchestrator` (slim, for the two-service split). We no longer compile llama.cpp in-image; a new upstream release is a one-line `LLAMA_IMAGE` bump. Embedding baked in, zero-config defaults, GitHub Actions build. Deployed on Proxmox VE 9.1 as a native OCI container. See [setup/docker.md](setup/docker.md) + [setup/image-releases.md](setup/image-releases.md).
