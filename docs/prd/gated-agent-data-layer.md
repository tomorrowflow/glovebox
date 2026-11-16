# Gated agent data layer

Vocabulary follows `CONTEXT.md`. Decisions are recorded in `docs/adr/0001` to `0007`; research is in `docs/research/`.

## Problem Statement

The operator runs self-hosted AI agents and wants them to help with mail, calendar and messengers: what is unread, what needs a reply, what has a deadline, when is there free time. Today an agent can only do that by reading the messages themselves. Every message, invite and chat line is text that someone else wrote, and an agent that reads it can be instructed by it. A single hostile mail can turn a helpful agent with other tools into the attacker's agent.

Warnings in prompts, filters and "sanitized" excerpts do not close this path: a string that reaches the model can carry instructions, whatever its length or JSON type. The operator needs a way for agents to work with this data that does not depend on the model resisting what it reads.

## Solution

Glovebox is a gate between the operator's sources and the agents. Agents connect to it as MCP clients and get read-only tools that answer triage and availability questions with permitted fields only: closed-vocabulary enums, bounded numbers and timestamps, booleans, and handles. The text stays behind the gate.

Judgments that need the text (topic, reply-needed, action-requested, deadline) are made inside the gate by a quarantined classifier that has no tools and no credentials. The gate validates its output against the closed vocabulary; anything else becomes `unknown`.

The tagline is "Agents handle your messages without ever touching the text. Read-only MCP servers that keep prompt injection out by construction." That is the goal, never a proven blanket claim. What Glovebox promises is the following contract, stated verbatim from ADR 0001.

### The guarantee

No free-form string authored by a message sender, event organizer or chat participant crosses the gate into the agent's context. Outputs contain only closed-vocabulary enums, bounded parsed numbers and timestamps, booleans, and gateway-generated opaque handles.

### Non-claims

(a) An attacker may influence classifications and values: a hostile message can yield a valid but wrong `urgent: true`. The guarantee is text isolation, not classification correctness.

(b) Enum, count, ordering and timing side channels are bounded, not zero.

(c) The guarantee covers every gate-controlled output channel (structured content, content blocks, errors, metadata, tool descriptions) but not the agent's other tools or data sources.

(d) The client/model adapter must be verified to preserve the boundary.

Do not weaken this contract silently and do not claim more than it, in code, documentation or the README.

## User Stories

### Triage across mail and messengers

1. As an agent, I want to list the recent items of an account as handles with permitted fields, so that I can triage without reading any text.
2. As an agent, I want each item's read status, so that I can tell the operator what is still unread.
3. As an agent, I want each item's age as a minute-precision timestamp, so that I can point at what has waited longest.
4. As an agent, I want to know whether an item has attachments, so that I can flag items that need a closer look by the operator.
5. As an agent, I want each item's sender handle and thread or conversation handle, so that I can group items without learning who the sender is.
6. As an agent, I want to filter items by permitted fields (read status, age range, attachment presence, sender handle, thread handle, semantic fields), so that I can answer "what is unread and needs a reply".
7. As an agent, I want to count items by a permitted field, so that I can report "12 unread, 3 need a reply" without listing them.
8. As an agent, I want to group items by sender handle, thread handle or topic, so that I can show the operator where the volume is.
9. As an agent, I want to sort by a fixed set of sort orders, so that results are predictable and not ranked by a model an attacker can steer.
10. As an agent, I want the same handle for the same sender or thread across calls, so that my grouping is consistent between sessions.
11. As an agent, I want messenger conversations served with the same fields as mail, so that one triage routine covers every source.

### Semantic fields

12. As an agent, I want each item's topic as an operator-configured topic ID, so that I can route items by subject area.
13. As an agent, I want to list the configured topic IDs, so that I know the vocabulary I can filter on.
14. As an agent, I want a reply-needed value of `yes`, `no` or `unknown`, so that I can build a reply queue for the operator.
15. As an agent, I want an action-requested value of `yes`, `no` or `unknown`, so that I can separate requests from information.
16. As an agent, I want a deadline as a timestamp or `unknown`, so that I can order work by due date.
17. As an agent, I want `unknown` whenever the gate cannot produce a valid value, so that I never mistake a failure for a "no".
18. As an operator, I want semantic fields treated as untrusted judgments everywhere, so that no agent or automation takes an action on the strength of a classification alone.

### Calendar availability

