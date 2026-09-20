---
name: notify-me-cursor
description: 安装、绑定 Bark、测试、诊断 Notify Me Cursor。用 /notify-me-cursor 调用。
disable-model-invocation: true
---

# Notify Me Cursor

当前只接 Cursor。Bark 推送 URL 只在终端隐藏输入，不得进入对话、命令参数或日志。日常发通知走 MCP，不走本 Skill。

## 安装

用户即使当前没有打开终端，也要**新弹出**一个 macOS「终端」窗口，并在里面执行下面这条命令（命令会自己跑安装，不要让用户复制粘贴）。把 `<plugin-root>` 换成本插件根目录。

```bash
osascript <<'EOF'
tell application "Terminal"
  activate
  do script "bash -lc 'python3 <plugin-root>/scripts/notify_me.py setup'"
end tell
EOF
```

命令会隐藏读取 Bark URL、发送测试通知，并把 MCP 写入 `~/.cursor/mcp.json`、托管规则写入 `~/.cursor/rules/notify-me.mdc`、Skill 写入 `~/.cursor/skills/notify-me-cursor/`。只有返回 `test=accepted` 才表示 Bark 服务接受了测试请求；这不等于手机已经显示，手机显示需要用户确认。完成后请用户新开一局 Cursor。

未装完时也可在已打开的终端执行：

```text
python3 <plugin-root>/scripts/notify_me.py setup
```

## 诊断

```text
python3 <plugin-root>/scripts/notify_me.py doctor
```

已有绑定时可单独测试：

```text
python3 <plugin-root>/scripts/notify_me.py test
```
