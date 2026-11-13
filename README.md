# snippets

小さなツールを置いている。各ディレクトリが独立していて、
単体で使える。ライセンスは MPL-2.0。

## tts

日本語の読み上げ (TTS) と文字起こし (STT) まわりのスクリプト群。

| ファイル | 用途 |
|---|---|
| `tts.py` | テキスト → WAV (Style-Bert-VITS2) |
| `clip_tts.py` | クリップボードの内容を読み上げる常駐ツール |
| `read_aloud.py` | ファイル/標準入力を読み上げる |
| `chat_to_speech.py` / `chat_to_speech_direct.py` | 会話ログを音声化 |
| `api_server.py` | TTS を HTTP API として提供 |
| `irodori_server.py` | Irodori-TTS を参照音声クローンで常駐させる |
| `stt.py` | 音声ファイル → テキスト (Whisper、話者分離に対応) |
| `mic_stt.py` | マイク入力をリアルタイムに文字起こし |
| `auto_rename_stt.py` | 録音ファイルを内容から自動で命名する |
| `eng2kana.py` | 英単語をカタカナ読みに変換 |
| `fetch_dict.py` | 用語の読みを取得して `dictionary.csv` に追記 |
| `local_llm.py` | OpenAI 互換 API (ローカル LLM) のクライアント |
| `bin/` | 上記のランチャ (Linux / Windows) |

### 用意するもの

- **音声エンジン本体は含まない。** Style-Bert-VITS2 / Irodori-TTS は別途インストールする
  (場所は `IRODORI_DIR` 環境変数で指定できる)
- **声の見本 `tts/voices/calm.wav` / `bright.wav` を同梱。** Irodori-TTS の VoiceDesign
  (Aratako/Irodori-TTS-600M-v3-VoiceDesign、MIT) で、文章の指定だけから作った声。実在の人の声は使っていない。
  声を増やすときは `tts/voices/` に `<名前>.wav` を置くか、`IRODORI_VOICES_DIR` で場所を指定する
- **`dictionary.csv` は空の雛形。** `fetch_dict.py` で育てる
- `clip_tts.desktop` の `Exec` のパスは環境に合わせて書き換える
- 話者分離を使う場合のみ `HF_TOKEN` 環境変数と pyannote の規約同意が必要

### 接続先

音声合成と文字起こしの処理はローカルで動く。ローカルのサーバには `127.0.0.1` で接続する。
次の場合はネットワークに出る。

- `fetch_dict.py`: e-words.jp から読みを取得する
- 初回のモデル取得: Whisper (約3GB)、Irodori (約4GB)、話者分離のモデルを HuggingFace から取得する
- `clip_tts.py`: Irodori が無いと自動で clone してインストールする (`--no-start-server` で無効)
- `bin/stt` / `bin/mic_stt`: 不足パッケージを pip で自動導入する (`CLIP_TTS_NO_AUTOINSTALL=1` で無効)
- `setup_irodori`: uv を公式のインストーラで導入する
- `play_latest_wav.sh`: Windows で ffplay が無いと ffmpeg を取得する

## keyboard/windows

Mac の JIS キーボードを Windows で使うためのキーリマップ。
サードパーティ製ソフト不要で、PowerShell の低レベルキーボードフックだけで動く。
ON/OFF をライブ切り替えできる。詳細は `keyboard/windows/README.md`。

> 管理者権限での昇格、既存のレジストリ値 `Scancode Map` の削除、タスクスケジューラへの登録を行う。
> 業務用 PC で使う場合は、先に社内規程を確認すること。

## mousejiggler

一定間隔でマウスを左右に小さく動かす (Windows)。座標の書き換えではなく実際の入力イベントを
送るので、システムのアイドル時間がリセットされる。PowerShell だけで動く。

| ファイル | 用途 |
|---|---|
| `MouseJiggler.ps1` | 本体。間隔や距離は引数で変えられる (ファイル冒頭に説明) |
| `MouseJiggler起動.bat` | ダブルクリックで起動する |
| `mousejiggler.sh` | Git Bash から起動する |

止めるときは Ctrl + C。
