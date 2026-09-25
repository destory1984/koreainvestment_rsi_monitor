#!/usr/bin/env python3
"""
kis_report.py — 채점 보고서 페이지를 만든다 (replay_cache/report.html, 웹 서버의 /report).

한 페이지에 셋을 모은다.
  1. 봉 길이 비교 — kis_tf 로 1·3·5·10·15·30·60분봉 RSI 알림·시그널을 같은 기간에 채점
  2. 종목 × 봉 길이 — 종목마다 어느 봉 길이가 맞았나 (미국 정규장·국내 알림, 60분 뒤)
  3. 5분봉(지금 쓰는 것) 알림을 종류·세션별로
  4. 실제로 울린 알림의 성적 — 알림 기록(kis_alerts.jsonl)의 15·30·60분 뒤 (서버 「성적」 표와 같다)

맞음 비율 옆 「±」는 동전 던지기(50%)여도 이만큼은 흔들린다는 폭(2 표준편차)이다.
이 폭을 넘은 칸만 굵게 칠한다. 넘지 않은 칸은 우연일 수 있다.

시세를 담은 페이지라 저장소(docs)에 올리지 않는다. replay_cache/ 는 .gitignore.

  python kis_report.py              쌓아 둔 1분봉으로 (새로 받지 않는다, 13종목 40초쯤)
  python kis_report.py --fetch      1분봉을 이어 받은 뒤
  python kis_report.py --open       다 만들면 브라우저로 열기
"""
import argparse
import html
import math
import sys
import webbrowser
from datetime import datetime

import kis_diverge as dv
import kis_replay as rp
import kis_tf as tf
import kis_web as w

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = rp.CACHE / "report.html"
H = tf.HORIZONS


def is_alert(r):
    return r["kind"].startswith("알림")


def main_session(r):
    """미국 정규장이나 국내 (프리·애프터는 뺀다)."""
    return r["session"] == "정규"


# ── 칸 ───────────────────────────────────────────────────────
def cell(rows, bases, h):
    """맞음%·초과% 칸 하나. 동전 던지기 폭을 넘으면 굵게 색칠."""
    s = tf.stats(rows, bases, h)
    moved = [r for r in rows if r[h] not in (None, 0)]
    if s["hit"] is None:
        return '<td class="num dim">-</td><td class="num dim">-</td>'
    band = 1 / math.sqrt(len(moved))          # 2σ = 2 × 0.5 / √n
    sure = abs(s["hit"] - 0.5) > band
    cls = ("pos" if s["hit"] > 0.5 else "neg") + (" sure" if sure else "")
    hit = f'<td class="num {cls}" title="{len(moved)}건 (보합 뺌), 우연 폭 ±{band * 100:.0f}%p">{s["hit"] * 100:.0f}%</td>'
    ex = s["excess"]
    if ex is None:
        return hit + '<td class="num dim">-</td>'
    return hit + f'<td class="num {"pos" if ex > 0 else "neg"}">{ex:+.2f}</td>'


def head(first, extra=("건수", "하루")):
    top = f'<th rowspan="2">{first}</th>' + "".join(f'<th rowspan="2" class="num">{x}</th>' for x in extra)
    top += "".join(f'<th colspan="2">{h}분 뒤</th>' for h in H)
    sub = "".join('<th class="num">맞음</th><th class="num">초과%</th>' for _ in H)
    return f"<thead><tr>{top}</tr><tr>{sub}</tr></thead>"


def tf_table(all_rows, bases, per_day, pick, best=True):
    """봉 길이마다 한 줄. 60분 뒤 초과가 가장 큰 줄에 표시."""
    body, scores = [], {}
    for t, rows in all_rows.items():
        rs = [r for r in rows if pick(r)]
        if not rs:
            continue
        scores[t] = tf.stats(rs, bases, 60)["excess"]
        body.append((t, f"<td class='num'>{len(rs)}</td><td class='num'>{len(rs) / per_day:.1f}</td>"
                        + "".join(cell(rs, bases, h) for h in H)))
    top = max((t for t in scores if scores[t] is not None), key=lambda t: scores[t], default=None) if best else None
    trs = "".join(f"<tr{' class=best' if t == top else ''}><th>{t}분{' ★' if t == top else ''}</th>{tds}</tr>"
                  for t, tds in body)
    return f"<table>{head('봉')}<tbody>{trs}</tbody></table>"


def grid(per_symb, bases, info, tfs, h=60):
    """종목 × 봉 길이: 알림(정규장) h 분 뒤 초과% 와 맞음%. 종목마다 가장 좋은 칸에 표시."""
    ths = "".join(f"<th colspan='2'>{t}분</th>" for t in tfs)
    sub = "".join("<th class='num'>맞음</th><th class='num'>초과%</th>" for _ in tfs)
    trs = []
    for symb, rows in per_symb.items():
        tds, best, best_ex = [], None, None
        for t in tfs:
            rs = [r for r in rows.get(t, []) if is_alert(r) and main_session(r)]
            ex = tf.stats(rs, bases, h)["excess"] if rs else None
            if ex is not None and (best_ex is None or ex > best_ex):
                best, best_ex = t, ex
            tds.append((t, cell(rs, bases, h) if rs else '<td class="dim">-</td><td class="dim">-</td>'))
        name = html.escape(tf.k.KR_INFO.get(symb, {}).get("name", symb))
        cells = "".join(f"<td class='wrap{' best' if t == best else ''}' colspan='2'><table class='in'><tr>{c}</tr></table></td>"
                        for t, c in tds)
        trs.append(f"<tr><th>{name}<span class='dim'> {info[symb]['days']}일</span></th>{cells}</tr>")
    return (f"<table class='grid'><thead><tr><th rowspan='2'>종목</th>{ths}</tr><tr>{sub}</tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table>")


