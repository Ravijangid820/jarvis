# Diagrams — every flow, visually

One page of flow diagrams for the whole system (GitHub renders these natively). Prose deep-dives:
[ARCHITECTURE.md](ARCHITECTURE.md) · [WORKFLOWS.md](WORKFLOWS.md) · [API.md](API.md). Current as of
**v2.6.0**.

---

## 1. Full system

```mermaid
flowchart TB
  subgraph CLIENTS["Clients & agents — outbound-only, no listening ports"]
    B["Browser / phone PWA<br/>React 19 · chat + Admin"]
    V["Voice listener (server box)<br/>whisper.cpp · wake word 'Jarvis'"]
    CAM["Camera agent (laptop/Pi)<br/>YuNet+SFace · events only"]
    VOL["Volume agent (Windows)<br/>pulls /devices/commands"]
  end
  subgraph BOX["Server — Proxmox LXC 192.168.0.101 (or Docker/OCI)"]
    O["Orchestrator — FastAPI<br/>:5000 HTTPS · Bearer auth"]
    L["llama-server (official llama.cpp)<br/>127.0.0.1:8081 · Qwen3.5-2B Q4 · ctx 4096"]
    E["ONNX embedder (in-process)<br/>embeddinggemma-300m · 768-d · ~175 ms"]
    CH[("ChromaDB<br/>cosine vectors")]
    DB[("SQLite WAL<br/>users · sessions · messages<br/>facts · settings · audit")]
    T["Piper TTS<br/>en_GB-alan · cached"]
  end
  HA["Home Assistant<br/>:8123 (same LAN)"]
  B -->|HTTPS| O
  V -->|POST /inbox| O
  CAM -->|POST /events| O
  CAM -.->|pull config| O
  VOL -.->|pull commands| O
  O --> L
  O --> E --> CH
  O --> DB
  O --> T
  O -->|"REST · server-held token<br/>allowlisted entities only"| HA
```

Solid = inbound requests; dotted = agents pulling (they open **no** ports). The HA token never
leaves the server; the LLM never sees it.

---

## 2. The intent ladder — how a message becomes an action ⭐

```mermaid
flowchart TB
  U["user message<br/>(web chat or voice)"] --> P{"pending proposal?<br/>('Should I…?' asked earlier)"}
  P -->|"'yes'"| EXEC
  P -->|"'no'"| CANCEL["'Okay — leaving it as is.'"]
  P -->|other / none| L1{"1️⃣ regex fast-paths (~0 ms)<br/>volume · reminders · home commands<br/>'turn on the fan', 'stop X', 'switch it off'"}
  L1 -->|match| EXEC["EXECUTOR (the only path to action)<br/>allowlist resolve → can_control_devices<br/>→ presence gate → act → AUDIT LOG"]
  L1 -->|miss| L2{"2️⃣ semantic router (~175 ms)<br/>embed the utterance, cosine vs<br/>per-device exemplar phrases"}
  L2 -->|"score ≥ 0.80 (device on/off)"| EXEC
  L2 -->|"score ≥ 0.63, or any routine"| ASK["ask: 'Should I turn on the fan?'<br/>(proposal remembered 2 min)"]
  L2 -->|below| L3{"3️⃣ anti-bluff guard<br/>names an allowlisted device<br/>+ a control verb?"}
  L3 -->|yes| CLAR["clarify: 'I think you want me to<br/>control X — try turn on/off X'"]
  L3 -->|no| LLM["4️⃣ normal chat — the LLM answers<br/>(tools offered on /inbox)"]
```

The design rule: a device-shaped message can **never** reach the toolless streaming LLM (which
would invent an ack) — it either acts, asks, or clarifies. And *every* action funnels through one
executor with the same gates, no matter which layer proposed it.

---

## 3. Chat request lifecycle

