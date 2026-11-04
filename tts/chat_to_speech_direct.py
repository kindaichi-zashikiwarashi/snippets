"""
会話ログをStyle-Bert-VITS2で音声化するスクリプト（サーバー不要版）
Usage: python chat_to_speech_direct.py <会話ログ.txt> [--output-dir output]
"""

import re
import sys
import argparse
from pathlib import Path

import numpy as np
from scipy.io import wavfile

# Style-Bert-VITS2のパスを追加
sys.path.insert(0, str(Path(__file__).parent / "Style-Bert-VITS2"))

import torch
from style_bert_vits2.constants import Languages
from style_bert_vits2.nlp import bert_models
from style_bert_vits2.tts_model import TTSModel


def parse_chat_log(filepath: str) -> list[dict]:
    """会話ログをパースして発話リストを返す"""
    text = Path(filepath).read_text(encoding="utf-8")
    entries = []

    pattern = r"【(ユーザー|Claude)】\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\n"
    parts = re.split(pattern, text)

    i = 1
    while i < len(parts) - 1:
        speaker = parts[i]
        content = parts[i + 1].strip()
        content = re.sub(r"\[ツール:.*?\].*", "", content)
        content = re.sub(r"```[\s\S]*?```", "", content)
        content = re.sub(r"\*\*(.+?)\*\*", r"\1", content)
        content = re.sub(r"`(.+?)`", r"\1", content)
        content = re.sub(r"^[-*#>]+\s*", "", content, flags=re.MULTILINE)
        content = re.sub(r"\n{2,}", "\n", content).strip()

        if content and len(content) > 1 and not re.match(r"^[a-zA-Z0-9_\-./\\:\s]+$", content):
            if len(content) > 100:
                content = content[:100]
            entries.append({"speaker": speaker, "text": content})
        i += 2

    return entries


def main():
    parser = argparse.ArgumentParser(description="会話ログを音声化（サーバー不要版）")
    parser.add_argument("logfile", help="会話ログファイル")
    parser.add_argument("--output-dir", default="output", help="出力ディレクトリ")
    parser.add_argument("--max-entries", type=int, default=None, help="最大発話数")
    args = parser.parse_args()

    entries = parse_chat_log(args.logfile)
    if args.max_entries:
        entries = entries[:args.max_entries]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"デバイス: {device}")

    # BERTモデル読み込み
    print("BERTモデルを読み込み中...")
    bert_models.load_model(Languages.JP, device_map=device)
    bert_models.load_tokenizer(Languages.JP)

    # TTSモデル読み込み
    model_root = Path(__file__).parent / "Style-Bert-VITS2" / "model_assets"

    print("音声モデルを読み込み中...")
    # ユーザー用: 男性声 (jvnv-M1-jp)
    user_model = TTSModel(
        model_path=model_root / "jvnv-M1-jp" / "jvnv-M1-jp_e158_s14000.safetensors",
        config_path=model_root / "jvnv-M1-jp" / "config.json",
        style_vec_path=model_root / "jvnv-M1-jp" / "style_vectors.npy",
        device=device,
    )

    # Claude用: 女性声 (jvnv-F1-jp)
    claude_model = TTSModel(
        model_path=model_root / "jvnv-F1-jp" / "jvnv-F1-jp_e160_s14000.safetensors",
        config_path=model_root / "jvnv-F1-jp" / "config.json",
        style_vec_path=model_root / "jvnv-F1-jp" / "style_vectors.npy",
        device=device,
    )

    print(f"\n発話数: {len(entries)}")
    for i, entry in enumerate(entries):
        speaker = entry["speaker"]
        text = entry["text"]
        label = "user" if speaker == "ユーザー" else "claude"
        model = user_model if speaker == "ユーザー" else claude_model
        filename = out_dir / f"{i:04d}_{label}.wav"

        print(f"[{i+1}/{len(entries)}] {label}: {text[:50]}...")
        try:
            sr, audio = model.infer(
                text=text,
                language=Languages.JP,
                speaker_id=0,
                style="Neutral",
            )
            wavfile.write(str(filename), sr, audio)
        except Exception as e:
            print(f"  エラー: {e}")
            continue

    print(f"\n完了！ {out_dir} に音声ファイルを出力しました。")


if __name__ == "__main__":
    main()
