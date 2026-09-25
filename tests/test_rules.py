"""알림·시그널·소리 가리기·되감기 규칙 시험. 네트워크와 실제 기록 파일은 건드리지 않는다.

  python -m unittest discover tests
"""
import asyncio
import json
import math
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kis_alert as al      # noqa: E402
import kis_replay as rp     # noqa: E402
import kis_rsi as k         # noqa: E402
import kis_signal as ks     # noqa: E402
import kis_telegram as tg   # noqa: E402
import kis_tf as tf         # noqa: E402
import kis_web as w         # noqa: E402

T0 = datetime(2026, 9, 24, 10, 0)


def bars_from(closes, t0=T0, gap_at=None, spread=0.0):
    """5분봉. gap_at 번째 봉 앞에 15시간(장 닫힘)을 둔다."""
    out, t = [], t0
    for i, c in enumerate(closes):
        if i == gap_at:
            t += timedelta(hours=15)
        out.append({"time_us": t.strftime("%Y%m%d %H%M%S"), "open": c, "high": c + spread,
                    "low": c - spread, "close": c, "volume": 1})
        t += timedelta(minutes=5)
    return out


def temp_path(suffix):
    return Path(tempfile.mkdtemp()) / f"t{suffix}"


# ── 선 알림 (Gate) ─────────────────────────────────────────────
N = 1_000_000_000   # 시각(초). 첫 쿨다운이 0 초부터 세어지므로 실제 시각처럼 큰 값에서 시작한다


class GateTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(al.level_of(65.0), ("neutral", ""))     # 선 위가 아니라 넘어야
        self.assertEqual(al.level_of(65.01), ("above", "warn"))
        self.assertEqual(al.level_of(70.01), ("above", "strong"))
        self.assertEqual(al.level_of(34.99), ("below", "warn"))
        self.assertEqual(al.level_of(29.99), ("below", "strong"))

    def test_first_reading_beyond_line_is_start(self):
        a = al.Gate().check(72, now=N + 0)
        self.assertTrue(a["start"])
        self.assertEqual((a["zone"], a["strength"], a["edge"]), ("above", "strong", 70))

    def test_cross_and_escalate(self):
        g = al.Gate()
        self.assertIsNone(g.check(50, now=N + 0))
        a = g.check(66, now=N + 10)
        self.assertEqual((a["kind"], a["start"], a.get("suppressed")), ("above", False, None))
        self.assertIsNone(g.check(67, now=N + 20))                  # 같은 구간이면 조용
        self.assertEqual(g.check(71, now=N + 30)["kind"], "above_strong")

    def test_rearm_needs_margin(self):
        g = al.Gate()
        g.check(50, now=N + 0)
        g.check(66, now=N + 0)
        g.check(60, now=N + 10_000)                                 # 58 까지 안 내려옴
        a = g.check(66, now=N + 10_001)
        self.assertIn("재무장", a["suppressed"])

    def test_cooldown(self):
        g = al.Gate()
        g.check(50, now=N + 0)
        g.check(66, now=N + 0)
        g.check(57, now=N + 60)                                     # 재무장
        self.assertIn("쿨다운", g.check(66, now=N + 120)["suppressed"])
        g.check(57, now=N + 300)
        self.assertNotIn("suppressed", g.check(66, now=N + al.ALERT_COOLDOWN_SEC + 1))

    def test_below_side(self):
        g = al.Gate()
        g.check(50, now=N + 0)
        self.assertEqual(g.check(34, now=N + 0)["kind"], "below")
        g.check(40, now=N + 10_000)                                 # 42 까지 안 올라옴
        self.assertIn("42 이상", g.check(34, now=N + 10_001)["suppressed"])


# ── 종목별 선 ──────────────────────────────────────────────────
class LinesTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(al.Lines.parse([25, 75]), al.Lines(20, 25, 75, 80))
        self.assertEqual(al.Lines.parse(["20", "25", "75", "80"]), al.Lines(20, 25, 75, 80))
        for bad in ([35, 30, 65, 70], [30, 35, 65], [-5, 0, 65, 70], [30, 35, 65, 101], [50, 50]):
            with self.assertRaises(ValueError, msg=bad):
                al.Lines.parse(bad)

    def test_gate_uses_lines(self):
        g = al.Gate(al.Lines(20, 25, 75, 80))
        g.check(50, now=N)
        self.assertIsNone(g.check(70, now=N + 1))              # 기본이면 70 초과지만 이 종목은 밴드 안
        a = g.check(76, now=N + 2)
        self.assertEqual((a["kind"], a["edge"]), ("above", 75))
        g.check(70, now=N + 10_000)                             # 75 - 7 = 68 까지 안 내려옴 → 재무장 안 됨
        self.assertIn("68 이하", g.check(76, now=N + 10_001)["suppressed"])

    def test_signals_use_lines(self):
        bars = SignalTest().v_shape(15, 15)
        low = min(r for r, lo, hi in ks.rsi_band(bars) if r is not None)
        wide = al.Lines(low - 10, low - 5, 95, 99)             # 떨어진 RSI 보다 더 아래에 선
        self.assertTrue(ks.signals(bars))
        self.assertEqual([x for x in ks.signals(bars, lines=wide) if x.side == "buy"], [])

    def test_server_set_lines(self):
        h = w.Hub.__new__(w.Hub)
        h.settings = {"lines": {}}
        h.books = {"SOXL": types.SimpleNamespace(symb="SOXL", bars=SignalTest().v_shape(15, 15))}
        h.gates, h.signals, h.sup_seen, h.period = {}, {}, {("SOXL", "above", "쿨다운")}, 14
        h.prefetch = lambda books: None
        sent = []

        async def bc(m):
            sent.append(m)
        h.broadcast = bc
        h.row = lambda b: {"symb": b.symb, "lines": h.lines(b.symb)._asdict()}
        with mock.patch.object(w, "SETTINGS", temp_path(".json")):
            asyncio.run(h.set_lines("SOXL", [25, 75]))
            self.assertEqual(h.settings["lines"]["SOXL"], [20, 25, 75, 80])
            self.assertEqual(h.gates["SOXL"].lines, al.Lines(20, 25, 75, 80))
            self.assertEqual(h.sup_seen, set())
            self.assertEqual(sent[-1]["rows"][0]["lines"]["upper"], 75)
            asyncio.run(h.set_lines("SOXL", [30, 35, 65, 70]))       # 기본과 같으면 설정에서 지운다
            self.assertNotIn("SOXL", h.settings["lines"])
            with self.assertRaises(ValueError):
                asyncio.run(h.set_lines("SOXL", [70, 30]))
        h.settings["lines"]["X"] = "망가진 값"
        self.assertEqual(h.lines("X"), al.DEFAULT_LINES)

    def test_replay_lines_for(self):
        self.assertEqual(rp.lines_for("A", "25,75"), al.Lines(20, 25, 75, 80))
        with mock.patch.object(rp, "SETTINGS", temp_path(".json")):
            self.assertEqual(rp.lines_for("A"), al.DEFAULT_LINES)
            rp.SETTINGS.write_text(json.dumps({"lines": {"A": [10, 20, 80, 90]}}), encoding="utf-8")
            self.assertEqual(rp.lines_for("A"), al.Lines(10, 20, 80, 90))


