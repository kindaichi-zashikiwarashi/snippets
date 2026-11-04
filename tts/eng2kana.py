"""
英単語→カタカナ変換モジュール
alkana（約7万語辞書）をベースに、dictionary.csv の手動辞書で補正する
"""

import csv
import re
from pathlib import Path

# alkana は英単語→カナのフォールバック辞書。未インストールでも dictionary.csv だけは
# 使えるよう、モジュール読み込み時ではなく実際に使う直前に遅延 import する
# (Irodori モードを system python3 で動かす環境などで dictionary が死なないように)。

_DICT_CSV = Path(__file__).parent / "dictionary.csv"


def load_dictionary_csv(path: Path = _DICT_CSV) -> dict:
    """dictionary.csv を読み込んで TTS用の「英語/漢字 → 読み」マップを返す

    CSVは `reading,written` 形式。TTSは右列→左列（written→reading）で使う。
    e.g. 「Slack」を「スラック」と読ませる。
    """
    result = {}
    if not path.exists():
        return result
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or not row[0] or row[0].startswith("#"):
                continue
            if row[0].strip().lower() == "reading":  # ヘッダー行
                continue
            if len(row) >= 2 and row[1].strip():
                # written (col2) → reading (col1)
                # 同じwrittenが複数行ある場合は最初の登録（=正規読み）を採用。
                # 後続行はSTT用の誤認識補正のために存在することがあるので無視する。
                written = row[1].strip()
                if written not in result:
                    result[written] = row[0].strip()
    return result


