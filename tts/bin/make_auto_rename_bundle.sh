#!/usr/bin/env bash
# auto_rename を他の環境で使うための「まとめフォルダ」を作る。
#
# tts/ には TTS 側のツールも同居しているため、必要なファイルだけを集めた
# フォルダを生成する。生成先の中身を、導入先のディレクトリへマージする。
#
#   使い方: make_auto_rename_bundle.sh [出力先] [--zip]
#     出力先の既定は tts/auto_rename_bundle
#     --zip を付けると <出力先>_YYYYMMDD.zip も作る (既存は上書きせず連番を足す)
#
# ⚠ ここに集まるのはコピー。直す場所は常に tts/ の方で、直したらこれを再実行する。
set -euo pipefail
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
TTS_DIR="$(cd "$(dirname "$SELF")/.." && pwd)"
ZIP=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --zip) ZIP=1 ;;
        -*) echo "不明なオプション: $a" >&2; exit 1 ;;
        *) ARGS+=("$a") ;;
    esac
done
OUT="${ARGS[0]:-$TTS_DIR/auto_rename_bundle}"

mkdir -p "$OUT/bin"
# 実行して出た .pyc を持っていかない
rm -rf "$OUT/__pycache__"

# 本体 3 ファイル（auto_rename_stt.py は隣の stt.py を呼び、local_llm を import する）
for f in auto_rename_stt.py stt.py local_llm.py; do
    cp -f "$TTS_DIR/$f" "$OUT/$f"
done
# 文字起こしの表記ゆれ補正に使う辞書（無くても動くが、置換が効かなくなる）
cp -f "$TTS_DIR/dictionary.csv" "$OUT/dictionary.csv"
cp -f "$TTS_DIR/requirements-stt.txt" "$OUT/requirements-stt.txt"
# ランチャー（python の自動選択と cuDNN の LD_LIBRARY_PATH 設定が入っている）
for f in auto_rename_stt stt; do
    cp -f "$TTS_DIR/bin/$f" "$OUT/bin/$f"
    chmod +x "$OUT/bin/$f"
done
# Windows 用。cmd.exe / PowerShell から使う
for f in auto_rename_stt.cmd stt.cmd; do
    cp -f "$TTS_DIR/bin/$f" "$OUT/bin/$f"
done

# .env は HF_TOKEN が入っているのでコピーしない。雛形だけ置く
cat > "$OUT/.env.example" <<'ENVEOF'
# 話者分離 (pyannote) に必要。使わないなら未設定でよい（警告を出して話者分離なしで続行する）
# https://huggingface.co/settings/tokens で取得し、このファイルを .env にリネームする
HF_TOKEN=hf_xxxxxxx
ENVEOF

cat > "$OUT/README.md" <<'DOCEOF'
# auto_rename 配布用フォルダ

`tts/bin/make_auto_rename_bundle.sh` が生成したコピー。**ここを直さない**。
直す場所は `tts/` 側で、直したらスクリプトを再実行する。

このフォルダの中身を、導入先のディレクトリへそのままマージする。
`auto_rename_stt.py` は同じフォルダの `stt.py` と `local_llm.py` を使うので、
3 ファイルは必ず同じ場所に置く。

## 使い方

```bash
./bin/auto_rename_stt voice.m4a --no-summarize   # 文字起こしだけ（LLM不要）
./bin/auto_rename_stt voice.m4a                  # 要約・タイトル生成つき（LLM必要）
./bin/stt voice.m4a -o out.txt                   # 文字起こしだけ使いたいとき
```

どのディレクトリからでも使うなら `~/.local/bin` に symlink を張る:

```bash
ln -s "$PWD/bin/auto_rename_stt" ~/.local/bin/auto-rename
```

## 必要なもの

| もの | 無いとどうなるか |
|---|---|
| ffmpeg（PATH 上） | 「ffmpegが見つかりません」で終了 |
| Python パッケージ（`requirements-stt.txt`）＋ torch・torchaudio | import エラー、または `libcudnn_ops_infer.so.8 not found` |
| nvidia-cudnn-cu12 の 8 系（ctranslate2 4.4.0 用） | 同上。`bin/` のランチャー経由なら `LD_LIBRARY_PATH` は自動設定 |
| GPU と CUDA ドライバ | `--device cpu` で回避できる（遅い） |
| Whisper モデル（初回に HuggingFace から約 3GB ダウンロード） | 初回だけネットと空き容量が必要 |
| `HF_TOKEN`（`.env` か環境変数）＋ pyannote の規約同意 | 警告を出して話者分離なしで続行（落ちはしない） |

