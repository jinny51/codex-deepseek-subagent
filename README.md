# Codex DeepSeek Subagent

这个插件让你在不更换 Codex 主模型的情况下，临时请 DeepSeek 帮忙读日志、搜索代码或整理大量文本。

它不会把整个 Codex 切换成 DeepSeek，也不会在安装后自动调用 DeepSeek。只有当一条消息以 `$use-deepseek-subagent` 开头时，Codex 才可以为这条消息启动一次 DeepSeek 助手。下一条消息会自动恢复默认状态。

> **当前状态：公开预览版。** 插件安装、显式开关、任务交接和离线测试已经通过，但 DeepSeek Responses API 的真实工具调用还没有完成带有效凭据的端到端验证。首次真实调用仍可能因为接口兼容性而失败。插件遇到这种情况会明确报错，不会暗中换用其他模型或传输方式。它现在可以从公开 GitHub 仓库安装，但还不是 Codex 通用插件目录里的稳定版本。

## 先把四个名字分清楚

| 用途 | 名称 |
| --- | --- |
| 仓库和插件 | `codex-deepseek-subagent` |
| 安装、检查或卸载 DeepSeek 助手 | `$setup-deepseek-subagent` |
| 允许当前这条消息使用 DeepSeek | `$use-deepseek-subagent` |
| 插件内部的 DeepSeek Agent 名 | `deepseek_evidence_worker`，普通用户不需要直接输入 |

这是一个插件，不是两个项目。之所以还需要执行一次安装命令，是因为当前 Codex 插件不能直接替你注册个人 Agent 配置。安装 Skill 只会管理这一份文件：

```text
<CODEX_HOME>/agents/deepseek-evidence-worker.toml
```

## 实际使用时会发生什么

```text
你明确开启一次
      ↓
Codex 把一个边界清楚、以读取为主的任务交给 DeepSeek
      ↓
DeepSeek 返回搜索、摘录或分析结果
      ↓
原来的 Codex 检查结果并回答你
```

你不会进入一个单独的 DeepSeek 聊天。DeepSeek 只在后台处理一次子任务，当前 Codex 始终负责拆分任务、判断结果是否可信，以及给出最后答复。

如果没有显式开关，插件会阻止 DeepSeek 助手启动。这个限制由插件 Hook 检查，不依赖模型“自觉遵守”，所以 Codex 自己写出开关文字也不能给自己授权。

## 什么任务适合交给它

适合：

- 从大量日志中整理时间线和关键错误；
- 在较大的代码库中寻找某类调用、配置或实现位置；
- 批量读取文本后提取事实、清单或摘要；
- 其他范围明确、以读取和搜索为主、结果容易复核的工作。

不适合：

- 修改代码、删除文件或执行其他高风险操作；
- 需要拍板的架构设计和最终验收；
- 图片、视频等非文本任务；
- 包含密钥、个人信息、受监管资料或其他不应发送给 DeepSeek 的内容。

`$use-deepseek-subagent` 表示“允许 Codex 为这条消息考虑使用 DeepSeek”，不是强制派工。如果任务不合适，Codex 可以不启动 DeepSeek。

## 安装前要知道的事

你需要：

- 支持 `UserPromptSubmit`、`PreToolUse` 和 `SubagentStart` Hook 的 Codex；
- Python 3.10 或更高版本；Windows 还需要 `py -3` 可用；
- 自己的 DeepSeek API key；
- 接受任务内容和 DeepSeek 读取到的必要文件内容会发送给 DeepSeek，并可能产生 DeepSeek API 费用。

API key 必须放在操作系统环境变量 `DEEPSEEK_API_KEY` 中，并在启动 Codex 之前配置好。不要把 key 发进聊天、提交到 GitHub 或写进 Agent 配置。如果 key 曾经出现在聊天、日志或截图里，应先去 DeepSeek 控制台撤销并重新生成。

### 怎样设置环境变量

- **Windows 桌面版 Codex：** 在 Windows 搜索“编辑系统环境变量”，打开“环境变量”，新建当前用户变量 `DEEPSEEK_API_KEY`，再完全退出并重新启动 Codex。不要只在 WSL 里执行 `export`；Windows 版 Codex 看不到 WSL 环境变量。
- **macOS、Linux 或 WSL 中的 Codex CLI：** 在准备启动 `codex` 的同一个终端会话中设置并导出 `DEEPSEEK_API_KEY`，然后从这个终端启动 Codex。建议使用不会把真实 key 留在命令历史里的密码提示或系统密钥管理方式。
- **其他桌面启动方式：** 确保变量属于启动 Codex 的那个操作系统用户和桌面会话。只在另一个终端、容器、WSL 或远程主机里设置通常不会生效。

配置环境变量只表示 Codex 具备认证条件，不会自动开启 DeepSeek。仍然必须在每条需要委派的消息最前面写 `$use-deepseek-subagent`。

目前 DeepSeek 助手使用 `deepseek-v4-flash` 和 Responses 接口。DeepSeek 的 `/responses` 路径已经确认存在，但认证后的字段、流式输出和工具调用兼容性仍需要真实测试才可确认。详情见 [Provider 兼容性说明](docs/PROVIDER_COMPATIBILITY.md)。

