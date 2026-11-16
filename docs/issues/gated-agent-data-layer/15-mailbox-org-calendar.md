# 15: Source: mailbox.org calendar

**Track:** source

**What to build:** The operator's mailbox.org calendar is served through the gate live. After the operator adds the account, agents get busy and free windows, overlaps and upcoming event times for it, kept current by ongoing ingestion. The adapter never changes the calendar (ADR 0003).

**Blocked by:** 01, 02, 05, 07, 08, 10

**Status:** ready-for-agent

- [ ] The adapter uses an existing maintained calendar library, chosen after checking what exists.
- [ ] Events within the 90-day look-ahead are ingested and kept current, including recurring and all-day events, and feed the availability tools of ticket 10.
- [ ] The adapter sends no request that creates, changes, answers or deletes an event; a test against the stand-in asserts this.
- [ ] The account reports its read-only enforcement kind. Preferred: provider-enforced through a read-only principal or credential if mailbox.org offers one; otherwise gateway-only, documented as such.
- [ ] Outage, authentication failure and malformed calendar data from the stand-in produce the failure behavior of ticket 08.
- [ ] Hostile-corpus assertion through the stand-in: corpus strings in every text property, UID, URL, attendee and organizer value, extension token and server error text reach no channel.
- [ ] Setup steps for the operator are documented.
- [ ] Operator-run: at merge, the operator adds the real mailbox.org calendar and confirms that busy and free windows match the real calendar for the coming week, that a newly created event shows up, and that the documented read-only enforcement kind matches the real setup. If the account cannot be set up, the source is recorded as `blocked` with the reason, not as delivered.
