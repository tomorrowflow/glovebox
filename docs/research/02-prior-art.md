# Research 2: Prior art on keeping untrusted text from the privileged model

Source: Fróði, answered on the card on 2026-11-08. Confidence: high on the cited designs and their limits; moderate on completeness of the prior-art survey.

## Finding

Published isolation designs support a narrower claim than "prompt injection out by construction": keep attacker-controlled strings out of the privileged planner **and** enforce what derived values may influence. Classification correctness and the absence of all information channels are not established by JSON typing. No verified, evaluated mail or messenger MCP gateway with the exact proposed guarantee was found.

## Works

1. Simon Willison, "The Dual LLM pattern for building AI assistants that can resist prompt injection" (2023). The privileged planner sees variable names, never email text or quarantined summaries; a conventional controller stores and substitutes values and renders to the user. The quarantined LLM has no tools. A design proposal, not evaluated proof. It warns about malicious links and images and about user copy-paste and social engineering; his 2025 update acknowledges CaMeL's correction. https://simonwillison.net/2023/Apr/25/dual-llm-pattern/
2. "Defeating Prompt Injections by Design" (CaMeL, 2025), especially sections 3.1, 5-7 and Appendix I. Trusted-query code generation, quarantined extraction, and interpreter-enforced provenance and capability policies. Reports 77% utility versus 84% undefended, and resistance to the evaluated AgentDojo attacks; these are benchmark results, not universal immunity. Dual-LLM alone permits poisoned recipient or document parameters. STRICT mode tracks implicit dependencies; NORMAL mode permits conditional-fetch and count leakage. It demonstrates one-bit leakage through exceptions and halted execution (the quarantined-LLM case is mitigated by STRICT); timing channels remain possible. Wrong summaries and text-to-text manipulation are explicit non-goals. https://arxiv.org/html/2503.18813v2
3. "Design Patterns for Securing LLM Agents against Prompt Injections" (2025), sections 3, 4.1, 4.3, 4.10. Typed map/reduce (one document to a Boolean or fixed category), symbolic Dual-LLM values, code-then-execute; includes an email and calendar assistant case study. A malicious file can still induce a wrong classification; opaque draft replies can still contain exfiltration or injections aimed at recipients. Strict API formatting and short names reduce the likelihood but are not a proof. Qualitative patterns, not a comprehensive attack evaluation. https://arxiv.org/html/2506.08837v2
4. "Securing AI Agents with Information-Flow Control" (Fides, 2025). Hidden variables with selective reveal and deterministic integrity and confidentiality checks; formal integrity noninterference, but a deliberately weaker confidentiality ("explicit secrecy"): observable action choices may leak information. Reports blocking all tested AgentDojo injections. Not an architecture where the model never sees untrusted text: a reveal taints its context and constrains later actions. https://arxiv.org/html/2505.23643
5. "Agent Security with FIDES" (Microsoft implementation docs). Practical gateway and middleware prior art, not a mail-only gateway. Experimental Python integration; tool labels and complete interception matter. CodeAct nested host-tool calls bypass its middleware and are unsupported. No independent product-wide immunity result. https://learn.microsoft.com/en-us/agent-framework/agents/security
6. "AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents" (2024). A reproducible email, calendar and Slack task and adversarial-tool-result evaluation underlying CaMeL and Fides; it is not itself a protective gateway. https://arxiv.org/abs/2406.13352

## Boundary implications (Fróði's analysis, not measured bypasses)

- Sender display names, subjects, attachment names, previews, error strings and handle labels remain attacker text unless withheld or strictly mapped.
- Fixed enums bound bandwidth, but attackers can choose classifications; sequences, list length and order, counts and errors can carry signals.
- Opaque references prevent content exposure only if they are generated independently of content and never dereferenced into planner context; authorization must still gate their use.
- No reviewed source proves zero enum or length channels, or exhaustively covers these metadata fields.
