# Home Assistant integration (smart-home control by chat/voice)

Jarvis controls Home Assistant devices through **narrow, allowlisted LLM tools**: say
*"turn on the kitchen light"* → the model emits a `home_control` tool call → **code** (never the
model) validates it and calls HA's REST API. Also adds a **Home Assistant** row to the admin
services board.

## Security model (same rules as all device control)
- The HA token lives **server-side** (config/env) — the LLM never sees it.
- Mint the token from a **dedicated non-admin HA user**, so even the token is least-privilege.
- The model can only reference entities on your **allowlist** — there is deliberately **no**
  "call any service" tool, so a prompt injection can at worst toggle an allowlisted light.
- Every action passes the existing gates: `_can_control_devices` per user, the optional camera
  presence gate, per-user rate limit, and the **audit log**.
- Ambiguity is refused, never guessed: "the light" with three lights → Jarvis asks which one.

## 1. Prepare Home Assistant (once)
1. **Settings → People → Add user** → `jarvis`, **not** an administrator.
2. Log in as `jarvis` once → Profile (avatar) → **Security** → **Long-Lived Access Token** → create + copy.
3. Verify from wherever Jarvis runs:
   ```bash
   curl -H "Authorization: Bearer <token>" http://<HA-host>:8123/api/
   # → {"message": "API running."}
   ```
   No devices yet? Create a test entity: **Settings → Devices & Services → Helpers → Create helper →
   Toggle** (gives e.g. `input_boolean.test_light`).

## 2. Configure Jarvis
Three values — via env (containers) or the `home_assistant` block in `config/jarvis.json` (native).
The feature is **off** until url + token are set; env wins over the file.

| Env | jarvis.json | Example |
| --- | --- | --- |
| `HA_URL` | `home_assistant.url` | `http://192.168.0.120:8123` |
| `HA_TOKEN` | `home_assistant.token` | `eyJhbGciOi…` |
| `HA_ALLOWED_ENTITIES` | `home_assistant.allowed_entities` | `input_boolean.test_light,light.kitchen` (env: comma-separated; json: array) |

Docker (combined image shown; compose passes the same vars through):
```bash
docker run -d --name jarvis --init -p 5000:5000 \
  -e ADMIN_PASS=secret \
  -e HA_URL=http://host.docker.internal:8123 \
  -e HA_TOKEN=<token> \
  -e HA_ALLOWED_ENTITIES=input_boolean.test_light \
  -v jarvis-data:/app/memory \
  ghcr.io/ravijangid820/jarvis-combined:latest
```
> **`host.docker.internal`** = "the machine running Docker" — use it when HA runs on the *same*
> PC (Docker Desktop). On a LAN/Proxmox setup use HA's real IP. This URL is the only thing that
> changes when HA moves.

Native: fill the block in `config/jarvis.json` (or put the three vars in `.env`) and restart.

## 3. Use it
- *"Turn on the test light"* → `Okay — test light on.` (watch it flip in HA's dashboard)
- *"Is the test light on?"* → `Test Light is on.`
- *"Toggle the kitchen light"*, *"turn everything off"* → only allowlisted entities respond.
- **Admin → System Services** shows `Home Assistant · N entities allowlisted · <url>` (green when
  HA answers with your token).
- Every action lands in **Admin → Audit log** (`device.home_assistant`).

## Notes
- Works with any entity the generic `homeassistant.turn_on/off/toggle` services accept: `light.*`,
  `switch.*`, `input_boolean.*`, `fan.*`, scenes, …
- The user speaking must have **device-control permission** in Jarvis (admins have it; grant per user
  in the admin console) — HA access is *authorized in Jarvis code*, per user, not by the LLM.
- Brightness/color, sensors-as-context, and MQTT events are future extensions
  (see [FUTURE_IDEAS](../FUTURE_IDEAS.md)).
