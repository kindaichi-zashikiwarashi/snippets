# local-llm

`tts/auto_rename_stt.py` と `tts/stt.py --summarize` が、要約とタイトル生成を頼む先の用意。

文字起こしそのものは LLM なしで動く。LLM が要るのは要約とタイトル生成だけ。

## 頼み先は 3 つから選べる

| 頼み先 | 指定 | 文字起こしの行き先 |
|---|---|---|
| ローカルの LLM (既定) | `--llm local` | 手元の機械から出ない |
| Claude Code の CLI | `--llm claude` | **クラウドへ送られる** |
| Codex の CLI | `--llm codex` | **クラウドへ送られる** |

```bash
auto_rename_stt voice.m4a                 # 既定 (local)
auto_rename_stt voice.m4a --llm claude    # この 1 回だけ Claude に頼む
```

毎回同じ頼み先にするなら、環境変数 `LLM_BACKEND` か `tts/.env` に書く
(`tts/.env.example` がひな形)。`--llm` を付けたときはそちらが優先される。

`claude` / `codex` は、それぞれの CLI がインストール済みでログイン済みであることが前提。
会議の録音など外へ出せない内容には使わないこと。

## ローカルの LLM を用意する

OpenAI 互換の API (`/v1/chat/completions`) を持つサーバなら何でもよい。
既定の接続先は `http://127.0.0.1:8081/v1`、モデル名は `gemma4-chat`。

### A. 一式を自動で入れる (Linux / Windows、NVIDIA の GPU)

```bash
python3 local-llm/install.py            # Windows は  py local-llm\install.py
```

やること:

