#!/usr/bin/env bash
set -euo pipefail

# 使い方:
#   ./watch_and_play.sh <監視ディレクトリ> [ポーリング間隔(秒)]
#
# 動作:
#   指定ディレクトリを監視し、新しいwavファイルが来たら自動再生する。
#   再生済みファイルは記録し、同じファイルは2回再生しない。
#
# キー操作:
#   s/スペース  一時停止 / 再開
#   a           巻き戻し（10秒）
#   d           早送り（10秒）
#   r           最後に再生したファイルを最初からリプレイ
#   p           前のファイルを再生
#   l           再生履歴を表示
#   q           終了
#   Ctrl+C      終了
#
# 例:
#   ./watch_and_play.sh ./output          # outputディレクトリを5秒間隔で監視
#   ./watch_and_play.sh ./output 3        # 3秒間隔
#   ./watch_and_play.sh ./output 1        # 1秒間隔（ほぼリアルタイム）

dir="${1:-}"
interval="${2:-5}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ffplay の探索（play_latest_wav.sh と同じロジック）
preferred_ffmpeg_bin="${FFMPEG_BIN:-$HOME/ffmpeg/bin}"
cache_dir="$script_dir/.ffmpeg"

resolve_ffplay() {
  # A. PATH 上（Linux ではこれで見つかる）
  if command -v ffplay >/dev/null 2>&1; then
    command -v ffplay
    return 0
  fi
  # B. Windows: 既存 ffmpeg/bin
  if [[ -x "$preferred_ffmpeg_bin/ffplay.exe" ]]; then
    printf '%s\n' "$preferred_ffmpeg_bin/ffplay.exe"
    return 0
  fi
  if [[ -x "$script_dir/ffplay.exe" ]]; then
    printf '%s\n' "$script_dir/ffplay.exe"
    return 0
  fi
  if [[ -x "$cache_dir/bin/ffplay.exe" ]]; then
    printf '%s\n' "$cache_dir/bin/ffplay.exe"
    return 0
  fi
  echo "ffplay not found. Install ffmpeg or run play_latest_wav.sh first." >&2
  exit 3
}

if [[ -z "$dir" ]]; then
  echo "usage: $0 <directory> [interval_seconds]" >&2
  exit 1
fi

mkdir -p "$dir"

ffplay_bin="$(resolve_ffplay)"

# 再生済みファイルを記録する一時ファイル
played_list="$(mktemp)"
trap 'rm -f "$played_list"' EXIT

# 再生履歴（順番を保持）
declare -a play_history=()
last_played=""

# 一時停止状態
paused_file=""
paused_pos=0
SEEK_STEP=10  # 早送り/巻き戻しの秒数

play_file() {
  local wav="$1"
  local start_pos="${2:-0}"
  local filename="$(basename "$wav")"
  local size
  size=$(stat -c%s "$wav" 2>/dev/null || stat -f%z "$wav" 2>/dev/null || echo "0")

  local ffplay_arg ffplay_bin_arg
  if command -v cygpath >/dev/null 2>&1; then
    ffplay_arg="$(cygpath -w "$wav")"
    ffplay_bin_arg="$(cygpath -w "$ffplay_bin")"
  else
    ffplay_arg="$wav"
    ffplay_bin_arg="$ffplay_bin"
  fi

  while true; do
    if [[ "$start_pos" == "0" ]]; then
      echo "[再生] $filename ($(( size / 1024 ))KB)"
    else
      echo "[再開] $filename (${start_pos}秒から)"
    fi

    if [[ "$start_pos" != "0" ]]; then
      "$ffplay_bin_arg" -nodisp -autoexit -loglevel error -hide_banner -ss "$start_pos" "$ffplay_arg" \
        </dev/null >/dev/null 2>&1 &
    else
      "$ffplay_bin_arg" -nodisp -autoexit -loglevel error -hide_banner "$ffplay_arg" \
        </dev/null >/dev/null 2>&1 &
    fi
    ffplay_pid=$!
    local play_start
    play_start=$(date +%s)
    local action="done"

    # 再生中のキー操作
    while kill -0 "$ffplay_pid" 2>/dev/null; do
      local key=""
      read -s -t 0.5 -n 1 key 2>/dev/null || true
      case "$key" in
        s|S|" ")
          action="pause"
          break
          ;;
        d|D)
          action="forward"
          break
          ;;
        a|A)
          action="rewind"
          break
          ;;
      esac
    done

    # ffplay停止
    if kill -0 "$ffplay_pid" 2>/dev/null; then
      kill "$ffplay_pid" 2>/dev/null || true
    fi
    wait "$ffplay_pid" 2>/dev/null || true

    # 経過時間を計算
    local now elapsed
    now=$(date +%s)
    elapsed=$((now - play_start + start_pos))

    case "$action" in
      pause)
        paused_file="$wav"
        paused_pos=$elapsed
        echo "[一時停止] ${elapsed}秒"
        return 0
        ;;
      forward)
        start_pos=$((elapsed + SEEK_STEP))
        echo "[早送り] → ${start_pos}秒"
        # ループ継続 → 新しいstart_posで再生
        ;;
      rewind)
        start_pos=$((elapsed - SEEK_STEP))
        if [[ $start_pos -lt 0 ]]; then start_pos=0; fi
        echo "[巻き戻し] → ${start_pos}秒"
        # ループ継続
        ;;
      done)
        paused_file=""
        paused_pos=0
        return 0
        ;;
    esac
  done
}

