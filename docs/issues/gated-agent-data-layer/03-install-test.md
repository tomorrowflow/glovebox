# 03: Install the test environment on the 790

**Track:** environment

**What to build:** The long-lived test environment of ADR 0007, running on the 790 under the dedicated Linux identity and OpenClaw node `glovebox-test`. It runs only `origin/main` in rootless Docker with fictional fixture data and stand-ins, is refreshed every 5 minutes by `glovebox-test-env.timer`, and is reachable for nodes through `GLOVEBOX_TEST_URL` and `GLOVEBOX_TEST_TOKEN` on port 8765.

The test host is settled: the operator decided on 2026-11-15 that `glovebox-test` runs on the 790. Nothing has been installed yet; this ticket performs the installation.

**Blocked by:** 01, 02

**Blocks:** only tickets whose proof uses the long-lived test environment (13).

**Status:** ready-for-agent

- [ ] Operator-run: on the 790, the operator runs `~/linux-test-node-wizard.sh tomorrowflow/glovebox`, which creates the Linux user and OpenClaw node `glovebox-test` with rootless Docker. Checked before the remaining steps.
- [ ] Operator-run: on node `glovebox-test`, dev runs `scripts/env.py up`, then `scripts/env.py check`, and `check` passes. The operator confirms the result.
- [ ] Operator-run: on node `glovebox-test`, dev runs `scripts/env.py install-timer`; the operator confirms that `glovebox-test-env.timer` is active and that a later `main` commit arrives on the test environment within 5 minutes without anyone touching it.
- [ ] Operator-run: the operator puts `GLOVEBOX_TEST_URL` (port 8765 on the 790) and `GLOVEBOX_TEST_TOKEN` into the node profile. The token was generated on the test node and is not committed anywhere.
- [ ] Operator-run: the operator confirms that the test environment holds only the fictional fixture cast and stand-ins, and no production credential or data.
- [ ] Operator-run: from a factory node, `scripts/env.py check` against `GLOVEBOX_TEST_URL` with `GLOVEBOX_TEST_TOKEN` passes. This needs a gate change that 02 does not make: the test gate is published on loopback only and refuses any `Host` header other than loopback (`docs/environments.md`).
