"""
サーバー不要のTTSツール（コマンド一発で変換）

Usage:
  python tts.py "読み上げたいテキスト" -o output.wav
  python tts.py input.md -o output.wav
  python tts.py input.html -o output.wav
  echo "テキスト" | python tts.py -o output.wav

対応: テキスト直接指定、.txt, .md, .html, .htm, stdin
"""

import os
import subprocess
import sys
from pathlib import Path

# --- venv自動検出 ---
_VENV_CANDIDATES = [
    Path("C:/tmp/sbv2_venv/Scripts/python.exe"),                          # Windows
    Path.home() / "tts" / "Style-Bert-VITS2" / "venv" / "bin" / "python", # Ubuntu
    Path(__file__).parent / "Style-Bert-VITS2" / "venv" / "Scripts" / "python.exe",
    Path(__file__).parent / "Style-Bert-VITS2" / "venv" / "bin" / "python",
]
_REEXEC_FLAG = "_TTS_VENV_REEXEC"

def _in_venv():
    if os.environ.get("VIRTUAL_ENV"):
        return True
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)

if not os.environ.get(_REEXEC_FLAG) and not _in_venv():
    for _venv_py in _VENV_CANDIDATES:
        if _venv_py.exists():
            print(f"venvを自動検出: {_venv_py}")
            env = os.environ.copy()
            env[_REEXEC_FLAG] = "1"
            try:
                r = subprocess.run([str(_venv_py)] + sys.argv, env=env)
                sys.exit(r.returncode)
            except KeyboardInterrupt:
                sys.exit(0)

# --- ここからvenv内で実行される ---

import io
import re
import struct
import argparse
from html.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).parent / "Style-Bert-VITS2"))
from eng2kana import remove_unreadable, replace_english_words, replace_japanese_readings, add_pauses, PARAGRAPH_BREAK


# --- HTML パーサー ---

class HTMLTextExtractor(HTMLParser):
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


# --- テキスト前処理 ---

def strip_markdown(text):
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\[(.+?)\]\(.*?\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^\|[-:| ]+\|$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\|", " ", text)
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*_]{3,}$", "", text, flags=re.MULTILINE)
    return text


def extract_html(text):
    parser = HTMLTextExtractor()
    parser.feed(text)
    return parser.get_text()


def preprocess(text, input_type=None):
    if input_type == ".html" or input_type == ".htm":
        text = extract_html(text)
    elif input_type == ".md":
        text = strip_markdown(text)

    text = re.sub(r"[━─=\-]{3,}", "", text)
    text = replace_japanese_readings(text)  # %→パーセント等を先に処理
    text = remove_unreadable(text)
    text = replace_english_words(text)
    # 英字を含む孤立トークンだけ削除（純粋な数字は残す）
    text = re.sub(r"\b(?=[a-zA-Z0-9_.\-/\\]*[a-zA-Z])[a-zA-Z0-9_.\-/\\]{2,}\b", "", text)
    text = add_pauses(text)
    return text.strip()


def split_sentences(text, max_len=100):
    raw_parts = re.split(r"(?<=[。！？])", text)
    sentences = []
    pending_break = False
    for part in raw_parts:
        part = part.strip()
        while part.startswith(PARAGRAPH_BREAK):
            pending_break = True
            part = part[1:].lstrip()
        if not part:
            continue
        if not re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", part):
            continue
        prefix = PARAGRAPH_BREAK if pending_break else ""
        pending_break = False
        if len(part) > max_len:
            sub_parts = re.split(r"(?<=[、,])", part)
            buf = ""
            first = True
            for sp in sub_parts:
                if len(buf) + len(sp) > max_len:
                    if buf:
                        sentences.append((prefix if first else "") + buf)
                        first = False
                    buf = sp[:max_len]
                else:
                    buf += sp
            if buf:
                sentences.append((prefix if first else "") + buf)
        else:
            sentences.append(prefix + part)
    return sentences


# --- WAV処理 ---

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


