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
65·70 초과 알림과 매도 시그널은 내리면 맞음. 「수익」도 같은 쪽으로 샀다고 치고 셈한다
(초과·매도는 부호를 뒤집는다).

「기준」은 같은 종목·같은 기간 아무 봉에서나 같은 쪽으로 들어갔을 때의 평균 수익이다.
그냥 흐름을 탄 것인지 보려고 둔다. 「초과」 = 수익 - 기준. 0 근처면 값어치가 없다.

────────────────────────────────────────────────────────────
데이터

한국투자증권 해외주식 분봉을 120개씩 거슬러 받는다 (정규장·프리·애프터).
받은 것은 replay_cache/ 에 쌓아 두고 다음에 새로 받은 것과 합친다.
주간거래(한국 낮) 분봉은 한국투자증권이 지금 세션 것만 주므로, 되감을 때마다 쌓아야
날이 갈수록 오버나이트 알림도 채점할 수 있다. 웹 화면과 같은 규칙으로 오버나이트 봉을
넣을 종목만 넣는다 (최근 오버나이트 5분 칸 절반 넘게 체결, 또는 kis_settings.json 의 "night").
국내 종목은 아직 되감지 않는다.

────────────────────────────────────────────────────────────
쓰는 법

  python kis_replay.py                   종목 목록(kis_watchlist.json) 전체, 8거래일
  python kis_replay.py TSLA SOXL --days 15
  python kis_replay.py --by 세션          정규·프리·애프터·주간 따로
  python kis_replay.py --by 종목
  python kis_replay.py --list            하나씩 다 보기
  python kis_replay.py --csv scored.csv  엑셀로 볼 것
  python kis_replay.py --offline         새로 받지 않고 쌓아 둔 것만
