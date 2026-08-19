---
name: setup-deepseek-worker
description: Plan, install, diagnose, or uninstall the managed deepseek_evidence_worker Agent file for Codex DeepSeek Bridge. Codex-only; use for local worker setup and configuration health, not in Chat/Work, for delegating work, or for managing API keys and Hook trust.
---

# Set Up the DeepSeek Worker

Use the bundled deterministic script at `scripts/manage_agent.py`; do not
recreate or hand-edit the Agent TOML. Use an already available Python 3.10 or
newer executable (`python3` on POSIX; `python`, `python.exe`, or `py -3` on
Windows) and let the script resolve `CODEX_HOME` (falling back to `~/.codex`).

```text
python3 <absolute-skill-path>/scripts/manage_agent.py plan
python3 <absolute-skill-path>/scripts/manage_agent.py install
python3 <absolute-skill-path>/scripts/manage_agent.py doctor
python3 <absolute-skill-path>/scripts/manage_agent.py uninstall
```

- Use `plan` or `doctor` for inspection.
- Run `install` only when the user has asked to install or update the worker.
- Run `uninstall` only when the user has asked to remove the managed Agent file.
- Stop on a conflict or user-modified managed file. Never overwrite or delete it
  to force success.

The script owns only
`<CODEX_HOME>/agents/deepseek-evidence-worker.toml`. Its ownership manifest is
embedded in that file, so it creates no sidecar configuration. It never edits
`config.toml`, `AGENTS.md`, Hook configuration, or Hook trust state.

The script reports only whether `DEEPSEEK_API_KEY` is `present` or `missing`.
Never ask for, read back, print, store, or validate the key value. A missing key
does not authorize changing the user's environment.

For transport-state diagnostics, resolve the plugin root from this Skill path
and run the bundled Hook program without a model call:

```text
python3 <absolute-plugin-root>/hooks/bridge.py doctor
python3 <absolute-plugin-root>/hooks/bridge.py status
```

`doctor` checks the private state directory and lock backend. `status` reports
only handoff IDs, hashes, byte counts, states, and timestamps; it never prints
an assignment body. Use `cancel --handoff-id <uuid>` or
`resolve --quarantine-id <uuid>` only for the exact item the user has asked to
remove or resolve.

After an install or update, summarize the exact action and target path. The user
must review the plugin Hook through `/hooks`; do not bypass or forge Hook trust.
Use a new Codex task for Agent discovery and for any later paid smoke test. Do
not make a provider call unless the user separately asks for that test.
