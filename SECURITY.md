# Security

## API keys

Never paste a Provider key into chat, source control, screenshots, issues, Hook state, or Agent TOML. Configure `DEEPSEEK_API_KEY` outside Codex and restart the Codex process so the custom Provider can inherit it.

A key that appears in chat, logs, or a public artifact must be treated as compromised and revoked before further use.

The setup tool reports only `present` or `missing`; it does not print, store, validate, or transmit the value.

## Explicit per-turn authorization

Installing the plugin globally makes the capability available; it does not enable DeepSeek for a session. A user prompt authorizes at most one target child only when, after leading whitespace is removed, its first complete whitespace-delimited token is exactly `$use-deepseek-subagent`.

A later mention, quoted example, attached punctuation such as `$use-deepseek-subagent:`, or marker text produced by the parent model, a tool, a Skill, or another Agent is not authorization. The grant is bound to the current parent session, turn, and normalized working directory. It is consumed when `PreToolUse` accepts the target spawn and cannot be reused, copied, or carried into another turn, session, or directory.

Without a matching unconsumed grant, the target spawn must be denied before a DeepSeek child is created. This enforcement requires the plugin's `UserPromptSubmit`, `PreToolUse`, and `SubagentStart` Hooks to be enabled and trusted. If any required Hook is unavailable, untrusted, or disabled, treat delegation as unavailable rather than bypassing the guard.

## External Provider boundary

When `deepseek_evidence_worker` runs, its assignment, required context, file contents it reads, and tool results are sent to the configured DeepSeek endpoint. Delegate only data the user has authorized for that external processing boundary.

`sandbox_mode = "read-only"` reduces mutation risk by default. It does not prevent disclosure, and current Codex runtime permission overrides can still affect a child.

## Local plaintext bridge

`UserPromptSubmit` inspects the original prompt for the exact prefix but does not persist its body. The resulting short-lived grant stores only the identity and lifecycle metadata required to bind one authorization to the current parent session, turn, and normalized working directory.

After a grant is consumed, a body-free terminal tombstone prevents the same user-turn event from recreating authority. A corrupt tombstone remains blocking at its exact key and is removed only through an explicit `resolve-consumed --key-hash ...` operation.

The bridge stores one bounded assignment in local plaintext between `PreToolUse` and `SubagentStart`. Reservation state is bound to the parent session, normalized working directory, and target role; the parent turn id is retained only as audit metadata because the child receives a new turn id. It uses a short TTL, a content digest, private state directories where the platform supports them, and at-most-once delivery.

Before Hook stdout is attempted, the bridge commits at-most-once delivery by writing a body-free receipt and removing the assignment. The receipt therefore proves a local delivery attempt, not that the child or Provider observed it. Corrupt state is quarantined for explicit inspection; it is not silently overwritten.

This is a compatibility transport, not encryption, DLP, or protection from malicious processes running as the same operating-system user.

## Hook trust

All three plugin command Hooks remain untrusted until the user reviews their exact definition through `/hooks`. Never bypass or forge Hook trust during installation. A Hook change requires a new review.

## Reports

Security reports must exclude API keys, authorization headers, full environment dumps, private task contents, and unredacted local paths. Rotate an exposed credential before investigating the underlying bug.