1. GPU を調べ (`nvidia-smi`)、ドライバの版と VRAM から使うものを決める
2. [llama.cpp](https://github.com/ggml-org/llama.cpp) の `llama-server` と
   [llama-swap](https://github.com/mostlygeek/llama-swap) を、公式のリリースから取得する
3. 要約用の軽い Gemma 4 (GGUF、unsloth 配布、Apache-2.0) を Hugging Face から取得する
4. 設定を書き出す

何を取得するか、どこに置くか、必要な空きを表示して、確認してから進む。
`--dry-run` を付けると表示だけで終わる。

既定では、要約とタイトル付けに足りる軽いモデルを選ぶ。

| VRAM | 既定で選ばれるモデル | 大きさ |
|---|---|---|
| 7GB 以上 | Gemma 4 E4B | 5.1GB |
| それ未満 / GPU なし | Gemma 4 E2B | 2.6GB |

もっと賢いのが欲しければ `--model 12b` (6.7GB、VRAM 11GB 以上) か `--model 26b` (14.2GB、
VRAM 20GB 以上)。GPU が複数あるときは、一番大きい VRAM で判断する。

安全のための決まり:

- 取得する版は `install.py` の中で固定してあり、取得したファイルは SHA-256 を照合する。
  合わなければ使わずに止まる
- LLM は `127.0.0.1` でしか待ち受けない (同じ機械の中からしかつながらない)
- 既にある設定ファイルは上書きしない

**自分で実行しなくてもよい。** `auto_rename_stt` で要約しようとしてローカル LLM が見つからないと、
「今入れますか？」と聞いてくるので、`y` と答えればこれが動く (端末から実行したときだけ。
cron などからは聞かずにエラーで止まる。聞かれたくなければ `LLM_NO_INSTALL_PROMPT=1`)。

llama-swap は、頼まれたときだけモデルを読み込み、5 分使われなければ降ろす。
文字起こし (Whisper) と GPU のメモリを取り合わない。

起動の仕方は環境で変わる。どちらも `auto_rename_stt` が必要なときに自分で起動する。

- **Linux (systemd あり)**: `systemctl --user` のユニット `llm-swap` を書き出す
- **Windows、または `--no-service`**: 起動スクリプトを書き出し、その場所を `tts/.env` の
  `LLM_SERVER_CMD` に書く

#### Ubuntu 22.04 など古い Linux の場合

公式の llama.cpp の完成品は glibc 2.38 以上 (Ubuntu 24.04 以降) が要る。
それより古いと `install.py` は取得する前に止まるので、llama.cpp を
[自分でビルド](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)して渡す。

```bash
python3 local-llm/install.py --llama-server /path/to/llama-server
```

手持ちのモデルを使うなら `--model-path /path/to/model.gguf`。

#### 確認できている範囲

- Ubuntu 22.04 + RTX 3090: `--llama-server` で自分のビルドを渡し、起動スクリプト方式
  (`--no-service`) で、取得・照合・設定・自動起動・GPU での要約まで確認済み (E2B)
- 「今入れますか？」の流れ: はい / いいえ / 端末なし の 3 通りを確認済み
- 公式の完成品 (Ubuntu 24.04 以降、Windows) の起動、systemd ユニット方式、
  Windows の起動スクリプトは**未確認**

#### 消すには

置き場所 (既定は Linux `~/.local/share/snippets-local-llm`、Windows
`%LOCALAPPDATA%\snippets-local-llm`) を削除する。Linux で systemd を使った場合は
`~/.config/systemd/user/llm-swap.service` も削除する。

### B. 既に動いているサーバにつなぐ (Ollama / LM Studio など。Windows でも可)

サーバは自分で起動しておき、接続先とモデル名を `tts/.env` か環境変数で指定する。

```bash
# Ollama の例
LLM_API_BASE=http://127.0.0.1:11434/v1
LLM_MODEL=<ollama list に出るモデル名>
LLM_SERVER_UNIT=

# LM Studio の例
LLM_API_BASE=http://127.0.0.1:1234/v1
LLM_MODEL=<読み込んだモデルの名前>
LLM_SERVER_UNIT=
```

`LLM_SERVER_UNIT=` (空) は「応答が無くても自動起動を試みない」の意味。

> Ollama と LM Studio での動作は未確認。

## 設定の一覧

| 環境変数 | 既定 | 意味 |
|---|---|---|
| `LLM_BACKEND` | `local` | `local` / `claude` / `codex` |
| `LLM_API_BASE` | `http://127.0.0.1:8081/v1` | local の接続先 |
| `LLM_MODEL` | `gemma4-chat` | local のモデル名 |
| `LLM_API_KEY` | なし | local のサーバが鍵を要求する場合のみ |
| `LLM_SERVER_UNIT` | `llm-swap` | local が応答しないとき起動する systemd --user のユニット。空で自動起動なし |
| `LLM_SERVER_CMD` | なし | local が応答しないとき実行する起動スクリプト (systemd が無い環境用)。指定するとユニットより優先 |
| `LLM_NO_INSTALL_PROMPT` | なし | `1` にすると、ローカル LLM が無くても「今入れますか？」と聞かない |
| `LLM_DISABLE_THINKING` | `1` | local に「考える機能を切る」指定 (`chat_template_kwargs`) を送る。`0` で送らない |
| `LLM_MAX_INPUT_CHARS` | local 16000 / ほか 100000 | 1 回に渡す最大文字数。超える分は分割して要約し、あとで統合する |
| `LLM_CLI_MODEL` | なし | `claude` / `codex` に渡すモデル名。未設定なら各 CLI の既定 |

## うまく動かないとき

- **「ローカル LLM サーバが応答しません」**: サーバが起動していないか、`LLM_API_BASE` が違う。
  `curl http://127.0.0.1:8081/v1/models` で確かめる
- **「LLM から空の応答が返りました」**: 考える機能を持つモデルが、考えるだけで出力の上限を
  使い切っている。サーバ側で考える機能を切るか、別のモデルにする
- **サーバがリクエストを拒否する (HTTP 400)**: `chat_template_kwargs` を受け付けない
  サーバの可能性がある。`LLM_DISABLE_THINKING=0` を試す
- **要約が途中で切れる / 文脈が足りないと言われる**: サーバの文脈の長さに対して入力が長い。
  `LLM_MAX_INPUT_CHARS` を小さくする
- **要約は要らない**: `--no-summarize` を付ける (LLM に一切触らない。リネームもしない)
