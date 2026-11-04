"""ローカル LLM (:8081) を叩く共通ヘルパ。

チャット用 :8081 (llama-swap 経由の gemma4-chat) の OpenAI 互換 API を使う。
Claude Code CLI の代わりに要約やタイトル生成をローカルで済ませるためのもの。

:8081 が応答しなければ systemd --user の llm-swap を自動で起動する
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_BASE = "http://127.0.0.1:8081/v1"
MODEL = "gemma4-chat"

# サーバの ctx は既定 24576。プロンプト以外(出力・テンプレート)の余白を引いた
# 実質の入力上限を文字数で見積もる。日本語は概ね 1 文字 ≒ 1 トークン。
MAX_INPUT_CHARS = 16000


class LocalLLMError(RuntimeError):
    """ローカル LLM が使えない/失敗した"""


# 要約が不要なときの逃げ道。systemd が無い環境 (Windows 等) では自動起動できないため、
# 失敗メッセージには必ずこれを添える
NO_LLM_HINT = (
    "  要約・タイトル生成が不要なら auto_rename_stt.py に --no-summarize を付ける"
    "（LLM に一切触らず、文字起こしだけ作る）。\n"
    "  別の環境で使うなら local_llm.py の API_BASE / MODEL / SERVER_UNIT を"
    "その環境に合わせて書き換える。"
)


def is_available(timeout: float = 3.0) -> bool:
    """llama-server が起動しているか"""
    try:
        with urllib.request.urlopen(f"{API_BASE}/models", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


# :8081 を受ける llama-swap の systemd --user ユニット
SERVER_UNIT = "llm-swap"
# llama-swap 本体が応答するまでの待ち時間。モデルは最初のリクエスト時に
# 読み込まれる (その待ちは chat() の timeout 側) ので、ここは短くてよい
START_TIMEOUT = 60


def ensure_server(timeout: float = START_TIMEOUT) -> None:
    """未起動なら llm-swap を起動し、応答するまで待つ。起動できなければ例外"""
    if is_available():
        return

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

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_available():
            print("  起動しました（モデルは最初の要約時に読み込まれます）")
            return
        time.sleep(1)
    raise LocalLLMError(
        f"ローカル LLM サーバを起動しましたが、{timeout:.0f} 秒待っても応答がありません。\n"
        f"  ログの確認: journalctl --user -u {SERVER_UNIT} -n 50\n"
        + NO_LLM_HINT
    )


def chat(system: str, user: str, max_tokens: int = 2000,
         temperature: float = 0.3, timeout: int = 900) -> str:
    """1 往復のチャット。応答テキストを返す。"""
    # gemma4-chat は thinking モデルで、無効化しないと reasoning_content に
    # max_tokens を食い潰され content が空になる。"/no_think" は不安定なため
    # chat_template_kwargs で確実に切る
    req = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data = json.dumps(req, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}/chat/completions", data,
        {"Content-Type": "application/json"},
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
                "（gemma4-chat の thinking 暴走）: "
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
        progress("\n要約中（ローカル LLM）...")
        return chat(SUMMARY_SYSTEM, f"{SUMMARY_INSTRUCTION}\n\n{chunks[0]}",
                    max_tokens=3000)

    progress(f"\n要約中（ローカル LLM、{len(chunks)} 分割）...")
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