show_help() {
  echo ""
  echo "  s/スペース: 一時停止 / 再開"
  echo "  a: 巻き戻し (${SEEK_STEP}秒)"
  echo "  d: 早送り (${SEEK_STEP}秒)"
  echo "  r: リプレイ（最初から）"
  echo "  p: 前の音声を再生"
  echo "  l: 再生履歴を表示"
  echo "  q: 終了"
  echo ""
}

# 起動時: 最新のwavを1つ再生し、残りは「再生済み」として登録
if ls "$dir"/*.wav >/dev/null 2>&1; then
  latest="$(ls -1t "$dir"/*.wav | head -n 1)"
  # 最新以外を再生済みに登録
  ls -1t "$dir"/*.wav | tail -n +2 >> "$played_list" 2>/dev/null || true
fi

echo "=== TTS Watch & Play ==="
echo "監視: $dir"
echo "間隔: ${interval}秒"
echo "ffplay: $ffplay_bin"
echo "キー: s=一時停止/再開 a=巻き戻し d=早送り r=リプレイ p=前 l=履歴 q=終了"
echo "========================"
echo ""
echo "新しいwavファイルを待っています..."

# 起動時に最新ファイルを再生
if [[ -n "${latest:-}" && -f "$latest" ]]; then
  play_file "$latest"
  echo "$latest" >> "$played_list"
  play_history+=("$latest")
  last_played="$latest"
fi

while true; do
  # 新しいwavファイルを時刻順で取得
  for wav in $(ls -1t "$dir"/*.wav 2>/dev/null); do
    # 再生済みならスキップ
    if grep -qF "$wav" "$played_list" 2>/dev/null; then
      continue
    fi

    # ファイルサイズが0でないか確認（書き込み中の可能性）
    size=$(stat -c%s "$wav" 2>/dev/null || stat -f%z "$wav" 2>/dev/null || echo "0")
    if [[ "$size" -eq 0 ]]; then
      continue
    fi

    # 少し待ってサイズが変わらないか確認（書き込み完了待ち）
    sleep 0.5
    size2=$(stat -c%s "$wav" 2>/dev/null || stat -f%z "$wav" 2>/dev/null || echo "0")
    if [[ "$size" != "$size2" ]]; then
      continue
    fi

    # 再生
    play_file "$wav"

    # 記録
    echo "$wav" >> "$played_list"
    play_history+=("$wav")
    last_played="$wav"
  done

  # キー入力チェック（ノンブロッキング）
  key=""
  read -s -t "$interval" -n 1 key 2>/dev/null || true

  case "$key" in
    s|S|" ")
      # 一時停止中なら再開
      if [[ -n "$paused_file" && -f "$paused_file" ]]; then
        play_file "$paused_file" "$paused_pos"
      else
        echo "(一時停止中のファイルがありません)"
      fi
      ;;
    r|R)
      # リプレイ: 最後に再生したファイルを最初から
      if [[ -n "$last_played" && -f "$last_played" ]]; then
        echo "[リプレイ]"
        paused_file=""
        paused_pos=0
        play_file "$last_played"
      else
        echo "(再生履歴がありません)"
      fi
      ;;
    p|P)
      # 前のファイル: 履歴の一つ前を再生
      hist_len=${#play_history[@]}
      if [[ $hist_len -ge 2 ]]; then
        prev="${play_history[$((hist_len - 2))]}"
        if [[ -f "$prev" ]]; then
          last_played="$prev"
          echo "[前の音声]"
          play_file "$prev"
        fi
      elif [[ $hist_len -eq 1 ]]; then
        echo "(これが最初の音声です)"
        play_file "${play_history[0]}"
      else
        echo "(再生履歴がありません)"
      fi
      ;;
    l|L)
      # 履歴表示
      echo ""
      echo "--- 再生履歴 ---"
      if [[ ${#play_history[@]} -eq 0 ]]; then
        echo "  (なし)"
      else
        for i in "${!play_history[@]}"; do
          marker=""
          if [[ "${play_history[$i]}" == "$last_played" ]]; then
            marker=" <<"
          fi
          echo "  $((i+1)). $(basename "${play_history[$i]}")${marker}"
        done
      fi
      echo "----------------"
      ;;
    q|Q)
      echo "終了します。"
      exit 0
      ;;
    "?"|h|H)
      show_help
      ;;
    "")
      # タイムアウト（キー入力なし）→ 次のポーリングへ
      ;;
    *)
      ;;
  esac
done
