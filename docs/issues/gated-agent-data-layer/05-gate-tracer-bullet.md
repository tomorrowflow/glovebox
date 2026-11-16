# 05: Gate tracer bullet: list fixture mail items as permitted fields

**Track:** gate

**What to build:** The first complete path through the gate. An agent with a valid agent token connects over Streamable HTTP, calls one read tool, and gets the recent mail items of a fixture account as handles and deterministic permitted fields: item handle, account handle, source kind, sender handle, thread handle, received time at minute precision, read status, attachment presence. Nothing else crosses. The fixture mail source, the handle registry, persistence in SQLite and the output validator exist in their first form; a single static token from configuration is enough for now (ticket 07 replaces it).

This ticket sets the pattern for every later acceptance test: tests drive MCP tool calls and inspect every channel of the result (ADR 0006).

**Blocked by:** 01, 02

**Status:** ready-for-agent

- [ ] A request without a valid token is rejected before any tool runs.
- [ ] The tool declares an output schema with no unconstrained string and no open object, and returns structured content with an empty content array.
- [ ] Every response passes through one output validator that checks the entire response; a response that fails is replaced by a fixed error enum and nothing of it is released.
- [ ] Handles are generated from randomness, have a fixed length and character set, are stable across calls and service restarts, and are unrelated to upstream IDs and content.
- [ ] Hostile-string corpus: with corpus strings in every attacker-writable field of the fixture mail (names, addresses, subject, body, filenames, MIME strings, Message-ID and other upstream IDs, raw date strings), no corpus string or fragment appears in any channel of any response: structured content, content blocks, errors, metadata, tool names, descriptions and schemas.
- [ ] The corpus and the "no fragment in any channel" assertion are reusable test helpers that later tickets extend with their fields.
- [ ] The tool listing contains only the read tool; no resources, prompts or write tools are offered.
- [ ] Raw text is not persisted: after ingestion the database holds only permitted fields, handle mappings and sync state.
- [ ] The throwaway stack from ticket 02 serves the tool against the fixture cast.
