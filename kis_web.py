"""RSI·MACD 를 웹 화면으로 보여준다.

    python kis_web.py                       # 저장해 둔 종목으로 http://localhost:8000
    python kis_web.py TSLA 005930           # 종목을 더해서 띄우기 (국내는 종목코드 여섯 자리)
    python kis_web.py --host 0.0.0.0        # 같은 공유기의 휴대폰에서도 보기

서버 하나가 한국투자증권 실시간 연결을 잡고, 브라우저 여러 개에 값을 나눠 준다.
종목은 화면에서 더하고 뺄 수 있고, kis_watchlist.json 에 저장된다.
RSI 가 35/65, 30/70 을 넘으면 서버가 말로 알린다 (kis_alert.py). 브라우저를 닫아도 알림은 계속 나간다.
"""
import argparse
import asyncio
import calendar
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import kis_alert as al
import kis_rsi as k

HERE = Path(__file__).parent
STATIC = HERE / "static"
WATCHLIST = HERE / "kis_watchlist.json"
SETTINGS = HERE / "kis_settings.json"
MAX_TICKERS = 40  # 실시간 연결 하나에 41개까지 구독된다


class Hub:
    def __init__(self, tickers, nmin, period, tts_cache):
        self.tickers, self.nmin, self.period = tickers, nmin, period
        self.books = {}
        self.gates = {}           # 종목 -> 알림 상태
        self.events = deque(maxlen=30)
        self.voice = al.Voice(tts_cache)
        try:
            self.settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            self.settings = {}
        self.settings.setdefault("sound", True)
        self.clients = set()
        self.dirty = set()
        self.status = "시작 중"
        self.ws = None            # 한국투자증권 실시간 연결 (열려 있을 때만)
        self.approval_key = None
        self.lock = asyncio.Lock()

    # ── 종목 목록 ─────────────────────────────────────────────
    def save(self):
        WATCHLIST.write_text(json.dumps(
            [f"{b.excd}:{b.symb}" for b in self.books.values()], indent=1), encoding="utf-8")

    def load(self):
        self.appkey, self.secret = k.load_keys()
        saved = json.loads(WATCHLIST.read_text(encoding="utf-8")) if WATCHLIST.exists() else []
        for t in saved + self.tickers:
            try:
                self._add(t)
            except Exception as e:
                print(f"{t}: 건너뜀 — {e}", flush=True)
        self.save()
        if not self.voice.server_up():
            print(f"로컬 TTS 서버({al.TTS_LOCAL_URL})가 없다 — 알림은 윈도우 음성으로 읽는다.", flush=True)
        if self.settings["sound"]:
            self.voice.greet(*self.greetings())
        self.prefetch(list(self.books.values()))

    def greetings(self):
        return (self.settings.get("greeting", al.TTS_GREETING),
                self.settings.get("greeting_again", al.TTS_GREETING_AGAIN))

    def prefetch(self, books):
        texts = [t for t in self.greetings() if t]
        for b in books:
            texts += al.phrases(b.name, b.symb)
        missing = sum(not self.voice.cached(t) for t in texts)
        if missing and self.voice.prefetch(texts, lambda made, n: print(
                f"알림 문장 {made}/{n}개 만듦" + ("" if made == n else f" — {self.voice.last_error}"),
                flush=True)):
            print(f"알림 문장 {missing}개를 로컬 TTS 로 만드는 중", flush=True)

    def _add(self, ticker):
        """종목을 찾아 분봉을 받는다 (블로킹). 이미 있으면 그 종목을 돌려준다."""
        excd, symb = k.resolve(self.appkey, self.secret, ticker)
        if symb in self.books:
            return self.books[symb], False
        if len(self.books) >= MAX_TICKERS:
            raise ValueError(f"종목은 {MAX_TICKERS}개까지다.")
        bars = k.fetch_bars(self.appkey, self.secret, excd, symb, self.nmin)
        if not bars:
            raise ValueError(f"{symb}: 분봉이 없다.")
        book = k.Book(excd, symb, bars, self.nmin)
        self.books[symb] = book
        self.gates[symb] = al.Gate()
        time.sleep(0.1)
        return book, True

    async def add(self, ticker):
        async with self.lock:
            book, new = await asyncio.to_thread(self._add, ticker)
            if not new:
                return book
            self.save()
            if self.ws:
                await k.subscribe(self.ws, self.approval_key, book.key)
        self.prefetch([book])
        await self.broadcast({"type": "add", "row": self.row(book)})
        return book

    async def remove(self, symb):
        async with self.lock:
            book = self.books.pop(symb, None)
            if not book:
                raise KeyError(symb)
            self.gates.pop(symb, None)
            self.save()
            self.dirty.discard(symb)
            if self.ws:
                try:
                    await k.subscribe(self.ws, self.approval_key, book.key, on=False)
                except Exception:
                    pass
        await self.broadcast({"type": "remove", "symb": symb})

    # ── 한국투자증권 쪽 ────────────────────────────────────────
    def reload_bars(self):
        """끊겼다 붙으면 그 사이 체결을 놓쳤으니 분봉을 새로 받는다."""
        for b in list(self.books.values()):
            b.bars = k.fetch_bars(self.appkey, self.secret, b.excd, b.symb, self.nmin)
            time.sleep(0.1)

    def on_tick(self, d):
        book = self.books.get(d["SYMB"])
        if book:
            book.on_tick(d)
            self.dirty.add(book.symb)

    def on_open(self, ws):
        self.ws = ws
        self.set_status("실시간")

    async def kis_loop(self):
        first = True
        while True:
            try:
                if not first:
                    self.set_status("분봉 다시 받는 중")
                    await asyncio.to_thread(self.reload_bars)
                    await self.broadcast({"type": "reload"})
                first = False
                self.set_status("연결 중")
                self.approval_key = await asyncio.to_thread(k.get_approval_key, self.appkey, self.secret)
                keys = [b.key for b in self.books.values()]
                await k.live(self.approval_key, keys, self.on_tick, self.on_open)
                self.ws = None
                self.set_status("연결 끊김 — 5초 뒤 다시")
                await asyncio.sleep(5)
            except k.KisInUse as e:
                self.ws = None
                self.set_status(str(e))
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ws = None
                self.set_status(f"연결 끊김 ({type(e).__name__}) — 5초 뒤 다시")
                await asyncio.sleep(5)

    # ── 알림 ──────────────────────────────────────────────────
    def check_alerts(self, symbs):
        for s in symbs:
            b, g = self.books.get(s), self.gates.get(s)
            if not b or not g:
                continue
            v = b.indicators(self.period)["rsi"]
            if v is None:
                continue
            a = g.check(v)
            if not a:
                continue
            said = al.say_breach(b.name, b.symb, a["zone"] == "above", a["edge"])
            ev = {"t": datetime.now().strftime("%H:%M:%S"), "symb": s, "name": b.name,
                  "rsi": v, "zone": a["zone"], "strength": a["strength"],
                  "text": f"{_num(a['edge'])} {'초과' if a['zone'] == 'above' else '미만'}"
                          + (" (시작 때부터)" if a["start"] else ""),
                  "suppressed": a.get("suppressed", "")}
            self.events.appendleft(ev)
            print(f"{ev['t']} 알림 {b.name} {ev['text']} RSI {v:.2f}"
                  + (f" — 억제 ({ev['suppressed']})" if ev["suppressed"] else ""), flush=True)
            if not ev["suppressed"] and self.settings["sound"]:
                strong = a["strength"] == "strong"
                self.voice.say(said if (not strong or al.SAY_STRONG) else "",
                               "full" if strong else "short")
            asyncio.get_event_loop().create_task(self.broadcast({"type": "alert", "event": ev}))

    def set_sound(self, on):
        self.settings["sound"] = bool(on)
        SETTINGS.write_text(json.dumps(self.settings), encoding="utf-8")

    # ── 브라우저 쪽 ───────────────────────────────────────────
    def set_status(self, s):
        self.status = s
        print(f"{datetime.now():%H:%M:%S} {s}", flush=True)
        asyncio.get_event_loop().create_task(self.broadcast({"type": "status", "status": s}))

    def row(self, b):
        ind = b.indicators(self.period)
        bar = b.bars[-1] if b.bars else None
        return {"symb": b.symb, "excd": b.excd, "name": b.name, "price": b.price, "rate": b.rate,
                "time": b.us_time, **ind,
                "bar": bar and {"time": epoch(bar["time_us"]), "open": bar["open"],
                                "high": bar["high"], "low": bar["low"], "close": bar["close"]}}

    def state(self):
        return {"status": self.status, "nmin": self.nmin, "period": self.period,
                "lower": al.LOWER, "upper": al.UPPER,
                "strong_lower": al.STRONG_LOWER, "strong_upper": al.STRONG_UPPER,
                "max": MAX_TICKERS, "sound": self.settings["sound"],
                "events": list(self.events),
                "rows": [self.row(b) for b in self.books.values()]}

    def chart(self, symb):
        b = self.books.get(symb)
        if not b:
            raise HTTPException(404, symb)
        s = b.series(self.period)
        times = [epoch(x["time_us"]) for x in b.bars]
        # 값이 없는 봉도 시각만 넣어 둔다. 세 차트의 봉 수가 같아야 스크롤을 맞출 수 있다
        pts = lambda xs: [{"time": t} if v is None else {"time": t, "value": v}
                          for t, v in zip(times, xs)]
        return {"symb": symb,
                "bars": [{"time": t, "open": x["open"], "high": x["high"], "low": x["low"],
                          "close": x["close"]} for t, x in zip(times, b.bars)],
                "rsi": pts(s["rsi"]), "macd": pts(s["macd"]), "signal": pts(s["signal"]),
                "hist": pts(s["hist"])}

    async def broadcast(self, msg):
        text = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                self.clients.discard(ws)

    async def push_loop(self):
        """체결은 초에 수십 개씩 오니 0.5초마다 바뀐 종목만 묶어 알림을 보고 화면에 보낸다.
        알림은 브라우저가 없어도 본다."""
        while True:
            await asyncio.sleep(0.5)
            if not self.dirty:
                continue
            dirty, self.dirty = self.dirty, set()
            self.check_alerts(dirty)
            if self.clients:
                rows = [self.row(self.books[s]) for s in dirty if s in self.books]
                await self.broadcast({"type": "rows", "rows": rows})