当前开发和离线联调使用的是 `codex-cli 0.148.0-alpha.15`。版本号不是唯一判断标准：安装后必须能在 `/hooks` 中看到上面三个 Hook；缺少任何一个都不要尝试委派。

## 安装

### 1. 安装公开插件

在终端运行：

```bash
codex plugin marketplace add jinny51/codex-deepseek-subagent --ref main
codex plugin add codex-deepseek-subagent@jinny51-codex
codex plugin list --marketplace jinny51-codex
```

当前公开预览版暂时从 `main` 安装。正式发布 tag 后，安装说明会改为固定版本。

如果你同时使用 Windows Codex 和 WSL Codex，请在真正运行该 Codex、并使用同一份 `CODEX_HOME` 配置的环境中执行这些安装命令。在另一个环境里看到 `plugin list` 成功，不代表当前桌面版已经安装。

安装完成后，完全退出并重新启动 Codex，再新建一个任务。这样 Codex 才能加载新插件，并继承你提前设置的 `DEEPSEEK_API_KEY`。

### 2. 检查安装位置

在新任务中输入：

```text
$setup-deepseek-subagent 请先检查准备安装到哪里，不要修改文件。
```

正常情况下，它只会计划写入：

```text
<CODEX_HOME>/agents/deepseek-evidence-worker.toml
```

### 3. 安装 DeepSeek 助手

确认位置正确后输入：

```text
$setup-deepseek-subagent 安装 DeepSeek 子智能体，并在完成后运行检查。
```

安装器不会修改你的顶层 `config.toml`、个人 `AGENTS.md` 或 Hook 信任设置，也不会调用任何模型。它只检查环境变量这个名字是否存在，不会打印或保存 key 的值，也不能证明 key 有效、有余额或兼容 Responses 接口。

### 4. 检查并信任 Hook

在 Codex 中打开 `/hooks`，确认这个插件提供的三个 Hook：

- `UserPromptSubmit`：检查开关是不是由你写在当前消息最前面；
- `PreToolUse`：在 DeepSeek 启动前检查这次授权并接住任务；
- `SubagentStart`：把这一份任务交给正确的 DeepSeek 助手。

确认无误后按 Codex 的提示信任它们。插件不会替你跳过这个步骤。

完成后再新建一个 Codex 任务，让新注册的 Agent 生效。

## 怎么显式开启

普通消息不会使用 DeepSeek：

```text
帮我检查 logs/ 下的启动错误。
```

把开关写在消息最前面，才允许本条消息使用一次：

```text
$use-deepseek-subagent 请只读分析 logs/ 下的启动失败，整理时间线和关键错误，不要修改文件。
```

DeepSeek 的结果会返回给原来的 Codex，由 Codex 检查后统一回答。

下一条消息即使继续同一个话题，也已经自动关闭：

```text
再看看另一个日志。
```

如果还想使用 DeepSeek，必须再次把 `$use-deepseek-subagent` 写在消息最前面。

下面这些写法都不会开启：

```text
请解释 $use-deepseek-subagent 是什么
“$use-deepseek-subagent 帮我看日志”
$use-deepseek-subagent: 帮我看日志
```

规则故意要求“第一个完整单词必须完全相同”，避免教程文字、引用内容或模型自己的输出意外触发外部调用。一条有效消息最多允许一个 DeepSeek 助手；需要第二个时，请重新发一条带开关的消息。

## 建议的第一次测试

第一次不要使用自己的项目。先在单独目录克隆这个公开仓库：

```bash
git clone https://github.com/jinny51/codex-deepseek-subagent.git
cd codex-deepseek-subagent
```

确认 Codex 当前打开的项目就是这个公开仓库，再把下面 marker 末尾的 `A7C4` 换成你自己的随机字符并发送：

```text
$use-deepseek-subagent 这是一次连通性测试，请实际启动 DeepSeek 子任务。只读扫描 docs/ 下所有 Markdown 文件，返回每个文件的一级标题，最后一行原样输出 DS_SMOKE_A7C4。不要修改文件；如果没有真正启动 deepseek_evidence_worker，请明确报告失败，不要由主任务代做。
```

成功时，你会看到 `deepseek_evidence_worker` 的结果回到当前 Codex，标题清单和 marker 都完整出现，再由 Codex 给出最终回答。只看到主任务自己总结，不能算 DeepSeek 联调通过。

如果出现 `transport_error` 或 DeepSeek 接口错误，不要反复重试。先输入：

```text
$setup-deepseek-subagent 检查 Agent 配置和任务交接状态，不要调用模型。
```

常见原因包括：Codex 没有完全重启、环境变量没有被 Codex 进程继承、Hook 尚未信任、Agent 尚未在新任务中生效，或者 DeepSeek Responses 接口不兼容。

## 数据、权限和费用

当你显式开启时，交给子任务的说明、DeepSeek 为完成任务读取的文件内容，以及相关工具结果会发送到你配置的 DeepSeek 服务。内容如何保留、是否用于训练以及如何计费，取决于你的 DeepSeek 账户和 DeepSeek 的政策。

