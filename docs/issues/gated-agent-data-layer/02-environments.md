# 02: Environments tooling and documentation

**Track:** environment

**What to build:** Everything ADR 0007 needs to bring up a throwaway or test stack with one command. A ticket can run `scripts/env.py up` on its factory node and get the gate in rootless Docker with a deterministic stand-in for every outside service and a seeded fictional fixture cast; `check` says whether the stack is healthy; `reset` reseeds; `down` removes it. The same tooling runs the long-lived test environment on the 790 from a user timer. `docs/environments.md` describes every tier and holds the production runbook.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] One compose file serves throwaway and test, with a compose override where the tiers differ; the gate listens on port 8765 on test.
- [ ] `scripts/env.py` is an executable Python script with `up`, `check`, `reset`, `down`, `ensure-test` and `install-timer`.
- [ ] `up` creates the gitignored env file with a freshly generated test token when the file is missing, and leaves an existing file alone. No secret is committed.
- [ ] `check` exits non-zero when the gate is unreachable or unhealthy, and prints only fixed status text.
- [ ] `reset` seeds the fictional fixture cast. The fixtures contain no real, pseudonymised or production-copied data.
- [ ] `ensure-test` brings the stack to the current `origin/main` and is safe to run repeatedly.
- [ ] `install-timer` installs and starts the user timer `glovebox-test-env.timer`, which runs `ensure-test` every 5 minutes; the timer unit is in the repository.
- [ ] Every outside service has a deterministic stand-in, fixture mode or canned stub: mailbox.org mail, mailbox.org calendar, Office 365 mail and calendar, the Signal, iMessage and WhatsApp bridges, local inference, external inference. No vendor sandbox account is used.
- [ ] `docs/environments.md` lists for every tier the Linux user, OpenClaw node, host, port, compose project, commands, data rule and stand-ins. It names the 790 as the test host with user and node `glovebox-test`, `GLOVEBOX_TEST_URL` and `GLOVEBOX_TEST_TOKEN`, and records the production host as open (ADR 0007).
- [ ] `docs/environments.md` contains the production runbook: the operator, as `frogger`, deploys a `main` commit that is green on test, and verifies it with `env.py check`. It states that tickets and agents never touch production.
- [ ] `AGENTS.md` points to `docs/environments.md` and `scripts/env.py`.
- [ ] Proven on a throwaway stack: `up`, `check`, `reset`, `check`, `down` succeed in the factory user's rootless Docker, and `ensure-test` and `install-timer` are exercised as far as a throwaway node allows.
