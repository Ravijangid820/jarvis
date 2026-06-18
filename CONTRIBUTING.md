# Contributing to Jarvis

Thanks for your interest! This is a self-hosted, offline AI assistant tuned for constrained
hardware. Contributions — bug fixes, docs, features, hardening — are welcome.

## Development setup

You need [`uv`](https://docs.astral.sh/uv/) (Python) and, for the UI, Node. Then:

```bash
git clone <repo> jarvis && cd jarvis
uv sync                              # Python env from pyproject + uv.lock

# Fast dev bootstrap (skip the heavy native build + model downloads while iterating):
SKIP_NATIVE=1 SKIP_MODELS=1 bash src/scripts/setup.sh

# Run the orchestrator locally (no systemd needed):
(cd src/orchestrator && uv run uvicorn main:app --host 127.0.0.1 --port 5000)
```

The app and tests run from **any checkout path** (paths resolve relative to the repo, overridable
with `JARVIS_HOME`/`JARVIS_CONFIG`) and as **any user** — you don't need root or `/srv/jarvis`.

## Tests & linting (required before a PR)

CI runs these on every push; please run them locally first:

```bash
uv run pytest -q                                          # full suite
uv run ruff check src/orchestrator src/scripts tests      # lint
```

Tests don't need the model: they set `JARVIS_NO_EMBED=1` and point `JARVIS_HOME` at a temp dir, so
they boot the real app against a throwaway SQLite DB. Add tests for new behaviour — especially auth,
authorization, and input validation.

## Code conventions

- **Match the surrounding code** — naming, comment density, and idioms. Comments explain *why*, not *what*.
- **Keep the import graph acyclic:** `config → {db, auth, llm} → memory → chat → main`. Don't introduce cycles. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Security is not optional:** all SQL is parameterized; user input is length-bounded and validated; authorization is enforced **in code, never by the LLM**; never log secrets; never commit `config/jarvis.json` or any `*.key`.
- Prefer small, focused PRs. Update the relevant docs and `docs/CHANGELOG.md` in the same PR.

## Commit & PR style

- Commit messages: imperative mood, conventional-ish prefixes — `feat:`, `fix:`, `docs:`, `security:`, `chore:`, `test:`.
- Open a PR against `main`; fill in the PR template (what/why, tests run, docs updated).
- A PR should leave `pytest` green and `ruff` clean.

## Reporting security issues

**Do not open a public issue for a vulnerability.** Follow [SECURITY.md](SECURITY.md).

## License of contributions

By contributing, you agree that your contributions are licensed under the project's license,
the **Apache License 2.0** (see [LICENSE](LICENSE)).
