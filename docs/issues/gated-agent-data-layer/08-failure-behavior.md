# 08: Failure behavior and sync status

**Track:** gate

**What to build:** When a source is down, blocked or delivers something unparseable, an agent is told so in fixed terms and never mistakes it for an empty inbox. Each account reports its sync status and last successful sync time, and no failure path carries upstream text (ADR 0001, PRD "Failure behavior").

**Blocked by:** 01, 02, 05

**Status:** ready-for-agent

- [ ] Each account reports a sync status from a closed vocabulary that includes at least current, stale, incomplete and blocked, plus the last successful sync time at minute precision.
- [ ] When the fixture source is made unavailable, item and count tools return a fixed error enum or results marked stale or incomplete; never an apparently complete empty result.
- [ ] After the fixture source recovers, status returns to current without operator action.
- [ ] An unparseable fixture item is quarantined or skipped; it is visible only as a bounded status code and a capped count, and the rest of the response is unaffected.
- [ ] A source that cannot be reached because access is not granted reports `blocked`.
- [ ] Errors come from a fixed vocabulary of server constants. With corpus strings in upstream error messages, exception text and the unparseable item, no corpus string or fragment appears in any channel, and none is written to logs.
- [ ] A response that fails output validation yields the fixed error enum and nothing of the failed response.
