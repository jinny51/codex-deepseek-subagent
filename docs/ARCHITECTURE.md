# 架构说明

## 两层产品结构

### Codex 插件

插件负责两件事：

- 提供“只有用户当前消息显式开启后，何时委派、怎样限制范围、如何验证结果”的 Skill。
- 通过官方 Hook 建立逐 turn 的一次性 grant，并完成 provider-neutral 的一次性任务交付。

插件不修改个人 `hooks.json` 和 `AGENTS.md`。Hook 跟随插件启用、禁用和版本更新，并继续使用 Codex 的人工 trust 流程。

### 自定义 Agent 注册器

当前插件清单不能直接注册 `~/.codex/agents/*.toml`，因此一个确定性脚本单独管理 worker TOML。它只允许 `plan/install/doctor/uninstall`，记录 ownership 和安装后哈希，不触碰顶层 `config.toml`。

## 显式授权与自动桥接流程

```text
原始用户 prompt 提交
             │
             ▼
UserPromptSubmit（无 matcher）
  - 去除前导空白
  - 只接受完整首 token $use-deepseek-subagent
  - 按 session + parent turn + cwd 建立一次性 grant
  - 不保存 prompt 正文
             │
             ▼
OpenAI 主 Agent评估任务并调用 spawn_agent
             │
             ▼
PreToolUse（匹配 Agent）
  - 只处理目标 role
  - 要求同 session + parent turn + cwd 的未消费 grant
  - 检查 fork_turns=none
  - 检查 assignment 大小和当前权限模式
  - 原子消费 grant，并按 parent session + cwd + role 建立 reservation
  - 记录 parent turn/tool_use_id 供审计，不拿 parent turn 做 child lookup
  - 无 grant 或任何校验失败时 deny，child 不创建
             │
             ▼
Codex 创建原生 DeepSeek child
             │
             ▼
SubagentStart（匹配目标 role）
  - 领取相同 parent session + cwd + role 的 reservation
  - 校验摘要、TTL 和身份
  - 注入 developer context
  - 擦除 assignment，保留无正文 receipt
             │
             ▼
Codex 原生 wait / cancel / callback
             │
             ▼
主 Agent验证并整合结果
```

Skill 元数据把 `allow_implicit_invocation` 设为 `false`，禁止 Codex 因任务语义自动选择委派 Skill。运行时安全边界仍由 Hook 执行：只有 `UserPromptSubmit` 能创建 grant，父 Agent、工具结果或 assignment 中出现同名 marker 都不会改变授权状态。

Token 规则有意保持简单且可审计：对原始 prompt 去除前导空白后，按空白取第一个 token，并要求它与 `$use-deepseek-subagent` 完全相等。句中提及、引用、`$use-deepseek-subagent:` 等近似形式都不授权。

## 为什么不再手工 stage

父 Agent 不需要先执行 shell 命令保存任务。`UserPromptSubmit` 和同一 parent turn 的 `PreToolUse` 提供可比较的 `session_id`、`turn_id` 和 `cwd`；`PreToolUse` 还提供原始 spawn 参数和 `tool_use_id`。因此桥接可以在真正 spawn 之前验证“这次用户消息是否授权”并捕获 assignment；无需相信模型生成的 marker，无需让模型执行额外 shell 命令，也无需使用跨 session 共享的全局 pending 槽。

`SubagentStart` 的 `turn_id` 属于 child 新提交的 turn，并不等于父 `turn_id`。所以 delivery lookup 只能使用官方明确共享的父 session，再结合规范化 cwd 和 exact role。父 turn 和 tool-use ID 只进入 envelope/receipt 用于诊断。

## 关联模型

Grant 的最小身份包含：

- `session_id`
- 当前父 `turn_id`
- `cwd` 的规范化摘要
- 目标 role 和 attribution 摘要
- 创建与过期时间

Grant 不保存原始用户 prompt、assignment 或 marker 副本。它只证明某个用户 turn 具备尝试一次目标 spawn 的资格。Grant 文件存在即表示 active；`PreToolUse` 接受目标 spawn 时，先持久化不含 Prompt 的 terminal tombstone，再删除 active grant 并建立 reservation。Tombstone 防止同一个 UserPromptSubmit 事件重放后恢复授权；损坏 tombstone 仍留在原 key 上 fail closed，只能由用户针对精确 key 显式解除。

Reservation 的最小身份包含：

- `session_id`
- `parent_turn_id`（仅审计）
- `cwd` 的规范化摘要
- 目标 `agent_type`
- 父 `tool_use_id`
- `task_name`
- 创建与过期时间
- assignment 的 UTF-8 长度和 SHA-256

`SubagentStart` 当前不提供父 `turn_id`、`tool_use_id` 或 `task_name`，所以同一个 parent-session/cwd/role 同时只允许一个未领取 reservation。不同 session 可以并行；已经领取任务的 workers 不持有 dispatch lock，可以继续并行执行。

## 状态机

```text
user prompt -> grant active -> consumed tombstone + captured
                  │                             │
                  └-> expired                   ▼
                                  claimed -> delivery_committed -> stdout attempt
                                      │              │
                                      └-> quarantine └-> body-free receipt
                                               ↑
                                             invalid
```

- `grant active`：UserPromptSubmit 已确认精确首 token，只记录当前 session/turn/cwd 的一次资格。
- `consumed tombstone`：PreToolUse 已接受一个目标 spawn；记录不含 Prompt，并阻止同一用户 turn 通过事件重放恢复授权。
- `captured`：PreToolUse 已验证 assignment，spawn 尚未开始。
- `claimed`：SubagentStart 已原子取得 reservation。
- `delivery_committed`：body-free receipt 已持久化且 assignment 已删除；之后才尝试写 Hook stdout。它表示 at-most-once 提交，不声称 child 或 Provider 一定观察到了内容。
- `receipt`：只保留 ID、哈希、大小、身份和时间，不保留正文。
- `quarantine`：结构或摘要不合法，必须显式检查，不自动覆盖。

## 大小与上下文

Assignment 用 UTF-8 字节数硬限制；超限直接阻止 spawn，不做静默截断。大日志和代码应通过工作区路径、范围和搜索目标引用，而不是内联进 developer context。

Hook 使用正数 `additionalContextLimit`，并确保 assignment 的字节上限明显低于该预算。

## 退役路径

任务载体桥接不是永久 API。项目将提供原生 transport probe；只有真实验证 provider-neutral message、child identity 和 callback 全部正确后，才允许删除 reservation/交付兼容层。版本号本身不是迁移证据。

逐条显式授权是产品边界，不随传输 workaround 一起退役。未来若 Codex 原生提供等价的“只由当前原始用户 turn 授权一次特定外部 Provider child”能力，可以用原生机制替换 grant Hook；不能因为原生 message 已可读取，就退回隐式或会话常开委派。
