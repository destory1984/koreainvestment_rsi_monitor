"""RSI·MACD 를 웹 화면으로 보여준다.

    python kis_web.py                       # 저장해 둔 종목으로 http://localhost:8000
    python kis_web.py TSLA 005930           # 종목을 더해서 띄우기 (국내는 종목코드 여섯 자리)
    python kis_web.py --host 0.0.0.0        # 같은 공유기의 휴대폰에서도 보기

서버 하나가 한국투자증권 실시간 연결을 잡고, 브라우저 여러 개에 값을 나눠 준다.
종목은 화면에서 더하고 뺄 수 있고, kis_watchlist.json 에 저장된다.
RSI 가 35/65, 30/70 을 넘으면 서버가 말로 알린다 (kis_alert.py). 브라우저를 닫아도 알림은 계속 나간다.
봉이 닫힐 때마다 RSI·MACD 매수·매도 시그널을 본다 (kis_signal.py).
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
import kis_signal as ks

HERE = Path(__file__).parent
STATIC = HERE / "static"
WATCHLIST = HERE / "kis_watchlist.json"
SETTINGS = HERE / "kis_settings.json"
ALERT_LOG = HERE / "kis_alerts.jsonl"   # 알림 기록. 한 줄에 하나, 서버를 다시 켜도 남는다
HISTORY = 500                          # 화면에 들고 있을 알림 수
MAX_TICKERS = 40  # 실시간 연결 하나에 41개까지 구독된다


class Hub:
    def __init__(self, tickers, nmin, period, tts_cache):
        self.tickers, self.nmin, self.period = tickers, nmin, period
        self.books = {}
        self.gates = {}           # 종목 -> 알림 상태
        self.sup_seen = set()     # 이미 적은 억제 (종목, 종류, 까닭). 실제로 울리면 지운다
        self.events = deque(self.read_log(), maxlen=HISTORY)   # 최근 것이 앞
        self.voice = al.Voice(tts_cache)
        try:
            self.settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            self.settings = {}
        self.settings.setdefault("sound", True)
        self.settings.setdefault("signal_sound", True)   # 시그널도 말로 알릴지
        self.signals = {}         # 종목 -> 닫힌 봉들에서 난 시그널 전부
        self.closed = set()       # 새 봉이 생겨 앞 봉이 닫힌 종목
        self.clients = set()
        self.dirty = set()
        self.status = "시작 중"
        self.markets = []         # 한 줄 띠: [{"name", "price", "rate"}]
        self.ws = None            # 한국투자증권 실시간 연결 (열려 있을 때만)
        self.approval_key = None
        self.day = k.us_day_session()   # 미국 주간거래 시간이면 해외 종목을 R 쪽으로 구독한다
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
        eng = self.voice.engine()
        print("알림 목소리: " + {"local": "로컬 TTS (Qwen3-TTS)",
                                 "edge": "Edge 음성 (인터넷, 로컬 TTS 서버는 없음)"}.get(
            eng, "윈도우 음성 (로컬 TTS 서버도 edge-tts 도 없음)"), flush=True)
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
            print(f"알림 문장 {missing}개를 만드는 중", flush=True)

    def _add(self, ticker):
        """종목을 찾아 분봉을 받는다 (블로킹). 이미 있으면 그 종목을 돌려준다."""
        excd, symb = k.resolve(self.appkey, self.secret, ticker)
        if symb in self.books:
            return self.books[symb], False
        if len(self.books) >= MAX_TICKERS:
            raise ValueError(f"종목은 {MAX_TICKERS}개까지다.")
        bars = k.fetch_bars_24h(self.appkey, self.secret, excd, symb, self.nmin)
        if not bars:
            raise ValueError(f"{symb}: 분봉이 없다.")
        book = k.Book(excd, symb, bars, self.nmin)
        self.books[symb] = book
        self.gates[symb] = al.Gate()
        self.signals[symb] = ks.signals(bars[:-1], self.period)
        time.sleep(0.1)
        return book, True

    async def add(self, ticker):
        async with self.lock:
            book, new = await asyncio.to_thread(self._add, ticker)
            if not new:
                return book
            self.save()
            if self.ws:
                await k.subscribe(self.ws, self.approval_key, book.key_for(self.day))
        self.prefetch([book])
        await self.broadcast({"type": "add", "row": self.row(book)})
        return book

    async def remove(self, symb):
        async with self.lock:
            book = self.books.pop(symb, None)
            if not book:
                raise KeyError(symb)
            self.gates.pop(symb, None)
            self.signals.pop(symb, None)
            self.save()
            self.dirty.discard(symb)
            if self.ws:
                try:
                    await k.subscribe(self.ws, self.approval_key, book.key_for(self.day), on=False)
                except Exception:
                    pass
        await self.broadcast({"type": "remove", "symb": symb})

    async def reorder(self, symbs):
        """적은 차례대로 줄을 세운다. 목록에 빠진 종목은 원래 차례대로 뒤에 붙인다."""
        async with self.lock:
            order = [s for s in dict.fromkeys(symbs) if s in self.books]
            order += [s for s in self.books if s not in order]
            self.books = {s: self.books[s] for s in order}
            self.save()
        await self.broadcast({"type": "order", "symbs": order})
        return order

    # ── 한국투자증권 쪽 ────────────────────────────────────────
    def reload_bars(self):
        """끊겼다 붙으면 그 사이 체결을 놓쳤으니 분봉을 새로 받는다."""
        for b in list(self.books.values()):
            b.bars = k.fetch_bars_24h(self.appkey, self.secret, b.excd, b.symb, self.nmin)
            self.signals[b.symb] = ks.signals(b.bars[:-1], self.period)
            time.sleep(0.1)

    def on_tick(self, d):
        book = self.books.get(d["SYMB"])
        if book:
            if book.on_tick(d):
                self.closed.add(book.symb)
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
                keys = [b.key_for(self.day) for b in self.books.values()]
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

    async def session_loop(self):
        """미국 주간거래가 열리고 닫힐 때 해외 종목 구독을 D↔R 로 바꿔 건다."""
        while True:
            await asyncio.sleep(30)
            day = k.us_day_session()
            if day == self.day:
                continue
            async with self.lock:
                self.day = day
                if self.ws:
                    try:
                        for b in self.books.values():
                            if b.excd != "KRX":
                                await k.subscribe(self.ws, self.approval_key, b.key_for(not day), on=False)
                                await k.subscribe(self.ws, self.approval_key, b.key_for(day))
                    except Exception:
                        pass   # 끊겼으면 다시 붙을 때 새 키로 건다
            print("미국 " + ("주간거래로 바꿔 받는다" if day else "정규장·프리·애프터로 바꿔 받는다"), flush=True)

    # ── 지수 띠 ───────────────────────────────────────────────
    def fetch_markets(self):
        out = []
        for name, kind, code in k.MARKETS:
            try:
                price, rate = k.fetch_market(self.appkey, self.secret, kind, code)
            except Exception:
                price, rate = None, None
            out.append({"name": name, "price": price, "rate": rate, "kind": kind})
            time.sleep(0.1)
        try:
            price, rate = k.fetch_bitcoin()
        except Exception:
            price, rate = None, None
        out.append({"name": "비트코인", "price": price, "rate": rate, "kind": "BTC"})
        return out

    async def markets_loop(self):
        """지수는 실시간으로 밀어 주는 통로가 없어 10초마다 묻는다."""
        while True:
            try:
                self.markets = await asyncio.to_thread(self.fetch_markets)
                await self.broadcast({"type": "markets", "markets": self.markets})
            except Exception as e:
                print(f"지수 띠 — {e}", flush=True)
            await asyncio.sleep(10)

    # ── 알림 ──────────────────────────────────────────────────
    @staticmethod
    def read_log():
        try:
            lines = ALERT_LOG.read_text(encoding="utf-8").splitlines()[-HISTORY:]
        except FileNotFoundError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    def last_alert(self, symb):
        """그 종목에서 마지막으로 실제로 울린 선 알림 (억제된 것과 시그널은 빼고)."""
        return next((e for e in self.events if e["symb"] == symb and not e["suppressed"]
                     and e.get("type") != "signal"), None)

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
            # 선 언저리에서 오르내리면 0.5초마다 억제가 나온다. 같은 까닭의 억제는 한 번만 적는다
            sup = a.get("suppressed", "")
            if sup:
                key = (s, a["kind"], "재무장" if "재무장" in sup else "쿨다운")
                if key in self.sup_seen:
                    continue
                self.sup_seen.add(key)
            else:
                self.sup_seen = {x for x in self.sup_seen if x[:2] != (s, a["kind"])}
            said = al.say_breach(b.name, b.symb, a["zone"] == "above", a["edge"])
            now = datetime.now()
            ev = {"ts": now.timestamp(), "d": now.strftime("%m-%d"), "t": now.strftime("%H:%M:%S"),
                  "bar": epoch(b.bars[-1]["time_us"]), "symb": s, "name": b.name,
                  "rsi": v, "zone": a["zone"], "strength": a["strength"],
                  "text": f"{_num(a['edge'])} {'초과' if a['zone'] == 'above' else '미만'}"
                          + (" (시작 때부터)" if a["start"] else ""),
                  "suppressed": a.get("suppressed", "")}
            self.events.appendleft(ev)
            with ALERT_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            print(f"{ev['t']} 알림 {b.name} {ev['text']} RSI {v:.2f}"
                  + (f" — 억제 ({ev['suppressed']})" if ev["suppressed"] else ""), flush=True)
            if not ev["suppressed"] and self.settings["sound"]:
                strong = a["strength"] == "strong"
                self.voice.say(said if (not strong or al.SAY_STRONG) else "",
                               "full" if strong else "short")
            asyncio.get_event_loop().create_task(self.broadcast({"type": "alert", "event": ev}))

    def check_signals(self, symbs):
        """앞 봉이 닫힌 종목의 시그널을 다시 셈한다. 방금 닫힌 봉에서 새로 났으면 알린다."""
        for s in symbs:
            b = self.books.get(s)
            if not b or len(b.bars) < 3:
                continue
            old = {(x.bar, x.side) for x in self.signals.get(s, [])}
            self.signals[s] = ks.signals(b.bars[:-1], self.period)
            just = b.bars[-2]["time_us"]
            for x in self.signals[s]:
                if x.bar != just or (x.bar, x.side) in old:
                    continue
                now = datetime.now()
                ev = {"type": "signal", "ts": now.timestamp(), "d": now.strftime("%m-%d"),
                      "t": now.strftime("%H:%M:%S"), "bar": epoch(x.bar), "symb": s, "name": b.name,
                      "rsi": x.rsi, "side": x.side, "zone": "above" if x.side == "buy" else "below",
                      "strength": "strong" if x.grade == "강" else "warn",
                      "text": f"{x.word} 시그널 ({x.grade}, {x.trend})",
                      "detail": f"무장 중 RSI {'최저' if x.side == 'buy' else '최고'} {x.extreme:.1f}"
                                f" · MACD {x.macd:+.4f} / 시그널 {x.signal:+.4f}",
                      "suppressed": ""}
                self.events.appendleft(ev)
                with ALERT_LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                print(f"{ev['t']} 시그널 {b.name} {ev['text']} RSI {x.rsi:.2f}", flush=True)
                if self.settings["sound"] and self.settings["signal_sound"]:
                    self.voice.say(al.say_signal(b.name, s, x.side),
                                   "full" if x.grade == "강" else "short")
                asyncio.get_event_loop().create_task(self.broadcast({"type": "alert", "event": ev}))

    def last_signal(self, symb):
        x = (self.signals.get(symb) or [None])[-1]
        if not x:
            return None
        return {"side": x.side, "word": x.word, "grade": x.grade, "trend": x.trend,
                "bar": epoch(x.bar)}

    def set_sound(self, on):
        self.settings["sound"] = bool(on)
        SETTINGS.write_text(json.dumps(self.settings, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 브라우저 쪽 ───────────────────────────────────────────
    def set_status(self, s):
        self.status = s
        print(f"{datetime.now():%H:%M:%S} {s}", flush=True)
        asyncio.get_event_loop().create_task(self.broadcast({"type": "status", "status": s}))

    def row(self, b):
        ind = b.indicators(self.period)
        bar = b.bars[-1] if b.bars else None
        g = self.gates.get(b.symb)
        return {"symb": b.symb, "excd": b.excd, "name": b.name, "price": b.price, "rate": b.rate,
                "time": b.us_time, "day": b.day_quote, **ind,
                "zone": al.level_of(ind["rsi"]) if ind["rsi"] is not None else ("neutral", ""),
                "rearm": bool(g and not all(g.armed.values())),
                "last_alert": self.last_alert(b.symb),
                "last_signal": self.last_signal(b.symb),
                "bar": bar and {"time": epoch(bar["time_us"]), "open": bar["open"],
                                "high": bar["high"], "low": bar["low"], "close": bar["close"]}}

    def state(self):
        return {"status": self.status, "nmin": self.nmin, "period": self.period,
                "lower": al.LOWER, "upper": al.UPPER,
                "strong_lower": al.STRONG_LOWER, "strong_upper": al.STRONG_UPPER,
                "max": MAX_TICKERS, "sound": self.settings["sound"], "markets": self.markets,
                "signal_sound": self.settings["signal_sound"],
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
                "hist": pts(s["hist"]),
                "signals": [{"time": epoch(x.bar), "side": x.side, "word": x.word, "grade": x.grade,
                             "trend": x.trend} for x in self.signals.get(symb, [])]}

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
            closed, self.closed = self.closed, set()
            self.check_alerts(dirty)
            self.check_signals(closed)
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
    tasks = [asyncio.create_task(hub.kis_loop()), asyncio.create_task(hub.push_loop()),
             asyncio.create_task(hub.markets_loop()), asyncio.create_task(hub.session_loop())]
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


@app.put("/api/order")
async def api_order(symbs: list[str] = Body(..., embed=True)):
    return {"symbs": await hub.reorder([s.upper() for s in symbs])}


@app.post("/api/sound")
def api_sound(on: bool = Body(..., embed=True)):
    hub.set_sound(on)
    return {"sound": hub.settings["sound"]}


@app.post("/api/signal-sound")
def api_signal_sound(on: bool = Body(..., embed=True)):
    hub.settings["signal_sound"] = bool(on)
    hub.set_sound(hub.settings["sound"])   # 설정 파일에 같이 적는다
    return {"signal_sound": hub.settings["signal_sound"]}


@app.post("/api/sound/test")
def api_sound_test(symb: str = Body("", embed=True)):
    """소리 시험. 그 종목의 65 초과 문장을 말머리와 함께 읽는다."""
    b = hub.books.get(symb.upper()) or next(iter(hub.books.values()), None)
    text = al.say_breach(b.name, b.symb, True, al.UPPER) if b else "소리 시험"
    hub.voice.say(text, "short")
    return {"text": text, "engine": hub.voice.engine(), "cached": hub.voice.cached(text)}


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
