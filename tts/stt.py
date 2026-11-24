"""
動画/音声ファイルの文字起こしツール（話者分離対応）

Usage:
  python stt.py video.mp4 -o transcript.txt
  python stt.py audio.wav -o transcript.txt
  python stt.py meeting.flv --no-diarize -o transcript.txt

対応: .mp4, .flv, .avi, .mkv, .webm, .mov, .ts, .wmv / .wav, .mp3, .m4a, .flac, .ogg, .aac, .wma
      （どれも ffmpeg で 16kHz mono WAV に変換してから処理する）

必要パッケージ:
  pip install -r requirements-stt.txt

話者分離を使う場合:
  HF_TOKEN 環境変数に HuggingFace トークンを設定すること
  （pyannote/speaker-diarization へのアクセス権が必要）

要約 (--summarize) を使う場合:
  既定はローカルLLM (:8081)。応答しなければ systemd の llm-swap を自動で起動する。
  接続先やモデルは環境変数で変えられる。--llm claude / codex でクラウドにも頼める
  (詳しくは local_llm.py の冒頭)
"""

import os
import subprocess
import sys
from pathlib import Path

# PyTorchとctranslate2のOpenMPランタイム重複を回避
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# --- venv自動検出 ---
# miseのシステムPython等で実行された場合、正しいvenvのPythonで再実行する

_VENV_CANDIDATES = [
    Path("C:/tmp/sbv2_venv/Scripts/python.exe"),                          # Windows
    Path.home() / "tts" / "Style-Bert-VITS2" / "venv" / "bin" / "python", # Ubuntu
    Path(__file__).parent / "Style-Bert-VITS2" / "venv" / "Scripts" / "python.exe",
    Path(__file__).parent / "Style-Bert-VITS2" / "venv" / "bin" / "python",
]

_REEXEC_FLAG = "_STT_VENV_REEXEC"

def _in_venv():
    """既にvenv内で実行中かどうか"""
    if os.environ.get("VIRTUAL_ENV"):
        return True
    # sys.prefixがbase_prefixと異なればvenv内
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)

def _venv_cudnn_lib_dir(venv_py: Path) -> Path | None:
    """venv 同梱の nvidia-cudnn-cu* パッケージの lib ディレクトリを探す（Linuxのみ）"""
    site_packages_glob = venv_py.parent.parent.glob("lib/python3.*/site-packages")
    for sp in site_packages_glob:
        cudnn_lib = sp / "nvidia" / "cudnn" / "lib"
        if cudnn_lib.is_dir():
            return cudnn_lib
    return None

if not os.environ.get(_REEXEC_FLAG) and not _in_venv():
    for _venv_py in _VENV_CANDIDATES:
        if _venv_py.exists():
            print(f"venvを自動検出: {_venv_py}")
            env = os.environ.copy()
            env[_REEXEC_FLAG] = "1"
            # LD_LIBRARY_PATH に他venv(python3.10等)のcudnnが先に入っていると
            # ctranslate2がそちらを掴んで libcudnn_ops_infer.so.8 not found で
            # 落ちる（cudnnメジャーバージョン不一致）。venv自身のcudnnを先頭に置く
            _cudnn_dir = _venv_cudnn_lib_dir(_venv_py)
            if _cudnn_dir is not None:
                existing = env.get("LD_LIBRARY_PATH", "")
                env["LD_LIBRARY_PATH"] = f"{_cudnn_dir}:{existing}" if existing else str(_cudnn_dir)
            try:
                r = subprocess.run([str(_venv_py)] + sys.argv, env=env)
                sys.exit(r.returncode)
            except KeyboardInterrupt:
                sys.exit(0)

# --- .env読み込み ---
def _load_dotenv():
    """スクリプト隣の .env を読んで環境変数にセット（python-dotenv不要）"""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value

_load_dotenv()

# --- ここからvenv内で実行される ---

import argparse
import tempfile
import warnings
from datetime import timedelta

# pyannote.audio と faster-whisper の無害なWarningを抑制
warnings.filterwarnings("ignore", category=UserWarning, module="pyannote")
warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")
warnings.filterwarnings("ignore", message=".*std\\(\\): degrees of freedom.*")
warnings.filterwarnings("ignore", message=".*reproducibility.*")

