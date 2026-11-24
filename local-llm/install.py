#!/usr/bin/env python3
"""要約用のローカル LLM を、この機械に一式そろえる。Linux / Windows 用。

やること:
  1. GPU を調べる (nvidia-smi)。VRAM とドライバの版から、使う llama.cpp とモデルを決める
  2. llama.cpp (llama-server) と llama-swap を公式の配布元から取得する
  3. VRAM に合う大きさのモデル (Gemma 4) を取得する
  4. 設定を書き出す。Linux は systemd --user のユニット、Windows は起動スクリプト

安全のための決まり:
  - 取得元は GitHub の公式リリースと Hugging Face だけ。版は下の表で固定してある
  - 取得したファイルは SHA-256 を照合し、合わなければ使わずに止める
  - 既にある設定ファイルは上書きしない
  - LLM は 127.0.0.1 でしか待ち受けない

使い方:
  python3 local-llm/install.py              # 調べた内容と取得量を表示し、確認してから進む
  python3 local-llm/install.py --dry-run    # 何をするか表示するだけ
主なオプション:
  --model auto|26b|12b|e4b|e2b   モデルの大きさ (既定 auto = 要約用の軽いもの。VRAM 7GB 以上で
                                 E4B、それ未満で E2B)
  --model-path FILE              手持ちの GGUF を使う (モデルは取得しない)
  --llama-server FILE            手持ちの llama-server を使う (llama.cpp は取得しない)
  --backend auto|cuda13|cuda12|vulkan|cpu
  --dir DIR                      置き場所
  --port N                       待ち受けポート (既定 8081)
  --no-service                   Linux でも systemd を使わず、起動スクリプト方式にする
  --yes                          確認を省く

標準ライブラリだけで動く。
"""

import argparse
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TTS_DIR = HERE.parent / "tts"
IS_WINDOWS = os.name == "nt"
OS_KEY = "windows" if IS_WINDOWS else "linux"

# ---------------------------------------------------------------------------
# 固定している版。更新するときは、名前・大きさ・SHA-256 を配布元の表示と照らして書き換える
# ---------------------------------------------------------------------------
LLAMA_TAG = "b12110"   # llama.cpp v0.9.0 に対応するビルド
LLAMA_URL = "https://github.com/ggml-org/llama.cpp/releases/download/" + LLAMA_TAG + "/"
SWAP_URL = "https://github.com/mostlygeek/llama-swap/releases/download/v272/"

