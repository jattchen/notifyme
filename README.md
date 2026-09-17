# Notify Me

Grok 卡住、遇到严重风险、或这一次要的事做完时，往 iPhone 上的 [Bark](https://bark.day.app) 推一条短通知。

**用法：** 把本仓库发给你的 Grok，说「帮我安装 Notify Me」。

用户只需在弹出的终端里粘贴一次 Bark 推送 URL。不要把 URL 发到对话里。

需要：Grok（macOS）、`python3`、Bark。仓库公开前，本机还需要已登录的 `gh`。

## Agent 安装

用户即使当前没有打开终端，也要**新弹出**一个 macOS「终端」窗口，并在里面执行下面这条命令（命令会自己跑安装，不要让用户复制粘贴）。

```bash
osascript <<'EOF'
tell application "Terminal"
  activate
  do script "bash -lc 'gh api repos/jattchen/notifyme/contents/install.sh -H \"Accept: application/vnd.github.raw\" | bash'"
end tell
EOF
```

仓库公开后也可用：

```bash
osascript <<'EOF'
tell application "Terminal"
  activate
  do script "bash -lc 'curl -fsSL https://raw.githubusercontent.com/jattchen/notifyme/main/install.sh | bash'"
end tell
EOF
```

完成标准：用户在该窗口输入了 Bark URL，窗口显示绑定成功、测试通知已发送、已写入 AGENTS。然后告诉用户新开一局 Grok。Bark URL 不得出现在对话或工具参数里。

## 命令行安装

在已经打开的终端里执行同一套安装：

```bash
gh api repos/jattchen/notifyme/contents/install.sh -H "Accept: application/vnd.github.raw" | bash
```

公开后：

```bash
curl -fsSL https://raw.githubusercontent.com/jattchen/notifyme/main/install.sh | bash
```

诊断：`python3 ~/.grok/notify-me doctor`

## Codex 适配版

Codex 使用独立的 `notify-me-codex` 插件身份，不覆盖或复用 Grok 的 `notify-me`。插件包含标准 Agent Plugins `plugin.json`/`mcp.json`，并保留 `.codex-plugin/` 兼容清单；正常通知通过本地 stdio MCP 工具 `notifyme` 发送。

安装后首次配置由插件 Skill 引导，在真实终端中运行插件根目录的 `scripts/notify_me.py setup`。该命令会隐藏读取 Bark URL、发送测试通知，并把 Notify Me Codex 托管规则写入 `~/.codex/AGENTS.md`。诊断运行插件根目录的 `scripts/notify_me.py doctor`。

正常通知必须由当前顶层主 Agent 直接调用 `mcp__notifyme_codex__notifyme`，并带上 `op`、`condition`、`item_id`、`state`、`message` 和当前项目绝对路径 `workspace`。只有 `status=accepted` 才能说明 Bark 服务接受了请求；Bark URL 不得进入对话、命令参数或日志。
