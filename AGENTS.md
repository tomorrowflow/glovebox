# AGENTS.md

Glovebox is one Python service on the official MCP Python SDK, with SQLite for state (ADR 0005). Agents connect over Streamable HTTP.

## Commands

Python 3.12 or newer. Run from the repository root.

| Task | Command |
|------|---------|
| Install (creates `.venv`, pins from `constraints.txt`) | `python3 -m venv .venv && .venv/bin/pip install -c constraints.txt -e ".[dev]"` |
| Test | `.venv/bin/python -m pytest` |
| Lint and format check | `.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .` |
| Format | `.venv/bin/python -m ruff format .` |
| Type check | `.venv/bin/python -m mypy` |
| Run the service | `.venv/bin/glovebox` |

The service listens on `127.0.0.1:8765`, MCP endpoint `/mcp`; override with `GLOVEBOX_HOST` and `GLOVEBOX_PORT`. The scaffold has no authentication yet (agent tokens come later), so keep it on loopback. CI (`.github/workflows/ci.yml`) runs the install, lint, type check and test commands above.

Dependencies are pinned exactly in `pyproject.toml`; `constraints.txt` pins the full set. When you change a dependency, update both.

## Environments

Bring up a throwaway stack for your ticket with `python3 scripts/env.py up`, then `check`, `reset` and `down`. It runs the gate in rootless Docker with a stand-in for every outside service and a seeded fictional fixture cast. Never put real data in a fixture. Tiers, the test environment and the production runbook are in `docs/environments.md`. Tickets and agents never touch production.

## Read first

- `CONTEXT.md`: the project's vocabulary. Use its terms.
- `docs/adr/`: decisions. ADR 0001 states the guarantee and non-claims, ADR 0006 the one acceptance seam.
- `docs/prd/gated-agent-data-layer.md`: the spec. Tickets are in `docs/issues/gated-agent-data-layer/`.

## Testing

Acceptance tests drive the service through MCP tool calls with a real MCP client over HTTP (ADR 0006). Tests live in `tests/`.
