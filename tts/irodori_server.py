#!/usr/bin/env python
"""Irodori-TTS クローン常駐サーバ (:5001)。

voices/ に置いた声を **参照音声としてクローン**し、毎回まったく同じ声で読み上げる。
seed 固定で完全再現。500M-v3 (speaker-conditioned) を使うため VRAM も軽い (~2GB)。

起動:
  cd ~/ai-tools/Irodori-TTS          # IRODORI_DIR で上書き可
  env -u LD_LIBRARY_PATH HF_HUB_DISABLE_XET=1 \
    .venv/bin/python irodori_server.py --port 5001

API:
  GET  /health           -> {"status":"ok"}
  GET  /styles           -> 使える声(voice)一覧
  POST /synth  {"text":"...", "style":"calm"|"bright"}  -> audio/wav

声を増やすには VOICES に {名前: 参照wav} を足すだけ (参照wav は voices/ に置く)。
参照wav の場所は IRODORI_VOICES_DIR で上書き可 (既定: このスクリプト隣の voices/)。
"""
import io
import os
import glob
import json
import wave
import argparse
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import torch
from huggingface_hub import hf_hub_download
from irodori_tts.inference_runtime import InferenceRuntime, RuntimeKey, SamplingRequest

# クローン用チェックポイント (speaker-conditioned)。VoiceDesign(600M) より軽い。
CLONE_REPO = "Aratako/Irodori-TTS-500M-v3"

# 参照音声(声の定義)の置き場所を解決する
def _voices_dir() -> Path:
    env = os.environ.get("IRODORI_VOICES_DIR")
    cands = []
    if env:
        cands.append(Path(env))
    cands.append(Path(__file__).resolve().parent / "voices")            # サーバ隣 (デプロイ先)
    cands.append(Path(__file__).resolve().parent.parent / "voices")     # リポジトリ直下
    for d in cands:
        if d.is_dir() and glob.glob(str(d / "*.wav")):
            return d
    # 見つからなくても隣を返す (後で分かりやすいエラーにする)
    return Path(__file__).resolve().parent / "voices"

VOICES_DIR = _voices_dir()

# 声のプリセット: voice名 -> 参照wav。voices/ に <名前>.wav を置けば自動で増える。
VOICES = {
    Path(p).stem: p
    for p in sorted(glob.glob(str(VOICES_DIR / "*.wav")))
}
DEFAULT_STYLE = "calm" if "calm" in VOICES else (next(iter(VOICES), None))

# 声ごとの固定 seed (完全再現のため)。名前の文字コード和から安定生成。
def _seed_for(name: str) -> int:
    return sum(ord(c) for c in name) * 100003 + 7

print(f"[irodori] voices dir: {VOICES_DIR}", flush=True)
print(f"[irodori] voices: {list(VOICES)} (default={DEFAULT_STYLE})", flush=True)
if not VOICES:
    print(f"[irodori] 警告: 参照wavが無い。{VOICES_DIR} に <名前>.wav を置いてください", flush=True)

RT = None  # main() でポート確保後にロードする
# GPU 推論は一度に1つだけ。サーバをマルチスレッド化しても VRAM 二重使用や
# モデルの非スレッドセーフ動作を避けるため synth() 本体をこのロックで直列化する。
_infer_lock = threading.Lock()


def _load_model():
    global RT
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[irodori] loading clone model: {CLONE_REPO} (device={device}) ...", flush=True)
    if device == "cpu":
        print("[irodori] 警告: CUDA が使えないため CPU で動作します (低速)。", flush=True)
    ckpt = hf_hub_download(repo_id=CLONE_REPO, filename="model.safetensors")
    RT = InferenceRuntime.from_key(
        RuntimeKey(checkpoint=ckpt, model_device=device, codec_device=device)
    )
    print("[irodori] model ready", flush=True)


def _to_wav_bytes(audio: torch.Tensor, sr: int) -> bytes:
    # torchaudio.save は torchcodec (FFmpeg DLL) 依存で Windows だとロードに失敗する
    # ことがあるため、標準ライブラリ wave で s16 mono WAV を直接書く。
    a = audio.detach().to(device="cpu", dtype=torch.float32)
    if a.dim() > 1:
        a = a[0]  # (channels, samples) → 先頭チャンネル (出力はモノラル)
    pcm = (a.clamp(-1.0, 1.0) * 32767.0).to(torch.int16).numpy().tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def synth(text: str, style: str) -> bytes:
    ref = VOICES.get(style) or VOICES.get(DEFAULT_STYLE)
    if not ref:
        raise RuntimeError(f"参照音声が無い (voice={style!r}, 利用可: {list(VOICES)})")
    req = SamplingRequest(
        text=text,
        ref_wav=ref,             # 参照音声をクローン (声を固定)
        no_ref=False,
        cfg_scale_speaker=5.0,   # infer.py の既定 (話者再現の強さ)
        # 拡散のステップ数。40→24 で 1.5〜2倍速化（音質はわずかに低下）。
        # 環境変数 IRODORI_NUM_STEPS で調整可（例: 品質重視 32 / 速度重視 20）。
        num_steps=int(os.environ.get("IRODORI_NUM_STEPS", "24")),
        seed=_seed_for(style),   # 声ごとに固定 → 完全再現
    )
    with _infer_lock:      # 推論は1本ずつ (health/styles は並行で即応する)
        res = RT.synthesize(req, log_fn=None)
    return _to_wav_bytes(res.audio, res.sample_rate)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/synth", "/"):
            self.send_error(404); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            text = (data.get("text") or "").strip()
            style = data.get("style", DEFAULT_STYLE)
            if not text:
                self.send_error(400, "text required"); return
            wav = synth(text, style)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
        except Exception as e:
            import traceback
            traceback.print_exc()
            # メッセージは HTTP ステータス行に載るため latin-1 のみ・1行・短く。
            # 日本語や複数行をそのまま渡すと send_error 自体が例外で死に、
            # クライアントには "Remote end closed connection" だけが見える。
            msg = (str(e).splitlines() or ["synth error"])[0][:120]
            msg = msg.encode("ascii", "replace").decode("ascii")
            self.send_error(500, msg)

    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
        elif self.path == "/styles":
            body = json.dumps(
                {"styles": {k: os.path.basename(v) for k, v in VOICES.items()},
                 "default": DEFAULT_STYLE, "mode": "clone"},
                ensure_ascii=False).encode("utf-8")
        else:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _Server(ThreadingHTTPServer):
    # Windows は SO_REUSEADDR だと同一ポートへの二重 bind が通ってしまい、
    # サーバ二重起動 → VRAM 二重消費 の原因になるため無効化
    allow_reuse_address = os.name != "nt"
    daemon_threads = True   # 終了時にワーカースレッドを待たない
    # マルチスレッド: 長い /synth 処理中でも /health・/styles が即応する。
    # (単一スレッドだと合成中は死活確認が返せず「サーバが落ちた」と誤判定される)

    def handle_error(self, request, client_address):
        # クライアント(clip_tts)が停止/切替で接続を切るのは正常。
        # WinError 10053/10054 等のトレースバックをログに吐かない。
        import sys as _sys
        e = _sys.exc_info()[1]
        if isinstance(e, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5001)
    args = p.parse_args()
    # モデルロードより先にポートを掴む。二重起動は GPU にモデルを載せる前に弾く
    try:
        srv = _Server(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"[irodori] port {args.port} は使用中 (既にサーバが動いている?): {e}", flush=True)
        raise SystemExit(1)
    _load_model()
    print(f"[irodori] serving http://127.0.0.1:{args.port}  voices={list(VOICES)} (clone)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
