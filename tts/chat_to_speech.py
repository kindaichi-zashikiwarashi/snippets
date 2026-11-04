"""
会話ログをStyle-Bert-VITS2で音声化するスクリプト
Usage: python chat_to_speech.py <会話ログ.txt> [--output output.wav]
"""

import io
import re
import struct
import argparse
import urllib.request
import urllib.parse
from pathlib import Path

from eng2kana import remove_unreadable, replace_english_words, add_pauses

API_URL = "http://127.0.0.1:5000/voice"


def clean_for_tts(text: str) -> str:
    """TTSに適したテキストに変換する"""
    # ツール呼び出し行を除去
    text = re.sub(r"\[ツール:.*?\].*", "", text)
    # Markdown装飾を除去（太字→テキスト、リストマーカー除去）
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^[-*#>]+\s*", "", text, flags=re.MULTILINE)
    # 罫線・記号の繰り返し除去
    text = re.sub(r"[━─=\-]{3,}", "", text)
    # 読み上げ不適切なトークンを除去（URL, パス, コマンド, コード等）
    text = remove_unreadable(text)
    # 英単語をカタカナに変換（alkana + 手動辞書）
    text = replace_english_words(text)
    # 変換できなかった英数字トークンを除去
    text = re.sub(r"\b[a-zA-Z0-9_.\-/\\]{2,}\b", "", text)
    # 空行整理
    text = re.sub(r"\n{2,}", "\n", text).strip()
    # 区切り文字に一拍置く
    text = add_pauses(text)
    return text


def parse_chat_log(filepath: str) -> list[dict]:
    """会話ログをパースして発話リストを返す"""
    text = Path(filepath).read_text(encoding="utf-8")
    entries = []

    pattern = r"【(ユーザー|Claude)】\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\n"
    parts = re.split(pattern, text)

    i = 1
    while i < len(parts) - 1:
        speaker = parts[i]
        content = clean_for_tts(parts[i + 1])

        # 日本語が含まれない or 短すぎるものはスキップ
        has_japanese = bool(re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", content))
        if content and len(content) > 1 and has_japanese:
            entries.append({"speaker": speaker, "text": content})
        i += 2

    return entries


def synthesize(text: str, model_id: int = 0, speaker_id: int = 0, style: str = "Neutral") -> bytes:
    """Style-Bert-VITS2のAPIで音声合成"""
    params = urllib.parse.urlencode({
        "text": text,
        "model_id": model_id,
        "speaker_id": speaker_id,
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


def read_wav_data(wav_bytes: bytes) -> tuple[int, int, int, bytes]:
    """WAVバイトからサンプルレート、チャンネル数、ビット深度、PCMデータを読む"""
    with io.BytesIO(wav_bytes) as f:
        f.read(12)  # RIFF header
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


def make_wav(sample_rate: int, channels: int, bits_per_sample: int, pcm_data: bytes) -> bytes:
    """PCMデータからWAVファイルを生成"""
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


def make_silence(sample_rate: int, channels: int, bits_per_sample: int, duration_sec: float) -> bytes:
    """無音のPCMデータを生成"""
    num_samples = int(sample_rate * duration_sec)
    return b"\x00" * (num_samples * channels * bits_per_sample // 8)


def main():
    parser = argparse.ArgumentParser(description="会話ログを音声化")
    parser.add_argument("logfile", help="会話ログファイル")
    parser.add_argument("--output", default="output.wav", help="出力WAVファイル")
    parser.add_argument("--max-entries", type=int, default=None, help="最大発話数")
    parser.add_argument("--pause", type=float, default=0.8, help="発話間の無音（秒）")
    parser.add_argument("--user-model", type=int, default=0, help="ユーザー声のmodel_id (0=amitaro, 5=koharune-ami)")
    args = parser.parse_args()

    entries = parse_chat_log(args.logfile)
    if args.max_entries:
        entries = entries[:args.max_entries]

    print(f"発話数: {len(entries)}")

    all_pcm = b""
    sample_rate = channels = bits_per_sample = None
    success_count = 0

    for i, entry in enumerate(entries):
        speaker = entry["speaker"]
        text = entry["text"]
        # API制限: 100文字
        if len(text) > 100:
            text = text[:100]

        label = "user" if speaker == "ユーザー" else "claude"
        # model_id: 0=amitaro, 5=koharune-ami
        # user_model_idはコマンドライン引数で切り替え
        model_id = args.user_model if speaker == "ユーザー" else 0

        print(f"[{i+1}/{len(entries)}] {label}: {text[:50]}...")
        try:
            wav_bytes = synthesize(text, model_id=model_id)
            sr, ch, bps, pcm = read_wav_data(wav_bytes)
            if sample_rate is None:
                sample_rate, channels, bits_per_sample = sr, ch, bps
            # 発話間に無音を挿入
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

    # 1ファイルに結合して出力
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(make_wav(sample_rate, channels, bits_per_sample, all_pcm))

    print(f"\n完了！ {output_path} に出力しました（{success_count}件, {output_path.stat().st_size / 1024 / 1024:.1f}MB）")


if __name__ == "__main__":
    main()
