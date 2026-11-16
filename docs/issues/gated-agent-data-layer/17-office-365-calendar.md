# 17: Source: Office 365 calendar

**Track:** source

**What to build:** The operator's work Office 365 calendar is served through the gate live, with a credential that cannot write. Agents get busy and free windows, overlaps and upcoming event times for the work calendar, alone or together with the private one. This source may need employer or admin consent; without it the source stays explicitly blocked and reported (ADR 0003).

**Blocked by:** 01, 02, 05, 07, 08, 10

**Status:** ready-for-agent

- [ ] The adapter uses the provider's official API through an existing maintained library, shared with ticket 16 where that ticket is already merged.
- [ ] The adapter requests only read permissions for calendars; no permission that can create, change or answer events.
- [ ] Events within the 90-day look-ahead are ingested and kept current, including recurring and all-day events, and feed the availability tools of ticket 10.
- [ ] The account reports read-only enforcement as provider-enforced, and the documentation names the permissions that make it so.
- [ ] A missing or withdrawn consent yields account status `blocked`; other accounts are unaffected.
- [ ] Outage, throttling, authentication failure and malformed responses from the stand-in produce the failure behavior of ticket 08.
- [ ] Hostile-corpus assertion through the stand-in: corpus strings in every event property, upstream ID and API error text reach no channel.
- [ ] Setup steps for the operator are documented, including the consent the employer has to grant.
- [ ] Operator-run: at merge, the operator connects the real work calendar and confirms that busy and free windows match the real calendar for the coming week and that the granted permissions are read-only. If consent is not granted, the operator records the source as `blocked` with the reason; the ticket then closes as blocked, not as delivered.