_LEGACY_OVERRIDE = {
    # IT用語
    "setup": "セットアップ",
    "keyboard": "キーボード",
    "screenshot": "スクリーンショット",
    "download": "ダウンロード",
    "upload": "アップロード",
    "update": "アップデート",
    "package": "パッケージ",
    "debug": "デバッグ",
    "electron": "エレクトロン",
    "electronmon": "エレクトロンモン",
    "webpack": "ウェブパック",
    "runtime": "ランタイム",
    "middleware": "ミドルウェア",
    "framework": "フレームワーク",
    "database": "データベース",
    "template": "テンプレート",
    "component": "コンポーネント",
    "container": "コンテナ",
    "instance": "インスタンス",
    "interface": "インターフェース",
    "pipeline": "パイプライン",
    "workflow": "ワークフロー",
    "endpoint": "エンドポイント",
    "payload": "ペイロード",
    "callback": "コールバック",
    "localhost": "ローカルホスト",
    "timeout": "タイムアウト",
    "rollback": "ロールバック",
    "deploy": "デプロイ",
    "release": "リリース",
    "plugin": "プラグイン",
    "widget": "ウィジェット",
    "layout": "レイアウト",
    "token": "トークン",
    "cache": "キャッシュ",
    "queue": "キュー",
    "batch": "バッチ",
    "cron": "クロン",
    "daemon": "デーモン",
    "proxy": "プロキシ",
    "router": "ルーター",
    "socket": "ソケット",
    "thread": "スレッド",
    "process": "プロセス",
    "async": "アシンク",
    "sync": "シンク",
    "mutex": "ミューテックス",
    "chunk": "チャンク",
    "stream": "ストリーム",
    "buffer": "バッファ",
    "parser": "パーサー",
    "linter": "リンター",
    "formatter": "フォーマッター",
    "compiler": "コンパイラ",
    "transpiler": "トランスパイラ",
    "bundler": "バンドラー",
    "loader": "ローダー",
    "handler": "ハンドラー",
    "listener": "リスナー",
    "resolver": "リゾルバー",
    "provider": "プロバイダー",
    "wrapper": "ラッパー",
    "helper": "ヘルパー",
    "utility": "ユーティリティ",
    "logger": "ロガー",
    "tracker": "トラッカー",
    "watcher": "ウォッチャー",
    "scheduler": "スケジューラー",
    "runner": "ランナー",
    "worker": "ワーカー",
    "manager": "マネージャー",
    "controller": "コントローラー",
    "service": "サービス",
    "gateway": "ゲートウェイ",
    "webhook": "ウェブフック",
    "sandbox": "サンドボックス",
    "worktree": "ワークツリー",
    "checkout": "チェックアウト",
    "rebase": "リベース",
    "stash": "スタッシュ",
    "fetch": "フェッチ",
    "upstream": "アップストリーム",
    "downstream": "ダウンストリーム",
    "clone": "クローン",
    "compose": "コンポーズ",
    "listen": "リッスン",
    "performance": "パフォーマンス",
    "refactor": "リファクタ",
    "feature": "フィーチャー",
    "hotfix": "ホットフィックス",
    "bugfix": "バグフィックス",
    "vendor": "ベンダー",
    "module": "モジュール",
    "cowork": "コワーク",
    # ツール名
    "npm": "エヌピーエム",
    "git": "ギット",
    "docker": "ドッカー",
    "node": "ノード",
    "vscode": "ブイエスコード",
    "github": "ギットハブ",
    "gitlab": "ギットラブ",
    "slack": "スラック",
    "chrome": "クローム",
    "firefox": "ファイアフォックス",
    "ubuntu": "ウブントゥ",
    "linux": "リナックス",
    "windows": "ウィンドウズ",
    "pytorch": "パイトーチ",
    "numpy": "ナンパイ",
    "pandas": "パンダス",
    "gradio": "グラディオ",
    "fastapi": "ファストエーピーアイ",
    "uvicorn": "ユニコーン",
    # 略語
    "IDE": "アイディーイー",
    "API": "エーピーアイ",
    "URL": "ユーアールエル",
    "URI": "ユーアールアイ",
    "CLI": "シーエルアイ",
    "GUI": "ジーユーアイ",
    "LLM": "エルエルエム",
    "GPU": "ジーピーユー",
    "CPU": "シーピーユー",
    "RAM": "ラム",
    "VRAM": "ブイラム",
    "CUDA": "クーダ",
    "SSD": "エスエスディー",
    "HDD": "エイチディーディー",
    "SSH": "エスエスエイチ",
    "SSL": "エスエスエル",
    "TLS": "ティーエルエス",
    "HTTP": "エイチティーティーピー",
    "HTTPS": "エイチティーティーピーエス",
    "REST": "レスト",
    "JSON": "ジェイソン",
    "YAML": "ヤメル",
    "XML": "エックスエムエル",
    "CSV": "シーエスブイ",
    "SQL": "エスキューエル",
    "HTML": "エイチティーエムエル",
    "CSS": "シーエスエス",
    "PDF": "ピーディーエフ",
    "WAV": "ウェーブ",
    "PNG": "ピング",
    "JPG": "ジェイペグ",
    "JPEG": "ジェイペグ",
    "GIF": "ジフ",
    "SVG": "エスブイジー",
    "TTS": "ティーティーエス",
    "STT": "エスティーティー",
    "NLP": "エヌエルピー",
    "AI": "エーアイ",
    "ML": "エムエル",
    "CI": "シーアイ",
    "CD": "シーディー",
    "PR": "ピーアール",
    "OS": "オーエス",
    "IP": "アイピー",
    "DNS": "ディーエヌエス",
    "TCP": "ティーシーピー",
    "UDP": "ユーディーピー",
    "CVE": "シーブイイー",
    "OOM": "オーオーエム",
    "EOF": "イーオーエフ",
    "NULL": "ヌル",
    "README": "リードミー",
    "CHANGELOG": "チェンジログ",
    "TODO": "トゥードゥー",
    "FIXME": "フィックスミー",
    "HACK": "ハック",
    # 一般的な英単語（alkanaが変換できない/不自然なもの）
    "file": "ファイル",
    "folder": "フォルダ",
    "directory": "ディレクトリ",
    "path": "パス",
    "root": "ルート",
    "home": "ホーム",
    "user": "ユーザー",
    "admin": "アドミン",
    "owner": "オーナー",
    "role": "ロール",
    "scope": "スコープ",
    "status": "ステータス",
    "error": "エラー",
    "warning": "ウォーニング",
    "success": "サクセス",
    "failed": "フェイルド",
    "pending": "ペンディング",
    "active": "アクティブ",
    "disabled": "ディセーブルド",
    "enabled": "イネーブルド",
    "true": "トゥルー",
    "false": "フォールス",
    "null": "ヌル",
}

# CSVから読み込み。失敗したらレガシー辞書にフォールバック
OVERRIDE = load_dictionary_csv() or _LEGACY_OVERRIDE

# 小文字正規化した辞書を事前作成
_OVERRIDE_LOWER = {k.lower(): v for k, v in OVERRIDE.items()}


def english_to_kana(word: str, dict_only: bool = False) -> str | None:
    """
    英単語をカタカナに変換する。
    1. 手動辞書（OVERRIDE=dictionary.csv）を最優先
    2. dict_only=False のときのみ alkana（約7万語辞書）にフォールバック
    3. どちらにもなければNoneを返す

    dict_only=True: dictionary.csv に載っている語だけ変換し、それ以外は None
    （Irodori 等ネイティブに英語を読めるエンジンで、カスタム読みだけ効かせたい用途）。
    """
    lower = word.lower()

    # 手動辞書
    if lower in _OVERRIDE_LOWER:
        return _OVERRIDE_LOWER[lower]

    # dict_only のときは alkana に頼らない（辞書外の英単語はそのまま）
    if dict_only:
        return None

    # alkana（未インストールなら諦めて None。辞書だけは上で効いている）
    try:
        import alkana
    except ImportError:
        return None
    kana = alkana.get_kana(lower)
    if kana:
        return kana

    return None