def make_silence_pcm(sample_rate, channels, bits_per_sample, duration_sec):
    num_samples = int(sample_rate * duration_sec)
    return b"\x00" * (num_samples * channels * bits_per_sample // 8)


# --- メイン ---

def main():
    parser = argparse.ArgumentParser(description="サーバー不要TTS（テキスト/Markdown/HTML→WAV）")
    parser.add_argument("input", nargs="?", default=None,
                        help="テキスト文字列 or ファイルパス (.txt/.md/.html)。省略時はstdinから読む")
    parser.add_argument("--output", "-o", default="output.wav", help="出力WAVファイル")
    parser.add_argument("--model-id", "-m", type=int, default=0, help="モデルID (0=amitaro)")
    parser.add_argument("--pause", type=float, default=0.5, help="文間の無音（秒）")
    parser.add_argument("--dry-run", action="store_true", help="前処理結果だけ表示")
    parser.add_argument("--clipboard", "-c", action="store_true",
                        help="クリップボードの内容を入力にする")
    parser.add_argument("--play", action="store_true",
                        help="生成後にffplayで自動再生する")
    args = parser.parse_args()

    # 入力テキスト取得
    input_type = None
    if args.clipboard:
        import pyperclip
        text = pyperclip.paste()
        if not text:
            print("クリップボードが空です。", file=sys.stderr)
            sys.exit(1)
        print(f"入力: クリップボード ({len(text)}文字)")
    elif args.input is None:
        text = sys.stdin.read()
    elif Path(args.input).is_file():
        p = Path(args.input)
        input_type = p.suffix.lower()
        text = p.read_text(encoding="utf-8")
        print(f"入力: {p} ({input_type})")
    else:
        text = args.input

    # 前処理
    processed = preprocess(text, input_type)
    sentences = split_sentences(processed)
    print(f"文数: {len(sentences)}")

    if args.dry_run:
        for i, s in enumerate(sentences):
            print(f"  [{i+1}] {s}")
        return

    if not sentences:
        print("読み上げ可能なテキストがありません。")
        return

    # モデル読み込み（初回のみ、数秒かかる）
    print("モデルを読み込み中...")
    import torch
    from style_bert_vits2.constants import Languages
    from style_bert_vits2.nlp import bert_models
    from style_bert_vits2.tts_model import TTSModel

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"デバイス: {device}")

    bert_models.load_model(Languages.JP, device_map=device)
    bert_models.load_tokenizer(Languages.JP)

    model_root = Path(__file__).parent / "Style-Bert-VITS2" / "model_assets"

    # モデル一覧
    MODELS = {
        0: ("amitaro", "amitaro.safetensors", "config.json", "style_vectors.npy"),
        1: ("jvnv-F1-jp", "jvnv-F1-jp_e160_s14000.safetensors", "config.json", "style_vectors.npy"),
        2: ("jvnv-F2-jp", "jvnv-F2_e166_s20000.safetensors", "config.json", "style_vectors.npy"),
        3: ("jvnv-M1-jp", "jvnv-M1-jp_e158_s14000.safetensors", "config.json", "style_vectors.npy"),
        4: ("jvnv-M2-jp", "jvnv-M2-jp_e159_s17000.safetensors", "config.json", "style_vectors.npy"),
        5: ("koharune-ami", "koharune-ami.safetensors", "config.json", "style_vectors.npy"),
    }

    mid = args.model_id
    if mid not in MODELS:
        print(f"モデルID {mid} が見つかりません。0-5を指定してください。")
        return

    name, weights, cfg, style_vec = MODELS[mid]
    model = TTSModel(
        model_path=model_root / name / weights,
        config_path=model_root / name / cfg,
        style_vec_path=model_root / name / style_vec,
        device=device,
    )

    print(f"モデル: {name}")
    print()

    # 音声合成
    from scipy.io import wavfile
    import numpy as np

    all_pcm = b""
    sample_rate = None

    for i, sentence in enumerate(sentences):
        # 段落マーカー検出（改行直後の文は無音を長くとる）
        paragraph_break = sentence.startswith(PARAGRAPH_BREAK)
        if paragraph_break:
            sentence = sentence.lstrip(PARAGRAPH_BREAK).lstrip()
        print(f"[{i+1}/{len(sentences)}] {sentence[:50]}...")
        try:
            sr, audio = model.infer(
                text=sentence,
                language=Languages.JP,
                speaker_id=0,
                sdp_ratio=0.0,
                noise=0.0,
                noise_w=0.0,
                length=0.85,
                style="Neutral",
                style_weight=0.0,
            )
            if sample_rate is None:
                sample_rate = sr
            # numpy array → bytes
            pcm = audio.tobytes()
            if all_pcm:
                pause_sec = args.pause * (3.0 if paragraph_break else 1.0)
                all_pcm += make_silence_pcm(sample_rate, 1, 16, pause_sec)
            all_pcm += pcm
        except Exception as e:
            print(f"  エラー: {e}")
            continue

    if sample_rate is None:
        print("音声が生成されませんでした。")
        return

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(make_wav(sample_rate, 1, 16, all_pcm))
    print(f"\n完了！ {output_path} ({output_path.stat().st_size / 1024 / 1024:.1f}MB)")

    # --play指定時は自動再生
    if args.play:
        import shutil as _shutil
        ffplay_bin = _shutil.which("ffplay")
        if not ffplay_bin:
            # Windows の既定場所をフォールバック
            for p in [str(Path.home() / "ffmpeg" / "bin" / "ffplay.exe")]:
                if Path(p).exists():
                    ffplay_bin = p
                    break
        if ffplay_bin:
            import subprocess
            print("再生中...")
            subprocess.run(
                [ffplay_bin, "-nodisp", "-autoexit", "-loglevel", "error",
                 "-hide_banner", str(output_path)],
                stdin=subprocess.DEVNULL,
            )
        else:
            print("ffplay が見つかりません。再生をスキップします。", file=sys.stderr)


if __name__ == "__main__":
    main()
