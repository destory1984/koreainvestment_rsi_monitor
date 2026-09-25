"""RSI 가 선을 넘으면 말로 알린다. webull_rsi_monitor(rsi_monitor.py)의 규칙과 목소리를 그대로 옮겼다.

경고선은 두 겹이다.
  바깥선 35 / 65 — 말머리 소리(땡) 뒤에 "테슬라 65 초과" 를 읽는다
  강한 선 30 / 70 — 세 음 경보 뒤에 "테슬라 70 초과" 를 읽는다

같은 선에서 64 → 66 → 64 → 66 처럼 오르내려도 한 번만 울리게 두 가지로 막는다.
  재무장  한 번 울리면 선에서 7 만큼 되돌아와야(65 알림이면 58 아래) 다시 울린다
  쿨다운  재무장이 되어도 같은 종류는 10분 안에 다시 울리지 않는다

목소리는 있는 것 가운데 가장 좋은 것을 쓴다. 한 번 만든 문장은 tts_cache/ 에 쌓고 그 뒤로는 파일만 튼다.
  1) 로컬 TTS 서버 (Qwen3-TTS 1.7B, 화자 Sohee, 127.0.0.1:47650) — GPU 가 있어 띄워 둔 사람만
  2) Edge 음성 (ko-KR-SunHiNeural) — 인터넷만 있으면 된다. 키도 돈도 필요 없다
  3) 윈도우 음성 (SAPI) → 파워셸 음성 — 아무것도 없을 때
캐시 파일 이름은 rsi_monitor.py 와 같은 규칙이라, 그쪽 tts_cache 를 가리키면 만들어 둔 소리를 같이 쓴다.
"""
import hashlib
import io
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# ── 경고선 ─────────────────────────────────────────────────────
LOWER, UPPER = 35.0, 65.0
STRONG_LOWER, STRONG_UPPER = 30.0, 70.0
SAY_STRONG = True             # 강한 선에서도 읽는다
REARM_MARGIN = 7.0
ALERT_COOLDOWN_SEC = 600

# ── 목소리 ─────────────────────────────────────────────────────
# 로컬 TTS 서버. 없으면 윈도우 음성(SAPI)으로 읽는다. 다른 곳에 띄웠으면 환경변수 KIS_TTS_URL 로 준다.
TTS_LOCAL_URL = os.environ.get("KIS_TTS_URL", "http://127.0.0.1:47650/tts")
TTS_LOCAL_SPEAKER = "Sohee"
TTS_LOCAL_SEED = 42
TTS_LOCAL_TIMEOUT = 60        # 첫 문장은 모델이 깨느라 오래 걸릴 수 있다
TTS_LOCAL_EMOTION = (("시작", "기쁘고 활기찬 목소리로, 또렷하게 말해 주세요."),)
TTS_TRIM_LEVEL = 0.01         # 이보다 작은 소리는 빈 자리로 본다
TTS_TRIM_KEEP = 0.06          # 잘라낸 뒤 앞뒤에 남길 초
TTS_EDGE_VOICE = "ko-KR-SunHiNeural"
TTS_EDGE_RATE = 20            # Edge 빠르기(%). +30 은 빨랐다
TTS_VOICE = "Heami"           # SAPI 로 물러날 때 고를 목소리
TTS_RATE = 6                  # SAPI 빠르기 -10 ~ 10
TTS_PITCH = 0                 # Edge 음높이, 한 칸이 5Hz
TTS_VOLUME = 0                # Edge 크기, 한 칸이 5%
# 시작 인사. kis_settings.json 의 "greeting" / "greeting_again" 으로 바꾼다 (빈 문자열이면 인사 없음)
TTS_GREETING = "모니터링을 시작합니다."
TTS_GREETING_AGAIN = "모니터링을 다시 시작합니다."

CHIME_WAV = r"C:\Windows\Media\Speech On.wav"
BEEP_TONES = {"short": ((1175, 90),), "full": ((880, 180), (1175, 180), (880, 180))}

