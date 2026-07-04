# Install: combined image (single container)

**`ghcr.io/ravijangid820/jarvis-combined`** — everything in one container: the official `llama-server`,
the orchestrator, the web UI, and both models (LLM + embedding) baked in. Works offline, zero config.

**Pick this if** you want the simplest possible deployment on one machine — including **Proxmox VE 9.1
OCI containers** (which run an image's default entrypoint; this image's default runs both services).

## Prerequisites
- Docker (or Proxmox VE ≥ 9.1 for the OCI route) on any x86-64 host with **~3–4 GB RAM** free.

## Docker
```bash
docker run -d --name jarvis --init -p 5000:5000 --restart unless-stopped \
  -e ADMIN_PASS='pick-a-strong-one' \
  -v jarvis-data:/app/memory \
  ghcr.io/ravijangid820/jarvis-combined:latest

docker logs -f jarvis                      # wait for the [jarvis] banner
curl -fsS http://localhost:5000/health     # → {"status":"ok",...}
```
Open **http://localhost:5000** — login `admin` / your `ADMIN_PASS` (defaults to `admin`/`admin` if unset).

## Proxmox VE (OCI container)
1. **Storage → CT Templates → Pull from OCI Registry** → reference `ravijangid820/jarvis-combined`,
   pick a tag (e.g. `latest`).
2. **Create CT** → Template = that image → ~2–4 GB RAM, 2 cores.
3. **Resources → Add → Mount Point** at `/app/memory` (so memory survives CT re-creation — OCI CTs are
   recreated to update).
4. **Options** → set `ADMIN_PASS` → **Start**. Open `http://<CT-ip>:5000`.

## Configuration
No config required. Override anything with `-e` (Docker) or Options (Proxmox) — common ones:
`ADMIN_USER`/`ADMIN_PASS`, `LLM_CTX` (context window), `LLAMA_THREADS`, `LLM_MODEL` (your own GGUF),
`EMBED_MODEL`. Full list + how config layers work: [docker.md](docker.md).

### Tokens — there are none
**No HuggingFace token is needed anywhere** — build or runtime. The embedding is a **torch-free ONNX
bundle** (public, SHA-256-pinned, verified identical to the original model) fetched by the image build
itself; `docker build -f Dockerfile.combined .` with zero arguments produces a working image.
**Your own embedding model:** export a bundle with `src/scripts/export_embed_onnx.py`, mount it, and set
`EMBED_ONNX_DIR` + `EMBED_MODEL` (the bundle's `meta.json` must match `EMBED_MODEL`, or it's refused —
that guard protects your stored memories' vector space).

## Verify
Log in → **Admin → System Services**: expect `N/N operational`, the LLM row showing the loaded model
(`Qwen3.5-2B-Q4_K_M · ctx 4096`), and Embeddings green. Then send a chat.

## Update
```bash
docker pull ghcr.io/ravijangid820/jarvis-combined:latest && docker rm -f jarvis && <run command again>
```
(Proxmox: pull the new tag, recreate the CT — the `/app/memory` mount point keeps your data.)

## Notes
- Runs on **any x86-64 CPU with AVX** (auto-detected) — see [docker.md](docker.md) for the support matrix.
- Trade-off vs the [split](orchestrator-image.md): simplest, but the two services restart together.
- Published tags: [image-releases.md](image-releases.md).
