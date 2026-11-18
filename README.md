# Glovebox

A gated data layer for self-hosted AI agents. It is meant to serve mail, calendar and messenger data to agents as permitted fields only, so text an attacker wrote never reaches the agent's model.

The project is at its scaffold: the service starts and answers an MCP client over Streamable HTTP with an empty tool list. It reads no source and exposes no tool yet.

## The guarantee and what it does not claim

The guarantee (ADR 0001): no free-form string authored by a message sender, event organizer or chat participant crosses the gate into the agent's context. Outputs contain only closed-vocabulary enums, bounded parsed numbers and timestamps, booleans, and gateway-generated opaque handles.

It is a guarantee of text isolation, not blanket prompt-injection immunity. Non-claims:

- An attacker may influence classifications and values; a hostile message can yield a valid but wrong answer.
- Enum, count, ordering and timing side channels are bounded, not zero.
- It covers every gate-controlled output channel, not the agent's other tools or data sources.
- The client and model adapter must be verified to preserve the boundary.

## Develop

```
python3 -m venv .venv && .venv/bin/pip install -c constraints.txt -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy
.venv/bin/glovebox
```

See `AGENTS.md` for details, `CONTEXT.md` for the vocabulary and `docs/adr/` for the decisions.
