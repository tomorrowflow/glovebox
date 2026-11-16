# 19: Source: iMessage

**Track:** source

**What to build:** The operator's iMessage conversations are served through the gate live as messenger items. Access depends on the operator's Apple-device setup, so the bridge may run as a separate process on that device and feed the gate (ADR 0005). Agents see recent messages as items with conversation and sender handles and permitted fields; the gate never sends or changes anything. If the device setup does not allow it, the source stays explicitly blocked and reported (ADR 0003).

**Blocked by:** 01, 02, 05, 07, 08, 11

**Status:** ready-for-agent

- [ ] The adapter uses an existing maintained iMessage bridge or access method, chosen after checking what exists; the choice, its maintenance state and its device requirements are documented.
- [ ] Where the bridge runs on a separate device, the connection between bridge and gate is authenticated, and agents can reach neither the bridge nor the device.
- [ ] Messages from the rolling 30-day window are ingested and kept current and mapped to the messenger item fields of ticket 11.
- [ ] The gate calls no bridge operation that sends, reacts, deletes or changes any state; a test against the stand-in asserts this.
- [ ] The account reports read-only enforcement as gateway-only, unless the access method is read-only by itself; the documentation states which and why.
- [ ] Any local history the bridge or device keeps is documented and unreadable for the agents' Linux users. Glovebox itself stores no raw message text.
- [ ] An unreachable device or bridge yields the stale or `blocked` status of ticket 08; never an empty result.
- [ ] Hostile-corpus assertion through the stand-in: corpus strings in bodies, contact and group names, reaction strings, filenames, content types and bridge error text reach no channel.
- [ ] Operator-run: at merge, the operator sets up the bridge on the Apple device and confirms that live messages appear as items with handles and permitted fields, that a newly received message shows up, and that nothing is marked read or sent. If the device setup does not allow it, the operator records the source as `blocked` with the reason; the ticket then closes as blocked, not as delivered.
