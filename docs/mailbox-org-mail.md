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

Library versions above come from PyPI (via `pip`). The provider facts come from the review research of 25 Nov 2026 on mailbox.org's official guides, not from an authenticated account (confidence high for the endpoint and the documented protocol controls; nothing about private or enterprise arrangements): the endpoint is `imap.mailbox.org`, port 993, implicit SSL/TLS, with the account's primary email address as the user name; and when a dedicated mail app password is generated, the guides offer a choice of **IMAP and/or SMTP**, and list no read-only permission or OAuth scope. Sources: the guides to [e-mail configuration](https://kb.mailbox.org/de/privat/e-mail/e-mail-konfiguration/), the [e-mail password](https://kb.mailbox.org/de/privat/sicherheit-privatsphaere/e-mail-passwort/) (also [for business](https://kb.mailbox.org/de/business/sicherheit-privatsphaere/e-mail-passwort/)) and [app passwords for external programs](https://kb.mailbox.org/de/privat/sicherheit-und-privatsphaere/applikationspasswoerter-fuer-externe-programme/), and RFC 9051 on [`EXAMINE`](https://www.rfc-editor.org/rfc/rfc9051.html#section-6.3.2) and [`FETCH` with `BODY.PEEK`](https://www.rfc-editor.org/rfc/rfc9051.html#section-6.4.5). Check these again at setup: providers change their settings.

## What an item is

- Only the **inbox** is read. Sent, Drafts, Junk and other folders are not.
- `sender_handle` stands for the `From` address, lowercased: one address is one sender. `thread_handle` stands for the thread's root message: the first ID in `References`, else `In-Reply-To`, else the message's own `Message-ID` (a message with none is its own thread). The gate turns both into opaque handles; names, recipients, filenames, MIME parameters and every other header are never read out of a message.
- `received` is the time the server recorded (`INTERNALDATE`, minute precision, UTC), not the `Date` the sender wrote, which a sender controls.
- `read_status` is `read` if the message has the `\Seen` flag, in any letter case (system flags are case-insensitive, RFC 9051), else `unread`; a keyword that merely is called `Seen` does not count. The gate never sets it.
- `has_attachments` comes from the message's `BODYSTRUCTURE`: a part whose disposition is `attachment`, or a part with no disposition that is not text, not a signature and not inside `multipart/related` (the images of an HTML body). An inline image with a disposition of `inline` is not an attachment. Only the disposition's type is read, never a name. This is a rule of thumb: a mail client may draw the paperclip differently for an unusual message.
- The subject and the text part (the plain part, else the HTML part with its tags removed) go to the quarantined classifier in memory, cut to 16,000 characters, and nowhere else: the gate stores, logs and serves none of it. Of each message the gate fetches the first 64 KiB, so a very large message's text is cut there.
- The window's mail is re-read at every tool call, like every source; only the window's messages are asked for (`SINCE`, a day early, and the gate drops what is older). The **whole window** is read, however many messages it holds, a hundred to a command, so no one response is unbounded and no valid mail of the window is left out. The price is time: a read is cut off after `GLOVEBOX_MAILBOX_READ_DEADLINE_SECONDS` (whole seconds, 1 to 3600, default 120; a bad value stops the service from starting), counted from the start of the read to the end of the logout, checked before every command and enforced even while the server stalls or trickles bytes: the whole connection setup is under the same clock: the name lookup (run on a thread that is left behind if the time runs out) and each connect attempt get only the time left, capped at 10 seconds for an attempt, and the socket is watched as soon as it is connected, so a TLS handshake or a greeting that never completes is cut off at the limit too. A read that is not done by then is an outage (`stale`, with the fixed `source_unavailable` error), never a smaller success; an operator with a very large inbox raises the limit. A message the gate cannot understand (no sender, a date or structure the library cannot parse) is skipped and counted, and the account is `incomplete`.
- A message's handle is bound to the mailbox's `UIDVALIDITY` and the message's UID. If the server renumbers the mailbox (a new `UIDVALIDITY`, rare) the messages get new handles.

## Read-only enforcement: gateway-only

Every account entry reports `read_only_enforcement`. For mailbox.org mail it is **`gateway`**, not `provider`: an IMAP login can also write, and IMAP's `EXAMINE` is a request not to, not a credential boundary (ADR 0003, `docs/research/03-attacker-fields-and-credentials.md`). The gate holds a login that could write; what holds the line is the gate:

- The gate has no write tool and exposes no operation that sends, marks, moves, flags or deletes (ADR 0003).
- After the login the gate sends exactly four kinds of command: `EXAMINE` (the read-only open: never `SELECT`), `UID SEARCH`, `UID FETCH` and `LOGOUT`. Every fetch item is metadata or `BODY.PEEK[...]`, which does not set `\Seen`. A test runs the gate against the IMAP stand-in through every read tool and every failure, and asserts that the commands it received are within that set, that every fetch item peeks, and that no message's flags changed; another test shows that the same stand-in does record a mutation when a client selects for writing and fetches without `PEEK`.
- mailbox.org's app passwords can be limited to **IMAP and/or SMTP**, and no read-only permission is documented (see above). An IMAP-only app password (SMTP unchecked) keeps the password from sending mail, but it does not stop an IMAP client from changing the mailbox: it is still **`gateway`**, never `provider`. `EXAMINE` and `BODY.PEEK` are controls of the gate's own session, not limits on what another client could do with the password. If mailbox.org ever documents a read-only credential, use it and change the kind to `provider` (`READ_ONLY_ENFORCEMENT` in `src/glovebox/sources.py`).

## Setting up the account (operator-only)

Do this on the gateway host as the gateway's own user, with `GLOVEBOX_DB` set as for any account (`docs/environments.md`, "Production").

1. In mailbox.org, generate a dedicated, individually revocable **app password** for the gate with **IMAP only** (leave SMTP unchecked), and use it instead of the account's main password.
2. Put the login in a credentials file that only the gateway's user may read, for example `~/.config/glovebox/mailbox.credentials`, with `chmod 600`: two lines, the user name (the account's primary email address) on the first and the app password on the second, and nothing else:

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

