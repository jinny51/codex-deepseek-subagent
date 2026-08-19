# Contributing

Changes must preserve the [design contract](docs/DESIGN_CONTRACT.md). In particular, the OpenAI parent remains responsible for selection, verification, and final delivery; the bridge remains a removable compatibility layer around Codex native subagent lifecycle.

Before opening a pull request:

1. Run the repository and setup test suites described in [docs/TESTING.md](docs/TESTING.md).
2. Run the plugin and Skill validators when available.
3. Confirm no Provider credential or private assignment entered the diff, fixtures, logs, or commit history.
4. Explain any change to data scope, local plaintext lifetime, permissions, Hook trust, or paid-call behavior.
5. Do not add direct Provider calls, MCP transport, a daemon, inherited-turn fallback, automatic replay, or global Provider switching without first changing the design contract and demonstrating why native Codex lifecycle can no longer satisfy the goal.

Real Provider smoke tests must be manual, explicitly authorized, low-cost, and separate from ordinary CI.
