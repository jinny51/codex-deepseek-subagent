---
name: delegate-deepseek-work
description: Delegate bounded, read-heavy text, code, or log evidence gathering from the current Codex parent to the installed deepseek_evidence_worker. Codex-only; use when deciding, spawning, waiting for, or troubleshooting this worker, and not in Chat/Work, during installation, or for decision-heavy and sensitive work.
---

# Delegate DeepSeek Work

Keep the current parent model, provider, login, and final responsibility unchanged.
The worker is an external-provider child for selective evidence gathering, not a
replacement parent.

## Decide whether to delegate

Delegate only when all of these are true:

- The job is bounded and preferably read-only.
- Most of the effort is searching, enumerating, extracting, or reading a large
  amount of text, code, or logs for a comparatively small conclusion.
- The useful result can be checked by the parent.
- The job does not require image understanding or tightly coupled judgment.
- The user has accepted that the assignment, the child-visible context, and
  tool results needed for the job are sent to the configured DeepSeek endpoint.

Do not delegate secrets, credentials, personal data, regulated material, or
private source unless the user has explicitly authorized that external data
boundary. A read-only sandbox limits mutation by default; it does not prevent
data disclosure.

## Create one self-contained assignment

Write a natural-language `message` that gives the child everything needed for
one job: objective, bounded inputs or paths, scope, exclusions, available
permissions, requested evidence/output, and stopping condition. Prefer paths
and read ranges over embedding large raw logs. Do not create bridge JSON or
protocol markers; the trusted bridge Hook wraps the message.

Spawn through the native subagent tool with:

- exact `agent_type`: `deepseek_evidence_worker`;
- a unique task name;
- `fork_turns="none"`;
- no model or provider override.

Use one child per self-contained assignment. Do not depend on inherited root
history or an essential follow-up message. Receive the contribution through the
native callback or a task-sized wait, then verify material claims before using
them in the parent's answer.

## Fail without substitution

Treat `transport_error`, a missing role, a denied spawn, a missing callback, or
an unavailable provider as an explicit delegation failure. Do not substitute a
different provider, direct API call, provider CLI, MCP adapter, wrapper process,
or second Codex process. Report the failed boundary and use
`$setup-deepseek-worker` for installation diagnostics when appropriate.
