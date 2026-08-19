# Provider 兼容性

## 当前约束

当前 Codex 自定义模型 Provider 使用 Responses wire API。`deepseek_evidence_worker` 因而要求目标 DeepSeek endpoint 能接收 Codex 发出的 Responses 请求，并支持任务所需的工具调用。

DeepSeek 面向 V4 的公开 API 参考主要描述 `/chat/completions` 和 Anthropic 兼容接口，没有在同一 V4 说明中完整承诺 Responses 字段兼容范围。因此不能仅凭模型名称或一个可访问 URL 就宣称集成稳定。

## 2026-08-19 的无凭据探针

在不发送 API key 和任务内容的情况下：

- `OPTIONS https://api.deepseek.com/responses` 返回 HTTP 200，并带有 `Allow: POST`；
- 对随机不存在路径执行相同请求，返回中没有 `Allow: POST`；
- 无认证 `POST /responses` 返回 401，因此只能确认路由存在，不能确认认证后的请求字段、流式事件、工具调用或错误恢复兼容性。

这个结果说明 `/responses` 不是必然 404，但不构成可用性证明。

## 发布门槛

正式声明某个 DeepSeek/Codex 组合可用前，必须通过用户明确发起的低成本 smoke：

1. OpenAI 主 Agent保持原模型和 Provider；
2. PreToolUse 捕获带随机 marker 的 assignment；
3. DeepSeek child 收到 JSON bridge contract；
4. child 至少完成一次受控工具调用并返回 marker；
5. 结果通过 Codex 原生 callback 回到父任务；
6. assignment 状态已擦除且无 quarantine；
7. 未发生 Provider、模型或传输 fallback。

安装、CI 和无凭据 endpoint 探针都不能代替这项真实 smoke。

## 失败策略

如果 DeepSeek Responses 路径不可用或不兼容，本项目应明确报告 Provider boundary failure。不得在后台启动 ChatCompletions 代理、直接 HTTP 客户端、MCP 转发、第二个 Codex CLI 或其他 Provider 来伪造成功。

可接受的后续方向只有：

- 等待或使用 DeepSeek 官方兼容的 Responses 路径；
- 用户明确选择并接受另一个 Responses-compatible Provider 及其独立数据/费用边界；
- 等待 Codex 官方重新提供受支持的 ChatCompletions custom-provider wire。
