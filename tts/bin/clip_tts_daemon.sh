#!/usr/bin/env bash
# clip_tts を「ターミナルなしで」常駐起動するラッパー (GUI ランチャー/autostart から呼ぶ用)。
#
# サーバの起動・所有・終了時停止(VRAM解放)・二重起動防止は clip_tts.py 自身が行うので、
# ここは「端末なしで clip_tts を起動する」だけの薄いラッパー。
#
# 使い方 (通常は clip_tts.desktop 経由でクリック起動):
#   clip_tts_daemon.sh [voice]        # 既定 voice=calm
set -u
VOICE="${1:-calm}"
SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
BIN_DIR="$(cd "$(dirname "$SELF")" && pwd)"

if pgrep -f "clip_tts[.]py" >/dev/null 2>&1; then
  echo "clip_tts はすでに起動しています" >&2
  exit 0
fi

# 通知(notify-send)は出さない: 他の通知に紛れて邪魔になるため。
# clip_tts が: サーバ自動起動→所有→常駐→終了時にサーバ停止(VRAM解放) まで面倒を見る。
# 端末なし・prefetch オフ・操作は OS ショートカット(シグナル)。
exec "$BIN_DIR/clip_tts" -v "$VOICE" --no-prefetch < /dev/null
