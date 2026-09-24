"""한국투자증권 Open API 로 미국주식 분봉과 실시간 체결가를 받아온다.

    python kis_us.py bars TSLA              # 5분봉 (오늘+전일, 최근 120개)
    python kis_us.py bars NYS:BE --min 1    # 거래소를 직접 적을 수도 있다
    python kis_us.py live TSLA SOXL         # 실시간 체결가 (Ctrl+C 로 끝)
    python kis_us.py rsi TSLA SOXL          # 5분봉 RSI(14)·MACD 를 실시간으로 (줄 단위)
    python kis_us.py watch TSLA SOXL        # 같은 것을 표 하나로

키는 kis_config.json (저장소에 안 올라감) 이나 환경변수 KIS_APPKEY / KIS_APPSECRET 에 둔다.
    {"appkey": "...", "appsecret": "..."}
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Git Bash 는 파이프로 붙어서 cp949 로 찍히면 한글이 깨진다
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
CONFIG = HERE / "kis_config.json"
TOKEN_CACHE = HERE / "kis_token.json"
EXCHANGE_CACHE = HERE / "kis_exchange.json"

REST = "https://openapi.koreainvestment.com:9443"
WS = "ws://ops.koreainvestment.com:21000/tryitout"
EXCHANGES = ("NAS", "NYS", "AMS")

# HDFSCNT0 (해외주식 실시간체결가) 필드 순서
LIVE_FIELDS = ["RSYM", "SYMB","ZDIV", "TYMD", "XYMD", "XHMS", "KYMD", "KHMS", "OPEN", "HIGH",
               "LOW", "LAST", "SIGN", "DIFF", "RATE", "PBID", "PASK", "VBID", "VASK",
               "EVOL", "TVOL", "TAMT", "BIVL", "ASVL", "STRN", "MTYP"]


def load_keys():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    appkey = os.environ.get("KIS_APPKEY") or cfg.get("appkey")
    secret = os.environ.get("KIS_APPSECRET") or cfg.get("appsecret")
    if not appkey or not secret:
        sys.exit(f"앱키가 없다. {CONFIG.name} 에 appkey/appsecret 을 적거나 환경변수를 설정할 것.")
    return appkey, secret


def get_token(appkey, secret):
    """접근토큰은 24시간 유효하고 1분에 한 번만 새로 받을 수 있어서 파일에 모아 둔다."""
    if TOKEN_CACHE.exists():
        c = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        if c.get("appkey") == appkey and c["expires"] > time.time() + 600:
            return c["token"]
    r = requests.post(f"{REST}/oauth2/tokenP", json={
        "grant_type": "client_credentials", "appkey": appkey, "appsecret": secret}, timeout=10)
    r.raise_for_status()
    j = r.json()
    TOKEN_CACHE.write_text(json.dumps({
        "appkey": appkey, "token": j["access_token"],
        "expires": time.time() + int(j.get("expires_in", 86400))}), encoding="utf-8")
    return j["access_token"]


def get_approval_key(appkey, secret):
    r = requests.post(f"{REST}/oauth2/Approval", json={
        "grant_type": "client_credentials", "appkey": appkey, "secretkey": secret}, timeout=10)
    r.raise_for_status()
    return r.json()["approval_key"]


def fetch_bars(appkey, secret, excd, symb, nmin=5, pinc=True, nrec=120):
    """해외주식분봉조회 (HHDFS76950200). 최신 것부터 온다."""
    token = get_token(appkey, secret)
    r = requests.get(f"{REST}/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
                     headers={"authorization": f"Bearer {token}", "appkey": appkey,
                              "appsecret": secret, "tr_id": "HHDFS76950200", "custtype": "P"},
                     params={"AUTH": "", "EXCD": excd, "SYMB": symb, "NMIN": str(nmin),
                             "PINC": "1" if pinc else "0", "NEXT": "", "NREC": str(nrec),
                             "FILL": "", "KEYB": ""}, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("rt_cd") != "0":
        raise RuntimeError(f"{excd}:{symb} {j.get('msg_cd')} {j.get('msg1')}")
    bars = []
    for b in j.get("output2") or []:
        if not b.get("last"):
            continue
        bars.append({
            "time_us": f"{b['xymd']} {b['xhms']}", "time_kr": f"{b['kymd']} {b['khms']}",
            "open": float(b["open"]), "high": float(b["high"]), "low": float(b["low"]),
            "close": float(b["last"]), "volume": int(b["evol"] or 0)})
    return list(reversed(bars))


def resolve(appkey, secret, ticker):
    """'NYS:BE' 는 그대로, 'BE' 는 나스닥→뉴욕→아멕스 순으로 찾아서 기억해 둔다."""
    if ":" in ticker:
        excd, symb = ticker.upper().split(":", 1)
        return excd, symb
    symb = ticker.upper()
    cache = json.loads(EXCHANGE_CACHE.read_text(encoding="utf-8")) if EXCHANGE_CACHE.exists() else {}
    if symb in cache:
        return cache[symb], symb
    for excd in EXCHANGES:
        try:
            if fetch_bars(appkey, secret, excd, symb, nrec=1):
                cache[symb] = excd
                EXCHANGE_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
                return excd, symb
        except RuntimeError:
            pass
        time.sleep(0.1)
    sys.exit(f"{symb}: 어느 거래소에서도 못 찾았다. NAS:{symb} 처럼 직접 적을 것.")


def cmd_bars(args):
    appkey, secret = load_keys()
    for t in args.tickers:
        excd, symb = resolve(appkey, secret, t)
        bars = fetch_bars(appkey, secret, excd, symb, args.min, pinc=not args.today)
        print(f"\n{excd}:{symb} {args.min}분봉 {len(bars)}개 (미국시각)")
        for b in bars[-args.n:]:
            print(f"  {b['time_us']}  O {b['open']:>9.4f}  H {b['high']:>9.4f}  "
                  f"L {b['low']:>9.4f}  C {b['close']:>9.4f}  V {b['volume']:>9}")
        time.sleep(0.1)


def rsi_series(closes, period=14):
    """와일더 RSI. 처음 period 개는 산술평균, 그 뒤로는 (앞 평균*(period-1) + 이번 값)/period."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    ups = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    downs = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    au, ad = sum(ups[:period]) / period, sum(downs[:period]) / period
    for i in range(period, len(ups) + 1):
        if i > period:
            au = (au * (period - 1) + ups[i - 1]) / period
            ad = (ad * (period - 1) + downs[i - 1]) / period
        out[i] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def bar_start(ymd, hms, nmin):
    """미국 시각 체결 시점을 그 체결이 들어갈 봉의 시작 시각('YYYYMMDD HHMMSS')으로."""
    h, m = int(hms[:2]), int(hms[2:4])
    m -= m % nmin
    return f"{ymd} {h:02d}{m:02d}00"


