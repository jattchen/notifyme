---
name: notify-me-codex
description: 安装、绑定 Bark、测试、诊断 Notify Me Codex；正常通知直接使用 MCP 工具。
---

# Notify Me Codex

Notify Me Codex 只负责本地激活、私密 Bark 绑定和按托管规则发送通知。Bark URL 只能在真实终端中隐藏输入，不得进入对话、命令参数或日志。

## 首次配置

先确认当前插件已经启用，再在当前插件根目录运行：

```text
python3 <notify-me-codex-plugin-root>/scripts/notify_me.py setup
```

命令会验证地址、发送测试通知，并把带版本标记的规则写入 `~/.codex/AGENTS.md`。只有返回 `test=accepted` 才表示 Bark 服务接受了测试请求；这不等于手机已经显示，手机显示需要用户确认。

诊断使用：

```text
python3 <notify-me-codex-plugin-root>/scripts/notify_me.py doctor
```

已有绑定时可单独测试：

```text
python3 <notify-me-codex-plugin-root>/scripts/notify_me.py test
```

## 正常通知

正常通知直接调用 `mcp__notifyme_codex__notifyme`，不要先加载本 Skill，也不要调用 CLI。请求必须包含以下扁平字段：

- `condition`: `answer`、`auth`、`action`、`severe-risk` 或 `done`
- `item_id`: 当前事件的稳定标识
- `state`: 当前语义状态的稳定标识
- `message`: 简短、面向用户、使用用户语言的正文
- `workspace`: 当前项目根目录的绝对路径
- `url`: 可选。用户点推送时打开的 http/https 网页，不是 Bark 设备地址。第一次发送就要带上。

只有当前顶层主 Agent 可以发送。子 Agent、委派 Agent 和普通进度不发送；中间步骤不能使用 `done`。只有 `ok=true` 且 `status=accepted` 才能说 Bark 服务已接受通知；`deduplicated`、`failed` 或错误结果都不能声称已发送。

触发条件：

- `answer`: 缺少必要信息或选择，且主线暂时无法继续。
- `auth`: 等待用户授权、许可或凭据。
- `action`: 必须由用户在对话外完成操作。
- `severe-risk`: 继续可能造成灾难性或大范围不可逆后果。
- `done`: 用户这一次要求的整件事已经完成。

同一事件重试时保持完全相同的 `item_id`、`state`、`condition` 和 `workspace`。不要传入 Bark 设备地址、任务全文、内部日志或密钥。
