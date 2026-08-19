# Privacy

Codex DeepSeek Subagent is a local Codex plugin. The plugin author does not operate a service that receives task content, API keys, telemetry, or usage data from the plugin.

When the user explicitly begins a prompt with `$use-deepseek-subagent`, the plugin may allow Codex to create the configured DeepSeek child. The assignment, child-visible context, files the child reads, and tool results required by that task are then sent by Codex to the Provider configured in the personal Agent file. That Provider's privacy, retention, training, and account policies apply independently.

The plugin never stores the DeepSeek API key. Codex reads `DEEPSEEK_API_KEY` from the environment for Provider authentication.

For task delivery, the plugin writes a bounded assignment to private local state for the short interval between `PreToolUse` and `SubagentStart`. It removes the assignment body when it commits the delivery attempt. Body-free grants, consumed-grant tombstones, receipts, hashes, IDs, paths, and timestamps may remain locally for replay protection, diagnosis, and bounded cleanup.

No prompt body is saved when the plugin creates an explicit one-use grant. A read-only child default reduces mutation risk but does not prevent disclosure to the configured external Provider.

See [SECURITY.md](SECURITY.md) and [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) for the complete local-state and threat model.
