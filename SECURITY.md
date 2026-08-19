# Security

## API keys

Never paste a Provider key into chat, source control, screenshots, issues, Hook state, or Agent TOML. Configure `DEEPSEEK_API_KEY` outside Codex and restart the Codex process so the custom Provider can inherit it.

A key that appears in chat, logs, or a public artifact must be treated as compromised and revoked before further use.

The setup tool reports only `present` or `missing`; it does not print, store, validate, or transmit the value.

## External Provider boundary

When `deepseek_evidence_worker` runs, its assignment, required context, file contents it reads, and tool results are sent to the configured DeepSeek endpoint. Delegate only data the user has authorized for that external processing boundary.

`sandbox_mode = "read-only"` reduces mutation risk by default. It does not prevent disclosure, and current Codex runtime permission overrides can still affect a child.

## Local plaintext bridge

The bridge stores one bounded assignment in local plaintext between `PreToolUse` and `SubagentStart`. State is bound to the parent session, normalized working directory, and target role; the parent turn id is retained only as audit metadata because the child receives a new turn id. It uses a short TTL, a content digest, private state directories where the platform supports them, and at-most-once delivery.

Before Hook stdout is attempted, the bridge commits at-most-once delivery by writing a body-free receipt and removing the assignment. The receipt therefore proves a local delivery attempt, not that the child or Provider observed it. Corrupt state is quarantined for explicit inspection; it is not silently overwritten.

This is a compatibility transport, not encryption, DLP, or protection from malicious processes running as the same operating-system user.

## Hook trust

Plugin Hooks remain untrusted until the user reviews their exact definition through `/hooks`. Never bypass or forge Hook trust during installation. A Hook change requires a new review.

## Reports

Security reports must exclude API keys, authorization headers, full environment dumps, private task contents, and unredacted local paths. Rotate an exposed credential before investigating the underlying bug.
