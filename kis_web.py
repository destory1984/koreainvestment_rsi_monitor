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
import re
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import kis_alert as al
import kis_replay as rp
import kis_telegram as tg
import kis_rsi as k
import kis_signal as ks

HERE = Path(__file__).parent
STATIC = HERE / "static"
WATCHLIST = HERE / "kis_watchlist.json"
SETTINGS = HERE / "kis_settings.json"
ALERT_LOG = HERE / "kis_alerts.jsonl"   # 알림 기록. 한 줄에 하나, 서버를 다시 켜도 남는다
HISTORY = 500                          # 화면에 들고 있을 알림 수
MAX_TICKERS = 40  # 실시간 연결 하나에 41개까지 구독된다
AFTER = (15, 30, 60)   # 알림 뒤 이만큼 분 지나 가격이 알림 쪽으로 갔는지 본다 (kis_replay 와 같은 셈)
AFTER_SLACK = 10 * 60  # 그 시각 뒤 이만큼(초) 안에 시작한 봉이 없으면 장이 닫힌 것으로 본다
HISTORY_DAYS = 5      # 켤 때 받은 분봉 앞에 DB(replay_cache/bars.db)에서 이어 붙일 날짜 수. 0 이면 안 붙인다
MTF = (1, 5, 15, 60)   # 한 줄에 나란히 보일 RSI 시간봉 (분). 알림·시그널은 주 분봉(기본 5)으로만
SOUND_SESSIONS = {"day": "미국 주간거래", "pre": "미국 프리장", "regular": "미국 정규장",
                  "after": "미국 애프터", "kr": "국내 종목"}   # 소리를 따로 켜고 끄는 때


