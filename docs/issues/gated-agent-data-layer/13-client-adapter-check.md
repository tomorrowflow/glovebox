# 13: Client-adapter check

**Track:** adapter-check

**What to build:** A check that shows what actually reaches model context when the operator's agent client calls a gate tool. MCP clients only SHOULD validate, and an adapter may serialize structured content into the model's context, so the boundary has to be verified at the client too (ADR 0001 non-claim d, ADR 0006). The check runs against the long-lived test environment with its hostile fixture cast.

**Blocked by:** 01, 02, 03, 05

**Status:** ready-for-agent

- [ ] The target client is named: the client the operator's agents run on. The check documents its version and how it presents tool results to the model.
- [ ] The check calls gate tools through that client against `GLOVEBOX_TEST_URL` with `GLOVEBOX_TEST_TOKEN` and captures what the client hands to the model for the tool listing, a successful result and an error result.
- [ ] The captured model input contains only permitted fields and static tool text; no hostile-corpus string or fragment from the fixture cast appears in it.
- [ ] If the client needs a serialized text copy of the structured content to work, the gate provides one that contains only the same validated permitted fields, and the acceptance tests of ticket 05 cover that copy.
- [ ] The check can be rerun with one documented command and fails when a corpus string reaches model context.
- [ ] The documentation states that the result holds for this client and version only, and that every further target client needs its own check.
