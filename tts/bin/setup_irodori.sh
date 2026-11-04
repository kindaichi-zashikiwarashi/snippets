#!/usr/bin/env bash
# Irodori-TTS (VoiceDesign サーバ) を自動セットアップする。Linux / macOS 用。
# Windows は setup_irodori.cmd を使う。
#
# やること:
#   1. uv (Python 環境マネージャ) が無ければ導入
#   2. Irodori-TTS リポジトリを clone (既にあれば skip)
#   3. uv sync で依存を解決 (torch>=2.10 + git 依存 dacvae/silentcipher 等)
#   4. irodori_server.py (このリポジトリ版) を clone 先へ配置
#   5. 起動コマンドを表示 (モデルは初回起動時に HF から自動 DL, 約4GB)
#
# 使い方:
#   setup_irodori.sh                       # 既定 (~/ai-tools/Irodori-TTS) に導入
#   IRODORI_DIR=/path setup_irodori.sh     # 導入先を変更
#
# 注意:
#   - Irodori は torch>=2.10 (cu128) を要求。GPU で使うなら NVIDIA ドライバ >=553 が必要
#     (それ未満だと CPU 動作になり非常に遅い)。
#   - VRAM 約3.9GB。他の GPU 処理と同時に動かすと足りないことがある。
set -euo pipefail

IRODORI_DIR="${IRODORI_DIR:-$HOME/ai-tools/Irodori-TTS}"
REPO=https://github.com/Aratako/Irodori-TTS.git
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"  # ai/tts

echo "== Irodori セットアップ =="
echo "  導入先: $IRODORI_DIR"

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
  echo "[1/4] uv を導入中..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "uv 導入に失敗。https://docs.astral.sh/uv/ 参照" >&2; exit 1; }
echo "[1/4] uv OK ($(uv --version))"

# 2. clone
if [ -d "$IRODORI_DIR/.git" ]; then
  echo "[2/4] clone 済み ($IRODORI_DIR) → skip"
else
  echo "[2/4] clone 中..."
  mkdir -p "$(dirname "$IRODORI_DIR")"
  git clone "$REPO" "$IRODORI_DIR"
fi

# 3. uv sync
echo "[3/4] uv sync (依存解決。torch 等で数分〜十数分かかる)..."
( cd "$IRODORI_DIR" && uv sync )

# 4. server + 参照音声(声の定義) 配置
echo "[4/4] irodori_server.py と voices/ を配置..."
cp "$HERE/irodori_server.py" "$IRODORI_DIR/irodori_server.py"
mkdir -p "$IRODORI_DIR/voices"
cp "$HERE/voices/"*.wav "$IRODORI_DIR/voices/" 2>/dev/null || \
  echo "  (voices/*.wav が無い。クローンの声を使うなら tts/voices/ に見本wavを置く)"

echo
echo "== 完了 =="
echo "サーバ起動 (初回はモデル DL 約4GB, 〜数分):"
echo "  cd $IRODORI_DIR && \\"
echo "    env -u LD_LIBRARY_PATH HF_HUB_DISABLE_XET=1 \\"
echo "    nohup uv run --no-sync python irodori_server.py --port 5001 > /tmp/irodori-server.log 2>&1 &"
echo
echo "確認: curl http://127.0.0.1:5001/health"
echo "その後: clip_tts -v calm   (別ディレクトリからでも可)"
