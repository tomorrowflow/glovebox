# 20: Source: WhatsApp

**Track:** source

**What to build:** The operator's WhatsApp conversations are served through the gate live as messenger items. After the operator pairs the account, agents see recent messages as items with conversation and sender handles and permitted fields. The gate never sends, reacts or marks anything read. Access depends on a linked device or bridge; if it cannot be set up, the source stays explicitly blocked and reported (ADR 0003).

**Blocked by:** 01, 02, 05, 07, 08, 11

**Status:** ready-for-agent

- [ ] The adapter uses an existing maintained WhatsApp bridge or library for a personal account, chosen after checking what exists; the choice, its maintenance state and any terms-of-service risk for the account are documented for the operator before pairing.
- [ ] Messages from the rolling 30-day window, counted from pairing where the bridge has no earlier history, are ingested and kept current and mapped to the messenger item fields of ticket 11.
- [ ] The gate calls no bridge operation that sends, reacts, deletes, sends read receipts or changes any state; a test against the stand-in asserts this.
- [ ] The account reports read-only enforcement as gateway-only; the documentation states that the bridge session could write and that agents cannot reach the bridge.
- [ ] Any local history the bridge keeps is documented (what, where, how long) and is unreadable for the agents' Linux users. Glovebox itself stores no raw message text.
- [ ] Pairing is an operator-only step outside MCP.
- [ ] A lost or unpaired session yields account status `blocked` or stale as defined in ticket 08; never an empty result.
- [ ] Hostile-corpus assertion through the stand-in: corpus strings in bodies, captions, profile and group names, quotes, contacts, locations, reaction strings, filenames, content types and bridge error text reach no channel.
- [ ] Operator-run: at merge, the operator pairs the real WhatsApp account and confirms that live messages appear as items with handles and permitted fields, that a newly received message shows up, that reading through the gate sends no read receipt, and that the bridge's history is unreadable for the agents' Linux users. If pairing is not possible, the operator records the source as `blocked` with the reason; the ticket then closes as blocked, not as delivered.