def print_tick(d):
    print(f"{datetime.now():%H:%M:%S}  {d['SYMB']:<6} {float(d['LAST']):>10.4f}  "
          f"{float(d['RATE']):>+6.2f}%  체결 {d['EVOL']:>6}  누적 {d['TVOL']}  "
          f"(미국 {d['XHMS']})", flush=True)


async def live(approval_key, keys, on_tick=print_tick):
    import websockets
    async with websockets.connect(WS, ping_interval=None) as ws:
        for k in keys:
            await ws.send(json.dumps({
                "header": {"approval_key": approval_key, "custtype": "P",
                           "tr_type": "1", "content-type": "utf-8"},
                "body": {"input": {"tr_id": "HDFSCNT0", "tr_key": k}}}))
        async for msg in ws:
            if msg[0] in "01":  # 데이터: 암호화|TR|건수|필드^필드^...
                _, tr_id, count, data = msg.split("|", 3)
                vals = data.split("^")
                n = len(LIVE_FIELDS)
                for i in range(int(count)):
                    on_tick(dict(zip(LIVE_FIELDS, vals[i * n:(i + 1) * n])))
                continue
            j = json.loads(msg)
            if j["header"]["tr_id"] == "PINGPONG":
                await ws.send(msg)
            else:
                body = j.get("body", {})
                if "ALREADY IN USE" in (body.get("msg1") or ""):
                    raise SystemExit("이 앱키로 실시간 연결이 이미 열려 있다. 앱키 하나에 연결은 하나뿐이니 "
                                     "다른 창의 kis_us.py 를 끄고 다시 할 것.")
                print(f"[{j['header'].get('tr_key')}] {body.get('msg1')}", flush=True)


def cmd_live(args):
    appkey, secret = load_keys()
    keys = []
    for t in args.tickers:
        excd, symb = resolve(appkey, secret, t)
        keys.append(f"{args.prefix}{excd}{symb}")
    print("구독:", ", ".join(keys))
    try:
        asyncio.run(live(get_approval_key(appkey, secret), keys))
    except KeyboardInterrupt:
        pass


