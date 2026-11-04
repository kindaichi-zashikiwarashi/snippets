#!/usr/bin/env bash
# clip_tts 用の GNOME グローバルショートカットを登録/解除する。
#
# X11 では passive リスナー(pynput)はキーを消費できずアプリに漏れる(印刷ダイアログ誤爆等)ため、
# OS(GNOME) のカスタムキーバインドにコマンドを割り当てて OS 側でキーを専有させる。
# コマンドは常駐中の clip_tts.py にシグナルを送る (pkill -USR1/-USR2)。
#
# 使い方:
#   clip_tts_gnome_hotkey.sh            # 登録 (既定: 再生=Ctrl+Shift+P / 停止=Ctrl+Shift+S)
#   clip_tts_gnome_hotkey.sh --play '<Super>p' --stop '<Super>s'   # キー変更
#   clip_tts_gnome_hotkey.sh --remove   # 解除
#
# 前提: GNOME (gsettings)。事前に clip_tts を常駐させておく (例: clip_tts -v calm)。
set -euo pipefail

PLAY_KEY='<Control><Alt>p'
STOP_KEY='<Control><Alt>s'
REMOVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --play) PLAY_KEY="$2"; shift 2;;
    --stop) STOP_KEY="$2"; shift 2;;
    --remove) REMOVE=1; shift;;
    -h|--help) grep '^#' "$0" | sed 's/^# \?//'; exit 0;;
    *) echo "不明な引数: $1" >&2; exit 1;;
  esac
done

command -v gsettings >/dev/null 2>&1 || { echo "gsettings が無い (GNOME 専用)" >&2; exit 1; }

SCHEMA=org.gnome.settings-daemon.plugins.media-keys
CK=org.gnome.settings-daemon.plugins.media-keys.custom-keybinding
BASE=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
PLAY_PATH="$BASE/clip-tts-play/"
STOP_PATH="$BASE/clip-tts-stop/"

# clip_tts.py への pkill パターン。'[.]' により、この登録コマンド自身の
# argv 文字列にはマッチせず (自己巻き込み回避)、実プロセスだけに当たる。
PAT='clip_tts[.]py'

# 現在の custom-keybindings リスト (GVariant) を bash 配列っぽく扱う
current="$(gsettings get "$SCHEMA" custom-keybindings 2>/dev/null || echo "@as []")"

list_without() {  # $1 を除いた要素を1行ずつ返す
  echo "$current" | grep -oE "'[^']+'" | tr -d "'" | grep -vx "$1" || true
}

join_variant() {  # 標準入力の各行 → ['a', 'b'] 形式
  local items=() line
  while IFS= read -r line; do [ -n "$line" ] && items+=("'$line'"); done
  local IFS=,; echo "[${items[*]}]"
}

if [ "$REMOVE" = 1 ]; then
  new="$( { list_without "$PLAY_PATH"; echo "$STOP_PATH"; } | grep -vx "$STOP_PATH" | join_variant )"
  gsettings set "$SCHEMA" custom-keybindings "$new"
  echo "clip_tts のショートカットを解除しました。"
  exit 0
fi

# リストに2パスを追加 (重複排除)
new="$( { list_without "$PLAY_PATH" | grep -vx "$STOP_PATH"; echo "$PLAY_PATH"; echo "$STOP_PATH"; } | join_variant )"
gsettings set "$SCHEMA" custom-keybindings "$new"

gsettings set "$CK:$PLAY_PATH" name 'clip_tts 再生'
gsettings set "$CK:$PLAY_PATH" command "pkill -USR1 -f '$PAT'"
gsettings set "$CK:$PLAY_PATH" binding "$PLAY_KEY"

gsettings set "$CK:$STOP_PATH" name 'clip_tts 停止'
gsettings set "$CK:$STOP_PATH" command "pkill -USR2 -f '$PAT'"
gsettings set "$CK:$STOP_PATH" binding "$STOP_KEY"

echo "登録しました:"
echo "  再生: $PLAY_KEY → pkill -USR1 -f '$PAT'"
echo "  停止: $STOP_KEY → pkill -USR2 -f '$PAT'"
echo "常駐中の clip_tts があれば、どのアプリからでも上記キーで再生/停止できます。"
echo "解除: $0 --remove"
