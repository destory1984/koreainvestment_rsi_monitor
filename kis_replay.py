#!/usr/bin/env python3
"""
kis_replay.py — 지난 며칠 분봉을 되감아 알림과 시그널을 채점한다.

선을 35/65·30/70 에 둘지, 시그널이 쓸 만한지 느낌이 아니라 숫자로 보려는 것이다.
webull_rsi_monitor 의 rsi_replay.py 와 같은 생각이다.

────────────────────────────────────────────────────────────
되감기는 진짜 규칙을 쓴다

알림은 kis_alert.Gate (선·재무장·쿨다운), 시그널은 kis_signal.signals 를 그대로 쓴다.
여기에 규칙을 다시 적지 않는다. 그래야 채점한 것이 실제로 도는 것과 같다.

실시간에서는 체결마다 RSI 를 새로 셈해 Gate 에 넣는다. 되감기에는 체결이 없으니
봉마다 저가·고가·종가 자리의 RSI 세 개를 넣는다. 양봉이면 저가→고가→종가,
음봉이면 고가→저가→종가 차례로 지나갔다고 본다. 봉 안에서 선을 잠깐 넘었다 돌아온 것도
잡히지만, 차례가 실제와 다를 수는 있다.

────────────────────────────────────────────────────────────
채점

들어간 값은 알림·시그널이 난 봉의 종가, 나온 값은 그로부터 15/30/60분 뒤 봉의 종가다.
그 시각 뒤 10분 안에 봉이 없으면 (장이 닫혔거나 거래가 끊겼으면) 채점하지 않는다.

「맞음」은 RSI 가 말한 쪽으로 갔는지다. 35·30 미만 알림과 매수 시그널은 오르면 맞음,
65·70 초과 알림과 매도 시그널은 내리면 맞음. 값이 그대로(보합)인 것은 빼고 센다 — 국내 종목은 호가 단위가
커서 15분 뒤 그대로인 것이 삼성전자는 22% 나 된다. 보합 비율은 「보합」 칸에 따로 보인다. 「수익」도 같은 쪽으로 샀다고 치고 셈한다
(초과·매도는 부호를 뒤집는다).

「기준」은 같은 종목·같은 기간 아무 봉에서나 같은 쪽으로 들어갔을 때의 평균 수익이다.
그냥 흐름을 탄 것인지 보려고 둔다. 「초과」 = 수익 - 기준. 0 근처면 값어치가 없다.

────────────────────────────────────────────────────────────
데이터

한국투자증권 해외주식 분봉을 120개씩 거슬러 받는다 (정규장·프리·애프터).
받은 것은 replay_cache/bars.db (SQLite) 에 쌓아 두고 다음에 새로 받은 것과 합친다.
DB 로 둔 까닭: 30분마다 도는 --collect 와 손으로 돌리는 되감기가 겹쳐도 안전하게 쓰고,
쌓인 기간이 길어져도 새 봉만 넣으니 쓰는 시간이 늘지 않는다 (6개월 1종목 2.4MB, 봉 12개 넣기 몇 ms).
예전 JSON 캐시(replay_cache/*.json)가 있으면 처음 열 때 DB 로 옮기고 원본은 replay_cache/json_backup/ 에 둔다.
주간거래(한국 낮) 분봉은 한국투자증권이 지금 세션 것만 주므로, 되감을 때마다 쌓아야
날이 갈수록 오버나이트 알림도 채점할 수 있다. 웹 화면과 같은 규칙으로 오버나이트 봉을
넣을 종목만 넣는다 (최근 오버나이트 5분 칸 절반 넘게 체결, 또는 kis_settings.json 의 "night").

국내 종목은 1분봉(FHKST03010230)을 거슬러 받아 묶는다. 한 달 넘게 거슬러 받아진다 (3주에 25초쯤).
웹 서버와 같이 KRX 정규장(09:00~15:30)만 쓴다. 분봉에 같이 오는 넥스트레이드 애프터(15:30~20:00)는
서버 실시간에 없어 뺀다 (예전에 쌓아 둔 캐시에 있어도 뺀다).

────────────────────────────────────────────────────────────
쓰는 법

  python kis_replay.py                   종목 목록(kis_watchlist.json) 전체, 8거래일
  python kis_replay.py TSLA SOXL --days 15
  python kis_replay.py 005930 000660     국내 종목 (6자리)
  python kis_replay.py --by 세션          정규·프리·애프터·주간 따로
  python kis_replay.py --by 종목
  python kis_replay.py --list            하나씩 다 보기
  python kis_replay.py --csv scored.csv  엑셀로 볼 것
  python kis_replay.py --offline         새로 받지 않고 쌓아 둔 것만
  python kis_replay.py --collect         주간거래 분봉만 받아 쌓기 (30분마다 돌리는 용도)

────────────────────────────────────────────────────────────
--collect

한국투자증권은 주간거래 분봉을 지금 세션 것만 준다. 세션이 끝나면 못 받으니 세션 동안 틈틈이
받아 둬야 한다. PC 가 언제 켜져 있을지 모르니 30분마다 돌리고, 스스로 가린다:
  - 주간거래 중이거나 끝난 지 1시간 안이 아니면 바로 끝낸다
  - 종목마다 쌓아 둔 주간거래 봉이 30분 안의 것이면 건너뛴다
받은 봉은 replay_cache/bars.db 에 넣고, 한 줄씩 replay_cache/collect.log 에 남긴다.
"""
import argparse
import csv
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import kis_alert as al
import kis_rsi as k
import kis_signal as ks

