# Environments

Glovebox has three tiers (ADR 0007). Each runs under its own Linux identity, so a ticket can never reach test state or production data. One tool, `scripts/env.py`, runs the throwaway and test tiers; production is the operator's own work.

## The tiers

| | Throwaway | Test | Production |
|---|-----------|------|------------|
| Linux user | `factory` | `glovebox-test` | `frogger` |
| OpenClaw node | the ticket's factory node | `glovebox-test` | none |
| Host | the factory node's host | the 790 | `zotac1070` (ADR 0007) |
| Port | a free loopback port, chosen by Docker | **8765**, on loopback only (see Data rule below) | operator's choice |
| Compose project | `glovebox-<8 hex digits of the checkout path>` | `glovebox-test` | none: the operator's own deployment; only `env.py check --url` is used |
| Compose files | `compose.yaml` | `compose.yaml` + `compose.test.yaml` | not applicable |
| Who touches it | the ticket | the timer `glovebox-test-env.timer` | the operator only |
| What runs | the ticket's own checkout, in rootless Docker | only `origin/main`, in rootless Docker, long-lived | a `main` commit that is green on test |
| Data rule | fictional fixture cast only | fictional fixture cast only | the operator's real accounts; secrets only on the host (ADR 0004) |
| Stand-ins | all of them (below) | all of them (below) | none: real sources |

Nobody reaches a tier above their own. Tickets and agents never touch production.

### Commands

| Tier | Commands |
|------|----------|
| Throwaway | `python3 scripts/env.py up`, `check`, `reset`, `down` |
| Test | `python3 scripts/env.py up`, `check`, `reset`, `down`, `ensure-test`, `install-timer` (as `glovebox-test`; the tier is detected from the user) |
| Production | the runbook below |

`scripts/env.py` is stdlib-only Python and needs `docker compose` 2.24 or newer (rootless Docker; the test override uses `!override`) and `git`.