pyannote は以下 2 つのページで「Agree」を押しておく必要がある。

- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

## 要約・タイトル生成（`--no-summarize` を付けないとき）

`local_llm.py` の先頭に、環境依存の設定が直書きされている。

| 定数 | 既定値 | 意味 |
|---|---|---|
| `API_BASE` | `http://127.0.0.1:8081/v1` | OpenAI 互換 API の場所 |
| `MODEL` | `gemma4-chat` | モデル名 |
| `SERVER_UNIT` | `llm-swap` | 未応答のとき `systemctl --user start` するユニット |

**導入先に `llm-swap` ユニットが無いと、文字起こしを始める前に
「ローカル LLM サーバの起動に失敗しました」で終了する。** 対処は次のどれか。

1. 要約が要らないなら `--no-summarize` を付ける（LLM に一切触らない）
2. :8081 に OpenAI 互換サーバを立て、モデル名を `gemma4-chat` にする
3. `local_llm.py` の上記 3 定数を、その環境に合わせて書き換える

## Python の選択

ランチャーは次の順で python を選ぶ。合うものが無ければ `python3` を使う。

1. 環境変数 `SBV2_VENV_PYTHON`
2. `~/tts/Style-Bert-VITS2/venv/bin/python`（Linux）
3. `C:\tmp\sbv2_venv\Scripts\python.exe`（Windows）

別の venv を使う環境では `SBV2_VENV_PYTHON` を指定する。

## Windows で使う場合

```cmd
bin\auto_rename_stt.cmd voice.m4a --no-summarize
bin\stt.cmd voice.m4a -o out.txt
```

- `.cmd` は `SBV2_VENV_PYTHON` が未設定なら `C:\tmp\sbv2_venv\Scripts\python.exe` を使う。
  venv の場所が違うなら、環境変数 `SBV2_VENV_PYTHON` を設定する。
- **`systemctl` が無いので、LLM の自動起動はできない。** 要約するなら
  `API_BASE` の先に OpenAI 互換サーバを自分で立てておく。要らなければ `--no-summarize`。
- ffmpeg は PATH 上にあればよい。`stt.py` は PATH の前に
  `$FFMPEG_BIN`（未設定なら `$HOME/ffmpeg/bin`）も見る。
- パッケージの導入は `py -m pip install -r requirements-stt.txt` ＋ torch / torchaudio。

## うまく動かないとき

- **何も表示されずに終了する**: python が見つかっていない。ランチャーは探した場所を
  一覧で出すので、それを見て `SBV2_VENV_PYTHON` に python のパスを設定する。
- **Windows でウィンドウが一瞬で消える**: `.cmd` をダブルクリックしないこと。
  cmd.exe か PowerShell を開いてそこから実行すれば、メッセージが読める。
- **`ffmpegが見つかりません`**: ffmpeg を PATH に入れる。
- **要約の手前で止まる**: その環境に LLM が無い。`--no-summarize` を付ける。
DOCEOF

echo "生成: $OUT"
ls -1 "$OUT" "$OUT/bin" | sed 's/^/  /'

if [ "$ZIP" = 1 ]; then
    # 旧版を消さないよう、日付 + 連番で別名にする
    parent="$(cd "$(dirname "$OUT")" && pwd)"
    base="$(basename "$OUT")"
    zip_path="$parent/${base}_$(date +%Y%m%d).zip"
    n=2
    while [ -e "$zip_path" ]; do
        zip_path="$parent/${base}_$(date +%Y%m%d)_${n}.zip"
        n=$((n + 1))
    done
    # 中身を直置きで固める (トップレベルに囲みフォルダを作らない)。
    # こうすると unar -D で導入先のディレクトリへそのままマージでき、
    # -D 無し (既定) なら unar がアーカイブ名のフォルダを作って展開する。
    # -r でドットファイル (.env.example) も入る。実行権限は zip が保持する
    (cd "$OUT" && zip -r -q "$zip_path" . -x '*__pycache__*' '*.pyc')
    echo "zip: $zip_path"
    echo "  マージ展開: unar -D -o <導入先> $(basename "$zip_path")"
fi
