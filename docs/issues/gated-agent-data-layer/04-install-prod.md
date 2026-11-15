# 04: Install production

**Track:** environment

**What to build:** The production gate, run by the operator as `frogger` from a `main` commit that is green on test, following the runbook in `docs/environments.md`. Tickets and agents never touch production; this ticket is the operator's own work.

**Why:** ADR 0007 leaves the production host as an open decision. This ticket cannot start until the operator has chosen it; when it is chosen, record the host in ADR 0007 and `docs/environments.md` first.

**Blocked by:** 01, 02, and the open production-host decision (ADR 0007)

**Blocks:** nothing

**Status:** ready-for-agent

- [ ] Operator-run: the operator chooses the production host and records it in ADR 0007 and `docs/environments.md`. Checked before installation.
- [ ] Operator-run: the operator, as `frogger`, follows the production runbook in `docs/environments.md` with a `main` commit that is green on test.
- [ ] Operator-run: `scripts/env.py check` passes against production.
- [ ] Operator-run: the operator confirms that production secrets and upstream credentials exist only on the production host, in permission-restricted files that the agents' Linux users cannot read (ADR 0004).
- [ ] Operator-run: the operator confirms that no agent token for production was issued by a ticket or an agent.
