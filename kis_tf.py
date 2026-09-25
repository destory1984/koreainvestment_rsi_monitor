#!/usr/bin/env python3
"""
kis_tf.py — 몇 분봉 RSI 로 알림을 울려야 가장 잘 맞는지 견준다.

────────────────────────────────────────────────────────────
어떻게

1분봉 한 벌(처음엔 한국투자증권이 주는 약 25거래일, 그 뒤로는 수집이 쌓은 만큼)을 받아 거기서 3·5·10·15·30·60분봉을 만든다.
그래서 모든 봉 길이가 같은 기간·같은 가격을 본다.

실시간 서버는 체결마다 「진행 중인 봉까지 넣은 RSI」를 알림 규칙(kis_alert.Gate)에 넣는다.
여기서는 1분봉 하나를 체결 몇 개로 보고, 1분마다 저가·고가·종가 자리의 RSI 를
(양봉이면 저→고→종, 음봉이면 고→저→종) 진행 중인 N분봉 RSI 로 셈해 Gate 에 넣는다.
60분봉 RSI 알림도 60분을 기다리지 않고 선을 넘는 그 분에 울린다 — 실시간과 같다.

시그널(kis_signal.signals)은 닫힌 N분봉으로 내고, 봉이 닫히는 분에 들어간다.

채점은 kis_replay 와 같다: 들어간 1분봉 종가 → 15·30·60·120분 뒤 1분봉 종가, 알림이 말한 쪽으로 갔으면 맞음,
보합은 빼고 센다. 「기준」은 같은 기간 아무 분에나 같은 쪽으로 들어갔을 때, 「초과」 = 수익 − 기준.
앞 WARMUP_DAYS 거래일은 긴 봉의 RSI 가 자리 잡는 중이라 모든 봉 길이에서 똑같이 세지 않는다.

주간거래(미국 20:00~04:00) 1분봉은 지금 세션 것만 받아져서 (수집이 쌓기 시작한 09-25 전 것이 없어) 모든 종목에서 뺀다.
그래서 서버에서 오버나이트 봉을 넣는 종목(MU·SOXL 등)은 RSI 가 서버와 조금 다르다.

────────────────────────────────────────────────────────────
쓰는 법

  python kis_tf.py                      종목 목록 전체, 1·3·5·10·15·30·60분
  python kis_tf.py TSLA SOXL --tf 1,5,15
  python kis_tf.py --offline            새로 받지 않고 쌓아 둔 1분봉만 (replay_cache/bars.db, nmin=1)
  python kis_tf.py --detail             알림 종류(35 미만 등)·시그널까지 나눠 보기
  python kis_tf.py --by 종목              종목마다 가장 잘 맞는 봉 길이
"""
import argparse
import json
import sys
from collections import defaultdict

import kis_alert as al
import kis_replay as rp
import kis_rsi as k
import kis_signal as ks

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TFS = (1, 3, 5, 10, 15, 30, 60)
HORIZONS = (15, 30, 60, 120)
WARMUP_DAYS = 5


def load_minutes(con, appkey, secret, excd, symb, offline):
    """DB(nmin=1)의 1분봉에 새 것을 이어 받고, 주간거래를 뺀 전부를 돌려준다.
    수집(kis_replay --collect)이 날마다 이어 쌓으니 한 달보다 긴 기간도 본다."""
    if not offline:
        rp.topup_minutes(con, appkey, secret, excd, symb)
        con.commit()
    bars = rp.get_bars(con, excd, symb, 1)
    if excd == "KRX":
        return [b for b in bars if b["time_us"][9:] <= k.KRX_CLOSE]
    return [b for b in bars if rp.session(rp.ts(b["time_us"])) != "주간"]


def aggregate(minutes, nmin):
    """1분봉 → N분봉. 봉마다 (N분봉, 그 봉에 든 1분봉 번호들)."""
    out, keys = [], {}
    for i, m in enumerate(minutes):
        key = k.bar_start(m["time_us"][:8], m["time_us"][9:], nmin)
        if key not in keys:
            keys[key] = len(out)
            out.append(({"time_us": key, "open": m["open"], "high": m["high"], "low": m["low"],
                         "close": m["close"], "volume": m["volume"]}, []))
        b, idx = out[keys[key]]
        b["high"], b["low"], b["close"] = max(b["high"], m["high"]), min(b["low"], m["low"]), m["close"]
        b["volume"] += m["volume"]
        idx.append(i)
    return out