class Hub:
    def __init__(self, tickers, nmin, period, tts_cache):
        self.tickers, self.nmin, self.period = tickers, nmin, period
        self.books = {}
        self.tf = {}              # 종목 -> {분: Book}. 주 분봉 말고 나란히 보일 시간봉들 (뒤에서 받는다)
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
        # 소리 나는 때: 세션마다 켜고 끄기, 그리고 조용한 시각(이 PC 시각). 알림 기록에는 늘 쌓인다
        self.settings.setdefault("sound_sessions", {})
        for key in SOUND_SESSIONS:
            self.settings["sound_sessions"].setdefault(key, True)
        self.settings.setdefault("quiet", {"on": False, "from": "00:00", "to": "07:00"})
        self.settings.setdefault("mute", [])   # 소리를 끈 종목들 (기록은 쌓인다)
        self.settings.setdefault("telegram", True)   # 텔레그램으로도 보낼지 (토큰·대화방이 있어야)
        self.settings.setdefault("lines", {})        # 종목 -> [강한 아래, 아래, 위, 강한 위]. 없으면 기본 30·35·65·70
        self.tg = tg.Telegram()
        self.signals = {}         # 종목 -> 닫힌 봉들에서 난 시그널 전부
        self.closed = set()       # 새 봉이 생겨 앞 봉이 닫힌 종목
        self.clients = set()
        self.dirty = set()
        self.status = "시작 중"
        self.markets = []         # 한 줄 띠: [{"name", "price", "rate"}]
        self.kr_closed, self.kr_closed_day = [], ""   # 국내 휴장일과 그것을 받은 날
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
        self.fill_after()   # 꺼져 있던 사이 결과가 난 알림들
        self.load_tf()   # 1·15·60분봉은 뒤에서

    def greetings(self):
        return (self.settings.get("greeting", al.TTS_GREETING),
                self.settings.get("greeting_again", al.TTS_GREETING_AGAIN))

    def prefetch(self, books):
        texts = [t for t in self.greetings() if t]
        for b in books:
            texts += al.phrases(b.name, b.symb, self.lines(b.symb))
        missing = sum(not self.voice.cached(t) for t in texts)
        if missing and self.voice.prefetch(texts, lambda made, n: print(
                f"알림 문장 {made}/{n}개 만듦" + ("" if made == n else f" — {self.voice.last_error}"),
                flush=True)):
            print(f"알림 문장 {missing}개를 만드는 중", flush=True)

    def _bars(self, excd, symb):
        """분봉을 받는다 (블로킹). 미국 종목은 Webull 처럼 오버나이트 거래가 활발한 종목만 주간거래 봉을 넣는다.
        Webull 은 인기 종목만 24시간 거래를 해 주고 나머지는 애프터장까지만 그린다. 그 목록을 알 길이 없으니
        최근 오버나이트 5분 칸 절반 넘게 체결이 있었는지로 가른다. 틀리면 화면에서 바꾸고 설정에 남긴다."""
        bars = k.fetch_bars(self.appkey, self.secret, excd, symb, self.nmin)
        if excd not in k.DAY_EXCD:
            self.store([(excd, symb, bars[:-1], True)])
            return self._history(excd, symb, bars, False) + bars, None
        day, have, of = k.fetch_night(self.appkey, self.secret, excd, symb, self.nmin)
        auto = have > of * k.NIGHT_SHARE
        on = self.settings.get("night", {}).get(symb, auto)
        # 받은 것은 되감기 DB 에도 쌓는다 (마지막 봉은 아직 진행 중이라 뺀다). 주간거래 봉은 정규 쪽 봉을 덮지 않는다
        self.store([(excd, symb, bars[:-1], True), (excd, symb, day[:-1], False)], night=(excd, symb, on))
        bars = k.merge_bars(bars, day) if on else bars
        return self._history(excd, symb, bars, on) + bars, {"on": on, "auto": auto, "have": have, "of": of}

    def _history(self, excd, symb, bars, night_on):
        """DB 에 쌓인, 받은 봉보다 앞선 봉들 (블로킹). 최근 HISTORY_DAYS 날짜만. 차트를 며칠 뒤까지 보고
        RSI·MACD 를 긴 기록으로 셈하려는 것이다. 받은 봉과 같은 규칙으로 거른다: 국내는 KRX 정규장만,
        오버나이트를 안 넣는 미국 종목은 주간거래 봉을 뺀다. 못 읽으면 빈 목록."""
        if not bars or not HISTORY_DAYS:
            return []
        try:
            con = rp.db()
            try:
                old = rp.get_bars(con, excd, symb, self.nmin, before=bars[0]["time_us"], days=HISTORY_DAYS)
            finally:
                con.close()
        except Exception as e:
            print(f"{symb}: DB 에서 지난 봉을 못 읽음 — {e}", flush=True)
            return []
        if excd == "KRX":
            return [b for b in old if k.KRX_OPEN <= b["time_us"][9:] <= k.KRX_CLOSE]
        if not night_on:
            return [b for b in old if rp.session(rp.ts(b["time_us"])) != "주간"]
        return old

    def store(self, items, night=None):
        """분봉을 되감기 DB(replay_cache/bars.db)에 쌓는다 (블로킹). items 는 [(excd, symb, 봉들, 덮어쓸지)].
        서버가 켜져 있는 동안 봉이 쌓이니, 한국투자증권이 지금 세션 것만 주는 주간거래 봉도 모인다.
        DB 에 못 써도 서버는 그대로 돈다."""
        try:
            con = rp.db()
            try:
                for excd, symb, bars, replace in items:
                    rp.put_bars(con, excd, symb, self.nmin, bars, replace=replace)
                if night:
                    rp.set_night(con, *night[:2], self.nmin, night[2])
                con.commit()
            finally:
                con.close()
        except Exception as e:
            print(f"분봉 DB 에 못 씀 — {e}", flush=True)

    def store_closed(self, symbs):
        """방금 닫힌 봉을 DB 에 넣는다. 실시간으로 만든 봉이라 한국투자증권이 준 봉이 이미 있으면 두고 없을 때만.
        알림을 늦추지 않게 봉만 베껴 두고 쓰기는 다른 스레드가 한다 (DB 가 잠겨 있으면 30초까지 기다리니)."""
        items = [(b.excd, b.symb, [dict(b.bars[-2])], False)
                 for b in (self.books.get(s) for s in symbs) if b and len(b.bars) >= 2]
        if items:
            threading.Thread(target=self.store, args=(items,), daemon=True).start()

    def _tf_books(self, book):
        """주 분봉 말고 MTF 의 다른 시간봉들을 받는다 (블로킹). 오버나이트 봉은 주 분봉과 같은 결정을 따른다."""
        out = {}
        for tf in MTF:
            if tf == self.nmin:
                continue
            try:
                if book.excd == "KRX":   # 국내는 1분봉을 묶어 만드니 긴 봉은 조금만 받는다
                    bars = k.fetch_kr_bars(self.appkey, self.secret, book.symb, tf, 120 if tf == 1 else 30)
                else:
                    bars = k.fetch_bars(self.appkey, self.secret, book.excd, book.symb, tf)
                    if book.night and book.night["on"]:
                        try:
                            bars = k.merge_bars(bars, k.fetch_bars(self.appkey, self.secret,
                                                                   k.DAY_EXCD[book.excd], book.symb, tf))
                        except Exception:
                            pass
            except Exception as e:
                print(f"{book.symb} {tf}분봉 못 받음 — {e}", flush=True)
                continue
            b = k.Book(book.excd, book.symb, bars, tf)
            b.night = book.night
            out[tf] = b
            time.sleep(0.1)
        return out

    def load_tf(self, books=None):
        """여러 시간봉을 뒤에서 받는다. 켤 때 5분봉만 먼저 받아 화면을 빨리 띄우려는 것이다."""
        def run():
            for b in list(books or self.books.values()):
                if b.symb in self.books:
                    self.tf[b.symb] = self._tf_books(b)
                    self.dirty.add(b.symb)
        threading.Thread(target=run, daemon=True).start()

    async def set_night(self, symb, on):
        """오버나이트 봉을 넣을지 손으로 정한다. 저절로 정한 것과 같으면 설정에서 지운다."""
        book = self.books.get(symb)
        if not book or not book.night:
            raise KeyError(symb)
        night = self.settings.setdefault("night", {})
        if on == book.night["auto"]:
            night.pop(symb, None)
        else:
            night[symb] = on
        self.save_settings()
        async with self.lock:
            book.bars, book.night = await asyncio.to_thread(self._bars, book.excd, symb)
            self.signals[symb] = ks.signals(book.bars[:-1], self.period, lines=self.lines(symb))
            self.tf[symb] = await asyncio.to_thread(self._tf_books, book)
        await self.broadcast({"type": "reload"})

    def _add(self, ticker):
        """종목을 찾아 분봉을 받는다 (블로킹). 이미 있으면 그 종목을 돌려준다."""
        excd, symb = k.resolve(self.appkey, self.secret, ticker)
        if symb in self.books:
            return self.books[symb], False
        if len(self.books) >= MAX_TICKERS:
            raise ValueError(f"종목은 {MAX_TICKERS}개까지다.")
        bars, night = self._bars(excd, symb)
        if not bars:
            raise ValueError(f"{symb}: 분봉이 없다.")
        book = k.Book(excd, symb, bars, self.nmin)
        book.night = night
        if excd != "KRX":
            # 첫 체결이 올 때까지 등락 칸이 비지 않게 현재가를 한 번 묻는다 (주간거래 시간이면 주간거래 가격)
            day = k.us_day_session()
            try:
                book.price, book.rate = k.fetch_quote(self.appkey, self.secret,
                                                      k.DAY_EXCD[excd] if day else excd, symb)
                book.day_quote = day
            except Exception as e:
                print(f"{symb}: 현재가 못 받음 — {e}", flush=True)
        self.books[symb] = book
        self.gates[symb] = al.Gate(self.lines(symb))
        self.signals[symb] = ks.signals(bars[:-1], self.period, lines=self.lines(symb))
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
        self.load_tf([book])
        await self.broadcast({"type": "add", "row": self.row(book)})
        return book

    async def remove(self, symb):
        async with self.lock:
            book = self.books.pop(symb, None)
            if not book:
                raise KeyError(symb)
            self.gates.pop(symb, None)
            self.signals.pop(symb, None)
            self.tf.pop(symb, None)
            self.save()
            if symb in self.settings["mute"] or symb in self.settings["lines"]:
                if symb in self.settings["mute"]:
                    self.settings["mute"].remove(symb)
                self.settings["lines"].pop(symb, None)
                self.save_settings()
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
            b.bars, b.night = self._bars(b.excd, b.symb)
            self.signals[b.symb] = ks.signals(b.bars[:-1], self.period, lines=self.lines(b.symb))
            self.tf[b.symb] = self._tf_books(b)
            time.sleep(0.1)

    def on_tick(self, d):
        book = self.books.get(d["SYMB"])
        if book:
            if book.on_tick(d):
                self.closed.add(book.symb)
            for b in self.tf.get(book.symb, {}).values():
                b.on_tick(d)
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
                price, rate = (k.fetch_bitcoin() if kind == "BTC"
                               else k.fetch_market(self.appkey, self.secret, kind, code))
            except Exception:
                price, rate = None, None
            out.append({"name": name, "price": price, "rate": rate, "kind": kind})
            if kind != "BTC":
                time.sleep(0.1)
        return out

    def holidays(self):
        year = datetime.now().year
        return {"kr": self.kr_closed, "us": k.us_holidays([year, year + 1]),
                "us_early": k.us_early_closes([year, year + 1])}

    def load_holidays(self):
        """국내 휴장일을 하루 한 번 받는다 (블로킹). 받았으면 True."""
        today = datetime.now().strftime("%Y%m%d")
        if self.kr_closed_day == today:
            return False
        try:
            self.kr_closed, self.kr_closed_day = k.kr_holidays(self.appkey, self.secret), today
            return True
        except Exception as e:
            print(f"국내 휴장일 — {e}", flush=True)
            return False

    async def markets_loop(self):
        """지수는 실시간으로 밀어 주는 통로가 없어 10초마다 묻는다. 날이 바뀌면 휴장일도 새로 받는다."""
        while True:
            if await asyncio.to_thread(self.load_holidays):
                await self.broadcast({"type": "holidays", "holidays": self.holidays()})
            try:
                self.markets = await asyncio.to_thread(self.fetch_markets)
                await self.broadcast({"type": "markets", "markets": self.markets})
            except Exception as e:
                print(f"지수 띠 — {e}", flush=True)
            await asyncio.sleep(10)

    # ── 알림 ──────────────────────────────────────────────────
    @staticmethod
    def read_log():
        """알림 기록을 최근 것부터. 「그 뒤」 결과는 따로 적힌 줄({"type": "after"})을 알림에 붙인다."""
        try:
            lines = ALERT_LOG.read_text(encoding="utf-8").splitlines()[-HISTORY * 2:]
        except FileNotFoundError:
            return []
        out, after = [], {}
        for ln in lines:
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if ev.get("type") == "after":
                after[(ev["of"], ev["symb"])] = ev["after"]
            else:
                out.append(ev)
        for ev in out:
            if (ev["ts"], ev["symb"]) in after:
                ev["after"] = after[(ev["ts"], ev["symb"])]
        return out[::-1][:HISTORY]

    @staticmethod
    def scores(symb=None):
        """실제로 울린 알림·시그널의 성적 (알림 기록 전체). 종류별로 15·30·60분 뒤 건수·맞음(보합 뺌)·평균(%).
        + 는 알림이 말한 쪽으로 간 것 (fill_after 와 같다). 억제된 것, 켤 때 이미 넘어 있던 것은 뺀다."""
        try:
            lines = ALERT_LOG.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        events, after = [], {}
        for ln in lines:
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if ev.get("type") == "after":
                after[(ev["of"], ev["symb"])] = ev["after"]
            else:
                events.append(ev)
        groups = {}
        for ev in events:
            if symb and ev["symb"] != symb:
                continue
            if ev.get("suppressed") or ev.get("start") or "(시작 때부터)" in ev.get("text", ""):
                continue
            a = after.get((ev["ts"], ev["symb"])) or ev.get("after")
            if not a:
                continue
            if ev.get("type") == "signal":
                kind = f"{'매수' if ev['side'] == 'buy' else '매도'} {'강' if ev['strength'] == 'strong' else '약'}"
            else:
                kind = ev["text"]
            g = groups.setdefault(kind, {h: [] for h in AFTER})
            for h in AFTER:
                v = a.get(str(h))
                if v is not None:
                    g[h].append(v)
        out = []
        for kind, g in groups.items():
            row = {"kind": kind}
            for h in AFTER:
                vs = g[h]
                moved = [v for v in vs if v != 0]
                row[str(h)] = {"n": len(vs), "hit": sum(v > 0 for v in moved) / len(moved) if moved else None,
                               "avg": sum(vs) / len(vs) if vs else None}
            out.append(row)

        def key(r):   # 알림은 선 숫자 차례(미만 먼저), 그다음 시그널
            k = r["kind"]
            if k[0].isdigit():
                n = float(k.split()[0])
                return (0, n if "미만" in k else 100 + n)
            return (1, ["매수 강", "매수 약", "매도 강", "매도 약"].index(k) if k in ("매수 강", "매수 약", "매도 강", "매도 약") else 9)
        return sorted(out, key=key)

    def last_alert(self, symb):
        """그 종목에서 마지막으로 실제로 울린 선 알림 (억제된 것과 시그널은 빼고)."""
        return next((e for e in self.events if e["symb"] == symb and not e["suppressed"]
                     and e.get("type") != "signal"), None)

    def market_closed(self, book):
        """그 종목 시장이 하루 쉬는 때 (주말·휴일, 장 마감은 아님). 화면의 「휴장」과 같다.
        이때는 알림·시그널을 보지 않는다 — 켤 때마다 「시작 때부터」 기록이 쌓였다."""
        if book.excd == "KRX":
            now = datetime.now()
            return now.weekday() >= 5 or now.strftime("%Y-%m-%d") in self.kr_closed
        return k.us_session() is None

    def check_alerts(self, symbs):
        for s in symbs:
            b, g = self.books.get(s), self.gates.get(s)
            if not b or not g or self.market_closed(b):
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
                  "rsi": v, "price": b.price, "start": a["start"], "zone": a["zone"], "strength": a["strength"],
                  "text": f"{_num(a['edge'])} {'초과' if a['zone'] == 'above' else '미만'}"
                          + (" (시작 때부터)" if a["start"] else ""),
                  "suppressed": a.get("suppressed", ""),
                  # 켤 때 이미 선 너머였던 것은 기록만 한다. 켤 때마다 알림이 몰려 울리지 않게
                  "muted": "" if a.get("suppressed") else "켤 때 이미 넘어 있음" if a["start"] else self.muted(b)}
            self.events.appendleft(ev)
            with ALERT_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            print(f"{ev['t']} 알림 {b.name} {ev['text']} RSI {v:.2f}"
                  + (f" — 억제 ({ev['suppressed']})" if ev["suppressed"] else ""), flush=True)
            if not ev["suppressed"] and not a["start"] and self.settings["telegram"]:
                self.tg.send(tg.alert_text(b.name, b.excd, ev), silent=bool(ev["muted"]))
            if not ev["suppressed"] and self.settings["sound"] and not ev["muted"]:
                strong = a["strength"] == "strong"
                self.voice.say(said if (not strong or al.SAY_STRONG) else "",
                               "full" if strong else "short")
            asyncio.get_event_loop().create_task(self.broadcast({"type": "alert", "event": ev}))

    def check_signals(self, symbs):
        """앞 봉이 닫힌 종목의 시그널을 다시 셈한다. 방금 닫힌 봉에서 새로 났으면 알린다."""
        for s in symbs:
            b = self.books.get(s)
            if not b or len(b.bars) < 3 or self.market_closed(b):
                continue
            old = {(x.bar, x.side) for x in self.signals.get(s, [])}
            self.signals[s] = ks.signals(b.bars[:-1], self.period, lines=self.lines(s))
            just = b.bars[-2]["time_us"]
            for x in self.signals[s]:
                if x.bar != just or (x.bar, x.side) in old:
                    continue
                now = datetime.now()
                ev = {"type": "signal", "ts": now.timestamp(), "d": now.strftime("%m-%d"),
                      "t": now.strftime("%H:%M:%S"), "bar": epoch(x.bar), "symb": s, "name": b.name,
                      "rsi": x.rsi, "price": b.price,
                      "side": x.side, "zone": "above" if x.side == "buy" else "below",
                      "strength": "strong" if x.grade == "강" else "warn",
                      "text": f"{x.word} 시그널 ({x.grade}, {x.trend})",
                      "detail": f"무장 중 RSI {'최저' if x.side == 'buy' else '최고'} {x.extreme:.1f}"
                                f" · MACD {x.macd:+.4f} / 시그널 {x.signal:+.4f}",
                      "suppressed": "", "muted": self.muted(b)}
                self.events.appendleft(ev)
                with ALERT_LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                print(f"{ev['t']} 시그널 {b.name} {ev['text']} RSI {x.rsi:.2f}", flush=True)
                if self.settings["telegram"]:
                    self.tg.send(tg.signal_text(b.name, b.excd, ev), silent=bool(ev["muted"]))
                if self.settings["sound"] and self.settings["signal_sound"] and not ev["muted"]:
                    self.voice.say(al.say_signal(b.name, s, x.side),
                                   "full" if x.grade == "강" else "short")
                asyncio.get_event_loop().create_task(self.broadcast({"type": "alert", "event": ev}))

    def fill_after(self, symbs=None):
        """알림 뒤 15·30·60분 결과를 채운다. 말한 쪽으로 갔으면 +(%). 장이 닫혀 못 보면 None.
        들어간 값은 알림 때 가격, 나온 값은 알림 봉에서 그만큼 뒤에 시작한 봉의 종가(그 봉이 닫힌 뒤).
        셋 다 정해지면 알림 기록에 한 줄 더 적어 다시 켜도 남게 한다. 바뀐 알림들을 돌려준다."""
        changed = []
        for ev in self.events:
            if symbs is not None and ev["symb"] not in symbs:
                continue
            if ev.get("suppressed") or ev.get("start") or "(시작 때부터)" in ev.get("text", ""):
                continue
            after = ev.setdefault("after", {})
            if len(after) == len(AFTER):
                continue
            b = self.books.get(ev["symb"])
            if not b or not b.bars:
                continue
            times = [epoch(x["time_us"]) for x in b.bars]
            try:
                i0 = times.index(ev["bar"])
            except ValueError:
                continue   # 알림 봉이 받은 분봉 밖이다
            entry = ev.get("price") or b.bars[i0]["close"]
            up = ev["zone"] == "below" if ev.get("type") != "signal" else ev["side"] == "buy"
            before = dict(after)
            for h in AFTER:
                if str(h) in after:
                    continue
                target = ev["bar"] + h * 60
                j = next((j for j in range(i0 + 1, len(times)) if times[j] >= target), None)
                if j is None or j == len(times) - 1 and times[j] <= target + AFTER_SLACK:
                    continue   # 아직 그 봉이 없거나 진행 중
                if times[j] > target + AFTER_SLACK:
                    after[str(h)] = None   # 그사이 장이 닫혔다
                else:
                    r = (b.bars[j]["close"] / entry - 1) * 100
                    after[str(h)] = round(r if up else 0.0 - r, 3)
            if after != before:
                changed.append(ev)
                if len(after) == len(AFTER):
                    with ALERT_LOG.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"type": "after", "of": ev["ts"], "symb": ev["symb"],
                                            "after": after}, ensure_ascii=False) + "\n")
        return changed

    def last_signal(self, symb):
        x = (self.signals.get(symb) or [None])[-1]
        if not x:
            return None
        return {"side": x.side, "word": x.word, "grade": x.grade, "trend": x.trend,
                "bar": epoch(x.bar)}

    def muted(self, book):
        """이 종목 알림의 소리를 가릴 까닭. 울려도 되면 빈 문자열."""
        if book.symb in self.settings["mute"]:
            return "종목 소리 끔"
        q = self.settings["quiet"]
        if q["on"]:
            now, a, b = datetime.now().strftime("%H:%M"), q["from"], q["to"]
            if (a <= now < b) if a <= b else (now >= a or now < b):
                return f"조용한 시각 {a}~{b}"
        key = "kr" if book.excd == "KRX" else k.us_session()
        if key and not self.settings["sound_sessions"].get(key, True):
            return f"{SOUND_SESSIONS[key]} 소리 끔"
        return ""

    def lines(self, symb):
        """그 종목의 RSI 선. 설정에 없거나 틀렸으면 기본."""
        try:
            return al.Lines.parse(self.settings["lines"][symb])
        except (KeyError, ValueError, TypeError):
            return al.DEFAULT_LINES

    async def set_lines(self, symb, values):
        """종목 선을 바꾼다 (values 가 None 이거나 기본과 같으면 기본으로). 알림 상태를 새로 시작하고 시그널을 다시 셈한다."""
        book = self.books.get(symb)
        if not book:
            raise KeyError(symb)
        ln = al.Lines.parse(values) if values else al.DEFAULT_LINES   # 틀리면 ValueError
        if ln == al.DEFAULT_LINES:
            self.settings["lines"].pop(symb, None)
        else:
            self.settings["lines"][symb] = list(ln)
        self.save_settings()
        self.gates[symb] = al.Gate(ln)
        self.sup_seen = {x for x in self.sup_seen if x[0] != symb}
        self.signals[symb] = ks.signals(book.bars[:-1], self.period, lines=ln)
        self.prefetch([book])   # 「25 미만」 같은 새 문장
        await self.broadcast({"type": "rows", "rows": [self.row(book)]})
        return ln

    async def set_mute(self, symb, on):
        if symb not in self.books:
            raise KeyError(symb)
        mute = [s for s in self.settings["mute"] if s != symb] + ([symb] if on else [])
        self.settings["mute"] = mute
        self.save_settings()
        await self.broadcast({"type": "rows", "rows": [self.row(self.books[symb])]})

    def set_sound(self, on):
        self.settings["sound"] = bool(on)
        self.save_settings()

    def save_settings(self):
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
                "time": b.us_time, "day": b.day_quote, "night": b.night, **ind,
                "mute": b.symb in self.settings["mute"],
                "mtf": self.mtf(b, ind["rsi"]),
                "zone": al.level_of(ind["rsi"], self.lines(b.symb)) if ind["rsi"] is not None else ("neutral", ""),
                "lines": self.lines(b.symb)._asdict(),
                "rearm": bool(g and not all(g.armed.values())),
                "last_alert": self.last_alert(b.symb),
                "last_signal": self.last_signal(b.symb),
                "bar": bar and {"time": epoch(bar["time_us"]), "open": bar["open"],
                                "high": bar["high"], "low": bar["low"], "close": bar["close"]}}

    def mtf(self, b, rsi_main):
        """{분: RSI}. 아직 못 받은 시간봉은 None."""
        tf = self.tf.get(b.symb, {})
        out = {}
        for m in MTF:
            if m == self.nmin:
                out[m] = rsi_main
            elif m in tf:
                out[m] = k.rsi_series([x["close"] for x in tf[m].bars[-400:]], self.period)[-1]
            else:
                out[m] = None
        return out

    def state(self):
        return {"status": self.status, "nmin": self.nmin, "period": self.period,
                "lower": al.LOWER, "upper": al.UPPER,
                "strong_lower": al.STRONG_LOWER, "strong_upper": al.STRONG_UPPER,
                "max": MAX_TICKERS, "sound": self.settings["sound"], "markets": self.markets,
                "signal_sound": self.settings["signal_sound"],
                "sound_sessions": self.settings["sound_sessions"], "quiet": self.settings["quiet"],
                "session_names": SOUND_SESSIONS, "holidays": self.holidays(),
                "telegram": {"ready": self.tg.ready, "on": self.settings["telegram"], "error": self.tg.last_error},
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

    async def push_after(self, changed):
        if changed:
            await self.broadcast({"type": "after", "events": [
                {"ts": e["ts"], "symb": e["symb"], "after": e["after"]} for e in changed]})

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
            if closed:
                await self.push_after(self.fill_after(closed))
                self.store_closed(closed)
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


@app.get("/api/scores")
def api_scores(symb: str = ""):
    """실제로 울린 알림의 성적. symb 를 주면 그 종목만."""
    return {"symb": symb.upper(), "horizons": list(AFTER), "rows": Hub.scores(symb.upper() or None)}


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


@app.post("/api/night")
async def api_night(symb: str = Body(...), on: bool = Body(...)):
    try:
        await hub.set_night(symb.upper(), on)
    except KeyError:
        raise HTTPException(404, symb)
    return hub.books[symb.upper()].night


@app.post("/api/mute")
async def api_mute(symb: str = Body(...), on: bool = Body(...)):
    """그 종목만 소리 끄기 (on=true 면 끈다). 알림 기록에는 「🔇 종목 소리 끔」으로 남는다."""
    try:
        await hub.set_mute(symb.upper(), on)
    except KeyError:
        raise HTTPException(404, symb)
    return {"symb": symb.upper(), "mute": on}


@app.post("/api/lines")
async def api_lines(symb: str = Body(...), lines: list = Body(None)):
    """종목 RSI 선. lines 는 [강한 아래, 아래, 위, 강한 위] 나 [아래, 위]. null 이면 기본(30·35·65·70)으로."""
    try:
        ln = await hub.set_lines(symb.upper(), lines)
    except KeyError:
        raise HTTPException(404, symb)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"symb": symb.upper(), "lines": ln._asdict()}


