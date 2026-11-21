---
status: accepted
---

# Three environment tiers; test and production on the Mac mini, run natively

Glovebox has a hosted runtime (the gate service and its SQLite database), so it gets environments. Each tier runs under its own operating-system identity so that a ticket can never reach test state or production data.

| Tier | User | OpenClaw node | Host | What runs | Who touches it |
|------|------|---------------|------|-----------|----------------|
| Throwaway | `factory` | the ticket's factory node | the factory node's host | the ticket's own stack in rootless Docker | the ticket |
| Test | `glovebox-test`, a hidden macOS user | `glovebox-test` | the Mac mini (`florians-mac-mini`, macOS, arm64) | only `origin/main`, natively: a Python virtualenv run by launchd | `scripts/env.py ensure-test`, from the LaunchDaemon `glovebox.test.ensure-test` every 5 minutes |
| Production | `frogger` | none | the Mac mini | a `main` commit that is green on test, natively | the operator only; never tickets or agents |

**Hosts and runtime (superseding the decisions of 2026-11-15 and 2026-11-20).** The operator decided on 2026-11-21 that test and production both run on the Mac mini. The earlier choice was the 790 for test and `zotac1070` for production, both Linux with rootless Docker. The reason for the move is the iMessage source: it reads Messages' `chat.db` and needs a Mac signed in to Messages. Production must live where Messages does, and test is on the same machine and the same runtime so that what test proves is what production runs. Both run **natively**, with no Docker: the Mac mini has no container runtime, and the iMessage bridge has to read `chat.db` natively anyway. Throwaway is unchanged: rootless Docker on the Linux factory nodes. Docker on macOS is not used for any tier.

**Test identity.** `glovebox-test` is a hidden macOS user with no login session and no sudo, with the OpenClaw node `glovebox-test`. Its jobs are therefore system LaunchDaemons with `UserName` set to that user, installed once by root from the plists in `deploy/launchd/`. After that, `scripts/env.py` manages the stack without root: the daemons run `env.py serve`, and `up`, `down` and `ensure-test` only restart those user-owned processes, never launchd.

**Production identity.** `frogger` on the Mac mini, operator only. Messages is signed in under `frogger`, so the production gate runs as a LaunchAgent in that user's login session, with its secrets in files only `frogger` can read. Nothing is installed there until the operator runs `04-install-prod`.

**Shared tooling.** Throwaway and test share one executable Python `scripts/env.py` with `up`, `check`, `reset`, `down`, `ensure-test` and `install-timer`, plus `serve` for the native daemons. Throwaway runs in Docker from one compose file. The test tier on macOS runs the gate and the stand-ins from a virtualenv built from the checkout. `up` creates the gitignored env file with the test token when it is missing. `install-timer` installs the user timer `glovebox-test-env.timer` where the platform is Linux, and writes the launchd plists on macOS.

**Test endpoint.** Port **8765**. Nodes read `GLOVEBOX_TEST_URL` and `GLOVEBOX_TEST_TOKEN`. While the gate has no authentication it listens on loopback of the test host only; reaching it from other hosts needs a later change to the gate (`docs/environments.md`).

**Test data.** A fictional fixture cast that `reset` seeds. Never real, pseudonymised or production-copied data. The test tier and the production tier share a machine but not a user, a checkout, a data directory or a secret: `glovebox-test` cannot read `frogger`'s files.

**Stand-ins.** Every outside service gets a deterministic stand-in, fixture mode or a canned stub: mailbox.org mail, mailbox.org calendar, Office 365 mail and calendar, the Signal, iMessage and WhatsApp bridges, local inference, and external inference. No vendor sandbox account unless the operator approves it. A stand-in exists for testing only and never counts as delivering a source (ADR 0003). The test tier's iMessage stand-in is an HTTP fixture, so the test tier needs no Messages and no GUI login.

**Secrets.** Test secrets are generated on the test node and never committed. Production secrets stay with the operator.

**Promotion.** The operator deploys a `main` commit that is green on test, following the runbook in `docs/environments.md`.

## Consequences

- Live sources are never exercised on throwaway or test; their acceptance is operator-run against the operator's own accounts.
- Local inference is stubbed on throwaway and test, so those tiers say nothing about classification quality or inference resource use.
- Test and production share hardware, so test says nothing about production's resource headroom, and a fault of the machine takes both down.
- The native test tier has no container isolation. Its isolation is the hidden user: no login session, no sudo, and a home directory that other users cannot read (`docs/environments.md` lists this as a precondition).
- Only the part of production that reads Messages needs a GUI login session; the test tier needs none.