def lines_table(info, bases, h=60):
    """종목 × 선: 5분봉 정규장 알림의 하루 몇 번·h 분 뒤 맞음·초과. 종목마다 초과가 가장 큰 선에 표시, 지금 쓰는 선에 ●."""
    names = list(tf.LINE_SETS)
    ths = "".join(f"<th colspan='3'>{n}</th>" for n in names)
    sub = "".join("<th class='num'>하루</th><th class='num'>맞음</th><th class='num'>초과%</th>" for _ in names)
    total = {n: [] for n in names}
    trs = []

    def row(label, per, days, now=None):
        tds, best, best_ex = [], None, None
        for n in names:
            rs = [r for r in per.get(n, []) if main_session(r)]
            ex = tf.stats(rs, bases, h)["excess"] if rs else None
            if ex is not None and (best_ex is None or ex > best_ex):
                best, best_ex = n, ex
            tds.append((n, rs))
        cells = ""
        for n, rs in tds:
            mark = " best" if n == best else ""
            dot = " ●" if n == now else ""
            cells += (f"<td class='num{mark}'>{len(rs) / days:.1f}{dot}</td>"
                      + (cell(rs, bases, h).replace("<td class=\"num", f"<td class=\"num{mark}") if rs
                         else f"<td class='dim{mark}'>-</td><td class='dim{mark}'>-</td>"))
        return f"<tr><th>{label}</th>{cells}</tr>"

    all_days = 0
    for symb, i in info.items():
        per = i.get("lines") or {}
        now = next((n for n, (lo, up) in tf.LINE_SETS.items() if (i["now"].lower, i["now"].upper) == (lo, up)), None)
        name = html.escape(tf.k.KR_INFO.get(symb, {}).get("name", symb))
        trs.append(row(name, per, i["days"], now))
        for n in names:
            total[n] += per.get(n, [])
        all_days += i["days"]
    trs.insert(0, row("<b>모두</b>", total, all_days or 1))
    return (f"<table><thead><tr><th rowspan='2'>종목</th>{ths}</tr><tr>{sub}</tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table>")


def kind_table(rows, bases, per_day, key, order=None):
    groups = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    names = order or sorted(groups)
    trs = "".join(f"<tr><th>{html.escape(g)}</th><td class='num'>{len(groups[g])}</td>"
                  f"<td class='num'>{len(groups[g]) / per_day:.1f}</td>"
                  + "".join(cell(groups[g], bases, h) for h in H) + "</tr>"
                  for g in names if g in groups)
    return f"<table>{head('종류')}<tbody>{trs}</tbody></table>"


def live_table():
    rows = w.Hub.scores()
    if not rows:
        return "<p class='dim'>아직 채점된 실제 알림이 없다.</p>"
    hs = [str(h) for h in w.AFTER]
    top = "<th rowspan='2'>종류</th>" + "".join(f"<th colspan='3'>{h}분 뒤</th>" for h in hs)
    sub = "".join("<th class='num'>건수</th><th class='num'>맞음</th><th class='num'>평균%</th>" for _ in hs)
    trs = []
    for r in rows:
        tds = ""
        for h in hs:
            x = r[h]
            hit = "-" if x["hit"] is None else f"{x['hit'] * 100:.0f}%"
            avg = "-" if x["avg"] is None else f"{x['avg']:+.2f}"
            hc = "" if x["hit"] is None else ("pos" if x["hit"] > 0.5 else "neg")
            ac = "" if x["avg"] is None else ("pos" if x["avg"] > 0 else "neg")
            tds += f"<td class='num'>{x['n']}</td><td class='num {hc}'>{hit}</td><td class='num {ac}'>{avg}</td>"
        trs.append(f"<tr><th>{html.escape(r['kind'])}</th>{tds}</tr>")
    return f"<table><thead><tr>{top}</tr><tr>{sub}</tr></thead><tbody>{''.join(trs)}</tbody></table>"


COST = 0.2      # 사고팔 때 드는 비용 가정 (%, 왕복: 수수료·세금·호가 차이). --cost 로 바꾼다
MIN_PICK = 8    # 앞 절반에서 이보다 적게 울린 것은 고르지 않는다 (지금 것을 둔다)


def day_of(r):
    return r["time"].strftime("%Y%m%d")