def _split_camelcase(word: str) -> list[str]:
    """CamelCase/PascalCaseを単語境界で分割する。
    例: ResponseExample → ['Response', 'Example']
        XMLParser → ['XML', 'Parser']
        getUserName → ['get', 'User', 'Name']
    """
    return re.split(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", word)


def _convert_compound(word: str, dict_only: bool = False) -> str | None:
    """CamelCaseを分割して各パーツを辞書引き。すべて変換できれば結合して返す"""
    parts = _split_camelcase(word)
    if len(parts) < 2:
        return None
    converted = []
    for p in parts:
        kana = english_to_kana(p, dict_only=dict_only)
        if not kana:
            return None  # 一つでも変換できなければ諦める
        converted.append(kana)
    return "".join(converted)


def replace_english_words(text: str, dict_only: bool = False) -> str:
    """テキスト中の英単語をカタカナに変換する。
    dict_only=True のときは dictionary.csv に載っている語だけ変換し、
    それ以外の英単語はそのまま残す（Irodori 等ネイティブに英語を読めるエンジン向け）。"""
    def _replace(match):
        word = match.group(0)
        # 末尾に数字を含む単語（OAuth2, Python3等）を考慮
        m = re.match(r"^([A-Za-z]+)(\d+)$", word)
        if m:
            # 全体を辞書に問い合わせ（OAuth2→オーオースツー）
            kana = english_to_kana(word, dict_only=dict_only)
            if kana:
                return kana
            # CamelCase分割（GPT4等は通常 "GPT" + "4"）
            base = m.group(1)
            base_kana = english_to_kana(base, dict_only=dict_only) or _convert_compound(base, dict_only=dict_only)
            if base_kana:
                return base_kana + m.group(2)
            return word
        # 通常の単語
        kana = english_to_kana(word, dict_only=dict_only)
        if kana:
            return kana
        # CamelCase分割（ResponseExample→レスポンスエグザンプル等）
        compound = _convert_compound(word, dict_only=dict_only)
        if compound:
            return compound
        return word

    # 英単語を検出（2文字以上、内側に数字を含むものも対象: B2B, OAuth2, GPT4等）
    # \b は日本語境界で動かないので、前後を明示的にチェック
    return re.sub(r"(?<![A-Za-z\d])[A-Za-z][A-Za-z\d]+(?![A-Za-z\d])", _replace, text)


# 辞書の「漢字/非ASCIIキー」だけを抽出（長いキーから順にソート）
_JP_OVERRIDE_KEYS = sorted(
    [k for k in OVERRIDE.keys() if not (k.isascii() and k.replace("_", "").isalnum())],
    key=len, reverse=True,
)


def replace_japanese_readings(text: str) -> str:
    """辞書で漢字→読み（ひらがな/カナ）の置換を行う。
    TTSが読み違えやすい固有名詞を事前変換する。
    """
    for key in _JP_OVERRIDE_KEYS:
        text = text.replace(key, OVERRIDE[key])
    return text


def remove_unreadable(text: str) -> str:
    """読み上げに不適切なトークンを句読点に置換し、間を取る"""
    P = "。"  # 長めの間（コードブロック、URL等の大きな塊）
    p = "、"  # 短い間（変数名、バージョン番号等の小さなトークン）
    # Unicode罫線文字（box-drawing block U+2500-U+257F）
    # 3文字以上連続する罫線（表の枠線）→ 改行（段落区切りとして扱う）
    text = re.sub(r"[─-╿]{3,}", "\n", text)
    # 個別の罫線文字（│等の縦線・角）→ スペース
    text = re.sub(r"[─-╿]", " ", text)
    # 記号マーカー（★☆◆◇■□●○◎▲▼等）→ 除去
    text = re.sub(r"[★☆◆◇■□●○◎▲△▼▽►◄▶◀⬛⬜]", "", text)
    # URL
    text = re.sub(r"https?://\S+", P, text)
    # メールアドレス
    text = re.sub(r"[\w.\-]+@[\w.\-]+\.\w+", p, text)
    # ファイルパス（Windows: C:\... / Unix: /path/to/...）
    text = re.sub(r"[A-Za-z]:\\[\w\\.\-/]+", p, text)
    text = re.sub(r"(?<!\w)[/~][\w/.\-]{3,}", p, text)
    # コマンドライン（npm run dev, git push 等）— 日本語文字の手前で止める
    text = re.sub(r"(?:npm|docker|git|pip|python|python3|node|cd|ls|mkdir|curl|source|apt|sudo|bash|sh|cat|echo|grep|chmod|chown|kill|rm|cp|mv|tar|wget|ssh|scp)(?:\s+[a-zA-Z0-9_\-./\\:=<>]+)+", P, text)
    # コードブロック（Markdown）
    text = re.sub(r"```[\s\S]*?```", P, text)
    # インラインコード（バッククォート内）
    text = re.sub(r"`[^`]+`", p, text)
    # バージョン番号（v2.3.1, 1.0.0, ==4.44.2 等）
    text = re.sub(r"[v=<>!]*\d+\.\d+[\.\d]*\S*", p, text)
    # ハッシュ値（7ef17c49, aa56d4f2 等の16進数っぽい文字列）
    text = re.sub(r"\b[0-9a-f]{7,}\b", p, text)
    # ファイル拡張子つきトークン（foo.js, bar.py 等）
    text = re.sub(r"\b\w+\.(js|ts|py|rb|go|rs|java|c|cpp|h|hpp|css|html|htm|md|txt|json|yaml|yml|toml|xml|csv|sql|sh|bat|cmd|log|cfg|ini|env|lock|wav|mp3|png|jpg|gif|svg|pdf|zip|tar|gz)\b", p, text)
    # snake_case / SCREAMING_SNAKE_CASE の変数名（2つ以上の_区切り）
    text = re.sub(r"\b[a-zA-Z]+(?:_[a-zA-Z0-9]+){2,}\b", p, text)
    # camelCase / PascalCase の長い識別子（大文字小文字混在で15文字以上）
    text = re.sub(r"\b[a-z]+(?:[A-Z][a-z]+){3,}\w*\b", p, text)
    # 記号の羅列（--option, -flag, >=, <=, ==, != 等）
    # ASCII英数字＋ハイフン/アンダースコアに限定（日本語は\wに含まれるので除外）
    text = re.sub(r"--?[A-Za-z0-9_\-]+", p, text)
    text = re.sub(r"[<>=!]{2,}", p, text)
    # 括弧内が英数字・記号だけのもの（(exit code 1), [WinError 10061] 等）
    text = re.sub(r"\([a-zA-Z0-9_\s\-:./\\]+\)", p, text)
    text = re.sub(r"\[[a-zA-Z0-9_\s\-:./\\]+\]", p, text)
    # 数字は読み上げ対象として残す（"5章"の5を読みたい等）
    return text


def add_pauses(text: str) -> str:
    """スペース・タブ・改行などの区切りを読点に変換し、TTSが一拍置くようにする"""
    # 〜～ → 「から」（範囲記号として読み上げる）
    text = re.sub(r"[〜～]", "から", text)
    # 矢印記号 → 読点（「A→B」を「A、B」として読む）
    text = re.sub(r"[→←⇒⇐⇔↑↓↗↘↙↖⬆⬇⬅➡]", "、", text)
    # 中黒（・）→ 読点（軽く一拍）
    text = text.replace("・", "、")
    # 各種括弧 → 読点（軽く一拍）
    text = re.sub(r"[()（）「」『』【】〔〕\[\]{}｛｝]", "、", text)
    # ASCII !? を全角 ！？ に揃える（文末判定で使う）
    text = text.replace("!", "！").replace("?", "？")
    # タブ → 読点（改行は最後にマーカー化する）
    text = re.sub(r"\t+", "、", text)
    # 連続スペース（2つ以上）→ 読点
    text = re.sub(r" {2,}", "、", text)
    # 単一スペース（前後が日本語 or カタカナ）→ 読点
    # 例: 「ファイル パス」→「ファイル、パス」
    text = re.sub(r"(?<=[\u3040-\u30FF\u4E00-\u9FFF]) (?=[\u3040-\u30FF\u4E00-\u9FFF])", "、", text)
    # 英単語変換後のカタカナ間のスペース → 読点
    text = re.sub(r"(?<=[\u30A0-\u30FF]) (?=[\u30A0-\u30FF])", "、", text)
    # 残った単一スペース（カタカナ⇔ひらがな/漢字、英数⇔日本語等の境界）も軽く一拍
    text = re.sub(r"(?<=\S) (?=\S)", "、", text)
    # 句読点の連続を整理（改行マーカー処理前に実施）
    text = re.sub(r"[、,]+", "、", text)
    # 改行 → 句点 + 段落区切りマーカー（合成側で長い無音に変換）
    text = re.sub(r"\n+", f"。{PARAGRAPH_BREAK}", text)
    # 句点の連続整理（マーカーは含まないので影響なし）
    text = re.sub(r"。+", "。", text)
    text = re.sub(r"、。", "。", text)
    text = re.sub(r"。、", "。", text)
    # 文頭・文末の句読点・空白除去（マーカー\x07は除外）
    text = re.sub(r"^[、。 \t]+", "", text)
    text = re.sub(r"[、。 \t]+$", "", text)
    return text


# 段落区切りマーカー（改行を句点よりさらに長い間として扱う内部マーカー）
PARAGRAPH_BREAK = "\x07"