def ema_series(values, n):
    """지수이동평균. 첫 값에서 시작한다 (Webull 과 같다)."""
    k, out, e = 2 / (n + 1), [], None
    for v in values:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def macd_series(closes, fast=12, slow=26, signal=9):
    """MACD 선 = EMA(fast) - EMA(slow), 시그널 = MACD 의 EMA(signal), 히스토그램 = 둘의 차."""
    line = [f - s for f, s in zip(ema_series(closes, fast), ema_series(closes, slow))]
    sig = ema_series(line, signal)
    return line, sig, [m - g for m, g in zip(line, sig)]


class Book:
    """한 종목의 분봉. 체결이 오면 진행 중인 봉을 고치고, 시각이 넘어가면 새 봉을 붙인다."""

    def __init__(self, excd, symb, bars, nmin):
        self.excd, self.symb, self.bars, self.nmin = excd, symb, bars, nmin
        self.price = bars[-1]["close"] if bars else None
        self.rate = None
        self.us_time = ""

    @property
    def key(self):
        return f"D{self.excd}{self.symb}"

    def on_tick(self, d):
        """체결 하나를 반영한다. 새 봉이 생기면 True."""
        price = float(d["LAST"])
        start = bar_start(d["XYMD"], d["XHMS"], self.nmin)
        new = not self.bars or start > self.bars[-1]["time_us"]
        if new:
            self.bars.append({"time_us": start, "open": price, "high": price, "low": price,
                              "close": price, "volume": 0})
        elif start < self.bars[-1]["time_us"]:
            return False  # 늦게 온 체결
        b = self.bars[-1]
        b["close"], b["high"], b["low"] = price, max(b["high"], price), min(b["low"], price)
        b["volume"] += int(d["EVOL"] or 0)
        self.price, self.rate, self.us_time = price, float(d["RATE"]), d["XHMS"]
        return new

    def indicators(self, period=14):
        closes = [b["close"] for b in self.bars[-400:]]
        line, sig, hist = macd_series(closes)
        return {"rsi": rsi_series(closes, period)[-1],
                "macd": line[-1], "signal": sig[-1], "hist": hist[-1]}


def load_books(tickers, nmin):
    appkey, secret = load_keys()
    books = {}
    for t in tickers:
        excd, symb = resolve(appkey, secret, t)
        books[symb] = Book(excd, symb, fetch_bars(appkey, secret, excd, symb, nmin), nmin)
        time.sleep(0.1)
    return appkey, secret, books


def run_live(appkey, secret, books, on_tick):
    def route(d):
        book = books.get(d["SYMB"])
        if book:
            on_tick(book, d)
    try:
        asyncio.run(live(get_approval_key(appkey, secret), [b.key for b in books.values()], route))
    except KeyboardInterrupt:
        pass


def fmt_ind(ind):
    rsi = "  -   " if ind["rsi"] is None else f"{ind['rsi']:6.2f}"
    return (f"RSI {rsi}  MACD {ind['macd']:+.4f}  시그널 {ind['signal']:+.4f}  "
            f"히스토 {ind['hist']:+.4f}")


def cmd_rsi(args):
    """종목마다 한 줄씩, RSI 가 --step 이상 바뀌거나 MACD 가 시그널을 건널 때 찍는다."""
    appkey, secret, books = load_books(args.tickers, args.min)
    for book in books.values():
        print(f"\n{book.excd}:{book.symb} {args.min}분봉  봉 {len(book.bars)}개 (미국시각)")
        closes = [b["close"] for b in book.bars]
        r = rsi_series(closes, args.period)
        line, sig, hist = macd_series(closes)
        for i in range(max(0, len(closes) - args.n), len(closes)):
            ind = {"rsi": r[i], "macd": line[i], "signal": sig[i], "hist": hist[i]}
            print(f"  {book.bars[i]['time_us']}  C {closes[i]:>9.4f}  {fmt_ind(ind)}")
    if args.once:
        return
    shown = {}

    def on_tick(book, d):
        if book.on_tick(d):
            print(f"--- {book.symb} 새 봉 {book.bars[-1]['time_us']}", flush=True)
        ind = book.indicators(args.period)
        last = shown.get(book.symb)
        if ind["rsi"] is None:
            return
        if last and abs(ind["rsi"] - last["rsi"]) < args.step and (ind["hist"] > 0) == (last["hist"] > 0):
            return
        shown[book.symb] = ind
        print(f"{datetime.now():%H:%M:%S}  {book.symb:<6} {book.price:>10.4f}  {fmt_ind(ind)}  "
              f"(미국 {book.us_time})", flush=True)

    print("\n구독:", ", ".join(b.key for b in books.values()))
    run_live(appkey, secret, books, on_tick)