| Command | What it does |
|---------|--------------|
| `up` | Creates `.env` with a freshly generated `GLOVEBOX_TEST_TOKEN` if the file is missing (and leaves an existing file alone), runs `docker compose pull`, builds the gate image, seeds the fixture cast, and starts the stack. Safe to repeat. |
| `check` | Exits 0 when the stand-ins are healthy and the gate answers an MCP `initialize` request with a valid JSON-RPC 2.0 success reply (matching id, `protocolVersion`, `capabilities`, and `serverInfo` with `name` equal to `glovebox` and a string `version`); exits 1 otherwise (any other reply, a reply that is not HTTP, or a URL that is not `http` or `https`, counts as `unhealthy`). Prints fixed status words only (`ok`, `unreachable`, `unhealthy`, `missing`), never a response body, an error message or a secret. `--url URL` checks a gate at that MCP URL instead of the local stack; the bearer token then comes from the environment variable `GLOVEBOX_TEST_TOKEN`, or from the one named by `--token-env`. Without `--url` the token comes from the environment, else from `.env`. |
| `reset` | Reseeds the fictional fixture cast from `fixtures/cast.json`. Seeding twice gives byte-identical state. Overlapping seeds (a manual `reset` during the timer's `ensure-test`) take turns on a lock file in the state volume, so neither fails. |
| `down` | Removes the containers, network and volumes. `.env` stays, so the token survives. |
| `ensure-test` | Test tier only. Fetches `origin/main`, moves the checkout to it, runs `up` and then `check` with the updated script. Safe to run repeatedly; it exits without effect if another run holds `.env.lock`, and it refuses to run if the checkout has local changes. |
| `install-timer` | Test tier only. Writes `glovebox-test-env.service` and `glovebox-test-env.timer` into `~/.config/systemd/user/` (units in `deploy/systemd/`), reloads systemd and runs `systemctl --user enable --now glovebox-test-env.timer`. The timer runs `ensure-test` 30 seconds after it starts and then every 5 minutes. `--no-enable --unit-dir DIR` only writes the units, and works on any tier. |

Environment knobs: `GLOVEBOX_ENV_TIER` (`throwaway` or `test`) overrides the detected tier, `GLOVEBOX_ENV_PROJECT` the compose project name, and `GLOVEBOX_ENV_PUBLISH_PORT` the test tier's published port (default 8765). They exist so the tooling can be exercised on a throwaway host without touching the long-lived test stack.

## The stack

`compose.yaml` holds three services; `compose.test.yaml` is the override where test differs (fixed port 8765, `restart: unless-stopped`). Both publish the gate on loopback only, because it has no authentication yet.

- `gate`: the Glovebox service, built from `deploy/Dockerfile.gate`, listening on 8765 in the container. Throwaway publishes it on a free loopback port; `check` finds it with `docker compose port`.
- `standins`: the stand-ins, one listener per outside service (`deploy/standins/standins.py`).
- `seed`: a one-shot job that writes the fixture cast into the `state` volume (`deploy/standins/seed.py`).

The gate has no authentication yet (agent tokens come with ticket 07), so `GLOVEBOX_TEST_TOKEN`, the test environment's stand-in for an agent token, is generated, passed to the gate and sent by `check`, but not yet enforced. For that reason the test tier publishes 8765 on loopback of the 790 only, and `scripts/env.py` has no way to widen it. Nodes on other hosts cannot reach `GLOVEBOX_TEST_URL` yet. Two gate-side changes come first, and this ticket makes neither: a setting for the address the gate is published on, and an allowed-hosts setting, because the gate's MCP transport answers `421 Misdirected Request` to any `Host` header other than `localhost`, `127.0.0.1` or `[::1]` (DNS-rebinding protection). They need their own ticket, before ticket 03's last item can pass.

### Environment names

| Name | Where | Meaning |
|------|-------|---------|
| `GLOVEBOX_TEST_TOKEN` | `.env` on the stack's host; node profile | The test token. Generated on the node that runs the stack; never committed. |
| `GLOVEBOX_TEST_URL` | node profile | The MCP URL of the test environment: `http://<the 790>:8765/mcp`. Set by the operator. |
Nodes that use the test environment read `GLOVEBOX_TEST_URL` and `GLOVEBOX_TEST_TOKEN`. To check it from another node: `python3 scripts/env.py check --url "$GLOVEBOX_TEST_URL"` (this works once the gate allows the 790's host, see above). On the 790 itself, `python3 scripts/env.py check` as `glovebox-test` always works.

## Data rule

Throwaway and test hold only the **fictional fixture cast** in `fixtures/cast.json`: invented people, addresses in the reserved `example.test` domain and numbers in the reserved fictional range 555-0100 to 555-0199. Never put real, pseudonymised or production-copied data in a fixture, an environment, a log or a commit. `.env` is gitignored. Test secrets are generated on the test node; production secrets stay with the operator.

## Stand-ins

Every outside service has a deterministic stand-in; no vendor sandbox account is used. The stand-ins speak plain HTTP and JSON from the seeded state, not the vendor protocols, and each answers `GET /healthz` (503 until seeded) and `GET /` with its fixture. They are for testing only and never count as delivering a source (ADR 0003). The gate reaches them at the addresses below through `GLOVEBOX_STANDIN_*` variables; no gate feature reads them yet, the source tickets do.

| Outside service | Stand-in | Address in the stack | Variable |
|-----------------|----------|----------------------|----------|
| mailbox.org mail | canned fixture | `standins:9101` | `GLOVEBOX_STANDIN_MAILBOX_MAIL` |
| mailbox.org calendar | canned fixture | `standins:9102` | `GLOVEBOX_STANDIN_MAILBOX_CALENDAR` |
| Office 365 mail | canned fixture | `standins:9103` | `GLOVEBOX_STANDIN_O365_MAIL` |
| Office 365 calendar | canned fixture | `standins:9104` | `GLOVEBOX_STANDIN_O365_CALENDAR` |
| Signal bridge | canned fixture | `standins:9105` | `GLOVEBOX_STANDIN_SIGNAL` |
| iMessage bridge | canned fixture | `standins:9106` | `GLOVEBOX_STANDIN_IMESSAGE` |
| WhatsApp bridge | canned fixture | `standins:9107` | `GLOVEBOX_STANDIN_WHATSAPP` |
| Local inference | canned stub: `POST /v1/chat/completions` always answers `unknown` | `standins:9108` | `GLOVEBOX_STANDIN_INFERENCE_LOCAL` |
| External inference | canned stub, same answer | `standins:9109` | `GLOVEBOX_STANDIN_INFERENCE_EXTERNAL` |

Consequences (ADR 0007): live sources are never exercised on throwaway or test; their acceptance is operator-run against the operator's own accounts. The inference stand-ins say nothing about classification quality or inference resource use.

## Test host: the 790

The operator decided on 2026-11-15 that `glovebox-test` runs on the 790. The host carries the dedicated Linux user `glovebox-test` and the OpenClaw node `glovebox-test`, with rootless Docker, created by the operator with `~/linux-test-node-wizard.sh tomorrowflow/glovebox`. The test environment is installed by ticket 03. Preconditions for the timer: the `glovebox-test` user can `git fetch origin` without a prompt, has `docker compose` on the user service's `PATH`, and has lingering enabled (`loginctl enable-linger glovebox-test`) so its user timer runs without a login session. The timer sets `DOCKER_HOST` to the rootless socket in `$XDG_RUNTIME_DIR`.

## Production runbook

The operator does this as `frogger`. Tickets and agents never touch production: no ticket installs it, no agent holds a production token, and production secrets and upstream credentials exist only on the production host in files that the agents' Linux users cannot read (ADR 0004).

The production host is `zotac1070` (ADR 0007), chosen by the operator and recorded on 2026-11-20. Nothing is installed there until the operator runs ticket 04.

1. Pick a `main` commit that is green on test: its CI passed, and the test environment has run it (`ensure-test` reports `at <commit>`, and `python3 scripts/env.py check` passes on the test node).
2. As `frogger` on the production host, check out exactly that commit.
3. Provide the production configuration and secrets from the operator's own store, in permission-restricted files on that host. Do not copy anything from `.env` of test, and do not use fixtures or stand-ins.
4. Build the gate image from `deploy/Dockerfile.gate` at that commit and run it with the production configuration. Production does not use `compose.yaml`, `compose.test.yaml` or the stand-ins.
5. Verify: `python3 scripts/env.py check --url <production MCP URL>`, with the production agent token in the variable named by `--token-env`. It must exit 0.
6. If `check` fails, stop the deployment, restore the previous commit and investigate on throwaway or test.
