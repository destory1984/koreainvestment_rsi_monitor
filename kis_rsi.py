"""한국투자증권 Open API 로 미국주식 분봉과 실시간 체결가를 받아온다.

    python kis_rsi.py bars TSLA              # 5분봉 (오늘+전일, 최근 120개)
    python kis_rsi.py bars NYS:BE --min 1    # 거래소를 직접 적을 수도 있다
    python kis_rsi.py live TSLA SOXL         # 실시간 체결가 (Ctrl+C 로 끝)
    python kis_rsi.py rsi TSLA SOXL          # 5분봉 RSI(14)·MACD 를 실시간으로 (줄 단위)
    python kis_rsi.py watch TSLA SOXL        # 같은 것을 표 하나로

키는 kis_config.json (저장소에 안 올라감) 이나 환경변수 KIS_APPKEY / KIS_APPSECRET 에 둔다.
    {"appkey": "...", "appsecret": "..."}
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Git Bash 는 파이프로 붙어서 cp949 로 찍히면 한글이 깨진다.
# 작업 스케줄러가 pythonw 로 띄우면 stdout 이 아예 없다 (None)
for _s in (sys.stdout, sys.stderr):
    if _s is not None:
        _s.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
CONFIG = HERE / "kis_config.json"
TOKEN_CACHE = HERE / "kis_token.json"
EXCHANGE_CACHE = HERE / "kis_exchange.json"

REST = "https://openapi.koreainvestment.com:9443"
WS = "ws://ops.koreainvestment.com:21000/tryitout"
EXCHANGES = ("NAS", "NYS", "AMS")
DAY_EXCD = {"NAS": "BAQ", "NYS": "BAY", "AMS": "BAA"}   # 미국 주간거래(한국 낮)의 거래소 코드
NEW_YORK = ZoneInfo("America/New_York")

# HDFSCNT0 (해외주식 실시간체결가) 필드 순서
US_TR, KR_TR = "HDFSCNT0", "H0STCNT0"
LIVE_FIELDS = ["RSYM", "SYMB", "ZDIV", "TYMD", "XYMD", "XHMS", "KYMD", "KHMS", "OPEN", "HIGH",
               "LOW", "LAST", "SIGN", "DIFF", "RATE", "PBID", "PASK", "VBID", "VASK",
               "EVOL", "TVOL", "TAMT", "BIVL", "ASVL", "STRN", "MTYP"]
# H0STCNT0 (국내주식 실시간체결가, KRX) 필드 순서
KR_FIELDS = ["MKSC_SHRN_ISCD", "STCK_CNTG_HOUR", "STCK_PRPR", "PRDY_VRSS_SIGN", "PRDY_VRSS",
             "PRDY_CTRT", "WGHN_AVRG_STCK_PRC", "STCK_OPRC", "STCK_HGPR", "STCK_LWPR", "ASKP1",
             "BIDP1", "CNTG_VOL", "ACML_VOL", "ACML_TR_PBMN", "SELN_CNTG_CSNU", "SHNU_CNTG_CSNU",
             "NTBY_CNTG_CSNU", "CTTR", "SELN_CNTG_SMTN", "SHNU_CNTG_SMTN", "CCLD_DVSN", "SHNU_RATE",
             "PRDY_VOL_VRSS_ACML_VOL_RATE", "OPRC_HOUR", "OPRC_VRSS_PRPR_SIGN", "OPRC_VRSS_PRPR",
             "HGPR_HOUR", "HGPR_VRSS_PRPR_SIGN", "HGPR_VRSS_PRPR", "LWPR_HOUR", "LWPR_VRSS_PRPR_SIGN",
             "LWPR_VRSS_PRPR", "BSOP_DATE", "NEW_MKOP_CLS_CODE", "TRHT_YN", "ASKP_RSQN1", "BIDP_RSQN1",
             "TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN", "VOL_TNRT", "PRDY_SMNS_HOUR_ACML_VOL",
             "PRDY_SMNS_HOUR_ACML_VOL_RATE", "HOUR_CLS_CODE", "MRKT_TRTM_CLS_CODE", "VI_STND_PRC"]
KR_INFO = {}  # 종목코드 -> {"name", "price", "rate"} (국내 분봉을 받을 때 채운다)


def _bashrc_keys():
    """~/.bashrc 의 export KIS_APPKEY=... 줄. 작업 스케줄러처럼 셸을 거치지 않고 뜰 때 쓴다."""
    try:
        text = (Path.home() / ".bashrc").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    found = re.findall(r"^\s*export\s+(KIS_APP(?:KEY|SECRET))=(['\"]?)(.+?)\2\s*$", text, re.M)
    return {name: value for name, _, value in found}


def load_keys():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    rc = {} if os.environ.get("KIS_APPKEY") or cfg.get("appkey") else _bashrc_keys()
    appkey = os.environ.get("KIS_APPKEY") or cfg.get("appkey") or rc.get("KIS_APPKEY")
    secret = os.environ.get("KIS_APPSECRET") or cfg.get("appsecret") or rc.get("KIS_APPSECRET")
    if not appkey or not secret:
        sys.exit("한국투자증권 앱키가 없다. 먼저  python kis_rsi.py setup  으로 넣을 것.\n"
                 "(환경변수 KIS_APPKEY / KIS_APPSECRET 로 줘도 된다)")
    return appkey, secret


def cmd_setup(args):
    """앱키·시크릿을 물어 kis_config.json 에 적고, 토큰을 받아 맞는 키인지 본다."""
    from getpass import getpass
    print("KIS Developers(https://apiportal.koreainvestment.com) 에서 받은 실전투자 앱키를 넣는다.")
    print(f"키는 이 폴더의 {CONFIG.name} 에만 적힌다 (저장소에는 올라가지 않는다).\n")
    appkey = input("앱키(App Key): ").strip()
    secret = getpass("시크릿(App Secret, 화면에 안 보임): ").strip()
    if not appkey or not secret:
        sys.exit("비어 있다. 다시 할 것.")
    try:
        TOKEN_CACHE.unlink(missing_ok=True)
        get_token(appkey, secret)
    except Exception as e:
        sys.exit(f"토큰을 받지 못했다 — 키가 맞는지, 실전투자 키인지 볼 것.\n{e}")
    CONFIG.write_text(json.dumps({"appkey": appkey, "appsecret": secret}), encoding="utf-8")
    print(f"\n됐다. {CONFIG.name} 에 적었다. 이제  python kis_web.py TSLA  로 띄운다.")


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


def fetch_bars(appkey, secret, excd, symb, nmin=5, pinc=True, nrec=120, keyb=""):
    """해외주식분봉조회 (HHDFS76950200). 최신 것부터 온다. 국내 종목은 fetch_kr_bars 로 넘긴다.
    keyb 에 앞서 받은 가장 옛 봉의 'YYYYMMDDHHMMSS' 를 주면 그보다 앞 120개를 받는다 (그 봉부터 겹쳐 온다)."""
    if excd == "KRX":
        return fetch_kr_bars(appkey, secret, symb, nmin)
    token = get_token(appkey, secret)
    r = requests.get(f"{REST}/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
                     headers={"authorization": f"Bearer {token}", "appkey": appkey,
                              "appsecret": secret, "tr_id": "HHDFS76950200", "custtype": "P"},
                     params={"AUTH": "", "EXCD": excd, "SYMB": symb, "NMIN": str(nmin),
                             "PINC": "1" if pinc else "0", "NEXT": "1" if keyb else "", "NREC": str(nrec),
                             "FILL": "", "KEYB": keyb}, timeout=10)
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


NIGHT_SHARE = 0.5   # 최근 오버나이트 5분 칸의 이만큼 넘게 체결이 있으면 Webull 24시간 거래 종목으로 본다


def night_slots(nmin=5, now=None):
    """지금부터 거슬러 올라간 최근 오버나이트(미국 주간거래) 봉 시작 시각들. 한 번 분량(8시간)."""
    count = 8 * 60 // nmin
    t = (now or datetime.now(NEW_YORK)).replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % nmin)
    out = []
    for _ in range(count * 40):   # 주말을 건너도 넉넉하게
        if len(out) >= count:
            break
        if us_day_session(t):
            out.append(t.strftime("%Y%m%d %H%M%S"))
        t -= timedelta(minutes=nmin)
    return out


def fetch_night(appkey, secret, excd, symb, nmin=5):
    """미국 주간거래 분봉과, 최근 오버나이트 칸 가운데 체결이 있었던 칸 수.
    (봉들, 체결 있던 칸, 전체 칸). 못 받으면 봉 없이 돌려준다."""
    slots = night_slots(nmin)
    try:
        day = fetch_bars(appkey, secret, DAY_EXCD[excd], symb, nmin)
    except Exception:
        day = []
    have = {b["time_us"] for b in day}
    return day, sum(s in have for s in slots), len(slots)


def merge_bars(bars, day):
    """정규장·프리·애프터 분봉에 주간거래 분봉을 시각 순서로 끼워 넣는다. 겹치면 정규 쪽."""
    merged = {b["time_us"]: b for b in day}
    merged.update({b["time_us"]: b for b in bars})
    return [merged[t] for t in sorted(merged)]


def fetch_kr_bars(appkey, secret, code, nmin=5, need=120):
    """국내주식 1분봉(FHKST03010230, 한 번에 120개)을 거슬러 받아 nmin 분봉으로 묶는다."""
    token = get_token(appkey, secret)
    now = datetime.now()
    date, hour = now.strftime("%Y%m%d"), "200000"
    mins = []
    for _ in range(need * nmin // 120 + 3):
        r = requests.get(f"{REST}/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
                         headers={"authorization": f"Bearer {token}", "appkey": appkey,
                                  "appsecret": secret, "tr_id": "FHKST03010230", "custtype": "P"},
                         params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                                 "FID_INPUT_HOUR_1": hour, "FID_INPUT_DATE_1": date,
                                 "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""},
                         timeout=10)
        r.raise_for_status()
        j = r.json()
        if j.get("rt_cd") != "0":
            raise RuntimeError(f"KRX:{code} {j.get('msg_cd')} {j.get('msg1')}")
        o1 = j.get("output1") or {}
        if o1.get("hts_kor_isnm") and code not in KR_INFO:
            KR_INFO[code] = {"name": o1["hts_kor_isnm"], "price": float(o1["stck_prpr"]),
                             "rate": float(o1["prdy_ctrt"])}
        page = [b for b in j.get("output2") or [] if b.get("stck_prpr")]
        if not page:
            break
        mins += page
        last = page[-1]  # 가장 이른 것
        t = datetime.strptime(last["stck_bsop_date"] + last["stck_cntg_hour"], "%Y%m%d%H%M%S")
        t = t.replace(second=0) - timedelta(minutes=1)
        date, hour = t.strftime("%Y%m%d"), t.strftime("%H%M%S")
        if len({bar_start(b["stck_bsop_date"], b["stck_cntg_hour"], nmin) for b in mins}) > need:
            break
        time.sleep(0.1)
    bars = {}
    for b in reversed(mins):  # 이른 것부터
        k = bar_start(b["stck_bsop_date"], b["stck_cntg_hour"], nmin)
        o, h, l, c = (float(b[x]) for x in ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_prpr"))
        v = int(b["cntg_vol"] or 0)
        if k not in bars:
            bars[k] = {"time_us": k, "open": o, "high": h, "low": l, "close": c, "volume": v}
        else:
            x = bars[k]
            x["high"], x["low"], x["close"] = max(x["high"], h), min(x["low"], l), c
            x["volume"] += v
    return [bars[k] for k in sorted(bars)][-need:]


# 한 줄 띠에 보일 지수·환율. (이름, 종류, 코드)
#   N 해외지수 · X 환율 — 해외 일별시세(FHKST03030100) 의 요약값을 쓴다
#   U 국내지수 — 국내업종 현재지수(FHPUP02100000)
# 다우존스는 한국투자증권 코드를 찾지 못해 뺐다 (.DJI 등은 빈 값이 온다).
# 띠에 이 차례로 놓인다. 비트코인은 한국투자증권에 없어 업비트에서 받는다 (kind "BTC").
MARKETS = [("S&P500", "N", "SPX"), ("나스닥", "N", "COMP"), ("코스피", "U", "0001"),
           ("원/달러", "X", "FX@KRW"), ("비트코인", "BTC", "KRW-BTC"), ("니케이", "N", "JP#NI225")]


def fetch_quote(appkey, secret, excd, symb):
    """해외주식 현재가 (HHDFS00000300). (현재가, 전일 정규장 종가 대비 등락률 %).
    excd 에 주간거래 거래소(BAQ 등)를 주면 주간거래 가격이 온다."""
    token = get_token(appkey, secret)
    r = requests.get(f"{REST}/uapi/overseas-price/v1/quotations/price",
                     headers={"authorization": f"Bearer {token}", "appkey": appkey,
                              "appsecret": secret, "tr_id": "HHDFS00000300", "custtype": "P"},
                     params={"AUTH": "", "EXCD": excd, "SYMB": symb}, timeout=10)
    r.raise_for_status()
    o = r.json().get("output") or {}
    if not o.get("last"):
        raise RuntimeError(f"{excd}:{symb} 현재가 없음")
    return float(o["last"]), float(o["rate"])


def fetch_market(appkey, secret, kind, code):
    """지수·환율 하나의 (현재값, 등락률%)."""
    token = get_token(appkey, secret)
    head = {"authorization": f"Bearer {token}", "appkey": appkey, "appsecret": secret, "custtype": "P"}
    if kind == "U":
        r = requests.get(f"{REST}/uapi/domestic-stock/v1/quotations/inquire-index-price",
                         headers={**head, "tr_id": "FHPUP02100000"},
                         params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code}, timeout=10)
        o = r.json().get("output") or {}
        return float(o["bstp_nmix_prpr"]), float(o["bstp_nmix_prdy_ctrt"])
    today = datetime.now()
    r = requests.get(f"{REST}/uapi/overseas-price/v1/quotations/inquire-daily-chartprice",
                     headers={**head, "tr_id": "FHKST03030100"},
                     params={"FID_COND_MRKT_DIV_CODE": kind, "FID_INPUT_ISCD": code,
                             "FID_INPUT_DATE_1": (today - timedelta(days=10)).strftime("%Y%m%d"),
                             "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),
                             "FID_PERIOD_DIV_CODE": "D"}, timeout=10)
    o = r.json().get("output1") or {}
    return float(o["ovrs_nmix_prpr"]), float(o["prdy_ctrt"])


def fetch_bitcoin():
    """업비트 공개 시세(키 없음). (원화 가격, 전일 대비 %)."""
    r = requests.get("https://api.upbit.com/v1/ticker", params={"markets": "KRW-BTC"}, timeout=10)
    o = r.json()[0]
    return float(o["trade_price"]), float(o["signed_change_rate"]) * 100


def resolve(appkey, secret, ticker):
    """'NYS:BE' 는 그대로, 'BE' 는 나스닥→뉴욕→아멕스 순으로 찾아서 기억해 둔다.
    숫자 여섯 자리('005930')나 'KRX:005930' 은 국내 종목이다."""
    t = ticker.strip().upper()
    if re.fullmatch(r"(KRX:)?\d{6}", t):
        return "KRX", t[-6:]
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
    raise LookupError(f"{symb}: 어느 거래소에서도 못 찾았다. NAS:{symb} 처럼 직접 적을 것.")


def cmd_bars(args):
    appkey, secret = load_keys()
    for t in args.tickers:
        excd, symb = resolve(appkey, secret, t)
        bars = fetch_bars(appkey, secret, excd, symb, args.min, pinc=not args.today)
        print(f"\n{excd}:{symb} {args.min}분봉 {len(bars)}개 (미국시각)")
        for b in bars[-args.n:]:
            print(f"  {b['time_us']}  O {px(b['open'], 9)}  H {px(b['high'], 9)}  "
                  f"L {px(b['low'], 9)}  C {px(b['close'], 9)}  V {b['volume']:>9}")
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
    print(f"{datetime.now():%H:%M:%S}  {d['SYMB']:<6} {px(float(d['LAST']), 10)}  "
          f"{float(d['RATE']):>+6.2f}%  체결 {d['EVOL']:>6}  누적 {d.get('TVOL', '')}  "
          f"(현지 {d['XHMS']})", flush=True)


def px(p, width=0):
    """미국 주식 가격: 1달러 이상이면 XX.YY, 밑이면 소수 넷째 자리까지."""
    return f"{p:>{width}.{2 if p >= 1 else 4}f}"


def normalize(tr_id, rec):
    """국내 체결을 해외 체결과 같은 이름으로 맞춘다. Book 은 SYMB·LAST·RATE·EVOL·XYMD·XHMS 만 본다."""
    if tr_id == US_TR:
        return rec
    return {"SYMB": rec["MKSC_SHRN_ISCD"], "LAST": rec["STCK_PRPR"], "RATE": rec["PRDY_CTRT"],
            "EVOL": rec["CNTG_VOL"], "TVOL": rec["ACML_VOL"], "XYMD": rec["BSOP_DATE"],
            "XHMS": rec["STCK_CNTG_HOUR"]}


def us_day_session(now=None):
    """미국 주간거래 시간인가. 미국 동부 20:00~04:00, 일요일 밤부터 금요일 새벽까지 (한국 낮).
    이때는 정규장 쪽(D) 실시간에 체결이 오지 않아 주간거래 쪽(R)으로 받아야 한다. 휴장일은 따지지 않는다."""
    t = now or datetime.now(NEW_YORK)
    if t.hour >= 20:
        return t.weekday() in (6, 0, 1, 2, 3)
    if t.hour < 4:
        return t.weekday() in (0, 1, 2, 3, 4)
    return False


def us_session(now=None):
    """미국 동부 시각으로 지금 세션: "day"(주간거래) / "pre" / "regular" / "after" / None(휴장). 휴장일은 모른다."""
    t = now or datetime.now(NEW_YORK)
    if us_day_session(t):
        return "day"
    if t.weekday() >= 5:
        return None
    h = t.hour + t.minute / 60
    return "pre" if 4 <= h < 9.5 else "regular" if 9.5 <= h < 16 else "after" if 16 <= h < 20 else None


class KisInUse(Exception):
    """앱키 하나에 실시간 연결은 하나뿐인데 이미 다른 곳에서 열려 있다."""

    def __str__(self):
        return ("이 앱키로 실시간 연결이 이미 열려 있다. 앱키 하나에 연결은 하나뿐이니 "
                "다른 창의 kis_rsi.py / kis_web.py 를 끄고 다시 할 것.")


async def subscribe(ws, approval_key, key, on=True):
    """실시간 체결가 구독(on) 또는 해지. 연결 하나에 41개까지 넣을 수 있다.
    key 는 해외면 'DNASTSLA' 같은 문자열, 국내면 ('H0STCNT0', '005930')."""
    tr_id, tr_key = key if isinstance(key, tuple) else (US_TR, key)
    await ws.send(json.dumps({
        "header": {"approval_key": approval_key, "custtype": "P",
                   "tr_type": "1" if on else "2", "content-type": "utf-8"},
        "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}}}))


async def live(approval_key, keys, on_tick=print_tick, on_open=None):
    """on_open(ws) 을 주면 연결 직후 불러 준다. 연결 중에 구독을 넣고 빼려면 그 ws 를 쓴다."""
    import websockets
    async with websockets.connect(WS, ping_interval=None) as ws:
        for k in keys:
            await subscribe(ws, approval_key, k)
        if on_open:
            on_open(ws)
        async for msg in ws:
            if msg[0] in "01":  # 데이터: 암호화|TR|건수|필드^필드^...
                _, tr_id, count, data = msg.split("|", 3)
                fields = KR_FIELDS if tr_id == KR_TR else LIVE_FIELDS
                vals, count, n = data.split("^"), int(count), len(fields)
                skip = 1 if len(vals) == count * (n + 1) else 0  # 앞에 구독 키가 한 칸 더 붙어 오면
                for i in range(count):
                    rec = vals[i * (n + skip) + skip:(i + 1) * (n + skip)]
                    on_tick(normalize(tr_id, dict(zip(fields, rec))))
                continue
            j = json.loads(msg)
            if j["header"]["tr_id"] == "PINGPONG":
                await ws.send(msg)
            else:
                body = j.get("body", {})
                if "ALREADY IN USE" in (body.get("msg1") or ""):
                    raise KisInUse()
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
    except KisInUse as e:
        sys.exit(str(e))


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
        info = KR_INFO.get(symb, {}) if excd == "KRX" else {}
        self.name = info.get("name", symb)
        self.price = info.get("price") or (bars[-1]["close"] if bars else None)
        self.rate = info.get("rate")
        self.us_time = ""
        self.day_quote = False   # 지금 가격이 미국 주간거래 체결가인가
        self.night = None        # 오버나이트 봉을 넣을지 {"on", "auto", "have", "of"} (웹에서 정한다)

    @property
    def key(self):
        return self.key_for()

    def key_for(self, day=False):
        """실시간 구독 키. day 면 미국 주간거래 쪽 ('RBAQTSLA' 같은). 국내는 늘 같다."""
        if self.excd == "KRX":
            return (KR_TR, self.symb)
        return f"R{DAY_EXCD[self.excd]}{self.symb}" if day else f"D{self.excd}{self.symb}"

    def on_tick(self, d):
        """체결 하나를 반영한다. 새 봉이 생기면 True."""
        price = float(d["LAST"])
        if d.get("RSYM", "").startswith("R") and self.night and not self.night["on"]:
            # Webull 이 오버나이트 거래를 안 하는 종목: 가격만 고치고 봉에는 넣지 않는다
            self.price, self.rate, self.us_time, self.day_quote = price, float(d["RATE"]), d["XHMS"], True
            return False
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
        self.day_quote = d.get("RSYM", "").startswith("R")   # 주간거래 체결 ('RBAQTSLA')
        return new

    def series(self, period=14):
        """차트용: 봉마다 RSI·MACD·시그널·히스토그램."""
        closes = [b["close"] for b in self.bars]
        line, sig, hist = macd_series(closes)
        return {"rsi": rsi_series(closes, period), "macd": line, "signal": sig, "hist": hist}

    def indicators(self, period=14):
        closes = [b["close"] for b in self.bars[-400:]]
        line, sig, hist = macd_series(closes)
        rsi = rsi_series(closes, period)
        # rsi_prev 는 앞 봉이 닫힐 때의 RSI. 지금 RSI 와 견줘 오르는지 내리는지 본다
        return {"rsi": rsi[-1], "rsi_prev": rsi[-2] if len(rsi) > 1 else None,
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
    except KisInUse as e:
        sys.exit(str(e))


def fmt_ind(ind):
    rsi = "  -   " if ind["rsi"] is None else f"{ind['rsi']:6.2f}"
    return (f"RSI {rsi}  MACD {ind['macd']:+.4f}  시그널 {ind['signal']:+.4f}  "
            f"히스토 {ind['hist']:+.4f}")


def cmd_rsi(args):
    """종목마다 한 줄씩, RSI 가 --step 이상 바뀌거나 MACD 가 시그널을 건널 때 찍는다."""
    appkey, secret, books = load_books(args.tickers, args.min)
    for book in books.values():
        print(f"\n{book.excd}:{book.symb} {args.min}분봉  봉 {len(book.bars)}개 (현지 시각)")
        closes = [b["close"] for b in book.bars]
        r = rsi_series(closes, args.period)
        line, sig, hist = macd_series(closes)
        for i in range(max(0, len(closes) - args.n), len(closes)):
            ind = {"rsi": r[i], "macd": line[i], "signal": sig[i], "hist": hist[i]}
            print(f"  {book.bars[i]['time_us']}  C {px(closes[i], 9)}  {fmt_ind(ind)}")
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
        print(f"{datetime.now():%H:%M:%S}  {book.symb:<6} {px(book.price, 10)}  {fmt_ind(ind)}  "
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
            p = ind["rsi_prev"]
            if r is not None and p is not None:  # 앞 봉이 닫힐 때보다 오르면 ▲, 내리면 ▼
                rsi += " [red]▲[/]" if r - p > 0.05 else " [bright_blue]▼[/]" if r - p < -0.05 else " [dim]—[/]"
            hc = "green" if ind["hist"] > 0 else "red"
            rate = "" if b.rate is None else f"[{'green' if b.rate >= 0 else 'red'}]{b.rate:+.2f}%[/]"
            us = f"{b.us_time[:2]}:{b.us_time[2:4]}:{b.us_time[4:]}" if b.us_time else ""
            t.add_row(b.symb, px(b.price), rate, rsi, f"{ind['macd']:+.4f}",
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
    s = sub.add_parser("setup", help="앱키·시크릿 넣기 (처음 한 번)")
    s.set_defaults(func=cmd_setup)
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
    try:
        args.func(args)
    except LookupError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
