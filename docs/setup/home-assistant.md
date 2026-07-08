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

## 2. Configure Jarvis — the easy way (Admin UI)
Log in as an admin → **Admin → Smart Home**:
1. Paste your **HA URL** and the **long-lived token** → **Test connection** (confirms both before saving).
2. **Save** — the token is stored server-side (in the DB, never shown again or given to the AI).
3. **Load devices from HA** → tick the devices Jarvis may control → **Save**.

That's it — no restart, no file editing. The tab shows a **Connected** pill and the allowlist count.
Everything below is the equivalent env/file config (for containers or headless setups).

## 2b. Configure Jarvis — env / file (headless)
Three values — via env (containers) or the `home_assistant` block in `config/jarvis.json` (native).
The feature is **off** until url + token are set. **Precedence: env > UI/DB > jarvis.json.** When set
via **env**, the Admin UI shows the config **read-only** (edit the env to change).

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

## Networking — Jarvis and HA must be mutually routable
The Jarvis server must be able to **initiate** connections to HA, so they need to be on the same network
(or otherwise routable box→HA). Same LAN is simplest — e.g. **HA as a Proxmox OCI container on the
box's LAN**. ⚠️ A Tailscale **subnet router** that lets you reach the Jarvis UI from afar is *inbound
only* — it does **not** let the box reach *out* to a device on another network (a Tailscale-only laptop,
or HA on a different subnet/hotspot). If HA must live off-LAN, put the **box itself** on Tailscale and
use HA's `100.x` tailnet IP. See [FUTURE_IDEAS](../FUTURE_IDEAS.md) → Networking.

## Automations, scripts & scenes
All three appear in the device picker and can be allowlisted. Semantics:
- **"turn on/off the &lt;automation&gt;"** — enables/disables it (off also **aborts a run in progress**).
- **"run / trigger / execute the &lt;automation|script|scene&gt;"** — executes its actions NOW.
  Automations run with `skip_condition: false`, so the automation's own guard conditions still apply.
- "run/start the &lt;plain device&gt;" gracefully means "turn it on".

Data-leak posture (by construction): payloads to HA are **hardcoded shapes** (`entity_id` only — no
variables/service-data channel the LLM could inject into); HA's responses are **discarded** (booleans
back), so no HA state flows toward the model beyond the on/off state of *allowlisted* entities via
`home_status`; the token never appears in logs, API responses, or the UI after saving.

## Notes
- Works with any entity the generic `homeassistant.turn_on/off/toggle` services accept: `light.*`,
  `switch.*`, `input_boolean.*`, `fan.*`, scenes, …
- The user speaking must have **device-control permission** in Jarvis (admins have it; grant per user
  in the admin console) — HA access is *authorized in Jarvis code*, per user, not by the LLM.
- Brightness/color, sensors-as-context, and MQTT events are future extensions
  (see [FUTURE_IDEAS](../FUTURE_IDEAS.md)).