def _num(v):
    return f"{v:g}"


def epoch(local_time):
    """'YYYYMMDD HHMMSS'(그 시장의 현지 시각)를 그대로 UTC 인 척 초로. 차트 축이 현지 시각으로 찍힌다."""
    return calendar.timegm(time.strptime(local_time, "%Y%m%d %H%M%S"))


hub: Hub = None


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(hub.load)
    tasks = [asyncio.create_task(hub.kis_loop()), asyncio.create_task(hub.push_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def api_state():
    return hub.state()


@app.get("/api/chart/{symb}")
def api_chart(symb: str):
    return hub.chart(symb.upper())


@app.post("/api/tickers")
async def api_add(ticker: str = Body(..., embed=True)):
    ticker = ticker.strip()
    if not ticker:
        raise HTTPException(400, "종목을 적을 것.")
    try:
        book = await hub.add(ticker)
    except (LookupError, ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    return {"symb": book.symb}


@app.delete("/api/tickers/{symb}")
async def api_remove(symb: str):
    try:
        await hub.remove(symb.upper())
    except KeyError:
        raise HTTPException(404, symb)
    return {"ok": True}


@app.post("/api/sound")
def api_sound(on: bool = Body(..., embed=True)):
    hub.set_sound(on)
    return {"sound": hub.settings["sound"]}


@app.post("/api/sound/test")
def api_sound_test(symb: str = Body("", embed=True)):
    """소리 시험. 그 종목의 65 초과 문장을 말머리와 함께 읽는다."""
    b = hub.books.get(symb.upper()) or next(iter(hub.books.values()), None)
    text = al.say_breach(b.name, b.symb, True, al.UPPER) if b else "소리 시험"
    hub.voice.say(text, "short")
    return {"text": text, "server": hub.voice.server_up(), "cached": hub.voice.cached(text)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


def main():
    global hub
    p = argparse.ArgumentParser(description="RSI·MACD 웹 대시보드")
    p.add_argument("tickers", nargs="*", help="더할 종목. 없으면 저장해 둔 목록만")
    p.add_argument("--min", type=int, default=5, help="분 단위 (기본 5)")
    p.add_argument("--period", type=int, default=14, help="RSI 기간 (기본 14)")
    p.add_argument("--tts-cache", default=str(HERE / "tts_cache"),
                   help="소리 파일을 쌓는 곳. rsi 의 tts_cache 를 주면 거기 만들어 둔 소리를 같이 쓴다")
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 이면 같은 공유기의 다른 기기에서도 열린다")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않는다")
    args = p.parse_args()
    hub = Hub(args.tickers, args.min, args.period, args.tts_cache)
    import threading
    import uvicorn
    import webbrowser
    k.load_keys()  # 키가 없으면 여기서 안내하고 끝낸다
    url = f"http://localhost:{args.port}"
    print(url, flush=True)
    if not args.no_browser:
        threading.Timer(2.0, webbrowser.open, (url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
