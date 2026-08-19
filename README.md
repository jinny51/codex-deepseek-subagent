# Codex DeepSeek Bridge

Codex DeepSeek Bridge 让 Codex 的主任务继续使用当前 OpenAI 模型，同时把边界清楚、偏只读、文本吞吐量大的工作交给一个独立的 DeepSeek 子任务。

它解决的是“选择性委派”问题，而不是把 Codex 全局切换到 DeepSeek。主任务仍负责拆分、验证和最终交付；`deepseek_evidence_worker` 只提供受约束的证据或摘要。

这个项目由两部分组成：

- 一个 Codex 插件，提供委派 Skill 和自动任务桥 Hook；
- 一个确定性的 Agent 注册器，只管理个人 Codex 中的一份 worker TOML。

## 为什么需要桥接

Codex 已支持为自定义子 Agent 指定不同模型和 Provider，但跨 Provider 的 spawn message 在部分版本中可能不能被目标模型可靠读取。本项目使用两个官方生命周期 Hook 建立一个临时兼容层：

1. `PreToolUse` 在 `spawn_agent` 执行前捕获目标 worker 的明文任务，并用父 session、工作目录和角色建立一次性 reservation；父 turn 仅作为审计元数据保存。
2. `SubagentStart` 只领取同一父 session、工作目录和角色的 reservation，并把任务注入子 Agent 的 developer context。

子任务的创建、权限、取消、等待和 callback 仍由 Codex 原生管理。桥接层不直接调用 DeepSeek API，也不启动第二个 Codex CLI、MCP 服务或常驻 daemon。

## 和传统 stage 方案的区别

父 Agent 不需要先执行 shell 命令保存任务。当前 Codex 会让 `spawn_agent` 经过 `PreToolUse`，所以插件能在加密边界之前自动捕获原始 assignment，并用父 session、工作目录和角色建立关联。

如果捕获、权限、大小或状态校验失败，`PreToolUse` 会拒绝这次 spawn。不同 session 可以同时 dispatch；同一个 session/cwd/role 在任一时刻只允许一个尚未领取的 reservation，避免无法关联的 child 乱序串任务。`SubagentStart` 的 turn 是 child 新 turn，不能拿来匹配父 turn。

## 要求

- 当前 Codex 版本的 Hook 必须支持通过 `PreToolUse` 观察 `spawn_agent`（matcher alias 为 `Agent`）。
- Python 3.10 或更新版本；Windows 需要 `py -3` 可用。
- 一个由用户自己在本机配置的 `DEEPSEEK_API_KEY`。
- DeepSeek 端可用的 `deepseek-v4-flash` Responses API 路径。

## 安装

### 1. 添加并安装插件

开发阶段可以固定到当前分支或 commit；正式发布后应固定到 tag，而不是长期跟随可变的 `main`。

```bash
codex plugin marketplace add jinny51/codex-deepseek-bridge --ref main
codex plugin add codex-deepseek-bridge@jinny51-codex
```

安装或升级插件后，启动一个新的 Codex 任务。

### 2. 注册 DeepSeek worker

在 Codex 中调用：

```text
$setup-deepseek-worker
```

先运行 `plan`，确认目标仅为：

```text
<CODEX_HOME>/agents/deepseek-evidence-worker.toml
```

再执行 `install`。注册器不会修改顶层 `config.toml`、个人 `AGENTS.md`、Hook 配置或 Hook trust，也不会调用任何模型。

### 3. 配置凭据并信任 Hook

在 Codex 进程启动前，通过操作系统环境配置 `DEEPSEEK_API_KEY`。不要把 Key 发进聊天、提交到仓库或写进 Agent TOML。若 Key 曾出现在聊天或日志中，应先在 Provider 控制台撤销并轮换。

完全重启 Codex 后，通过 `/hooks` 审查插件的两个命令 Hook：

- `PreToolUse`：仅捕获目标 role 的原生 spawn 参数；
- `SubagentStart`：仅向同一身份的目标 child 交付 assignment。

插件不能也不会自动信任 Hook。

## 使用

正常情况下，直接说明希望使用 DeepSeek worker，或显式调用：

```text
$delegate-deepseek-work
```

父 Agent 会评估任务是否适合委派，并使用原生 `spawn_agent`、`fork_turns="none"` 和唯一 task name。桥接过程自动发生；结果通过 Codex 原生 callback 返回，父 Agent随后验证并整合。

适合的例子包括日志时间线、代码路径枚举、批量文本提取和大范围只读搜索。不适合架构决策、最终验收、高风险修改、图像任务，以及未经用户同意发送给外部 Provider 的敏感内容。

## 诊断和卸载

Agent 配置：

```text
$setup-deepseek-worker
```

Bridge 运行时可以在已安装插件目录执行：

```bash
python3 hooks/bridge.py doctor
python3 hooks/bridge.py status
```

这些命令只显示元数据，不显示 assignment 正文。`uninstall` 只删除仍与 ownership hash 匹配的自有 Agent 文件；检测到用户修改时会拒绝删除。

完整卸载必须按顺序进行：

1. 在插件仍启用时，通过 `$setup-deepseek-worker` 执行 Agent `uninstall`；
2. 再运行 `codex plugin remove codex-deepseek-bridge@jinny51-codex`。

Codex 插件卸载不会自动删除独立的个人 Agent TOML。若顺序颠倒，遗留 worker 会因为收不到有效 bridge contract 而安全返回 `transport_error`，但仍需要重新安装插件后运行注册器，或由用户确认归属后手动处理。

## 安全边界

- Assignment 会在 `PreToolUse` 到 `SubagentStart` 的短窗口内以本地明文存在；桥接在尝试写 Hook stdout 前先提交 at-most-once 状态并擦除正文，因此 receipt 不等于 child 已确认接收。
- Assignment、child 所需上下文和工具结果会发送到 DeepSeek。
- Worker 的 `read-only` 是默认权限，不是保密或 DLP 边界；父任务的实时权限设置仍可能影响 child。
- Hook 状态按父 session、规范化 cwd 和 role 绑定，记录但不依赖父 turn，并有大小上限、短 TTL、摘要、原子状态转换和一次性交付。
- 交付结果不确定时不会自动 replay。

完整说明见 [安全模型](docs/SECURITY_MODEL.md) 和 [SECURITY.md](SECURITY.md)。

## 开发与测试

离线测试不读取 Provider 凭据，也不调用任何模型：

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 -m unittest discover \
  -s plugins/codex-deepseek-bridge/skills/setup-deepseek-worker/tests \
  -p "test_*.py" -v
```

CI 覆盖 Ubuntu、Windows、macOS 以及 Python 3.11/3.12。详情见 [测试说明](docs/TESTING.md)。

## 设计边界

项目目标和不可破坏的行为边界见：

- [设计合同](docs/DESIGN_CONTRACT.md)
- [架构说明](docs/ARCHITECTURE.md)
- [安全模型](docs/SECURITY_MODEL.md)
- [Provider 兼容性与真实 smoke 门槛](docs/PROVIDER_COMPATIBILITY.md)

## 许可证

MIT License。Copyright (c) 2026 jinny51。
