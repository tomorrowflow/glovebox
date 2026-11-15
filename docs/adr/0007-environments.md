---
status: accepted
---

# Three environment tiers, with the test and production hosts still open

Glovebox has a hosted runtime (the gate service and its SQLite database), so it gets environments. Each tier runs under its own Linux identity so that a ticket can never reach test state or production data.

| Tier | Linux user | OpenClaw node | What runs | Who touches it |
|------|-----------|---------------|-----------|----------------|
| Throwaway | `factory` | the ticket's factory node | the ticket's own stack in rootless Docker | the ticket |
| Test | `glovebox-test` | `glovebox-test` | only `origin/main`, in rootless Docker, long-lived | `scripts/env.py ensure-test`, from user timer `glovebox-test-env.timer` every 5 minutes |
| Production | `frogger` | none | a `main` commit that is green on test | the operator only; never tickets or agents |

**Shared tooling.** Throwaway and test share one compose file and one executable Python `scripts/env.py` with `up`, `check`, `reset`, `down`, `ensure-test` and `install-timer`. `up` creates the gitignored env file with the test token when it is missing. `install-timer` installs and starts the user timer `glovebox-test-env.timer`.

**Test endpoint.** Port **8765**. Nodes read `GLOVEBOX_TEST_URL` and `GLOVEBOX_TEST_TOKEN`.

**Test data.** A fictional fixture cast that `reset` seeds. Never real, pseudonymised or production-copied data.

**Stand-ins.** Every outside service gets a deterministic stand-in, fixture mode or a canned stub: mailbox.org mail, mailbox.org calendar, Office 365 mail and calendar, the Signal, iMessage and WhatsApp bridges, local inference, and external inference. No vendor sandbox account unless the operator approves it. A stand-in exists for testing only and never counts as delivering a source (ADR 0003).

**Secrets.** Test secrets are generated on the test node and never committed. Production secrets stay with the operator.

**Promotion.** The operator deploys a `main` commit that is green on test, following the runbook in `docs/environments.md`.

## Open decisions

- **Test host.** Not chosen. Pick a host that runs no production. Required before the test installation (`03-install-test`) starts.
- **Production host.** Not chosen. Required before the production installation (`04-install-prod`) starts.

## Consequences

- Live sources are never exercised on throwaway or test; their acceptance is operator-run against the operator's own accounts.
- Local inference is stubbed on throwaway and test, so those tiers say nothing about classification quality or inference resource use.