"""
import argparse
import csv
import json
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
WATCHLIST = HERE / "kis_watchlist.json"
SETTINGS = HERE / "kis_settings.json"
HORIZONS = (15, 30, 60)
SLACK_MIN = 10          # 나온 값 봉이 목표 시각에서 이만큼 늦어도 받아 준다
WARMUP = 30             # 앞쪽 이만큼 봉은 RSI 가 자리 잡는 중이라 알림을 세지 않는다
BARS_PER_DAY = 16 * 12  # 미국 04:00~20:00 5분봉


def ts(t):
    return datetime.strptime(t, "%Y%m%d %H%M%S")


def session(t):
    h = t.hour + t.minute / 60
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


def night_on(appkey, secret, excd, symb, nmin):
    """웹 화면과 같은 규칙: 오버나이트 봉을 넣을지와, 지금 세션의 주간거래 분봉."""
    day, have, of = k.fetch_night(appkey, secret, excd, symb, nmin)
    try:
        override = json.loads(SETTINGS.read_text(encoding="utf-8")).get("night", {})
    except Exception:
        override = {}
    return override.get(symb, have > of * k.NIGHT_SHARE), day


def load_bars(appkey, secret, excd, symb, nmin, days, offline):
    """쌓아 둔 것과 새로 받은 것을 합친다. 오버나이트 봉을 안 넣는 종목이면 빼고 돌려준다."""
    path = CACHE / f"{excd}_{symb}_{nmin}.json"
    cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"bars": {}, "night": False}
    bars, night = cached["bars"], cached["night"]
    if not offline:
        bars.update(fetch_history(appkey, secret, excd, symb, nmin, days))
        night, day = night_on(appkey, secret, excd, symb, nmin)
        bars.update({b["time_us"]: b for b in day if b["time_us"] not in bars})
        CACHE.mkdir(exist_ok=True)
        path.write_text(json.dumps({"bars": bars, "night": night}), encoding="utf-8")
    out = [bars[t] for t in sorted(bars)]
    if not night:
        out = [b for b in out if session(ts(b["time_us"])) != "주간"]
    return out, night


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
    return r if up else -r


def score(symb, bars, events, horizons):
    rows = []
    for e in events:
        t = ts(bars[e["i"]]["time_us"])
        row = {"symb": symb, "time": t, "session": session(t), "kind": e["kind"], "up": e["up"],
               "rsi": e["rsi"], "price": bars[e["i"]]["close"]}
        for h in horizons:
            j = exit_index(bars, e["i"], h)
            row[h] = None if j is None else ret(bars, e["i"], j, e["up"])
        rows.append(row)
    return rows


def baseline(bars, horizons):
    """세션마다, 아무 봉에서나 샀을 때 h 분 뒤 평균 수익 (오른 쪽 기준). 파는 쪽은 부호만 뒤집는다."""
    acc = defaultdict(list)
    for i in range(WARMUP, len(bars)):
        s = session(ts(bars[i]["time_us"]))
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
        out.append({"group": g, "kind": kind, "n": len(rs), "scored": len(got),
                    "hit": sum(r[horizon] > 0 for r in got) / len(got) if got else None,
                    "avg": avg, "base": bavg,
                    "excess": None if avg is None or bavg is None else avg - bavg})
    order = {kd: n for n, kd in enumerate(KIND_ORDER)}
    return sorted(out, key=lambda x: (str(x["group"]), order.get(x["kind"], 99)))


def pct(x, width=7, sign=True):
    return " " * (width - 1) + "-" if x is None else f"{x:>+{width}.2f}" if sign else f"{x * 100:>{width - 1}.0f}%"


def show(rows, bases, horizons, by, label=""):
    for h in horizons:
        print(f"\n■ {h}분 뒤" + (f" — {label}별" if label else ""))
        print(f"{'':8}{'종류':<12}{'건수':>5}{'채점':>5}{'맞음':>7}{'수익%':>8}{'기준%':>8}{'초과%':>8}")
        for s in summarize(rows, bases, h, by):
            print(f"{str(s['group']):8}{s['kind']:<12}{s['n']:>5}{s['scored']:>5}"
                  f"{pct(s['hit'], 7, False)}{pct(s['avg'], 8)}{pct(s['base'], 8)}{pct(s['excess'], 8)}")


def show_list(rows, horizons):
    for r in sorted(rows, key=lambda r: r["time"]):
        hs = "  ".join(f"{h}분 {pct(r[h], 6)}" for h in horizons)
        print(f"{r['time']:%m-%d %H:%M} {r['session']:<3} {r['symb']:<6} {r['kind']:<10} "
              f"RSI {r['rsi']:5.1f}  {k.px(r['price'], 9)}  {hs}")


def write_csv(rows, path, horizons):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["시각(현지)", "세션", "종목", "종류", "RSI", "가격"] + [f"{h}분%" for h in horizons])
        for r in sorted(rows, key=lambda r: r["time"]):
            w.writerow([f"{r['time']:%Y-%m-%d %H:%M}", r["session"], r["symb"], r["kind"],
                        f"{r['rsi']:.2f}", r["price"]] + ["" if r[h] is None else f"{r[h]:.3f}" for h in horizons])


def main():
    ap = argparse.ArgumentParser(description="지난 분봉을 되감아 알림·시그널을 채점한다")
    ap.add_argument("tickers", nargs="*", help="없으면 kis_watchlist.json 의 미국 종목 전부")
    ap.add_argument("--days", type=int, default=8, help="거슬러 받을 거래일 (기본 8)")
    ap.add_argument("--min", type=int, default=5, help="분봉 (기본 5)")
    ap.add_argument("--period", type=int, default=14, help="RSI 기간 (기본 14)")
    ap.add_argument("--by", choices=["세션", "종목"], help="이것별로 나눠 보기")
    ap.add_argument("--list", action="store_true", help="하나씩 다 보기")
    ap.add_argument("--csv", help="채점 결과를 이 파일에 적기")
    ap.add_argument("--offline", action="store_true", help="새로 받지 않고 replay_cache 만 쓰기")
    args = ap.parse_args()

    tickers = args.tickers or json.loads(WATCHLIST.read_text(encoding="utf-8"))
    appkey = secret = None
    if not args.offline:
        appkey, secret = k.load_keys()
    rows, bases = [], {}
    for t in tickers:
        if args.offline and ":" not in t:
            found = sorted(CACHE.glob(f"*_{t.upper()}_{args.min}.json"))
            if not found:
                print(f"{t}: 쌓아 둔 것 없음")
                continue
            excd, symb = found[0].name.split("_")[0], t.upper()
        else:
            excd, symb = k.resolve(appkey, secret, t)
        if excd == "KRX":
            print(f"{symb}: 건너뜀 (국내 종목은 아직 되감지 않는다)")
            continue
        try:
            bars, night = load_bars(appkey, secret, excd, symb, args.min, args.days, args.offline)
        except Exception as e:
            print(f"{symb}: 건너뜀 — {e}")
            continue
        events = replay_alerts(bars, args.period) + replay_signals(bars, args.period)
        rows += score(symb, bars, events, HORIZONS)
        bases[symb] = baseline(bars, HORIZONS)
        print(f"{symb:<6} 봉 {len(bars):>5}  {bars[0]['time_us'][:8]}~{bars[-1]['time_us'][:8]}"
              f"  오버나이트 {'넣음' if night else '뺌'}  알림·시그널 {len(events)}")
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
