---
name: notify-me-claude
description: 安装、绑定 Bark、测试、诊断 Notify Me Claude。用 /notify-me-claude 调用。
disable-model-invocation: true
---

# Notify Me Claude

当前只接 Claude Code。Bark 推送 URL 只在终端隐藏输入，不得进入对话、命令参数或日志。日常发通知走 MCP 工具 `mcp__plugin_notify-me-claude_notifyme_claude__notifyme`，不走本 Skill。

下文 `<plugin-root>` 是本 Skill 所在目录（加载时显示的 Base directory）往上两级的绝对路径，即包含 `scripts/notify_me.py` 的目录。

## 安装

插件未装时先在终端执行：

```text
claude plugin marketplace add jattchen/notifyme
claude plugin install notify-me-claude@notifyme
```

## 绑定 Bark

用户即使当前没有打开终端，也要**新弹出**一个 macOS「终端」窗口，并在里面执行下面这条命令（命令会自己跑配置，不要让用户复制粘贴）。

```bash
osascript <<'OSA'
tell application "Terminal"
  activate
  do script "bash -lc 'python3 <plugin-root>/scripts/notify_me.py setup'"
end tell
OSA
```

命令会隐藏读取 Bark URL、发送测试通知，把托管规则写入 `~/.claude/rules/notify-me-claude.md`，并把上述 MCP 工具加入 `~/.claude/settings.json` 的 `permissions.allow`，免得用户离开时通知卡在授权弹窗。只有返回 `test=accepted` 才表示 Bark 服务接受了测试请求；这不等于手机已经显示，手机显示需要用户确认。完成后请用户新开一局 Claude Code。

## 诊断

```text
python3 <plugin-root>/scripts/notify_me.py doctor
```

`bound`、`rule_managed`、`permission_allowed`、`plugin_installed`、`plugin_enabled` 都应为 `true`。

已有绑定时可单独测试：

```text
python3 <plugin-root>/scripts/notify_me.py test
```
