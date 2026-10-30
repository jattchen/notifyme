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

安装后首次配置或诊断时显式调用插件 Skill，再在真实终端中运行插件根目录的 `scripts/notify_me.py setup`。该命令会隐藏读取 Bark URL、发送测试通知，并把 Notify Me Codex 托管规则写入 `~/.codex/AGENTS.md`。诊断运行插件根目录的 `scripts/notify_me.py doctor`。

正常通知必须由当前顶层主 Agent 直接调用 `mcp__notifyme_codex__notifyme`，并传入扁平的 `condition`、`item_id`、`state`、`message` 和当前项目绝对路径 `workspace`。只有 `status=accepted` 才能说明 Bark 服务接受了请求；Bark URL 不得进入对话、命令参数或日志。

## Cursor 适配版

Cursor 使用独立的 `notify-me-cursor` 插件身份，不覆盖或复用 Grok / Codex。正常通知通过本地 stdio MCP 服务器 `notifyme_cursor` 的工具 `notifyme` 发送。

安装后首次配置或诊断时显式调用插件 Skill，再弹出真实终端运行插件根目录的 `scripts/notify_me.py setup`。该命令会隐藏读取 Bark URL、发送测试通知，把 MCP 写入 `~/.cursor/mcp.json`，把托管规则写入 `~/.cursor/rules/notify-me.mdc`，并把 Skill 写到 `~/.cursor/skills/notify-me-cursor/`。诊断运行插件根目录的 `scripts/notify_me.py doctor`。

正常通知必须由当前顶层主 Agent 直接调用 MCP 服务器 `notifyme_cursor` 的工具 `notifyme`，并传入扁平的 `condition`、`item_id`、`state`、`message` 和当前项目绝对路径 `workspace`。只有 `status=accepted` 才能说明 Bark 服务接受了请求；Bark URL 不得进入对话、命令参数或日志。完成后请新开一局 Cursor。

## Claude Code 适配版

Claude Code 使用独立的 `notify-me-claude` 插件身份，不覆盖或复用 Grok / Codex / Cursor。本仓库根目录就是 Claude Code 插件市场 `notifyme`，插件自带本地 stdio MCP 服务器 `notifyme_claude`，工具名为 `mcp__plugin_notify-me-claude_notifyme_claude__notifyme`。

```bash
claude plugin marketplace add jattchen/notifyme
claude plugin install notify-me-claude@notifyme
```

安装后在 Claude Code 里显式调用 `/notify-me-claude`，它会运行插件根目录的 `scripts/notify_me.py open-setup`，弹出真实终端跑 `setup`，并把当前会话的 `CLAUDE_CONFIG_DIR` 带过去。`setup` 会隐藏读取 Bark URL、发送测试通知，先把上述工具加入 `~/.claude/settings.json` 的 `permissions.allow`，免得用户离开时通知卡在授权弹窗，再把托管规则写入 `~/.claude/rules/notify-me-claude.md`（每个项目都会加载）。`settings.json` 无法合并时，`setup` 在询问 URL 之前就停下，不写任何文件；插件未安装或未启用时会给出警告。插件本身不能带常驻规则，所以这一步不能省。诊断运行插件根目录的 `scripts/notify_me.py doctor`。

正常通知必须由当前顶层主 Agent 直接调用该 MCP 工具，并传入扁平的 `condition`、`item_id`、`state`、`message` 和当前项目绝对路径 `workspace`。只有 `status=accepted` 才能说明 Bark 服务接受了请求；Bark URL 不得进入对话、命令参数或日志。完成后请新开一局 Claude Code。

## 应用命令行

aiusage 这类本机程序使用独立的 `notify-me` 命令，不走上面的 Agent 插件。协议是版本 1：标准输出一行 JSON，成功时退出码为 0。

它负责排队、查询、取消和有限次发送。不能保证手机只收到一次。租约到期后的取消自己写成已取消，并记住远端结果可能已经发出；查询只读，不会声称手机上的通知已被撤回。取消一个还不存在的事件会留下墓碑。墓碑不占活跃名额，但总行数满了会明确失败。

安装只替换这个应用自己的启动程序。schema 8 在明确升级前只读，升级后的 schema 9 旧程序认不出来，因此已经投递过就不能把数据库退回去。细节、命令和恢复规则见 [docs/application-cli.md](docs/application-cli.md)。

需要 Python 3.9 或更高，标准库即可。本次不把程序装到真实用户目录，也不发送真实 Bark 通知。