# 읽기 어려운 티커는 한 낱말로 정해 둔다. 없으면 철자대로 끊어 읽는다.
TTS_SAY_AS = {
    "TSLA": "테슬라", "DRAM": "디램", "BE": "블룸에너지", "FCEL": "에프-셀", "GEV": "지 E 브이",
    "INTC": "인텔", "KORU": "코루", "LITE": "루멘텀", "POET": "포엣", "SNDK": "샌디",
    "SOXL": "속슬", "NVDA": "엔비디아", "MU": "마이크론",
}
_LETTER_KO = {
    "A": "에이", "B": "비", "C": "씨", "D": "디", "E": "이", "F": "에프", "G": "지",
    "H": "에이치", "I": "아이", "J": "제이", "K": "케이", "L": "엘", "M": "엠", "N": "엔",
    "O": "오", "P": "피", "Q": "큐", "R": "알", "S": "에스", "T": "티", "U": "유", "V": "브이",
    "W": "더블유", "X": "엑스", "Y": "와이", "Z": "제트",
}


def _num(v):
    return f"{v:g}"


def spell(name, symb=""):
    """읽을 이름. 국내 종목은 이름('SK하이닉스' → '에스케이하이닉스'), 미국은 정해 둔 것이나 철자."""
    said = TTS_SAY_AS.get(symb.upper()) or TTS_SAY_AS.get(name.upper())
    if said:
        return said
    if re.search(r"[가-힣]", name):
        return re.sub(r"[A-Za-z]", lambda m: _LETTER_KO[m.group().upper()], name)
    out = [_LETTER_KO.get(c.upper(), c) for c in name if c.isalnum()]
    return " ".join(out) or name


def say_breach(name, symb, above, edge):
    return f"{spell(name, symb)} {_num(edge)} {'초과' if above else '미만'}"


def say_signal(name, symb, side):
    return f"{spell(name, symb)} {'매수' if side == 'buy' else '매도'} 시그널"


def phrases(name, symb, lines=None):
    """이 종목으로 읽을 수 있는 문장 전부. 미리 만들어 둘 때 쓴다."""
    ln = lines or DEFAULT_LINES
    out = [say_breach(name, symb, True, ln.upper), say_breach(name, symb, False, ln.lower),
           say_signal(name, symb, "buy"), say_signal(name, symb, "sell")]
    if SAY_STRONG:
        out += [say_breach(name, symb, True, ln.strong_upper), say_breach(name, symb, False, ln.strong_lower)]
    return out


# ── 알림 판정 ──────────────────────────────────────────────────
RANK = {"": 0, "warn": 1, "strong": 2}


class Lines(NamedTuple):
    """RSI 선 넷. 종목마다 다르게 둘 수 있다 (웹 설정 "lines"). 기본은 30·35·65·70."""
    strong_lower: float
    lower: float
    upper: float
    strong_upper: float

    @classmethod
    def parse(cls, values):
        """[30, 35, 65, 70] 이나 [35, 65](강한 선은 5 바깥) 를 받아 Lines. 차례가 틀리면 ValueError."""
        v = [float(x) for x in values]
        if len(v) == 2:
            v = [v[0] - 5, v[0], v[1], v[1] + 5]
        if len(v) != 4 or not (0 <= v[0] < v[1] < v[2] < v[3] <= 100):
            raise ValueError("선은 0 ≤ 강한 아래 < 아래 < 위 < 강한 위 ≤ 100 이어야 한다")
        return cls(*v)


DEFAULT_LINES = Lines(STRONG_LOWER, LOWER, UPPER, STRONG_UPPER)


def level_of(v, lines=None):
    ln = lines or DEFAULT_LINES
    if v > ln.strong_upper:
        return "above", "strong"
    if v > ln.upper:
        return "above", "warn"
    if v < ln.strong_lower:
        return "below", "strong"
    if v < ln.lower:
        return "below", "warn"
    return "neutral", ""