插件不会保存 API key。正常情况下，任务说明只会在交接的短时间内以本地明文存在，随后删除正文，并留下不含正文的状态记录用于防止重复执行和排查错误。如果交接文件在删除前损坏，它可能连同任务正文一起进入本地隔离区，并保留到用户明确处理。

普通完成记录和已使用授权记录超过 7 天后会进入清理条件，并在插件下一次运行任务、检查或状态清理时删除。如果插件此后不再运行，这些记录可能在本地保留更久。

DeepSeek 助手默认是只读的，这是为了降低误修改风险，不代表保密，也不是绝对的权限隔离。不要把不允许发送给 DeepSeek 的内容交给它。

插件清单中的 `Write` 能力用于安装或卸载这份个人 Agent 配置，并写入短期本地交接状态；它不表示 DeepSeek 助手应当修改你的项目。

详情见 [隐私说明](PRIVACY.md)、[安全说明](SECURITY.md) 和 [完整安全模型](docs/SECURITY_MODEL.md)。

## 更新

刷新公开仓库并重新安装插件：

```bash
codex plugin marketplace upgrade jinny51-codex
codex plugin add codex-deepseek-subagent@jinny51-codex
```

然后在新的 Codex 任务中输入：

```text
$setup-deepseek-subagent 更新由这个插件管理的 DeepSeek 子智能体，并运行检查。
```

更新后完全重启 Codex，并在新任务中使用新版本。

如果 `/hooks` 提示 Hook 内容发生变化，请重新审查后再信任。发布方也必须更新插件版本号，否则 Codex 的安装缓存可能继续使用旧版本。

## 卸载

先在插件仍然启用时输入：

```text
$setup-deepseek-subagent 卸载由这个插件注册的 DeepSeek 子智能体。
```

如果 Agent 文件被手工修改，安装器会拒绝强制删除并说明冲突，这是为了保护用户改动。先确认文件归属和差异，不要为了卸载而直接覆盖。

移除 Agent 和插件不会保证立刻清除所有本地交接记录。卸载前可先让 `$setup-deepseek-subagent` 检查任务交接状态，并处理任何明确列出的隔离项；隔离区中的正文不会被普通卸载静默删除。

再在终端移除插件：

```bash
codex plugin remove codex-deepseek-subagent@jinny51-codex
```

如果以后也不再使用这个公开仓库，可以继续移除 marketplace：

```bash
codex plugin marketplace remove jinny51-codex
```

必须先卸载子智能体再移除插件，因为 Codex 移除插件时不会自动删除那份个人 Agent 配置。

插件也不会替你删除操作系统中的 `DEEPSEEK_API_KEY`。不再使用时，请自行在系统环境变量中移除它。

最后完全退出并重新启动 Codex，打开一个新任务，确认两个 Skill 和三个 Hook 不再出现。删除环境变量也只有在新进程中才会生效。

## 常见问题

### 安装插件后，所有对话都会走 DeepSeek 吗？

不会。插件是全局可用，但每条消息默认关闭。只有消息以 `$use-deepseek-subagent` 开头时，本条消息才有一次使用资格。

### 这是两个项目吗？

不是。它是一个插件。插件中带有两个 Skill，并额外注册一份 DeepSeek Agent 配置。

### 我是在直接和 DeepSeek 聊天吗？

不是。原来的 Codex 仍是主任务。DeepSeek 只完成一个后台子任务，结果会回到 Codex，由 Codex 复核并回答你。

### Codex 能自己决定开启 DeepSeek 吗？

不能。只有你在当前原始消息最前面写出的完整开关才有效。模型输出、工具输出、引用和句中提及都不能授权。

### 为什么不直接在插件里请求 DeepSeek API？

这个项目只补上“把一次任务从当前 Codex 可靠地交给 DeepSeek 助手”这一段。子任务的启动、等待、取消和结果返回仍交给 Codex 自己管理，因此不会另外启动代理服务、第二个 Codex 或常驻后台程序。

### 安装成功就代表一定能调用成功吗？

不代表。安装和离线测试只能证明插件结构、开关和任务交接逻辑正常。DeepSeek 当前接口是否完整兼容 Codex 发出的 Responses 请求，必须通过一次真实端到端测试确认。

## 给维护者的文档

普通使用不需要理解下面这些内部细节：

- [项目为什么这样设计](docs/DESIGN_CONTRACT.md)
- [插件内部流程](docs/ARCHITECTURE.md)
- [安全边界和本地状态](docs/SECURITY_MODEL.md)
- [DeepSeek Provider 兼容性](docs/PROVIDER_COMPATIBILITY.md)
- [测试范围和命令](docs/TESTING.md)
- [参与开发](CONTRIBUTING.md)

离线测试不会读取 Provider 凭据，也不会调用任何模型：

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 -m unittest discover \
  -s plugins/codex-deepseek-subagent/skills/setup-deepseek-subagent/tests \
  -p "test_*.py" -v
```

## 许可证

MIT License。Copyright (c) 2026 jinny51。
