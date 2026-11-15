# 10: Calendar availability

**Track:** calendar

**What to build:** An agent can ask when the operator is busy or free, which events overlap, and when upcoming events start and end, for one or several calendar accounts its token covers. Events appear as handles with times and a busy status; titles, descriptions, locations, attendees and organizer details never cross the gate. Built against a fixture calendar source.

**Blocked by:** 01, 02, 05

**Status:** ready-for-agent

- [ ] An agent can request busy and free windows for a time range across one or more calendar accounts.
- [ ] An agent can list upcoming events with event handle, account handle, start, end, all-day flag and busy status from a closed vocabulary.
- [ ] An agent can ask which events overlap in a time range and gets the overlapping event handles with their times.
- [ ] Events beyond the 90-day look-ahead are neither ingested nor served.
- [ ] Recurring events and all-day events in the fixtures produce correct windows across a daylight-saving change.
- [ ] Upstream status and participation values outside the local vocabulary become `unknown`.
- [ ] Events carry no semantic fields.
- [ ] The limits of ticket 06 that apply (page size, cursors, minute precision, fixed sort orders) hold for event listings.
- [ ] Hostile-corpus assertion: with corpus strings in event title, description, location, UID, URLs, attachment names, attendee and organizer names and addresses, comments and extension status tokens, no corpus string or fragment appears in any channel.
