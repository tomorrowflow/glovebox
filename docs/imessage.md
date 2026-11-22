# Source: iMessage

The `imessage` source serves the operator's received iMessages as messenger items: handles and permitted fields only, the rolling 30 days, read-only (ADR 0003, ticket 19). It is implemented and tested against the test tier's HTTP stand-in and a synthetic Messages database. **It has not been run against a real Messages database or a signed-in Mac: that is the operator's acceptance run below, and until it passes production stays parked** (ADR 0003: a stand-in never counts as delivering a source).

## The access method, and why

The gate reads the Mac's own Messages database, `~/Library/Messages/chat.db`, in place and read-only, with Python's standard `sqlite3`. There is no separate bridge process for production: the gate runs as `frogger` on the Mac mini, where Messages is signed in (ADR 0007), so the database is a local file of the gate's own user.

| Option | Verdict |
|---|---|
| **Read `chat.db` directly** (chosen) | Needs nothing beyond macOS and a signed-in Messages: no second process, no network hop, no extra credential, and no operation that could send. Read-only is enforced by the gate (below). The cost is that Apple does not document the schema. |
| `imsg` ([openclaw/imsg](https://github.com/openclaw/imsg)) | Maintained: v0.21.1 (16 Nov 2026), a commit on main on 21 Nov. macOS 14 or later; Full Disk Access for the launching context. Its `history`, `chats` and `watch` commands read SQLite read-only, but the generic macOS CLI and RPC also expose mutating methods (sends and tapbacks through AppleScript Automation, and an optional helper that needs SIP disabled). It adds a process between database and gate and nothing the same-Mac reader lacks. The read-only Linux build reads a copy, not the live Messages. Not adopted. If ever adopted: invoke only fixed read commands, grant no Automation and no helper, and never expose the general RPC. |
| `imessage-exporter` ([ReagentX](https://github.com/ReagentX/imessage-exporter)) | Maintained: 5.0.1 (29 Oct 2026), a commit on develop on 15 Nov; macOS, Linux and Windows; its development README records schema coverage through a recent macOS build, which is not a guarantee for this Mac. Reads the source read-only and cannot send, but writes TXT or HTML archives and can copy attachments, which is the raw archive ADR 0004 rules out. Kept as the schema reference. |
| BlueBubbles Server ([BlueBubblesApp](https://github.com/BlueBubblesApp/bluebubbles-server)) | Maintained: v1.9.12 (27 Oct 2026), also the latest commit on master. Needs signed-in Messages, Full Disk Access and a server password (Accessibility is optional); the current repository gives macOS 10.15 or later (Apple Silicon 11 or later), and an older "Sierra" claim in its overview contradicts it. It polls the same database and sends through AppleScript even without the optional Private API, and its REST API documents password authentication, not a read-only credential. It would add a listening endpoint and a second product to run. Not adopted. |
| Driving Messages with AppleScript or Shortcuts | Can send, needs Automation permission, and cannot list the recent history. Not adopted. |
| Reimplementations of the iMessage protocol | Hold the Apple ID's credentials and can send; unofficial and fragile. Not adopted. |

None of these improves a same-Mac, no-archive, no-send design, and none is needed: no Messages Automation, SIP change, bridge service or added credential. Status above is from public sources as of 22 Nov 2026 (release pages and READMEs; no device, credential or message was accessed). Re-check it before choosing a different method.

**Maintenance state.** `sqlite3` ships with Python and is as maintained as the interpreter. Apple's private Messages schema is not a supported API and can change between macOS releases; two changes are already handled: dates became nanoseconds, and from macOS Ventura the text can be left out of `message.text` and kept only in the typed `attributedBody` blob (the reader takes only the string out of it). The database is opened with a `file:` URI and `mode=ro` (Python's `uri=True`), never `immutable=1`: immutable tells SQLite the file cannot change, which is false for a live database that Messages is writing. SQLite enforces read-only on the message data through the connection; the WAL and `-shm` sidecar files and locks can still be involved. When a macOS update changes a table or column the reader needs, it fails and the account reports `stale`, never an empty result; access denied is `blocked`. **After every macOS update, and on the operator's own macOS build, re-check three things:** the schema (the account is not `stale`), the freshness of the WAL (a newly received message appears), and local sync (Messages is still signed in and syncing).

Sources: [SQLite URIs in Python](https://docs.python.org/3/library/sqlite3.html#how-to-work-with-sqlite-uris), [read-only WAL databases](https://www.sqlite.org/wal.html#read_only_databases), [the `immutable` URI parameter](https://www.sqlite.org/uri.html#uriimmutable).

**Device requirements.**

- A Mac signed in to Messages as the operator's Apple ID: the Mac mini, user `frogger`, signed in once in the GUI, with Messages kept running and logged in so that new messages sync (ADR 0007). The production gate is therefore a LaunchAgent in that login session.
- **Full Disk Access** for the process that opens `chat.db`. macOS grants it per executable: here the gate's own interpreter (`readlink -f ~/glovebox/.venv/bin/python`, a Homebrew path), because the gate opens the file itself. A grant can go stale, for example when a Homebrew upgrade changes that path (not necessarily on every upgrade), so verify it **under the actual LaunchAgent**, not only in a terminal, after upgrades. Without it, opening the file fails and the account is `blocked`. Full Disk Access is broader than this one database: the read-only guarantee is at the access-method level (the connection), not an operating-system sandbox.
- A consequence to accept: the gate's process, not a helper, holds Full Disk Access. It is the process that serves agents, so the interpreter's permission is only as safe as the gate's code. Granting a separate small reader would narrow this, at the price of a second process; it is not done here.
- The test tier needs none of this: it reads the iMessage stand-in over HTTP, which needs no Messages, no GUI session and no permission.

**What the gate cannot see.** An unreadable file is `blocked` and a database it cannot understand is `stale`, but the gate cannot tell that Messages is signed out, not running or not syncing: the file stays readable, so the account is `current` and merely has no newer messages. Treat a long-silent iMessage account as something to check on the Mac (the operator's acceptance run covers sign-in; nothing automatic does after it). Reading a database in WAL mode also touches the `-shm` index file next to it, which holds no message content; no message row is changed. If a macOS permission withholds only the `-wal` or `-shm` file, SQLite's open fails with no cause attached and the account reports `stale`, not `blocked`.

## What an item is

- Only **received** messages are items. The operator's own messages, reactions (tapbacks), and system messages (group renames, joins) are left out silently.
- `thread_handle` stands for the conversation (direct or group alike) and `sender_handle` for the sender's phone number or address as Messages records it; the gate turns both into opaque handles. Names, group names, display names, attachment names and types, reactions and every other column are never selected (`chatdb.QUERY`).
- `read_status` is `read` or `unread` from Messages' own flag; the gate never changes it. Attachment presence is a flag; nothing of the attachment is read.
- The text goes to the quarantined classifier in memory and nowhere else; the gate stores, logs and serves none of it. A message that cannot be understood (no sender, no conversation, an impossible date) is skipped and counted (`incomplete`), and so is any excess over 50,000 messages in the window.
- The database is read at every tool call, like every source, and only the window's rows are read.

## Read-only enforcement: gateway-only

Every account entry reports `read_only_enforcement`. For iMessage it is **`gateway`**, not `provider`: the macOS permission that lets the process open the file does not distinguish reading from writing, and the file is writable by its owner, so the access method itself could write. What holds the line is the gate:

- The gate has no write tool and exposes no operation that sends, reacts, deletes or marks read (ADR 0003).
- `chatdb.open_read_only` opens the file with SQLite `mode=ro`, sets `query_only`, and installs an authorizer that refuses everything except reading, so an `UPDATE`, `ATTACH`, `PRAGMA` or schema change fails inside SQLite even if one were written by mistake. A test attempts each of them.
- The adapter selects only the columns it maps and never opens the attachments directory.
- Sending is a different path (Messages itself); the gate never touches it. The gate also marks nothing read: Messages does that when the operator opens a conversation.

If a bridge is used instead of the local file (the stand-in does this), the gate makes one request, `GET` of the configured location, and no other method; a test records every request the stand-in receives and asserts that.

## A separate bridge, if one is ever needed

None is needed today. If the gate is ever moved off the Mac, a bridge on the Mac would serve the messenger document over HTTP (the stand-in's shape: `account`, and `conversations` with `messages`) and the gate would read it as an `imessage` account with an `http(s)` source:

- The connection is always authenticated when the bridge is on another host. The gate sends a bearer token from the account's credentials file (`account add --credentials FILE`, mode `0600`, ADR 0004), and only over `https` or to this host. `account add` refuses a bridge on another host without `--credentials`, and refuses a plain-`http` one with it; the gate enforces the same at every read (a source or credentials file that changed since is `blocked`, and nothing is sent). Only the stand-in and a bridge on this host (loopback) may go without a token. A redirect is a failure and is not followed.
- The bridge must be reachable only from the gate's host and never by agents; it holds the raw text and a write-capable session (ADR 0005). Agents reach only the gate's MCP endpoint with an agent token.
- A bridge that refuses the token, or answers 401 or 403, makes the account `blocked`; any other failure makes it `stale`. No upstream text is kept: error text is dropped.

## Local history, and who can read it

- Messages keeps its own history on the Mac, in `~/Library/Messages/chat.db` (with `-wal` and `-shm` files) and received attachments under `~/Library/Messages/Attachments`. `~/Library/Messages` belongs to the user `frogger`, and macOS privacy controls withhold it from any process without Full Disk Access. The acceptance run checks the file modes rather than assuming them.
- The agents run as other Linux users on other hosts, which reach the gate only over its authenticated HTTP endpoint. They have no account on the Mac mini, so they cannot read these files. The operator verifies this at acceptance (below), and again whenever the Mac's users change.
- Glovebox stores no raw message text. Its database (`GLOVEBOX_DB`) holds handles, permitted fields, classifications and sync state; the SQLite connection keeps its temporary tables in memory.
- Messages' own history is the operator's own, outside Glovebox, and follows the operator's Messages settings (including how long it keeps messages).

## The test tier

The seeded iMessage stand-in (`standins:9106`, `GLOVEBOX_STANDIN_IMESSAGE`) serves a fictional conversation at `GET /`. The operator adds it as an account, the way the stack's bootstrap does not:

```sh
glovebox account add imessage-test --kind imessage --source http://127.0.0.1:9106
glovebox token issue --account imessage-test --label test-agent
```

The tests drive it with a real MCP client (`tests/test_imessage.py`): the mapping, freshness and `blocked`, no request but `GET /`, account scope, bearer authentication, cancellation, a gate restart, and a hostile corpus in every field the bridge, a sender or a chat participant controls, including the bridge's error text.

## Acceptance on the Mac mini (operator-only)

Run once at merge, as `frogger`, in the GUI session, **in an isolated gate: a scratch database, another port, and a checkout of your own. Not the shared test tier and not the production install.** Production stays parked until this passes. If device access does not work, record the source as `blocked` with the reason; the ticket then closes as blocked, not as delivered.

1. **Sign-in and permission.** Messages is signed in under `frogger` (open Messages, and check that a conversation loads). Grant Full Disk Access to the real interpreter in System Settings, Privacy & Security: `readlink -f <checkout>/.venv/bin/python`.
2. **Snapshot what must not change**, read-only (the `sqlite3` you run needs the same permission, or use Terminal with it):

   ```sh
   sqlite3 -readonly ~/Library/Messages/chat.db \
     "select count(*) from message where is_from_me = 0 and is_read = 0; \
      select count(*), coalesce(max(ROWID), 0) from message where is_from_me = 1;"
   ```

3. **An isolated gate** on a free port, with a scratch database and an account on the real file:

   ```sh
   export GLOVEBOX_DB=$(mktemp -d)/acceptance.db GLOVEBOX_PORT=18765
   <checkout>/.venv/bin/glovebox account add imessage --kind imessage --source ~/Library/Messages/chat.db
   export GLOVEBOX_CHECK_TOKEN=$(<checkout>/.venv/bin/glovebox token issue --account imessage 2>/dev/null)
   <checkout>/.venv/bin/glovebox serve &
   ```

4. **Check what crosses the gate:** `<checkout>/.venv/bin/python scripts/imessage_check.py --url http://127.0.0.1:18765/mcp`. Expect `read_only_enforcement: gateway`, status `current`, a recent last sync, and items that carry only handles, minute-precision `received`, `read_status` and `has_attachments`, with no text. Without Full Disk Access the status is `blocked` and the exit status 1: that is the result to record if you cannot grant it.
5. **A newly received message.** Send yourself a message from another device, run step 4 again: one more item, with a newer `received` time, `unread` unless you opened it, and nothing else about it.
6. **Nothing changed.** Repeat the two queries of step 2: the unread count has changed only by what you received or read yourself, and the count and highest ROWID of your own messages are the same, so nothing was sent. Do not open the conversation in Messages between step 2 and step 6 (that marks it read).
7. **Agents cannot read the history.** For each agent user that exists on this Mac: `sudo -u <user> cat ~/Library/Messages/chat.db` fails with `Permission denied`. Agents on other hosts have no account here.
8. Stop the isolated gate (`kill %1`) and delete the scratch directory.

Record the result on the ticket: delivered (all steps pass), or `blocked` with the step that failed and the reason.