19. As an agent, I want the busy and free windows of an account in a time range, so that I can propose meeting times.
20. As an agent, I want to know which events overlap, so that I can warn the operator about conflicts.
21. As an agent, I want the start and end times of upcoming events with their handles, so that I can plan around them without learning what they are about.
22. As an agent, I want availability across several accounts I am authorized for, so that private and work calendars are both respected.

### Text isolation

23. As an operator, I want no sender name, address, subject, body, filename, event title, location or upstream ID to reach an agent, so that an attacker has no free-form channel into the model.
24. As an operator, I want every gate-controlled output channel covered (structured content, content blocks, errors, metadata, tool descriptions), so that text cannot leak around the main result.
25. As an operator, I want the gate to validate a whole response before releasing it, so that a bug in one field cannot let raw text out.
26. As an operator, I want handles generated independently of content, so that a handle itself carries nothing an attacker chose.
27. As an operator, I want handles never dereferenced into an agent's context, so that a handle cannot be turned back into text through the gate.
28. As an operator, I want output limits (page size, capped counts, fixed sort orders, minute precision), so that side channels are bounded even though they are not zero.
29. As an operator, I want the non-claims written down next to the guarantee, so that nobody reads the tagline as proven immunity.
30. As an operator, I want a check of what reaches the model in my agents' client, so that I know the adapter preserves the boundary.

### Read-only

31. As an operator, I want the gate to expose no write operation at all, so that an agent cannot send, draft, mark read, label or accept anything.
32. As an operator, I want each source to document whether its read-only is provider-enforced or gateway-only, so that I know what a compromised gate could do upstream.
33. As an operator, I want provider-enforced read-only credentials wherever the provider offers them, so that the credential itself limits the damage.

### Sources

34. As an operator, I want my mailbox.org mail served through the gate, so that agents can triage my private mail.
35. As an operator, I want my mailbox.org calendar served through the gate, so that agents can see my private availability.
36. As an operator, I want my work Office 365 mail served through the gate, so that agents can triage work mail without it reaching a model as text.
37. As an operator, I want my work Office 365 calendar served through the gate, so that agents can see my work availability.
38. As an operator, I want Signal conversations served through the gate, so that agents can tell me what is waiting there.
39. As an operator, I want iMessage conversations served through the gate, so that agents can tell me what is waiting there.
40. As an operator, I want WhatsApp conversations served through the gate, so that agents can tell me what is waiting there.
41. As an operator, I want a source that is blocked by admin consent or device pairing reported as blocked, so that I am never shown a canned integration as if it were live.
42. As an operator, I want the other sources to ship while one is blocked, so that one employer decision does not hold everything.

### Operator setup and access

43. As an operator, I want to add and pair accounts outside MCP, so that no agent can set up or change a source.
44. As an operator, I want upstream credentials in permission-restricted files that agents cannot read, so that agents never hold them.
45. As an operator, I want to issue each agent its own agent token scoped to specific accounts and read operations, so that an agent sees only what I assigned.
46. As an operator, I want to revoke an agent token, so that a compromised agent loses access immediately.
47. As an operator, I want to configure the topic list, so that classification uses my categories and not ones a model invented.
48. As an operator, I want local inference by default, so that message text does not leave my host.
49. As an operator, I want external inference only where I enabled it for a specific account, so that work mail never goes to a cloud model by accident.
50. As an operator, I want no automatic cloud fallback when local inference is down, so that an outage cannot change where text is sent.
51. As an agent, I want a request for an account outside my token's scope refused with a fixed error, so that the failure tells me nothing about that account.

### Failure behavior

52. As an agent, I want a provider outage reported as a fixed error enum with an explicit stale or incomplete status, so that I never report an empty inbox that is really an outage.
53. As an agent, I want each account's sync status and last successful sync time, so that I can tell the operator how fresh my answer is.
54. As an operator, I want unparseable source items quarantined or skipped with a bounded status code, so that one malformed message cannot break or leak through a response.
55. As an operator, I want upstream error text never echoed, so that an attacker cannot use an error path as a text channel.
56. As an operator, I want malformed classifier output turned into `unknown`, so that a steered classifier cannot push text out.

### Retention and logging

57. As an operator, I want raw text discarded after processing, so that Glovebox does not become a second archive of my messages.
58. As an operator, I want classifier inputs and raw classifier outputs kept out of logs, so that logs are not a side door to the text.
59. As an operator, I want any history a messenger bridge keeps documented and unreachable for agents, so that I know where message text lives on my host.

