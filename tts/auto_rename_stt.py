"""
動画/音声を文字起こし→要約→自動で「YYYYMMDD_タイトル.拡張子」にリネームするツール

Usage:
  python auto_rename_stt.py video.mp4
  python auto_rename_stt.py voice.m4a                   # 音声ファイルも可
  python auto_rename_stt.py video.flv --no-rename       # 提案だけ表示
  python auto_rename_stt.py video.mp4 --no-diarize      # 話者分離なし
  python auto_rename_stt.py video.mp4 --prefix "会議_"   # プレフィックス指定
  python auto_rename_stt.py voice.m4a --no-summarize    # 要約なし（LLM不要・リネームしない）

動作:
  1. ローカルLLM (:8081) が応答しなければ llm-swap を起動する
  2. stt.py を呼んで文字起こし＋要約を生成
  3. 要約をローカルLLMに渡して短い日本語タイトルを生成
  4. 日付（ファイル名から抽出、無ければ更新日時）を YYYYMMDD 形式で取得
  5. 元ファイルと文字起こしファイルを YYYYMMDD_タイトル.拡張子 にリネーム
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import local_llm


_TIMESTAMP_LINE_RE = re.compile(r"\s*\d{2}:\d{2}:\d{2} → \d{2}:\d{2}:\d{2}\s+(.*)")


def extract_speech_text(transcript: str) -> str:
    """文字起こしファイルからタイムスタンプ・話者ラベル行を除いた発話テキストのみ抽出"""
    speech_parts = []
    for line in transcript.splitlines():
        m = _TIMESTAMP_LINE_RE.match(line)
        if m:
            speech_parts.append(m.group(1))
    return "".join(speech_parts)


def extract_date_from_filename(name: str) -> str | None:
    """ファイル名から YYYYMMDD を抽出"""
    # YYYY-MM-DD, YYYY_MM_DD, YYYY.MM.DD 形式
    m = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", name)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    return None


def get_date(path: Path) -> str:
    """動画のYYYYMMDDを決定（ファイル名優先、無ければ更新日時）"""
    date = extract_date_from_filename(path.name)
    if date:
        return date
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return mtime.strftime("%Y%m%d")


def sanitize_filename(text: str, max_len: int = 40) -> str:
    """ファイル名に使えない文字を除去し、長さ制限"""
    # Windows/Linuxで使えない文字を除去
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "", text)
    # 先頭末尾の空白や記号を除去
    text = text.strip(" 　-_.,、。「」『』（）()")
    return text[:max_len]


def generate_title(summary_text: str) -> str | None:
    """要約から短いタイトルを生成（ローカルLLM）"""
    print(f"\nタイトル生成中（{local_llm.backend_label()}）...")
    try:
        title = local_llm.generate_title(summary_text)
    except local_llm.LocalLLMError as e:
        print(f"タイトル生成に失敗しました: {e}", file=sys.stderr)
        return None

    # 複数行が返ってきたら最初の行だけ
    title = title.split("\n")[0].strip()
    title = sanitize_filename(title)
    return title or None


def rename_with_sidecars(video_path: Path, new_base: str) -> Path:
    """動画と関連ファイル（.txt, .summary.txt）をまとめてリネーム"""
    suffix = video_path.suffix
    new_video = video_path.parent / f"{new_base}{suffix}"

    # 既存の関連ファイルも追従
    old_txt = video_path.with_suffix(".txt")
    old_summary = video_path.parent / f"{video_path.stem}.summary.txt"
    new_txt = video_path.parent / f"{new_base}.txt"
    new_summary = video_path.parent / f"{new_base}.summary.txt"

    if new_video.exists() and new_video != video_path:
        raise FileExistsError(f"同名ファイルが既にあります: {new_video}")

    video_path.rename(new_video)
    if old_txt.exists():
        old_txt.rename(new_txt)
    if old_summary.exists():
        old_summary.rename(new_summary)
    return new_video


def main():
    # Git Bash (mintty) では標準出力がパイプ扱いになり、表示がためられて遅れて出る。1 行ごとに出す
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        description="動画/音声を文字起こし→要約→自動リネーム",
    )
    parser.add_argument("video", help="動画/音声ファイルパス")
    parser.add_argument("--no-rename", action="store_true",
                        help="リネームせず提案だけ表示")
    parser.add_argument("--no-diarize", action="store_true",
                        help="話者分離を無効化（高速化）")
    parser.add_argument("--no-summarize", action="store_true",
                        help="要約・タイトル生成をしない（LLM不要。"
                             "文字起こしだけ作り、リネームはしない）")
    parser.add_argument("--llm", choices=local_llm.BACKENDS, default=None,
                        help="要約・タイトル生成の頼み先 (default: 環境変数 LLM_BACKEND、"
                             "無ければ local)。claude / codex は文字起こしをクラウドへ送る")
    parser.add_argument("--batch-size", type=int, default=8,
                        help="stt.pyのbatch_size")
    parser.add_argument("--model", default="large-v2",
                        help="Whisperモデル (default: large-v2、VRAM不足ならmedium/small/tinyへ)")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"],
                        help="Whisper実行デバイス (default: cuda、VRAM不足ならcpu)")
    parser.add_argument("--diarize-device", default="auto",
                        choices=["auto", "cuda", "cpu"],
                        help="話者分離デバイス (default: auto: OOM時CPUへ自動)")
    parser.add_argument("--prefix", default="",
                        help="タイトル前に付ける接頭辞（例: '会議_'）")
    args = parser.parse_args()

    video_path = Path(args.video).resolve()
    if not video_path.exists():
        print(f"ファイルが見つかりません: {video_path}", file=sys.stderr)
        sys.exit(1)

    script_dir = Path(__file__).parent
    stt_py = script_dir / "stt.py"
    if not stt_py.exists():
        print(f"stt.py が見つかりません: {stt_py}", file=sys.stderr)
        sys.exit(1)

    # 要約・タイトル生成に LLM が要るので、文字起こし（数十分かかり得る）の前に
    # 確認し、local で落ちていれば起動する。llama-swap はモデルを最初の要約リクエストで
    # 読み込むので、ここで起動しても文字起こし中の Whisper と VRAM を取り合わない
    # --no-summarize なら LLM は一切使わないので、起動も確認もしない
    if not args.no_summarize:
        try:
            if args.llm:
                local_llm.set_backend(args.llm)
                # 下で起動する stt.py にも同じ頼み先を引き継ぐ
                os.environ["LLM_BACKEND"] = args.llm
            if local_llm.BACKEND != "local":
                print(f"注意: 要約とタイトル生成を {local_llm.backend_label()} に頼みます。"
                      "文字起こしの全文がクラウドへ送られます。")
            local_llm.ensure_server()
        except local_llm.LocalLLMError as e:
            print(e, file=sys.stderr)
            sys.exit(1)

    # 出力先は動画と同じディレクトリに一時的に置く
    transcript_txt = video_path.with_suffix(".txt")
    summary_txt = video_path.parent / f"{video_path.stem}.summary.txt"

    # stt.py を実行（--no-summarize 以外は要約付き）
    # -u: 文字起こしの進捗を、ためずにその場で出す (mintty で特に効く)
    cmd = [sys.executable, "-u", str(stt_py), str(video_path),
           "-o", str(transcript_txt),
           "--batch-size", str(args.batch_size),
           "--model", args.model,
           "--device", args.device,
           "--diarize-device", args.diarize_device]
    if not args.no_summarize:
        cmd.append("-s")
    if args.no_diarize:
        cmd.append("--no-diarize")
    print(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("stt.py がエラーで終了しました。", file=sys.stderr)
        sys.exit(1)

    # 要約なしではタイトルが作れないので、文字起こしの場所を知らせて終わる
    if args.no_summarize:
        print(f"\n文字起こし: {transcript_txt}")
        print("(--no-summarize 指定のため、要約・タイトル生成・リネームはしない)")
        return

    # 文字起こしが短すぎると、LLMが要約の代わりに「データ不足」等のメタ応答を返す
    # ことがあり、それがそのままタイトルに使われて内容と無関係な名前でリネームされる
    # （実測: 4秒/1文の音声を10回要約させると1回はこの症状が発生）。
    # 実際の会議・動画であればまず届かない長さなので、閾値未満は自動命名の対象外にする。
    # タイムスタンプ「  00:00:00 → 00:00:04  」(23文字/セグメント)や話者ラベル行は
    # 水増しになるため、判定は発話テキストのみで行う
    MIN_SPEECH_CHARS = 200
    transcript_text_check = transcript_txt.read_text(encoding="utf-8") if transcript_txt.exists() else ""
    speech_text = extract_speech_text(transcript_text_check)
    if len(speech_text.strip()) < MIN_SPEECH_CHARS:
        print(
            f"文字起こし（発話部分）が短すぎます（{len(speech_text.strip())}文字 < "
            f"{MIN_SPEECH_CHARS}文字）。自動命名は信頼できないため中止します。\n"
            f"  文字起こし結果: {transcript_txt}\n"
            "  手動でファイル名を付けてください。",
            file=sys.stderr,
        )
        sys.exit(1)

    if not summary_txt.exists():
        print(f"要約ファイルが見つかりません: {summary_txt}", file=sys.stderr)
        sys.exit(1)

    summary_text = summary_txt.read_text(encoding="utf-8")
    if not summary_text.strip():
        print(f"要約が空です: {summary_txt}", file=sys.stderr)
        sys.exit(1)

    # タイトル生成
    title = generate_title(summary_text)
    if not title:
        print("タイトル生成に失敗しました。", file=sys.stderr)
        sys.exit(1)

    # 新ファイル名を決定
    date = get_date(video_path)
    new_base = f"{date}_{args.prefix}{title}"

    print()
    print(f"旧: {video_path.name}")
    print(f"新: {new_base}{video_path.suffix}")

    if args.no_rename:
        print("(--no-rename 指定のためリネームはスキップ)")
        return

    try:
        new_video = rename_with_sidecars(video_path, new_base)
        print(f"\nリネーム完了: {new_video}")
    except FileExistsError as e:
        print(f"\nエラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
