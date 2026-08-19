# Codex DeepSeek Subagent

Codex DeepSeek Subagent 让 Codex 的主任务继续使用当前 OpenAI 模型，同时在用户逐条明确授权时，把边界清楚、偏只读、文本吞吐量大的工作交给一个独立的 DeepSeek 子任务。

它解决的是“显式、受约束的选择性委派”问题，而不是把 Codex 全局切换到 DeepSeek。插件全局安装后，所有新任务都有使用这项能力的条件，但每条普通消息仍只由当前 OpenAI 主任务处理。只有原始用户消息以完整首 token `$use-deepseek-subagent` 开头时，当前 turn 才能获得一次 DeepSeek child 授权。主任务仍负责拆分、验证和最终交付；`deepseek_evidence_worker` 只提供受约束的证据或摘要。

这个项目由两部分组成：

- 一个 Codex 插件，提供显式委派 Skill、一次性授权和任务桥 Hook；
- 一个确定性的 Agent 注册器，只管理个人 Codex 中的一份 worker TOML。

## 为什么需要桥接

Codex 已支持为自定义子 Agent 指定不同模型和 Provider，但跨 Provider 的 spawn message 在部分版本中可能不能被目标模型可靠读取。本项目使用三个官方生命周期 Hook 建立“用户授权 + 临时兼容传输”两道边界：

1. `UserPromptSubmit` 检查当前原始用户 prompt。去除前导空白后，只有第一个完整、以空白分隔的 token 恰好为 `$use-deepseek-subagent`，才按父 session、当前 turn 和工作目录创建一次性 grant；不保存 prompt 正文。
2. `PreToolUse` 在 `spawn_agent` 执行前检查目标 worker 是否拥有同 session、turn 和工作目录的未消费 grant。没有就拒绝 child 创建；有则在接受这一个 spawn 时消费 grant，捕获明文 assignment，并建立一次性 reservation。
3. `SubagentStart` 只领取同一父 session、工作目录和角色的 reservation，并把任务注入子 Agent 的 developer context。

子任务的创建、权限、取消、等待和 callback 仍由 Codex 原生管理。桥接层不直接调用 DeepSeek API，也不启动第二个 Codex CLI、MCP 服务或常驻 daemon。

Skill 元数据中的 `allow_implicit_invocation: false` 防止 Codex 因语义匹配自动加载委派 Skill；Hook 的 grant 检查则阻止主 Agent绕过 Skill 直接 spawn。模型自己写出的 `$use-deepseek-subagent`、句中提及、引用示例和附带标点的近似 token 都不能授权。

## 和传统 stage 方案的区别

父 Agent 不需要先执行 shell 命令保存任务。`UserPromptSubmit` 先建立与当前用户 turn 绑定的一次性 grant；当前 Codex 再让 `spawn_agent` 经过 `PreToolUse`，所以插件能在加密边界之前同时验证授权和捕获原始 assignment。

没有 grant，或授权、捕获、权限、大小、状态校验失败时，`PreToolUse` 会在 DeepSeek child 创建前拒绝这次 spawn。一个 grant 只允许一个 child，消费后不能重试、复制或跨 turn/session/cwd 复用。不同 session 可以同时 dispatch；同一个 session/cwd/role 在任一时刻只允许一个尚未领取的 reservation，避免无法关联的 child 乱序串任务。`SubagentStart` 的 turn 是 child 新 turn，不能拿来匹配父 turn。

## 要求

- 当前 Codex 版本的 Hook 必须支持 `UserPromptSubmit`，并支持通过 `PreToolUse` 观察 `spawn_agent`（matcher alias 为 `Agent`）。
- Python 3.10 或更新版本；Windows 需要 `py -3` 可用。
- 一个由用户自己在本机配置的 `DEEPSEEK_API_KEY`。
- DeepSeek 端可用的 `deepseek-v4-flash` Responses API 路径。

## 安装

### 1. 添加并安装插件

开发阶段可以固定到当前分支或 commit；正式发布后应固定到 tag，而不是长期跟随可变的 `main`。

```bash
codex plugin marketplace add jinny51/codex-deepseek-subagent --ref main
codex plugin add codex-deepseek-subagent@jinny51-codex
```

安装或升级插件后，启动一个新的 Codex 任务。

> 预发布改名说明：早期私有开发分支曾短暂使用
> `codex-deepseek-bridge` 身份，但从未公开发布。如果你曾手工测试该开发版，先用旧版
> `$setup-deepseek-worker` 卸载其 managed Agent，再执行
> `codex plugin remove codex-deepseek-bridge@jinny51-codex`，之后再安装当前插件。
> 当前安装器会对旧 ownership marker 明确报冲突，不会静默接管或覆盖。

### 2. 注册 DeepSeek worker

在 Codex 中调用：

```text
$setup-deepseek-subagent
```

先运行 `plan`，确认目标仅为：

```text
<CODEX_HOME>/agents/deepseek-evidence-worker.toml
```

再执行 `install`。注册器不会修改顶层 `config.toml`、个人 `AGENTS.md`、Hook 配置或 Hook trust，也不会调用任何模型。