def walk_alerts(minutes, groups, period, lines):
    """1분마다 진행 중인 N분봉 RSI 를 Gate 에 넣는다. [{"i": 1분봉 번호, "kind", "up", "rsi"}]."""
    gate, out = al.Gate(lines), []
    closes = []            # 닫힌 N분봉 종가
    au = ad = None

    def rsi(u, d):
        return 100.0 if d == 0 else 100 - 100 / (1 + u / d)

    def step(price):
        diff = price - closes[-1]
        return (au * (period - 1) + max(diff, 0)) / period, (ad * (period - 1) + max(-diff, 0)) / period

    for bar, idx in groups:
        if au is not None:
            for i in idx:
                m = minutes[i]
                path = (m["low"], m["high"], m["close"]) if m["close"] >= m["open"] else \
                       (m["high"], m["low"], m["close"])
                now = rp.ts(m["time_us"]).timestamp()
                for p in path:
                    v = rsi(*step(p))
                    a = gate.check(v, now)
                    if a and not a.get("suppressed") and not a["start"]:
                        name = f"{al._num(a['edge'])} {'초과' if a['zone'] == 'above' else '미만'}"
                        out.append({"i": i, "kind": "알림 " + name, "up": a["zone"] == "below", "rsi": v})
        # 봉이 닫힌다
        if au is not None:
            au, ad = step(bar["close"])
        closes.append(bar["close"])
        if au is None and len(closes) == period + 1:
            ups = [max(closes[j] - closes[j - 1], 0) for j in range(1, period + 1)]
            downs = [max(closes[j - 1] - closes[j], 0) for j in range(1, period + 1)]
            au, ad = sum(ups) / period, sum(downs) / period
    return out


def walk_signals(groups, period, lines):
    """닫힌 N분봉 시그널. 들어가는 곳은 그 봉의 마지막 1분봉."""
    bars = [b for b, _ in groups]
    last = {b["time_us"]: idx[-1] for b, idx in groups}
    return [{"i": last[x.bar], "kind": f"{x.word} {x.grade}", "up": x.side == "buy", "rsi": x.rsi}
            for x in ks.signals(bars[:-1], period, lines=lines)]


def run_ticker(symb, minutes, tfs, period, lines, kr):
    """봉 길이마다 채점 줄들, 그리고 기준."""
    days = sorted({m["time_us"][:8] for m in minutes})
    if len(days) <= WARMUP_DAYS:
        return {}, {}, None
    start_day = days[WARMUP_DAYS]
    start = next(i for i, m in enumerate(minutes) if m["time_us"][:8] >= start_day)
    rows = {}
    for tf in tfs:
        groups = aggregate(minutes, tf)
        events = walk_alerts(minutes, groups, period, lines) + walk_signals(groups, period, lines)
        events = [e for e in events if e["i"] >= start]
        rows[tf] = rp.score(symb, minutes, events, HORIZONS, kr)
    # 기준: 채점 구간의 아무 분에서나 (WARMUP 봉은 이미 앞에서 지났으니 0 부터)
    base = baseline(minutes[start:], kr)
    return rows, base, (start_day, days[-1], len(days) - WARMUP_DAYS)


def baseline(minutes, kr):
    acc = defaultdict(list)
    for i in range(len(minutes)):
        s = rp.session(rp.ts(minutes[i]["time_us"]), kr)
        for h in HORIZONS:
            j = rp.exit_index(minutes, i, h)
            if j is not None:
                acc[(s, h)].append(rp.ret(minutes, i, j, True))
    return {key: sum(v) / len(v) for key, v in acc.items()}


# ── 보여 주기 ─────────────────────────────────────────────────
def stats(rows, bases, h):
    got = [r for r in rows if r[h] is not None]
    moved = [r for r in got if r[h] != 0]
    base = [bases[r["symb"]].get((r["session"], h)) for r in got]
    base = [b if r["up"] else -b for b, r in zip(base, got) if b is not None]
    avg = sum(r[h] for r in got) / len(got) if got else None
    bavg = sum(base) / len(base) if base else None
    return {"n": len(rows), "hit": sum(r[h] > 0 for r in moved) / len(moved) if moved else None,
            "avg": avg, "excess": None if avg is None or bavg is None else avg - bavg}


def fmt(s):
    hit = "     -" if s["hit"] is None else f"{s['hit'] * 100:5.0f}%"
    ex = "      -" if s["excess"] is None else f"{s['excess']:+7.3f}"
    return f"{hit}{ex}"