# ── RSI·시그널 ─────────────────────────────────────────────────
class SignalTest(unittest.TestCase):
    def test_band_close_matches_rsi_series(self):
        closes = [100 + (i % 7) - (i % 3) * 1.5 for i in range(60)]
        band = ks.rsi_band(bars_from(closes, spread=0.5))
        want = k.rsi_series(closes)
        for (rc, lo, hi), r in zip(band, want):
            if rc is None:
                continue
            self.assertAlmostEqual(rc, r, places=9)
            self.assertLessEqual(lo, rc)
            self.assertGreaterEqual(hi, rc)

    @staticmethod
    def wiggle(n, base=100.0):
        """RSI 50 언저리로 조금씩 오르내림 (아주 평평하면 RSI 가 100 이 된다)"""
        return [base + (0.2 if i % 2 else -0.2) for i in range(n)]

    def v_shape(self, down, up):
        """옆걸음 → 떨어짐 → 오름. 떨어질 때 RSI 가 선 밑으로, 오를 때 MACD 히스토그램이 양으로."""
        closes = self.wiggle(40) + [100 - 0.8 * i for i in range(1, down + 1)]
        closes += [closes[-1] + 0.9 * i for i in range(1, up + 1)]
        return bars_from(closes)

    def test_buy_after_drop_and_rebound(self):
        sig = ks.signals(self.v_shape(15, 15))
        self.assertTrue(sig)
        self.assertEqual((sig[0].side, sig[0].grade), ("buy", "강"))
        self.assertLessEqual(sig[0].extreme, al.STRONG_LOWER)

    def test_sell_is_mirror(self):
        bars = self.v_shape(15, 15)
        for b in bars:
            for f in ("open", "high", "low", "close"):
                b[f] = 200 - b[f]
        sig = ks.signals(bars)
        self.assertEqual((sig[0].side, sig[0].grade), ("sell", "강"))

    def test_no_signal_without_arming(self):
        self.assertEqual(ks.signals(bars_from(self.wiggle(200))), [])

    def test_sell_on_bar_that_spikes_then_closes_back(self):
        # 09-25 TSLA 07:40 처럼: 앞 봉 종가 RSI 가 65 위, 이번 봉은 봉 안에서 더 치솟았다가 65 밑으로 닫힘
        closes = self.wiggle(40) + [100 + 0.5 * i for i in range(1, 9)]
        bars = bars_from(closes)
        top = closes[-1]
        bars.append({"time_us": "20260924 134000", "open": top, "high": top + 1.0, "low": top - 1.2,
                     "close": top - 1.2, "volume": 1})
        band = ks.rsi_band(bars)
        self.assertGreaterEqual(band[-2][0], al.UPPER)               # 앞 봉 종가는 65 위
        self.assertLess(band[-1][0], al.UPPER)                       # 이번 봉 종가는 65 밑
        self.assertGreater(band[-1][2], band[-2][2])                 # 봉 안 최고는 더 높았다
        sig = ks.signals(bars)
        self.assertEqual((sig[-1].side, sig[-1].bar), ("sell", "20260924 134000"))

    def test_signal_comes_when_rsi_returns(self):
        # 옛 규칙(MACD 부호)은 꼭대기에서 한참 뒤에 났다. 지금은 RSI 가 선 안으로 돌아오는 봉에서 난다
        bars = self.v_shape(15, 15)
        band = ks.rsi_band(bars)
        i = next(j for j, b in enumerate(bars) if b["time_us"] == ks.signals(bars)[0].bar)
        self.assertLessEqual(band[i - 1][0], al.LOWER)
        self.assertGreater(band[i][0], al.LOWER)

    def test_no_signal_when_arming_is_off(self):
        self.assertEqual(ks.signals(self.v_shape(15, 15), arm_bars=0), [])


