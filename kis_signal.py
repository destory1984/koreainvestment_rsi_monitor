"""RSI 와 MACD 를 엮어 매수·매도 시그널을 낸다. webull_rsi_monitor 의 rsi_signal.py 규칙을 옮겼다.

  무장    닫힌 봉 안에서 RSI 가 35 이하로 가면 매수 쪽을, 65 이상으로 가면 매도 쪽을
          12봉 동안 무장한다. RSI 바닥이 먼저 오고 MACD 전환은 몇 봉 늦게 오므로
          두 조건이 같은 봉에서 맞기를 기다리지 않는다.
  방아쇠  무장한 동안 닫힌 봉에서 MACD 히스토그램이 음→양(매수) / 양→음(매도)
  등급    무장 기간에 RSI 가 30/70 까지 갔으면 '강', 아니면 '약'
  추세    매수 때 MACD > 0 (매도 때 < 0) 이면 '추세 순응', 반대면 '역추세'

시그널을 낸 쪽은 무장을 푼다. RSI 가 다시 선을 넘어야 무장한다.

Webull 버전은 봉 안 RSI 최저·최고를 2분마다 화면에서 읽은 값으로 어림했다. 여기서는
봉의 저가·고가를 종가 자리에 넣어 그 봉의 RSI 를 계산한다. 마지막 봉의 RSI 는 종가가
오를수록 커지므로 이것이 봉 안에서 RSI 가 닿을 수 있던 최저·최고와 같다.

분봉 전체로 처음부터 다시 셈하므로 상태를 저장할 필요가 없다. 진행 중인 봉은 넣지 않는다
(봉이 닫히기 전에 되돌아가는 헛크로스를 잡지 않으려는 것이다).
"""
from dataclasses import dataclass

import kis_alert as al
import kis_rsi as k

ARM_BARS = 12


@dataclass
class Signal:
    side: str          # "buy" / "sell"
    grade: str         # "강" / "약"
    trend: str         # "추세 순응" / "역추세"
    bar: str           # 봉 시작 시각 'YYYYMMDD HHMMSS' (현지)
    rsi: float         # 그 봉 마감 RSI
    extreme: float     # 무장 기간의 RSI 최저(매수) / 최고(매도)
    macd: float
    signal: float
    hist: float

    @property
    def word(self):
        return "매수" if self.side == "buy" else "매도"


def rsi_band(bars, period=14):
    """봉마다 (마감 RSI, 봉 안 최저 RSI, 봉 안 최고 RSI). 앞쪽 period 개는 None."""
    closes = [b["close"] for b in bars]
    n = len(closes)
    out = [(None, None, None)] * n
    if n <= period + 1:
        return out
    ups = [max(closes[i] - closes[i - 1], 0) for i in range(1, n)]
    downs = [max(closes[i - 1] - closes[i], 0) for i in range(1, n)]
    au, ad = sum(ups[:period]) / period, sum(downs[:period]) / period

    def rsi(u, d):
        return 100.0 if d == 0 else 100 - 100 / (1 + u / d)

    for i in range(period + 1, n):
        prev = closes[i - 1]

        def step(price):
            diff = price - prev
            return ((au * (period - 1) + max(diff, 0)) / period,
                    (ad * (period - 1) + max(-diff, 0)) / period)

        lo = rsi(*step(bars[i]["low"]))
        hi = rsi(*step(bars[i]["high"]))
        au, ad = step(closes[i])
        out[i] = (rsi(au, ad), lo, hi)
    return out


def signals(bars, period=14, arm_bars=ARM_BARS):
    """닫힌 봉들에서 난 시그널 전부 (오래된 것부터)."""
    band = rsi_band(bars, period)
    line, sig, hist = k.macd_series([b["close"] for b in bars])
    out = []
    buy_left = sell_left = 0
    buy_ext, sell_ext = 100.0, 0.0
    for i, b in enumerate(bars):
        r, lo, hi = band[i]
        if r is None:
            continue
        buy_left, sell_left = max(0, buy_left - 1), max(0, sell_left - 1)
        if not buy_left:
            buy_ext = 100.0
        if not sell_left:
            sell_ext = 0.0
        if lo <= al.LOWER:
            buy_left, buy_ext = arm_bars, min(buy_ext, lo)
        if hi >= al.UPPER:
            sell_left, sell_ext = arm_bars, max(sell_ext, hi)
        prev = hist[i - 1] if i else None
        if prev is None:
            continue
        if buy_left and prev <= 0 < hist[i]:
            out.append(Signal("buy", "강" if buy_ext <= al.STRONG_LOWER else "약",
                              "추세 순응" if line[i] > 0 else "역추세",
                              b["time_us"], r, buy_ext, line[i], sig[i], hist[i]))
            buy_left, buy_ext = 0, 100.0
        elif sell_left and prev >= 0 > hist[i]:
            out.append(Signal("sell", "강" if sell_ext >= al.STRONG_UPPER else "약",
                              "추세 순응" if line[i] < 0 else "역추세",
                              b["time_us"], r, sell_ext, line[i], sig[i], hist[i]))
            sell_left, sell_ext = 0, 0.0
    return out
