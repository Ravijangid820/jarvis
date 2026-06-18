# Security Policy

## Reporting a vulnerability

**Please do not report security issues in public GitHub issues.**

Use GitHub's private vulnerability reporting: the repository's **Security** tab → **Report a
vulnerability** (this opens a private advisory only you and the maintainer can see). Include:

- what the issue is and where (file / endpoint),
- how to reproduce or a proof-of-concept,
- the impact you think it has.

You'll get an acknowledgement; fixes for confirmed issues are prioritized by severity. Please give
a reasonable window to address it before any public disclosure.

## Supported versions

Active development happens on `main`; fixes land there. There are no separate maintenance branches.

## Security model (what to expect)

Jarvis is designed to run **self-hosted and offline** — no cloud APIs, no telemetry. The posture:

- **No process runs as root** in the recommended install: both the orchestrator and the LLM server
  run as a dedicated unprivileged user, sandboxed with systemd (`ProtectSystem=strict`, minimal
  `ReadWritePaths`, `ProtectHome`, `NoNewPrivileges`). A simpler root install is also offered.
- **Authentication** is web-login session tokens **or** per-user API keys — both SHA-256 hashed at
  rest; there is no static master secret. Passwords use PBKDF2 (600k iterations).
- **Authorization is enforced in code, never by the LLM.** Device actions (e.g. volume) check the
  caller's permissions server-side; device-scoped API keys are bound to a single device.
- **Input is bounded and validated**; all SQL is parameterized; a strict Content-Security-Policy and
  standard security headers are sent; the LLM server binds to `127.0.0.1`.
- **Secrets are never committed** (`config/jarvis.json`, `*.key` are gitignored) and never logged.

This is a **single-operator, trusted-LAN** threat model: the API is reachable over LAN + a private
Tailscale network behind a host firewall (see [docs/DEPLOY.md](docs/DEPLOY.md)). TLS is terminated
at the network edge.

The project has had a multi-round security self-audit; the findings and their fixes are tracked in
**[docs/AUDIT.md](docs/AUDIT.md)**.
