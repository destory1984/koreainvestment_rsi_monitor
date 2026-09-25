#!/usr/bin/env python3
"""
tts_make.py — 로컬 TTS 서버를 잠깐 띄워 알림 문장을 녹음하고, 끝나면 내린다.

로컬 TTS(Qwen3-TTS, 화자 Sohee)는 VRAM 을 8GB 남짓 먹으니 늘 띄워 두지 않는다.
종목을 더했거나 읽는 법(kis_alert.TTS_SAY_AS)을 바꿨을 때만 이것을 돌린다.

  1. .venv_tts 의 tts_server.py 를 창 없이 띄우고 준비될 때까지 기다린다
     (이미 떠 있으면 그것을 쓰고, 끝나도 내리지 않는다)
  2. 종목 목록의 알림 문장(초과·미만·매수·매도 시그널)과 인사 가운데 로컬 목소리 파일이 없는 것을 만든다.
     Edge 음성(.mp3)으로만 있던 문장도 로컬로 다시 만들고 .mp3 는 지운다
  3. 서버를 내린다

  python tts_make.py              없는 것만 만든다
  python tts_make.py --redo BE    BE 문장은 있어도 지우고 다시 만든다 (여럿 적어도 된다)
  python tts_make.py --dry-run    만들 문장만 보여 준다

종목 이름(국내 종목은 'SK하이닉스' 같은 이름을 읽는다)은 웹 서버가 떠 있으면 거기서 받고,
없으면 한국투자증권에서 찾는다. 웹 서버는 다시 켜지 않아도 새 파일을 쓴다. 다만 읽는 법을 바꿨다면
웹 서버가 옛 문장을 들고 있으니 다시 켜야 새 문장으로 읽는다.
"""
import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import kis_alert as al

HERE = Path(__file__).parent
VENV_PY = HERE / ".venv_tts" / "Scripts" / "python.exe"
WATCHLIST = HERE / "kis_watchlist.json"
SETTINGS = HERE / "kis_settings.json"
PORT = 47650
WAIT_SEC = 900   # 처음에는 모델(약 4.3GB)을 받느라 오래 걸린다


def port_open():
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def names():
    """[(이름, 티커)]. 웹 서버가 떠 있으면 거기서, 아니면 한국투자증권에서."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/state", timeout=3) as r:
            return [(row["name"], row["symb"]) for row in json.load(r)["rows"]]
    except Exception:
        pass
    import kis_rsi as k
    appkey, secret = k.load_keys()
    out = []
    for t in json.loads(WATCHLIST.read_text(encoding="utf-8")):
        excd, symb = k.resolve(appkey, secret, t)
        if excd == "KRX":
            k.fetch_kr_bars(appkey, secret, symb)   # 이름을 KR_INFO 에 채운다
        out.append((k.KR_INFO.get(symb, {}).get("name", symb) if excd == "KRX" else symb, symb))
    return out


def greetings():
    try:
        cfg = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    return [t for t in (cfg.get("greeting", al.TTS_GREETING), cfg.get("greeting_again", al.TTS_GREETING_AGAIN)) if t]


def main():
    ap = argparse.ArgumentParser(description="로컬 TTS 서버를 잠깐 띄워 알림 문장을 녹음한다")
    ap.add_argument("--redo", nargs="+", default=[], metavar="티커", help="이 종목 문장은 있어도 다시 만든다")
    ap.add_argument("--dry-run", action="store_true", help="만들 문장만 보여 준다")
    args = ap.parse_args()
    redo = {s.upper() for s in args.redo}

    voice = al.Voice(HERE / "tts_cache")
    jobs = [(t, "") for t in greetings()]
    for name, symb in names():
        jobs += [(t, symb) for t in al.phrases(name, symb)]
    todo = [t for t, symb in dict.fromkeys(jobs)
            if symb in redo or not voice.path(t, "local").is_file()]
    print(f"문장 {len(jobs)}개 중 만들 것 {len(todo)}개")
    for t in todo:
        print("  " + t)
    if not todo or args.dry_run:
        return

    if not VENV_PY.exists():
        sys.exit(f"{VENV_PY} 가 없다. README 의 「로컬 TTS 를 쓰려면」대로 먼저 깔 것.")
    started = None
    if not port_open():
        print("로컬 TTS 서버를 띄운다…", flush=True)
        started = subprocess.Popen([str(VENV_PY), "tts_server.py"], cwd=HERE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        t0 = time.time()
        while not port_open():
            if started.poll() is not None:
                sys.exit(f"로컬 TTS 서버가 떴다가 죽었다 (종료 코드 {started.returncode}). "
                         f"창에서 직접 `.venv_tts/Scripts/python tts_server.py` 를 돌려 까닭을 볼 것.")
            if time.time() - t0 > WAIT_SEC:
                started.kill()
                sys.exit("로컬 TTS 서버가 준비되지 않는다.")
            time.sleep(2)
        print(f"준비됨 ({time.time() - t0:.0f}초)", flush=True)
    try:
        made = 0
        for t in todo:
            for engine in ("local", "edge"):   # 다시 만들 것과 Edge 로만 있던 것은 지운다
                p = voice.path(t, engine)
                if p.exists():
                    p.unlink()
            t0 = time.time()
            voice.make(t)
            p = voice.path(t, "local")
            ok = p.is_file()
            made += ok
            print(f"  {'만듦' if ok else '실패'} {t} ({time.time() - t0:.1f}초)"
                  + ("" if ok else f" — {voice.last_error}"), flush=True)
        print(f"{made}/{len(todo)}개 만들었다")
    finally:
        if started:
            started.terminate()
            try:
                started.wait(10)
            except subprocess.TimeoutExpired:
                started.kill()
            print("로컬 TTS 서버를 내렸다")


if __name__ == "__main__":
    main()
