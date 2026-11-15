# 21: Classification quality

**Track:** classifier

**What to build:** A repeatable measurement of how well the quarantined classifier fills the semantic fields, and the prompt and configuration work to bring it to a level the operator accepts. This is worked last: live source coverage comes before classification quality (ADR 0003). It is separate from text isolation and never changes or gates the isolation tests (ADR 0006).

**Blocked by:** 01, 02, 05, 09, 12

**Status:** ready-for-agent

- [ ] A labelled evaluation set of fictional messages exists, covering mail and messenger styles, several languages the operator uses, each topic, each value of reply-needed and action-requested, and explicit, relative and absent deadlines. It contains no real, pseudonymised or production-copied data.
- [ ] The set includes messages that try to steer the classifier (instructions to mark themselves urgent, to pick a topic, to output text). They are scored for classification only; text isolation for them is already covered at the acceptance seam.
- [ ] One documented command runs the set against the real local model and reports per-field agreement and the share of `unknown`, as numbers only.
- [ ] The evaluation is not part of the acceptance tests or CI gate; throwaway and test environments keep the inference stand-in.
- [ ] The results, the model used and its observed resource use on the gateway host are recorded in the documentation, without prescribing hardware.
- [ ] The documentation repeats that a wrong-but-valid classification remains possible and that semantic fields never authorize an action (ADR 0001 non-claim a).
- [ ] Operator-run: before this ticket closes, the operator runs the evaluation on the gateway host with the real local model and accepts the reported quality or names the fields that need more work.