def edge_of(zone, strength, lines=None):
    ln = lines or DEFAULT_LINES
    if zone == "above":
        return ln.strong_upper if strength == "strong" else ln.upper
    return ln.strong_lower if strength == "strong" else ln.lower


class Gate:
    """종목 하나의 알림 상태. rsi_monitor.py 의 Target 알림 부분과 같다."""

    def __init__(self, lines=None):
        self.lines = ln = lines or DEFAULT_LINES
        self.EDGES = {"above": (ln.upper, True), "below": (ln.lower, False),
                      "above_strong": (ln.strong_upper, True), "below_strong": (ln.strong_lower, False)}
        self.level = ("unknown", "")
        self.last_alert = {}
        self.armed = {}

    def check(self, v, now=None):
        """새 RSI 를 보고 알릴 것이 있으면 dict, 없으면 None. 억제된 것은 suppressed 에 이유를 단다.
        now 는 쿨다운을 셀 시각(초). 되감기에서 봉 시각을 넘긴다. 없으면 지금."""
        now = time.time() if now is None else now
        zone, strength = level_of(v, self.lines)
        prev_zone, prev_str = self.level
        self.level = (zone, strength)
        out = None
        if zone != "neutral" and (prev_zone in ("unknown",) or prev_zone != zone
                                  or RANK[strength] > RANK[prev_str]):
            kind = zone if strength == "warn" else f"{zone}_strong"
            out = {"kind": kind, "zone": zone, "strength": strength,
                   "edge": edge_of(zone, strength, self.lines), "start": prev_zone == "unknown"}
            why = self._gate(kind, now)
            if why:
                out["suppressed"] = why
            else:
                self.last_alert[kind] = now
                self.armed[kind] = False
        self._rearm(v)
        return out

    def _gate(self, kind, now):
        if not self.armed.get(kind, True):
            edge, upper = self.EDGES[kind]
            back = edge - REARM_MARGIN if upper else edge + REARM_MARGIN
            return f"{_num(back)} {'이하로 내려와야' if upper else '이상으로 올라와야'} 재무장"
        left = ALERT_COOLDOWN_SEC - (now - self.last_alert.get(kind, 0.0))
        if ALERT_COOLDOWN_SEC and left > 0:
            return f"쿨다운 {left / 60:.1f}분 남음"
        return ""

    def _rearm(self, v):
        for kind, (edge, upper) in self.EDGES.items():
            if (upper and v <= edge - REARM_MARGIN) or (not upper and v >= edge + REARM_MARGIN):
                self.armed[kind] = True


# ── 소리 ──────────────────────────────────────────────────────
def _instruct(text):
    for word, ins in TTS_LOCAL_EMOTION:
        if word in text:
            return ins
    return ""


def trim_wav(data):
    """WAV 앞뒤 빈 자리를 잘라낸다. 못 하겠으면 받은 그대로."""
    try:
        import numpy as np
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return data
            rate, frames = w.getframerate(), w.readframes(w.getnframes())
        x = np.frombuffer(frames, dtype="<i2")
        loud = np.flatnonzero(np.abs(x.astype(np.float32) / 32768) > TTS_TRIM_LEVEL)
        if loud.size == 0:
            return data
        keep = int(TTS_TRIM_KEEP * rate)
        a, b = max(0, int(loud[0]) - keep), min(len(x), int(loud[-1]) + 1 + keep)
        out = io.BytesIO()
        with wave.open(out, "wb") as w2:
            w2.setnchannels(1)
            w2.setsampwidth(2)
            w2.setframerate(rate)
            w2.writeframes(x[a:b].tobytes())
        return out.getvalue()
    except Exception:
        return data


