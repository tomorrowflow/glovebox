# 14: Source: mailbox.org mail

**Track:** source

**What to build:** The operator's mailbox.org mail is served through the gate live. After the operator adds the account, agents see its recent mail as items with handles and permitted fields, kept current by ongoing ingestion. The adapter never changes the mailbox. A source counts as delivered only with live ingestion; the stand-in is for tests (ADR 0003).

**Blocked by:** 01, 02, 05, 07, 08

**Status:** ready-for-agent

- [ ] The adapter uses an existing maintained mail library, chosen after checking what exists.
- [ ] Mail from the rolling 30-day window is ingested and kept current; read status, received time, attachment presence, sender and thread are mapped to permitted fields and handles.
- [ ] Reading does not mutate the mailbox: no message is marked seen, moved, flagged or deleted. A test against the stand-in asserts that no mutating command is ever sent.
- [ ] The account reports its read-only enforcement kind. Expected: gateway-only, because IMAP read access is not a credential boundary; if mailbox.org offers a read-only credential, it is used and reported as provider-enforced.
- [ ] Outage, authentication failure and malformed messages from the stand-in produce the failure behavior of ticket 08.
- [ ] Hostile-corpus assertion through the stand-in: corpus strings in every header, body part, filename, MIME parameter and server error text reach no channel.
- [ ] The semantic fields of ticket 09 are filled for this source once ticket 09 is merged; this ticket does not wait for it.
- [ ] Setup steps for the operator are documented, including where the credentials file goes.
- [ ] Operator-run: at merge, the operator adds the real mailbox.org account and confirms that live mail appears as items with handles and permitted fields, that a newly received mail shows up, that reading through the gate leaves messages unread in the mailbox, and that the documented read-only enforcement kind matches the real setup. If the account cannot be set up, the source is recorded as `blocked` with the reason, not as delivered.
- [ ] Operator-run: before the MVP is called complete, the operator interrupts the connection to mailbox.org and confirms that agents see the fixed outage error with stale status, and normal results after recovery.
