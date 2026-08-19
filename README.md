# Codex DeepSeek Bridge

Codex DeepSeek Bridge 让 Codex 的主任务继续使用当前 OpenAI 模型，同时把边界清楚、偏只读、文本吞吐量大的工作交给一个独立的 DeepSeek 子任务。

它解决的是“选择性委派”问题，而不是把 Codex 全局切换到 DeepSeek。主任务仍负责拆分、验证和最终交付；DeepSeek worker 只提供受约束的证据或摘要。

## 为什么需要桥接

Codex 已支持为自定义子 Agent 指定不同模型和 Provider，但跨 Provider 的 spawn message 在部分版本中可能不能被目标模型可靠读取。本项目使用两个官方生命周期 Hook 建立一个临时兼容层：

1. `PreToolUse` 在 `spawn_agent` 执行前捕获目标 worker 的明文任务，并用父任务的 session/turn 身份建立一次性 reservation。
2. `SubagentStart` 只领取同一 session、turn、工作目录和角色的 reservation，并把任务注入子 Agent 的 developer context。

子任务的创建、权限、取消、等待和 callback 仍由 Codex 原生管理。桥接层不直接调用 DeepSeek API，也不启动第二个 Codex CLI、MCP 服务或常驻 daemon。

## 当前状态

项目正在实现首个可测试版本。当前设计目标和不可破坏的行为边界见：

- [设计合同](docs/DESIGN_CONTRACT.md)
- [架构说明](docs/ARCHITECTURE.md)
- [安全模型](docs/SECURITY_MODEL.md)

## 许可证

MIT License。Copyright (c) 2026 jinny51。
