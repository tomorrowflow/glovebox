# Glovebox

A gated data layer for self-hosted AI agents. It is meant to serve mail, calendar and messenger data to agents as permitted fields only, so that free-form text an attacker wrote does not cross the gate into the agent's context.

The project is at its first slices through the gate: an MCP client with an operator-issued bearer token connects over Streamable HTTP and queries the items of fixture mail and fixture messenger accounts, which come back as handles and permitted fields only (item, account, sender and thread handles, source kind, received time at minute precision, read status, attachment presence). Three read tools do it:

- `list_items` filters by read status, received-time range, attachment presence, sender, thread and account handle, sorts in one of four fixed orders, and pages with an opaque cursor.
- `count_items` counts the items that match the same filters.
- `group_items` counts them grouped by sender handle, thread handle, read status or source kind.

A further tool, `list_topics`, returns the topic IDs the operator configured. Each item also carries four semantic fields and a classification status: topic (a configured topic ID, `other` or `unknown`), reply needed and action requested (`yes`, `no`, `unknown`) and deadline (a minute timestamp or `unknown`). A quarantined classifier inside the gate proposes them from the item's subject and body; the gate checks each field against its closed vocabulary and turns anything invalid into `unknown`, so none of the classifier's output text can cross. They are untrusted judgments: a hostile message can steer them to a valid but wrong value, and they never authorize an action. The only classifier so far is a deterministic stub; real inference comes later. The semantic fields can be filtered, grouped and counted like the others.

There is no free-text or arbitrary semantic query parameter: inputs are enums, handles, minute timestamps, booleans, a page size of at most 50 and cursors, and anything else is refused with a fixed error code. A response holds at most 50 items or groups, a count above 1000 is reported as `1000+`, a cursor is valid only for the token identity and query it was issued for, and mail older than a rolling 30 days is neither ingested nor served. These limits bound the count, ordering and timing side channels; they do not remove them. The only sources are the fixture ones the stand-ins serve: `fixture_mail` and `fixture_messenger`. A messenger item has the fields of a mail item, its thread handle being a conversation handle (direct or group), its read status `unknown` where the messenger has none, and attachment presence the only thing said of media; mail and messenger accounts are queried together in one call when a token covers both.

The operator sets the gate up from the command line on the gateway host, outside MCP (`glovebox account ...`, `glovebox token ...`): accounts with credentials files that only the gateway's user may access, and revocable agent tokens scoped to accounts and read operations. A token value is shown once and only a digest is stored; a revoked token is refused on its next request. Another tool, `list_accounts`, tells an agent which accounts its token covers (handle and source kind). An account or cursor outside the token's scope is answered exactly like one that does not exist. The service still listens on loopback by default.

## The guarantee and what it does not claim

The guarantee (ADR 0001): no free-form string authored by a message sender, event organizer or chat participant crosses the gate into the agent's context. Outputs contain only closed-vocabulary enums, bounded parsed numbers and timestamps, booleans, and gateway-generated opaque handles.

It is a guarantee of text isolation, not blanket prompt-injection immunity. Non-claims:

- An attacker may influence classifications and values; a hostile message can yield a valid but wrong answer.
- Enum, count, ordering and timing side channels are bounded, not zero.
- It covers every gate-controlled output channel (structured content, content blocks, errors, metadata, tool descriptions), not the agent's other tools or data sources.
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
