#!/usr/bin/env bash
set -euo pipefail

# 使い方:
#   ./play_latest_wav.sh <wavを探すディレクトリ>
#
# 動作:
#   1. 最新の .wav を1つ探す
#   2. ffplay で無表示再生
#
# ffplay の探索順:
#   A. FFMPEG_BIN (未設定なら ~/ffmpeg/bin)
#   B. スクリプトと同じディレクトリの ffplay.exe
#   C. PATH 上の ffplay
#   D. どれも無ければ curl で取得

dir="${1:-}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# FFMPEG_BIN (未設定なら ~/ffmpeg/bin) を最優先で使う
preferred_ffmpeg_bin="${FFMPEG_BIN:-$HOME/ffmpeg/bin}"

# ダウンロード先キャッシュ
cache_dir="$script_dir/.ffmpeg"
download_url="https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

if [[ -z "$dir" || ! -d "$dir" ]]; then
  echo "usage: $0 <directory>" >&2
  exit 1
fi

download_ffplay_bundle() {
  # Linux なら apt で入れるよう案内
  if [[ "$(uname -s)" != MINGW* && "$(uname -s)" != MSYS* && "$(uname -s)" != CYGWIN* ]]; then
    echo "ffplay not found. Install ffmpeg:" >&2
    echo "  sudo apt install -y ffmpeg" >&2
    exit 3
  fi

  local tmp zipfile ffplay_path bundle_root

  tmp="$(mktemp -d)"
  zipfile="$tmp/ffmpeg.zip"

  echo "ffplay not found. Downloading FFmpeg essentials build..." >&2
  curl -L --fail --retry 3 -o "$zipfile" "$download_url"

  if command -v tar >/dev/null 2>&1; then
    tar -xf "$zipfile" -C "$tmp"
  elif command -v unzip >/dev/null 2>&1; then
    unzip -q "$zipfile" -d "$tmp"
  else
    echo "Need 'tar' or 'unzip' to extract ffmpeg zip." >&2
    rm -rf "$tmp"
    exit 3
  fi

  ffplay_path="$(find "$tmp" -type f -path '*/bin/ffplay.exe' | head -n 1 || true)"
  if [[ -z "$ffplay_path" ]]; then
    echo "ffplay.exe was not found in downloaded archive." >&2
    rm -rf "$tmp"
    exit 4
  fi

  bundle_root="$(cd "$(dirname "$ffplay_path")/.." && pwd)"

  rm -rf "$cache_dir"
  mv "$bundle_root" "$cache_dir"
  rm -rf "$tmp"

  printf '%s\n' "$cache_dir/bin/ffplay.exe"
}

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

  # C. スクリプトと同じ場所
  if [[ -x "$script_dir/ffplay.exe" ]]; then
    printf '%s\n' "$script_dir/ffplay.exe"
    return 0
  fi

  # D. キャッシュ済み
  if [[ -x "$cache_dir/bin/ffplay.exe" ]]; then
    printf '%s\n' "$cache_dir/bin/ffplay.exe"
    return 0
  fi

  # E. ダウンロード（Windows）/ apt案内（Linux）
  download_ffplay_bundle
}

ffplay_bin="$(resolve_ffplay)"

# 最新の wav を1つだけ拾う
latest="$(ls -1t -- "$dir"/*.wav 2>/dev/null | head -n 1 || true)"

if [[ -z "$latest" ]]; then
  echo "No .wav file found in: $dir" >&2
  exit 2
fi

# Windows exe に渡すので、使えるなら Windows パスへ変換
if command -v cygpath >/dev/null 2>&1; then
  ffplay_arg="$(cygpath -w "$latest")"
  ffplay_bin_arg="$(cygpath -w "$ffplay_bin")"
else
  ffplay_arg="$latest"
  ffplay_bin_arg="$ffplay_bin"
fi

# 無表示で再生
"$ffplay_bin_arg" -nodisp -autoexit -loglevel error -hide_banner "$ffplay_arg" \
  </dev/null >/dev/null 2>&1