### 3. 配置凭据并信任 Hook

在 Codex 进程启动前，通过操作系统环境配置 `DEEPSEEK_API_KEY`。不要把 Key 发进聊天、提交到仓库或写进 Agent TOML。若 Key 曾出现在聊天或日志中，应先在 Provider 控制台撤销并轮换。

完全重启 Codex 后，通过 `/hooks` 审查插件的三个命令 Hook：

- `UserPromptSubmit`：只从当前原始用户 prompt 创建短期一次性 grant；
- `PreToolUse`：先验证并消费匹配 grant，再捕获目标 role 的原生 spawn 参数；
- `SubagentStart`：仅向同一身份的目标 child 交付 assignment。

插件不能也不会自动信任 Hook。

## 使用

插件是“全局安装、逐条消息开启”。要允许当前消息使用 DeepSeek，去除前导空白后的第一个完整 token 必须是 Skill 名称：

```text
$use-deepseek-subagent 帮我分析 logs/ 下的启动失败，只输出时间线和关键错误，不修改文件。
```

单独一行写 token 后再换行描述任务也有效。下面这些都不授权：

```text
帮我分析日志
请解释 $use-deepseek-subagent 是什么
$use-deepseek-subagent: 帮我分析日志
```

一次有效前缀只允许当前 session、当前 turn、当前工作目录中的一个 DeepSeek child。使用后自动关闭；下一条消息要再次使用，必须再次以前缀开头。没有这个前缀时，桥接 Hook 会拒绝目标 child 创建，不会调用 DeepSeek。模型不能通过在自己的输出或 `spawn_agent.message` 中补写 token 来获得授权。

显式前缀是必要条件，不是强制派工命令。父 Agent 仍会检查任务是否适合外部、偏只读的证据收集；适合时才使用原生 `spawn_agent`、`fork_turns="none"` 和唯一 task name。不适合时不应为了“用上 DeepSeek”而扩大数据边界或改变项目目的。桥接过程自动发生；结果通过 Codex 原生 callback 返回，父 Agent 随后验证并整合。

适合的例子包括日志时间线、代码路径枚举、批量文本提取和大范围只读搜索。不适合架构决策、最终验收、高风险修改、图像任务，以及未经用户同意发送给外部 Provider 的敏感内容。

## 诊断和卸载

Agent 配置：

```text
$setup-deepseek-subagent
```

Bridge 运行时可以在已安装插件目录执行：

```bash
python3 hooks/bridge.py doctor
python3 hooks/bridge.py status
```

这些命令只显示元数据，不显示 assignment 正文。`uninstall` 只删除仍与 ownership hash 匹配的自有 Agent 文件；检测到用户修改时会拒绝删除。

损坏的 consumed-grant tombstone 会留在原 key 上并持续阻止同一授权被重建。只有用户确认精确 key 后，才使用 `python3 hooks/bridge.py resolve-consumed --key-hash <64位小写十六进制>` 解除；再次提交同一 Prompt 不会自动修复或绕过。

完整卸载必须按顺序进行：

1. 在插件仍启用时，通过 `$setup-deepseek-subagent` 执行 Agent `uninstall`；
2. 再运行 `codex plugin remove codex-deepseek-subagent@jinny51-codex`。

Codex 插件卸载不会自动删除独立的个人 Agent TOML。若顺序颠倒，遗留 worker 会因为收不到有效 bridge contract 而安全返回 `transport_error`，但仍需要重新安装插件后运行注册器，或由用户确认归属后手动处理。

## 安全边界

- Assignment 会在 `PreToolUse` 到 `SubagentStart` 的短窗口内以本地明文存在；桥接在尝试写 Hook stdout 前先提交 at-most-once 状态并擦除正文，因此 receipt 不等于 child 已确认接收。
- `UserPromptSubmit` 只检查原始 prompt 是否具有精确首 token；grant 不保存 prompt 正文，只保存授权关联所需的 session、turn、工作目录摘要、目标 role 和有效期。接受一次目标 spawn 后会删除 grant。
- Grant 不跨 turn、session 或 cwd，最多消费一次；缺少匹配 grant 时必须在 child 创建前失败。
- 已消费 grant 使用不含 Prompt 的 tombstone 防止事件重放。损坏 tombstone 仍留在原 key 上 fail closed，只能由用户针对精确 key 显式解除。
- Assignment、child 所需上下文和工具结果会发送到 DeepSeek。
- Worker 的 `read-only` 是默认权限，不是保密或 DLP 边界；父任务的实时权限设置仍可能影响 child。
- Reservation 状态按父 session、规范化 cwd 和 role 绑定，记录但不依赖父 turn，并有大小上限、短 TTL、摘要、原子状态转换和一次性交付；grant 则必须额外绑定当前父 turn。
- 交付结果不确定时不会自动 replay。

完整说明见 [安全模型](docs/SECURITY_MODEL.md) 和 [SECURITY.md](SECURITY.md)。

## 开发与测试

离线测试不读取 Provider 凭据，也不调用任何模型：

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 -m unittest discover \
  -s plugins/codex-deepseek-subagent/skills/setup-deepseek-subagent/tests \
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