### Environments and delivery

60. As a developer agent, I want a throwaway stack with fixture sources and a stub classifier, so that I can prove a ticket without any real account.
61. As an operator, I want a long-lived test environment that runs only `origin/main` with fictional data, so that I can see the current state before promoting it.
62. As an operator, I want production to be touched only by me, following a runbook, so that no ticket or agent can reach my real data.

## Implementation Decisions

### Shape

- One Python service, the gate, built on the official MCP Python SDK, with SQLite for handles, classifications and sync state (ADR 0005). Agents connect over authenticated Streamable HTTP.
- Modules, each behind a small interface:
  - **Source adapters**, one per source. An adapter fetches items for an account within the retrieval window and hands the gate a normalized internal record. It is the only module that talks to a provider or bridge and the only one that sees raw upstream structure.
  - **Handle registry.** Maps upstream entities to handles and back, per account.
  - **Quarantined classifier.** Takes the text of one item and the operator's topic list; returns a proposal for the semantic fields. Has a stub implementation for tests and stand-ins.
  - **Output validator.** The single exit: checks an entire response against the permitted-field rules before release.
  - **Query layer.** Listing, filtering, grouping, counting and sorting over persisted permitted fields.
  - **MCP tool surface.** Static tool definitions over the query layer.
  - **Operator configuration.** Accounts, credentials files, topic list, inference settings, agent tokens. A local command-line interface on the gateway host; never reachable through MCP.
- Device-dependent bridges (for example iMessage) may run as separate processes where the source requires it. Connector libraries and bridges are existing maintained ones; each source ticket checks what exists before building anything.

### Permitted fields

- Every value in every response is one of: an enum from a closed vocabulary fixed in code or in operator configuration; a number within stated bounds; a timestamp normalized to minute precision; a boolean; a handle.
- No field has an unconstrained string type. Response objects are closed (no additional properties). Enumerations that mirror an upstream vocabulary with extension tokens (MIME types, participation status) are mapped to a local closed vocabulary; anything unmapped becomes `unknown`.
- Never exposed, in any form: sender or participant names and addresses, subjects, bodies, previews, filenames, MIME strings, event titles, descriptions, locations, URLs, group or room names, reaction strings, upstream IDs (including Message-ID, UID, Graph or bridge IDs), raw header or timestamp strings, upstream error text.
- Topic IDs are operator-chosen identifiers restricted to a short fixed character pattern. They are operator-authored, not attacker-authored. Topic names and descriptions used to brief the classifier stay in operator configuration and are not served.

### Handles

- Generated by the gate from randomness, not derived from content or from upstream IDs. Fixed length and character set.
- Stable per account: the same upstream entity keeps its handle across calls and sessions. The same person in two accounts has two unrelated handles.
- Kinds: account, item, thread or conversation, sender, event.
- Scoped to authorization: a handle presented by an agent whose token does not cover the handle's account is answered exactly like a handle that does not exist.
- Operations on handles: listing, filtering, grouping, counting, sorting on permitted fields. Nothing dereferences a handle into text, and nothing mutates.

### Tool surface

- All tools are read-only. There is no tool, resource, prompt or other MCP capability that writes upstream or returns text.
- Tool names, descriptions and schemas are static constants. They never include account names, topic names or anything derived from source data.
- Each tool declares an output schema and returns structured content with an empty content array. This is a considered exception to the MCP recommendation to also return a serialized text copy (`docs/research/01-mcp-structured-output.md`); if a target client needs the copy, it may contain only the same validated permitted fields.
- Inputs are constrained the same way as outputs: enums, handles, timestamps, booleans, bounded numbers and opaque page cursors. There is no free-text query parameter and no arbitrary semantic query.
- Capabilities the surface covers:
  - accounts the token may read, each with source kind, sync status, last successful sync time and read-only enforcement kind;
  - the configured topic IDs;
  - listing mail and messenger items with filters, fixed sort orders and pagination;
  - counting and grouping items by a permitted field;
  - calendar busy and free windows for a time range, overlapping events, and upcoming events with start and end times.
- Item fields: item handle, account handle, source kind, sender handle, thread or conversation handle, received time, read status, attachment presence, topic, reply-needed, action-requested, deadline, classification status.
- Event fields: event handle, account handle, start, end, all-day, busy status. Calendar events carry no semantic fields in the MVP: the settled calendar scope is availability, overlaps and event times.
- Messenger items use the same fields as mail and the same 30-day window. Where a source has no read status, the field is `unknown`.

