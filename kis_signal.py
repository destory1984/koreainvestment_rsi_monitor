"""RSI 로 매수·매도 시그널을 낸다. webull_rsi_monitor 의 rsi_signal.py 규칙에서 방아쇠를 바꿨다.

  무장    닫힌 봉 안에서 RSI 가 35 이하로 가면 매수 쪽을, 65 이상으로 가면 매도 쪽을
          12봉 동안 무장한다. 무장 동안 RSI 가 가장 낮았던(매수)/높았던(매도) 값을 기억한다 (등급에 쓴다).
  방아쇠  무장한 동안 닫힌 봉의 종가 RSI 가 선 안으로 돌아오면
          (앞 봉 종가 RSI 가 35 이하 → 이번 봉 35 위 = 매수 / 65 이상 → 65 밑 = 매도).
          봉 안에서 더 치솟았다가 그 봉에서 바로 돌아와도 낸다 (09-25 TSLA 07:40 이 그랬다).
  등급    무장 기간에 RSI 가 30/70 까지 갔으면 '강', 아니면 '약'
  추세    매수 때 MACD > 0 (매도 때 < 0) 이면 '추세 순응', 반대면 '역추세'

시그널을 낸 쪽은 무장을 푼다. RSI 가 다시 선을 넘어야 무장한다.

방아쇠는 원래 「MACD 히스토그램 부호가 바뀔 때」였다. 히스토그램은 꼭대기에서 한참 줄어야 부호가 바뀌어
시그널이 극값에서 30분(중앙값) 늦게 났다 — 09-25 TSLA 는 07:35 꼭대기 382.56, 시그널 08:10 381.50.
DB 에 쌓인 13종목 21,108봉으로 견준 60분 뒤 (늦음은 극값 봉에서 시그널 봉까지 중앙값):
  MACD 부호 (옛 규칙)                    654개  30분  맞음 49%  초과 +0.056
  RSI 복귀, 극값 봉은 빼고                782개  10분       51%       +0.062
  RSI 복귀, 극값 봉에서 돌아와도 (지금)  1012개   5분       50%       +0.048
어느 것도 방향을 잘 맞히지는 않고(50% 언저리), 차이는 주로 얼마나 제때 나느냐다. 사용자 예(TSLA 07:40)를
잡는 마지막 것으로 했다. 시그널이 30% 쯤 많아진다. 10-02 채점 때 다시 본다.

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


def signals(bars, period=14, arm_bars=ARM_BARS, lines=None):
    """닫힌 봉들에서 난 시그널 전부 (오래된 것부터). lines 는 그 종목의 RSI 선 (없으면 기본)."""
    ln = lines or al.DEFAULT_LINES
    band = rsi_band(bars, period)
    line, sig, hist = k.macd_series([b["close"] for b in bars])
    out = []
    # 쪽마다 [남은 무장 봉, 극값 RSI, 극값 봉 번호]
    arm = {"buy": [0, 100.0, None], "sell": [0, 0.0, None]}

    def disarm(side):
        arm[side] = [0, 100.0 if side == "buy" else 0.0, None]

    for i, b in enumerate(bars):
        r, lo, hi = band[i]
        if r is None:
            continue
        for side in arm:
            arm[side][0] = max(0, arm[side][0] - 1)
            if not arm[side][0]:
                disarm(side)
        if lo <= ln.lower:
            a = arm["buy"]
            a[0] = arm_bars
            if lo < a[1]:
                a[1], a[2] = lo, i
        if hi >= ln.upper:
            a = arm["sell"]
            a[0] = arm_bars
            if hi > a[1]:
                a[1], a[2] = hi, i
        prev = band[i - 1][0] if i else None
        if prev is None:
            continue
        for side in ("buy", "sell"):
            left, ext, at = arm[side]
            if not left or at is None:
                continue
            buy = side == "buy"
            if (prev <= ln.lower < r) if buy else (prev >= ln.upper > r):
                out.append(Signal(side, "강" if (ext <= ln.strong_lower if buy else ext >= ln.strong_upper) else "약",
                                  "추세 순응" if (line[i] > 0 if buy else line[i] < 0) else "역추세",
                                  b["time_us"], r, ext, line[i], sig[i], hist[i]))
                disarm(side)
                break
    return out
