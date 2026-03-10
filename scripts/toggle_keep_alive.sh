#!/bin/bash
# 一键切换保活状态 — 发送 SIGUSR1 给 mail-sync 进程
# 用法: bash scripts/toggle_keep_alive.sh
# 绑定 macOS 快捷键: 快捷指令 → 运行 Shell 脚本

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PID=$(pm2 pid mail-sync 2>/dev/null)

if [[ -z "$PID" || "$PID" == "0" ]]; then
  echo "mail-sync 未运行"
  osascript -e 'display notification "mail-sync 未运行" with title "MailAgent"' 2>/dev/null
  exit 1
fi

kill -USR1 "$PID"
echo "已发送 SIGUSR1 给 PID=$PID"
osascript -e 'display notification "保活状态已切换" with title "MailAgent Keep-Alive"' 2>/dev/null
