---
name: notify-me
description: 安装、绑定 Bark、测试、诊断 Grok Notify Me。用 /notify-me 调用。
disable-model-invocation: true
---

# Notify Me

当前只接 Grok。Bark 推送 URL 只在终端隐藏输入，不得进入对话、命令参数或日志。

## 安装

按仓库 README 的「Agent 安装」：用 osascript **弹出新的「终端」窗口**跑安装命令。用户不必先自己开终端，也不要让用户复制多条命令。完成标准与 README 相同。

未装完时也可在已打开的终端执行：

```text
python3 ~/.grok/notify-me install
```

## 诊断

```text
python3 ~/.grok/notify-me doctor
```
