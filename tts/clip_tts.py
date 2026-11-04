"""
クリップボード→TTS再生ツール（常駐型）

Usage:
  python clip_tts.py                    # デフォルトモデル(amitaro, SBV2 in-process)で起動
  python clip_tts.py -m 1               # SBV2 のモデル変更
  python clip_tts.py -v calm            # Irodori VoiceDesign(:5001) の落ち着いた女性声
  python clip_tts.py -v bright          # 同・明るいアニメ声
  python clip_tts.py -v calm --pitch 1.2 --speed 1.1   # 高さ/話速の加工 (0.5〜2.0)

キー操作:
  p / スペース: クリップボードの内容を読み上げ
  s          : 再生中の音声を停止
  q          : 終了

動作:
  1. 起動時にTTSモデルを一度だけロード（以後は即生成）
  2. pキーでクリップボードのテキストを合成→再生
  3. sキーで再生中の音声を停止

バックエンド:
  -v sbv2 (既定): Style-Bert-VITS2 をプロセス内ロード (Windows/venv 前提の従来動作)
  -v それ以外   : Irodori VoiceDesign サーバ (:5001) に HTTP で投げる。torch 不要・
                  システム python3 で動く。サーバ起動方法は irodori_server.py 参照。

プラットフォーム:
  Windows  : グローバルホットキー (RegisterHotKey, キー専有) + ターミナルキー
  Linux/Mac: ターミナルキー + クリップボード監視 + OS ショートカット (シグナル経由)
             OS(GNOME 等) のキー設定に pkill -USR1/-USR2 … を割り当てる。
             X11 の passive リスナー(pynput)はキーを消費できずアプリに漏れる
             (印刷ダイアログ誤爆等) ため採用しない。設定は bin/clip_tts_gnome_hotkey.sh。
"""

import os
import subprocess
import sys
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
_REEXEC_FLAG = "_CLIP_TTS_VENV_REEXEC"

def _in_venv():
    if os.environ.get("VIRTUAL_ENV"):
        return True
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)

def _wants_irodori(argv):
    """-v/--voice で sbv2 以外が指定されたか (argparse 前の事前判定)。
    Irodori モードは torch 不要なので SBV2 venv への再実行をスキップする。"""
    for i, a in enumerate(argv):
        if a in ("-v", "--voice") and i + 1 < len(argv):
            return argv[i + 1] != "sbv2"
        if a.startswith("--voice="):
            return a.split("=", 1)[1] != "sbv2"
    return False

if not os.environ.get(_REEXEC_FLAG) and not _in_venv() and not _wants_irodori(sys.argv[1:]):
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

# --- venv内処理 ---

import argparse
import atexit
import json
import re
import shutil
import struct
import tempfile
import threading
import time
import urllib.request
from html.parser import HTMLParser

IS_WINDOWS = sys.platform == "win32"


def _pid_alive(pid: int) -> bool:
    """PID が生きているか。
    Windows の os.kill はシグナル 0 でも TerminateProcess で相手を殺してしまうため、
    OpenProcess + GetExitCodeProcess で照会だけ行う。"""
    if not pid:
        return False
    if IS_WINDOWS:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 他ユーザ所有だが生きている
    except OSError:
        return False
    return True


def _single_instance_or_exit():
    """二重起動を防ぐ。既に別の clip_tts が常駐していたら終了する。
    (グローバルショートカット=シグナルが複数プロセスに届き、重複再生・停止不能になるため)"""
    lock = Path(tempfile.gettempdir()) / "clip_tts.lock"
    if lock.exists():
        try:
            other = int((lock.read_text().strip() or "0"))
        except Exception:
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            print(f"既に clip_tts が起動中です (PID={other})。二重起動を中止しました。",
                  file=sys.stderr)
            print("（停止: pkill -f 'clip_tts[.]py' / 複数使う場合: --allow-multiple）",
                  file=sys.stderr)
            sys.exit(0)
    try:
        lock.write_text(str(os.getpid()))
        atexit.register(lambda: lock.unlink() if lock.exists() else None)
    except OSError:
        pass

# --- RegisterHotKey ベースのグローバルホットキー (Windows のみ) ---

_hotkey_thread_id = None  # PostThreadMessage で WM_QUIT を送るために保持

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as _wintypes

    _WM_HOTKEY   = 0x0312
    _WM_QUIT     = 0x0012
    _MOD_ALT     = 0x0001
    _MOD_CTRL    = 0x0002
    _MOD_SHIFT   = 0x0004
    _MOD_WIN     = 0x0008
    _MOD_NOREPEAT = 0x4000

    _VK_MAP = {
        'space': 0x20, 'backspace': 0x08, 'tab': 0x09, 'enter': 0x0D,
        'esc': 0x1B, 'escape': 0x1B, 'delete': 0x2E, 'insert': 0x2D,
        'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22,
        'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28,
        **{f'f{i}': 0x6F + i for i in range(1, 13)},
    }

    class _MSG(ctypes.Structure):
        _fields_ = [
            ('hwnd',    _wintypes.HWND),
            ('message', _wintypes.UINT),
            ('wParam',  _wintypes.WPARAM),
            ('lParam',  _wintypes.LPARAM),
            ('time',    _wintypes.DWORD),
            ('ptX',     _wintypes.LONG),
            ('ptY',     _wintypes.LONG),
        ]

    def _parse_hotkey_win(hotkey_str):
        """'ctrl+shift+p' → (mods, vk)。失敗時は ValueError"""
        mods, vk = _MOD_NOREPEAT, 0
        for p in (p.strip().lower() for p in hotkey_str.split('+')):
            if   p in ('ctrl', 'control'): mods |= _MOD_CTRL
            elif p in ('alt', 'menu'):     mods |= _MOD_ALT
            elif p == 'shift':             mods |= _MOD_SHIFT
            elif p in ('win', 'windows'):  mods |= _MOD_WIN
            elif p in _VK_MAP:             vk = _VK_MAP[p]
            elif len(p) == 1:
                r = ctypes.windll.user32.VkKeyScanA(ord(p))
                if r == -1:
                    raise ValueError(f"不明なキー: {p!r}")
                vk = r & 0xFF
            else:
                raise ValueError(f"不明なトークン: {p!r}")
        if not vk:
            raise ValueError(f"キーが未指定: {hotkey_str!r}")
        return mods, vk

    def _run_hotkey_loop(hotkey_map):
        """RegisterHotKey + GetMessage ループ（専用スレッドで実行）
        hotkey_map: {id: (hotkey_str, callback)}
        """
        global _hotkey_thread_id
        _hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        registered = []
        for hid, (hk_str, _) in hotkey_map.items():
            try:
                mods, vk = _parse_hotkey_win(hk_str)
            except ValueError as e:
                print(f"ホットキー解析失敗: {e}", file=sys.stderr)
                continue
            if ctypes.windll.user32.RegisterHotKey(0, hid, mods, vk):
                registered.append(hid)
            else:
                err = ctypes.windll.kernel32.GetLastError()
                print(f"ホットキー登録失敗 ({hk_str}): error={err}", file=sys.stderr)

        msg = _MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_HOTKEY:
                entry = hotkey_map.get(msg.wParam)
                if entry:
                    hk_str, cb = entry
                    # 押下がプロセスに届いた瞬間にターミナルへ即エコー。合成/再生の前に
                    # 必ず1行出るので「押しても無反応」に見えて連打する事故を防ぐ。
                    print(f"\n⌨  {hk_str} 受信", flush=True)
                    threading.Thread(target=cb, daemon=True).start()

        for hid in registered:
            ctypes.windll.user32.UnregisterHotKey(0, hid)


