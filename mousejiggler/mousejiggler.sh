#!/usr/bin/env bash
# git bash から実行するマウスジグラー
#
# 使い方:
#   ./mousejiggler.sh                 # 既定（10秒ごと / 5.5cm）
#   ./mousejiggler.sh -IntervalSeconds 15 -DistanceCm 6
# 止めるとき: Ctrl + C
#
# ※ Windows上でマウスを動かすには Win32 API が必要なため、
#    実際の動作は同梱の MouseJiggler.ps1 を powershell 経由で呼び出しています。
#    入口は git bash です。

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PS1_PATH="$DIR/MouseJiggler.ps1"

if [ ! -f "$PS1_PATH" ]; then
  echo "MouseJiggler.ps1 が見つかりません: $PS1_PATH" >&2
  exit 1
fi

# git bash のパスを Windows 形式へ変換
WIN_PS1="$(cygpath -w "$PS1_PATH")"

# Ctrl+C はそのまま powershell の子プロセスへ伝わり、停止します
exec powershell.exe -ExecutionPolicy Bypass -NoProfile -File "$WIN_PS1" "$@"
