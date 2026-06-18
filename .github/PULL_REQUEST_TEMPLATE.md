<!-- Thanks for contributing! Keep PRs small and focused. -->

## What & why

<!-- What does this change, and why? Link any related issue. -->

## How tested

<!-- Commands run / scenarios checked. -->

## Checklist

- [ ] `uv run pytest -q` passes
- [ ] `uv run ruff check src/orchestrator src/scripts tests` is clean
- [ ] Docs updated if behaviour changed (and `docs/CHANGELOG.md`)
- [ ] No secrets committed; new input is validated/bounded; authorization enforced in code (not the LLM)
