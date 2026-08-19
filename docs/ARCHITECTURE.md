# 架构说明

## 两层产品结构

### Codex 插件

插件负责两件事：

- 提供“何时委派、怎样限制范围、如何验证结果”的 Skill。
- 通过官方 Hook 完成 provider-neutral 的一次性任务交付。

插件不修改个人 `hooks.json` 和 `AGENTS.md`。Hook 跟随插件启用、禁用和版本更新，并继续使用 Codex 的人工 trust 流程。

### 自定义 Agent 注册器

当前插件清单不能直接注册 `~/.codex/agents/*.toml`，因此一个确定性脚本单独管理 worker TOML。它只允许 `plan/install/doctor/uninstall`，记录 ownership 和安装后哈希，不触碰顶层 `config.toml`。

## 自动桥接流程

```text
OpenAI 主 Agent 调用 spawn_agent
             │
             ▼
PreToolUse（匹配 Agent）
  - 只处理目标 role
  - 检查 fork_turns=none
  - 检查 assignment 大小和当前权限模式
  - 按 session + turn + cwd 建立 reservation
  - 失败时 deny，spawn 不发生
             │
             ▼
Codex 创建原生 DeepSeek child
             │
             ▼
SubagentStart（匹配目标 role）
  - 领取相同 session + turn + cwd 的 reservation
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

## 为什么不再手工 stage

当前 Codex 会让 `spawn_agent` 经过 `PreToolUse`，并向 Hook 提供原始参数、父 `session_id`、`turn_id`、`tool_use_id` 和 `cwd`。因此桥接可以在真正 spawn 之前自动捕获 assignment；无需让模型执行一条额外 shell 命令，也无需使用跨任务共享的全局 pending 槽。

## 关联模型

Reservation 的最小身份包含：

- `session_id`
- `turn_id`
- `cwd` 的规范化摘要
- 目标 `agent_type`
- 父 `tool_use_id`
- `task_name`
- 创建与过期时间
- assignment 的 UTF-8 长度和 SHA-256

`SubagentStart` 当前不提供父 `tool_use_id`，所以同一个 session、turn 和 role 同时只允许一个未领取 reservation。不同 session 可以并行；已经领取任务的 workers 不持有 dispatch lock，可以继续并行执行。

## 状态机

```text
captured -> claimed -> delivered -> receipt
    │          │
    └-> expired/quarantine <- invalid
```

- `captured`：PreToolUse 已验证 assignment，spawn 尚未开始。
- `claimed`：SubagentStart 已原子取得 reservation。
- `delivered`：developer context 已成功写出，assignment 立即删除。
- `receipt`：只保留 ID、哈希、大小、身份和时间，不保留正文。
- `quarantine`：结构或摘要不合法，必须显式检查，不自动覆盖。

## 大小与上下文

Assignment 用 UTF-8 字节数硬限制；超限直接阻止 spawn，不做静默截断。大日志和代码应通过工作区路径、范围和搜索目标引用，而不是内联进 developer context。

Hook 使用正数 `additionalContextLimit`，并确保 assignment 的字节上限明显低于该预算。

## 退役路径

桥接不是永久 API。项目将提供原生 transport probe；只有真实验证 provider-neutral message、child identity 和 callback 全部正确后，才允许禁用桥接。版本号本身不是迁移证据。