# --- Linux/Mac のグローバルショートカットはシグナルで受ける ---
# X11 では passive リスナー (pynput 等) はキーを「消費」できないため、ホットキーが
# アクティブアプリにも漏れて誤爆する (例: Ctrl+Shift+P で印刷ダイアログが開く)。
# 正攻法は OS(GNOME 等) のキーボードショートカットにコマンドを割り当てる方式:
#   OS がキーを専有 → アプリに漏れない。コマンドが下のシグナルを clip_tts に送る。
#     再生       : pkill -USR1 -f 'clip_tts[.]py'
#     停止       : pkill -USR2 -f 'clip_tts[.]py'
#     一時停止/再開: pkill -RTMIN   -f 'clip_tts[.]py'
#     巻き戻し    : pkill -RTMIN+1 -f 'clip_tts[.]py'
#     早送り      : pkill -RTMIN+2 -f 'clip_tts[.]py'
# 割り当ては bin/clip_tts_gnome_hotkey.sh で自動設定できる (GNOME)。

# Style-Bert-VITS2 リポジトリの場所 (Windows: スクリプト隣接 / Linux: ~/tts)
SBV2_DIR = next(
    (d for d in (Path(__file__).parent / "Style-Bert-VITS2",
                 Path.home() / "tts" / "Style-Bert-VITS2") if d.exists()),
    Path(__file__).parent / "Style-Bert-VITS2")
sys.path.insert(0, str(SBV2_DIR))
try:
    from eng2kana import remove_unreadable, replace_english_words, replace_japanese_readings, add_pauses, PARAGRAPH_BREAK
    HAS_ENG2KANA = True
except ImportError:
    # Irodori モードは辞書なしで英単語を読めるため eng2kana 無しでも動く
    # (alkana が無い system python3 での実行を許す)
    HAS_ENG2KANA = False
    PARAGRAPH_BREAK = "\x07"

    def remove_unreadable(t):
        return t

    def replace_english_words(t, dict_only=False):
        return t

    def replace_japanese_readings(t):
        return t

    def add_pauses(t):
        return re.sub(r"\n{2,}", PARAGRAPH_BREAK, t)

try:
    import pyperclip
except ImportError:
    # pyperclip 無し環境用フォールバック (Windows: Win32 API / Linux: xclip 等)
    class pyperclip:  # noqa: N801 - 最小互換シム
        @staticmethod
        def paste():
            if IS_WINDOWS:
                import ctypes
                CF_UNICODETEXT = 13
                u32 = ctypes.windll.user32
                k32 = ctypes.windll.kernel32
                # 64bit Python では restype 既定が c_int (32bit) でポインタが壊れるため明示
                u32.GetClipboardData.restype = ctypes.c_void_p
                k32.GlobalLock.restype = ctypes.c_void_p
                k32.GlobalLock.argtypes = [ctypes.c_void_p]
                k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
                # 他アプリがクリップボードを掴んでいると OpenClipboard が失敗するため軽くリトライ
                for _ in range(3):
                    if u32.OpenClipboard(None):
                        try:
                            h = u32.GetClipboardData(CF_UNICODETEXT)
                            if not h:
                                return ""
                            p = k32.GlobalLock(h)
                            if not p:
                                return ""
                            try:
                                return ctypes.wstring_at(p)
                            finally:
                                k32.GlobalUnlock(h)
                        finally:
                            u32.CloseClipboard()
                    time.sleep(0.01)
                return ""
            for cmd in (["xclip", "-selection", "clipboard", "-o"],
                        ["wl-paste"], ["xsel", "-b"]):
                if shutil.which(cmd[0]):
                    try:
                        return subprocess.run(cmd, capture_output=True, text=True,
                                              timeout=5).stdout
                    except Exception:
                        pass
            return ""


# --- テキスト前処理（tts.pyと同じ） ---

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


def preprocess(text):
    """テキストを音声化できる形に整形"""
    # HTMLタグっぽいものがあれば除去
    if re.search(r"<[a-z][^>]*>", text, re.IGNORECASE):
        parser = HTMLTextExtractor()
        parser.feed(text)
        text = parser.get_text()
    # Markdown記法っぽいものがあれば除去
    if re.search(r"[#`*\[]", text):
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
    """文を分割する。先頭にPARAGRAPH_BREAK(\\x07)が残る文は段落区切り直後を示す"""
    raw_parts = re.split(r"(?<=[。！？])", text)
    sentences = []
    pending_break = False
    for part in raw_parts:
        part = part.strip()
        # 先頭の段落マーカーを検出して保留
        while part.startswith(PARAGRAPH_BREAK):
            pending_break = True
            part = part[1:].lstrip()
        if not part:
            continue
        if not re.search(r"[぀-ヿ一-鿿]", part):
            continue
        # 段落マーカーを文の先頭に付け直す（合成側で検出するため）
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


def make_wav_stream_header(sample_rate, channels=1, bits_per_sample=16):
    """ストリーミング用の WAV ヘッダ。data サイズを最大値にしておき、以降 PCM を継ぎ足しで
    流し込んで EOF(パイプ close) まで再生させる。ffplay は生 PCM の -ac/-ar 指定を
    受け付けないため、自己記述の WAV として先頭にこのヘッダを送る。"""
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 0xFFFFFFFF, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample,
        b"data", 0xFFFFFFFF,
    )


