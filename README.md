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
