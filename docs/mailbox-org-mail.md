# Source: mailbox.org mail

The `mailbox_org_mail` source serves the operator's mailbox.org inbox as mail items: handles and permitted fields only, the rolling 30 days, never changing the mailbox (ADR 0003, ticket 14). It is implemented and tested against an IMAP server stand-in (`tests/imap_standin.py`). **It has not been run against a real mailbox.org account: that is the operator's acceptance run below, and until it passes the source is not delivered** (ADR 0003: a stand-in never counts as delivering a source). The stand-in exists only in the test suite; the throwaway and test stacks' HTTP stand-ins do not speak IMAP.

## The access method, and why

The gate logs in to `imap.mailbox.org` over IMAP with TLS (port 993) and reads the inbox. IMAP is mailbox.org's standard mail access; there is no REST API for mail to prefer.

| Option | Verdict |
|---|---|
| **IMAPClient** ([mjs/imapclient](https://github.com/mjs/imapclient), BSD-3, no dependencies) | Chosen: maintained (4.3.0, the newest on PyPI when checked on 25 Nov 2026), a thin layer over the standard `imaplib` with response parsing, and it lets the caller choose each command and fetch item, which the read-only rule needs: `select_folder(readonly=True)` sends `EXAMINE`, and `fetch` takes `BODY.PEEK[...]`. It parses `BODYSTRUCTURE`, from which attachment presence is read without downloading attachments. It ships no type stubs; mypy ignores its imports. |
| `imap-tools` | Maintained (1.16.0, the newest on PyPI when checked). Not evaluated in depth: IMAPClient already gives the per-command control the read-only rule needs and a parsed `BODYSTRUCTURE`, and this library's whole-message objects are aimed at reading attachments and names the gate must not read. Not adopted. |
| Python's `imaplib` alone | Standard library, but the response parsing (`BODYSTRUCTURE`, literals, dates) would be ours to write and keep correct against hostile input. Not adopted. |
| A sync tool (`mbsync`, `offlineimap`) | Makes a full local copy of the mail: the raw archive ADR 0004 rules out. Not adopted. |

I could not reach mailbox.org's documentation while building this (no outside access), so nothing below about the provider is from its pages. **Re-check at setup:** the server name and port (`imap.mailbox.org`, 993, TLS), and whether mailbox.org lets you create an app password limited to read-only access (see "Read-only enforcement").

## What an item is

- Only the **inbox** is read. Sent, Drafts, Junk and other folders are not.
- `sender_handle` stands for the `From` address, lowercased: one address is one sender. `thread_handle` stands for the thread's root message: the first ID in `References`, else `In-Reply-To`, else the message's own `Message-ID` (a message with none is its own thread). The gate turns both into opaque handles; names, recipients, filenames, MIME parameters and every other header are never read out of a message.
- `received` is the time the server recorded (`INTERNALDATE`, minute precision, UTC), not the `Date` the sender wrote, which a sender controls.
- `read_status` is `read` if the message has the `\Seen` flag, else `unread`. The gate never sets it.
- `has_attachments` comes from the message's `BODYSTRUCTURE`: a part whose disposition is `attachment`, or a part with no disposition that is not text, not a signature and not inside `multipart/related` (the images of an HTML body). An inline image with a disposition of `inline` is not an attachment. Only the disposition's type is read, never a name. This is a rule of thumb: a mail client may draw the paperclip differently for an unusual message.
- The subject and the text part (the plain part, else the HTML part with its tags removed) go to the quarantined classifier in memory, cut to 16,000 characters, and nowhere else: the gate stores, logs and serves none of it. Of each message the gate fetches the first 64 KiB, so a very large message's text is cut there.
- The window's mail is re-read at every tool call, like every source; only the window's messages are asked for (`SINCE`, a day early, and the gate drops what is older). A window with more than 2,000 messages is cut to the newest 2,000 and the rest is reported as skipped, so the account is `incomplete`, never `current`. A message the gate cannot understand (no sender, a date or structure the library cannot parse) is skipped and counted, and the account is `incomplete`.
- A message's handle is bound to the mailbox's `UIDVALIDITY` and the message's UID. If the server renumbers the mailbox (a new `UIDVALIDITY`, rare) the messages get new handles.

## Read-only enforcement: gateway-only

Every account entry reports `read_only_enforcement`. For mailbox.org mail it is **`gateway`**, not `provider`: an IMAP login can also write, and IMAP's `EXAMINE` is a request not to, not a credential boundary (ADR 0003, `docs/research/03-attacker-fields-and-credentials.md`). The gate holds a login that could write; what holds the line is the gate:

- The gate has no write tool and exposes no operation that sends, marks, moves, flags or deletes (ADR 0003).
- After the login the gate sends exactly four kinds of command: `EXAMINE` (the read-only open: never `SELECT`), `UID SEARCH`, `UID FETCH` and `LOGOUT`. Every fetch item is metadata or `BODY.PEEK[...]`, which does not set `\Seen`. A test runs the gate against the IMAP stand-in through every read tool and every failure, and asserts that the commands it received are within that set, that every fetch item peeks, and that no message's flags changed; another test shows that the same stand-in does record a mutation when a client selects for writing and fetches without `PEEK`.
- mailbox.org may offer an app password limited to read access, or to IMAP. **If it can be limited to read-only, use it and change the documented kind to `provider`** (`READ_ONLY_ENFORCEMENT` in `src/glovebox/sources.py`); this was not verified. A password limited to IMAP still writes, so it stays `gateway`.

## Setting up the account (operator-only)

Do this on the gateway host as the gateway's own user, with `GLOVEBOX_DB` set as for any account (`docs/environments.md`, "Production").

1. In mailbox.org, create an **app password** for the gate if the account allows it (preferable to the main password: it can be revoked on its own), and make sure IMAP access is enabled for the account.
2. Put the login in a credentials file that only the gateway's user may read, for example `~/.config/glovebox/mailbox.credentials`, with `chmod 600`: two lines, the user name (the mailbox.org address) on the first and the password on the second, and nothing else:

   ```
   operator@mailbox.org
   the-app-password
   ```

   A file readable by group or others is refused by `account add` and by the service at every read (ADR 0004). There is no encryption of the file; the host's user separation is the protection.
3. Add the account:

   ```sh
   glovebox account add mailbox --kind mailbox_org_mail --source imaps://imap.mailbox.org \
     --credentials ~/.config/glovebox/mailbox.credentials
   glovebox token issue --account mailbox --label <agent>
   ```

   `account add` refuses a missing credentials file, a file of any other shape, a source that is not `imaps://HOST[:PORT]` (a plain `imap://` source is accepted only for this host, which is how the test stand-in is reached, so the login is never sent in clear across a network) and a source with a path or credentials in it. The server's certificate is verified.
4. Check: `glovebox account list` shows `ok` as the credentials status, and an agent's `list_accounts` shows the account with status `current` after its first read.

A wrong password, or a server that refuses the login, makes the account `blocked`. A server that cannot be reached, answers wrongly or drops the connection makes it `stale`. Nothing the server says is kept: error text is dropped. An unreachable server never looks like an empty inbox: reads return the fixed `source_unavailable` error with the account's status and last successful sync time.

## The test tier

`tests/test_mailbox_org.py` drives the gate with a real MCP client against the IMAP stand-in over a real socket: the mapping above, new and changed mail showing up at the next read, no mutating command, the outage, refusal and malformed-message behavior of ticket 08, the account-setup refusals, and a hostile corpus in every header, text part, filename, MIME parameter, flag and server status line, checked in every response channel, the log and the stored files.

## Acceptance on the real account (operator-only)

Run once at merge, **in an isolated gate: a scratch database, another port, and a checkout of your own**, with the real account. If the account cannot be set up, record the source as `blocked` with the reason; the ticket then closes as blocked, not as delivered.

1. Add the account as above and issue a token. Through an agent connection (or `scripts/` of your own), call `list_accounts`, then `list_items`: confirm live mail appears as items with handles and the permitted fields, status `current`, and a plausible count for the last 30 days.
2. Send yourself a mail and call `list_items` again: it appears, `unread`.
3. In your mail client, note which messages are unread, call `list_items`, `count_items` and `group_items` several times, and confirm in the mailbox that **every message that was unread is still unread** and nothing was moved, flagged or deleted. (If you want proof from the server side, check mailbox.org's login or IMAP log if it offers one: only `EXAMINE`, search and fetch.)
4. Confirm that the account's `read_only_enforcement` is the one this document says (`gateway`), or check whether mailbox.org offers a read-only app password and, if it does, use it and change the kind to `provider`.
5. Before the MVP is called complete: cut the connection to mailbox.org (for example block `imap.mailbox.org` on port 993 at the host's firewall, or take the host off the network), call `list_items`, and confirm the fixed `source_unavailable` error with the account's status `stale` and its last sync time. Restore the connection and confirm normal results again, with no operator action.