```mermaid
sequenceDiagram
  autonumber
  participant U as Client
  participant MW as Auth + rate limit
  participant H as Handler (/inbox · /chat/stream)
  participant IR as Intent ladder
  participant C as chat.py + budget.py
  participant M as memory (RAG)
  participant L as llama-server
  participant T as Piper TTS
  U->>MW: message (Bearer / jk- key)
  MW->>H: validated (session ownership, input caps)
  H->>IR: fast-paths + semantic router
  alt device intent
    IR-->>U: instant ack (no LLM) — done
  else conversation
    H->>C: build_messages()
    C->>M: RAG top-5 (cosine ≤ 0.6) + knowledge (cap 512 tok)
    C->>C: token budget: 4096 − reserve(512) − margin(96)
    H->>L: /v1/chat/completions (stream · cache_prompt · n_predict)
    L-->>U: tokens stream live (SSE)
    H->>T: TTS if voice_feedback (disk-cached)
    H->>C: store user+jarvis messages · title new sessions
  end
  Note over M: when idle ≥120 s → fact extraction → embed → ChromaDB
```

---

## 4. Semantic router internals

```mermaid
flowchart LR
  subgraph BUILD["index build (startup + every allowlist save, background)"]
    AL["allowlist entities"] --> EX["exemplars per (entity, action)<br/>• command templates: 'turn on the desk fan'<br/>• function-class phrases: fan→'it is hot in here'<br/>light→'it is dark' · heater→'i am freezing'"]
    EX --> EMB1["embed as documents<br/>(same ONNX embedder as RAG)"] --> IDX[("exemplar index")]
  end
  subgraph ROUTE["per message"]
    Q["utterance"] --> EMB2["embed as query"] --> SIM["cosine vs all exemplars<br/>best per (entity, action)"]
    SIM --> D{"decision"}
  end
  IDX -.-> SIM
  D -->|"≥ 0.80"| ACT["act (devices only)"]
  D -->|"≥ 0.63 · routines always · close-call margin"| CONF["confirm ('Should I…?')"]
  D -->|below| NONE["not a device intent"]
```

Thresholds calibrated on the box against the real embedder: unrelated chat peaked at **0.627**,
true paraphrases spanned **0.656–0.829** — the confirm line (0.63) sits in the gap.

---

## 5. Home Assistant — verbs and what they really do

```mermaid
flowchart TB
  SAY["you say…"] --> ON["'turn on / enable X'"] & OFF["'turn off / disable X'"] & STOP["'stop / cancel X'"] & RUN["'run / trigger X'"]
  ON -->|device| DON["switches on"]
  ON -->|automation| AON["arms its triggers"]
  OFF -->|device| DOFF["switches off"]
  OFF -->|automation| AOFF["disarms + aborts a run in progress"]
  STOP -->|device| DOFF2["switches off"]
  STOP -->|"automation/script"| SSTOP["aborts the current run —<br/>automation STAYS ENABLED<br/>(turn_off+stop_actions, then re-arm)"]
  RUN -->|"automation"| TRIG["fires its actions NOW<br/>(automation.trigger, skip_condition:false —<br/>its own guard conditions still apply)"]
  RUN -->|"script/scene"| SRUN["executes / applies"]
  RUN -->|device| DON2["'start the fan' = on"]
```

Every arrow passes: entity **allowlist** → `_can_control_devices` → presence gate → **audit log**.
Payloads to HA are hardcoded shapes (`entity_id` only) and responses are discarded — nothing for a
prompt injection to smuggle in either direction.

---

## 6. Memory / RAG

```mermaid
flowchart TB
  subgraph W["write path — never blocks chat"]
    MSG["stored messages"] --> IDLE{"idle ≥ 120 s?"}
    IDLE -->|yes| EXT["LLM fact extraction → JSON"]
    EXT --> DED{"duplicate?<br/>sim ≥ 0.90 or word-overlap ≥ 0.85"}
    DED -->|new| SQ[("SQLite facts")] --> BG["background embed worker"] --> CHR[("ChromaDB<br/>jarvis_memory_cos")]
  end
  subgraph R["read path — during prompt assembly"]
    QU["user message"] --> QE["embed (query prefix)"] --> S["top-5 cosine search"]
    S --> F{"distance ≤ 0.6?"}
    F -->|yes| INJ["→ prompt knowledge block (cap 512 tok)"]
    F -->|no| DROP["discarded"]
  end
  CHR -.-> S
```

