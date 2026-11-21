# Environments

Glovebox has three tiers (ADR 0007). Each runs under its own operating-system identity, so a ticket can never reach test state or production data. One tool, `scripts/env.py`, runs the throwaway and test tiers; production is the operator's own work. Throwaway runs in rootless Docker on the Linux factory nodes. Test and production run **natively** (a Python virtualenv started by launchd, no Docker) on the Mac mini, because the iMessage source needs macOS.

## The tiers

| | Throwaway | Test | Production |
|---|-----------|------|------------|
| User | `factory` (Linux) | `glovebox-test` (hidden macOS user) | `frogger` (macOS) |
| OpenClaw node | the ticket's factory node | `glovebox-test` | none |
| Host | the factory node's host | the Mac mini, `florians-mac-mini` (macOS, arm64) | the Mac mini |
| Runtime | rootless Docker | native: a virtualenv, run by launchd system daemons | native: a virtualenv, run by a launchd agent |
| Port | a free loopback port, chosen by Docker | **8765**, on loopback only (see Data rule below) | operator's choice |
| Compose project | `glovebox-<8 hex digits of the checkout path>` | none | none: the operator's own deployment; only `env.py check --url` is used |
| Who touches it | the ticket | the daemon `glovebox.test.ensure-test` | the operator only |
| What runs | the ticket's own checkout | only `origin/main`, long-lived | a `main` commit that is green on test |
| Data rule | fictional fixture cast only | fictional fixture cast only | the operator's real accounts; secrets only on the host (ADR 0004) |
| Stand-ins | all of them (below) | all of them (below) | none: real sources |

Nobody reaches a tier above their own. Tickets and agents never touch production.

### Commands

| Tier | Commands |
|------|----------|
| Throwaway | `python3 scripts/env.py up`, `check`, `reset`, `down` |
| Test | `python3 scripts/env.py up`, `check`, `reset`, `down`, `ensure-test`, `install-timer` (as `glovebox-test`; the tier is detected from the user) |
| Production | the runbook below |

`scripts/env.py` is stdlib-only Python and needs `git`. Throwaway needs `docker compose` 2.24 or newer (rootless Docker). The native test tier needs Python 3.12 or newer (see the Mac test host section) and network access to PyPI for `up`.

The backend follows the platform: the test tier is native on macOS and in Docker elsewhere; throwaway is always Docker. `GLOVEBOX_ENV_BACKEND` (`docker` or `native`) overrides it for the test tier, so the tooling can be exercised on a Linux node.