def cmd_watch(args):
    """종목 전체를 표 하나로 띄워 두고 체결이 올 때마다 고친다."""
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table

    appkey, secret, books = load_books(args.tickers, args.min)

    def table():
        t = Table(title=f"{args.min}분봉 RSI({args.period}) · MACD(12,26,9)   "
                        f"{datetime.now():%H:%M:%S} KST", title_justify="left")
        for c in ("종목", "가격", "등락", "RSI", "MACD", "시그널", "히스토", "미국시각"):
            t.add_column(c, justify="left" if c == "종목" else "right")
        for b in books.values():
            ind = b.indicators(args.period)
            r = ind["rsi"]
            rsi = "-" if r is None else (f"[bold cyan]{r:.2f}[/]" if r <= args.lower else
                                         f"[bold red]{r:.2f}[/]" if r >= args.upper else f"{r:.2f}")
            hc = "green" if ind["hist"] > 0 else "red"
            rate = "" if b.rate is None else f"[{'green' if b.rate >= 0 else 'red'}]{b.rate:+.2f}%[/]"
            us = f"{b.us_time[:2]}:{b.us_time[2:4]}:{b.us_time[4:]}" if b.us_time else ""
            t.add_row(b.symb, f"{b.price:.4f}", rate, rsi, f"{ind['macd']:+.4f}",
                      f"{ind['signal']:+.4f}", f"[{hc}]{ind['hist']:+.4f}[/]", us)
        return t

    with Live(table(), console=Console(), refresh_per_second=2) as view:
        def on_tick(book, d):
            book.on_tick(d)
            view.update(table())
        run_live(appkey, secret, books, on_tick)


def main():
    p = argparse.ArgumentParser(description="한국투자증권 API 미국주식 분봉·실시간")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bars", help="분봉 조회")
    b.add_argument("tickers", nargs="+")
    b.add_argument("--min", type=int, default=5, help="분 단위 (기본 5)")
    b.add_argument("--today", action="store_true", help="전일 봉은 빼고 오늘 것만")
    b.add_argument("-n", type=int, default=20, help="출력할 봉 수 (기본 20)")
    b.set_defaults(func=cmd_bars)
    l = sub.add_parser("live", help="실시간 체결가")
    l.add_argument("tickers", nargs="+")
    l.add_argument("--prefix", default="D",
                   help="D=정규장·프리/애프터 (무료 실시간), R=주간거래 (R+BAQ 같은 거래소 코드 필요)")
    l.set_defaults(func=cmd_live)
    r = sub.add_parser("rsi", help="분봉 RSI·MACD + 실시간 갱신 (줄 단위)")
    r.add_argument("tickers", nargs="+")
    r.add_argument("--min", type=int, default=5, help="분 단위 (기본 5)")
    r.add_argument("--period", type=int, default=14, help="RSI 기간 (기본 14)")
    r.add_argument("-n", type=int, default=5, help="처음에 보여줄 봉 수 (기본 5)")
    r.add_argument("--step", type=float, default=0.1,
                   help="RSI 가 이만큼 바뀌어야 새로 찍는다 (기본 0.1)")
    r.add_argument("--once", action="store_true", help="분봉 값만 보고 끝내기")
    r.set_defaults(func=cmd_rsi)
    w = sub.add_parser("watch", help="RSI·MACD 표를 띄워 두고 실시간 갱신")
    w.add_argument("tickers", nargs="+")
    w.add_argument("--min", type=int, default=5, help="분 단위 (기본 5)")
    w.add_argument("--period", type=int, default=14, help="RSI 기간 (기본 14)")
    w.add_argument("--lower", type=float, default=30, help="이 아래면 RSI 를 파랗게 (기본 30)")
    w.add_argument("--upper", type=float, default=70, help="이 위면 RSI 를 빨갛게 (기본 70)")
    w.set_defaults(func=cmd_watch)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
