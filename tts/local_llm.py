"""要約・タイトル生成を LLM に頼む共通ヘルパ。

頼み先 (バックエンド) は 3 つから選べる。既定は local。

  local  : OpenAI 互換 API のサーバ (llama.cpp / llama-swap / Ollama / LM Studio 等)。
           文字起こしは手元の機械から外へ出ない。
  claude : Claude Code の CLI (`claude -p`)。
  codex  : Codex の CLI (`codex exec`)。

⚠ claude / codex を選ぶと、文字起こしの全文がクラウドへ送られる。

設定は環境変数、またはこのファイルの隣の .env で行う (書き換え不要)。

  LLM_BACKEND          local / claude / codex (既定 local)。--llm オプションが優先
  LLM_API_BASE         local の接続先 (既定 http://127.0.0.1:8081/v1)
  LLM_MODEL            local のモデル名 (既定 gemma4-chat)
  LLM_API_KEY          local のサーバが鍵を要求する場合のみ
  LLM_SERVER_UNIT      local が応答しないとき起動する systemd --user のユニット名
                       (既定 llm-swap)。空にすると自動起動しない
  LLM_SERVER_CMD       local が応答しないとき実行する起動スクリプトのパス
                       (systemd が無い Windows など向け。指定するとユニットより優先)
  LLM_DISABLE_THINKING local で「考える」機能を切る指定を送るか (既定 1。0 で送らない)
  LLM_MAX_INPUT_CHARS  1 回に渡す最大文字数 (既定 local 16000 / claude・codex 100000)
  LLM_CLI_MODEL        claude / codex に渡すモデル名 (未設定なら各 CLI の既定)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_dotenv() -> None:
    """このファイルの隣の .env を読む。既に設定済みの環境変数は上書きしない"""
    env_path = Path(__file__).resolve().parent / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

BACKENDS = ("local", "claude", "codex")
BACKEND = "local"

API_BASE = (os.environ.get("LLM_API_BASE", "").strip()
            or "http://127.0.0.1:8081/v1").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "").strip() or "gemma4-chat"
API_KEY = os.environ.get("LLM_API_KEY", "").strip()
CLI_MODEL = os.environ.get("LLM_CLI_MODEL", "").strip()
DISABLE_THINKING = os.environ.get("LLM_DISABLE_THINKING", "1").strip() != "0"

# 実質の入力上限を文字数で見積もる。日本語は概ね 1 文字 ≒ 1 トークン。
# local の既定は ctx 24576 のサーバを想定し、出力とテンプレートの余白を引いた値。
_DEFAULT_MAX_INPUT_CHARS = {"local": 16000, "claude": 100000, "codex": 100000}
MAX_INPUT_CHARS = 16000


class LocalLLMError(RuntimeError):
    """LLM が使えない/失敗した (名前は互換のため据え置き。どのバックエンドでも使う)"""


def set_backend(name: str | None) -> None:
    """頼み先を切り替える。None や空文字なら何もしない"""
    global BACKEND, MAX_INPUT_CHARS
    if not name:
        return
    name = name.strip().lower()
    if name not in BACKENDS:
        raise LocalLLMError(
            f"LLM の頼み先 '{name}' は使えません。{' / '.join(BACKENDS)} から選んでください。"
        )
    BACKEND = name
    env_max = os.environ.get("LLM_MAX_INPUT_CHARS", "").strip()
    try:
        MAX_INPUT_CHARS = int(env_max) if env_max else _DEFAULT_MAX_INPUT_CHARS[name]
    except ValueError as e:
        raise LocalLLMError(f"LLM_MAX_INPUT_CHARS が数値ではありません: {env_max}") from e


def backend_label() -> str:
    """進捗表示用の名前"""
    return {"local": "ローカル LLM", "claude": "Claude", "codex": "Codex"}[BACKEND]


# 要約が不要なときの逃げ道。systemd が無い環境 (Windows 等) では自動起動できないため、
# 失敗メッセージには必ずこれを添える
NO_LLM_HINT = (
    "  要約・タイトル生成が不要なら auto_rename_stt.py に --no-summarize を付ける"
    "（LLM に一切触らず、文字起こしだけ作る）。\n"
    "  別のサーバを使うなら環境変数 LLM_API_BASE / LLM_MODEL / LLM_SERVER_UNIT で指定する。\n"
    "  頼み先を変えるなら --llm claude か --llm codex（文字起こしがクラウドへ送られる）。"
)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def is_available(timeout: float = 3.0) -> bool:
    """local のサーバが起動しているか"""
    try:
        req = urllib.request.Request(f"{API_BASE}/models", headers=_headers())
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


# local のサーバを受け持つ systemd --user ユニット。空なら自動起動しない
SERVER_UNIT = os.environ.get("LLM_SERVER_UNIT", "llm-swap").strip()
# 起動スクリプト (1 つのファイルのパス)。指定があればユニットより優先する
SERVER_CMD = os.environ.get("LLM_SERVER_CMD", "").strip()
# llama-swap 本体が応答するまでの待ち時間。モデルは最初のリクエスト時に
# 読み込まれる (その待ちは chat() の timeout 側) ので、ここは短くてよい
START_TIMEOUT = 60


def _wait_until_available(timeout: float, log_hint: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_available():
            print("  起動しました（モデルは最初の要約時に読み込まれます）")
            return
        time.sleep(1)
    raise LocalLLMError(
        f"ローカル LLM サーバを起動しましたが、{timeout:.0f} 秒待っても応答がありません。\n"
        f"  {log_hint}\n"
        + NO_LLM_HINT
    )


def _server_log_path() -> Path:
    return Path(tempfile.gettempdir()) / "local_llm_server.log"


def _spawn_server() -> None:
    """LLM_SERVER_CMD の起動スクリプトを、このプロセスから切り離して動かす"""
    cmd_path = Path(SERVER_CMD).expanduser()
    if not cmd_path.is_file():
        raise LocalLLMError(
            f"LLM_SERVER_CMD のファイルが見つかりません: {cmd_path}\n" + NO_LLM_HINT
        )
    print(f"ローカル LLM サーバ ({API_BASE}) が応答しないため起動します: {cmd_path}")
    try:
        log = open(_server_log_path(), "ab")
        if os.name == "nt":
            # .cmd は cmd.exe 経由で動かす。窓を出さず、親が終わっても残す
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(["cmd.exe", "/c", str(cmd_path)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, creationflags=flags)
        else:
            subprocess.Popen([str(cmd_path)], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
    except OSError as e:
        raise LocalLLMError(
            f"ローカル LLM サーバを起動できませんでした: {e}\n" + NO_LLM_HINT
        ) from e


def _is_mintty_pipe(stream) -> bool:
    """Windows で、stream が mintty (Git Bash / MSYS2 / Cygwin) の端末につながっているか。

    mintty から Windows 版の Python を動かすと、キーボードで打っていても標準入出力は
    パイプになり isatty() は False を返す。そのパイプの名前は
    \\msys-<番号>-pty<N>-from-master や \\cygwin-<番号>-pty<N>-to-master になるので、
    名前で見分ける。
    """
    if os.name != "nt" or stream is None:
        return False
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if kernel32.GetFileType(wintypes.HANDLE(handle)) != 3:   # 3 = FILE_TYPE_PIPE
            return False

        class FILE_NAME_INFO(ctypes.Structure):
            _fields_ = [("FileNameLength", wintypes.DWORD),
                        ("FileName", wintypes.WCHAR * 1024)]

        info = FILE_NAME_INFO()
        ok = kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(handle), 2,                           # 2 = FileNameInfo
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            return False
        name = info.FileName[: info.FileNameLength // 2]
    except (OSError, ValueError, AttributeError, ImportError):
        return False
    return ("msys-" in name or "cygwin-" in name) and "-pty" in name


def _is_interactive() -> bool:
    """本人がキーボードで操作しているか (端末、または mintty)"""
    stdin = sys.stdin
    if stdin is None:
        return False
    try:
        if stdin.isatty():
            return True
    except (OSError, ValueError):
        return False
    return _is_mintty_pipe(stdin)


_INSTALLER = Path(__file__).resolve().parent.parent / "local-llm" / "install.py"


def _offer_install() -> bool:
    """ローカル LLM が無いとき、local-llm/install.py で入れるか本人に聞く。入れたら True"""
    if not _INSTALLER.is_file() or not _is_interactive() or os.environ.get("LLM_NO_INSTALL_PROMPT"):
        return False
    print(
        "\nこの機械には、要約に使うローカル LLM が入っていないようです。\n"
        f"  {_INSTALLER} で入れられます (GPU を調べて、要約用の軽いモデルを取得します)。\n"
        "  入れずに続けるなら --no-summarize、クラウドに頼むなら --llm claude / codex。",
        flush=True,
    )
    try:
        if input("今入れますか？ [y/N] ").strip().lower() not in ("y", "yes"):
            return False
    except EOFError:
        return False
    r = subprocess.run([sys.executable, "-u", str(_INSTALLER)])
    if r.returncode != 0:
        print("ローカル LLM のインストールが終わりませんでした。", file=sys.stderr)
        return False
    _reload_settings()
    return True


def _reload_settings() -> None:
    """install.py が tts/.env に書いた接続先・起動方法を読み直す"""
    global API_BASE, MODEL, SERVER_UNIT, SERVER_CMD
    env_path = Path(__file__).resolve().parent / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if line.startswith("LLM_") and "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip().strip('"').strip("'")
    API_BASE = (os.environ.get("LLM_API_BASE", "").strip() or "http://127.0.0.1:8081/v1").rstrip("/")
    MODEL = os.environ.get("LLM_MODEL", "").strip() or "gemma4-chat"
    SERVER_UNIT = os.environ.get("LLM_SERVER_UNIT", "llm-swap").strip()
    SERVER_CMD = os.environ.get("LLM_SERVER_CMD", "").strip()


def ensure_server(timeout: float = START_TIMEOUT) -> None:
    """頼み先が使える状態か確かめる。使えなければ例外。

    local  : 未起動なら起動し、応答するまで待つ。起動できず、ローカル LLM が入っていない
             ようなら、その場で local-llm/install.py で入れるか聞く
    claude / codex : コマンドが PATH にあるかだけ見る
    """
    try:
        _ensure_server(timeout)
    except LocalLLMError:
        if BACKEND != "local" or not _offer_install():
            raise
        _ensure_server(timeout)


def _ensure_server(timeout: float) -> None:
    if BACKEND != "local":
        if not shutil.which(BACKEND):
            raise LocalLLMError(
                f"{BACKEND} コマンドが見つかりません。インストールして PATH を通すか、"
                "--llm local を使ってください。\n" + NO_LLM_HINT
            )
        return

    if is_available():
        return

    if SERVER_CMD:
        _spawn_server()
        _wait_until_available(timeout, f"ログの確認: {_server_log_path()}")
        return

    if not SERVER_UNIT:
        raise LocalLLMError(
            f"ローカル LLM サーバ ({API_BASE}) が応答しません。"
            "サーバを起動してからやり直してください"
            "（LLM_SERVER_UNIT が空なので自動起動はしません）。\n" + NO_LLM_HINT
        )

    print(
        f"ローカル LLM サーバ ({API_BASE}) が応答しないため起動します: "
        f"systemctl --user start {SERVER_UNIT}"
    )
    try:
        r = subprocess.run(
            ["systemctl", "--user", "start", SERVER_UNIT],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        # Windows など systemctl が無い環境はここに来る (FileNotFoundError)
        raise LocalLLMError(
            f"ローカル LLM サーバ ({API_BASE}) に接続できず、自動起動もできませんでした: {e}\n"
            + NO_LLM_HINT
        ) from e
    if r.returncode != 0:
        raise LocalLLMError(
            f"ローカル LLM サーバの起動に失敗しました: {r.stderr.strip()}\n"
            f"  状態の確認: systemctl --user status {SERVER_UNIT}\n"
            + NO_LLM_HINT
        )

    _wait_until_available(timeout, f"ログの確認: journalctl --user -u {SERVER_UNIT} -n 50")

def _chat_cli(system: str, user: str, timeout: int) -> str:
    """claude / codex の CLI に 1 回だけ問い合わせる。

    文字起こしには何が書かれているか分からないので、空の一時ディレクトリで動かし、
    ファイルを読み書きする道具は渡さない (claude) / 読み取り専用にする (codex)。
    本文は引数ではなく標準入力で渡す (長さの上限と、他のプロセスから見える問題を避ける)。
    """
    exe = shutil.which(BACKEND)
    if not exe:
        raise LocalLLMError(f"{BACKEND} コマンドが見つかりません。")

    with tempfile.TemporaryDirectory(prefix="llm_cli_") as work:
        out_file = Path(work) / "last_message.txt"
        if BACKEND == "claude":
            cmd = [exe, "-p", "--system-prompt", system,
                   "--no-session-persistence", "--tools", "",
                   "--strict-mcp-config"]  # 利用者が登録した外部連携 (MCP) も渡さない
            if CLI_MODEL:
                cmd += ["--model", CLI_MODEL]
            stdin_text = user
        else:  # codex
            cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral",
                   "-s", "read-only", "-C", work, "-o", str(out_file)]
            if CLI_MODEL:
                cmd += ["-m", CLI_MODEL]
            cmd.append("-")
            stdin_text = f"{system}\n\n{user}"

        try:
            r = subprocess.run(
                cmd, input=stdin_text.encode("utf-8"), capture_output=True,
                timeout=timeout, cwd=work,
            )
        except subprocess.TimeoutExpired as e:
            raise LocalLLMError(f"{BACKEND} が {timeout} 秒以内に応答しませんでした。") from e
        except OSError as e:
            raise LocalLLMError(f"{BACKEND} を起動できませんでした: {e}") from e

        stdout = r.stdout.decode("utf-8", errors="replace").strip()
        stderr = r.stderr.decode("utf-8", errors="replace").strip()
        if r.returncode != 0:
            raise LocalLLMError(
                f"{BACKEND} がエラーで終了しました (exit={r.returncode}): "
                + (stderr or stdout)[-500:]
            )

        content = stdout
        if BACKEND == "codex":
            try:
                content = out_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass  # 書き出されなかったら標準出力を使う

    if not content:
        raise LocalLLMError(f"{BACKEND} から空の応答が返りました。{stderr[-300:]}")
    return content


def chat(system: str, user: str, max_tokens: int = 2000,
         temperature: float = 0.3, timeout: int = 900) -> str:
    """1 往復のチャット。応答テキストを返す。

    max_tokens と temperature は local にだけ効く (CLI には渡す手段が無い)。
    """
    if BACKEND != "local":
        return _chat_cli(system, user, timeout)

    req = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # gemma4-chat のような thinking モデルは、無効化しないと reasoning_content に
    # max_tokens を食い潰され content が空になる。"/no_think" は不安定なため
    # chat_template_kwargs で切る (llama.cpp の指定。受け付けないサーバでは
    # LLM_DISABLE_THINKING=0 で送らないようにできる)
    if DISABLE_THINKING:
        req["chat_template_kwargs"] = {"enable_thinking": False}
    data = json.dumps(req, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}/chat/completions", data, _headers(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise LocalLLMError(f"LLM リクエスト失敗 (HTTP {e.code}): {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise LocalLLMError(f"LLM リクエスト失敗: {e}") from e

    try:
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LocalLLMError(f"LLM レスポンスの形式が想定外: {body}") from e

    content = (message.get("content") or "").strip()

    if choice.get("finish_reason") == "length":
        print(
            "警告: LLM 応答が max_tokens で打ち切られました（内容が途中で切れている可能性）",
            file=sys.stderr,
        )

    if not content:
        reasoning = message.get("reasoning_content")
        hint = ""
        if reasoning:
            hint = (
                "\n  reasoning_content に出力が入り content が空でした"
                "（thinking モデルが考えるだけで予算を使い切った）: "
                + reasoning[:200]
            )
        raise LocalLLMError(f"LLM から空の応答が返りました。{hint}")

    return content


def split_chunks(text: str, size: int | None = None) -> list[str]:
    """長文を size 文字以下のチャンクへ分割する。

    話者ブロック（空行区切り）→ 行 の順に、なるべく意味の切れ目で割る。
    size 省略時は呼び出し時点の MAX_INPUT_CHARS を使う（モジュール属性の
    実行時上書きが効くよう、デフォルト引数として束縛しない）。
    """
    if size is None:
        size = MAX_INPUT_CHARS
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        block = block + "\n\n"
        if len(block) > size:
            # 単一ブロックが大きすぎる場合は行単位で割る
            for line in block.splitlines(keepends=True):
                if len(current) + len(line) > size and current:
                    chunks.append(current)
                    current = ""
                current += line
            continue
        if len(current) + len(block) > size and current:
            chunks.append(current)
            current = ""
        current += block
    if current.strip():
        chunks.append(current)
    return chunks


SUMMARY_SYSTEM = (
    "あなたは会議の議事録作成者です。日本語で簡潔に要約します。"
    "推測で内容を補わず、文字起こしにある事実だけを書いてください。"
)

SUMMARY_INSTRUCTION = (
    "以下は動画/音声の文字起こしです。内容を日本語で要約してください。"
    "主要な話題・議題ごとにまとめ、決定事項やアクションアイテムがあれば明記し、"
    "話者が分かれている場合は誰が何を言ったかを含めてください。箇条書きを活用すること。"
)


def _consolidate(partials: list[str], progress) -> str:
    """部分要約群を1つの要約に統合する。joined が ctx を超える場合は中間統合を挟む。"""
    joined = "\n\n".join(
        f"--- 部分 {i} ---\n{p}" for i, p in enumerate(partials, 1)
    )
    if len(joined) <= MAX_INPUT_CHARS:
        progress("  統合要約中 ...")
        return chat(
            SUMMARY_SYSTEM,
            "以下は同じ会議/動画を順番に分割して要約したものです。"
            "重複を整理し、全体を通した 1 つの要約にまとめ直してください。"
            + SUMMARY_INSTRUCTION.replace("以下は動画/音声の文字起こしです。", "")
            + "\n\n" + joined,
            max_tokens=3000,
        )

    # 部分要約が多すぎて一度に統合できない → 中間統合を挟んでから再統合
    groups = split_chunks(joined)
    progress(f"  部分要約が多いため中間統合 ({len(groups)} グループ) ...")
    sub_summaries = []
    for i, group in enumerate(groups, 1):
        progress(f"    中間統合 {i}/{len(groups)} ...")
        sub_summaries.append(chat(
            SUMMARY_SYSTEM,
            "以下は同じ会議/動画の部分要約の一部です。要点を保ったまま"
            "日本語で圧縮してまとめてください。\n\n" + group,
            max_tokens=2000,
        ))
    return _consolidate(sub_summaries, progress)


def summarize(transcript_text: str, progress=print) -> str:
    """文字起こしを要約する。長い場合はチャンク要約 → 統合要約。"""
    ensure_server()
    chunks = split_chunks(transcript_text)

    if len(chunks) == 1:
        progress(f"\n要約中（{backend_label()}）...")
        return chat(SUMMARY_SYSTEM, f"{SUMMARY_INSTRUCTION}\n\n{chunks[0]}",
                    max_tokens=3000)

    progress(f"\n要約中（{backend_label()}、{len(chunks)} 分割）...")
    partials = []
    for i, chunk in enumerate(chunks, 1):
        progress(f"  部分要約 {i}/{len(chunks)} ...")
        partials.append(chat(
            SUMMARY_SYSTEM,
            f"以下は長い文字起こしの一部（{i}/{len(chunks)}）です。"
            "この範囲の内容だけを日本語の箇条書きで詳しく要約してください。"
            "前後の文脈は補わないでください。\n\n" + chunk,
            max_tokens=2000,
        ))

    return _consolidate(partials, progress)


def generate_title(summary_text: str) -> str:
    """要約から短い日本語タイトルを生成する。"""
    ensure_server()
    return chat(
        "あなたはファイル名を考える係です。指示された形式の文字列だけを返します。",
        "以下は会議/動画の要約です。動画ファイルのタイトルとして適切な"
        "日本語タイトルを1行だけ出力してください。"
        "条件: 20文字以内、記号なし、体言止め、タイトルだけを返す"
        "（説明文やカギ括弧は不要）。\n\n" + summary_text[:MAX_INPUT_CHARS],
        max_tokens=100,
        temperature=0.2,
    )


# 環境変数 LLM_BACKEND の値を反映する (不正な値はここで止める)
try:
    set_backend(os.environ.get("LLM_BACKEND", "").strip() or "local")
except LocalLLMError as _e:
    sys.exit(str(_e))
