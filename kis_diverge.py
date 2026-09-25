#!/usr/bin/env python3
"""
kis_diverge.py — RSI·MACD 다이버전스를 기계적으로 찾는다 (되감기·채점용, 서버 알림에는 아직 안 쓴다).

「가격은 더 내려갔는데(저점이 낮아졌는데) RSI 는 덜 빠졌다(저점이 높아졌다)」 = 상승 다이버전스, 그 반대 = 하락 다이버전스.
차트를 눈으로 보고 고르면 채점을 할 수 없으니 다음처럼 정한다 (닫힌 봉만 쓴다):

  저점·고점   앞뒤 PIVOT_K 봉보다 낮은 저가(높은 고가)를 낸 봉. 뒤 PIVOT_K 봉이 닫혀야 알 수 있으니 그만큼 늦게 확인된다.
  다이버전스  확인된 저점 p 와, 그 앞 LOOKBACK 봉 안의 바로 앞 저점 q 를 견준다.
              가격 저가[p] < 저가[q] 이고 RSI[p] > RSI[q] 면 상승 (고점은 반대로 하락).
  히든        가격 저점은 높아졌는데(저가[p] > 저가[q]) RSI 저점은 낮아졌다(RSI[p] < RSI[q]) = 상승 히든 (고점은 반대로 하락 히든).
              오르는 흐름 안의 되돌림이 끝났다는 뜻으로 읽는다 (카드웰의 「역전」). 보통 다이버전스는 흐름이 꺾인다는 쪽이다.
  이중        같은 두 점에서 MACD 히스토그램도 같은 쪽으로 어긋났다 (hist[p] > hist[q], 하락은 반대).
  지지·저항   p 의 저가(고가)가 전날 저가(고가)에서 변동폭(최근 14봉 평균 진폭)의 절반 안.
              전날 선을 잠깐 깨고 다이버전스가 나면 가짜 돌파로 본다는 영상의 생각을 흉내 낸 것이다.

신호는 p 가 확인되는 봉(p + PIVOT_K)이 닫힐 때 난다. RSI 는 종가 RSI.
"""
from dataclasses import dataclass

import kis_rsi as k

PIVOT_K = 3       # 저점·고점: 앞뒤 이만큼 봉보다 낮거나 높아야
LOOKBACK = 30     # 앞 저점·고점을 이만큼 봉 안에서 찾는다
MIN_GAP = 4       # 두 점이 이만큼 봉은 떨어져야 (붙은 두 봉은 같은 움직임)
RANGE_N = 14      # 지지·저항 거리를 잴 변동폭: 최근 이만큼 봉의 평균 진폭


@dataclass
class Divergence:
    confirm: int      # 신호가 나는 봉 (이 봉이 닫힐 때)
    pivot: int        # 이번 저점·고점 봉
    prev: int         # 견준 앞 저점·고점 봉
    up: bool          # True 면 상승 다이버전스 (오를 쪽)
    macd: bool        # MACD 히스토그램도 어긋났나
    level: bool       # 전날 저가·고가 근처였나
    hidden: bool = False   # 히든 다이버전스 (흐름이 이어진다는 쪽)


def pivots(values, k=None, low=True, right=None):
    """values 에서 앞 k 개(없으면 PIVOT_K)·뒤 right 개(없으면 k)보다 엄격히 낮은(low) 또는 높은 점의 번호들."""
    k = PIVOT_K if k is None else k
    right = k if right is None else right
    out = []
    for i in range(k, len(values) - right):
        v = values[i]
        side = values[i - k:i] + values[i + 1:i + right + 1]
        if (low and all(v < x for x in side)) or (not low and all(v > x for x in side)):
            out.append(i)
    return out


def prev_day_levels(bars):
    """봉마다 전날(그 봉 날짜 앞의 마지막 거래일) 저가·고가. 첫날은 None."""
    days = {}
    for b in bars:
        d = b["time_us"][:8]
        lo, hi = days.get(d, (b["low"], b["high"]))
        days[d] = (min(lo, b["low"]), max(hi, b["high"]))
    order = sorted(days)
    prev = {d: days[order[i - 1]] if i else None for i, d in enumerate(order)}
    return [prev[b["time_us"][:8]] for b in bars]


def find(bars, period=14, hidden=False, right=None):
    """닫힌 봉들에서 난 다이버전스 전부 (확인된 차례대로). hidden 이면 히든 다이버전스도 (hidden=True 로 표시).
    right 를 주면 저점·고점의 뒤쪽은 그만큼 봉만 보고 그만큼 일찍 확인한다 (앞쪽은 PIVOT_K 그대로)."""
    right = PIVOT_K if right is None else right
    closes = [b["close"] for b in bars]
    rsi = k.rsi_series(closes, period)
    _, _, hist = k.macd_series(closes)
    levels = prev_day_levels(bars)
    ranges = [b["high"] - b["low"] for b in bars]
    out = []
    for up in (True, False):
        price = [b["low"] if up else b["high"] for b in bars]
        piv = [p for p in pivots(price, PIVOT_K, low=up, right=right) if rsi[p] is not None]
        for n, p in enumerate(piv):
            q = next((q for q in reversed(piv[:n]) if MIN_GAP <= p - q <= LOOKBACK), None)
            if q is None:
                continue
            lower = price[p] < price[q] if up else price[p] > price[q]
            weaker = rsi[p] > rsi[q] if up else rsi[p] < rsi[q]
            stronger = rsi[p] < rsi[q] if up else rsi[p] > rsi[q]
            if lower and weaker:
                kind = False
                macd = hist[p] > hist[q] if up else hist[p] < hist[q]
            elif hidden and price[p] != price[q] and not lower and stronger:
                kind = True
                macd = hist[p] < hist[q] if up else hist[p] > hist[q]
            else:
                continue
            lv, near = levels[p], False
            if lv and p >= RANGE_N:
                rng = sum(ranges[p - RANGE_N:p]) / RANGE_N
                near = abs(price[p] - (lv[0] if up else lv[1])) <= rng / 2
            out.append(Divergence(p + right, p, q, up, macd, near, kind))
    return sorted(out, key=lambda d: d.confirm)