def _mci(cmd):
    import ctypes
    buf = ctypes.create_unicode_buffer(512)
    rc = ctypes.windll.winmm.mciSendStringW(cmd, buf, 510, None)
    if rc:
        err = ctypes.create_unicode_buffer(512)
        ctypes.windll.winmm.mciGetErrorStringW(rc, err, 510)
        raise RuntimeError(f"MCI {rc}: {err.value or cmd}")
    return buf.value


def play_wav(path):
    """wav·mp3 를 끝까지 틀고 돌아온다 (알림은 한 줄로 세워 하나씩 트니 기다려도 된다)."""
    alias = "kistts"
    try:
        _mci(f"close {alias}")
    except Exception:
        pass
    kind = "waveaudio" if str(path).lower().endswith(".wav") else "mpegvideo"
    _mci(f'open "{os.path.abspath(path)}" type {kind} alias {alias}')
    _mci(f"play {alias} wait")
    try:
        _mci(f"close {alias}")
    except Exception:
        pass


def play_chime(tone):
    try:
        import winsound
    except Exception:
        return
    if tone == "short" and os.path.isfile(CHIME_WAV):
        try:
            winsound.PlaySound(CHIME_WAV, winsound.SND_FILENAME)
            return
        except Exception:
            pass
    try:
        for f, ms in BEEP_TONES.get(tone, BEEP_TONES["full"]):
            winsound.Beep(f, ms)
    except Exception:
        pass