### Limits

- Mail and messenger ingestion: rolling 30-day window. Calendar: 90-day look-ahead. No backfill beyond that.
- At most 50 items per response, with opaque pagination cursors.
- Fixed sort orders only; no classifier-generated ranking.
- Counts capped: anything above 1000 is reported as `1000+`.
- Timestamps at minute precision.
- These bound leakage through counts, ordering and timing. They do not eliminate it.

### Quarantined classifier

- Runs inside the gate with no tools and no upstream credentials. Its only input is one item's text plus the operator's topic list; its only accepted output is a proposal for topic, reply-needed, action-requested and deadline.
- Server code validates each proposed value independently: topic must be a configured topic ID or `other`; reply-needed and action-requested must be `yes` or `no`; deadline must be a valid timestamp within the configured date range. Any value that fails becomes `unknown`. No explanation, confidence text or other extra output is kept.
- Results are cached with the item until its content or the operator configuration (topic list, inference settings) changes. Reclassification re-fetches the text from the source.
- Local inference by default, through an existing local runtime. External inference only for accounts where the operator enabled it explicitly; no automatic fallback in either direction. If inference is unavailable, semantic fields are `unknown` with a classification status that says so.
- Classifier inputs and raw outputs are never logged.
- Local inference has resource costs on the gateway host. The spec does not prescribe hardware.

### Authorization and credentials

- Upstream credentials: permission-restricted files outside agent access, no application-level encryption or key management (ADR 0004). Agents never receive them.
- Agent tokens: issued and revoked by the operator, each scoped to a set of accounts and to read operations. Stored so that a leaked database does not reveal usable tokens. Presented as a bearer credential on the Streamable HTTP transport.
- Requests without a valid token are rejected before any tool runs.
- Account setup, pairing and all configuration are operator-only and outside MCP.

### Read-only enforcement

- The gate has no code path that writes upstream. Adapters use the least-privileged access the provider offers and non-mutating read operations (for IMAP, reads that do not set the seen flag).
- Each adapter records its read-only enforcement kind, provider-enforced or gateway-only, and the gate reports it per account. Expected from research: Office 365 and CalDAV can be provider-enforced; IMAP and the messenger bridges are gateway-only. Each source ticket confirms this for the real setup.

### Failure behavior

- Malformed classifier output: `unknown`.
- Unparseable source items: quarantined or skipped, visible only as a bounded status code and count.
- Provider or bridge outage: a fixed error enum, and affected accounts carry an explicit `stale` or `incomplete` status with the last successful sync time. A response is never an apparently empty result when the truth is "could not fetch".
- A source blocked by access requirements reports the fixed status `blocked`.
- The output validator checks the entire response; on failure the gate returns a fixed error enum and releases nothing of the failed response.
- Error results use a fixed vocabulary of server constants. Upstream error text, exception messages and message text are never echoed in any channel.

### Retention

- Raw text exists in the gate only while an item is processed. Persisted: permitted fields, handle mappings, sync state, configuration.
- No raw-message archive. History that a required messenger bridge keeps is documented per source and unreachable for agents.

### Environments

- As ADR 0007: throwaway, `glovebox-test` and production tiers; one compose file and `scripts/env.py`; test port 8765; `GLOVEBOX_TEST_URL` and `GLOVEBOX_TEST_TOKEN`; fictional fixture cast; a deterministic stand-in for every outside service including local inference.
- The test environment runs on the 790, under the dedicated `glovebox-test` Linux identity and OpenClaw node.
- Open decision carried from the ADR: the production host, needed before the production installation.

## Testing Decisions