try:
    sys.stdout.reconfigure(encoding="utf-8")   # 윈도우 콘솔 cp949 에서 기호가 깨지지 않게
except Exception:
    pass

HERE = Path(__file__).parent
CACHE = HERE / "replay_cache"
DB = CACHE / "bars.db"
WATCHLIST = HERE / "kis_watchlist.json"
SETTINGS = HERE / "kis_settings.json"
HORIZONS = (15, 30, 60)
SLACK_MIN = 10          # 나온 값 봉이 목표 시각에서 이만큼 늦어도 받아 준다
WARMUP = 30             # 앞쪽 이만큼 봉은 RSI 가 자리 잡는 중이라 알림을 세지 않는다
BARS_PER_DAY = 16 * 12  # 미국 04:00~20:00 5분봉
KR_BARS_PER_DAY = 11 * 12 + 1   # 국내 09:00~20:00 5분봉 (정규 + 넥스트레이드 애프터)


def ts(t):
    return datetime.strptime(t, "%Y%m%d %H%M%S")


def session(t, kr=False):
    h = t.hour + t.minute / 60
    if kr:
        return "정규"
    if h >= 20 or h < 4:
        return "주간"
    if h < 9.5:
        return "프리"
    if h < 16:
        return "정규"
    return "애프터"