class Voice:
    """문장을 wav 로 만들어 두고 튼다. 알림은 줄을 세워 하나씩 — 겹치면 안 들린다."""

    def __init__(self, cache_dir):
        self.dir = Path(cache_dir)
        self.q = queue.Queue()
        self.last_path = ""       # 마지막으로 읽은 경로 ("local" / "sapi" / "powershell" / "")
        self.last_error = ""
        threading.Thread(target=self._worker, daemon=True).start()

    # 캐시 — rsi_monitor.tts_path 와 같은 열쇠
    def path(self, text, engine="local", lang="ko"):
        if engine == "local":
            sig = f"local|{TTS_LOCAL_SPEAKER}|{TTS_LOCAL_SEED}|{_instruct(text)}"
            sig += f"|trim{TTS_TRIM_LEVEL}:{TTS_TRIM_KEEP}"
            ext = "wav"
        else:
            sig, ext = f"edge|{TTS_EDGE_VOICE}|{TTS_EDGE_RATE}%|{TTS_PITCH}|{TTS_VOLUME}", "mp3"
        key = hashlib.blake2b(f"{lang}|{sig}|{text}".encode("utf-8"), digest_size=8).hexdigest()
        return self.dir / f"{lang}_{key}.{ext}"

    def cached_path(self, text):
        """만들어 둔 파일. 로컬 목소리를 앞세운다. 없으면 None."""
        for engine in ("local", "edge"):
            p = self.path(text, engine)
            if p.is_file() and p.stat().st_size > 0:
                return p
        return None

    def cached(self, text):
        return self.cached_path(text) is not None

    @staticmethod
    def edge_ok():
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False

    def engine(self):
        """지금 새 문장을 만들 통로. 'local' / 'edge' / '' (못 만듦 → 윈도우 음성)."""
        return "local" if self.server_up() else "edge" if self.edge_ok() else ""

    def make(self, text):
        """캐시에 있으면 그것, 없으면 로컬 → Edge 순서로 만들어 둔다."""
        p = self.cached_path(text)
        if p:
            return p
        errors = []
        for engine, save in (("local", self._local_save), ("edge", self._edge_save)):
            p = self.path(text, engine)
            tmp = p.with_name(p.name + ".part")   # 받다 끊긴 파일이 캐시에 남지 않게
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                save(text, tmp)
                if not tmp.is_file() or tmp.stat().st_size == 0:
                    raise RuntimeError("받은 소리가 비었다")
                os.replace(tmp, p)
                return p
            except Exception as e:
                tmp.unlink(missing_ok=True)
                errors.append(f"{engine}: {e}")
        raise RuntimeError(" / ".join(errors))

    def _edge_save(self, text, tmp):
        import asyncio
        import edge_tts
        c = edge_tts.Communicate(text, TTS_EDGE_VOICE, rate=f"{TTS_EDGE_RATE:+d}%",
                                 pitch=f"{TTS_PITCH * 5:+d}Hz", volume=f"{TTS_VOLUME * 5:+d}%")
        asyncio.run(c.save(str(tmp)))

    def _local_save(self, text, tmp):
        body = {"text": text, "speaker": TTS_LOCAL_SPEAKER, "seed": TTS_LOCAL_SEED,
                "instruct": _instruct(text)}
        req = urllib.request.Request(TTS_LOCAL_URL, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TTS_LOCAL_TIMEOUT) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"로컬 TTS HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"로컬 TTS 서버에 닿지 않는다 ({TTS_LOCAL_URL})") from None
        tmp.write_bytes(trim_wav(data))

    def server_up(self):
        try:
            with urllib.request.urlopen(TTS_LOCAL_URL.rsplit("/", 1)[0] + "/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def prefetch(self, texts, on_done=None):
        """없는 문장을 뒤에서 미리 만든다. 만들 통로가 없으면 그냥 둔다(울릴 때 윈도우 음성으로 읽힌다)."""
        if not self.engine():
            return False

        def run():
            missing = [t for t in dict.fromkeys(texts) if not self.cached(t)]
            made = 0
            for t in missing:
                try:
                    self.make(t)
                    made += 1
                except Exception as e:
                    self.last_error = str(e)
                    break
            if on_done:
                on_done(made, len(missing))
        threading.Thread(target=run, daemon=True).start()
        return True

    # 읽기
    def say(self, text, tone=""):
        """줄에 세운다. tone 이 'short'/'full' 이면 말머리 소리를 먼저 낸다."""
        self.q.put((text, tone))

    def _worker(self):
        while True:
            text, tone = self.q.get()
            try:
                if tone:
                    play_chime(tone)
                if text:
                    self.last_path = self._speak(text)
            except Exception as e:
                self.last_error = str(e)

    def _speak(self, text):
        self.last_error = ""
        try:
            p = self.make(text)
            play_wav(p)
            return "edge" if p.suffix == ".mp3" else "local"
        except Exception as e:
            self.last_error = str(e)
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            v = win32com.client.Dispatch("SAPI.SpVoice")
            for i in range(v.GetVoices().Count):
                tok = v.GetVoices().Item(i)
                if TTS_VOICE.lower() in tok.GetDescription().lower():
                    v.Voice = tok
                    break
            v.Rate = TTS_RATE
            v.Speak(text, 0)
            return "sapi"
        except Exception as e:
            self.last_error += f" / SAPI: {e}"
        try:
            import base64
            import subprocess
            q = text.replace("'", "''")
            script = ("Add-Type -AssemblyName System.Speech;"
                      "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                      f"$s.Rate={TTS_RATE};$s.Speak('{q}')")
            enc = base64.b64encode(script.encode("utf-16-le")).decode()
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                           timeout=30, creationflags=0x08000000,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "powershell"
        except Exception as e:
            self.last_error += f" / 파워셸: {e}"
            return ""

    def greet(self, first_text=None, again_text=None):
        """시작 인사. 그날 처음이면 긴 인사, 같은 날 다시 켜면 짧은 인사."""
        first_text = TTS_GREETING if first_text is None else first_text
        again_text = TTS_GREETING_AGAIN if again_text is None else again_text
        mark = self.dir / "greeted_kis.txt"
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            first = mark.read_text(encoding="utf-8").strip() != today
        except Exception:
            first = True
        text = first_text if first else again_text
        if text:
            self.say(text)
        if first:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                mark.write_text(today, encoding="utf-8")
            except Exception:
                pass
