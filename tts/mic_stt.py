"""
マイク音声→文字起こし→クリップボード転送ツール

Usage:
  python mic_stt.py
  python mic_stt.py --model large-v2
  python mic_stt.py --device 1          # マイクデバイス指定
  python mic_stt.py --list-devices      # デバイス一覧表示

キー操作:
  m  マイクミュート/ミュート解除
  q  終了

動作:
  1. マイクで音声を検知（VAD: 音声区間検出）
  2. 発話が終わったらfaster-whisperで文字起こし
  3. 認識結果をクリップボードにコピー
"""

import os
import sys
import subprocess
from pathlib import Path

# PyTorchとctranslate2のOpenMPランタイム重複を回避
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# --- venv自動検出 ---
_VENV_CANDIDATES = [
    Path("C:/tmp/sbv2_venv/Scripts/python.exe"),
    Path.home() / "tts" / "Style-Bert-VITS2" / "venv" / "bin" / "python",
    Path(__file__).parent / "Style-Bert-VITS2" / "venv" / "Scripts" / "python.exe",
    Path(__file__).parent / "Style-Bert-VITS2" / "venv" / "bin" / "python",
]
_REEXEC_FLAG = "_MIC_STT_VENV_REEXEC"

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

# --- .env読み込み ---
def _load_dotenv():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value

_load_dotenv()

# --- ここからvenv内で実行される ---

import argparse
import queue
import threading
import warnings
import numpy as np
import sounddevice as sd
import pyperclip

# faster-whisperの無害なWarningを抑制
warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")


# --- VAD (音声区間検出) ---

def load_vad_model():
    """Silero VADモデルを読み込む（GPU利用可能ならGPUへ）"""
    import torch
    model, utils = torch.hub.load(
        repo_or_dir="snakers4/silero-vad",
        model="silero_vad",
        force_reload=False,
        trust_repo=True,
    )
    if torch.cuda.is_available():
        model = model.cuda()
    return model


def is_speech(vad_model, audio_chunk, sample_rate=16000, threshold=0.5):
    """音声区間かどうかを判定（512サンプル単位でVADに渡す）"""
    import torch
    window_size = 512  # Silero VADが要求する16kHz時のウィンドウサイズ
    num_windows = len(audio_chunk) // window_size
    if num_windows == 0:
        return False
    device = next(vad_model.parameters()).device
    speech_count = 0
    for i in range(num_windows):
        window = audio_chunk[i * window_size:(i + 1) * window_size]
        audio_tensor = torch.from_numpy(window).float().to(device)
        confidence = vad_model(audio_tensor, sample_rate).item()
        if confidence > threshold:
            speech_count += 1
    # ウィンドウの半分以上で音声検出なら発話とみなす
    return speech_count > num_windows / 2


def dedupe_repetitions(text):
    """繰り返しフレーズを1回に圧縮（Whisperのハルシネーション対策）"""
    import re
    prev = ""
    while prev != text:
        prev = text
        # 3文字以上のフレーズが連続2回以上繰り返されたら1回に圧縮
        text = re.sub(r"(.{3,}?)\1+", r"\1", text)
    return text


# --- メイン ---