SQLite is the **source of truth** (chats, facts); ChromaDB is a rebuildable index
(`reembed_memory.py`). The embedder runs torch-free on ONNX — same vectors, verified cosine 1.0.

---

## 7. Module import graph (acyclic)

```mermaid
flowchart TB
  CFG["config.py — no app deps"] --> DB2["db.py<br/>SQLite + app_settings"]
  CFG --> AUTH["auth.py<br/>PBKDF2"]
  CFG --> HA2["ha.py<br/>HA client + guardrails"]
  CFG --> OE["onnx_embed.py<br/>torch-free embedder"]
  CFG --> LLM2["llm.py<br/>llama client + TTS"]
  DB2 & AUTH & LLM2 & OE --> MEM["memory.py<br/>embeddings · Chroma · facts"]
  MEM --> CHAT["chat.py<br/>sessions · prompt assembly"]
  MEM --> IR2["intent_router.py<br/>semantic router"]
  HA2 --> IR2
  CHAT & IR2 --> MAIN["main.py — routes only"]
  BUD["budget.py — pure token math"] -.-> CHAT
  INT["intents.py — pure parsers"] -.-> MAIN
```

---

## 8. Auth & security gates

```mermaid
flowchart TB
  R["request"] --> A{"credential?"}
  A -->|Bearer session| U1["web user"]
  A -->|jk- API key| U2["device identity<br/>(key may be device-bound)"]
  A -->|none| X401["401"]
  U1 & U2 --> RL["per-user rate limit"] --> RO{"route class"}
  RO -->|/admin/*| ADM{"admin? (device-bound keys never)"}
  ADM -->|no| X403["403"]
  RO -->|chat/devices| OWN["session-ownership + input caps"]
  OWN --> CTRL{"device action?"}
  CTRL -->|yes| G["can_control_devices → presence gate<br/>→ entity allowlist → AUDIT"]
  CTRL -->|no| OK["handled"]
  G --> OK
```

The LLM appears nowhere in this diagram — that's the point. Models propose; code decides.

---

## 9. Voice & vision

```mermaid
flowchart LR
  subgraph VOICE["voice loop (server box today; edge-voice on the roadmap)"]
    MIC["mic"] --> WS["whisper-stream"] --> WW{"wake word<br/>'Jarvis'?"} -->|yes| INB["POST /inbox"] --> ANS["answer → Piper → speakers"]
    WW -->|no| DROPV["dropped"]
  end
  subgraph VISION["camera agent — imagery never leaves the device"]
    FR["frames"] --> MO["motion gate"] --> FD["YuNet detect"] --> FRZ["SFace recognize<br/>vs pulled enrolled set"]
    FRZ -->|"name+score event only"| EV["POST /events"] --> PRES["presence · greet-on-arrival ·<br/>presence-gated control"]
  end
```

---

## 10. Deployment shapes & release pipeline

```mermaid
flowchart TB
  subgraph SHAPES["one codebase — three ways to run (identical defaults)"]
    S1["jarvis-combined<br/>one container · Proxmox OCI<br/>all-in-one entrypoint"]
    S2["compose split<br/>official llama.cpp:server + jarvis-orchestrator<br/>LLM swappable (:server-cuda)"]
    S3["native · systemd<br/>the 2011 box · local-CA HTTPS"]
  end
  DEV["push to main"] --> CI["CI: ruff + 118 tests"]
  DEV --> TAG["bump pyproject → git tag vX.Y.Z"]
  TAG --> GHA["Actions (no secrets needed)"]
  GHA --> IMG["GHCR: X.Y.Z + latest<br/>(manual/RC builds never move latest)"]
  IMG --> S1 & S2
  DEV --> S3
```

Supply chain: every download pinned + SHA-256-verified (LLM GGUF · ONNX embed bundle from the
project's own HF repo · Piper + voice · llama.cpp tag). **git tag = pyproject = image tags.**
