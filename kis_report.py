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


def build(offline=True, say=print):
    tf.k.load_kr_market()
    tickers = rp.collect_tickers()
    all_rows, bases, per_symb, ndays, info = tf.analyze(tickers, tf.TFS, offline=offline, say=say,
                                                        line_sets=tf.LINE_SETS)
    if not all_rows:
        raise SystemExit("채점할 것이 없다 (1분봉이 없다 — --fetch 로 받거나 수집을 기다린다).")
    per_day = sum(ndays.values())   # 종목 하나 하루
    days = [i["from"] for i in info.values()] + [i["to"] for i in info.values()]
    span = f"{min(days)[:4]}-{min(days)[4:6]}-{min(days)[6:]} ~ {max(days)[:4]}-{max(days)[4:6]}-{max(days)[6:]}"
    us = lambda r: not r["symb"].isdigit()
    five = all_rows.get(5, [])
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
    args = ap.parse_args()
    out = build(offline=not args.fetch)
    print(f"\n{out} 에 썼다. 웹 서버가 켜져 있으면 http://localhost:8000/report")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