def split_day(all_rows):
    """채점한 날들의 가운데 날. 이 날부터가 뒤 절반."""
    days = sorted({day_of(r) for rs in all_rows.values() for r in rs})
    return days[len(days) // 2] if days else None


def excess(rows, bases, h=60):
    return tf.stats(rows, bases, h)["excess"] if rows else None


def split_tf_table(all_rows, bases, mid, h=60):
    """봉 길이마다 앞·뒤 절반. ★ = 앞에서 초과가 가장 컸던 것."""
    pick = lambda r: is_alert(r) and main_session(r)
    body, front = [], {}
    for t, rows in all_rows.items():
        rs = [r for r in rows if pick(r)]
        a = [r for r in rs if day_of(r) < mid]
        b = [r for r in rs if day_of(r) >= mid]
        front[t] = excess(a, bases, h)
        body.append((t, len(a), len(b), cell(a, bases, h), cell(b, bases, h)))
    top = max((t for t in front if front[t] is not None), key=lambda t: front[t], default=None)
    trs = "".join(f"<tr{' class=best' if t == top else ''}><th>{t}분{' ★' if t == top else ''}</th>"
                  f"<td class='num'>{na}</td>{ca}<td class='num'>{nb}</td>{cb}</tr>" for t, na, nb, ca, cb in body)
    return ("<table><thead><tr><th rowspan='2'>봉</th><th colspan='3'>앞 절반</th><th colspan='3'>뒤 절반</th></tr>"
            "<tr><th class='num'>건수</th><th class='num'>맞음</th><th class='num'>초과%</th>"
            "<th class='num'>건수</th><th class='num'>맞음</th><th class='num'>초과%</th></tr></thead>"
            f"<tbody>{trs}</tbody></table>")


def pick_per_ticker(choices, default, bases, mid, h=60):
    """종목마다 앞 절반에서 초과가 가장 큰 것을 고른다. (고른 것, 앞 초과, 뒤 줄들, 지금 것 뒤 줄들)."""
    front = {}
    for name, rows in choices.items():
        a = [r for r in rows if is_alert(r) and main_session(r) and day_of(r) < mid]
        if len(a) >= MIN_PICK:
            front[name] = excess(a, bases, h)
    ok = {n: v for n, v in front.items() if v is not None}
    chosen = max(ok, key=ok.get) if ok else default
    back = lambda n: [r for r in choices.get(n, []) if is_alert(r) and main_session(r) and day_of(r) >= mid]
    return chosen, front.get(chosen), back(chosen), back(default)


def split_pick_table(label, per_symb_choices, default, bases, mid, h=60):
    """종목마다 앞에서 고른 것 vs 모두 지금 것, 뒤 절반에서. 아래에 종목마다 무엇을 골랐는지."""
    picked, kept, lines = [], [], []
    for symb, choices in per_symb_choices.items():
        chosen, fex, b_chosen, b_default = pick_per_ticker(choices, default, bases, mid, h)
        picked += b_chosen
        kept += b_default
        bx, dx = excess(b_chosen, bases, h), excess(b_default, bases, h)
        name = html.escape(tf.k.KR_INFO.get(symb, {}).get("name", symb))
        fmt = lambda v: "-" if v is None else f"{v:+.2f}"
        better = "" if bx is None or dx is None else (" pos" if bx > dx else " neg" if bx < dx else "")
        lines.append(f"<tr><th>{name}</th><td>{html.escape(str(chosen))}{'' if chosen != default else ' (지금)'}</td>"
                     f"<td class='num'>{fmt(fex)}</td><td class='num{better}'>{fmt(bx)}</td><td class='num'>{fmt(dx)}</td></tr>")
    top = ("<table><thead><tr><th>뒤 절반에서</th><th class='num'>건수</th>"
           + "".join(f"<th colspan='2'>{x}분 뒤</th>" for x in H) + "</tr></thead><tbody>"
           + f"<tr><th>종목마다 앞에서 고른 {label}</th><td class='num'>{len(picked)}</td>"
           + "".join(cell(picked, bases, x) for x in H) + "</tr>"
           + f"<tr><th>모두 지금 {label} ({html.escape(str(default))})</th><td class='num'>{len(kept)}</td>"
           + "".join(cell(kept, bases, x) for x in H) + "</tr></tbody></table>")
    detail = (f"<table><thead><tr><th>종목</th><th>고른 {label}</th><th class='num'>앞 초과%</th>"
              f"<th class='num'>뒤 초과%</th><th class='num'>지금 것 뒤</th></tr></thead><tbody>{''.join(lines)}</tbody></table>")
    return top + "<p></p>" + detail


def atr_table(rows, bases, cost, parts=5):
    """변동폭(5분봉 평균 진폭 %)을 다섯 칸으로 나눠, 칸마다 맞음·평균·비용 뺀 평균."""
    rs = sorted((r for r in rows if is_alert(r) and main_session(r) and r.get("atr")), key=lambda r: r["atr"])
    if len(rs) < parts * 10:
        return "<p class='dim'>알림이 모자라다.</p>"
    size = len(rs) / parts
    groups = [rs[round(i * size):round((i + 1) * size)] for i in range(parts)]
    trs = []
    for g in groups:
        tds = ""
        for x in H:
            s = tf.stats(g, bases, x)
            got = [r[x] for r in g if r[x] is not None]
            avg = sum(got) / len(got) if got else None
            net = None if avg is None else avg - cost
            hit = "-" if s["hit"] is None else f"{s['hit'] * 100:.0f}%"
            tds += (f"<td class='num {'pos' if (s['hit'] or 0) > .5 else 'neg'}'>{hit}</td>"
                    f"<td class='num'>{'-' if avg is None else f'{avg:+.2f}'}</td>"
                    f"<td class='num {'pos' if (net or 0) > 0 else 'neg'}'>{'-' if net is None else f'{net:+.2f}'}</td>")
        trs.append(f"<tr><th>{g[0]['atr']:.2f}~{g[-1]['atr']:.2f}%</th><td class='num'>{len(g)}</td>{tds}</tr>")
    head = ("<thead><tr><th rowspan='2'>변동폭</th><th rowspan='2' class='num'>건수</th>"
            + "".join(f"<th colspan='3'>{x}분 뒤</th>" for x in H) + "</tr><tr>"
            + "".join("<th class='num'>맞음</th><th class='num'>평균%</th><th class='num'>비용 뺀%</th>" for _ in H)
            + "</tr></thead>")
    return f"<table>{head}<tbody>{''.join(trs)}</tbody></table>"


DEDUP_MIN = 60    # 같은 종목에서 이 분 안에 또 난 알림은 하나로 센다 (한 번 움직임에 몰려 울린 것)
BOOT = 1000       # 날 단위로 다시 뽑는 횟수


def dedupe(rows, gap=DEDUP_MIN):
    """종목마다 앞 알림에서 gap 분 안에 난 알림을 뺀다 (종류·방향 상관없이)."""
    out, last = [], {}
    for r in sorted(rows, key=lambda r: (r["symb"], r["time"])):
        t = last.get(r["symb"])
        if t is None or (r["time"] - t).total_seconds() >= gap * 60:
            out.append(r)
            last[r["symb"]] = r["time"]
    return out


def row_excess(r, bases, h):
    b = bases[r["symb"]].get((r["session"], h))
    if r[h] is None or b is None:
        return None
    return r[h] - (b if r["up"] else -b)


def day_band(rows, bases, h=60, n=BOOT, seed=1):
    """날 단위로 다시 뽑아(bootstrap) 평균 초과의 90% 구간. 같은 날 알림은 같이 움직이니 날을 한 덩어리로 본다."""
    import random
    by_day = {}
    for r in rows:
        x = row_excess(r, bases, h)
        if x is not None:
            by_day.setdefault(day_of(r), []).append(x)
    days = list(by_day.values())
    if len(days) < 5:
        return None
    rnd, means = random.Random(seed), []
    for _ in range(n):
        pick = [x for _ in days for x in rnd.choice(days)]
        means.append(sum(pick) / len(pick))
    means.sort()
    return means[int(n * .05)], means[int(n * .95)]


def dedupe_table(all_rows, bases, h=60):
    """봉 길이마다: 모든 알림 vs 몰린 것 하나로 센 알림, 그리고 날 단위 90% 구간."""
    trs = []
    for t, rows in all_rows.items():
        rs = [r for r in rows if is_alert(r) and main_session(r)]
        one = dedupe(rs)
        band = day_band(one, bases, h)
        if band:
            cls = "pos sure" if band[0] > 0 else "neg sure" if band[1] < 0 else "dim"
            bt = f"<td class='num {cls}'>{band[0]:+.2f} ~ {band[1]:+.2f}</td>"
        else:
            bt = "<td class='dim'>-</td>"
        trs.append(f"<tr><th>{t}분</th><td class='num'>{len(rs)}</td>{cell(rs, bases, h)}"
                   f"<td class='num'>{len(one)}</td>{cell(one, bases, h)}{bt}</tr>")
    return ("<table><thead><tr><th rowspan='2'>봉</th><th colspan='3'>모든 알림</th>"
            f"<th colspan='4'>{DEDUP_MIN}분 안에 몰린 것은 하나로</th></tr>"
            "<tr><th class='num'>건수</th><th class='num'>맞음</th><th class='num'>초과%</th>"
            "<th class='num'>건수</th><th class='num'>맞음</th><th class='num'>초과%</th><th class='num'>초과 90% 구간</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table>")


def money_cells(rows, h, cost):
    """평균%, 비용 뺀%, 평균 최대 역행% 세 칸."""
    got = [r[h] for r in rows if r[h] is not None]
    maes = [r["mae"][h] for r in rows if r.get("mae") and r["mae"].get(h) is not None]
    if not got:
        return "<td class='dim'>-</td>" * 3
    avg = sum(got) / len(got)
    net = avg - cost
    mae = sum(maes) / len(maes) if maes else None
    return (f"<td class='num {'pos' if avg > 0 else 'neg'}'>{avg:+.2f}</td>"
            f"<td class='num {'pos' if net > 0 else 'neg'}'>{net:+.2f}</td>"
            f"<td class='num neg'>{'-' if mae is None else f'{mae:.2f}'}</td>")


MONEY_H = (30, 60, 120)


def money_head(first):
    return (f"<thead><tr><th rowspan='2'>{first}</th><th rowspan='2' class='num'>건수</th>"
            + "".join(f"<th colspan='3'>{h}분 뒤</th>" for h in MONEY_H) + "</tr><tr>"
            + "".join("<th class='num'>평균%</th><th class='num'>비용 뺀%</th><th class='num'>최대 역행%</th>" for _ in MONEY_H)
            + "</tr></thead>")


def money_table(all_rows, cost):
    """봉 길이마다 비용을 뺀 평균과, 들어간 뒤 반대로 가장 멀리 간 폭의 평균."""
    trs = []
    for t, rows in all_rows.items():
        rs = dedupe([r for r in rows if is_alert(r) and main_session(r)])
        trs.append(f"<tr><th>{t}분</th><td class='num'>{len(rs)}</td>" + "".join(money_cells(rs, h, cost) for h in MONEY_H) + "</tr>")
    return f"<table>{money_head('봉')}<tbody>{''.join(trs)}</tbody></table>"


RVOL_BANDS = ((0, .3, "아주 얇음"), (.3, 1, "얇음"), (1, 3, "보통"), (3, float("inf"), "많음"))


def rvol_table(rows, cost):
    """알림 앞 5분 거래량이 그 종목 보통의 몇 배였나로 나눠 본다 (5분봉 알림, 모든 세션)."""
    rs = [r for r in rows if is_alert(r) and r.get("rvol") is not None]
    trs = []
    for lo, hi, name in RVOL_BANDS:
        g = dedupe([r for r in rs if lo <= r["rvol"] < hi])
        if not g:
            continue
        rng = f"{lo:g}~{hi:g}배" if hi != float("inf") else f"{lo:g}배 넘게"
        sess = {}
        for r in g:
            key = ("국내 " if r["symb"].isdigit() else "") + r["session"]
            sess[key] = sess.get(key, 0) + 1
        mix = ", ".join(f"{k} {v * 100 // len(g)}%" for k, v in sorted(sess.items(), key=lambda kv: -kv[1])[:3])
        s = tf.stats(g, {r["symb"]: {} for r in g}, 60)
        hit = "-" if s["hit"] is None else f"{s['hit'] * 100:.0f}%"
        trs.append(f"<tr><th>{name}<span class='dim'> {rng}</span></th><td class='num'>{len(g)}</td>"
                   f"<td class='num'>{hit}</td>" + "".join(money_cells(g, h, cost) for h in MONEY_H)
                   + f"<td class='dim'>{mix}</td></tr>")
    head = ("<thead><tr><th rowspan='2'>거래량</th><th rowspan='2' class='num'>건수</th><th rowspan='2' class='num'>60분 맞음</th>"
            + "".join(f"<th colspan='3'>{h}분 뒤</th>" for h in MONEY_H) + "<th rowspan='2'>세션</th></tr><tr>"
            + "".join("<th class='num'>평균%</th><th class='num'>비용 뺀%</th><th class='num'>최대 역행%</th>" for _ in MONEY_H)
            + "</tr></thead>")
    return f"<table>{head}<tbody>{''.join(trs)}</tbody></table>"


ATR_GATES = (0, 0.4, 0.5, 0.66, 0.8, 1.0)   # 「변동폭이 이만큼(%) 넘을 때만 울렸다면」


def gate_table(rows, bases, mid, per_day, cost):
    """변동폭 문턱마다 5분봉 정규장 알림(몰린 것 하나로)을 앞·뒤 절반에서. 문턱을 앞에서 고르고 뒤에서 확인하려는 것."""
    base = [r for r in rows if is_alert(r) and main_session(r) and r.get("atr") is not None]
    halves = (("앞", lambda r: day_of(r) < mid), ("뒤", lambda r: day_of(r) >= mid))
    trs = []
    for g in ATR_GATES:
        tds = ""
        for _, inside in halves:
            rs = dedupe([r for r in base if r["atr"] >= g and inside(r)])
            s = tf.stats(rs, bases, 60)
            net = {}
            for h in (60, 120):
                got = [r[h] for r in rs if r[h] is not None]
                net[h] = sum(got) / len(got) - cost if got else None
            hit = "-" if s["hit"] is None else f"{s['hit'] * 100:.0f}%"
            f = lambda v: "<td class='dim'>-</td>" if v is None else f"<td class='num {'pos' if v > 0 else 'neg'}'>{v:+.2f}</td>"
            tds += f"<td class='num'>{len(rs)}</td><td class='num'>{hit}</td>{f(net[60])}{f(net[120])}"
        label = "문턱 없음 (지금)" if g == 0 else f"{g:g}% 넘을 때만"
        trs.append(f"<tr><th>{label}</th>{tds}</tr>")
    sub = "<th class='num'>건수</th><th class='num'>60분 맞음</th><th class='num'>60분 비용 뺀%</th><th class='num'>120분 비용 뺀%</th>"
    return ("<table><thead><tr><th rowspan='2'>변동폭 문턱</th><th colspan='4'>앞 절반</th><th colspan='4'>뒤 절반</th></tr>"
            f"<tr>{sub}{sub}</tr></thead><tbody>{''.join(trs)}</tbody></table>")


def verdict_table(groups, bases, mid, per_day, cost):
    """[(이름, 줄들)] 마다 한 줄: 몰린 것 하나로 센 건수·하루, 60분 맞음·초과·날 단위 90% 구간, 앞·뒤 절반 초과,
    비용 뺀 60·120분, 60분 최대 역행. 보고서의 다른 잣대를 한 줄에 모은 것."""
    trs = []
    for label, rows in groups:
        rs = dedupe(rows)
        if not rs:
            trs.append(f"<tr><th>{label}</th><td class='dim' colspan='11'>없음</td></tr>")
            continue
        band = day_band(rs, bases, 60)
        bt = ("<td class='dim'>-</td>" if not band else
              f"<td class='num {'pos sure' if band[0] > 0 else 'neg sure' if band[1] < 0 else 'dim'}'>{band[0]:+.2f} ~ {band[1]:+.2f}</td>")
        halves = ""
        for inside in (lambda r: day_of(r) < mid, lambda r: day_of(r) >= mid):
            x = excess([r for r in rs if inside(r)], bases, 60)
            halves += "<td class='dim'>-</td>" if x is None else f"<td class='num {'pos' if x > 0 else 'neg'}'>{x:+.2f}</td>"
        money = ""
        for h in (60, 120):
            got = [r[h] for r in rs if r[h] is not None]
            v = sum(got) / len(got) - cost if got else None
            money += "<td class='dim'>-</td>" if v is None else f"<td class='num {'pos' if v > 0 else 'neg'}'>{v:+.2f}</td>"
        maes = [r["mae"][60] for r in rs if r.get("mae") and r["mae"].get(60) is not None]
        mae = f"{sum(maes) / len(maes):.2f}" if maes else "-"
        trs.append(f"<tr><th>{label}</th><td class='num'>{len(rs)}</td><td class='num'>{len(rs) / per_day:.2f}</td>"
                   f"{cell(rs, bases, 60)}{bt}{halves}{money}<td class='num neg'>{mae}</td></tr>")
    head = ("<thead><tr><th rowspan='2'></th><th rowspan='2' class='num'>건수</th><th rowspan='2' class='num'>하루</th>"
            "<th colspan='3'>60분 뒤</th><th colspan='2'>60분 초과 (절반씩)</th><th colspan='2'>비용 뺀%</th>"
            "<th rowspan='2' class='num'>60분<br>최대 역행%</th></tr>"
            "<tr><th class='num'>맞음</th><th class='num'>초과%</th><th class='num'>90% 구간</th>"
            "<th class='num'>앞</th><th class='num'>뒤</th><th class='num'>60분</th><th class='num'>120분</th></tr></thead>")
    return f"<table>{head}<tbody>{''.join(trs)}</tbody></table>"


def divergence_section(info, all_rows, bases, mid, per_day, cost):
    div = [r for i in info.values() for r in i.get("div", []) if main_session(r)]
    five = [r for r in all_rows.get(tf.DIV_TF, []) if is_alert(r) and main_session(r)]
    sig = [r for r in all_rows.get(tf.DIV_TF, []) if not is_alert(r) and main_session(r)]
    groups = [
        ("다이버전스 — RSI 만", div),
        ("  · 상승 (오를 쪽)", [r for r in div if r["up"]]),
        ("  · 하락 (내릴 쪽)", [r for r in div if not r["up"]]),
        ("다이버전스 — RSI + MACD (이중)", [r for r in div if r["macd"]]),
        ("다이버전스 — 이중 + 전날 저가·고가 근처", [r for r in div if r["macd"] and r["level"]]),
        (f"{tf.DIV_TF}분봉 알림 — 모두 (지금)", five),
        (f"  · 앞 {tf.DIV_TF * tf.DIV_RECENT}분 안에 같은 쪽 다이버전스", [r for r in five if r.get("div")]),
        ("  · 다이버전스 없이", [r for r in five if not r.get("div")]),
        (f"{tf.DIV_TF}분봉 매수·매도 시그널 (지금, 참고)", sig),
    ]
    return verdict_table(groups, bases, mid, per_day, cost)


# ── 페이지 ────────────────────────────────────────────────────
CSS = """
:root { --bg:#f6f7f9; --panel:#fff; --line:#e3e6eb; --text:#1b1f24; --muted:#6b7380;
        --pos:#1f9d55; --neg:#d6453d; --pos-bg:#1f9d5522; --neg-bg:#d6453d22; --best:#eef3ff; }
@media (prefers-color-scheme: dark) { :root { --bg:#0f1216; --panel:#171b21; --line:#262c35; --text:#e6e9ee;
        --muted:#8b94a3; --pos:#3cc47c; --neg:#f0605a; --pos-bg:#3cc47c26; --neg-bg:#f0605a26; --best:#1d2633; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:12px/1.5 "Pretendard","Malgun Gothic",system-ui,sans-serif; }
main { max-width:1180px; margin:0 auto; padding:16px; display:grid; gap:14px; }
h1 { font-size:16px; margin:0; } h2 { font-size:13px; margin:0 0 4px; }
p { margin:0 0 8px; } .dim { color:var(--muted); }
section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; overflow-x:auto; }
table { border-collapse:collapse; font-variant-numeric:tabular-nums; }
th, td { padding:3px 8px; border-bottom:1px solid var(--line); white-space:nowrap; text-align:left; }
thead th { color:var(--muted); font-weight:500; text-align:center; }
.num { text-align:right; }
.pos { color:var(--pos); } .neg { color:var(--neg); }
.sure.pos { background:var(--pos-bg); font-weight:700; } .sure.neg { background:var(--neg-bg); font-weight:700; }
tr.best > *, td.best { background:var(--best); }
table.in td { border:0; padding:0 4px; } td.wrap { padding:2px 4px; }
.cols { display:grid; grid-template-columns:repeat(auto-fit, minmax(520px, 1fr)); gap:14px; }
"""


def build(offline=True, say=print, cost=COST):
    tf.k.load_kr_market()
    tickers = rp.collect_tickers()
    all_rows, bases, per_symb, ndays, info = tf.analyze(tickers, tf.TFS, offline=offline, say=say,
                                                        line_sets=tf.LINE_SETS, diverge=True)
    if not all_rows:
        raise SystemExit("채점할 것이 없다 (1분봉이 없다 — --fetch 로 받거나 수집을 기다린다).")
    per_day = sum(ndays.values())   # 종목 하나 하루
    days = [i["from"] for i in info.values()] + [i["to"] for i in info.values()]
    span = f"{min(days)[:4]}-{min(days)[4:6]}-{min(days)[6:]} ~ {max(days)[:4]}-{max(days)[4:6]}-{max(days)[6:]}"
    us = lambda r: not r["symb"].isdigit()
    five = all_rows.get(5, [])
    mid = split_day(all_rows)
    kinds = sorted({r["kind"] for r in five if is_alert(r)},
                   key=lambda kd: (0 if "미만" in kd else 1, float(kd.split()[1])))
    sections = [
        f"""<section><h1>채점 보고서</h1>
<p class="dim">{len(info)}종목, {span} (종목마다 앞 {tf.WARMUP_DAYS}거래일은 RSI 자리 잡기로 뺌). 주간거래 시간은 뺐다.
만든 때 {datetime.now():%Y-%m-%d %H:%M}.</p>
<p><b>맞음</b> = 알림이 말한 쪽으로 간 비율 (보합 뺌). <b>초과%</b> = 평균 수익 − 아무 때나 같은 쪽으로 들어간 평균.
<b>하루</b> = 종목 하나가 하루에 울리는 횟수. 굵게 칠한 칸만 동전 던지기 폭(±2σ)을 넘었다 — 나머지는 우연일 수 있다.
★ = 60분 뒤 초과가 가장 큰 봉 길이.</p></section>""",
        f"""<div class="cols"><section><h2>봉 길이 — 알림, 미국 정규장·국내</h2>
{tf_table(all_rows, bases, per_day, lambda r: is_alert(r) and main_session(r))}</section>
<section><h2>봉 길이 — 알림, 모든 세션</h2>
{tf_table(all_rows, bases, per_day, is_alert)}</section>
<section><h2>봉 길이 — 미만 알림 (오를 쪽), 정규장</h2>
{tf_table(all_rows, bases, per_day, lambda r: is_alert(r) and r["up"] and main_session(r))}</section>
<section><h2>봉 길이 — 초과 알림 (내릴 쪽), 정규장</h2>
{tf_table(all_rows, bases, per_day, lambda r: is_alert(r) and not r["up"] and main_session(r))}</section>
<section><h2>봉 길이 — 시그널, 정규장</h2>
{tf_table(all_rows, bases, per_day, lambda r: not is_alert(r) and main_session(r))}</section></div>""",
        f"""<section><h2>종목 × 봉 길이 — 알림, 정규장, 60분 뒤</h2>
<p class="dim">칸마다 맞음%·초과%. 파란 바탕 = 그 종목에서 초과가 가장 큰 봉 길이. 건수가 적은 종목은 흔들림이 크다 (칸에 마우스를 올리면 건수).</p>
{grid(per_symb, bases, info, list(all_rows))}</section>""",
        f"""<section><h2>나눠서 확인 — 앞 절반에서 고른 것이 뒤 절반에서도 좋은가 (알림, 정규장)</h2>
<p class="dim">앞 절반 = {mid[:4]}-{mid[4:6]}-{mid[6:]} 전, 뒤 절반 = 그날부터. 앞에서 가장 좋았던 것이 뒤에서도 좋아야 믿을 만하다.
뒤에서 무너지면 앞의 좋은 성적은 우연(과최적화)이었다. 종목마다 고를 때 앞에서 {MIN_PICK}번 넘게 울린 것만 고르고, 아니면 지금 것을 둔다.</p>
<div class="cols"><div><h2>봉 길이 (모두 합쳐, 60분 뒤)</h2>{split_tf_table(all_rows, bases, mid)}</div>
<div><h2>종목마다 선 고르기 (5분봉)</h2>{split_pick_table("선", {s: i["lines"] for s, i in info.items()}, "35/65", bases, mid)}</div>
<div><h2>종목마다 봉 길이 고르기</h2>{split_pick_table("봉", {s: {f"{t}분": rs for t, rs in per.items()} for s, per in per_symb.items()}, "5분", bases, mid)}</div></div></section>""",
        f"""<section><h2>몰린 알림을 하나로 세면 — 알림, 정규장, 60분 뒤</h2>
<p class="dim">한 번 크게 움직이면 알림이 연달아 울린다. 같은 종목에서 {DEDUP_MIN}분 안에 또 난 알림을 빼고 센다.
<b>초과 90% 구간</b>은 날을 한 덩어리로 {BOOT}번 다시 뽑아 셈했다 (같은 날·같은 업종 종목은 같이 움직이니 알림 하나하나를 따로 세면 폭이 너무 좁다).
구간이 0 위에 있으면(초록) 우연이 아닐 가능성이 높고, 0 을 걸치면(회색) 아직 모른다.</p>
{dedupe_table(all_rows, bases)}</section>""",
        f"""<section><h2>비용을 빼면·최대 역행 — 알림, 정규장 (몰린 것 하나로)</h2>
<p class="dim"><b>비용 뺀%</b> = 평균 − {cost:.2f}%. <b>최대 역행%</b> = 들어간 뒤 그 시간 안에 반대로 가장 멀리 간 폭의 평균 (1분봉 저가·고가).
맞음 비율이 높아도 역행이 크면 버티기 어렵다.</p>
{money_table(all_rows, cost)}</section>""",
        f"""<section><h2>다이버전스 — {tf.DIV_TF}분봉, 정규장 (몰린 것 하나로)</h2>
<p class="dim">가격 저점은 낮아졌는데 RSI 저점은 높아졌으면 상승 다이버전스, 고점은 그 반대 (<code>kis_diverge.py</code>).
저점·고점 = 앞뒤 {dv.PIVOT_K}봉보다 낮은(높은) 봉, 앞 점은 {dv.LOOKBACK}봉 안. 확인은 {dv.PIVOT_K}봉 뒤라 그만큼 늦게 들어간다.
이중 = MACD 히스토그램도 같이 어긋남. 전날 저가·고가 근처 = 변동폭 절반 안 (영상의 「지지·저항 + 가짜 돌파」 흉내).
아래 셋은 지금 알림을 다이버전스로 걸렀다면. 90% 구간이 0 위(초록)이고 앞·뒤 절반이 모두 + 이고 비용을 빼도 + 여야 쓸 만하다.</p>
{divergence_section(info, all_rows, bases, mid, per_day, cost)}</section>""",
        f"""<section><h2>변동폭별 — 5분봉 알림, 정규장</h2>
<p class="dim">변동폭 = 알림 때 그 앞 5분봉 14개의 평균 진폭(고가−저가) ÷ 가격. 알림을 변동폭 순으로 다섯 칸에 나눴다.
<b>비용 뺀%</b> = 평균 수익 − {cost:.2f}% (사고팔 때 수수료·세금·호가 차이를 합쳐 이만큼 든다고 가정, <code>--cost</code> 로 바꾼다).
비용을 빼고도 + 인 칸만 있으면 「변동폭이 그 위일 때만 울리기」를 생각할 만하다.</p>
{atr_table(five, bases, cost)}
<h2 style="margin-top:12px">변동폭 문턱을 두었다면 — 몰린 것 하나로, 앞·뒤 절반</h2>
<p class="dim">「변동폭이 문턱을 넘을 때만 울리기」를 흉내 냈다. 앞 절반에서 비용을 빼고도 + 가 되는 문턱을 고르고, 뒤 절반에서도 + 인지 본다.
문턱을 높일수록 덜 울린다 (건수).</p>
{gate_table(five, bases, mid, per_day, cost)}</section>""",
        f"""<section><h2>거래량별 — 5분봉 알림, 모든 세션 (몰린 것 하나로)</h2>
<p class="dim">알림 앞 {tf.RVOL_MIN}분 거래량이 그 종목·같은 세션의 보통(중앙값) 몇 배였나 (프리는 프리끼리, 정규는 정규끼리). 거래가 얇으면 종가가 매수·매도 호가 사이를 오가
RSI 가 끝에 닿았다 돌아오는 것처럼 보이지만, 그 값에 실제로 사고팔기는 어렵다. 얇은 칸의 좋은 성적은 착시일 수 있다.</p>
{rvol_table(five, cost)}</section>""",
        f"""<section><h2>종목별 선 — 5분봉 알림, 정규장, 60분 뒤</h2>
<p class="dim">선을 아래/위 둘로 적었다 (강한 선은 5 바깥, 35/65 면 30·35·65·70). ● = 지금 그 종목에 쓰는 선. 파란 바탕 = 초과가 가장 큰 선.
바깥 선일수록 덜 울리니 「하루」도 같이 본다.</p>
{lines_table(info, bases)}</section>""",
        f"""<div class="cols"><section><h2>5분봉 (지금 쓰는 것) — 종류별, 정규장</h2>
{kind_table([r for r in five if main_session(r)], bases, per_day, lambda r: r["kind"],
            kinds + ["매수 강", "매수 약", "매도 강", "매도 약"])}</section>
<section><h2>5분봉 알림 — 세션별</h2>
{kind_table([r for r in five if is_alert(r)], bases, per_day,
            lambda r: ("미국 " if us(r) else "국내 ") + r["session"],
            ["미국 프리", "미국 정규", "미국 애프터", "국내 정규"])}</section></div>""",
        f"""<section><h2>실제로 울린 알림 (서버 알림 기록)</h2>
<p class="dim">서버가 켜져 있는 동안 진짜로 울린 알림의 15·30·60분 뒤. 억제된 것·켤 때 이미 넘어 있던 것은 뺐다. 기준은 없다.</p>
{live_table()}</section>""",
    ]
    page = (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>채점 보고서</title><style>{CSS}</style></head><body><main>{''.join(sections)}</main></body></html>")
    rp.CACHE.mkdir(exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    return OUT


def main():
    ap = argparse.ArgumentParser(description="채점 보고서 페이지 (replay_cache/report.html)")
    ap.add_argument("--fetch", action="store_true", help="1분봉을 이어 받은 뒤 만들기")
    ap.add_argument("--open", action="store_true", help="다 만들면 브라우저로 열기")
    ap.add_argument("--cost", type=float, default=COST, help=f"사고팔 때 드는 비용 가정, %% 왕복 (기본 {COST})")
    args = ap.parse_args()
    out = build(offline=not args.fetch, cost=args.cost)
    print(f"\n{out} 에 썼다. 웹 서버가 켜져 있으면 http://localhost:8000/report")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
