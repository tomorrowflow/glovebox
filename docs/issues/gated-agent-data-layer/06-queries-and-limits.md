# 06: Queries and output limits

**Track:** gate

**What to build:** An agent can filter, group, count and sort items on permitted fields and page through results, within the limits that bound side channels (ADR 0001). It can answer "how many unread items are there per thread" or "list the oldest unread items with attachments" without any text crossing the gate.

**Blocked by:** 01, 02, 05

**Status:** ready-for-agent

- [ ] Items can be filtered by read status, received-time range, attachment presence, sender handle, thread handle and account handle.
- [ ] Items can be counted, and grouped with counts by sender handle, thread handle and each enum field.
- [ ] Sorting offers a fixed set of sort orders over permitted fields; there is no relevance or model-generated ranking.
- [ ] A response holds at most 50 items; further pages are reached with an opaque cursor that reveals nothing about content and is invalid for another token.
- [ ] Counts above 1000 are reported as `1000+`.
- [ ] All timestamps are at minute precision.
- [ ] Mail outside the rolling 30-day window is neither ingested nor served.
- [ ] Tool inputs accept only enums, handles, timestamps, booleans, bounded numbers and cursors; there is no free-text or semantic query parameter, and invalid input yields a fixed error enum.
- [ ] The hostile-corpus assertion of ticket 05 holds for every new tool and every error path.
