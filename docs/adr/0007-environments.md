---
status: accepted
---

# Three environment tiers, test on the 790, production host still open

Glovebox has a hosted runtime (the gate service and its SQLite database), so it gets environments. Each tier runs under its own Linux identity so that a ticket can never reach test state or production data.

| Tier | Linux user | OpenClaw node | Host | What runs | Who touches it |
|------|-----------|---------------|------|-----------|----------------|
| Throwaway | `factory` | the ticket's factory node | the factory node's host | the ticket's own stack in rootless Docker | the ticket |
| Test | `glovebox-test` | `glovebox-test` | the 790 | only `origin/main`, in rootless Docker, long-lived | `scripts/env.py ensure-test`, from user timer `glovebox-test-env.timer` every 5 minutes |
| Production | `frogger` | none | open | a `main` commit that is green on test | the operator only; never tickets or agents |

**Test host.** The operator decided on 2026-11-15: "glovebox-test runs on the 790". The 790 hosts the dedicated `glovebox-test` Linux identity and OpenClaw node. This selects the host only; nothing is installed until `03-install-test` runs.

**Shared tooling.** Throwaway and test share one compose file and one executable Python `scripts/env.py` with `up`, `check`, `reset`, `down`, `ensure-test` and `install-timer`. `up` creates the gitignored env file with the test token when it is missing. `install-timer` installs and starts the user timer `glovebox-test-env.timer`.

**Test endpoint.** Port **8765**. Nodes read `GLOVEBOX_TEST_URL` and `GLOVEBOX_TEST_TOKEN`. While the gate has no authentication it is published on loopback of the test host only; reaching it from other hosts needs a later change to the gate (`docs/environments.md`).

**Test data.** A fictional fixture cast that `reset` seeds. Never real, pseudonymised or production-copied data.

**Stand-ins.** Every outside service gets a deterministic stand-in, fixture mode or a canned stub: mailbox.org mail, mailbox.org calendar, Office 365 mail and calendar, the Signal, iMessage and WhatsApp bridges, local inference, and external inference. No vendor sandbox account unless the operator approves it. A stand-in exists for testing only and never counts as delivering a source (ADR 0003).

**Secrets.** Test secrets are generated on the test node and never committed. Production secrets stay with the operator.

**Promotion.** The operator deploys a `main` commit that is green on test, following the runbook in `docs/environments.md`.

## Open decisions

- **Production host.** Not chosen. Required before the production installation (`04-install-prod`) starts.

## Consequences

- Live sources are never exercised on throwaway or test; their acceptance is operator-run against the operator's own accounts.
- Local inference is stubbed on throwaway and test, so those tiers say nothing about classification quality or inference resource use.