| Command | What it does |
|---------|--------------|
| `up` | Creates `.env` with a freshly generated `GLOVEBOX_TEST_TOKEN` if the file is missing (and leaves an existing file alone). **Docker:** runs `docker compose pull`, builds the gate image, seeds the fixture cast, and starts the stack. **Native:** creates `.venv` from the checkout if it is missing, installs the checkout into it with the pins in `constraints.txt` when those changed, seeds the fixture cast, tells the launchd-run `serve` processes which release to run, and waits up to 90 seconds until both run that release and `check` passes. Safe to repeat: with the same commit and pins nothing restarts. |
| `check` | Exits 0 when the stand-ins are healthy and the gate answers an MCP `initialize` request with a valid JSON-RPC 2.0 success reply (matching id, `protocolVersion`, `capabilities`, and `serverInfo` with `name` equal to `glovebox` and a string `version`); exits 1 otherwise (any other reply, a reply that is not HTTP, or a URL that is not `http` or `https`, counts as `unhealthy`). Prints fixed status words only (`ok`, `unreachable`, `unhealthy`, `missing`), never a response body, an error message or a secret. `--url URL` checks a gate at that MCP URL instead of the local stack; the bearer token then comes from the environment variable `GLOVEBOX_TEST_TOKEN`, or from the one named by `--token-env`. Without `--url` the token comes from the environment, else from `.env`. Natively, `check` probes the gate on loopback and each stand-in's `/healthz`. |
| `reset` | Reseeds the fictional fixture cast from `fixtures/cast.json`. Seeding twice gives byte-identical state. Overlapping seeds (a manual `reset` during the timer's `ensure-test`) take turns on a lock file in the state directory, so neither fails. Natively the stand-ins read the state on every request, so no restart is needed. |
| `down` | **Docker:** removes the containers, network and volumes. **Native:** tells `serve` to stop the gate and the stand-ins, waits for them to exit and removes the seeded state; the daemons stay loaded and idle. `.env` stays (and `.venv` natively), so the token survives. |
| `ensure-test` | Test tier only. Fetches `origin/main`, moves the checkout to it, runs `up` and then `check` with the updated script. Safe to run repeatedly; it exits without effect if another run holds `.env.lock`, and it refuses to run if the checkout has local changes. |
| `install-timer` | Test tier. **Linux:** writes `glovebox-test-env.service` and `glovebox-test-env.timer` into `~/.config/systemd/user/` (units in `deploy/systemd/`), reloads systemd and runs `systemctl --user enable --now glovebox-test-env.timer`. The timer runs `ensure-test` 30 seconds after it starts and then every 5 minutes. **macOS:** only `--no-enable --unit-dir DIR` is accepted. It writes the three test plists of `deploy/launchd/` into `DIR` and creates `~/Library/Logs/glovebox-test`; the wizard (root) installs them, see the Mac test host section. `--user` sets the daemons' user (default `glovebox-test`) and `--python` the interpreter they run (default: the one running `install-timer`). On Linux `--no-enable --unit-dir DIR` only writes the units, and works on any tier. |
| `serve SERVICE` | Native test tier only; the launchd daemons run it, nobody runs it by hand. Runs `gate` or `standins` in the foreground: it keeps the service's process alive while `up` wants it, restarts it when the wanted release changes, or 5 seconds after the process dies, and stops it on `down` or when launchd stops the daemon. When `ensure-test` changes `scripts/env.py`, it stops the service and re-executes the new script in the same process. |

Environment knobs: `GLOVEBOX_ENV_TIER` (`throwaway` or `test`) overrides the detected tier, `GLOVEBOX_ENV_BACKEND` the backend, `GLOVEBOX_ENV_PROJECT` the compose project name, and `GLOVEBOX_ENV_PUBLISH_PORT` the test tier's gate port (default 8765; Docker and native). They exist so the tooling can be exercised on a throwaway host without touching the long-lived test stack.

## The stack

On Docker, `compose.yaml` holds three services; `compose.test.yaml` is the override where the Linux test tier differs (fixed port 8765, `restart: unless-stopped`). Natively the same three parts run without containers. Both publish the gate on loopback only (see the allowed-hosts note below).

- `gate`: the Glovebox service. Docker: built from `deploy/Dockerfile.gate`, listening on 8765 in the container; throwaway publishes it on a free loopback port and `check` finds it with `docker compose port`. Native: `.venv/bin/glovebox` on `127.0.0.1:8765`.
- `standins`: the stand-ins, one listener per outside service (`deploy/standins/standins.py`). Natively they bind `127.0.0.1` (`STANDINS_HOST`) on the same ports.
- `seed`: a one-shot job that writes the fixture cast into the state (`deploy/standins/seed.py`): the `state` volume on Docker, `.run/state/seed` under the checkout natively.

Native state lives under the checkout, owned by the tier's user and gitignored: `.venv/` (the virtualenv), `.run/` (the seeded state, the `desired` file that `up` and `down` write and `serve` reads, and one pid file per service that also names the release it runs) and `.env`. Logs go to `~/Library/Logs/glovebox-test/`; launchd does not rotate them, so the operator trims them.

The gate enforces one static bearer token from configuration, `GLOVEBOX_AGENT_TOKEN`; operator-issued agent tokens replace it with ticket 07. The stacks set it to `GLOVEBOX_TEST_TOKEN`, the test environment's token, which `check` sends. A request without it gets `401` before any MCP handler runs. The gate keeps its SQLite database (permitted fields, handle mappings, sync state; no raw text) at `GLOVEBOX_DB`: the `gate-data` volume on Docker, `.run/state/glovebox.db` natively, so `down` removes it on both, and handles survive a restart but not a `down`. The test tier listens on 8765 on loopback of the Mac mini only, and `scripts/env.py` has no way to widen it. Nodes on other hosts cannot reach `GLOVEBOX_TEST_URL` yet. Two gate-side changes come first, and neither is made yet: a setting for the address the gate is published on, and an allowed-hosts setting, because the gate's MCP transport answers `421 Misdirected Request` to any `Host` header other than `localhost`, `127.0.0.1` or `[::1]` (DNS-rebinding protection). They need their own ticket, before ticket 03's last item can pass.

### Environment names

| Name | Where | Meaning |
|------|-------|---------|
| `GLOVEBOX_TEST_TOKEN` | `.env` on the stack's host; node profile | The test token. Generated on the node that runs the stack; never committed. |
| `GLOVEBOX_TEST_URL` | node profile | The MCP URL of the test environment: `http://<the Mac mini>:8765/mcp`. Set by the operator. |

Nodes that use the test environment read `GLOVEBOX_TEST_URL` and `GLOVEBOX_TEST_TOKEN`. To check it from another node: `python3 scripts/env.py check --url "$GLOVEBOX_TEST_URL"` (this works once the gate allows the Mac mini's host, see above). On the Mac mini itself, `python3 scripts/env.py check` as `glovebox-test` always works.

## Data rule

Throwaway and test hold only the **fictional fixture cast** in `fixtures/cast.json`: invented people, addresses in the reserved `example.test` domain and numbers in the reserved fictional range 555-0100 to 555-0199. Never put real, pseudonymised or production-copied data in a fixture, an environment, a log or a commit. `.env` is gitignored. Test secrets are generated on the test node; production secrets stay with the operator. Test and production share the Mac mini but never a user, a checkout, a data directory or a secret.

## Stand-ins

Every outside service has a deterministic stand-in; no vendor sandbox account is used. The stand-ins speak plain HTTP and JSON from the seeded state, not the vendor protocols, and each answers `GET /healthz` (503 until seeded) and `GET /` with its fixture. They are for testing only and never count as delivering a source (ADR 0003). The gate reaches them through `GLOVEBOX_STANDIN_*` variables, at the addresses below. It reads only `GLOVEBOX_STANDIN_MAILBOX_MAIL` so far, as its fixture mail source (source kind `fixture_mail`); the source tickets read the rest. `GLOVEBOX_MAIL_FIXTURE`, a path to a document of the same shape, replaces it for tests and local runs; setting both is a configuration error.

| Outside service | Stand-in | Address in the Docker stack (native: `127.0.0.1:<port>`) | Variable |
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

## Mac test host

The operator decided on 2026-11-21 that test and production run on the Mac mini (`florians-mac-mini`, macOS, arm64), natively, and records why in ADR 0007: the iMessage source needs macOS. This replaces the 790 as the test host. The test tier is the hidden macOS user `glovebox-test` with the OpenClaw node `glovebox-test`, created by the operator with `~/test-node-wizard.sh`. The test environment is installed by ticket 03.

### The launchd contract

`glovebox-test` has no login session and no sudo, so a user agent could never run for it, and `env.py` could never call `launchctl` for it. Every test job is therefore a **system LaunchDaemon** with `UserName` set to `glovebox-test`, installed once by root, and `env.py` manages the stack without root after that.

1. As `glovebox-test`, with a checkout and Python 3.12 or newer: `python3 scripts/env.py install-timer --no-enable --unit-dir DIR`. It writes the three plists below into `DIR`, each with `UserName`, a `Label` and logs under `~/Library/Logs/glovebox-test`, and creates that log directory. launchd does not create it.
2. As root, once, the wizard copies them to `/Library/LaunchDaemons` (owner `root:wheel`, mode `644`) and bootstraps them in the system domain: `launchctl bootstrap system /Library/LaunchDaemons/<label>.plist`.
3. From then on, as `glovebox-test`: `env.py up`, `check`, `reset`, `down` and `ensure-test` need no root. `up` and `down` write `.run/desired`; the `serve` processes started by the daemons read it and start, restart or stop the gate and the stand-ins, which are children owned by `glovebox-test`. Nothing in `env.py` calls `launchctl`, `sudo` or anything in the system domain.

| Plist (`deploy/launchd/`) | Label | Runs | Lifetime |
|---|---|---|---|
| `glovebox.test.gate.plist` | `glovebox.test.gate` | `env.py serve gate --tier test` | at load, kept alive |
| `glovebox.test.standins.plist` | `glovebox.test.standins` | `env.py serve standins --tier test` | at load, kept alive |
| `glovebox.test.ensure-test.plist` | `glovebox.test.ensure-test` | `env.py ensure-test --tier test` | at load, then every 300 seconds |

The gate and stand-in daemons start idle: until the first `up` there is no virtualenv and nothing is asked for, so they run nothing. `env.py` is re-executed by `serve` when `ensure-test` updates it, so a change to the tooling needs no new bootstrap. A change to the plists themselves does: render again with `install-timer`, copy, and `launchctl bootout` and `bootstrap` as root.

### What needs a GUI login

| Part | Needs a login session? |
|------|------------------------|
| Test gate, stand-ins and `ensure-test` (this section) | No. System daemons of a hidden user. The iMessage stand-in is an HTTP fixture and needs no Messages. |
| Production gate as a launchd agent (below) | Yes: `frogger` must be logged in, because a LaunchAgent lives in the user's GUI session. |
| Messages signed in under `frogger` (the iMessage source) | Yes, a GUI session at least once to sign in and grant Full Disk Access, and a logged-in session for Messages to keep syncing. This is why production is a LaunchAgent and not a daemon. |

### Preconditions

Check these as `glovebox-test` before ticket 03:

- **Python.** `python3 --version` prints 3.12 or newer. Use the Homebrew interpreter (`/opt/homebrew/bin/python3`), not `/usr/bin/python3`, which is older, and pass it as `--python` to `install-timer`: launchd's default `PATH` would otherwise find the old one. Homebrew must allow `glovebox-test` to read it (the default).
- **git.** `GIT_TERMINAL_PROMPT=0 git fetch --dry-run origin` succeeds without any prompt, as `glovebox-test`. There is no keychain without a login session, so use an SSH deploy key in `~/.ssh` with read access to `tomorrowflow/glovebox`, or a token in a `chmod 600` `~/.git-credentials` file with `credential.helper store`, not `osxkeychain`. The plist sets `GIT_TERMINAL_PROMPT=0`, so a missing credential fails the run instead of hanging it.
- **launchd domain.** The jobs are in the **system** domain: `launchctl print system/glovebox.test.gate` works for root and shows `UserName` as `glovebox-test`. There is no `gui/<uid>` domain for this user.
- **Isolation.** `glovebox-test`'s home directory is not readable by `frogger` or by the other users, and `glovebox-test` cannot read `frogger`'s.
- **Network.** `up` runs `pip install` against PyPI, as `glovebox-test`.

## Production runbook

The operator does this as `frogger` on the Mac mini. Tickets and agents never touch production: no ticket installs it, no agent holds a production token, and production secrets and upstream credentials exist only on the production host in files that the agents' users cannot read (ADR 0004).

The production host is the Mac mini, the same machine as test and a different user (ADR 0007, 2026-11-21). Nothing is installed there until the operator runs ticket 04.

1. Pick a `main` commit that is green on test: its CI passed, and the test environment has run it (`ensure-test` reports `at <commit>`, and `python3 scripts/env.py check` passes as `glovebox-test`).
2. As `frogger`, stop the gate if an earlier deployment loaded it, before touching the checkout, the virtualenv or the plist. A loaded agent keeps running the old code, and `launchctl bootstrap` of a label that is already loaded fails or leaves the old process in place, so `check` could then pass against the previous release. This is safe to repeat and does nothing on the first deployment:

   ```sh
   launchctl bootout gui/$(id -u)/glovebox.prod.gate 2>/dev/null || true
   ```

   Confirm that nothing is left: `launchctl print gui/$(id -u)/glovebox.prod.gate` must fail with "Could not find service", and nothing may be listening on the production port (`lsof -nP -iTCP:<port> -sTCP:LISTEN`).
3. Clone the repository to `~/glovebox` if it is not there, and check out exactly that commit: `git -C ~/glovebox fetch origin && git -C ~/glovebox checkout --detach <commit>`.
4. Build the virtualenv at that commit with Homebrew Python 3.12 or newer, with the pinned dependencies: `cd ~/glovebox && /opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install -c constraints.txt -e .`. Production does not use `scripts/env.py`, `compose.yaml`, the stand-ins or the fixtures, and it does not use `.env` of test.
5. Provide the production configuration and secrets from the operator's own store, in files only `frogger` can read: `install -d -m 700 ~/.config/glovebox` and then `~/.config/glovebox/gate.env` with `chmod 600`, as `KEY=VALUE` lines. The gate refuses to start without `GLOVEBOX_AGENT_TOKEN`, the one static agent token it enforces until ticket 07 (at least 16 printable characters, no whitespace; choose it yourself and keep it out of every ticket and agent). Set `GLOVEBOX_DB=$HOME/.local/share/glovebox/gate.db` too, so the database, and with it every handle, sits outside the checkout and survives redeploys; without it the gate writes `glovebox.db` into the checkout. Production has no mail source configured yet, so the read tool answers its fixed `source_unavailable` error there. Do not copy anything from test. Never put a secret in the plist, in the checkout or in a log.
6. Install the launchd plist as a LaunchAgent. `deploy/launchd/glovebox.prod.gate.plist` is a template. A fresh account has neither `~/Library/LaunchAgents` nor the log directory, and launchd creates neither, so make both, then fill in the template, then lint the result:

   ```sh
   mkdir -p ~/Library/LaunchAgents ~/Library/Logs/glovebox
   sed -e "s|@REPO@|$HOME/glovebox|g" -e "s|@HOME@|$HOME|g" \
       -e "s|@SECRETS@|$HOME/.config/glovebox/gate.env|g" \
       ~/glovebox/deploy/launchd/glovebox.prod.gate.plist \
       > ~/Library/LaunchAgents/glovebox.prod.gate.plist
   plutil -lint ~/Library/LaunchAgents/glovebox.prod.gate.plist
   ```

   The paths must not contain `&`, `|` or `<` (`sed` does not escape them for XML). The agent sources `gate.env` and runs `.venv/bin/glovebox`. Load it in `frogger`'s GUI session, after the stop in step 2 has been confirmed: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/glovebox.prod.gate.plist`. It starts at every login of `frogger`, so after a reboot the Mac mini must log `frogger` in (automatic login, or the operator).
7. For the iMessage source (a separate ticket): sign in to Messages under `frogger` in the GUI, and grant **Full Disk Access** to the process that reads `~/Library/Messages/chat.db`. macOS ties the grant to the executable: for a Python bridge that is the real interpreter (`readlink -f ~/glovebox/.venv/bin/python`, which resolves to a Homebrew path) or a wrapper binary you grant instead. A Homebrew upgrade changes that path and silently revokes the grant, so check it after each upgrade. Grant it in System Settings, Privacy & Security, in the GUI session. This runbook does not install the bridge.
8. Verify: `python3 scripts/env.py check --url <production MCP URL>`, with the production agent token in the variable named by `--token-env`. It must exit 0. Because step 2 stopped the old gate and was confirmed, a pass is the new release answering.
9. If `check` fails, stop the deployment by repeating step 2, restore the previous commit, rebuild the virtualenv (step 4) and bootstrap again (step 6), and investigate on throwaway or test.

Out of scope here: the iMessage source itself, operator-issued agent tokens (ticket 07), and the publish-address and allowed-hosts settings (a separate ticket).