- **One seam: the MCP tool-call boundary** (ADR 0006). Acceptance tests connect as an MCP client with an agent token and call tools, with deterministic source fixtures and a stub classifier behind the gate. A good test asserts only what an agent can observe: the tools offered, and every channel of every result. It does not reach into the database, the adapters or the classifier.
- What is tested at the seam:
  - **Hostile-string corpus.** A corpus of marker strings and injection texts is placed in every attacker-writable upstream field of the fixtures (names, addresses, subjects, bodies, filenames, MIME strings, upstream IDs, event text, group names, reactions). No corpus string or fragment appears in any channel of any response, including errors, metadata and tool listings.
  - **Schema closure.** Every tool has an output schema with no unconstrained string and no open object; every response conforms.
  - **Malformed classifier output.** The stub returns prose, extra fields, out-of-vocabulary values, out-of-range dates and corpus strings; the affected fields are `unknown` and nothing else changes.
  - **Sanitized errors.** Fixture outages and upstream errors carrying corpus strings produce only fixed error enums and stale or incomplete status; never an empty success.
  - **Account isolation.** A token scoped to one account cannot list, count or probe another; foreign handles behave like nonexistent ones.
  - **Limits.** Page size, opaque cursors, capped counts, fixed sort orders, minute precision, retrieval windows.
  - **No write tools.** The tool listing contains only the read tools; no resources or prompts are offered.
  - **Handles.** Stable across calls, different across accounts, unrelated to upstream IDs.
- **Client-adapter check.** One small check per target client records what the client hands to the model for a gate tool call and asserts it contains only permitted fields. The first target is the client the operator's agents run on. Passing for one client says nothing about another.
- **Source adapters.** Each adapter is tested through the same seam against its deterministic stand-in. The live check against the real account cannot run on a factory node:
  - `Operator-run:` for each source, at merge of its ticket, the operator sets up the real account, confirms that live items appear with handles and permitted fields, and confirms the documented read-only enforcement kind. A source that cannot be set up is recorded as `blocked` with the reason, not as delivered.
  - `Operator-run:` for one mail source, before the MVP is called complete, the operator interrupts the provider connection and confirms that agents see the fixed outage error with stale status, and normal results after recovery.
  - `Operator-run:` the operator confirms on the gateway host that credential files are unreadable for the agents' Linux users.
- **Classification quality** is tested separately from text isolation, against a labelled set of fictional messages with the real local model. It is not part of the acceptance seam and never gates the isolation tests. Live source coverage comes before quality work.
- **What the tests do not show.** Neither schema validation nor the hostile corpus proves blanket immunity. The tests demonstrate the contract above; they do not extend it.
- **Prior art.** None in this repository; it holds only documentation so far. The first acceptance test in the scaffold and tracer-bullet tickets sets the pattern for the rest.

## Out of Scope

- Any upstream mutation: sending, drafting, marking read, labels, invite responses, deletions.
- Any text crossing the gate: summaries, quotations, excerpts, generated topic names, explanations, sender names or addresses, however shortened or sanitized.
- Agent-created categories and arbitrary semantic queries.
- Gmail and any source not listed.
- Attachment contents, OCR, voice transcription.
- Historical backfill beyond the retrieval windows.
- Application-level credential encryption, key management or a secrets manager.
- A raw-message archive in Glovebox.
- Automatic cloud fallback for inference.
- Defending the agent's other tools or data sources, and proving classification correctness.
- Showing message text to the operator. Glovebox tells agents which items matter; reading them stays with the operator's own mail and messenger clients.
- Microservices, message brokers, custom model serving.

## Further Notes

### Open risks

- Wrong-but-valid classifications remain possible; injection can steer the quarantined classifier within the allowed vocabulary.
- Side channels (counts, ordering, timing, error shapes) are bounded, not eliminated.
- MCP clients only SHOULD validate, and a client adapter may serialize structured content into model context. The client-adapter check mitigates this, but each target client needs its own verification.
- Office 365 may need employer or admin consent. Signal, iMessage and WhatsApp access depends on devices or bridges; iMessage depends on the Apple-device setup. Any of these can block a source indefinitely.
- Gateway-only read-only sources hold credentials that could write. A compromised gate, not a compromised agent, is the threat there.
- Local inference has resource costs that are not yet measured.

### Open decisions

- The production host (ADR 0007). The test host is settled: the 790.

### Interpretations made while writing this spec

These follow from the settled decisions but were not decided word for word; they can be changed without touching the guarantee.

- Messenger items use the same 30-day window as mail.
- Calendar events carry no semantic fields in the MVP.
- Topic IDs are operator-chosen identifiers in a restricted pattern, and topic names are not served.
- The grilling result counts "six sources"; the list it gives has seven feeds (mailbox.org mail and calendar, Office 365 mail and calendar, Signal, iMessage, WhatsApp). This spec follows the list.

### Order of work

Source coverage first, classification quality second, text isolation never relaxed for either. The MVP is complete when every listed source is either delivered live or recorded as blocked with its reason.