def table(title, rows_by_tf, bases, ndays, pick=None):
    """봉 길이마다 한 줄: 건수, 하루 몇 번, 그리고 시간마다 맞음%·초과%."""
    print(f"\n■ {title}")
    print(f"{'봉':>4}{'건수':>6}{'하루':>6}  " + "".join(f"{f'{h}분 뒤 맞음 초과%':>16}" for h in HORIZONS))
    for tf, rows in rows_by_tf.items():
        rs = [r for r in rows if pick is None or pick(r)]
        if not rs:
            continue
        per_day = len(rs) / ndays if ndays else 0
        print(f"{tf:>3}분{len(rs):>6}{per_day:>6.1f}  " + "".join(f"{'':>3}{fmt(stats(rs, bases, h))}" for h in HORIZONS))


def main():
    ap = argparse.ArgumentParser(description="몇 분봉 RSI 가 가장 잘 맞는지 견준다")
    ap.add_argument("tickers", nargs="*", help="없으면 kis_watchlist.json 의 종목 전부")
    ap.add_argument("--tf", default=",".join(map(str, TFS)), help="견줄 봉 길이 (분, 쉼표로)")
    ap.add_argument("--period", type=int, default=14, help="RSI 기간 (기본 14)")
    ap.add_argument("--lines", help="모든 종목에 이 RSI 선 (kis_replay --lines 와 같다)")
    ap.add_argument("--by", choices=["종목"], help="종목마다 따로")
    ap.add_argument("--detail", action="store_true", help="알림 종류·시그널까지 나눠 보기")
    ap.add_argument("--offline", action="store_true", help="새로 받지 않고 쌓아 둔 1분봉만")
    args = ap.parse_args()
    tfs = [int(x) for x in args.tf.split(",")]

    tickers = args.tickers or json.loads(rp.WATCHLIST.read_text(encoding="utf-8"))
    appkey = secret = None
    if not args.offline:
        appkey, secret = k.load_keys()
    con = rp.db()
    all_rows, bases, per_symb, ndays = defaultdict(list), {}, {}, {}
    for t in tickers:
        if args.offline and ":" not in t:
            excd, symb = rp.find_excd(con, t.upper(), 1), t.upper()
            if not excd:
                print(f"{t}: 쌓아 둔 1분봉 없음")
                continue
        else:
            excd, symb = k.resolve(appkey, secret, t)
        try:
            minutes = load_minutes(con, appkey, secret, excd, symb, args.offline)
        except Exception as e:
            print(f"{symb}: 건너뜀 — {e}")
            continue
        kr = excd == "KRX"
        rows, base, span = run_ticker(symb, minutes, tfs, args.period, rp.lines_for(symb, args.lines), kr)
        if not span:
            print(f"{symb}: 날이 모자람 ({len(minutes)} 분)")
            continue
        bases[symb], per_symb[symb], ndays[symb] = base, rows, span[2]
        for tf, rs in rows.items():
            all_rows[tf] += rs
        name = k.KR_INFO.get(symb, {}).get("name", symb) if kr else symb
        print(f"{name:<8} 1분봉 {len(minutes):>6}  채점 {span[0]}~{span[1]} ({span[2]}일)  "
              + " ".join(f"{tf}분 {len(rs)}" for tf, rs in rows.items()))
    if not all_rows:
        sys.exit("채점할 것이 없다.")

    avg_days = sum(ndays.values()) / len(ndays)
    per_ticker_days = avg_days * len(ndays)   # 「하루」 칸은 종목 하나 하루 몇 번
    is_alert = lambda r: r["kind"].startswith("알림")
    is_signal = lambda r: not is_alert(r)
    print("\n맞음 = 알림이 말한 쪽으로 간 비율 (보합 뺌), 초과% = 평균 수익 − 아무 때나 같은 쪽으로 들어간 평균")
    print("「하루」 = 종목 하나가 하루에 울리는 횟수")
    if args.by:
        for symb, rows in per_symb.items():
            table(f"{symb} — 알림", rows, bases, ndays[symb], is_alert)
        return
    table("알림 전체", all_rows, bases, per_ticker_days, is_alert)
    table("알림 — 미만 (오를 쪽)", all_rows, bases, per_ticker_days, lambda r: is_alert(r) and r["up"])
    table("알림 — 초과 (내릴 쪽)", all_rows, bases, per_ticker_days, lambda r: is_alert(r) and not r["up"])
    table("알림 — 미국 정규장", all_rows, bases, per_ticker_days, lambda r: is_alert(r) and r["session"] == "정규" and not r["symb"].isdigit())
    table("시그널 전체", all_rows, bases, per_ticker_days, is_signal)
    if args.detail:
        kinds = sorted({r["kind"] for rs in all_rows.values() for r in rs})
        for kd in kinds:
            table(kd, all_rows, bases, per_ticker_days, lambda r, kd=kd: r["kind"] == kd)


if __name__ == "__main__":
    main()
