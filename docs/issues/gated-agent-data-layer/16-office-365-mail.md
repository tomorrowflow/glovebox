# 16: Source: Office 365 mail

**Track:** source

**What to build:** The operator's work Office 365 mail is served through the gate live, with a credential that cannot write. After the operator adds the account, agents see recent work mail as items with handles and permitted fields. This source may need employer or admin consent; if consent is not granted, the source stays explicitly blocked and reported, and workplace restrictions are never bypassed (ADR 0003).

**Blocked by:** 01, 02, 05, 07, 08

**Status:** ready-for-agent

- [ ] The adapter uses the provider's official API through an existing maintained library, chosen after checking what exists.
- [ ] The adapter requests only read permissions, the narrowest that still allow classification of the body; no send or write permission is requested.
- [ ] Mail from the rolling 30-day window is ingested and kept current and mapped to permitted fields and handles; reading does not change read status.
- [ ] The account reports read-only enforcement as provider-enforced, and the documentation names the permissions that make it so.
- [ ] A missing or withdrawn consent yields account status `blocked`; other accounts are unaffected.
- [ ] Outage, throttling, authentication failure and malformed responses from the stand-in produce the failure behavior of ticket 08.
- [ ] Hostile-corpus assertion through the stand-in: corpus strings in every message property, upstream ID and API error text reach no channel.
- [ ] External inference stays off for this account unless the operator enables it explicitly (ADR 0002); the setup documentation says so.
- [ ] Setup steps for the operator are documented, including the consent the employer has to grant.
- [ ] Operator-run: at merge, the operator connects the real work account and confirms that live mail appears as items with handles and permitted fields, that the granted permissions are read-only, and that reading through the gate leaves messages unread. If consent is not granted, the operator records the source as `blocked` with the reason; the ticket then closes as blocked, not as delivered.
