# 11: Messenger items

**Track:** messenger

**What to build:** Messenger conversations are served with the same item fields and the same tools as mail, so one triage routine covers every source. An agent sees message items with conversation and sender handles; bodies, captions, names, group names, reactions and filenames never cross the gate. Built against a fixture messenger source that the three live messenger tickets reuse as their pattern.

**Blocked by:** 01, 02, 05, 06, 09

**Status:** ready-for-agent

- [ ] Messenger items carry the same fields as mail items, with a conversation handle in place of the thread handle and a source kind that names the messenger.
- [ ] Group and direct conversations are both served; sender handles are stable per account, and the same person in two accounts has unrelated handles.
- [ ] Messenger items carry the semantic fields of ticket 09, filled by the stub classifier and validated the same way as for mail.
- [ ] Where a messenger has no read status, the field is `unknown`.
- [ ] Messages outside the rolling 30-day window are neither ingested nor served.
- [ ] Item listing, filtering, grouping, counting and paging work across mail and messenger accounts in one call when the token covers both.
- [ ] Media messages report attachment presence only; media types outside the local vocabulary become `unknown`.
- [ ] Hostile-corpus assertion: with corpus strings in bodies, captions, sender and profile names, group names and topics, quotes, link previews, reaction strings, filenames, MIME strings and upstream IDs, no corpus string or fragment appears in any channel.
