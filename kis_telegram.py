#!/usr/bin/env python3
"""
kis_telegram.py — 알림을 텔레그램으로도 보낸다. PC 앞에 없을 때 폰으로 받으려는 것.

────────────────────────────────────────────────────────────
처음 한 번

  1. 텔레그램에서 @BotFather 에게 /newbot 을 보내 봇을 만들고 토큰(123456:ABC... 같은 것)을 받는다.
  2. python kis_telegram.py setup
     토큰을 넣고, 안내대로 방금 만든 봇에게 아무 말이나 보내면 대화방 번호를 찾아
     kis_config.json 에 적고 시험 메시지를 보낸다. kis_config.json 은 저장소에 안 올라간다.

환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 로 줘도 된다 (파일보다 먼저 본다).

  python kis_telegram.py test            시험 메시지 한 번

────────────────────────────────────────────────────────────
웹 서버에서

선 알림·시그널이 울릴 때 같이 보낸다 (억제된 것, 켤 때 이미 넘어 있던 것은 안 보낸다).
소리를 가린 알림(조용한 시각·세션 끔·종목 소리 끔)은 알림음 없이 보낸다.
「⚙ 설정」의 「텔레그램」으로 끄고 켠다. 보내기는 따로 도는 스레드가 하니 서버가 기다리지 않는다.
"""
import json
import os
import queue
import sys
import threading
import time
from getpass import getpass

import requests

import kis_rsi as k

API = "https://api.telegram.org/bot{token}/{method}"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load():
    """(토큰, 대화방 번호). 없으면 None."""
    try:
        cfg = json.loads(k.CONFIG.read_text(encoding="utf-8")) if k.CONFIG.exists() else {}
    except ValueError:
        cfg = {}
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_token"),
            os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat"))


def call(token, method, **params):
    """텔레그램 봇 API. 실패하면 RuntimeError (메시지에 토큰이 나오지 않게 지운다)."""
    try:
        r = requests.post(API.format(token=token, method=method), json=params, timeout=15)
        j = r.json()
    except Exception as e:
        raise RuntimeError(str(e).replace(token, "<토큰>")) from None
    if not j.get("ok"):
        raise RuntimeError(f"{j.get('error_code')} {j.get('description')}")
    return j["result"]


class Telegram:
    """알림을 줄에 넣으면 스레드가 차례로 보낸다."""

    def __init__(self):
        self.token, self.chat = load()
        self.last_error = ""
        self.q = queue.Queue()
        self.thread = None

    @property
    def ready(self):
        return bool(self.token and self.chat)

    def send(self, text, silent=False):
        if not self.ready:
            return
        if not self.thread:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        self.q.put((text, silent))

    def send_now(self, text):
        """기다렸다 보낸다 (시험용). 실패하면 RuntimeError."""
        if not self.ready:
            raise RuntimeError("텔레그램 설정이 없다 — python kis_telegram.py setup")
        call(self.token, "sendMessage", chat_id=self.chat, text=text)
        self.last_error = ""

    def _run(self):
        while True:
            text, silent = self.q.get()
            try:
                call(self.token, "sendMessage", chat_id=self.chat, text=text, disable_notification=silent)
                self.last_error = ""
            except Exception as e:
                self.last_error = str(e)
                print(f"텔레그램 — {e}", flush=True)
            time.sleep(0.05)   # 한 대화방에 1초 여러 개는 괜찮지만 몰릴 때 조금 띄운다


def price_text(excd, price):
    if price is None:
        return ""
    return f"{price:,.0f}원" if excd == "KRX" else f"${k.px(price)}"


def alert_text(name, excd, ev):
    """선 알림 한 줄: 「🔴 테슬라 65 초과 · RSI 66.2 · $412.30」. 위는 빨강, 아래는 파랑 (한국식)."""
    icon = "🔴" if ev["zone"] == "above" else "🔵"
    strong = " ‼" if ev["strength"] == "strong" else ""
    return f"{icon} {name} {ev['text']}{strong} · RSI {ev['rsi']:.1f} · {price_text(excd, ev.get('price'))}"


def signal_text(name, excd, ev):
    """시그널 한 줄: 「📈 SOXL 매수 시그널 (강, 추세 순응) · RSI 41.0 · $31.20」."""
    icon = "📈" if ev["side"] == "buy" else "📉"
    return f"{icon} {name} {ev['text']} · RSI {ev['rsi']:.1f} · {price_text(excd, ev.get('price'))}"


# ── 명령 ──────────────────────────────────────────────────────
def save(token, chat):
    cfg = json.loads(k.CONFIG.read_text(encoding="utf-8")) if k.CONFIG.exists() else {}
    cfg.update({"telegram_token": token, "telegram_chat": str(chat)})
    k.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")


def cmd_setup():
    print("텔레그램 @BotFather 에게 /newbot 을 보내 봇을 만들고 받은 토큰을 넣는다.")
    print(f"토큰은 이 폴더의 {k.CONFIG.name} 에만 적힌다 (저장소에는 올라가지 않는다).\n")
    token = getpass("봇 토큰 (화면에 안 보임): ").strip()
    if not token:
        sys.exit("비어 있다. 다시 할 것.")
    try:
        me = call(token, "getMe")
    except RuntimeError as e:
        sys.exit(f"토큰이 맞지 않다 — {e}")
    print(f"\n봇 @{me['username']} 을 찾았다. 텔레그램에서 이 봇에게 아무 말이나 보낼 것 (2분 기다린다).")
    seen = {u["update_id"] for u in call(token, "getUpdates")}
    chat, until = None, time.time() + 120
    while not chat and time.time() < until:
        time.sleep(2)
        for u in call(token, "getUpdates", timeout=0):
            msg = u.get("message") or {}
            if u["update_id"] not in seen and msg.get("chat"):
                chat = msg["chat"]["id"]
    if not chat:
        sys.exit("2분 안에 메시지가 오지 않았다. 다시 할 것.")
    call(token, "sendMessage", chat_id=chat, text="RSI Monitor 알림을 여기로 보낸다.")
    save(token, chat)
    print(f"\n됐다. 대화방 {chat} 을 {k.CONFIG.name} 에 적었고 시험 메시지를 보냈다. 웹 서버를 다시 켜면 보낸다.")


def cmd_test():
    t = Telegram()
    try:
        t.send_now("RSI Monitor 시험 메시지")
    except RuntimeError as e:
        sys.exit(str(e))
    print("보냈다.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        cmd_setup()
    elif cmd == "test":
        cmd_test()
    else:
        sys.exit("python kis_telegram.py setup | test")
