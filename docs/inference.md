# Inference for the quarantined classifier

The quarantined classifier (ADR 0002) asks a model for four things about one item: topic, reply-needed, action-requested and deadline. The gate does it with one chat completion on an OpenAI-compatible server. By default that server is a local runtime on the gateway host. The operator can name accounts whose items go to an external provider instead. Nothing else ever sends an item's text off the host. Ticket 12; code in `src/glovebox/inference.py` and `src/glovebox/classifier_config.py`.

**Not yet run on the real model.** The tests use the canned inference stand-ins, which answer the same completion every time and say nothing about classification quality or resource use. The operator's acceptance run is below, and until it passes the production classifier stays unconfigured.

## The local runtime, and why

The gate speaks `POST /v1/chat/completions` and nothing runtime-specific, so any maintained server with that endpoint works, and the choice is the operator's. Documented default: **Ollama**.

| Option | Verdict |
|---|---|
| **Ollama** (default) | Maintained: stable v0.40.17 (10 Nov 2026), v0.40.16 (9 Nov) and v0.40.15 (3 Nov) in the [release feed](https://github.com/ollama/ollama/releases), checked 23 Nov 2026. The native macOS build supports Apple M-series CPU and GPU on macOS Sonoma 14 or later, with Metal acceleration ([macOS](https://docs.ollama.com/macos), [GPU](https://docs.ollama.com/gpu)). It pulls and stores models itself and binds `127.0.0.1:11434` by default. It documents the OpenAI-compatible `POST /v1/chat/completions` (including `response_format`, `temperature` and `max_tokens`) and `GET /v1/models` ([compatibility](https://docs.ollama.com/api/openai-compatibility)). `llama3.1:8b` is a downloadable local model, a Q4_K_M artifact of 4.9 GB ([library](https://ollama.com/library/llama3.1:8b)). |
| `llama-server` from llama.cpp | The engine under several runtimes; the same endpoint. More to configure by hand (model files, flags) for the same result. A fine choice if the operator wants it: set `inference.local.url` and `model`. |
| LM Studio, MLX-based servers | Also speak the endpoint on macOS. GUI-first or younger as services; not documented further here. |
| vLLM and similar | Built for GPU servers; no fit for the Mac mini. |

Only Ollama's maintenance state and platform support were checked (research of 23 Nov 2026, from the sources linked above); the other rows are not re-checked. That is documented support, not an acceptance run on the gateway: API support establishes nothing about the quality of the four fields, JSON validity, usable context, memory or latency, which the operator measures below before production uses the classifier. Re-check the releases when upgrading.

## Configuration

`GLOVEBOX_CLASSIFIER_CONFIG` names a JSON file (see `AGENTS.md`). Its `inference` section, all of it optional:

```json
{
  "topics": [{"id": "billing", "name": "Invoices", "description": "Money owed or paid."}],
  "inference": {
    "profile": "2026-11",
    "local": {"url": "http://127.0.0.1:11434", "model": "llama3.1:8b"},
    "external": {
      "url": "https://inference.example.test",
      "model": "provider-model",
      "api_key_file": "/Users/frogger/.config/glovebox/provider.key",
      "accounts": ["work"]
    }
  }
}
```

- **No `inference` section: local inference** at the defaults above (`DEFAULT_LOCAL_URL`, `DEFAULT_LOCAL_MODEL`). Without a configuration file at all there is no classifier and every classification is `unavailable`.
- `local.url` must be on this host (`localhost` or a loopback address). The gate refuses anything else, so "local" cannot quietly mean another machine. The URL is the server's base: the gate adds `/v1/chat/completions` after any path prefix.
- `external` is used only for the accounts listed in `external.accounts`, by the operator's account name. It needs `model` and a `url` that is `https` (or loopback). `api_key_file` is optional: a file only the gateway's user may read (mode `0600`, as for credentials, ADR 0004), whose contents are sent as the bearer token. It goes to the external provider and nowhere else. A listed account that does not exist does nothing; every unlisted account stays local. There is no MCP tool, token operation or account setting that can change any of this.
- `backend: "stub"` is the deterministic test stand-in for a model; with it, `local` and `external` are refused.
- **No fallback in either direction.** An account has exactly one backend. When it is down, slow, answers with an error or with something that is not a chat completion, that item's fields are `unknown` with status `unavailable`, and nothing is sent to the other backend. Unavailable results are not cached, so the item is tried again on the next read.
- **Changing an account's inference settings makes its stored classifications stale**: its route (`local`, `external`), the `profile` label, and the address and model of the backend it uses. Another account's settings are not part of it, so moving `work` to external inference re-classifies `work` only. Use `profile` to force every account to re-classify when something changes that the gate cannot see (a model pulled again under the same name).
- The throwaway and test stacks set `GLOVEBOX_STANDIN_INFERENCE_LOCAL` and `GLOVEBOX_STANDIN_INFERENCE_EXTERNAL` to the stand-ins' addresses; when set, they replace the `url` of `local` and `external`. A real deployment leaves them unset: an address from the environment is not held to the rules of one in the file, so a non-loopback value for the local variable would send text off the host as "local", and an `http` one for the external variable would send its key in clear.
- **A loopback address is not proof of local execution.** Ollama can forward the prompts of a cloud model through its local API, and the computation then happens on Ollama's servers. The gate refuses an `inference.local.model` ending in `cloud` (`name:cloud`, `name-cloud`), but it checks names, not what the runtime does with them. So: use the exact local tag (`llama3.1:8b`), disable the feature in the runtime's own service environment (`OLLAMA_NO_CLOUD=1`, or `disable_ollama_cloud: true` in its server config; both take effect after a restart; see the [cloud](https://docs.ollama.com/cloud) and [FAQ](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features) pages), and in the acceptance run check where the model is loaded (`ollama ps`) and that nothing left the host.

## What the model call carries

One system message (the instructions and the topic list, names and descriptions) and one user message (the item's text, cut at 16,000 characters). No tool definitions, no source credential, no other item. The request asks for a JSON object, temperature 0 and at most 256 reply tokens. It goes with no proxy and follows no redirect, whatever the environment says. The reply text is passed to the validation of `classification.py` exactly as it came, with no repair: prose, a fenced block or extra fields are `invalid` or `unknown`. A call has one deadline of 60 seconds in all, from connecting to the last byte of the reply: a server that answers slowly or sends a byte now and then is cut off at it, and the item is `unavailable`. (A reply whose headers trickle in is bounded by the time left at each wait, not as a whole.)

The model sees the item's text, so a model that reads it can be steered by it. That is the guarantee's second line, not its first: only the four validated fields leave the gate (ADR 0001, 0002).

**Logs.** The request, the reply and any exception message are never logged, at any level. A failure is logged as the backend (`local` or `external`) and an HTTP status or the exception type, such as `local inference failed: URLError`. The gate uses no HTTP library that logs requests.

## Resource needs

Not prescribed, and not measured here. What to expect: the model's weights are held in memory while the runtime has it loaded (the 4.9 GB of `llama3.1:8b` is the size of the artifact to download, not the memory it needs: the context cache and the runtime come on top), and each item is one prompt of up to roughly 4,000 tokens plus a short reply, so latency is a few seconds per item on Apple Silicon, one after another. A first read of an account with a full 30-day window classifies each item once; after that only new and changed items. These are estimates, not measurements: take real numbers in the acceptance run and note them here.

The gate classifies while it answers a read, one item at a time. A runtime that hangs makes a read wait up to the deadline per item; a runtime that is simply not running fails at once.

## Operator: acceptance on the gateway host

At merge, on the Mac mini, with fictional messages only:

1. Install the runtime and pull the exact local tag: `ollama pull llama3.1:8b` (or the model chosen in `inference.local.model`; never a cloud tag). Set `OLLAMA_NO_CLOUD=1` in the runtime's service environment and restart it. Check `curl -s http://127.0.0.1:11434/v1/models` lists the model. The runtime must listen on loopback only (its default).
2. Write the production `classifier.json` (the topic list is the operator's call, not this repository's), set `GLOVEBOX_CLASSIFIER_CONFIG` in `~/.config/glovebox/gate.env` and redeploy as in the runbook of `docs/environments.md`.
3. Add a fixture-backed account with a handful of fictional messages (a question, a request with a deadline, an invoice, an announcement) and list its items. The semantic fields must be filled with sensible values and the status `classified`. Measure what the API's support does not establish: whether the four fields are right, whether the replies are valid JSON (a share of `invalid` or `partial` items is a finding), whether the runtime's context window holds the instructions, the topic list, the longest item (16,000 characters) and the reply (a context window that is too small silently drops the start of the prompt: check the runtime's setting, for Ollama `OLLAMA_CONTEXT_LENGTH`), the memory in use and the time per item.
4. Confirm where it ran and that no request left the host: `ollama ps` must show the model loaded on this host, and a packet capture or the macOS firewall's connection log for the gate's and the runtime's processes during step 3 must show connections to loopback only.
5. Confirm the outage with **fresh** items: the classifications of step 3 are cached, so a stopped runtime would not show on them. Stop the runtime, then add or receive new fictional messages (or change `inference.profile`, which makes every stored classification stale), list the items, and expect `unavailable` with the gate opening no other connection. Start the runtime again and expect the next read to fill the fields.
6. Write the measurements from step 3 into the resource section above, and enable the classifier for production only if they hold.