# ── 소리 가리기 ────────────────────────────────────────────────
class FakeNow(datetime):
    fixed = datetime(2026, 9, 25, 12, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


class MutedTest(unittest.TestCase):
    def hub(self, **settings):
        h = w.Hub.__new__(w.Hub)
        h.settings = {"quiet": {"on": False, "from": "00:00", "to": "07:00"},
                      "sound_sessions": {key: True for key in w.SOUND_SESSIONS}, "mute": []}
        h.settings.update(settings)
        return h

    def muted(self, h, excd="NAS", symb="MU", at=(12, 0), session="regular"):
        FakeNow.fixed = datetime(2026, 9, 25, *at)
        with mock.patch.object(w, "datetime", FakeNow), mock.patch.object(k, "us_session", lambda: session):
            return h.muted(types.SimpleNamespace(excd=excd, symb=symb))

    def test_nothing_muted(self):
        self.assertEqual(self.muted(self.hub()), "")

    def test_ticker_mute_comes_first(self):
        h = self.hub(mute=["MU"], quiet={"on": True, "from": "00:00", "to": "23:59"})
        self.assertEqual(self.muted(h), "종목 소리 끔")
        self.assertEqual(self.muted(h, symb="TSLA", at=(12, 0)), "조용한 시각 00:00~23:59")

    def test_quiet_across_midnight(self):
        h = self.hub(quiet={"on": True, "from": "23:00", "to": "07:00"})
        self.assertTrue(self.muted(h, at=(23, 30)))
        self.assertTrue(self.muted(h, at=(6, 59)))
        self.assertEqual(self.muted(h, at=(7, 0)), "")
        self.assertEqual(self.muted(h, at=(22, 59)), "")

    def test_session_off(self):
        h = self.hub()
        h.settings["sound_sessions"]["pre"] = False
        self.assertEqual(self.muted(h, session="pre"), "미국 프리장 소리 끔")
        self.assertEqual(self.muted(h, session="regular"), "")
        h.settings["sound_sessions"]["kr"] = False
        self.assertEqual(self.muted(h, excd="KRX", symb="005930"), "국내 종목 소리 끔")

    def test_market_closed(self):
        h = self.hub()
        h.kr_closed = ["2026-09-25"]
        kr, us = types.SimpleNamespace(excd="KRX"), types.SimpleNamespace(excd="NAS")
        for day, want in ((datetime(2026, 9, 25, 10), True),      # 추석
                          (datetime(2026, 9, 26, 10), True),      # 토요일
                          (datetime(2026, 9, 28, 10), False),     # 월요일 장중
                          (datetime(2026, 9, 28, 20), False)):    # 월요일 장 마감 뒤 — 쉬는 날은 아니다
            FakeNow.fixed = day
            with mock.patch.object(w, "datetime", FakeNow):
                self.assertEqual(h.market_closed(kr), want, day)
        with mock.patch.object(k, "us_session", lambda: None):
            self.assertTrue(h.market_closed(us))
        with mock.patch.object(k, "us_session", lambda: "after"):
            self.assertFalse(h.market_closed(us))

    def test_no_alert_on_closed_day(self):
        h = self.hub()
        h.kr_closed = []
        b = types.SimpleNamespace(excd="KRX", symb="005930", name="삼성전자", price=1.0,
                                  bars=bars_from([1, 2, 3]), indicators=lambda p: {"rsi": 80.0})
        h.books, h.gates, h.events, h.period = {"005930": b}, {"005930": al.Gate()}, deque(), 14
        FakeNow.fixed = datetime(2026, 9, 26, 10)                     # 토요일
        with mock.patch.object(w, "datetime", FakeNow), mock.patch.object(w, "ALERT_LOG", temp_path(".jsonl")):
            h.check_alerts({"005930"})
        self.assertEqual(list(h.events), [])
        self.assertEqual(h.gates["005930"].level, ("unknown", ""))  # 장이 열리면 처음부터 본다

    def test_set_mute(self):
        h = self.hub()
        h.books = {"MU": types.SimpleNamespace(symb="MU")}
        sent = []

        async def bc(m):
            sent.append(m)
        h.broadcast = bc
        h.row = lambda b: {"symb": b.symb, "mute": b.symb in h.settings["mute"]}
        with mock.patch.object(w, "SETTINGS", temp_path(".json")):
            asyncio.run(h.set_mute("MU", True))
            asyncio.run(h.set_mute("MU", True))
            self.assertEqual(h.settings["mute"], ["MU"])
            self.assertTrue(sent[-1]["rows"][0]["mute"])
            asyncio.run(h.set_mute("MU", False))
            self.assertEqual(h.settings["mute"], [])
            with self.assertRaises(KeyError):
                asyncio.run(h.set_mute("XX", True))


# ── 알림 뒤 결과 ───────────────────────────────────────────────
class AfterTest(unittest.TestCase):
    def setUp(self):
        self.log = temp_path(".jsonl")
        patcher = mock.patch.object(w, "ALERT_LOG", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bar0 = w.epoch(T0.strftime("%Y%m%d %H%M%S"))

    def hub(self, books, events):
        h = w.Hub.__new__(w.Hub)
        h.books = {s: types.SimpleNamespace(bars=b) for s, b in books.items()}
        h.events = deque(events)
        return h

    def ev(self, **kw):
        e = {"ts": 1.0, "symb": "A", "bar": self.bar0, "price": 100.0, "zone": "below",
             "suppressed": "", "text": "35 미만"}
        e.update(kw)
        return e

    def test_signs_and_log(self):
        closes = [100 + i for i in range(20)]
        h = self.hub({"A": bars_from(closes)}, [
            self.ev(), self.ev(ts=2.0, zone="above", text="65 초과"),
            self.ev(ts=3.0, type="signal", side="sell", zone="below", price=None)])
        changed = h.fill_after()
        self.assertEqual(len(changed), 3)
        self.assertEqual(h.events[0]["after"], {"15": 3.0, "30": 6.0, "60": 12.0})
        self.assertEqual(h.events[1]["after"]["60"], -12.0)     # 초과는 내려야 +
        self.assertEqual(h.events[2]["after"]["60"], -12.0)     # 매도 시그널, 값이 없으면 알림 봉 종가
        lines = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([x["of"] for x in lines], [1.0, 2.0, 3.0])
        self.assertEqual(h.fill_after(), [])                    # 다 정해진 것은 다시 안 본다

    def test_waits_for_bar_to_close(self):
        bars = bars_from([100 + i for i in range(13)])          # 60분 뒤 봉이 마지막(진행 중)
        h = self.hub({"A": bars}, [self.ev()])
        h.fill_after()
        self.assertEqual(h.events[0]["after"], {"15": 3.0, "30": 6.0})
        self.assertFalse(self.log.exists())
        bars.append({**bars[-1], "time_us": (T0 + timedelta(minutes=65)).strftime("%Y%m%d %H%M%S")})
        h.fill_after()
        self.assertEqual(h.events[0]["after"]["60"], 12.0)
        self.assertTrue(self.log.exists())

    def test_market_closed_in_between(self):
        h = self.hub({"A": bars_from([100 + i for i in range(20)], gap_at=8)}, [self.ev()])
        h.fill_after()
        self.assertEqual(h.events[0]["after"], {"15": 3.0, "30": 6.0, "60": None})

    def test_skips_suppressed_and_start(self):
        h = self.hub({"A": bars_from([100 + i for i in range(20)])}, [
            self.ev(suppressed="쿨다운"), self.ev(ts=2.0, start=True),
            self.ev(ts=3.0, text="35 미만 (시작 때부터)")])
        self.assertEqual(h.fill_after(), [])
        self.assertNotIn("after", h.events[0])

    def test_scores(self):
        rows = [self.ev(), self.ev(ts=2.0), self.ev(ts=3.0, suppressed="쿨다운"), self.ev(ts=4.0, start=True),
                self.ev(ts=5.0, type="signal", side="sell", strength="warn", text="매도 시그널 (약, 역추세)"),
                self.ev(ts=6.0, symb="B"),
                {"type": "after", "of": 1.0, "symb": "A", "after": {"15": 1.0, "30": 0.0, "60": None}},
                {"type": "after", "of": 2.0, "symb": "A", "after": {"15": -0.5, "30": 0.0, "60": 2.0}},
                {"type": "after", "of": 3.0, "symb": "A", "after": {"15": 9, "30": 9, "60": 9}},
                {"type": "after", "of": 4.0, "symb": "A", "after": {"15": 9, "30": 9, "60": 9}},
                {"type": "after", "of": 5.0, "symb": "A", "after": {"15": 0.3, "30": 0.3, "60": 0.3}},
                {"type": "after", "of": 6.0, "symb": "B", "after": {"15": 1, "30": 1, "60": 1}}]
        self.log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        got = {r["kind"]: r for r in w.Hub.scores("A")}
        self.assertEqual(list(got), ["35 미만", "매도 약"])              # 억제·켤 때 것은 뺀다, B 는 안 셈
        a = got["35 미만"]
        self.assertEqual((a["15"]["n"], a["15"]["hit"], a["15"]["avg"]), (2, 0.5, 0.25))
        self.assertEqual((a["30"]["n"], a["30"]["hit"]), (2, None))      # 보합뿐이면 맞음 없음
        self.assertEqual(a["60"]["n"], 1)                                 # 장 닫힘(None)은 안 셈
        self.assertEqual(len(w.Hub.scores()), 2)
        self.assertEqual(w.Hub.scores()[0]["15"]["n"], 3)                 # 전체면 B 도

    def test_read_log_attaches_after(self):
        rows = [self.ev(), self.ev(ts=2.0),
                {"type": "after", "of": 1.0, "symb": "A", "after": {"15": 1, "30": 2, "60": 3}}]
        self.log.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
        got = w.Hub.read_log()
        self.assertEqual([e["ts"] for e in got], [2.0, 1.0])  # 최근 것이 앞, after 줄은 알림이 아님
        self.assertEqual(got[1]["after"]["60"], 3)
        self.assertNotIn("after", got[0])


# ── 서버가 분봉을 DB 에 쌓기 ───────────────────────────────────
class StoreTest(unittest.TestCase):
    def setUp(self):
        cache = Path(tempfile.mkdtemp())
        for name, val in (("CACHE", cache), ("DB", cache / "bars.db")):
            patcher = mock.patch.object(rp, name, val)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.h = w.Hub.__new__(w.Hub)
        self.h.nmin = 5

    def test_api_bars_replace_live_bars_do_not(self):
        api = bars_from([100, 101, 102])
        self.h.store([("NAS", "TSLA", api, True)], night=("NAS", "TSLA", True))
        live = dict(api[1], close=555)                                         # 실시간으로 만든, 조금 다른 봉
        newer = dict(api[2], time_us="20260924 101500", close=103)
        self.h.books = {"TSLA": types.SimpleNamespace(excd="NAS", symb="TSLA", bars=[live, newer, dict(newer)])}
        with mock.patch.object(w.threading, "Thread",
                               lambda target, args, daemon: types.SimpleNamespace(start=lambda: target(*args))):
            self.h.store_closed({"TSLA"})
            self.h.books["TSLA"].bars = [live, newer]                         # 닫힌 봉(live)이 DB 에 이미 있다
            self.h.store_closed({"TSLA"})
        con = rp.db()
        got = rp.get_bars(con, "NAS", "TSLA", 5)
        self.assertEqual([b["close"] for b in got], [100, 101, 102, 103])     # 101 은 API 값 그대로
        self.assertTrue(rp.get_night(con, "NAS", "TSLA", 5))

    def test_store_failure_does_not_raise(self):
        with mock.patch.object(rp, "db", side_effect=sqlite3.OperationalError("database is locked")), \
                mock.patch("builtins.print") as out:
            self.h.store([("NAS", "TSLA", bars_from([1]), True)])
        self.assertIn("못 씀", out.call_args[0][0])


# ── 휴장일·세션 ────────────────────────────────────────────────
class SessionTest(unittest.TestCase):
    def at(self, s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=k.NEW_YORK)

    def test_us_sessions(self):
        cases = {"2026-09-08 05:00": "pre", "2026-09-08 10:00": "regular", "2026-09-08 17:00": "after",
                 "2026-09-08 21:00": "day", "2026-09-09 03:00": "day",
                 "2026-09-12 10:00": None, "2026-09-11 21:00": None,       # 토요일, 금요일 밤
                 "2026-09-13 21:00": "day"}                                # 일요일 밤
        for t, want in cases.items():
            self.assertEqual(k.us_session(self.at(t)), want, t)

    def test_us_holiday(self):
        self.assertIsNone(k.us_session(self.at("2026-09-07 10:00")))      # 노동절
        self.assertIsNone(k.us_session(self.at("2026-09-06 21:00")))      # 노동절 전날 밤
        self.assertEqual(k.us_session(self.at("2026-09-07 21:00")), "day")
        self.assertIsNone(k.us_session(self.at("2026-11-26 10:00")))      # 추수감사절
        self.assertIsNone(k.us_session(self.at("2028-04-14 10:00")))      # 2028 굿프라이데이 — 목록 없이 셈
        self.assertEqual(k.us_session(self.at("2026-10-12 10:00")), "regular")   # 콜럼버스데이는 연다

    def test_us_early_close(self):
        # 추수감사절 다음 날: 정규장 13:00, 애프터 17:00 까지
        for t, want in {"2026-11-27 12:59": "regular", "2026-11-27 13:00": "after",
                        "2026-11-27 16:59": "after", "2026-11-27 17:00": None}.items():
            self.assertEqual(k.us_session(self.at(t)), want, t)
        # 알려진 날들 — 2021 은 7/3 이 토요일, 12/24 가 휴장(크리스마스 대체)이라 추수감사절 다음 날뿐
        self.assertEqual(k.us_early_closes([2021]), ["2021-11-26"])
        self.assertEqual(k.us_early_closes([2025]), ["2025-07-03", "2025-11-28", "2025-12-24"])
        self.assertEqual(k.us_early_closes([2026]), ["2026-11-27", "2026-12-24"])   # 7/3 은 휴장(금)

    def test_kr_holidays_cached_once_a_day(self):
        page = {"rt_cd": "0", "output": [
            {"bass_dt": "20260925", "opnd_yn": "N"}, {"bass_dt": "20260928", "opnd_yn": "Y"},
            {"bass_dt": "20261009", "opnd_yn": "N"}]}
        calls = []

        def get(*a, **kw):
            calls.append(kw["params"]["BASS_DT"])
            return types.SimpleNamespace(json=lambda: page, raise_for_status=lambda: None)
        with mock.patch.object(k, "HOLIDAY_CACHE", temp_path(".json")), \
                mock.patch.object(k, "get_token", lambda *a: "t"), mock.patch.object(k.requests, "get", get):
            self.assertEqual(k.kr_holidays("a", "s"), ["2026-09-25", "2026-10-09"])
            self.assertEqual(k.kr_holidays("a", "s"), ["2026-09-25", "2026-10-09"])
        self.assertEqual(len(calls), 1)


# ── 국내 분봉은 KRX 정규장만 ────────────────────────────────────
class KrBarsTest(unittest.TestCase):
    def test_drops_nextrade_minutes(self):
        day = "20260923"
        minutes = [f"{h:02d}{m:02d}00" for h in range(9, 20) for m in range(60)
                   if (h, m) <= (19, 59) and not ((15, 20) <= (h, m) < (15, 30))]
        minutes.reverse()                                       # 최신 것부터 온다
        pages = [minutes[i:i + 120] for i in range(0, len(minutes), 120)]

        def get(*a, **kw):
            page = pages.pop(0) if pages else []
            out = [{"stck_bsop_date": day, "stck_cntg_hour": t, "stck_prpr": "100", "stck_oprc": "100",
                    "stck_hgpr": "100", "stck_lwpr": "100", "cntg_vol": "1"} for t in page]
            return types.SimpleNamespace(json=lambda: {"rt_cd": "0", "output1": {}, "output2": out},
                                         raise_for_status=lambda: None)
        with mock.patch.object(k, "get_token", lambda *a: "t"), mock.patch.object(k.requests, "get", get), \
                mock.patch.object(k.time, "sleep", lambda s: None):
            bars = k.fetch_kr_bars("a", "s", "005930", 5, need=200)
        times = [b["time_us"][9:13] for b in bars]
        self.assertEqual(times[0], "0900")
        self.assertEqual(times[-1], "1530")                     # 종가 단일가 봉까지
        self.assertEqual(len(bars), 77)

    def test_unified_market_keeps_nextrade(self):
        day = "20260923"
        minutes = [f"{h:02d}{m:02d}00" for h in range(8, 20) for m in range(60)
                   if not ((8, 50) <= (h, m) < (9, 0))]
        minutes.reverse()
        pages = [minutes[i:i + 120] for i in range(0, len(minutes), 120)]
        codes = []

        def get(url, headers, params, timeout):
            codes.append(params["FID_COND_MRKT_DIV_CODE"])
            page = pages.pop(0) if pages else []
            out = [{"stck_bsop_date": day, "stck_cntg_hour": t, "stck_prpr": "100", "stck_oprc": "100",
                    "stck_hgpr": "100", "stck_lwpr": "100", "cntg_vol": "1"} for t in page]
            return types.SimpleNamespace(json=lambda: {"rt_cd": "0", "output1": {}, "output2": out},
                                         raise_for_status=lambda: None)
        self.addCleanup(k.set_kr_market, "krx")
        self.assertEqual(k.set_kr_market("unified"), "unified")
        self.assertEqual(k.KR_TR, "H0UNCNT0")
        with mock.patch.object(k, "get_token", lambda *a: "t"), mock.patch.object(k.requests, "get", get), \
                mock.patch.object(k.time, "sleep", lambda s: None):
            bars = k.fetch_kr_bars("a", "s", "005930", 5, need=500)
        times = [b["time_us"][9:13] for b in bars]
        self.assertEqual((times[0], times[-1]), ("0800", "1955"))
        self.assertEqual(set(codes), {"UN"})
        self.assertEqual(k.set_kr_market("모름"), "krx")                  # 모르는 이름이면 KRX
        self.assertEqual((k.KR_TR, k.KR_CODE, k.KR_CLOSE), ("H0STCNT0", "J", "153059"))

    def test_load_kr_market_from_settings(self):
        self.addCleanup(k.set_kr_market, "krx")
        path = temp_path(".json")
        path.write_text('{"kr_market": "unified"}', encoding="utf-8")
        self.assertEqual(k.load_kr_market(path), "unified")
        self.assertEqual(k.load_kr_market(temp_path(".json")), "krx")   # 파일이 없으면 KRX


# ── 텔레그램 ───────────────────────────────────────────────────
class TelegramTest(unittest.TestCase):
    def test_texts(self):
        ev = {"zone": "above", "strength": "strong", "text": "70 초과", "rsi": 71.26, "price": 412.3}
        self.assertEqual(tg.alert_text("TSLA", "NAS", ev), "🔴 TSLA 70 초과 ‼ · RSI 71.3 · $412.30")
        ev = {"zone": "below", "strength": "warn", "text": "35 미만", "rsi": 34.0, "price": 286500.0}
        self.assertEqual(tg.alert_text("삼성전자", "KRX", ev), "🔵 삼성전자 35 미만 · RSI 34.0 · 286,500원")
        ev = {"side": "buy", "text": "매수 시그널 (강, 추세 순응)", "rsi": 41.0, "price": 0.5123}
        self.assertEqual(tg.signal_text("POET", "NAS", ev), "📈 POET 매수 시그널 (강, 추세 순응) · RSI 41.0 · $0.5123")

    def test_error_hides_token(self):
        def boom(url, **kw):
            raise ConnectionError(f"failed {url}")
        with mock.patch.object(tg.requests, "post", boom):
            with self.assertRaises(RuntimeError) as cm:
                tg.call("123:SECRET", "sendMessage", chat_id=1, text="x")
        self.assertNotIn("SECRET", str(cm.exception))

    def test_not_ready_does_nothing(self):
        with mock.patch.object(tg, "load", lambda: (None, None)):
            t = tg.Telegram()
        t.send("x")
        self.assertIsNone(t.thread)
        with self.assertRaises(RuntimeError):
            t.send_now("x")

    def test_send_passes_silent(self):
        sent = []

        def post(url, json=None, timeout=None):
            sent.append(json)
            return types.SimpleNamespace(json=lambda: {"ok": True, "result": {}})
        with mock.patch.object(tg, "load", lambda: ("tok", "42")), mock.patch.object(tg.requests, "post", post):
            t = tg.Telegram()
            t.send("a", silent=True)
            for _ in range(100):
                if sent:
                    break
                time.sleep(0.01)
        self.assertEqual(sent[0], {"chat_id": "42", "text": "a", "disable_notification": True})


# ── 되감기 채점 ────────────────────────────────────────────────
class ReplayTest(unittest.TestCase):
    def test_exit_index_and_closed_market(self):
        bars = bars_from([100 + i for i in range(20)], gap_at=8)
        self.assertEqual(rp.exit_index(bars, 0, 15), 3)
        self.assertIsNone(rp.exit_index(bars, 0, 60))

    def test_ret_sign_and_no_negative_zero(self):
        bars = bars_from([100, 110, 100])
        self.assertAlmostEqual(rp.ret(bars, 0, 1, True), 10)
        self.assertAlmostEqual(rp.ret(bars, 0, 1, False), -10)
        self.assertEqual(f"{rp.ret(bars, 0, 2, False):+.2f}", "+0.00")

    def test_hit_rate_ignores_flat(self):
        rows = [{"symb": "A", "session": "정규", "kind": "알림 35 미만", "up": True, 60: v}
                for v in (1.0, -1.0, 0.0, 0.0, 2.0)]
        s = rp.summarize(rows, {"A": {("정규", 60): 0.0}}, 60, None)[0]
        self.assertAlmostEqual(s["hit"], 2 / 3)
        self.assertAlmostEqual(s["flat"], 2 / 5)

    def test_db_put_get_night(self):
        con = rp.db(temp_path(".db"))
        bars = bars_from([100, 101, 102])
        self.assertEqual(rp.put_bars(con, "NAS", "TSLA", 5, bars), 3)
        self.assertEqual(rp.put_bars(con, "NAS", "TSLA", 5, bars), 0)          # 같은 봉은 새로 안 셈
        changed = [{**bars[0], "close": 999}, {**bars[2], "time_us": "20260924 102000"}]
        self.assertEqual(rp.put_bars(con, "NAS", "TSLA", 5, changed, replace=False), 1)
        got = rp.get_bars(con, "NAS", "TSLA", 5)
        self.assertEqual([b["close"] for b in got], [100, 101, 102, 102])      # replace=False 면 옛 값을 둔다
        rp.put_bars(con, "NAS", "TSLA", 5, changed)
        self.assertEqual(rp.get_bars(con, "NAS", "TSLA", 5)[0]["close"], 999)
        self.assertEqual(rp.get_bars(con, "NAS", "MU", 5), [])
        self.assertFalse(rp.get_night(con, "NAS", "TSLA", 5))
        rp.set_night(con, "NAS", "TSLA", 5, True)
        self.assertTrue(rp.get_night(con, "NAS", "TSLA", 5))
        self.assertEqual(rp.find_excd(con, "TSLA", 5), "NAS")

    def test_db_two_writers(self):
        # 되감기와 수집이 같은 DB 를 열어 둔 채 번갈아 쓴다. 한쪽이 쓰고 곧 commit 하면 다른 쪽도 쓴다.
        # (쓰는 중에 commit 하지 않고 붙들고 있으면 다른 쪽은 30초 기다리다 실패한다 — 그래서 load_bars 는
        # 네트워크를 다 받은 뒤에 쓴다)
        path = temp_path(".db")
        a, b = rp.db(path), rp.db(path)
        rp.get_bars(b, "NAS", "A", 5)                                          # b 가 읽는 중에도
        rp.put_bars(a, "NAS", "A", 5, bars_from([1, 2]))
        a.commit()
        rp.put_bars(b, "NAS", "B", 5, bars_from([3]))
        b.commit()
        self.assertEqual(len(rp.get_bars(rp.db(path), "NAS", "A", 5)), 2)
        self.assertEqual(len(rp.get_bars(rp.db(path), "NAS", "B", 5)), 1)

    def test_migrate_json(self):
        cache = Path(tempfile.mkdtemp())
        bars = bars_from([100, 101])
        (cache / "NAS_TSLA_5.json").write_text(json.dumps(
            {"bars": {b["time_us"]: b for b in bars}, "night": True}), encoding="utf-8")
        with mock.patch.object(rp, "CACHE", cache), mock.patch.object(rp, "DB", cache / "bars.db"):
            con = rp.db()
            self.assertEqual(len(rp.get_bars(con, "NAS", "TSLA", 5)), 2)
            self.assertTrue(rp.get_night(con, "NAS", "TSLA", 5))
        self.assertFalse((cache / "NAS_TSLA_5.json").exists())
        self.assertTrue((cache / "json_backup" / "NAS_TSLA_5.json").exists())

    def test_collect_adds_night_bars(self):
        cache = Path(tempfile.mkdtemp())
        night = bars_from([10, 11], t0=datetime(2026, 9, 24, 21, 0))
        with mock.patch.object(rp, "CACHE", cache), mock.patch.object(rp, "DB", cache / "bars.db"), \
                mock.patch.object(k, "us_day_session", lambda t=None: True), \
                mock.patch.object(k, "load_keys", lambda: ("a", "s")), \
                mock.patch.object(k, "resolve", lambda a, s, t: tuple(t.split(":"))), \
                mock.patch.object(rp, "night_on", lambda *a: (True, night)), \
                mock.patch.object(rp, "topup_minutes", lambda *a, **kw: 0), \
                mock.patch.object(rp.time, "sleep", lambda s: None), mock.patch("builtins.print"):
            rp.collect(["NAS:TSLA", "KRX:005930"], 5)
            con = rp.db()
            self.assertEqual(len(rp.get_bars(con, "NAS", "TSLA", 5)), 2)
            self.assertEqual(rp.get_bars(con, "KRX", "005930", 5), [])        # 국내는 주간거래가 없다
        self.assertIn("TSLA +2", (cache / "collect.log").read_text(encoding="utf-8"))

    def test_kr_session(self):
        self.assertEqual(rp.session(datetime(2026, 9, 23, 10, 0), kr=True), "정규")
        self.assertEqual(rp.session(datetime(2026, 9, 23, 21, 0)), "주간")
        self.assertEqual(rp.session(datetime(2026, 11, 27, 13, 30)), "애프터")    # 조기 폐장
        self.assertEqual(rp.session(datetime(2026, 11, 20, 13, 30)), "정규")


# ── 1분봉 쌓기·봉 길이 채점 ────────────────────────────────────
def minutes_from(closes, t0=T0, wiggle=0.0):
    """1분봉. wiggle 만큼 고가·저가를 벌린다."""
    return [{"time_us": (t0 + timedelta(minutes=i)).strftime("%Y%m%d %H%M%S"), "open": c,
             "high": c + wiggle, "low": c - wiggle, "close": c, "volume": 1} for i, c in enumerate(closes)]


class MinuteTest(unittest.TestCase):
    def test_ts_matches_strptime(self):
        for t in ("20260924 093000", "20261231 235959", "20260101 000000"):
            self.assertEqual(rp.ts(t), datetime.strptime(t, "%Y%m%d %H%M%S"))

    def test_aggregate(self):
        m = minutes_from([10, 12, 9, 11, 13, 14, 15], t0=datetime(2026, 9, 24, 9, 58), wiggle=0.5)
        g = tf.aggregate(m, 5)
        self.assertEqual([b["time_us"][9:] for b, _ in g], ["095500", "100000"])
        self.assertEqual([idx for _, idx in g], [[0, 1], [2, 3, 4, 5, 6]])
        b = g[1][0]
        self.assertEqual((b["open"], b["high"], b["low"], b["close"], b["volume"]), (9, 15.5, 8.5, 15, 5))

    def test_one_minute_walk_is_the_replay(self):
        # 1분봉을 1분봉으로 걸으면 kis_replay 되감기(봉마다 저·고·종 RSI 를 Gate 에)와 같은 알림이어야 한다
        closes = [100 + 6 * math.sin(i / 7) + (i % 5) * 0.3 for i in range(400)]
        m = minutes_from(closes, wiggle=0.4)
        want = [(e["i"], e["kind"]) for e in rp.replay_alerts(m, 14)]
        got = [(e["i"], e["kind"]) for e in tf.walk_alerts(m, tf.aggregate(m, 1), 14, None) if e["i"] >= rp.WARMUP]
        self.assertTrue(want)
        self.assertEqual(got, want)

    def test_long_bar_alerts_inside_the_bar(self):
        # 60분봉 RSI 알림도 봉이 닫히기 전, 선을 넘는 분에 울린다
        closes = [100 + (i // 60 % 2) * 0.5 for i in range(60 * 16)] + [100 - i * 0.2 for i in range(60)]
        m = minutes_from(closes)
        ev = [e for e in tf.walk_alerts(m, tf.aggregate(m, 60), 14, None) if e["i"] >= 60 * 16]
        self.assertTrue(ev)
        self.assertLess(ev[0]["i"] - 60 * 16, 59)                            # 봉 끝(59분)보다 앞

    def fake_pages(self, bars):
        """fetch_bars 흉내: keyb 보다 앞(그 봉 포함) 최신 3개씩."""
        calls = []

        def fetch(appkey, secret, excd, symb, nmin=5, keyb=""):
            calls.append((excd, keyb))
            upto = [b for b in bars if not keyb or b["time_us"].replace(" ", "") <= keyb]
            return upto[-3:]
        return fetch, calls

    def test_topup_stops_at_last_stored(self):
        con = rp.db(temp_path(".db"))
        m = minutes_from(range(10), t0=datetime(2026, 9, 24, 10, 0))
        rp.put_bars(con, "NAS", "TSLA", 1, m[:6])
        fetch, calls = self.fake_pages(m)
        with mock.patch.object(k, "fetch_bars", fetch), mock.patch.object(rp.time, "sleep", lambda s: None):
            self.assertEqual(rp.topup_minutes(con, "a", "s", "NAS", "TSLA"), 4)
        self.assertEqual(len(calls), 2)                                       # 마지막 봉(10:05)이 든 쪽까지만
        self.assertEqual(len(rp.get_bars(con, "NAS", "TSLA", 1)), 10)
        self.assertEqual(rp.last_minute(con, "NAS", "TSLA", False), "20260924 100900")
        self.assertIsNone(rp.last_minute(con, "NAS", "TSLA", True))

    def test_topup_night_uses_day_exchange(self):
        con = rp.db(temp_path(".db"))
        m = minutes_from(range(4), t0=datetime(2026, 9, 24, 21, 0))
        fetch, calls = self.fake_pages(m)
        with mock.patch.object(k, "fetch_bars", fetch), mock.patch.object(rp.time, "sleep", lambda s: None):
            self.assertEqual(rp.topup_minutes(con, "a", "s", "NAS", "TSLA", night=True), 4)
        self.assertEqual(calls[0][0], k.DAY_EXCD["NAS"])
        self.assertEqual(rp.last_minute(con, "NAS", "TSLA", True), "20260924 210300")

    def test_topup_kr_asks_only_recent_days(self):
        con = rp.db(temp_path(".db"))
        two_days_ago = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d 100000")
        rp.put_bars(con, "KRX", "005930", 1, [{"time_us": two_days_ago, "open": 1, "high": 1, "low": 1,
                                              "close": 1, "volume": 1}])
        seen = {}

        def fetch_kr(appkey, secret, code, nmin=5, need=120):
            seen["need"] = need
            return []
        with mock.patch.object(k, "fetch_kr_bars", fetch_kr):
            rp.topup_minutes(con, "a", "s", "KRX", "005930")
        self.assertEqual(seen["need"], 3 * 391)                               # 이틀 전 + 오늘

    def test_collect_tickers_adds_extra_once(self):
        d = Path(tempfile.mkdtemp())
        (d / "w.json").write_text('["NAS:TSLA", "KRX:005930"]', encoding="utf-8")
        (d / "c.json").write_text('["nas:msft", "NAS:TSLA"]', encoding="utf-8")
        with mock.patch.object(rp, "WATCHLIST", d / "w.json"), mock.patch.object(rp, "COLLECT_EXTRA", d / "c.json"):
            self.assertEqual(rp.collect_tickers(), ["NAS:TSLA", "KRX:005930", "NAS:MSFT"])
        with mock.patch.object(rp, "WATCHLIST", d / "w.json"), mock.patch.object(rp, "COLLECT_EXTRA", d / "none.json"):
            self.assertEqual(rp.collect_tickers(), ["NAS:TSLA", "KRX:005930"])


if __name__ == "__main__":
    unittest.main()