# (ファイル名, バイト数, SHA-256)。cuda は本体と CUDA ランタイム (cudart) の 2 つが要る
LLAMA_ASSETS = {
    ("linux", "cuda13"): [
        ("llama-b12110-bin-ubuntu-cuda-13.4-x64.tar.gz", 168275993,
         "2afd2ca540da119570b1dac36b5a2c862bb03a3b789c46661dfa55e600f5b19b"),
        ("cudart-llama-b12110-bin-ubuntu-cuda-13.4-x64.tar.gz", 440232657,
         "9b1110161943882ec85c8e9ae641075ee8c962a50f8f1e36534ee65ea1e56ed9"),
    ],
    ("linux", "cuda12"): [
        ("llama-b12110-bin-ubuntu-cuda-12.8-x64.tar.gz", 187314621,
         "4af1e1da23661db4a5b84b29aae16ebc053df689c5b8b1427c4670f2c6406d8c"),
        ("cudart-llama-b12110-bin-ubuntu-cuda-12.8-x64.tar.gz", 594371477,
         "6f1d562086d84ae85df880d3711079ec225602b51d530608e5001600625067dd"),
    ],
    ("linux", "vulkan"): [
        ("llama-b12110-bin-ubuntu-vulkan-x64.tar.gz", 35858360,
         "a5d741940956ec62e8783aee7e5a4c7c761af867d6f027c3277a51e1a3ac59b0"),
    ],
    ("linux", "cpu"): [
        ("llama-b12110-bin-ubuntu-x64.tar.gz", 23051958,
         "368227edcc229be5f9bed16a2223d79b5e41d0ac2e19bcf5825e82291c060e55"),
    ],
    ("windows", "cuda13"): [
        ("llama-b12110-bin-win-cuda-13.4-x64.zip", 169175655,
         "dc910a969190b5d42e189afd73117f6bbdd8dd8ed6586342c5e0b82cab62c673"),
        ("cudart-llama-bin-win-cuda-13.4-x64.zip", 423535356,
         "738f8c251ac22b70c3ae6f83a10cf222725df0395246a2cf58f32bdb85fbe668"),
    ],
    ("windows", "cuda12"): [
        ("llama-b12110-bin-win-cuda-12.4-x64.zip", 287014657,
         "1ce755bc3a061889234048f88e33328e17971eaad73c4666cfeb26026b317fee"),
        ("cudart-llama-bin-win-cuda-12.4-x64.zip", 391443627,
         "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6"),
    ],
    ("windows", "vulkan"): [
        ("llama-b12110-bin-win-vulkan-x64.zip", 37661146,
         "9732a5e48887ca54d0bfe4af4324dc85fd56cd8cda19b52df3542fa6ebe6874f"),
    ],
    ("windows", "cpu"): [
        ("llama-b12110-bin-win-cpu-x64.zip", 24708445,
         "98901e327c252875accaf2d1bf364e10e868843b0b63468baad1aa945cdc5da2"),
    ],
}
SWAP_ASSETS = {
    "linux": ("llama-swap_272_linux_amd64.tar.gz", 24279896,
              "3f21b4c13f55ea5576badcd6cc1d114ccdcf74a5d56346d3ac1e777d28e0fdf1"),
    "windows": ("llama-swap_272_windows_amd64.zip", 24174334,
                "a1784767d662503bdc85649f939747109229306bd818e77a417c52da28c0084c"),
}

# モデル (Gemma 4、unsloth 配布、Apache-2.0)。commit を固定して取得する。
# min_vram は「この VRAM (MiB) 以上の GPU なら選ぶ」目安。大きい順に並べる
MODELS = {
    "26b": dict(repo="unsloth/gemma-4-26B-A4B-it-qat-GGUF",
                commit="7b92b5b28818151e8669af2e45e88d6086f490dd",
                file="gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf", gb=14.2, min_vram=20000,
                sha="a7c5bc715f5ff8e99a3e8901ce7d2b42b402c669bf24f7c5250747633d0f5891"),
    "12b": dict(repo="unsloth/gemma-4-12B-it-qat-GGUF",
                commit="980b060c40a8539ac159e0501a3e0f66a6365af3",
                file="gemma-4-12B-it-qat-UD-Q4_K_XL.gguf", gb=6.7, min_vram=11000,
                sha="90fd44e29e0d7cffeb0fd00dc73cfdab9ed0b0e95306ecf7821ea634c940c370"),
    "e4b": dict(repo="unsloth/gemma-4-E4B-it-GGUF",
                commit="bfc15c382204943c3a8fff0c750b94ae2364d7a3",
                file="gemma-4-E4B-it-UD-Q4_K_XL.gguf", gb=5.1, min_vram=7000,
                sha="3cf61de12daa015ee0f7b68e7b7c541405bf220e1e942bad8b47cab827d7df80"),
    "e2b": dict(repo="unsloth/gemma-4-E2B-it-qat-GGUF",
                commit="66a399f68ddd113b06dff02fca9523e55465d11d",
                file="gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf", gb=2.6, min_vram=0,
                sha="e531007218dfab990486a5de7676a6932d6ea8dea233d1f698d7c21cf8a16889"),
}

# 公式の Linux 用の完成品が要求する glibc の版 (Ubuntu 24.04 以降に相当)
LINUX_MIN_GLIBC = (2, 38)