@app.post("/api/sound-when")
def api_sound_when(sessions: dict = Body(None), quiet: dict = Body(None)):
    """소리 나는 때. sessions 는 {"day": true, ...}, quiet 는 {"on", "from": "HH:MM", "to": "HH:MM"}."""
    if sessions:
        for key, on in sessions.items():
            if key in SOUND_SESSIONS:
                hub.settings["sound_sessions"][key] = bool(on)
    if quiet:
        q = hub.settings["quiet"]
        for f in ("from", "to"):
            v = quiet.get(f)
            if v is not None:
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                    raise HTTPException(400, f"{v}: HH:MM 으로 적을 것")
                q[f] = v
        if "on" in quiet:
            q["on"] = bool(quiet["on"])
    hub.save_settings()
    return {"sound_sessions": hub.settings["sound_sessions"], "quiet": hub.settings["quiet"]}


@app.post("/api/signal-sound")
def api_signal_sound(on: bool = Body(..., embed=True)):
    hub.settings["signal_sound"] = bool(on)
    hub.set_sound(hub.settings["sound"])   # 설정 파일에 같이 적는다
    return {"signal_sound": hub.settings["signal_sound"]}


@app.post("/api/telegram")
def api_telegram(on: bool = Body(..., embed=True)):
    hub.settings["telegram"] = bool(on)
    hub.save_settings()
    return {"on": hub.settings["telegram"], "ready": hub.tg.ready}