def main():
    parser = argparse.ArgumentParser(description="マイク音声→文字起こし→クリップボード")
    parser.add_argument("--model", "-m", default="large-v2",
                        help="Whisperモデル (default: large-v2)")
    parser.add_argument("--language", "-l", default="ja",
                        help="言語コード (default: ja)")
    parser.add_argument("--device", "-d", type=int, default=None,
                        help="マイクデバイスID（--list-devicesで確認）")
    parser.add_argument("--list-devices", action="store_true",
                        help="利用可能なオーディオデバイス一覧を表示")
    parser.add_argument("--threshold", "-t", type=float, default=0.7,
                        help="VAD閾値 (default: 0.7, 高いほど厳しい=雑音を弾く)")
    parser.add_argument("--silence", type=float, default=5.0,
                        help="発話終了と判定する無音秒数 (default: 5.0)")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    SAMPLE_RATE = 16000
    CHUNK_DURATION = 0.5  # VAD判定の単位（秒）
    CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_DURATION)
    SILENCE_TIMEOUT = args.silence  # 無音がこの秒数続いたら発話終了と判定
    MIN_SPEECH_DURATION = 0.5  # 最低発話時間（秒）短すぎるノイズを除外
    PRE_BUFFER_DURATION = 1.0  # 発話検知前のバッファ（秒）冒頭の切れ防止

    # Whisperモデル読み込み（GPUのVRAMに応じてcompute_typeを自動選択）
    import torch
    from faster_whisper import WhisperModel
    gpu_props = torch.cuda.get_device_properties(0)
    vram_gb = gpu_props.total_memory / (1024 ** 3)
    # VRAM 16GB以上ならfloat16（最速）、未満ならint8_float16（VRAM半減）
    compute_type = "float16" if vram_gb >= 16 else "int8_float16"
    print(f"GPU: {gpu_props.name} ({vram_gb:.1f}GB) → {compute_type}")
    print(f"Whisperモデル読み込み中 ({args.model})...")
    whisper_model = WhisperModel(args.model, device="cuda", compute_type=compute_type)

    # 辞書（dictionary.csv）を後処理用にロード
    # CSVは `reading,written` 形式。STTでは左→右で置換（カナ/ひらがな → 正しい表記）
    dictionary = {}
    dict_path = Path(__file__).parent / "dictionary.csv"
    if dict_path.exists():
        import csv as _csv
        with open(dict_path, "r", encoding="utf-8") as f:
            reader = _csv.reader(f)
            for row in reader:
                if not row or not row[0] or row[0].startswith("#"):
                    continue
                if row[0].strip().lower() == "reading":
                    continue
                if len(row) >= 2 and row[1].strip():
                    # reading (col1) → written (col2)
                    dictionary[row[0].strip()] = row[1].strip()
        if dictionary:
            print(f"辞書: {len(dictionary)}語を後処理で置換")

    # キーを英語/非英語に分類
    en_dict = {}
    other_dict = {}
    for k, v in dictionary.items():
        if k.isascii() and k.replace("_", "").isalnum():
            en_dict[k.lower()] = v
        else:
            other_dict[k] = v
    other_keys_sorted = sorted(other_dict.keys(), key=len, reverse=True)

    def apply_dict(text):
        import re
        if not dictionary:
            return text
        if en_dict:
            text = re.sub(r"[A-Za-z]+",
                          lambda m: en_dict.get(m.group(0).lower(), m.group(0)),
                          text)
        # カナ/ひらがな語は「同種文字で前後を囲まれてない」場合のみ置換
        for key in other_keys_sorted:
            value = other_dict[key]
            if all("\u30a0" <= c <= "\u30ff" for c in key):
                # カタカナのみ: 前後がカタカナでない位置だけマッチ
                pattern = r"(?<![\u30a0-\u30ff])" + re.escape(key) + r"(?![\u30a0-\u30ff])"
                text = re.sub(pattern, value, text)
            elif all("\u3040" <= c <= "\u309f" for c in key):
                # ひらがなのみ: 前後がひらがなでない位置だけマッチ
                pattern = r"(?<![\u3040-\u309f])" + re.escape(key) + r"(?![\u3040-\u309f])"
                text = re.sub(pattern, value, text)
            else:
                text = text.replace(key, value)
        return text

    # VADモデル読み込み
    print("VADモデル読み込み中...")
    vad_model = load_vad_model()

    # ミュート状態
    muted = False
    running = True

    # キー入力スレッド
    def key_listener():
        nonlocal muted, running
        while running:
            try:
                # Windows
                import msvcrt
                if msvcrt.kbhit():
                    key = msvcrt.getch().decode("utf-8", errors="ignore").lower()
                    if key == "m":
                        muted = not muted
                        status = "ミュート" if muted else "ミュート解除"
                        print(f"\n[{status}]")
                    elif key == "q":
                        running = False
                        return
            except ImportError:
                # Linux/Mac
                import select
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    key = sys.stdin.read(1).lower()
                    if key == "m":
                        muted = not muted
                        status = "ミュート" if muted else "ミュート解除"
                        print(f"\n[{status}]")
                    elif key == "q":
                        running = False
                        return
            import time
            time.sleep(0.1)

    key_thread = threading.Thread(target=key_listener, daemon=True)
    key_thread.start()

    # 音声バッファ
    audio_queue = queue.Queue()

    def audio_callback(indata, frames, time_info, status):
        if not muted:
            audio_queue.put(indata[:, 0].copy())

    print()
    print("=== マイク音声 → クリップボード ===")
    print(f"モデル: {args.model} / 言語: {args.language}")
    print(f"キー: m=ミュート切替 q=終了")
    print("===================================")
    print()
    print("話しかけてください...")

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=CHUNK_SIZE,
            device=args.device,
            callback=audio_callback,
        ):
            from collections import deque
            speech_buffer = []
            silence_chunks = 0
            is_speaking = False
            silence_limit = int(SILENCE_TIMEOUT / CHUNK_DURATION)
            min_speech_chunks = int(MIN_SPEECH_DURATION / CHUNK_DURATION)
            pre_buffer_size = int(PRE_BUFFER_DURATION / CHUNK_DURATION)
            pre_buffer = deque(maxlen=pre_buffer_size)

            while running:
                try:
                    chunk = audio_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                speech_detected = is_speech(vad_model, chunk, SAMPLE_RATE, args.threshold)

                if speech_detected:
                    if not is_speaking:
                        is_speaking = True
                        # 事前バッファを先頭に入れて冒頭の切れを防止
                        speech_buffer = list(pre_buffer)
                        silence_chunks = 0
                        print("* ", end="", flush=True)
                    speech_buffer.append(chunk)
                    silence_chunks = 0
                elif is_speaking:
                    # 発話中の無音 → バッファに追加しつつ無音カウント
                    speech_buffer.append(chunk)
                    silence_chunks += 1

                    if silence_chunks >= silence_limit:
                        # 発話終了 → 文字起こし
                        is_speaking = False

                        if len(speech_buffer) < min_speech_chunks:
                            print("(短すぎるのでスキップ)")
                            continue

                        audio_data = np.concatenate(speech_buffer)
                        duration = len(audio_data) / SAMPLE_RATE
                        print(f"({duration:.1f}秒) → ", end="", flush=True)

                        # 文字起こし
                        segments, info = whisper_model.transcribe(
                            audio_data,
                            beam_size=5,
                            language=args.language,
                            no_repeat_ngram_size=3,
                            condition_on_previous_text=False,
                            repetition_penalty=1.2,
                            vad_filter=True,
                            compression_ratio_threshold=2.0,
                            log_prob_threshold=-0.8,
                            no_speech_threshold=0.6,
                        )
                        text = "".join(seg.text for seg in segments).strip()
                        # ループ/繰り返しハルシネーションを圧縮
                        text = dedupe_repetitions(text)
                        # 辞書で英単語→カタカナ置換
                        text = apply_dict(text)

                        if text:
                            pyperclip.copy(text)
                            print(f"「{text}」 → クリップボード")
                        else:
                            print("(認識できず)")
                else:
                    # 発話前の無音 → 事前バッファに蓄積
                    pre_buffer.append(chunk)

    except KeyboardInterrupt:
        pass

    running = False
    print("\n終了しました。")


if __name__ == "__main__":
    main()
