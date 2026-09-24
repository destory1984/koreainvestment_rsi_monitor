"""로컬 TTS 서버 (Qwen3-TTS 1.7B CustomVoice). 모델을 한 번 올려 두고 요청마다 wav 를 돌려준다.

kis_alert.py 가 알림 문장을 이 서버에서 받아 tts_cache/ 에 쌓는다. 없어도 알림은 윈도우 음성으로 나간다.
NVIDIA GPU(VRAM 8GB 남짓)가 있어야 돈다. 모델(약 4.3GB)은 처음 켤 때 Hugging Face 에서 저절로 받는다 (키 필요 없음).

실행:  python tts_server.py [포트]     (기본 47650, 127.0.0.1 만 연다)
요청:  POST /tts  {"text": "...", "instruct": "...", "speaker": "Sohee", "seed": 42}  ->  16bit wav
       GET  /health
"""
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47650

model = Qwen3TTSModel.from_pretrained(MODEL_ID, device_map="cuda:0", dtype=torch.bfloat16)
lock = threading.Lock()          # 모델은 한 번에 하나씩만 돌린다


def synth(text: str, speaker: str, instruct: str, seed: int) -> bytes:
    with lock:
        torch.manual_seed(seed)
        wavs, sr = model.generate_custom_voice(text=text, speaker=speaker, language="Korean",
                                               instruct=instruct or None)
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/tts":
            return self._send(404, b"not found", "text/plain")
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            text = str(req["text"]).strip()
            if not text:
                raise ValueError("text 가 비었다")
            wav = synth(text, req.get("speaker", "Sohee"), req.get("instruct", ""), int(req.get("seed", 42)))
        except Exception as e:
            return self._send(400, str(e).encode("utf-8"), "text/plain; charset=utf-8")
        self._send(200, wav, "audio/wav")

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)


print(f"준비됨: http://127.0.0.1:{PORT}  ({MODEL_ID})", flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