MODEL_NAME = "gemma4-chat"   # tts/local_llm.py の既定 (LLM_MODEL) と同じ呼び名にしておく
CTX = 24576                  # tts/local_llm.py の既定の入力上限 (16000 文字) が収まる長さ
TTL = 300                    # 使われないとき、この秒数でモデルを降ろす


def die(msg: str) -> None:
    print(f"\nエラー: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# GPU を調べる
# ---------------------------------------------------------------------------
def detect_gpu():
    """(一番大きい GPU の VRAM MiB, ドライバの主版数, GPU 名の一覧)。NVIDIA が無ければ None"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--query-gpu=memory.total,driver_version,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    gpus = []
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            gpus.append((int(float(parts[0])), int(parts[1].split(".")[0]), parts[2]))
        except ValueError:
            continue
    if r.returncode != 0 or not gpus:
        return None
    return max(g[0] for g in gpus), gpus[0][1], [f"{g[2]} ({g[0]} MiB)" for g in gpus]


def pick_backend(gpu) -> tuple[str, str]:
    """(backend, 理由)。ドライバの版の境目は NVIDIA の公表値に基づく目安"""
    if gpu is None:
        return "cpu", "NVIDIA の GPU が見つからない (nvidia-smi が無いか失敗)。CPU で動かす"
    driver = gpu[1]
    cuda13_min = 581 if IS_WINDOWS else 580
    cuda12_min = 551 if IS_WINDOWS else 570
    if driver >= cuda13_min:
        return "cuda13", f"ドライバ {driver} は CUDA 13 に対応"
    if driver >= cuda12_min:
        return "cuda12", f"ドライバ {driver} は CUDA 12 に対応 (13 には古い)"
    return "vulkan", f"ドライバ {driver} は CUDA 版には古いので Vulkan 版を使う"


# 既定で選ぶのは要約用の軽いモデルだけ (E4B / E2B)。要約とタイトル付けならこれで足り、
# 文字起こし (Whisper) と GPU のメモリも取り合わない。大きいのは --model 12b / 26b で選ぶ
AUTO_CHOICES = ("e4b", "e2b")


def pick_model(gpu, backend: str) -> tuple[str, str]:
    if backend == "cpu" or gpu is None:
        return "e2b", "GPU を使えないので一番小さいモデル"
    vram = gpu[0]
    for key in AUTO_CHOICES:
        if vram >= MODELS[key]["min_vram"]:
            return key, f"要約用の軽いモデル (VRAM {vram} MiB。大きいのは --model 12b / 26b)"
    return "e2b", "一番小さいモデル"


# ---------------------------------------------------------------------------
# 取得と照合
# ---------------------------------------------------------------------------
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, dest: Path, size: int | None, sha: str) -> None:
    """url を dest に取得し、SHA-256 を照合する。合わなければ消して止める"""
    if dest.exists():
        if sha256_of(dest) == sha:
            print(f"  取得済み (照合 OK): {dest.name}")
            return
        print(f"  既にあるが中身が違うので取り直す: {dest.name}")
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    print(f"  取得中: {dest.name}" + (f" ({size / 1e6:.0f} MB)" if size else ""))
    req = urllib.request.Request(url, headers={"User-Agent": "local-llm-install"})
    h = hashlib.sha256()
    done = 0
    shown = -1
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0) or size or 0
            while True:
                block = r.read(1 << 20)
                if not block:
                    break
                f.write(block)
                h.update(block)
                done += len(block)
                if total:
                    pct = done * 100 // total // 10 * 10
                    if pct != shown:
                        shown = pct
                        print(f"    {pct:3d}%  {done / 1e6:.0f} / {total / 1e6:.0f} MB", flush=True)
    except (urllib.error.URLError, OSError) as e:
        part.unlink(missing_ok=True)
        die(f"取得に失敗しました: {url}\n  {e}")

    got = h.hexdigest()
    if got != sha:
        part.unlink(missing_ok=True)
        die(f"SHA-256 が合いません。壊れているか、配布物が差し替えられています。\n"
            f"  ファイル: {dest.name}\n  期待: {sha}\n  実際: {got}\n  取得したものは消しました。")
    part.replace(dest)
    print("    照合 OK")


def _safe_target(base: Path, name: str) -> Path:
    """書庫の中の名前が base の外を指していないか確かめる"""
    target = (base / name).resolve()
    if base.resolve() != target and base.resolve() not in target.parents:
        die(f"書庫の中に、展開先の外を指すパスがあります: {name}")
    return target


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                _safe_target(dest, name)
            z.extractall(dest)
        return
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            _safe_target(dest, m.name)
            if m.issym():
                if os.path.isabs(m.linkname):
                    die(f"書庫の中に絶対パスへのリンクがあります: {m.name}")
                _safe_target(dest, str(Path(m.name).parent / m.linkname))
            elif m.islnk():
                _safe_target(dest, m.linkname)
            elif not (m.isfile() or m.isdir()):
                die(f"書庫の中に想定外の種類のものがあります: {m.name}")
        try:
            t.extractall(dest, filter="tar")    # Python 3.12 以降
        except TypeError:
            t.extractall(dest)


def find_one(root: Path, name: str) -> Path:
    hits = sorted(p for p in root.rglob(name) if p.is_file())
    if not hits:
        die(f"{root} の中に {name} が見つかりません。配布物の中身が想定と違います。")
    return hits[0]


def glibc_version():
    """(主, 副)。分からなければ None"""
    try:
        name, ver = platform.libc_ver()
        if name != "glibc":
            return None
        a, b = ver.split(".")[:2]
        return int(a), int(b)
    except (ValueError, OSError):
        return None


def self_test(server: Path) -> None:
    """llama-server が起動できるか確かめる (モデルは読み込まない)"""
    env = dict(os.environ)
    if not IS_WINDOWS:
        env["LD_LIBRARY_PATH"] = str(server.parent) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    try:
        r = subprocess.run([str(server), "--version"], capture_output=True, text=True,
                           timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        die(f"llama-server を起動できません: {e}")
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        die("llama-server が起動しません。この機械の OS やドライバと合っていません。\n  "
            + "\n  ".join(out.splitlines()[:4])
            + "\n  自分でビルドした llama-server があれば --llama-server で指定できます。")
    print("  起動確認 OK: " + (out.splitlines()[0] if out else ""))


def free_gb(path: Path) -> float:
    p = path
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


# ---------------------------------------------------------------------------
# 設定の書き出し
# ---------------------------------------------------------------------------
def write_new(path: Path, text: str, executable: bool = False) -> None:
    if path.exists():
        die(f"既にあるので上書きしません: {path}\n"
            "  作り直すなら、そのファイルを別名に退避してからやり直してください。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\r\n" if path.suffix == ".cmd" else "\n")
    if executable and not IS_WINDOWS:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  書き出し: {path}")


def yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def make_config(server: Path, model: Path, backend: str) -> str:
    ngl = "0" if backend == "cpu" else "99"
    lines = [
        "# local-llm/install.py が書き出した llama-swap の設定。",
        "# llama-swap は、頼まれたときだけモデルを読み込み、使われなければ降ろす。",
        "healthCheckTimeout: 300",
        "logLevel: info",
        "startPort: 10001",
        "",
        "models:",
        f"  {yaml_quote(MODEL_NAME)}:",
        f"    ttl: {TTL}",
    ]
    if not IS_WINDOWS:
        # 配布物に入っている共有ライブラリを、機械に元からあるものより先に探させる
        lines += ["    env:", f"      - {yaml_quote('LD_LIBRARY_PATH=' + str(server.parent))}"]
    lines += [
        "    cmd: |",
        f'      "{server.as_posix()}"',
        "      --port ${PORT} --host 127.0.0.1",
        f"      --jinja -ngl {ngl}",
        f'      -m "{model.as_posix()}"',
        f"      -c {CTX}",
        "",
    ]
    return "\n".join(lines)


def make_unit(swap: Path, config: Path, port: int) -> str:
    return (
        "# local-llm/install.py が書き出したユニット\n"
        "[Unit]\n"
        f"Description=llama-swap (:{port}) - local LLM for summaries\n\n"
        "[Service]\n"
        "Type=simple\n"
        f'ExecStart="{swap}" --config "{config}" --listen 127.0.0.1:{port}\n'
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def make_start_script(swap: Path, config: Path, port: int) -> str:
    if IS_WINDOWS:
        return ("@echo off\n"
                "rem local-llm/install.py が書き出した起動スクリプト\n"
                f'"{swap}" --config "{config}" --listen 127.0.0.1:{port}\n')
    return ("#!/bin/sh\n"
            "# local-llm/install.py が書き出した起動スクリプト\n"
            f'exec "{swap}" --config "{config}" --listen 127.0.0.1:{port}\n')


def update_env(pairs: dict) -> None:
    """tts/.env に設定を足す。既に書いてある項目は変えない"""
    env_path = TTS_DIR / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    have = {line.split("=", 1)[0].strip() for line in existing.splitlines()
            if "=" in line and not line.lstrip().startswith("#")}
    add = [(k, v) for k, v in pairs.items() if k not in have]
    skipped = [k for k in pairs if k in have]
    if add:
        text = existing
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n# local-llm/install.py が追加\n" + "".join(f"{k}={v}\n" for k, v in add)
        env_path.write_text(text, encoding="utf-8")
        print(f"  {env_path} に追加: " + ", ".join(k for k, _ in add))
    for k in skipped:
        print(f"  注意: {env_path} に {k} が既にあるので変えていません。"
              f"必要なら {k}={pairs[k]} に直してください。")


# ---------------------------------------------------------------------------
def main() -> None:
    default_dir = (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "snippets-local-llm"
                   if IS_WINDOWS else Path.home() / ".local" / "share" / "snippets-local-llm")
    ap = argparse.ArgumentParser(description="要約用のローカル LLM を一式そろえる")
    ap.add_argument("--model", default="auto", choices=["auto", *MODELS])
    ap.add_argument("--model-path", help="手持ちの GGUF を使う (モデルは取得しない)")
    ap.add_argument("--llama-server", help="手持ちの llama-server を使う (llama.cpp は取得しない)")
    ap.add_argument("--backend", default="auto", choices=["auto", "cuda13", "cuda12", "vulkan", "cpu"])
    ap.add_argument("--dir", default=str(default_dir), help=f"置き場所 (既定 {default_dir})")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--no-service", action="store_true",
                    help="Linux でも systemd を使わず、起動スクリプト方式にする")
    ap.add_argument("--dry-run", action="store_true", help="何をするか表示するだけ")
    ap.add_argument("--yes", action="store_true", help="確認を省く")
    args = ap.parse_args()

    if sys.maxsize < 2**32 or (os.uname().machine if hasattr(os, "uname") else
                               os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64")).lower() not in (
                                   "x86_64", "amd64"):
        die("対応しているのは x86_64 (64bit) の Linux / Windows だけです。")

    root = Path(args.dir).expanduser().resolve()
    if any(c in str(root) for c in '"\n'):
        die(f"置き場所に使えない文字が入っています: {root}")

    # --- 1. 何を入れるか決める ---
    gpu = detect_gpu()
    backend, why_b = (args.backend, "指定どおり") if args.backend != "auto" else pick_backend(gpu)
    if args.model_path:
        model_file = Path(args.model_path).expanduser().resolve()
        if not model_file.is_file():
            die(f"--model-path のファイルが見つかりません: {model_file}")
        model_key, why_m, model = None, "指定のファイルを使う", None
    else:
        model_key, why_m = (args.model, "指定どおり") if args.model != "auto" else pick_model(gpu, backend)
        model = MODELS[model_key]
        model_file = root / "models" / model["file"]

    own_server = None
    if args.llama_server:
        own_server = Path(args.llama_server).expanduser().resolve()
        if not own_server.is_file():
            die(f"--llama-server のファイルが見つかりません: {own_server}")
    elif not IS_WINDOWS:
        glibc = glibc_version()
        if glibc is not None and glibc < LINUX_MIN_GLIBC:
            die(f"この機械の glibc は {glibc[0]}.{glibc[1]} で、公式の llama.cpp の完成品が動きません "
                f"({LINUX_MIN_GLIBC[0]}.{LINUX_MIN_GLIBC[1]} 以上、Ubuntu 24.04 以降が必要)。\n"
                "  llama.cpp を自分でビルドして、--llama-server /path/to/llama-server で指定してください。\n"
                "  ビルド手順: https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md")
    assets = [] if own_server else LLAMA_ASSETS[(OS_KEY, backend)]
    swap_asset = SWAP_ASSETS[OS_KEY]
    use_systemd = (not IS_WINDOWS) and (not args.no_service) and bool(shutil.which("systemctl"))

    dl_dir = root / "downloads"
    pending = [a for a in (*assets, swap_asset) if not (dl_dir / a[0]).exists()]
    need_gb = sum(a[1] for a in pending) / 1e9 * 2.2   # 書庫 + 展開した中身
    if model and not model_file.exists():
        need_gb += model["gb"]

    print("=== 調べた結果 ===")
    print("GPU      : " + (" / ".join(gpu[2]) + f"  (ドライバ {gpu[1]})" if gpu else "NVIDIA の GPU は見つからない"))
    print(f"llama.cpp: 手持ちのものを使う ({own_server})" if own_server
          else f"llama.cpp: {LLAMA_TAG} の {backend} 版  … {why_b}")
    print("モデル   : " + (f"{model['file']} ({model['gb']} GB)  … {why_m}" if model
                           else f"{model_file}  … {why_m}"))
    print(f"置き場所 : {root}")
    print(f"待ち受け : 127.0.0.1:{args.port} (この機械の中からしかつながらない)")
    print("起動方法 : " + ("systemd --user のユニット llm-swap" if use_systemd else "起動スクリプト"))
    print(f"必要な空き: 約 {need_gb:.1f} GB (今の空き {free_gb(root):.0f} GB)")
    print("取得元   : " + ("" if own_server else "github.com/ggml-org/llama.cpp, ")
          + "github.com/mostlygeek/llama-swap"
          + (", huggingface.co/" + model["repo"] if model else ""))
    if backend == "cpu":
        print("注意: GPU を使わないので、要約にはかなり時間がかかります。")
    if backend == "vulkan":
        print("注意: Vulkan 版は未確認です。うまく動かなければドライバを更新して cuda 版を使ってください。")

    config_path = root / "config.yaml"
    unit_path = Path.home() / ".config" / "systemd" / "user" / "llm-swap.service"
    start_path = root / ("start-llm.cmd" if IS_WINDOWS else "start-llm.sh")
    for p in ([config_path] + ([unit_path] if use_systemd else [start_path])):
        if p.exists():
            die(f"既に設定があります: {p}\n"
                "  入れ直すなら別名に退避してください。"
                + ("\n  今ある llm-swap ユニットを残したまま試すなら --no-service と --port を使えます。"
                   if p == unit_path else ""))

    if args.dry_run:
        print("\n(--dry-run なので、ここで終わります)")
        return
    if free_gb(root) < need_gb + 1:
        die(f"空きが足りません。約 {need_gb:.1f} GB 必要です。--dir で別の場所を指定できます。")
    if not args.yes:
        try:
            if input("\nこの内容で進めますか？ [y/N] ").strip().lower() not in ("y", "yes"):
                print("中止しました。")
                return
        except EOFError:
            die("確認の入力を受け取れません。対話できない環境では --yes を付けてください。")

    # --- 2. llama.cpp と llama-swap ---
    print("\n=== llama.cpp ===")
    if own_server:
        server = own_server
    else:
        llama_dir = root / f"llama.cpp-{LLAMA_TAG}-{backend}"
        for name, size, sha in assets:
            fetch(LLAMA_URL + name, dl_dir / name, size, sha)
        if not llama_dir.exists():
            tmp = root / (llama_dir.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            for name, _, _ in assets:
                print(f"  展開中: {name}")
                extract(dl_dir / name, tmp)
            tmp.replace(llama_dir)
        server = find_one(llama_dir, "llama-server.exe" if IS_WINDOWS else "llama-server")
        # CUDA のライブラリが別のフォルダに展開された場合は、llama-server の隣に集める
        # (Windows は隣の DLL しか探さない。Linux は設定の LD_LIBRARY_PATH で隣を指している)
        for lib in llama_dir.rglob("*.dll" if IS_WINDOWS else "*.so*"):
            if lib.is_file() and lib.parent != server.parent and not (server.parent / lib.name).exists():
                shutil.copy2(lib, server.parent / lib.name, follow_symlinks=False)
        if not IS_WINDOWS:
            server.chmod(server.stat().st_mode | stat.S_IXUSR)
    print(f"  llama-server: {server}")
    self_test(server)

    print("\n=== llama-swap ===")
    swap_dir = root / "llama-swap"
    fetch(SWAP_URL + swap_asset[0], dl_dir / swap_asset[0], swap_asset[1], swap_asset[2])
    if not swap_dir.exists():
        tmp = root / "llama-swap.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        extract(dl_dir / swap_asset[0], tmp)
        tmp.replace(swap_dir)
    swap = find_one(swap_dir, "llama-swap.exe" if IS_WINDOWS else "llama-swap")
    if not IS_WINDOWS:
        swap.chmod(swap.stat().st_mode | stat.S_IXUSR)
    print(f"  llama-swap: {swap}")

    # --- 3. モデル ---
    if model:
        print("\n=== モデル ===")
        url = f"https://huggingface.co/{model['repo']}/resolve/{model['commit']}/{model['file']}"
        fetch(url, model_file, int(model["gb"] * 1e9), model["sha"])

    # --- 4. 設定 ---
    print("\n=== 設定 ===")
    write_new(config_path, make_config(server, model_file, backend))
    env = {}
    if args.port != 8081:
        env["LLM_API_BASE"] = f"http://127.0.0.1:{args.port}/v1"
    if use_systemd:
        write_new(unit_path, make_unit(swap, config_path, args.port))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    else:
        write_new(start_path, make_start_script(swap, config_path, args.port), executable=True)
        env["LLM_SERVER_UNIT"] = ""
        env["LLM_SERVER_CMD"] = str(start_path)
    if env:
        if TTS_DIR.is_dir():
            update_env(env)
        else:
            print("  tts/ が見つからないので、次の設定を自分で環境変数か tts/.env に入れてください:")
            for k, v in env.items():
                print(f"    {k}={v}")

    shutil.rmtree(dl_dir, ignore_errors=True)   # 展開が済んだ書庫は消す (約 0.2〜0.8 GB)

    print("\n=== 完了 ===")
    print("要約は、必要になったときに LLM が自動で起動します。試すには:")
    print("  " + (f"systemctl --user start llm-swap" if use_systemd else f'"{start_path}"   (別の窓で)'))
    print(f"  curl http://127.0.0.1:{args.port}/v1/models")
    print("消すには: " + str(root) + (f" と {unit_path}" if use_systemd else "") + " を削除する")


if __name__ == "__main__":
    main()