def make_silence_pcm(sample_rate, channels, bits_per_sample, duration_sec):
    num_samples = int(sample_rate * duration_sec)
    return b"\x00" * (num_samples * channels * bits_per_sample // 8)


# --- Irodori VoiceDesign サーバ (:5001) クライアント ---

IRODORI_URL_DEFAULT = "http://127.0.0.1:5001"
IRODORI_SR = 48000  # irodori_server.py の出力サンプルレート
# 1リクエストに渡す最大文字数。Irodori の合成コストは文字数に対して超線形
# (実測: 32字0.096s/字, 64字0.196s/字, 128字0.277s/字 ≒ O(n^2))。
# 150字だと1チャンク20〜35秒かかり長文が激遅になるため、効率と句読点分割の
# 両立点である ~48字にする。小さすぎ(<32)ると固定オーバーヘッドが目立つ。
IRODORI_CHUNK = 48

def irodori_synth(text, style, base_url=IRODORI_URL_DEFAULT):
    """Irodori サーバに1チャンク投げて WAV bytes を得る"""
    body = json.dumps({"text": text, "style": style}).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/synth", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def irodori_health(base_url=IRODORI_URL_DEFAULT):
    """サーバ生死確認。OKなら styles の dict、死んでいれば None"""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/styles", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _irodori_port(base_url):
    m = re.search(r":(\d+)", base_url)
    return m.group(1) if m else "5001"


def _irodori_port_open(base_url):
    """ポートに接続できるか。ロード中のサーバは health に応答しないが
    bind 済みなので TCP 接続だけは通る → 「起動中の別サーバがいる」の判定に使う。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(_irodori_port(base_url))), timeout=1):
            return True
    except OSError:
        return False


def _install_irodori():
    """Irodori-TTS を自動インストールする (Python 直実装)。"""
    install_dir = Path(os.environ.get("IRODORI_DIR") or Path.home() / "ai-tools" / "Irodori-TTS")
    here = Path(__file__).parent

    uv = shutil.which("uv")
    git = shutil.which("git")
    if not git:
        print("git が見つかりません。git をインストールしてから再試行してください。", file=sys.stderr)
        return False
    if not uv:
        print("uv が見つかりません。https://docs.astral.sh/uv/ からインストールしてください。", file=sys.stderr)
        return False

    print("Irodori が見つからないため自動インストールを開始します。")
    print(f"  インストール先: {install_dir}")
    print("  (torch 等のダウンロードで数分〜十数分かかります)")

    # 1. git clone
    if (install_dir / ".git").exists():
        print("[1/3] 既にクローン済み → スキップ")
    else:
        print(f"[1/3] git clone Irodori-TTS...")
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        ret = subprocess.run(
            [git, "clone", "https://github.com/Aratako/Irodori-TTS.git", str(install_dir)]
        )
        if ret.returncode != 0:
            print("git clone に失敗しました。", file=sys.stderr)
            return False

    # 2. uv sync
    print("[2/3] uv sync (依存解決、数分〜十数分かかります)...")
    ret = subprocess.run([uv, "sync"], cwd=str(install_dir))
    if ret.returncode != 0:
        print("uv sync に失敗しました。", file=sys.stderr)
        return False

    # uv sync は CPU ビルドの torch を入れることがある。GPU があれば cu128 に差し替え。
    venv_py = install_dir / ".venv" / ("Scripts" if IS_WINDOWS else "bin") / ("python.exe" if IS_WINDOWS else "python")
    if venv_py.exists():
        r = subprocess.run(
            [str(venv_py), "-c", "import torch; print(torch.cuda.is_available())"],
            capture_output=True, text=True
        )
        if r.stdout.strip() != "True":
            if shutil.which("nvidia-smi"):
                print("  CUDA 非対応 torch が入りました。GPU 用 (cu128) に再インストールします...")
                # --reinstall 必須: 同バージョンが入っていると uv は "Audited" だけで何もしない
                subprocess.run(
                    [uv, "pip", "install", "--reinstall", "torch", "torchaudio",
                     "--index-url", "https://download.pytorch.org/whl/cu128"],
                    cwd=str(install_dir)
                )
            else:
                print("  注意: NVIDIA GPU が見つかりません。CPU モードで動作します (低速)。")

    # 3. irodori_server.py と voices/ を配置
    print("[3/3] irodori_server.py と voices/ を配置...")
    server_src = here / "irodori_server.py"
    if server_src.exists():
        shutil.copy2(str(server_src), str(install_dir / "irodori_server.py"))
    voices_dst = install_dir / "voices"
    voices_dst.mkdir(exist_ok=True)
    voices_src = here / "voices"
    if voices_src.is_dir():
        for wav in voices_src.glob("*.wav"):
            shutil.copy2(str(wav), str(voices_dst / wav.name))

    print("インストール完了。")
    return True


def start_irodori_server(base_url=IRODORI_URL_DEFAULT, _retry=False):
    """Irodori サーバをローカルで起動する。
    起動できたら Popen オブジェクト (truthy) を返す。失敗なら False。
    プロセスは切り離すので clip_tts と独立に生きる (poll() でのみ死活監視)。
    Irodori が未インストールの場合は自動インストール後に再試行する。
    """
    port = _irodori_port(base_url)
    candidates = [
        os.environ.get("IRODORI_DIR"),
        str(Path.home() / "ai-tools" / "Irodori-TTS"),
    ]
    for d in candidates:
        if not d or not Path(d).is_dir():
            continue
        env = os.environ.copy()
        env["HF_HUB_DISABLE_XET"] = "1"
        if not IS_WINDOWS:
            env.pop("LD_LIBRARY_PATH", None)   # cudnn 衝突回避 (Linux のみ)
        # venv python パスは OS で異なる。Windows はコンソールを持たない pythonw.exe を優先
        # (python.exe だと Win11 でサーバ用の黒いウィンドウが出てしまう)
        venv_bin = Path(d) / ".venv" / ("Scripts" if IS_WINDOWS else "bin")
        if IS_WINDOWS:
            venv_py = venv_bin / "pythonw.exe"
            if not venv_py.exists():
                venv_py = venv_bin / "python.exe"
        else:
            venv_py = venv_bin / "python"
        if venv_py.exists():
            cmd = [str(venv_py), "irodori_server.py", "--port", port]
        elif shutil.which("uv"):
            cmd = ["uv", "run", "--no-sync", "python", "irodori_server.py", "--port", port]
        else:
            continue
        try:
            log_path = Path(tempfile.gettempdir()) / "irodori-server.log"
            log = open(log_path, "ab")
            kwargs = dict(cwd=d, env=env, stdout=log,
                          stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            if IS_WINDOWS:
                # DETACHED_PROCESS は Win11 (既定ターミナル=Windows Terminal) だと
                # 黒いコンソールウィンドウが表示される。CREATE_NO_WINDOW で完全に隠す。
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            return subprocess.Popen(cmd, **kwargs)
        except Exception as e:
            print(f"Irodori サーバ起動に失敗: {e}", file=sys.stderr)
            return False
    # 候補ディレクトリが見つからなかった → 自動インストールして再試行
    if not _retry and _install_irodori():
        return start_irodori_server(base_url, _retry=True)
    return False


def split_chunks(text, limit=IRODORI_CHUNK):
    """句点・読点優先で limit 文字以内に分割。
    Irodori は英語も読めるので split_sentences のような日本語フィルタは掛けない。
    limit 内に区切りが無い時は語の途中で切れて不自然な間が出る (例: アカウント→
    アカ/ウント) ため、少しだけ先 (最大 limit+16) まで区切り(句読点・空白)を探してから切る。"""
    chunks = []
    while len(text) > limit:
        cut = max(text.rfind("。", 0, limit), text.rfind("、", 0, limit),
                  text.rfind("！", 0, limit), text.rfind("？", 0, limit))
        if cut < 0:
            # limit 内に区切り無し → 語の途中切りを避けるため少し先まで探す
            ext = text[:limit + 16]
            cut = max(ext.rfind("。"), ext.rfind("、"), ext.rfind("！"),
                      ext.rfind("？"), ext.rfind("　"), ext.rfind(" "))
        cut = cut + 1 if cut >= 0 else limit
        chunks.append(text[:cut])
        text = text[cut:]
    if text.strip():
        chunks.append(text)
    return chunks


def wav_to_pcm16(wav_bytes, sample_rate, ffmpeg_bin):
    """任意フォーマットの WAV bytes → mono s16 raw PCM (irodori の float WAV 結合用)"""
    p = subprocess.run(
        [ffmpeg_bin, "-v", "error", "-i", "pipe:0",
         "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "pipe:1"],
        input=wav_bytes, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg PCM変換失敗: {p.stderr.decode(errors='replace')[:200]}")
    return p.stdout


def apply_fx_file(wav_path, pitch, speed, ffmpeg_bin):
    """WAVファイルにピッチ/話速加工 (各0.5〜2.0)。加工後の新しい一時ファイルパスを返す。
    pitch は asetrate+atempo 補正で長さ保持、speed は atempo でピッチ保持"""
    if abs(pitch - 1.0) < 1e-3 and abs(speed - 1.0) < 1e-3:
        return wav_path
    ffprobe = shutil.which("ffprobe") or str(
        Path(ffmpeg_bin).parent / ("ffprobe.exe" if IS_WINDOWS else "ffprobe"))
    try:
        sr = int(subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of", "csv=p=0", wav_path],
            capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        # ffprobe が無いときの既定 (SBV2=44100 / Irodori=48000 なので WAV ヘッダから読む)
        try:
            with open(wav_path, "rb") as fh:
                sr = struct.unpack("<I", fh.read(28)[24:28])[0]
        except Exception:
            sr = IRODORI_SR
    filters = []
    if abs(pitch - 1.0) >= 1e-3:
        filters += [f"asetrate={int(sr * pitch)}", f"aresample={sr}",
                    f"atempo={1.0 / pitch:.6f}"]
    if abs(speed - 1.0) >= 1e-3:
        filters.append(f"atempo={speed:.6f}")
    dst = wav_path + ".fx.wav"
    p = subprocess.run(
        [ffmpeg_bin, "-y", "-loglevel", "error", "-i", wav_path,
         "-af", ",".join(filters), dst], capture_output=True)
    if p.returncode != 0:
        print(f"(ピッチ/速度加工失敗、無加工で再生: {p.stderr.decode(errors='replace')[:120]})",
              file=sys.stderr)
        return wav_path
    try:
        os.remove(wav_path)
    except OSError:
        pass
    return dst


# --- ffplay/ffmpeg検出 ---

def find_ffplay():
    bin = shutil.which("ffplay")
    if bin:
        return bin
    for p in [str(Path.home() / "ffmpeg" / "bin" / "ffplay.exe")]:
        if Path(p).exists():
            return p
    return None


def find_ffmpeg(ffplay_bin):
    """ffmpeg 本体を探す (ffplay と同じディレクトリを優先)"""
    bin = shutil.which("ffmpeg")
    if bin:
        return bin
    if ffplay_bin:
        cand = Path(ffplay_bin).parent / ("ffmpeg.exe" if IS_WINDOWS else "ffmpeg")
        if cand.exists():
            return str(cand)
    return None


# --- メイン ---

MODELS = {
    0: ("amitaro", "amitaro.safetensors", "config.json", "style_vectors.npy"),
    1: ("jvnv-F1-jp", "jvnv-F1-jp_e160_s14000.safetensors", "config.json", "style_vectors.npy"),
    2: ("jvnv-F2-jp", "jvnv-F2_e166_s20000.safetensors", "config.json", "style_vectors.npy"),
    3: ("jvnv-M1-jp", "jvnv-M1-jp_e158_s14000.safetensors", "config.json", "style_vectors.npy"),
    4: ("jvnv-M2-jp", "jvnv-M2-jp_e159_s17000.safetensors", "config.json", "style_vectors.npy"),
    5: ("koharune-ami", "koharune-ami.safetensors", "config.json", "style_vectors.npy"),
}


def main():
    parser = argparse.ArgumentParser(description="クリップボード→TTS再生（常駐型）")
    parser.add_argument("--model-id", "-m", type=int, default=0,
                        help="モデルID (0=amitaro) ※-v sbv2 のときのみ有効")
    parser.add_argument("--voice", "-v", default="sbv2",
                        help="声: sbv2=従来(in-process, -m でモデル選択) / "
                             "calm・bright 等=Irodori VoiceDesign サーバ(:5001) の style名")
    parser.add_argument("--irodori-url", default=IRODORI_URL_DEFAULT,
                        help=f"Irodori サーバURL (default: {IRODORI_URL_DEFAULT})")
    parser.add_argument("--pitch", type=float, default=1.0,
                        help="声の高さ倍率 0.5〜2.0 (長さ保持, default: 1.0)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="話速倍率 0.5〜2.0 (ピッチ保持, default: 1.0)")
    parser.add_argument("--pause", type=float, default=0.5,
                        help="文間の無音（秒）")
    parser.add_argument("--hotkey-play", default="ctrl+shift+p",
                        help="再生のグローバルホットキー (default: ctrl+shift+p / Mac: Option+Shift+P)")
    parser.add_argument("--hotkey-pause", default="ctrl+shift+space",
                        help="一時停止/再開のホットキー (default: ctrl+shift+space / Mac: Option+Shift+Space)")
    parser.add_argument("--hotkey-rewind", default="ctrl+shift+a",
                        help="巻き戻しのホットキー (default: ctrl+shift+a / Mac: Option+Shift+A)")
    parser.add_argument("--hotkey-forward", default="ctrl+shift+d",
                        help="早送りのホットキー (default: ctrl+shift+d / Mac: Option+Shift+D)")
    parser.add_argument("--hotkey-stop", default="ctrl+shift+s",
                        help="停止のグローバルホットキー (default: ctrl+shift+s / Mac: Option+Shift+S)")
    parser.add_argument("--hotkey-quit", default="ctrl+shift+q",
                        help="終了のグローバルホットキー (default: ctrl+shift+q / Mac: Option+Shift+Q)")
    parser.add_argument("--no-hotkey", action="store_true",
                        help="グローバルホットキーを使わない（ターミナル内キーのみ）")
    parser.add_argument("--no-prefetch", action="store_true",
                        help="クリップボード変更時の事前合成を無効化")
    parser.add_argument("--no-stream", action="store_true",
                        help="[Windows/Irodori] 長文のストリーミング再生を無効化（全文合成してから再生）")
    parser.add_argument("--autoplay", action="store_true",
                        help="クリップボード変更時に自動再生（デフォルト: pキーで手動再生）")
    parser.add_argument("--allow-multiple", action="store_true",
                        help="二重起動を許可（既定は禁止。複数常駐するとショートカットが全部に届き重複再生する）")
    parser.add_argument("--stop-server-on-exit", action="store_true",
                        help="終了時に Irodori サーバ(:5001)も停止して VRAM を解放（自分で起動した場合は自動で有効）")
    parser.add_argument("--no-start-server", action="store_true",
                        help="Irodori サーバが落ちていても自動起動しない（既定は自動起動して所有）")
    args = parser.parse_args()

    if args.model_id not in MODELS:
        print(f"モデルID {args.model_id} が見つかりません（0-5）", file=sys.stderr)
        sys.exit(1)

    for _n, _v in (("pitch", args.pitch), ("speed", args.speed)):
        if not (0.5 <= _v <= 2.0):
            parser.error(f"--{_n} は 0.5〜2.0 の範囲で指定してください (指定値: {_v})")

    # 二重起動防止（重複再生・停止不能の原因になるため）
    if not args.allow_multiple:
        _single_instance_or_exit()

    ffplay_bin = find_ffplay()
    if not ffplay_bin:
        print("ffplay が見つかりません。ffmpegをインストールしてください。", file=sys.stderr)
        sys.exit(1)
    ffmpeg_bin = find_ffmpeg(ffplay_bin)
    use_irodori = args.voice != "sbv2"
    if use_irodori and not ffmpeg_bin:
        print("ffmpeg が見つかりません (Irodori の WAV 結合に必要)。", file=sys.stderr)
        sys.exit(1)

    # 自分で起動して所有する Irodori サーバの Popen。終了時にこれを確実に止める
    # (pkill は Windows に無いので名前ではなく PID/ハンドルで止める)。
    _owned = {"proc": None}

    if use_irodori:
        # Irodori クローンサーバ (:5001) を使う。モデルロード不要 (torch 不要)。
        info = irodori_health(args.irodori_url)
        if info is None and not args.no_start_server:
            # サーバが落ちていれば clip_tts が自分で起動して所有する (Windows の in-process 相当)。
            # 自分で起動した場合は終了時にそのサーバも落として VRAM を解放する。
            print(f"Irodori サーバが起動していないので起動します ({args.irodori_url})...")
            _proc = start_irodori_server(args.irodori_url)
            if _proc:
                # 初回はモデルDL(約4GB)があるため最大10分待つ。
                # モデルはインストール先ではなく HF キャッシュに落ちるのでそちらを見る
                _hub = os.environ.get("HF_HUB_CACHE") or str(
                    Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub")
                _first_run = not (Path(_hub) / "models--Aratako--Irodori-TTS-500M-v3").exists()
                _max_wait = 600 if _first_run else 180
                if _first_run:
                    print("初回起動のためモデルをダウンロードします (約4GB、数分〜十数分)...")
                else:
                    print("ロード中 (〜1〜2分)...")
                for _i in range(_max_wait // 3):
                    time.sleep(3)
                    info = irodori_health(args.irodori_url)
                    if info is not None:
                        break
                    # サーバプロセスが即死していたら原因を切り分ける
                    if _proc is not None and hasattr(_proc, "poll") and _proc.poll() is not None:
                        if _irodori_port_open(args.irodori_url):
                            # bind 負け = 別のサーバがロード中 → そちらの完了を待つ
                            print("  別の Irodori サーバが起動中のためそちらを待ちます...")
                            _proc = None
                            continue
                        _log = Path(tempfile.gettempdir()) / "irodori-server.log"
                        print(f"Irodori サーバが起動直後に終了しました (exit={_proc.returncode})。",
                              file=sys.stderr)
                        print(f"ログ確認: {_log}", file=sys.stderr)
                        break
                    if _i > 0 and _i % 10 == 0:
                        print(f"  起動待ち... ({(_i + 1) * 3}秒経過)")
                if info is not None:
                    if _proc is not None:  # 自分の proc が生きている = 本当に所有
                        _owned["proc"] = _proc
                        args.stop_server_on_exit = True  # 終了時に落とす
                    print("Irodori サーバ起動完了。")
        if info is None:
            print(f"Irodori サーバ ({args.irodori_url}) に接続できません。", file=sys.stderr)
            _iport = _irodori_port(args.irodori_url)
            _idir = os.environ.get("IRODORI_DIR", "")
            if IS_WINDOWS:
                _idir = _idir or str(Path.home() / "ai-tools" / "Irodori-TTS")
                print(f"手動起動: cd \"{_idir}\" && "
                      f".venv\\Scripts\\python.exe irodori_server.py --port {_iport}", file=sys.stderr)
                print("または環境変数 IRODORI_DIR にインストール先を設定してください。", file=sys.stderr)
            else:
                _idir = _idir or str(Path.home() / "ai-tools" / "Irodori-TTS")
                print(f"手動起動: cd {_idir} && "
                      f"env -u LD_LIBRARY_PATH HF_HUB_DISABLE_XET=1 "
                      f"nohup .venv/bin/python irodori_server.py --port {_iport} &", file=sys.stderr)
            sys.exit(1)
        styles = list(info.get("styles", {}))
        if args.voice not in styles:
            print(f"style '{args.voice}' はサーバに未定義 (使える声: {styles})。"
                  f"voices/{args.voice}.wav を置いてサーバ再起動してください。", file=sys.stderr)
            sys.exit(1)
        name = f"irodori:{args.voice}"
        device = args.irodori_url
        model = None
    else:
        # モデルロード (SBV2 in-process)
        print("TTSモデルを読み込み中...")
        import torch
        from style_bert_vits2.constants import Languages
        from style_bert_vits2.nlp import bert_models
        from style_bert_vits2.tts_model import TTSModel

        device = "cuda" if torch.cuda.is_available() else "cpu"
        bert_models.load_model(Languages.JP, device_map=device)
        bert_models.load_tokenizer(Languages.JP)

        model_root = SBV2_DIR / "model_assets"
        name, weights, cfg, style_vec = MODELS[args.model_id]
        model = TTSModel(
            model_path=model_root / name / weights,
            config_path=model_root / name / cfg,
            style_vec_path=model_root / name / style_vec,
            device=device,
        )

    SEEK_STEP = 10  # 早送り/巻き戻しの秒数

    # グローバルショートカットの方式を決定
    #   Windows → RegisterHotKey (キー専有) / Linux・Mac → シグナル (OS ショートカット経由)
    hotkey_mode = None
    if not args.no_hotkey:
        hotkey_mode = "win" if IS_WINDOWS else "signal"

    print()
    print("=== クリップボード→TTS再生 ===")
    print(f"モデル: {name} / デバイス: {device}")
    if hotkey_mode == "win":
        print(f"グローバルホットキー: (Mac keyboard: Option=Ctrl として押す)")
        print(f"  {args.hotkey_play:<24} → 再生")
        print(f"  {args.hotkey_pause:<24} → 一時停止/再開")
        print(f"  {args.hotkey_rewind:<24} → 巻き戻し ({SEEK_STEP}秒)")
        print(f"  {args.hotkey_forward:<24} → 早送り ({SEEK_STEP}秒)")
        print(f"  {args.hotkey_stop:<24} → 停止")
        print(f"  {args.hotkey_quit:<24} → 終了")
        print(f"（ターミナル内でも p/space/a/d/s/q で同じ操作が可能）")
    elif hotkey_mode == "signal":
        print(f"ターミナルキー: p=再生 / space=一時停止 / a=巻戻 / d=早送 / s=停止 / q=終了")
        print(f"OS グローバルショートカット (PID={os.getpid()}): "
              f"下記を GNOME のキー設定に割当 (自動: bin/clip_tts_gnome_hotkey.sh)")
        print(f"  再生 : pkill -USR1 -f 'clip_tts[.]py'   （既定: Ctrl+Alt+P に割当）")
        print(f"  停止 : pkill -USR2 -f 'clip_tts[.]py'   （既定: Ctrl+Alt+S）")
    else:
        print("ターミナルキー: p=再生 / space=一時停止 / a=巻戻 / d=早送 / s=停止 / q=終了")
    print("==============================")
    print()

    # 再生状態
    state = {
        "ffplay": None,
        "wav_path": None,      # 現在再生中のWAVファイルパス
        "start_pos": 0.0,      # 最後にffplayに渡した -ss 値
        "start_time": None,    # ffplay起動時のtime.time()
        "paused_at": None,     # 一時停止中の再生位置（秒）。Noneなら停止or再生中
        "streaming": False,    # [Windows] ストリーミング再生中か (シーク/一時停止を無効化)
    }

    def current_position():
        """現在の再生位置（秒）を推定"""
        if state["paused_at"] is not None:
            return state["paused_at"]
        if state["start_time"] is None:
            return 0.0
        return state["start_pos"] + (time.time() - state["start_time"])

    def _kill_ffplay():
        p = state["ffplay"]
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        state["ffplay"] = None

    def _start_ffplay(start_pos=0.0):
        """現在のwav_pathをstart_posから再生開始"""
        if not state["wav_path"]:
            return
        cmd = [ffplay_bin, "-nodisp", "-autoexit", "-loglevel", "error", "-hide_banner"]
        if start_pos > 0:
            cmd.extend(["-ss", f"{start_pos:.2f}"])
        cmd.append(str(state["wav_path"]))
        state["ffplay"] = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        state["start_pos"] = start_pos
        state["start_time"] = time.time()
        state["paused_at"] = None

    def stop_playback():
        with playback_lock:
            _kill_ffplay()
            state["start_time"] = None
            state["paused_at"] = None
            state["streaming"] = False

    def pause_toggle():
        """一時停止/再開"""
        with playback_lock:
            if IS_WINDOWS and state.get("streaming"):
                print("(ストリーミング再生中は一時停止できません。停止=s)", flush=True)
                return
            if state["paused_at"] is not None:
                # 再開
                pos = state["paused_at"]
                print(f"[再開] {pos:.1f}秒から")
                _start_ffplay(pos)
                return
            p = state["ffplay"]
            if p and p.poll() is None:
                pos = current_position()
                _kill_ffplay()
                state["paused_at"] = pos
                print(f"[一時停止] {pos:.1f}秒")
            else:
                print("(再生中ではありません)")

    def seek(delta):
        """現在位置から delta 秒シーク"""
        with playback_lock:
            if IS_WINDOWS and state.get("streaming"):
                print("(ストリーミング再生中はシークできません。停止=s)", flush=True)
                return
            if not state["wav_path"]:
                print("(再生対象がありません)")
                return
            new_pos = max(0.0, current_position() + delta)
            # 再生中でも一時停止中でも、新しい位置から再開
            _kill_ffplay()
            print(f"[{'早送り' if delta > 0 else '巻き戻し'}] → {new_pos:.1f}秒")
            _start_ffplay(new_pos)

    # 事前合成のキャッシュ
    cache = {
        "text": None,       # 合成元のクリップボードテキスト（生）
        "wav_path": None,   # 合成済みWAVのパス
        "lock": threading.Lock(),
    }
    # 合成は一度に1つだけ。先読み(監視)スレッドと手動再生スレッドが同じ長文を
    # 別々にサーバへ投げると、単一スレッドのサーバが二重負荷で詰まるため直列化する。
    synth_lock = threading.Lock()

    # 再生の「停止→開始」をアトミックにするロック。p(再生)を素早く二度押しても
    # ffplay が二重起動して音が重なるのを防ぐ（stop/seek/pause も同じロックで直列化）。
    # stop_playback を内側から再入するため RLock。
    playback_lock = threading.RLock()

    def _play_wav(wav):
        """再生中を止めて wav を先頭から再生する（二重再生防止）。"""
        with playback_lock:
            stop_playback()
            state["wav_path"] = wav
            _start_ffplay(0.0)

    def _is_speech_like(text):
        """TTSで読み上げる価値があるテキストか判定（ノイズ除外）"""
        if not text or len(text.strip()) < 3:
            return False
        # 単純すぎる文字列（URLだけ、パスだけ等）はスキップ
        s = text.strip()
        if re.fullmatch(r"https?://\S+", s):
            return False
        if re.fullmatch(r"[A-Za-z0-9_./\\:\-=?&%+#]+", s):
            return False
        # 日本語 or 十分な英数混在
        if not re.search(r"[぀-ヿ一-鿿]", s) and len(s) < 20:
            return False
        if len(s) > 5000:  # 長すぎるのも事前合成は避ける
            return False
        return True

    _restart_lock = threading.Lock()

    def _restart_irodori():
        """セッション中にサーバが死んでいたら再起動して復帰を待つ (モデルDL済み前提で最大180秒)。
        事前合成スレッドと再生スレッドが同時に呼んでも二重 spawn しないよう直列化する。"""
        with _restart_lock:
            if irodori_health(args.irodori_url) is not None:
                return True  # 生きている (一時的な通信エラー or 他スレッドが復旧済み)
            print("  Irodori サーバが落ちています。再起動します (〜1〜2分)...")
            proc = start_irodori_server(args.irodori_url)
            if not proc:
                return False
            for _ in range(60):
                time.sleep(3)
                if irodori_health(args.irodori_url) is not None:
                    print("  Irodori サーバ復帰。")
                    if proc is not None:  # 自分で起動し直した → 終了時に落とす
                        _owned["proc"] = proc
                        args.stop_server_on_exit = True
                    return True
                if proc is not None and hasattr(proc, "poll") and proc.poll() is not None:
                    if _irodori_port_open(args.irodori_url):
                        proc = None  # bind 負け = 別サーバがロード中 → そちらを待つ
                        continue
                    return False
            return False

    # 語の途中で強制分割された時に「間」を入れると不自然 (例: アカウント→アカ ▷ ウント)
    # になるため、チャンクがこの文字で終わっている時だけポーズを挟む。
    _IRODORI_PAUSE_AFTER = "。、！？!?，,；;…"

    def _irodori_prepare(text):
        """Irodori に渡す前処理 (HTML/MD除去 + 辞書 + 記号→ポーズ)。空なら None を返す。
        eng2kana の alkana 変換は掛けず辞書語だけ置換 (Irodori は英単語・数字を読める)。"""
        # HTML/Markdown の除去だけ preprocess と同じものを掛ける
        if re.search(r"<[a-z][^>]*>", text, re.IGNORECASE):
            p = HTMLTextExtractor()
            p.feed(text)
            text = p.get_text()
        if re.search(r"[#`*\[]", text):
            text = strip_markdown(text)
        text = re.sub(r"[━─=\-]{3,}", "", text).strip()
        if not text:
            return None
        # http(s) の URL は読み上げない。ASCII の URL 文字が続く限りを1つの塊とみなし、
        # 空白・括弧・引用符・日本語(2バイト文字)が出た所で終端 → 一拍(、)に置換する。
        text = re.sub(r"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%]*", "、", text)
        # dictionary.csv のカスタム読み (固有名詞・製品名・略語)。辞書語だけ(dict_only)。
        text = replace_japanese_readings(text)
        text = replace_english_words(text, dict_only=True)
        # 記号は「とにかく全部」名前で読まず一拍(、)にする。残すのは:
        #   かな・漢字・英数字(全角含む)・長音ー・々・句読点(。、！？!?)・空白・％、
        #   および数字に付く 小数点/桁区切り/時刻コロン(3.14 / 1,000 / 10:30 / 16:9)。
        # それ以外(/ ・ : ; @ # & = < > 【】「」() ○●★※〆♪ ℃ ¥ → 等すべて)は 、 に。
        text = re.sub(r"[〜～]", "から", text)     # 波ダッシュだけは「から」と読む
        text = re.sub(r"[\r\n]+", "。", text)      # 改行(CR/LF)→句点(一拍)
        text = re.sub(r"(?<=\d)[.．](?=\d)", "\x01", text)   # 小数点を一時退避 (3.14)
        text = re.sub(r"(?<=\d)[,，](?=\d)", "\x02", text)   # 桁区切りを一時退避 (1,000)
        text = re.sub(r"(?<=\d)[:：](?=\d)", "\x03", text)   # 時刻/比を一時退避 (10:30, 16:9)
        text = re.sub(r"[^0-9A-Za-z０-９Ａ-Ｚａ-ｚ"
                      r"ぁ-ゖァ-ヺー々一-鿿"
                      r"。、！？!?\s%％\x01\x02\x03]", "、", text)
        text = text.replace("\x01", ".").replace("\x02", ",").replace("\x03", ":")
        # 記号・空白が連続しても「間」は一拍だけにする(何拍も空けない)。、。とその周囲の
        # 空白の連続を1つに畳む(run 内に。があれば。、無ければ、)。
        text = re.sub(r"[ \t　]*[、。][、。\s　]*",
                      lambda m: "。" if "。" in m.group() else "、", text)
        # 記号を含まない複数の空白も一拍(、)に(単一の空白は語間としてそのまま残す)。
        text = re.sub(r"[ \t　]{2,}", "、", text)
        text = re.sub(r"^[、。]+|[、。]+$", "", text).strip()   # 先頭・末尾の余分な間を除去
        return text or None

    def _cap_silence_pcm(pcm):
        """合成後 PCM 内の無音(=間)を最大 IRODORI_PAUSE_SEC 秒(既定0.25=半拍)に切り詰め、
        すべての間を短く均一にする。Irodori が 、。 等で作る間の長さは合成側で決まりテキストでは
        変えられないため、音声から無音区間を検出して上限を掛ける。IRODORI_PAUSE_SEC=0 で無効。"""
        try:
            sec = float(os.environ.get("IRODORI_PAUSE_SEC", "0.25"))
        except ValueError:
            sec = 0.25
        if sec <= 0 or not ffmpeg_bin or not pcm:
            return pcm
        # 0.12秒以上・-45dB以下の無音区間を検出し、残す無音を sec 秒までに切り詰める
        af = (f"silenceremove=stop_periods=-1:stop_duration=0.12"
              f":stop_threshold=-45dB:stop_silence={sec:.3f}")
        try:
            p = subprocess.run(
                [ffmpeg_bin, "-v", "error", "-f", "s16le", "-ar", str(IRODORI_SR),
                 "-ac", "1", "-i", "pipe:0", "-af", af, "-f", "s16le", "pipe:1"],
                input=pcm, capture_output=True, timeout=30)
            if p.returncode == 0 and p.stdout:
                return p.stdout
        except Exception:
            pass
        return pcm

    def _irodori_synth_chunk(chunk, i=0):
        """1チャンクを合成して PCM(bytes) を返す。無音(間)は半拍に切り詰める。
        サーバ死亡時は1回だけ再起動リトライ。失敗 None。"""
        for _attempt in range(2):
            try:
                wav = irodori_synth(chunk, args.voice, args.irodori_url)
                return _cap_silence_pcm(wav_to_pcm16(wav, IRODORI_SR, ffmpeg_bin))
            except Exception as e:
                if _attempt == 0 and _restart_irodori():
                    continue
                print(f"  エラー[{i+1}]: {e}", flush=True)
        return None

    def _irodori_gap_after(prev_chunk):
        """直前チャンクの末尾が句読点ならチャンク境界に挿入する無音を返す。半拍=args.pause の
        半分(既定 0.5→0.25 秒)。語の途中(強制分割)なら 0。SBV2 経路の args.pause は不変。"""
        if prev_chunk and prev_chunk.rstrip()[-1:] in _IRODORI_PAUSE_AFTER:
            return args.pause * 0.5
        return 0.0

    def _synthesize_irodori(text):
        """Irodori サーバでチャンクごとに合成し、PCM 連結して WAV ファイルを返す (全文一括)。"""
        prepared = _irodori_prepare(text)
        if not prepared:
            return None
        all_pcm = b""
        prev = None
        for i, chunk in enumerate(split_chunks(prepared)):
            pcm = _irodori_synth_chunk(chunk, i)
            if pcm is None:
                continue
            if all_pcm and _irodori_gap_after(prev) > 0:
                all_pcm += make_silence_pcm(IRODORI_SR, 1, 16, args.pause)
            all_pcm += pcm
            prev = chunk
        if not all_pcm:
            return None
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(make_wav(IRODORI_SR, 1, 16, all_pcm))
        tmp.close()
        return tmp.name

    def _stream_eligible():
        """ストリーミング再生を使う条件: Windows + Irodori + 加工(pitch/speed)なし。
        Ubuntu/Mac・SBV2・--no-stream・pitch/speed 変更時は従来の「全文合成→再生」経路。"""
        return (IS_WINDOWS and use_irodori and not getattr(args, "no_stream", False)
                and args.pitch == 1.0 and args.speed == 1.0)

    def _play_streaming_irodori(text):
        """[Windows 専用] 先頭チャンクが出来た瞬間に鳴らし、残りは裏で合成しながら
        ffplay へ PCM を流し込む。長文でも最初の音が ~2秒で出る。合成し終えた全文は
        キャッシュにも保存し、次回押下からは通常のファイル再生(シーク/一時停止可)になる。
        ※パイプ再生のため、ストリーミング中は シーク/早送り/巻戻し/一時停止 は無効
          (停止 s・終了 q は有効)。Ubuntu/Linux/Mac はこの経路を通らない。"""
        prepared = _irodori_prepare(text)
        if not prepared:
            print("(合成対象なし)", flush=True)
            return

        def _run():
            proc = None
            with synth_lock:            # 先読み(監視)スレッドと直列化 (サーバ二重負荷回避)
                # ロック待ちの間に先読みが完了していたら、それを通常再生(フル機能)する
                with cache["lock"]:
                    done = cache["wav_path"] if cache["text"] == text else None
                if done:
                    print(f"[即再生] {len(text)}文字 (先読み済み)", flush=True)
                    _play_wav(done)
                    return
                chunks = split_chunks(prepared)
                # ffplay は 生PCM の -ac/-ar 指定を受け付けないので「自己記述の WAV」を流す。
                # 先頭に data サイズ最大の WAV ヘッダを送り、以降 PCM を継ぎ足す(EOFまで再生)。
                cmd = [ffplay_bin, "-nodisp", "-autoexit", "-loglevel", "quiet",
                       "-hide_banner", "-i", "pipe:0"]
                with playback_lock:
                    stop_playback()
                    try:
                        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                                stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
                    except Exception as e:
                        print(f"(ffplay 起動失敗: {e})", flush=True)
                        return
                    state["ffplay"] = proc
                    state["wav_path"] = None
                    state["streaming"] = True
                    state["start_time"] = None
                    state["paused_at"] = None

                acc = b""
                prev = None
                interrupted = False
                try:  # 先頭に WAV ヘッダを送る (以降は PCM を継ぎ足す)
                    proc.stdin.write(make_wav_stream_header(IRODORI_SR))
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    interrupted = True
                for i, chunk in enumerate(chunks):
                    if interrupted:
                        break
                    with playback_lock:      # 停止(s)されたら中断
                        alive = state.get("ffplay") is proc and proc.poll() is None
                    if not alive:
                        interrupted = True
                        break
                    pcm = _irodori_synth_chunk(chunk, i)
                    if pcm is None:
                        continue
                    gap = _irodori_gap_after(prev)
                    out = (make_silence_pcm(IRODORI_SR, 1, 16, gap) if (acc and gap > 0) else b"") + pcm
                    if not acc:  # 最初の音が出る瞬間
                        print("▶ 再生中 (ストリーミング中はシーク/一時停止不可・巻戻すなら stop→再度p / 停止=s)", flush=True)
                    try:
                        proc.stdin.write(out)
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        interrupted = True
                        break
                    acc += out
                    prev = chunk
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                if acc and not interrupted:
                    # 全文を WAV 化してキャッシュ → stop→再度 p で完成ファイル再生
                    # (キャッシュヒットで即・シーク/早送り/巻戻し/一時停止すべて可)。
                    try:
                        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                        tmp.write(make_wav(IRODORI_SR, 1, 16, acc))
                        tmp.close()
                        with cache["lock"]:
                            cache["text"] = text
                            cache["wav_path"] = tmp.name
                    except Exception:
                        pass
                elif not acc and not interrupted:
                    print("(合成失敗)", flush=True)
            # synth_lock 解放後: この proc がまだ現役ならストリーミング状態を解除
            with playback_lock:
                if proc is not None and state.get("ffplay") is proc:
                    state["streaming"] = False

        threading.Thread(target=_run, daemon=True).start()

    def _synthesize(text):
        """テキストからWAVファイルを生成してパスを返す（失敗時None）"""
        if use_irodori:
            wav_path = _synthesize_irodori(text)
        else:
            wav_path = _synthesize_sbv2(text)
        if wav_path and ffmpeg_bin:
            wav_path = apply_fx_file(wav_path, args.pitch, args.speed, ffmpeg_bin)
        return wav_path

    def _synthesize_sbv2(text):
        """SBV2 (in-process) で合成して WAV ファイルパスを返す（従来経路）"""
        processed = preprocess(text)
        sentences = split_sentences(processed)
        if not sentences:
            return None

        all_pcm = b""
        sample_rate = None
        for i, sentence in enumerate(sentences):
            # 段落マーカー検出（改行直後の文は無音を長くとる）
            paragraph_break = sentence.startswith(PARAGRAPH_BREAK)
            if paragraph_break:
                sentence = sentence.lstrip(PARAGRAPH_BREAK).lstrip()
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
                pcm = audio.tobytes()
                if all_pcm:
                    pause_sec = args.pause * (3.0 if paragraph_break else 1.0)
                    all_pcm += make_silence_pcm(sample_rate, 1, 16, pause_sec)
                all_pcm += pcm
            except Exception as e:
                print(f"  エラー[{i+1}]: {e}")
                continue
        if sample_rate is None:
            return None

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(make_wav(sample_rate, 1, 16, all_pcm))
        tmp.close()
        return tmp.name

    def clipboard_watcher():
        """クリップボード変化を監視し、バックグラウンドで事前合成する"""
        last_text = pyperclip.paste()
        while not quit_flag.is_set():
            try:
                text = pyperclip.paste()
            except Exception:
                time.sleep(1)
                continue
            if text != last_text and _is_speech_like(text):
                last_text = text
                if _stream_eligible():
                    # [Windows/Irodori] ストリーミング可のときは重い事前合成をしない。
                    # 先読みが synth_lock を長時間握ると、手動再生時のストリーミングが
                    # 待たされ長文で無音待ちになる (807字で約30秒無音になっていた)。
                    # 押下時に都度ストリーミング(先頭~3秒)し、その結果をキャッシュするので
                    # 次回押下はフル機能のファイル再生 (シーク/一時停止可) になる。
                    if args.autoplay:
                        with playback_lock:
                            p = state["ffplay"]
                            idle = p is None or p.poll() is not None
                        if idle:
                            print(f"[自動再生] {len(text)}文字 (ストリーミング)", flush=True)
                            _play_streaming_irodori(text)
                else:
                    # 事前合成（従来経路: Ubuntu/Mac・SBV2・加工あり）
                    with cache["lock"]:
                        cache["text"] = text
                        cache["wav_path"] = None
                    # 合成は重い。synth_lock で再生スレッドと直列化 (二重合成を防ぐ)
                    with synth_lock:
                        # ロック待ちの間にクリップボードが更に変わっていたら先読みは捨てる
                        try:
                            cur = pyperclip.paste()
                        except Exception:
                            cur = text
                        if cur != text:
                            wav = None  # 既に次のテキストへ → 破棄 (再生側が拾う)
                        else:
                            try:
                                wav = _synthesize(text)
                            except Exception as e:
                                print(f"(事前合成エラー: {e})")
                                wav = None
                    # 合成中に別のテキストが来てないか確認
                    with cache["lock"]:
                        if cache["text"] == text:
                            cache["wav_path"] = wav
                            if wav:
                                print(f"[事前合成完了] {len(text)}文字")
                                # 自動再生: 何も再生中でなければ即再生
                                if args.autoplay:
                                    with playback_lock:
                                        p = state["ffplay"]
                                        if p is None or p.poll() is not None:
                                            _play_wav(wav)
                                            print("[自動再生]")
            else:
                last_text = text
            time.sleep(0.5)

    def play_clipboard():
        text = pyperclip.paste()
        if not text or not text.strip():
            print("(クリップボードが空)")
            return

        # キャッシュヒット判定 (先読み済みなら即再生)
        with cache["lock"]:
            wav = cache["wav_path"] if (cache["text"] == text and cache["wav_path"]) else None
        if wav:
            print(f"[即再生] {len(text)}文字 (先読み済み)", flush=True)
            _play_wav(wav)
            return

        # [Windows + Irodori] ストリーミング: 先頭チャンクが出来次第すぐ鳴らす
        # (先読み済みなら上のキャッシュヒットで既に返っている)。Ubuntu 等は下の従来経路。
        if _stream_eligible():
            print(f"[合成→即再生] {len(text)}文字 (ストリーミング)", flush=True)
            _play_streaming_irodori(text)
            return

        # ミス: これから合成する。押下が届いたことを「合成の前に」即表示する。
        # (合成は synth_lock 取得後に走るため、先読み中はロック解放まで無反応に
        #  見えて連打の原因になる。ここで先に一言出しておく。)
        print(f"[合成中] {len(text)}文字 …", flush=True)
        # 合成は synth_lock で直列化。ロック取得までに先読みが完了していれば再利用
        # (長文の二重合成=詰まりを防ぐ)。
        with synth_lock:
            with cache["lock"]:
                wav = cache["wav_path"] if cache["text"] == text else None
            if wav is None:
                wav = _synthesize(text)
            if not wav:
                print("(合成失敗)", flush=True)
                return
            with cache["lock"]:
                cache["text"] = text
                cache["wav_path"] = wav
        print(f"▶ 再生開始 ({len(text)}文字)", flush=True)
        _play_wav(wav)

    # 終了フラグ（ホットキーから触る）
    quit_flag = threading.Event()

    # 事前合成ウォッチャー起動
    if not args.no_prefetch:
        threading.Thread(target=clipboard_watcher, daemon=True).start()
        print("（クリップボード変化を監視して自動的に事前合成します）")

    def trigger_play():
        threading.Thread(target=play_clipboard, daemon=True).start()

    def trigger_stop():
        stop_playback()
        print("[停止]")

    def trigger_quit():
        stop_playback()
        print("終了")
        quit_flag.set()

    def trigger_pause():
        pause_toggle()

    def trigger_rewind():
        seek(-SEEK_STEP)

    def trigger_forward():
        seek(SEEK_STEP)

    # グローバルショートカット登録
    if hotkey_mode == "win":
        # Windows: RegisterHotKey API (専用スレッド)
        _hk_map = {
            1: (args.hotkey_play,    trigger_play),
            2: (args.hotkey_pause,   trigger_pause),
            3: (args.hotkey_rewind,  trigger_rewind),
            4: (args.hotkey_forward, trigger_forward),
            5: (args.hotkey_stop,    trigger_stop),
            6: (args.hotkey_quit,    trigger_quit),
        }
        _hk_thread = threading.Thread(target=_run_hotkey_loop, args=(_hk_map,), daemon=True)
        _hk_thread.start()
    elif hotkey_mode == "signal":
        # Linux/Mac: OS ショートカットから送られるシグナルで操作 (キー漏れなし)
        import signal
        _sig_actions = [
            (getattr(signal, "SIGUSR1", None), trigger_play),
            (getattr(signal, "SIGUSR2", None), trigger_stop),
            (getattr(signal, "SIGRTMIN", None), trigger_pause),
            (getattr(signal, "SIGRTMIN", 0) + 1 if hasattr(signal, "SIGRTMIN") else None,
             trigger_rewind),
            (getattr(signal, "SIGRTMIN", 0) + 2 if hasattr(signal, "SIGRTMIN") else None,
             trigger_forward),
        ]
        for _sig, _cb in _sig_actions:
            if _sig is None:
                continue
            # ハンドラは軽く: 別スレッドで実処理を回す (シグナル文脈を塞がない)
            def _make(cb):
                return lambda signum, frame: threading.Thread(target=cb, daemon=True).start()
            try:
                signal.signal(_sig, _make(_cb))
            except (OSError, ValueError):
                pass
        # SIGTERM/SIGINT (pkill やログアウト) でも綺麗に終了して VRAM を解放する。
        # 既定の SIGTERM は finally/atexit を通さず即死するため明示的に quit へ回す。
        def _sig_quit(signum, frame):
            quit_flag.set()
        for _qs in ("SIGTERM", "SIGINT"):
            _s = getattr(signal, _qs, None)
            if _s is not None:
                try:
                    signal.signal(_s, _sig_quit)
                except (OSError, ValueError):
                    pass

    # ターミナル内キー入力（Windows console + MinTTY/Git Bash + Linux に対応）
    def get_key():
        # cmd / PowerShell: msvcrt でノンブロッキング取得
        if IS_WINDOWS:
            try:
                import msvcrt
                if msvcrt.kbhit():
                    return msvcrt.getch().decode("utf-8", errors="ignore").lower()
            except Exception:
                pass
        # MinTTY（Git Bash）/ Linux: select フォールバック
        # ※Linux は下の cbreak 設定で1キー即時取得、MinTTY は p の後 Enter が必要
        try:
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                ch = sys.stdin.read(1)
                return ch.lower() if ch else None
        except Exception:
            pass
        return None

    # Linux/Mac: 端末を cbreak モードにして Enter 不要の1キー入力にする
    _termios_saved = None
    if not IS_WINDOWS and sys.stdin.isatty():
        try:
            import termios
            import tty
            _termios_saved = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            _termios_saved = None

    def _shutdown():
        """終了時の確実な後片付け: 再生停止 + ホットキー解除 + 端末復元 + モデル解放"""
        stop_playback()
        # RegisterHotKey スレッドに WM_QUIT を送って終了させる (Windows のみ)
        if IS_WINDOWS and hotkey_mode == "win" and _hotkey_thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(_hotkey_thread_id, _WM_QUIT, 0, 0)
            except Exception:
                pass
        # 端末モード復元 (Linux/Mac の cbreak)
        if _termios_saved is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _termios_saved)
            except Exception:
                pass
        # VRAM 解放。
        # SBV2(in-process): 自プロセスの torch キャッシュを解放。
        if model is not None:
            try:
                import gc
                import torch
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    try:
                        torch.cuda.ipc_collect()
                    except Exception:
                        pass
            except Exception:
                pass
        # Irodori(サーバ): VRAM を持つのはサーバ側なので、デーモンが起動したサーバを止める。
        # --stop-server-on-exit のときだけ (共有利用中の場合は付与されない)。
        # pkill は Windows に存在せず FileNotFoundError で握り潰され、サーバが
        # 残って VRAM を握りっぱなしになる。所有プロセスの PID/ハンドルで確実に止める。
        if use_irodori and getattr(args, "stop_server_on_exit", False):
            _sp = _owned.get("proc")
            try:
                if _sp is not None and hasattr(_sp, "poll") and _sp.poll() is None:
                    _sp.terminate()
                    if IS_WINDOWS:
                        # terminate が効かない場合の保険にツリーごと強制終了
                        subprocess.run(["taskkill", "/PID", str(_sp.pid), "/T", "/F"],
                                       timeout=5, check=False,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print("[VRAM解放] Irodori サーバを停止しました", file=sys.stderr)
                elif not IS_WINDOWS:
                    # ハンドルを失った場合のフォールバック (Unix のみ)
                    subprocess.run(["pkill", "-f", "[i]rodori_server.py"],
                                   timeout=5, check=False)
            except Exception:
                pass
        os._exit(0)

    try:
        while not quit_flag.is_set():
            key = get_key()
            if key == "p":
                trigger_play()
            elif key == " ":
                trigger_pause()
            elif key == "a":
                trigger_rewind()
            elif key == "d":
                trigger_forward()
            elif key == "s":
                trigger_stop()
            elif key == "q":
                trigger_quit()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n終了")
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
