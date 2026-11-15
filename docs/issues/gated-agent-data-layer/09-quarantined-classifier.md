# 09: Quarantined classifier and semantic fields

**Track:** classifier

**What to build:** Items carry the four semantic fields: topic, reply-needed, action-requested and deadline. The operator configures the topic list; a quarantined classifier inside the gate proposes values from an item's text, and the gate validates each one against the closed vocabulary, turning anything invalid into `unknown` (ADR 0002). An agent can list the topic IDs and filter, group and count on the semantic fields. This ticket uses the stub classifier only; real inference is ticket 12.

**Blocked by:** 01, 02, 05

**Status:** ready-for-agent

- [ ] The operator can configure the topic list outside MCP. Topic IDs match a short restricted pattern; topic names and descriptions stay in operator configuration and are never served.
- [ ] An agent can list the configured topic IDs.
- [ ] Each item carries topic (a configured topic ID, `other` or `unknown`), reply-needed and action-requested (`yes`, `no`, `unknown`), deadline (a minute-precision timestamp within the configured date range, or `unknown`) and a classification status from a closed vocabulary.
- [ ] The classifier interface receives one item's text and the topic list and nothing else: no tools, no upstream credentials, no handles of other items.
- [ ] Malformed classifier output: when the stub returns prose, extra fields, explanations, unconfigured topics, out-of-range or unparseable dates, or hostile-corpus strings, each affected field is `unknown`, valid fields in the same result are kept, and nothing of the raw output crosses the gate or is persisted.
- [ ] When the classifier is unavailable, semantic fields are `unknown` and the classification status says so; deterministic fields are still served.
- [ ] Classifications are cached and reused until the item's content, the topic list or the inference settings change; then the item is re-fetched from the source and reclassified.
- [ ] Items can be filtered, grouped and counted by each semantic field.
- [ ] Classifier inputs and raw outputs do not appear in logs.
- [ ] The hostile-corpus assertion holds with corpus strings in item text and in classifier output.
