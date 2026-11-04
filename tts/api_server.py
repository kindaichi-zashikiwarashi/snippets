"""
TTS前処理付きAPIサーバー
Style-Bert-VITS2のラッパーとして動作し、英単語変換・不要テキスト除去を自動で行う

Usage: python api_server.py [--port 5100]

エンドポイント:
  PUT /tts          テキスト→WAV音声を返す
  PUT /tts/preview  テキスト→前処理後テキストを返す（音声生成しない）
"""

import re
import json
import argparse
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

from eng2kana import remove_unreadable, replace_english_words, add_pauses

SBV2_URL = "http://127.0.0.1:5000/voice"
DEFAULT_MODEL_ID = 0  # amitaro


def preprocess(text: str) -> str:
    """TTS用にテキストを前処理する"""
    text = remove_unreadable(text)
    text = replace_english_words(text)
    # 変換できなかった英数字トークンを除去
    text = re.sub(r"\b[a-zA-Z0-9_.\-/\\]{2,}\b", "", text)
    text = add_pauses(text)
    # 日本語が含まれない場合はスキップ
    if not re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", text):
        return ""
    return text


def synthesize(text: str, model_id: int = DEFAULT_MODEL_ID) -> bytes:
    """Style-Bert-VITS2のAPIで音声合成"""
    # 100文字制限で分割
    chunks = []
    while len(text) > 100:
        # 句点か読点で区切る
        cut = max(text.rfind("。", 0, 100), text.rfind("、", 0, 100))
        if cut <= 0:
            cut = 100
        else:
            cut += 1
        chunks.append(text[:cut])
        text = text[cut:]
    if text:
        chunks.append(text)

    # 各チャンクを合成して結合（簡易実装: 最初のチャンクだけ返す）
    # 長文対応が必要な場合はWAV結合が必要だが、ここでは最初の100文字で返す
    target = chunks[0] if chunks else text
    params = urllib.parse.urlencode({
        "text": target,
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
    url = f"{SBV2_URL}?{params}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


class TTSHandler(BaseHTTPRequestHandler):
    def do_PUT(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""

        # JSONまたはプレーンテキスト対応
        try:
            data = json.loads(body)
            text = data.get("text", "")
            model_id = data.get("model_id", DEFAULT_MODEL_ID)
        except (json.JSONDecodeError, AttributeError):
            text = body
            model_id = DEFAULT_MODEL_ID

        if self.path == "/tts/preview":
            processed = preprocess(text)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            result = json.dumps({
                "original": text,
                "processed": processed,
            }, ensure_ascii=False)
            self.wfile.write(result.encode("utf-8"))
            return

        if self.path == "/tts":
            processed = preprocess(text)
            if not processed:
                self.send_response(204)
                self.end_headers()
                return
            try:
                audio = synthesize(processed, model_id=model_id)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("X-Processed-Text", urllib.parse.quote(processed))
                self.end_headers()
                self.wfile.write(audio)
            except Exception as e:
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        """POSTも受け付ける"""
        self.do_PUT()

    def log_message(self, format, *args):
        print(f"[TTS API] {args[0]}")


def main():
    parser = argparse.ArgumentParser(description="TTS前処理付きAPIサーバー")
    parser.add_argument("--port", type=int, default=5100, help="ポート番号（デフォルト: 5100）")
    args = parser.parse_args()

    server = HTTPServer(("0.0.0.0", args.port), TTSHandler)
    print(f"TTS API server: http://127.0.0.1:{args.port}")
    print(f"  PUT /tts          テキスト→WAV")
    print(f"  PUT /tts/preview  テキスト→前処理結果（JSON）")
    print(f"Backend: {SBV2_URL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