VIDEO_EXTS = {".mp4", ".flv", ".avi", ".mkv", ".webm", ".mov", ".ts", ".wmv"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"}

FFMPEG_SEARCH_PATHS = [
    Path.home() / "ffmpeg" / "bin",                        # Windows
    Path(__file__).parent / ".ffmpeg" / "bin",
]


def find_ffmpeg():
    """ffmpeg実行ファイルを探す（Windows/Linux両対応）"""
    import shutil
    # 候補パスから探す
    for p in FFMPEG_SEARCH_PATHS:
        for name in ("ffmpeg.exe", "ffmpeg"):
            exe = p / name
            if exe.exists():
                return str(exe)
    # PATH上を探す
    found = shutil.which("ffmpeg")
    if found:
        return found
    print("ffmpegが見つかりません。インストールしてください。", file=sys.stderr)
    sys.exit(1)


def extract_audio(input_path, output_wav, ffmpeg_bin):
    """動画/音声ファイルから音声を16kHz mono WAVで抽出"""
    cmd = [
        ffmpeg_bin, "-y", "-i", str(input_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(output_wav),
    ]
    print(f"音声を16kHz mono WAVに変換中: {input_path}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ffmpegエラー:\n{r.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"  → {output_wav} ({output_wav.stat().st_size / 1024 / 1024:.1f}MB)")


def load_dictionary_map():
    """dictionary.csv を読み込んで STT用の「読み → 表記」マップを返す

    CSVは `reading,written` 形式。STTは左列→右列（reading→written）で使う。
    e.g. 「スラック」を「Slack」に置換。
    """
    import csv as _csv
    dict_path = Path(__file__).parent / "dictionary.csv"
    if not dict_path.exists():
        return {}
    result = {}
    with open(dict_path, "r", encoding="utf-8") as f:
        reader = _csv.reader(f)
        for row in reader:
            if not row or not row[0] or row[0].startswith("#"):
                continue
            if row[0].strip().lower() == "reading":
                continue
            if len(row) >= 2 and row[1].strip():
                # reading (col1) → written (col2)
                # 同じreadingが複数行ある場合は最初の登録（=正規表記）を採用。
                reading = row[0].strip()
                if reading not in result:
                    result[reading] = row[1].strip()
    return result


def apply_dictionary(text, dictionary):
    """辞書に従ってテキストを置換する

    - 英単語キー（ASCII英字のみ）: 単語境界付き正規表現で置換（大文字小文字無視）
    - 非ASCIIキー: 長いものから順に文字列置換（漢字・かな・カナの表記揺れ対応）
    """
    import re
    if not dictionary:
        return text

    # キーを英語/非英語に分類
    en_dict = {}
    other_dict = {}
    for k, v in dictionary.items():
        if k.isascii() and k.replace("_", "").isalnum():
            en_dict[k.lower()] = v
        else:
            other_dict[k] = v

    # 英単語: 正規表現で単語として置換
    if en_dict:
        def _replace(m):
            return en_dict.get(m.group(0).lower(), m.group(0))
        text = re.sub(r"[A-Za-z]+", _replace, text)

    # 非英語: 長いキーから順に置換（短いキーが先に消費されないように）
    # カナ/ひらがなは「同種文字で前後を囲まれてない」場合のみマッチ → パスワードの中のパスを誤置換しない
    for key in sorted(other_dict.keys(), key=len, reverse=True):
        value = other_dict[key]
        if all("\u30a0" <= c <= "\u30ff" for c in key):
            pattern = r"(?<![\u30a0-\u30ff])" + re.escape(key) + r"(?![\u30a0-\u30ff])"
            text = re.sub(pattern, value, text)
        elif all("\u3040" <= c <= "\u309f" for c in key):
            pattern = r"(?<![\u3040-\u309f])" + re.escape(key) + r"(?![\u3040-\u309f])"
            text = re.sub(pattern, value, text)
        else:
            text = text.replace(key, value)

    return text


def _run_batched_transcribe(batched_model, audio_path, language, batch_size, initial_prompt):
    """OOM時に batch_size を半減して自動リトライする"""
    import torch
    while batch_size >= 1:
        try:
            print(f"文字起こし中（batch_size={batch_size}, VAD有効）...")
            segments, info = batched_model.transcribe(
                str(audio_path),
                beam_size=5,
                language=language,
                batch_size=batch_size,
                vad_filter=True,
                initial_prompt=initial_prompt,
            )
            # 結果を即座にリストに展開（OOM検知のため）
            seg_list = list(segments)
            return seg_list, info, batch_size
        except RuntimeError as e:
            if "out of memory" not in str(e).lower():
                raise
            new_size = max(1, batch_size // 2)
            if new_size == batch_size:
                raise
            print(f"  VRAM不足。batch_sizeを {batch_size} → {new_size} に下げて再試行...")
            torch.cuda.empty_cache()
            batch_size = new_size
    raise RuntimeError("batch_size=1でもOOM。モデルサイズを下げてください（例: --model medium）")


def transcribe_simple(audio_path, model_size, language, batch_size=8, device="cuda"):
    """faster-whisperで文字起こし（話者分離なし、VAD+バッチ推論で高速化）"""
    from faster_whisper import WhisperModel, BatchedInferencePipeline

    compute_type = "float16" if device == "cuda" else "int8"
    print(f"モデル読み込み中 ({model_size}, device={device})...")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    batched_model = BatchedInferencePipeline(model=model)

    segments, info, _ = _run_batched_transcribe(
        batched_model, audio_path, language, batch_size, None,
    )
    print(f"言語: {info.language} (確信度: {info.language_probability:.2f})")

    dictionary = load_dictionary_map()
    if dictionary:
        print(f"辞書で英単語をカタカナに置換（{len(dictionary)}語）")

    results = []
    for seg in segments:
        text = apply_dictionary(seg.text.strip(), dictionary)
        results.append({
            "start": seg.start,
            "end": seg.end,
            "text": text,
        })
    return results


def _free_vram(*objs):
    """渡されたオブジェクトを削除し、CUDA/Python両方のメモリを可能な限り解放"""
    import gc
    import torch
    for o in objs:
        try:
            del o
        except Exception:
            pass
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        try:
            torch.cuda.ipc_collect()
        except Exception:
            pass


def _print_vram_usage(prefix=""):
    """現在のVRAM使用量を表示（デバッグ用）"""
    try:
        import torch
        if torch.cuda.is_available():
            allocated = torch.cuda.memory_allocated() / 1024**3
            reserved = torch.cuda.memory_reserved() / 1024**3
            total = torch.cuda.get_device_properties(0).total_memory / 1024**3
            print(f"  {prefix}VRAM: {allocated:.2f}GB allocated / {reserved:.2f}GB reserved / {total:.1f}GB total")
    except Exception:
        pass


def transcribe_with_diarization(audio_path, model_size, language, hf_token,
                                 batch_size=8, device="cuda", diarize_device=None):
    """faster-whisper + pyannote.audioで文字起こし＋話者分離（VAD+バッチ推論で高速化）

    - device: Whisper用のデバイス ("cuda" or "cpu")
    - diarize_device: pyannote用のデバイス。Noneなら device に合わせる。
                      "auto" だと CUDA OOM 時に自動で CPU フォールバック
    """
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    from pyannote.audio import Pipeline

    import torch

    if diarize_device is None:
        diarize_device = device

    compute_type = "float16" if device == "cuda" else "int8"

    # 1. faster-whisperで文字起こし
    print(f"モデル読み込み中 ({model_size}, device={device})...")
    _print_vram_usage("起動時 ")
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    batched_model = BatchedInferencePipeline(model=model)

    segments_iter, info, _ = _run_batched_transcribe(
        batched_model, audio_path, language, batch_size, None,
    )
    print(f"言語: {info.language} (確信度: {info.language_probability:.2f})")

    dictionary = load_dictionary_map()
    if dictionary:
        print(f"辞書で英単語をカタカナに置換（{len(dictionary)}語）")

    segments = []
    for seg in segments_iter:
        text = apply_dictionary(seg.text.strip(), dictionary)
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": text,
        })
    print(f"  セグメント数: {len(segments)}")

    # Whisperモデルを解放（話者分離用にVRAMを空ける）
    _print_vram_usage("Whisper完了時 ")
    batched_model = None
    model = None
    segments_iter = None
    info = None
    _free_vram()
    _print_vram_usage("Whisper解放後 ")

    # 2. pyannote.audioで話者分離
    def _load_and_run_pyannote(dev):
        print(f"話者分離中 (device={dev})...")
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=hf_token,
        )
        pipeline.to(torch.device(dev))
        return pipeline(str(audio_path))

    try:
        diarize_target = "cuda" if diarize_device == "auto" else diarize_device
        diarization = _load_and_run_pyannote(diarize_target)
    except (torch.cuda.OutOfMemoryError, RuntimeError) as e:
        if "out of memory" not in str(e).lower() and not isinstance(e, torch.cuda.OutOfMemoryError):
            raise
        if diarize_device == "auto" and diarize_target == "cuda":
            print("  ⚠ VRAM不足、話者分離をCPUにフォールバック")
            _free_vram()
            diarization = _load_and_run_pyannote("cpu")
        else:
            raise

    # 3. 各セグメントに話者を割り当て（セグメント中央の時刻で判定）
    def get_speaker(start, end):
        mid = (start + end) / 2
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            if turn.start <= mid <= turn.end:
                return speaker
        return "UNKNOWN"

    results = []
    for seg in segments:
        seg["speaker"] = get_speaker(seg["start"], seg["end"])
        results.append(seg)
    return results


def format_time(seconds):
    td = timedelta(seconds=seconds)
    total_sec = int(td.total_seconds())
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def write_output(results, output_path, diarize):
    """結果をファイルに書き出す"""
    with open(output_path, "w", encoding="utf-8") as f:
        current_speaker = None
        for seg in results:
            speaker = seg.get("speaker")
            text = seg["text"]
            if not text:
                continue

            if diarize and speaker != current_speaker:
                if current_speaker is not None:
                    f.write("\n")
                f.write(f"[{speaker}]\n")
                current_speaker = speaker

            timestamp = f"{format_time(seg['start'])} → {format_time(seg['end'])}"
            f.write(f"  {timestamp}  {text}\n")


def summarize_transcript(transcript_text, output_path):
    """文字起こしを LLM で要約する (頼み先は local_llm.py。既定はローカル)"""
    sys.path.insert(0, str(Path(__file__).parent))
    import local_llm

    summary_path = output_path.with_suffix(".summary.txt")
    try:
        summary = local_llm.summarize(transcript_text)
    except local_llm.LocalLLMError as e:
        print(f"要約に失敗しました: {e}", file=sys.stderr)
        return None

    if not summary.strip():
        print("要約が空でした。ファイルは書き込みません。", file=sys.stderr)
        return None

    summary_path.write_text(summary, encoding="utf-8")
    print(f"要約: {summary_path}")
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="動画/音声ファイルの文字起こし（話者分離対応）",
    )
    parser.add_argument("input", help="動画/音声ファイルパス")
    parser.add_argument("--output", "-o", default=None,
                        help="出力テキストファイル（デフォルト: 入力ファイル名.txt）")
    parser.add_argument("--model", "-m", default="large-v2",
                        help="Whisperモデル (default: large-v2)")
    parser.add_argument("--language", "-l", default=None,
                        help="言語コード (例: ja, en)。省略時は自動検出")
    parser.add_argument("--no-diarize", action="store_true",
                        help="話者分離を無効化（faster-whisperのみ使用）")
    parser.add_argument("--no-timestamp", action="store_true",
                        help="タイムスタンプを出力しない")
    parser.add_argument("--summarize", "-s", action="store_true",
                        help="文字起こし後に LLM で要約を生成 (頼み先は --llm)")
    parser.add_argument("--llm", choices=["local", "claude", "codex"], default=None,
                        help="要約の頼み先 (default: 環境変数 LLM_BACKEND、無ければ local)。"
                             "claude / codex は文字起こしをクラウドへ送る")
    parser.add_argument("--batch-size", type=int, default=8,
                        help="バッチサイズ (default: 8、OOM時は自動で半減)")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"],
                        help="Whisperの実行デバイス (default: cuda)")
    parser.add_argument("--diarize-device", default="auto",
                        choices=["auto", "cuda", "cpu"],
                        help="話者分離のデバイス。autoでCUDA OOM時にCPUへ自動フォールバック (default: auto)")
    parser.add_argument("--force", action="store_true",
                        help="出力ファイルが既存でも上書きして再実行する")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ファイルが見つかりません: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else input_path.with_suffix(".txt")
    diarize = not args.no_diarize

    # 要約の頼み先。local_llm は要約の直前に import されるので、環境変数で渡す
    if args.llm:
        os.environ["LLM_BACKEND"] = args.llm

    # 既存ファイルチェック: 文字起こし済ならスキップ（要約だけ追加生成は許可）
    transcribed = output_path.exists() and output_path.stat().st_size > 0
    if transcribed and not args.force:
        print(f"既に文字起こし済: {output_path} (--force で再実行)")
        if args.summarize:
            summary_path = output_path.with_suffix(".summary.txt")
            if summary_path.exists() and summary_path.stat().st_size > 0:
                print(f"既に要約済: {summary_path}")
                return
            print("要約のみ生成します...")
            transcript_text = output_path.read_text(encoding="utf-8")
            summarize_transcript(transcript_text, output_path)
        return

    # HuggingFace トークン確認
    import os
    hf_token = os.environ.get("HF_TOKEN", "")
    if diarize and not hf_token:
        print("警告: HF_TOKEN 環境変数が未設定です。話者分離を無効化します。")
        print("  話者分離を使うには: export HF_TOKEN=hf_xxxxxxx")
        diarize = False

    suffix = input_path.suffix.lower()
    if suffix not in VIDEO_EXTS and suffix not in AUDIO_EXTS:
        print(f"未対応の形式です: {suffix}", file=sys.stderr)
        sys.exit(1)

    # 動画も音声も 16kHz mono WAV に変換してから使う。
    # 話者分離 (pyannote) は soundfile(libsndfile) で読むため、m4a/aac/wma を
    # そのまま渡すと "Format not recognised" で落ちる。Whisper は PyAV で
    # 読めてしまうので、文字起こしが終わった後の話者分離で初めて失敗する
    ffmpeg_bin = find_ffmpeg()
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    audio_path = Path(tmp_wav.name)

    try:
        extract_audio(input_path, audio_path, ffmpeg_bin)

        # 文字起こし
        if diarize:
            results = transcribe_with_diarization(
                audio_path, args.model, args.language, hf_token,
                batch_size=args.batch_size,
                device=args.device,
                diarize_device=args.diarize_device,
            )
        else:
            results = transcribe_simple(
                audio_path, args.model, args.language,
                batch_size=args.batch_size,
                device=args.device,
            )

        # タイムスタンプ除去オプション
        if args.no_timestamp:
            with open(output_path, "w", encoding="utf-8") as f:
                current_speaker = None
                for seg in results:
                    text = seg["text"]
                    if not text:
                        continue
                    speaker = seg.get("speaker")
                    if diarize and speaker != current_speaker:
                        if current_speaker is not None:
                            f.write("\n")
                        f.write(f"[{speaker}]\n")
                        current_speaker = speaker
                    f.write(f"{text}\n")
        else:
            write_output(results, output_path, diarize)

        print(f"\n文字起こし完了！ {output_path}")
        print(f"  セグメント数: {len(results)}")
        if diarize:
            speakers = set(s.get("speaker", "?") for s in results)
            print(f"  話者数: {len(speakers)} ({', '.join(sorted(speakers))})")

        # 要約（ローカルLLMがVRAMを使うため、念のためGC/CUDAキャッシュを解放しておく。
        # results はセグメント辞書のリストでVRAMを持たないため対象には含めない）
        if args.summarize:
            _free_vram()
            transcript_text = output_path.read_text(encoding="utf-8")
            summarize_transcript(transcript_text, output_path)

    finally:
        if tmp_wav:
            Path(tmp_wav.name).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
