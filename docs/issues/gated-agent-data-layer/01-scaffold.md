# 01: Scaffold the Python project

**Track:** scaffold

**What to build:** The project skeleton for the gate in the stack ADR 0005 settled: one Python service, the official MCP Python SDK, SQLite. A developer or agent can clone the repository, install it, and run the build, the tests, the linter and the service entry point with the commands that `AGENTS.md` names; CI runs the same commands. The service starts and answers an MCP client over Streamable HTTP with an empty tool list. No source, no classifier and no tool yet.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] The project installs with one documented command and pins its dependencies, including the official MCP Python SDK.
- [ ] One command runs the test suite; it contains at least one acceptance test at the MCP tool-call boundary (ADR 0006): an MCP client connects to the service and lists tools, and the list is empty.
- [ ] One command runs lint and formatting checks, and one runs type checks if the scaffold adopts a type checker.
- [ ] CI runs install, lint and tests on every push and pull request, and is green.
- [ ] `AGENTS.md` names the install, test, lint and run commands, and points to `CONTEXT.md`, `docs/adr/` and `docs/prd/gated-agent-data-layer.md`.
- [ ] The README replaces "placeholder" with a short description that states the guarantee and non-claims of ADR 0001 without claiming more than them.
- [ ] No code talks to any source, model or outside service.