`tests/test_mailbox_org.py` drives the gate with a real MCP client against the IMAP stand-in over a real socket: the mapping above, new and changed mail showing up at the next read, no mutating command, the outage, refusal and malformed-message behavior of ticket 08, case variants of the `\Seen` flag, a window of more than 2,000 messages read whole on every read, a stalled or trickling server, including a name lookup, a greeting or a TLS handshake that never completes, cut off at the time limit as an outage with stale status and recovery on the next read, a real TLS handshake against a self-signed stand-in (certificate verified), the account-setup refusals, and a hostile corpus in every header, text part, filename, MIME parameter, flag and server status line, checked in every response channel, the log and the stored files.

## Acceptance on the real account (operator-only)

Run once at merge, **in an isolated gate: a scratch database, another port, and a checkout of your own**, with the real account. If the account cannot be set up, record the source as `blocked` with the reason; the ticket then closes as blocked, not as delivered.

1. Add the account as above and issue a token. Through an agent connection (or `scripts/` of your own), call `list_accounts`, then `list_items`: confirm live mail appears as items with handles and the permitted fields, status `current`, and a plausible count for the last 30 days.
2. Send yourself a mail and call `list_items` again: it appears, `unread`.
3. In your mail client, note which messages are unread, call `list_items`, `count_items` and `group_items` several times, and confirm in the mailbox that **every message that was unread is still unread** and nothing was moved, flagged or deleted. (If you want proof from the server side, check mailbox.org's login or IMAP log if it offers one: only `EXAMINE`, search and fetch.)
4. Confirm that the account's `read_only_enforcement` is the one this document says (`gateway`), that the app password was generated with IMAP only, and that the account's own settings show no read-only option for it (the research above covers the public guides, not your account).
5. Before the MVP is called complete: cut the connection to mailbox.org (for example block `imap.mailbox.org` on port 993 at the host's firewall, or take the host off the network), call `list_items`, and confirm the fixed `source_unavailable` error with the account's status `stale` and its last sync time. Restore the connection and confirm normal results again, with no operator action.