# ── 데이터 ────────────────────────────────────────────────────
def fetch_history(appkey, secret, excd, symb, nmin, days):
    """정규 쪽 분봉을 days 거래일 어치 거슬러 받는다."""
    bars, keyb = {}, ""
    for _ in range(days * BARS_PER_DAY // 119 + 1):
        page = k.fetch_bars(appkey, secret, excd, symb, nmin, keyb=keyb)
        new = [b for b in page if b["time_us"] not in bars]
        if not new:
            break
        bars.update({b["time_us"]: b for b in page})
        keyb = page[0]["time_us"].replace(" ", "")
        time.sleep(0.12)
    return bars


# ── 저장 (SQLite) ─────────────────────────────────────────────
BAR_FIELDS = ("open", "high", "low", "close", "volume")


def db(path=None):
    """분봉 DB 를 연다. 다른 프로세스가 쓰는 중이면 30초까지 기다린다."""
    path = path or DB
    path.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("pragma journal_mode=wal")   # 읽는 동안에도 다른 쪽이 쓸 수 있게
    con.executescript("""
        create table if not exists bars (
            excd text, symb text, nmin int, t text,          -- t 는 현지 시각 'YYYYMMDD HHMMSS'
            open real, high real, low real, close real, volume int,
            primary key (excd, symb, nmin, t)) without rowid;
        create table if not exists tickers (
            excd text, symb text, nmin int, night int,       -- night: 오버나이트 봉을 되감기에 넣을지
            primary key (excd, symb, nmin));
    """)
    if path == DB:
        migrate(con)
    return con


def put_bars(con, excd, symb, nmin, bars, replace=True):
    """봉들을 넣는다. replace 가 아니면 이미 있는 봉은 두고 없는 것만. 새로 생긴 봉 수를 돌려준다."""
    rows = [(excd, symb, nmin, b["time_us"], *(b[f] for f in BAR_FIELDS)) for b in bars]
    if not rows:
        return 0
    times = {r[3] for r in rows}
    have = {t for (t,) in con.execute("select t from bars where excd=? and symb=? and nmin=? and t between ? and ?",
                                      (excd, symb, nmin, min(times), max(times)))}
    verb = "insert or replace" if replace else "insert or ignore"
    con.executemany(f"{verb} into bars values (?,?,?,?,?,?,?,?,?)", rows)
    return len(times - have)


def get_bars(con, excd, symb, nmin, before=None, days=None):
    """쌓아 둔 봉 (오래된 것부터). before 를 주면 그 시각보다 앞선 것만, days 를 주면 그중 마지막 days 날짜만."""
    key = (excd, symb, nmin)
    where, args = "excd=? and symb=? and nmin=?", key
    if before:
        where, args = where + " and t<?", args + (before,)
    if days:
        dates = con.execute(f"select distinct substr(t, 1, 8) d from bars where {where} order by d desc limit ?",
                            args + (days,)).fetchall()
        if not dates:
            return []
        where, args = where + " and t>=?", args + (dates[-1][0],)
    cur = con.execute(f"select t, open, high, low, close, volume from bars where {where} order by t", args)
    return [dict(zip(("time_us",) + BAR_FIELDS, r)) for r in cur]


def get_night(con, excd, symb, nmin):
    r = con.execute("select night from tickers where excd=? and symb=? and nmin=?", (excd, symb, nmin)).fetchone()
    return bool(r and r[0])


def set_night(con, excd, symb, nmin, on):
    con.execute("insert or replace into tickers values (?,?,?,?)", (excd, symb, nmin, int(bool(on))))


def find_excd(con, symb, nmin):
    """쌓아 둔 것에서 종목의 거래소를 찾는다 (--offline 에서 'TSLA' 처럼 줬을 때)."""
    r = con.execute("select excd from bars where symb=? and nmin=? limit 1", (symb, nmin)).fetchone()
    return r and r[0]


def migrate(con):
    """예전 JSON 캐시를 DB 로 옮기고 원본은 json_backup/ 으로 치운다 (지우지 않는다)."""
    old = sorted(CACHE.glob("*_*_*.json"))
    if not old:
        return
    backup = CACHE / "json_backup"
    backup.mkdir(exist_ok=True)
    n = 0
    for path in old:
        excd, *mid, nmin = path.stem.split("_")
        symb = "_".join(mid)
        cached = json.loads(path.read_text(encoding="utf-8"))
        n += put_bars(con, excd, symb, int(nmin), cached["bars"].values())
        set_night(con, excd, symb, int(nmin), cached.get("night"))
        con.commit()
        path.rename(backup / path.name)
    print(f"replay_cache: JSON {len(old)}개(봉 {n}개)를 bars.db 로 옮겼다. 원본은 json_backup/ 에 있다.", flush=True)


def night_on(appkey, secret, excd, symb, nmin):
    """웹 화면과 같은 규칙: 오버나이트 봉을 넣을지와, 지금 세션의 주간거래 분봉."""
    day, have, of = k.fetch_night(appkey, secret, excd, symb, nmin)
    try:
        override = json.loads(SETTINGS.read_text(encoding="utf-8")).get("night", {})
    except Exception:
        override = {}
    return override.get(symb, have > of * k.NIGHT_SHARE), day


def collect(tickers, nmin, fresh_min=30, grace_min=60):
    """주간거래 분봉만 받아 쌓는다. 받을 때가 아니거나 이미 최신이면 건너뛴다."""
    now = datetime.now(k.NEW_YORK)
    in_session = k.us_day_session(now) or k.us_day_session(now - timedelta(minutes=grace_min))
    log = []
    if not in_session:
        log.append("주간거래 시간이 아님")
    else:
        appkey, secret = k.load_keys()
        con = db()
        cutoff = (now - timedelta(minutes=fresh_min)).strftime("%Y%m%d %H%M%S")
        for t in tickers:
            excd, symb = k.resolve(appkey, secret, t)
            if excd not in k.DAY_EXCD:
                continue
            recent = con.execute("select t from bars where excd=? and symb=? and nmin=? and t>=?",
                                 (excd, symb, nmin, cutoff)).fetchall()
            if any(session(ts(x)) == "주간" for (x,) in recent):
                log.append(f"{symb} 최신")
                continue
            try:
                on, day = night_on(appkey, secret, excd, symb, nmin)
            except Exception as e:
                log.append(f"{symb} 실패 {e}")
                continue
            new = put_bars(con, excd, symb, nmin, day)
            set_night(con, excd, symb, nmin, on)
            con.commit()
            log.append(f"{symb} +{new}")
            time.sleep(0.12)
        con.close()
    line = f"{datetime.now():%Y-%m-%d %H:%M} (미국 {now:%m-%d %H:%M}) " + ", ".join(log)
    CACHE.mkdir(exist_ok=True)
    with open(CACHE / "collect.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def load_bars(con, appkey, secret, excd, symb, nmin, days, offline):
    """쌓아 둔 것과 새로 받은 것을 합친다. 오버나이트 봉을 안 넣는 종목이면 빼고 돌려준다."""
    # 다 받은 뒤에 한 번에 쓴다. 쓰는 동안은 DB 가 잠기니 그 사이에 네트워크를 기다리지 않는다
    if not offline and excd == "KRX":
        got = k.fetch_kr_bars(appkey, secret, symb, nmin, need=days * KR_BARS_PER_DAY)
        put_bars(con, excd, symb, nmin, got)
        con.commit()
    elif not offline:
        hist = fetch_history(appkey, secret, excd, symb, nmin, days)
        night, day = night_on(appkey, secret, excd, symb, nmin)
        put_bars(con, excd, symb, nmin, hist.values())
        put_bars(con, excd, symb, nmin, day, replace=False)   # 정규 쪽에서 받은 봉이 먼저
        set_night(con, excd, symb, nmin, night)
        con.commit()
    bars = get_bars(con, excd, symb, nmin)
    if excd == "KRX":
        return [b for b in bars if b["time_us"][9:] <= k.KRX_CLOSE], False
    night = get_night(con, excd, symb, nmin)
    if not night:
        bars = [b for b in bars if session(ts(b["time_us"])) != "주간"]
    return bars, night


# ── 되감기 ────────────────────────────────────────────────────
def replay_alerts(bars, period):
    """봉마다 저가·고가·종가 RSI 를 Gate 에 흘려 넣고, 울렸을 알림을 모은다 (억제된 것은 뺀다)."""
    gate, out = al.Gate(), []
    for i, (b, (rc, rlo, rhi)) in enumerate(zip(bars, ks.rsi_band(bars, period))):
        if rc is None:
            continue
        path = (rlo, rhi, rc) if b["close"] >= b["open"] else (rhi, rlo, rc)
        now = ts(b["time_us"]).timestamp()
        for v in path:
            a = gate.check(v, now)
            if a and not a.get("suppressed") and not a["start"] and i >= WARMUP:
                name = f"{al._num(a['edge'])} {'초과' if a['zone'] == 'above' else '미만'}"
                out.append({"i": i, "kind": "알림 " + name, "up": a["zone"] == "below", "rsi": v})
    return out


def replay_signals(bars, period):
    """닫힌 봉 전체로 시그널을 다시 낸다. 실시간과 같이 진행 중인 마지막 봉은 뺀다."""
    at = {b["time_us"]: i for i, b in enumerate(bars)}
    return [{"i": at[x.bar], "kind": f"{x.word} {x.grade}", "up": x.side == "buy", "rsi": x.rsi}
            for x in ks.signals(bars[:-1], period)]


def exit_index(bars, i, minutes):
    """i 번 봉에서 minutes 뒤의 봉. 그 뒤 SLACK_MIN 안에 없으면 None."""
    t0 = ts(bars[i]["time_us"])
    target, late = t0 + timedelta(minutes=minutes), t0 + timedelta(minutes=minutes + SLACK_MIN)
    for j in range(i + 1, len(bars)):
        t = ts(bars[j]["time_us"])
        if t >= target:
            return j if t <= late else None
    return None


def ret(bars, i, j, up):
    r = (bars[j]["close"] / bars[i]["close"] - 1) * 100
    return r if up else 0.0 - r   # 보합이 -0.00 으로 찍히지 않게


def score(symb, bars, events, horizons, kr=False):
    rows = []
    for e in events:
        t = ts(bars[e["i"]]["time_us"])
        row = {"symb": symb, "time": t, "session": session(t, kr), "kind": e["kind"], "up": e["up"],
               "rsi": e["rsi"], "price": bars[e["i"]]["close"]}
        for h in horizons:
            j = exit_index(bars, e["i"], h)
            row[h] = None if j is None else ret(bars, e["i"], j, e["up"])
        rows.append(row)
    return rows


def baseline(bars, horizons, kr=False):
    """세션마다, 아무 봉에서나 샀을 때 h 분 뒤 평균 수익 (오른 쪽 기준). 파는 쪽은 부호만 뒤집는다."""
    acc = defaultdict(list)
    for i in range(WARMUP, len(bars)):
        s = session(ts(bars[i]["time_us"]), kr)
        for h in horizons:
            j = exit_index(bars, i, h)
            if j is not None:
                acc[(s, h)].append(ret(bars, i, j, True))
    return {key: sum(v) / len(v) for key, v in acc.items()}


# ── 보여 주기 ─────────────────────────────────────────────────
KIND_ORDER = ["알림 30 미만", "알림 35 미만", "알림 65 초과", "알림 70 초과",
              "매수 강", "매수 약", "매도 강", "매도 약"]


def summarize(rows, bases, horizon, by):
    groups = defaultdict(list)
    for r in rows:
        groups[(r[by] if by else "", r["kind"])].append(r)
    out = []
    for (g, kind), rs in groups.items():
        got = [r for r in rs if r[horizon] is not None]
        base = [bases[r["symb"]].get((r["session"], horizon)) for r in got]
        base = [b if r["up"] else -b for b, r in zip(base, got) if b is not None]
        avg = sum(r[horizon] for r in got) / len(got) if got else None
        bavg = sum(base) / len(base) if base else None
        moved = [r for r in got if r[horizon] != 0]
        out.append({"group": g, "kind": kind, "n": len(rs), "scored": len(got),
                    "hit": sum(r[horizon] > 0 for r in moved) / len(moved) if moved else None,
                    "flat": (len(got) - len(moved)) / len(got) if got else None,
                    "avg": avg, "base": bavg,
                    "excess": None if avg is None or bavg is None else avg - bavg})
    order = {kd: n for n, kd in enumerate(KIND_ORDER)}
    return sorted(out, key=lambda x: (str(x["group"]), order.get(x["kind"], 99)))


def pct(x, width=7, sign=True):
    return " " * (width - 1) + "-" if x is None else f"{x:>+{width}.2f}" if sign else f"{x * 100:>{width - 1}.0f}%"


def show(rows, bases, horizons, by, label=""):
    for h in horizons:
        print(f"\n■ {h}분 뒤" + (f" — {label}별" if label else ""))
        print(f"{'':8}{'종류':<12}{'건수':>5}{'채점':>5}{'맞음':>7}{'보합':>6}{'수익%':>8}{'기준%':>8}{'초과%':>8}")
        for s in summarize(rows, bases, h, by):
            print(f"{str(s['group']):8}{s['kind']:<12}{s['n']:>5}{s['scored']:>5}"
                  f"{pct(s['hit'], 7, False)}{pct(s['flat'], 6, False)}{pct(s['avg'], 8)}{pct(s['base'], 8)}{pct(s['excess'], 8)}")


def show_list(rows, horizons):
    for r in sorted(rows, key=lambda r: r["time"]):
        hs = "  ".join(f"{h}분 {pct(r[h], 6)}" for h in horizons)
        price = f"{r['price']:>9,.0f}" if r["symb"].isdigit() else k.px(r["price"], 9)
        print(f"{r['time']:%m-%d %H:%M} {r['session']:<3} {r['symb']:<6} {r['kind']:<10} "
              f"RSI {r['rsi']:5.1f}  {price}  {hs}")


def write_csv(rows, path, horizons):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["시각(현지)", "세션", "종목", "종류", "RSI", "가격"] + [f"{h}분%" for h in horizons])
        for r in sorted(rows, key=lambda r: r["time"]):
            w.writerow([f"{r['time']:%Y-%m-%d %H:%M}", r["session"], r["symb"], r["kind"],
                        f"{r['rsi']:.2f}", r["price"]] + ["" if r[h] is None else f"{r[h]:.3f}" for h in horizons])


def main():
    ap = argparse.ArgumentParser(description="지난 분봉을 되감아 알림·시그널을 채점한다")
    ap.add_argument("tickers", nargs="*", help="없으면 kis_watchlist.json 의 종목 전부 (국내 포함)")
    ap.add_argument("--days", type=int, default=8, help="거슬러 받을 거래일 (기본 8)")
    ap.add_argument("--min", type=int, default=5, help="분봉 (기본 5)")
    ap.add_argument("--period", type=int, default=14, help="RSI 기간 (기본 14)")
    ap.add_argument("--by", choices=["세션", "종목"], help="이것별로 나눠 보기")
    ap.add_argument("--list", action="store_true", help="하나씩 다 보기")
    ap.add_argument("--csv", help="채점 결과를 이 파일에 적기")
    ap.add_argument("--offline", action="store_true", help="새로 받지 않고 replay_cache 만 쓰기")
    ap.add_argument("--collect", action="store_true", help="주간거래 분봉만 받아 쌓고 끝내기")
    args = ap.parse_args()

    tickers = args.tickers or json.loads(WATCHLIST.read_text(encoding="utf-8"))
    if args.collect:
        return collect(tickers, args.min)
    appkey = secret = None
    if not args.offline:
        appkey, secret = k.load_keys()
    rows, bases = [], {}
    con = db()
    for t in tickers:
        if args.offline and ":" not in t:
            excd, symb = find_excd(con, t.upper(), args.min), t.upper()
            if not excd:
                print(f"{t}: 쌓아 둔 것 없음")
                continue
        else:
            excd, symb = k.resolve(appkey, secret, t)
        try:
            bars, night = load_bars(con, appkey, secret, excd, symb, args.min, args.days, args.offline)
        except Exception as e:
            print(f"{symb}: 건너뜀 — {e}")
            continue
        events = replay_alerts(bars, args.period) + replay_signals(bars, args.period)
        kr = excd == "KRX"
        rows += score(symb, bars, events, HORIZONS, kr)
        bases[symb] = baseline(bars, HORIZONS, kr)
        extra = k.KR_INFO.get(symb, {}).get("name", "국내") if kr else f"오버나이트 {'넣음' if night else '뺌'}"
        print(f"{symb:<6} 봉 {len(bars):>5}  {bars[0]['time_us'][:8]}~{bars[-1]['time_us'][:8]}"
              f"  {extra}  알림·시그널 {len(events)}")
    if not rows:
        sys.exit("채점할 것이 없다.")
    by = {"세션": "session", "종목": "symb"}.get(args.by)
    if args.list:
        show_list(rows, HORIZONS)
    show(rows, bases, HORIZONS, by, args.by)
    if args.csv:
        write_csv(rows, args.csv, HORIZONS)
        print(f"\n{args.csv} 에 {len(rows)}줄 적었다")


if __name__ == "__main__":
    main()
