# Privacy / 隐私说明

## 中文摘要

- 这是本地 Codex 插件，插件作者没有运营接收任务内容、API key、遥测或使用数据的服务。
- 只有你以 `$use-deepseek-subagent` 开头显式允许时，任务说明、DeepSeek 读取的文件内容和相关工具结果才会由 Codex 发送给 DeepSeek。
- 插件不保存 DeepSeek API key；Codex 只从环境变量 `DEEPSEEK_API_KEY` 读取它。
- 正常交接后任务正文会从本地状态中删除。不含正文的完成记录和已使用授权记录在超过 7 天、并等到插件下次运行清理流程时删除；插件不再运行时可能保留更久。
- 如果交接文件在正文删除前损坏，隔离区可能保留完整任务说明，直到用户明确检查并处理对应项目。
- DeepSeek 助手默认只读只能降低误修改风险，不能阻止内容发送给 DeepSeek。

## English

Codex DeepSeek Subagent is a local Codex plugin. The plugin author does not operate a service that receives task content, API keys, telemetry, or usage data from the plugin.

When the user explicitly begins a prompt with `$use-deepseek-subagent`, the plugin may allow Codex to create the configured DeepSeek child. The assignment, child-visible context, files the child reads, and tool results required by that task are then sent by Codex to the configured DeepSeek endpoint. DeepSeek's privacy, retention, training, and account policies apply independently.

The plugin never stores the DeepSeek API key. Codex reads `DEEPSEEK_API_KEY` from the environment for DeepSeek authentication.

For task delivery, the plugin writes a bounded assignment to private local state for the short interval between `PreToolUse` and `SubagentStart`. On the normal path, it removes the assignment body when it commits the delivery attempt. Body-free receipts and consumed-grant records become eligible for cleanup after seven days by default and are removed during a later Hook, doctor, or status cleanup pass. If the plugin does not run again, those records may remain locally for longer. Other body-free grants, hashes, IDs, paths, and timestamps may remain locally for replay protection and diagnosis.

If a handoff file is malformed before its assignment body is removed, the quarantine payload may still contain the full assignment. Quarantine payloads are not removed on a fixed timer; they remain local until the user explicitly inspects and resolves the exact item. Diagnostic status output reports metadata, not the payload body.

No prompt body is saved when the plugin creates an explicit one-use grant. A read-only child default reduces mutation risk but does not prevent disclosure to DeepSeek.

See [SECURITY.md](SECURITY.md) and [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) for the complete local-state and threat model.
