# 12: Inference backends for the quarantined classifier

**Track:** classifier

**What to build:** The quarantined classifier runs on a real model. By default that is local inference through an existing local runtime on the gateway host; the operator can enable external inference for a specific account, and nothing else ever sends text off the host (ADR 0002). Throwaway and test keep using the deterministic stand-in.

**Blocked by:** 01, 02, 05, 09

**Status:** ready-for-agent

- [ ] With no inference configuration, classification uses local inference.
- [ ] External inference is used only for accounts where the operator enabled it explicitly; it is configured per account, outside MCP.
- [ ] No automatic fallback in either direction: when the configured backend for an account is unavailable, that account's semantic fields are `unknown` with the matching classification status, and no text is sent to the other backend.
- [ ] The model call has no tools, no credentials and no access to other items; its reply passes through the validation of ticket 09 unchanged.
- [ ] Classifier inputs and raw outputs are absent from logs at every log level, including on backend errors and timeouts.
- [ ] Changing an account's inference settings invalidates its cached classifications.
- [ ] Tests run against the canned inference stand-ins of ticket 02 and cover: local default, external opt-in for one account only, backend outage without fallback, and a stand-in reply containing hostile-corpus strings.
- [ ] The local runtime is an existing maintained one, chosen after checking what exists; its resource needs are noted in the documentation without prescribing hardware.
- [ ] Operator-run: at merge, the operator classifies a handful of fictional messages with the real local model on the gateway host and confirms that semantic fields are filled and that no request left the host.