@app.post("/api/telegram/test")
async def api_telegram_test():
    try:
        await asyncio.to_thread(hub.tg.send_now, "RSI Monitor 시험 메시지 (웹 화면에서 보냄)")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/sound/test")
def api_sound_test(symb: str = Body("", embed=True)):
    """소리 시험. 그 종목의 65 초과 문장을 말머리와 함께 읽는다."""
    b = hub.books.get(symb.upper()) or next(iter(hub.books.values()), None)
    text = al.say_breach(b.name, b.symb, True, hub.lines(b.symb).upper) if b else "소리 시험"
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
    p.add_argument("--log", help="찍는 것을 이 파일에 덧붙인다 (창 없이 띄울 때)")
    args = p.parse_args()
    import socket
    import threading
    import uvicorn
    import webbrowser
    if args.log:
        log = Path(args.log)
        if log.exists() and log.stat().st_size > 5_000_000:   # 너무 커지면 새로
            log.replace(log.with_suffix(log.suffix + ".old"))
        sys.stdout = sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
        print(f"\n── {datetime.now():%Y-%m-%d %H:%M:%S} 시작", flush=True)
    url = f"http://localhost:{args.port}"
    # 이미 떠 있으면 새로 띄우지 않는다. 둘이 뜨면 앱키 하나의 실시간 연결을 서로 빼앗는다
    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", args.port)) == 0:
            print(f"이미 떠 있다: {url}", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return
    hub = Hub(args.tickers, args.min, args.period, args.tts_cache)
    k.load_keys()  # 키가 없으면 여기서 안내하고 끝낸다
    print(url, flush=True)
    if not args.no_browser:
        threading.Timer(2.0, webbrowser.open, (url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
