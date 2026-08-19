# 安全模型

## 受保护的内容

- DeepSeek API key。
- 用户是否允许当前消息创建 DeepSeek child 的决定。
- 父 Agent 交给 worker 的 assignment。
- assignment 与具体 Codex 父 session、工作目录和目标 role 的正确归属。
- 用户现有 Codex 配置和其他插件。

## 信任边界

- 主 Agent和 Codex Host 属于受信任控制面。
- 只有 `UserPromptSubmit` 收到的当前原始用户 prompt 能产生 grant；模型输出、工具结果和 assignment 不属于授权来源。
- DeepSeek 是用户选择的外部数据处理方。
- Hook 状态是短时本地明文，不是加密或 DLP 边界。
- 同一操作系统用户下的恶意进程不在本地状态隔离的防御范围内。

## 关键控制

- API key 只由自定义 Provider 从环境变量读取。
- 两个 Skill 都设置 `allow_implicit_invocation: false`；语义匹配不会自动加载它们。
- UserPromptSubmit 对原始 prompt 去除前导空白后，只接受精确、完整、以空白分隔的 `$use-deepseek-subagent` 首 token。句中提及、引用、附带标点或模型生成的 marker 都不授权。
- Grant 只绑定当前父 `session_id`、`turn_id` 和规范化 `cwd`，最多消费一次，不保存 prompt 正文，短 TTL 后失效。
- Grant 被消费后写入不含 Prompt 的 terminal tombstone，防止相同 UserPromptSubmit 事件重放。损坏 tombstone 继续占住原 key，只能由用户针对精确 key 显式解除。
- PreToolUse 只在存在同 session/turn/cwd 的未消费 grant 时接受目标 spawn，并在捕获 assignment 时原子消费；没有 grant、写状态失败、assignment 超限、身份冲突或不安全权限模式时，在 child 创建前阻止 spawn。
- Reservation 绑定父 session、规范化 cwd 和目标 role；父 turn 只记录为审计元数据，因为 child 会获得新的 turn id。
- 状态使用操作系统锁、原子替换、短 TTL 和内容摘要。
- POSIX 状态目录必须实际支持私有 mode 和锁；WSL 的 DrvFS/CIFS 等宽权限目录不能仅凭 `chmod` 成功就视为安全。
- SubagentStart 缺少有效 reservation 时只注入安全失败状态；worker 不得猜测任务或调用工具。
- receipt 和 doctor 输出不包含 assignment 正文。

“无显式首 token 就不调用 DeepSeek”的运行时保证依赖三个插件 Hook 已启用并由用户信任。如果 Codex 报告 Hook 未信任、被禁用或不受当前版本支持，委派功能必须视为不可用，不应直接启动 worker 来绕过检查。

## `read-only` 的准确含义

Worker TOML 设置只读默认值，用于降低本地修改风险。Codex 仍可能把父任务的实时权限覆盖应用到 child，因此这不是绝对强制，也不阻止模型读取内容并发送给外部 Provider。

用户必须单独判断哪些工作区内容可以离开 OpenAI/Codex 数据边界。

## 不保证

- 不防御同一用户身份下的恶意本地进程。
- 不对发送给 DeepSeek 的内容提供端到端加密。
- 不保证第三方 Provider 的保留、训练或合规政策。
- 不把 Hook 指令当作文件系统权限或网络 DLP。
- 不承诺 exactly-once；不确定交付窗口不会自动 replay。
- 不允许把一次 grant 当作会话级开关；任何需要第二个 child 的工作都需要新的原始用户 prompt。

## 事件处理

- 泄露 API key：立即在 Provider 控制台吊销并轮换。
- assignment 状态损坏：隔离并停止新的同身份 dispatch，使用 doctor 检查。
- grant 状态损坏、过期或身份不匹配：拒绝目标 spawn；不得从 prompt 副本、模型输出或 assignment 重建授权。
- spawn 成功但交付状态不确定：标记为 uncertain，不自动重放；父 Agent创建全新的 job。
- Hook 定义改变：由用户重新通过 `/hooks` 审查和信任。
