"""
テキスト/Markdown/HTMLファイルを音声化するスクリプト
Usage: python read_aloud.py <file> [--output output.wav]

対応形式: .txt, .md, .html, .htm
"""

import io
import re
import struct
import argparse
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from eng2kana import remove_unreadable, replace_english_words, add_pauses

API_URL = "http://127.0.0.1:5000/voice"


# --- HTML パーサー ---

class HTMLTextExtractor(HTMLParser):
    """HTMLからテキストを抽出する"""

    SKIP_TAGS = {"script", "style", "code", "pre", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.result = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.result.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.result.append(data)

    def get_text(self) -> str:
        return "".join(self.result)


def extract_html(text: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(text)
    return parser.get_text()


# --- Markdown 処理 ---

def strip_markdown(text: str) -> str:
    # コードブロック除去
    text = re.sub(r"```[\s\S]*?```", "", text)
    # インラインコード除去
    text = re.sub(r"`(.+?)`", r"\1", text)
    # 画像除去
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    # リンク → テキストだけ残す
    text = re.sub(r"\[(.+?)\]\(.*?\)", r"\1", text)
    # 見出し記号除去
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 太字・斜体
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    # テーブルのセパレータ行除去
    text = re.sub(r"^\|[-:| ]+\|$", "", text, flags=re.MULTILINE)
    # テーブルの | を除去
    text = re.sub(r"\|", " ", text)
    # リストマーカー除去
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)
    # 引用マーカー除去
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
    # 水平線除去
    text = re.sub(r"^[-*_]{3,}$", "", text, flags=re.MULTILINE)
    return text


# --- 共通テキスト前処理 ---

def clean_for_tts(text: str) -> str:
    # 罫線・記号の繰り返し除去
    text = re.sub(r"[━─=\-]{3,}", "", text)
    # 読み上げ不適切なトークンを除去（URL, パス, コマンド, コード等）
    text = remove_unreadable(text)
    # 英単語をカタカナに変換（alkana + 手動辞書）
    text = replace_english_words(text)
    # 変換できなかった英数字トークンを除去
    text = re.sub(r"\b[a-zA-Z0-9_.\-/\\]{2,}\b", "", text)
    # 空行整理
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 区切り文字に一拍置く
    text = add_pauses(text)
    return text.strip()


def load_and_clean(filepath: str) -> str:
    """ファイルを読み込んで形式に応じた前処理を行う"""
    path = Path(filepath)
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if suffix in (".html", ".htm"):
        text = extract_html(text)
    elif suffix == ".md":
        text = strip_markdown(text)
    # .txt やその他はそのまま

    return clean_for_tts(text)


def split_sentences(text: str, max_len: int = 100) -> list[str]:
    """テキストを文単位で分割する（API制限対策）"""
    # 句読点・改行で分割
    raw_parts = re.split(r"(?<=[。！？\n])", text)
    sentences = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        # 日本語が含まれないものはスキップ
        if not re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", part):
            continue
        # まだ長い場合は読点で分割
        if len(part) > max_len:
            sub_parts = re.split(r"(?<=[、,])", part)
            buf = ""
            for sp in sub_parts:
                if len(buf) + len(sp) > max_len:
                    if buf:
                        sentences.append(buf)
                    buf = sp[:max_len]
                else:
                    buf += sp
            if buf:
                sentences.append(buf)
        else:
            sentences.append(part)
    return sentences


# --- WAV処理 ---

def read_wav_data(wav_bytes: bytes) -> tuple[int, int, int, bytes]:
    with io.BytesIO(wav_bytes) as f:
        f.read(12)
        while True:
            chunk_id = f.read(4)
            if len(chunk_id) < 4:
                break
            chunk_size = struct.unpack("<I", f.read(4))[0]
            if chunk_id == b"fmt ":
                fmt_data = f.read(chunk_size)
                channels = struct.unpack("<H", fmt_data[2:4])[0]
                sample_rate = struct.unpack("<I", fmt_data[4:8])[0]
                bits_per_sample = struct.unpack("<H", fmt_data[14:16])[0]
            elif chunk_id == b"data":
                pcm_data = f.read(chunk_size)
                return sample_rate, channels, bits_per_sample, pcm_data
            else:
                f.read(chunk_size)
    raise ValueError("Invalid WAV data")


def make_wav(sample_rate, channels, bits_per_sample, pcm_data):
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm_data)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample,
        b"data", data_size,
    )
    return header + pcm_data


def make_silence(sample_rate, channels, bits_per_sample, duration_sec):
    num_samples = int(sample_rate * duration_sec)
    return b"\x00" * (num_samples * channels * bits_per_sample // 8)


# --- 音声合成 ---

def synthesize(text: str, model_id: int = 0) -> bytes:
    params = urllib.parse.urlencode({
        "text": text,
        "model_id": model_id,
        "speaker_id": 0,
        "style": "Neutral",
        "style_weight": 0.0,
        "sdp_ratio": 0.0,
        "noise": 0.0,
        "noisew": 0.0,
        "length": 0.85,
        "language": "JP",
    })
    url = f"{API_URL}?{params}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def main():
    parser = argparse.ArgumentParser(description="テキスト/Markdown/HTMLファイルを音声化")
    parser.add_argument("file", help="入力ファイル (.txt, .md, .html)")
    parser.add_argument("--output", "-o", default=None, help="出力WAVファイル（デフォルト: 入力ファイル名.wav）")
    parser.add_argument("--model", "-m", type=int, default=0, help="モデルID (0=amitaro)")
    parser.add_argument("--pause", type=float, default=0.5, help="文間の無音（秒）")
    parser.add_argument("--paragraph-pause", type=float, default=1.0, help="段落間の無音（秒）")
    parser.add_argument("--dry-run", action="store_true", help="テキスト前処理結果だけ表示（音声生成しない）")
    args = parser.parse_args()

    input_path = Path(args.file)
    if args.output is None:
        output_path = input_path.with_suffix(".wav")
    else:
        output_path = Path(args.output)

    print(f"入力: {input_path} ({input_path.suffix})")
    text = load_and_clean(str(input_path))
    sentences = split_sentences(text)
    print(f"文数: {len(sentences)}")

    if args.dry_run:
        for i, s in enumerate(sentences):
            print(f"  [{i+1}] {s}")
        return

    all_pcm = b""
    sample_rate = channels = bits_per_sample = None
    success_count = 0

    for i, sentence in enumerate(sentences):
        print(f"[{i+1}/{len(sentences)}] {sentence[:50]}...")
        try:
            wav_bytes = synthesize(sentence, model_id=args.model)
            sr, ch, bps, pcm = read_wav_data(wav_bytes)
            if sample_rate is None:
                sample_rate, channels, bits_per_sample = sr, ch, bps
            if all_pcm:
                all_pcm += make_silence(sample_rate, channels, bits_per_sample, args.pause)
            all_pcm += pcm
            success_count += 1
        except Exception as e:
            print(f"  エラー: {e}")
            continue

    if success_count == 0:
        print("音声が生成されませんでした。")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(make_wav(sample_rate, channels, bits_per_sample, all_pcm))
    print(f"\n完了！ {output_path} （{success_count}文, {output_path.stat().st_size / 1024 / 1024:.1f}MB）")


if __name__ == "__main__":
    main()
