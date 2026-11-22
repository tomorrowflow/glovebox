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

The service listens on `127.0.0.1:8765`, MCP endpoint `/mcp`; override with `GLOVEBOX_HOST` and `GLOVEBOX_PORT`. It answers `401` to any request without an active agent token, checked against the database on every request, so keep it on loopback. `GLOVEBOX_DB` is the SQLite file (default `glovebox.db`), shared with the operator's command line.

The operator sets accounts and tokens up locally, outside MCP, with `.venv/bin/glovebox account add|list|remove` and `.venv/bin/glovebox token issue|list|revoke` (`glovebox --help`). `token issue` prints the token value once on stdout and stores only its digest. An account's credentials live in a file that only the gateway's user may access (mode `0600`): `account add --credentials FILE` and the service itself refuse a file open to group or others (ADR 0004). The throwaway and test stacks start the gate as `glovebox serve --bootstrap-fixture`, which registers a `fixture` account for the mail stand-in (`GLOVEBOX_STANDIN_MAILBOX_MAIL`, or `GLOVEBOX_MAIL_FIXTURE` for a file) and a token with the value of `GLOVEBOX_FIXTURE_TOKEN`; tests use the same commands through `tests/conftest.py`. The second fixture source kind, `fixture_messenger` (`account add NAME --kind fixture_messenger --source FILE_OR_URL`; tests pass `kinds=` to `start_gate`), reads a document of `conversations` with `messages`: its items are served by the same tools as mail, with the conversation handle in `thread_handle`. `GLOVEBOX_FIXED_NOW` (an ISO 8601 time with a UTC offset) pins the clock the 30-day mail window is measured from; tests set it so fixture mail does not age out, and a real deployment leaves it unset.

`GLOVEBOX_CLASSIFIER_CONFIG` names the operator's classifier configuration, a JSON file with `topics` (`id`, `name`, `description`; IDs match `^[a-z][a-z0-9_]{0,23}$`, `other` and `unknown` are reserved), an optional `deadline_range` (`from`, `to`, minute timestamps) and `inference` (`backend`, `profile`); it is read at startup, and without it every item's classification is `unavailable`. The only backend is the deterministic `stub`; `GLOVEBOX_STUB_CLASSIFIER_SCRIPT` and `GLOVEBOX_STUB_CLASSIFIER_PROBE` are test-only controls of it (see `src/glovebox/stub.py`). Changing the topic list or the inference settings makes stored classifications stale. The `list_topics` tool is one more read operation a token can be limited to; tokens issued before it existed do not name it and must be reissued to use it. CI (`.github/workflows/ci.yml`) runs the install, lint, type check and test commands above.

Dependencies are pinned exactly in `pyproject.toml`; `constraints.txt` pins the full set. When you change a dependency, update both.

## Environments

Bring up a throwaway stack for your ticket with `python3 scripts/env.py up`, then `check`, `reset` and `down`. It runs the gate in rootless Docker with a stand-in for every outside service and a seeded fictional fixture cast. Never put real data in a fixture. Test and production run natively on the Mac mini, with no Docker. Tiers, the test environment and the production runbook are in `docs/environments.md`. Tickets and agents never touch production.

## Read first

- `CONTEXT.md`: the project's vocabulary. Use its terms.
- `docs/adr/`: decisions. ADR 0001 states the guarantee and non-claims, ADR 0006 the one acceptance seam.
- `docs/prd/gated-agent-data-layer.md`: the spec. Tickets are in `docs/issues/gated-agent-data-layer/`.

Architecture map: docs/architecture/*.json (SeeCode specs, evidence-linked; rules in docs/architecture/README.md).

## Testing

Acceptance tests drive the service through MCP tool calls with a real MCP client over HTTP (ADR 0006). Tests live in `tests/`.

Fixture mail in tests is dated within the 30 days before the pinned `NOW` in `tests/conftest.py`; pass `now=` to `start_gate` to move the clock.

Every new field or tool extends the reusable hostile-corpus helpers: put corpus strings in the new attacker-writable fields of `hostile_mail_fixture` (`tests/hostile.py`), add any new tool to `OK_CALLS` and any new parameter to `hostile_calls` (`tests/hostile.py`), add any new response channel to `observe_gate` (it already includes the gate's log, which `RunningGate.log` captures), and check new output schemas with `closed_schema_problems` (`tests/schema.py`). `assert_no_corpus` then covers every channel. Responses leave the gate only through `output.release`, the one output validator.

Classifier tests (`tests/test_classifier.py`) drive the stub with a script file that maps a marker in an item's text to the raw output it returns, so a test chooses what the "model" says. Pass `extra_env=` to `start_gate`; `RunningGate.log` is everything the service printed, for "nothing in logs" checks. The classifier's inputs and raw output are never served, logged or stored; those tests check the log and the stored files as well as every response.
