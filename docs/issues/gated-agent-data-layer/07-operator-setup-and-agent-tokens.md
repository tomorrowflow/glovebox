# 07: Operator setup and agent tokens

**Track:** access

**What to build:** The operator sets up the gate from the command line on the gateway host, outside MCP: adds accounts, points each at its credentials file, and issues each agent a revocable agent token scoped to specific accounts and read operations. An agent sees exactly the accounts its token covers and learns nothing about the others (ADR 0004).

**Blocked by:** 01, 02, 05

**Status:** ready-for-agent

- [ ] The operator can add, list and remove accounts with a local command; none of this is reachable through MCP.
- [ ] Upstream credentials are read from files; the gate refuses to start an account whose credentials file is readable by group or others. There is no application-level encryption or key management.
- [ ] The operator can issue an agent token scoped to a set of accounts, list tokens and revoke one. The token value is shown once; the database does not hold a usable copy.
- [ ] A revoked token is rejected on its next request.
- [ ] An agent can list the accounts its token covers, each with account handle and source kind.
- [ ] Account isolation: a token scoped to one account cannot list, count, group or page another account's items; a handle or cursor from an account outside the token's scope is answered exactly like one that does not exist.
- [ ] Token values, credentials and credential file contents never appear in any response or log.
- [ ] The static token of ticket 05 is gone; the throwaway and test stacks get their token through this mechanism.
- [ ] Operator-run: on the gateway host, at merge, the operator confirms that the credential files are unreadable for the agents' Linux users.
