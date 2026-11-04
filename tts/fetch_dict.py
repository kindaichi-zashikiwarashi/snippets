"""
英単語のカタカナ読みを e-words.jp（IT用語辞典）から取得して
dictionary.csv 追加用のCSV行を出力する。

Usage:
  # 単語を直接指定
  python fetch_dict.py Storage Functions Kubernetes

  # 標準入力から
  echo -e "Storage\nFunctions" | python fetch_dict.py

  # ファイルから
  python fetch_dict.py --file words.txt

  # dictionary.csvに自動追記（重複は無視）
  python fetch_dict.py --append Storage Functions

出力形式: 「カタカナ,英語」のCSV行（dictionary.csv と同じ形式）
"""

import argparse
import csv
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DICT_PATH = Path(__file__).parent / "dictionary.csv"
UA = "tts-dict-fetch/1.0"
SLEEP_SEC = 0.5  # 連続アクセス時の負荷軽減


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        # e-words.jp は EUC-JP / UTF-8 が混在することがあるので両方試す
        raw = r.read()
    for enc in ("utf-8", "euc-jp", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


_KATA_RE = r"[ァ-ヿー・\s]+"


def _extract_from_html(html, word):
    """e-words.jp ページHTMLからカタカナ読みを抽出する"""
    # パターン1: 「読み方 ： カタカナ」または「読み方:カタカナ」
    m = re.search(rf"読み方\s*[：:]\s*({_KATA_RE})", html)
    if m:
        return re.sub(r"\s+", "", m.group(1))

    # パターン2: <h1>カタカナ【English】...</h1>
    m = re.search(r"<h1[^>]*>([^<【]+)【", html)
    if m:
        candidate = m.group(1).strip()
        if re.fullmatch(_KATA_RE, candidate):
            return re.sub(r"\s+", "", candidate)

    # パターン3: <title>カタカナとは...</title>
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        title = m.group(1)
        m2 = re.match(rf"({_KATA_RE})(?:とは|【)", title)
        if m2:
            return re.sub(r"\s+", "", m2.group(1))

    return None


def fetch_reading(word):
    """e-words.jp から英単語のカタカナ読みを取得する。失敗時None"""
    # URL候補を順に試す
    candidates = [
        word.replace(" ", "_"),
        word.replace(" ", ""),
        word.lower().replace(" ", "_"),
    ]
    seen = set()
    for variant in candidates:
        if variant in seen:
            continue
        seen.add(variant)
        url = f"https://e-words.jp/w/{urllib.parse.quote(variant)}.html"
        try:
            html = http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            print(f"  [HTTP {e.code}] {word}: {url}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  [取得失敗] {word}: {e}", file=sys.stderr)
            continue
        reading = _extract_from_html(html, word)
        if reading:
            return reading
    return None


def load_existing_dict():
    """dictionary.csv の現在のエントリ（小文字化したwritten側）を集合で返す"""
    existing = set()
    if not DICT_PATH.exists():
        return existing
    with open(DICT_PATH, "r", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or not row[0] or row[0].startswith("#"):
                continue
            if row[0].strip().lower() == "reading":
                continue
            if len(row) >= 2 and row[1].strip():
                existing.add(row[1].strip().lower())
    return existing


def append_to_dict(entries):
    if not entries:
        return
    with open(DICT_PATH, "a", encoding="utf-8", newline="") as f:
        f.write("\n# fetch_dict.py で自動追加（e-words.jp 由来）\n")
        writer = csv.writer(f, lineterminator="\n")
        for reading, written in entries:
            writer.writerow([reading, written])


def main():
    parser = argparse.ArgumentParser(description="e-words.jp から英単語のカタカナ読みを取得")
    parser.add_argument("words", nargs="*", help="対象の英単語")
    parser.add_argument("--file", help="単語リストファイル（1行1単語）")
    parser.add_argument("--append", action="store_true",
                        help="dictionary.csvに自動追記（重複は無視）")
    args = parser.parse_args()

    words = list(args.words)
    if args.file:
        words.extend(Path(args.file).read_text(encoding="utf-8").splitlines())
    if not words and not sys.stdin.isatty():
        words.extend(sys.stdin.read().splitlines())
    words = [w.strip() for w in words if w and w.strip()]

    if not words:
        parser.print_help()
        sys.exit(1)

    existing = load_existing_dict() if args.append else set()
    new_entries = []

    print(f"# {len(words)}件を取得中...", file=sys.stderr)
    for i, word in enumerate(words):
        if args.append and word.lower() in existing:
            print(f"  [skip] {word} (既存)", file=sys.stderr)
            continue
        reading = fetch_reading(word)
        if reading:
            print(f"{reading},{word}")
            new_entries.append((reading, word))
        else:
            print(f"# 読み未取得: {word}")
        if i < len(words) - 1:
            time.sleep(SLEEP_SEC)

    if args.append and new_entries:
        append_to_dict(new_entries)
        print(f"\n# {len(new_entries)}件を {DICT_PATH.name} に追記しました",
              file=sys.stderr)


if __name__ == "__main__":
    main()
