# 测试与质量门禁

本项目的自动测试只使用 Python 标准库，不安装第三方依赖，也不调用 OpenAI、DeepSeek
或其他网络 Provider。测试不会读取环境中的 API key；任何真实凭据都不应出现在测试参数、
fixture、日志或仓库文件中。

## 本地运行

完整门禁需要 Python 3.11 或 3.12。在仓库根目录执行：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
python -m unittest discover \
  -s plugins/codex-deepseek-bridge/skills/setup-deepseek-worker/tests \
  -p "test_*.py" -v
```

第一条命令运行 bridge 协议测试和仓库级结构门禁；第二条命令在隔离的临时 Codex home
中验证 setup 工具。测试不得修改真实个人 Codex 配置。

bridge 和 setup 实现仍兼容 Python 3.10；在 3.10 上执行测试发现时，依赖标准库
`tomllib` 的两项仓库结构检查会明确跳过。CI 始终使用 3.11/3.12，因此发布门禁不会
跳过 TOML 检查。

## CI 矩阵

GitHub Actions 在以下组合上运行相同测试：

- Ubuntu、Windows 和 macOS；
- Python 3.11 和 Python 3.12。

CI 使用只读仓库权限，不注入 Provider 凭据，不执行付费 smoke test。

## 仓库级静态门禁

`tests/test_project_structure.py` 检查：

- 所有 JSON 与 TOML 文件能被标准解析器读取；
- 插件 manifest、插件目录和 marketplace 条目名称一致；
- manifest 使用严格语义版本，并只引用实际存在的 companion 文件；
- 两个 Skill 的名称、目录、frontmatter 与 `agents/openai.yaml` 保持一致；
- 仓库只发布一个 `deepseek_evidence_worker` Agent 身份；
- bridge 协议 namespace 只有 `codex-deepseek-bridge/v1`；
- Hook 同时具备 spawn 前拦截和 child 启动交付入口，且所有
  `additionalContextLimit` 都是正整数；
- 不出现旧实验角色名、常见真实 token 格式、私钥头或具体 API key 赋值；
- CI 没有漏掉任何受支持操作系统或 Python 版本。

这些是产品边界测试，不代替运行时协议测试。bridge 测试仍需覆盖任务大小限制、关联、
并发、原子状态转换、过期、隔离、失败阻断和不确定交付；setup 测试仍需覆盖
`plan/install/doctor/uninstall`、所有权验证、冲突保护、幂等与卸载安全。

## 付费 smoke test

真实 Provider smoke 不属于 CI，也不应在安装过程中自动发生。需要验证真实 child 时，
必须由用户明确发起，并在运行前确认目标 Provider、费用、数据范围和当前 Hook 信任状态。
