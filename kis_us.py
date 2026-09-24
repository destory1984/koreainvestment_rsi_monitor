"""한국투자증권 Open API 로 미국주식 분봉과 실시간 체결가를 받아온다.

    python kis_us.py bars TSLA              # 5분봉 (오늘+전일, 최근 120개)
    python kis_us.py bars NYS:BE --min 1    # 거래소를 직접 적을 수도 있다
    python kis_us.py live TSLA SOXL         # 실시간 체결가 (Ctrl+C 로 끝)

키는 kis_config.json (저장소에 안 올라감) 이나 환경변수 KIS_APPKEY / KIS_APPSECRET 에 둔다.
    {"appkey": "...", "appsecret": "..."}
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Git Bash 는 파이프로 붙어서 cp949 로 찍히면 한글이 깨진다
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
CONFIG = HERE / "kis_config.json"
TOKEN_CACHE = HERE / "kis_token.json"
EXCHANGE_CACHE = HERE / "kis_exchange.json"

REST = "https://openapi.koreainvestment.com:9443"
WS = "ws://ops.koreainvestment.com:21000/tryitout"
EXCHANGES = ("NAS", "NYS", "AMS")

# HDFSCNT0 (해외주식 실시간체결가) 필드 순서
LIVE_FIELDS = ["RSYM", "SYMB","ZDIV", "TYMD", "XYMD", "XHMS", "KYMD", "KHMS", "OPEN", "HIGH",
               "LOW", "LAST", "SIGN", "DIFF", "RATE", "PBID", "PASK", "VBID", "VASK",
               "EVOL", "TVOL", "TAMT", "BIVL", "ASVL", "STRN", "MTYP"]


def load_keys():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    appkey = os.environ.get("KIS_APPKEY") or cfg.get("appkey")
    secret = os.environ.get("KIS_APPSECRET") or cfg.get("appsecret")
    if not appkey or not secret:
        sys.exit(f"앱키가 없다. {CONFIG.name} 에 appkey/appsecret 을 적거나 환경변수를 설정할 것.")
    return appkey, secret


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


def fetch_bars(appkey, secret, excd, symb, nmin=5, pinc=True, nrec=120):
    """해외주식분봉조회 (HHDFS76950200). 최신 것부터 온다."""
    token = get_token(appkey, secret)
    r = requests.get(f"{REST}/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
                     headers={"authorization": f"Bearer {token}", "appkey": appkey,
                              "appsecret": secret, "tr_id": "HHDFS76950200", "custtype": "P"},
                     params={"AUTH": "", "EXCD": excd, "SYMB": symb, "NMIN": str(nmin),
                             "PINC": "1" if pinc else "0", "NEXT": "", "NREC": str(nrec),
                             "FILL": "", "KEYB": ""}, timeout=10)
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


def resolve(appkey, secret, ticker):
    """'NYS:BE' 는 그대로, 'BE' 는 나스닥→뉴욕→아멕스 순으로 찾아서 기억해 둔다."""
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
    sys.exit(f"{symb}: 어느 거래소에서도 못 찾았다. NAS:{symb} 처럼 직접 적을 것.")


def cmd_bars(args):
    appkey, secret = load_keys()
    for t in args.tickers:
        excd, symb = resolve(appkey, secret, t)
        bars = fetch_bars(appkey, secret, excd, symb, args.min, pinc=not args.today)
        print(f"\n{excd}:{symb} {args.min}분봉 {len(bars)}개 (미국시각)")
        for b in bars[-args.n:]:
            print(f"  {b['time_us']}  O {b['open']:>9.4f}  H {b['high']:>9.4f}  "
                  f"L {b['low']:>9.4f}  C {b['close']:>9.4f}  V {b['volume']:>9}")
        time.sleep(0.1)


async def live(approval_key, keys):
    import websockets
    async with websockets.connect(WS, ping_interval=None) as ws:
        for k in keys:
            await ws.send(json.dumps({
                "header": {"approval_key": approval_key, "custtype": "P",
                           "tr_type": "1", "content-type": "utf-8"},
                "body": {"input": {"tr_id": "HDFSCNT0", "tr_key": k}}}))
        async for msg in ws:
            if msg[0] in "01":  # 데이터: 암호화|TR|건수|필드^필드^...
                _, tr_id, count, data = msg.split("|", 3)
                vals = data.split("^")
                n = len(LIVE_FIELDS)
                for i in range(int(count)):
                    d = dict(zip(LIVE_FIELDS, vals[i * n:(i + 1) * n]))
                    print(f"{datetime.now():%H:%M:%S}  {d['SYMB']:<6} {float(d['LAST']):>10.4f}  "
                          f"{float(d['RATE']):>+6.2f}%  체결 {d['EVOL']:>6}  누적 {d['TVOL']}  "
                          f"(미국 {d['XHMS']})", flush=True)
                continue
            j = json.loads(msg)
            if j["header"]["tr_id"] == "PINGPONG":
                await ws.send(msg)
            else:
                body = j.get("body", {})
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


def main():
    p = argparse.ArgumentParser(description="한국투자증권 API 미국주식 분봉·실시간")
    sub = p.add_subparsers(dest="cmd", required=True)
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
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
