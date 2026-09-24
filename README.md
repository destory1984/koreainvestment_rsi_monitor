# KoreaInvestment RSI Monitor

**한국투자증권 Open API 로 미국주식 분봉과 실시간 체결가를 받아서, RSI 와 MACD 를 실시간으로 보여준다.**

<sub>Live RSI and MACD for US stocks, computed from Korea Investment & Securities (KIS) Open API minute bars and trade feed. Korean UI.</sub>

- **1단계 (지금)** — 분봉과 실시간 체결가로 RSI·MACD 를 계산해서 보여준다.
- **2단계** — [webull_rsi_monitor](https://github.com/destory1984/webull_rsi_monitor) 처럼 선을 넘으면 소리·텔레그램으로 알린다.

---

## 왜 만들었나

미국주식 실시간 시세를 따로 사려면 비싸다. 한국투자증권 계좌가 있으면 Open API 로
미국 정규장·프리장·애프터장 실시간 체결가를 **무료로** 받을 수 있다. 분봉도 같은 키로 받는다.
전에는 Webull 화면을 캡처해서 RSI 숫자를 읽었는데, 이제 숫자를 직접 계산한다.

## 필요한 것

- Python 3.11+
- 한국투자증권 계좌와 [KIS Developers](https://apiportal.koreainvestment.com/) 에서 받은 앱키·시크릿
- 해외주식 시세를 쓰려면 한국투자증권 앱에서 해외주식 거래 신청이 되어 있어야 한다

```bash
pip install requests websockets
```

## 키 넣기

환경변수로 넣는다.

```bash
export KIS_APPKEY=앱키 KIS_APPSECRET=시크릿
```

아니면 같은 폴더에 `kis_config.json` 을 만든다. 이 파일은 `.gitignore` 에 들어 있다.

```json
{"appkey": "앱키", "appsecret": "시크릿"}
```

## 쓰는 법

### 분봉

```bash
python kis_us.py bars TSLA SOXL        # 5분봉, 최근 20개를 보여준다
python kis_us.py bars TSLA --min 1     # 1분봉
python kis_us.py bars NYS:BE -n 50     # 거래소를 직접 적기, 50개 보여주기
python kis_us.py bars TSLA --today     # 전날 봉은 빼기
```

한 번에 최근 120개까지 받는다. 5분봉이면 미국 시각 새벽 5시 프리장부터 지금까지 정도다.

### 실시간 체결가

```bash
python kis_us.py live TSLA SOXL FCEL
```

체결될 때마다 한 줄씩 찍는다. Ctrl+C 로 끝낸다.

```
04:04:59  TSLA     379.0700   -0.28%  체결   3000  누적 16040917  (미국 150505)
04:04:59  SOXL     145.2900   -0.66%  체결   1066  누적 29647825  (미국 150505)
```

한국 낮 시간의 주간거래는 `--prefix R` 로 받는다. 이때는 거래소 코드가 `BAQ`(나스닥),
`BAY`(뉴욕), `BAA`(아멕스) 라서 `BAQ:TSLA` 처럼 적어야 한다.

### RSI · MACD 표

```bash
python kis_us.py watch TSLA SOXL MU
```

종목마다 한 줄씩 표를 띄워 두고 체결이 올 때마다 고친다. RSI 가 30 이하면 파랗게, 70 이상이면
빨갛게 칠한다 (`--lower`, `--upper` 로 바꾼다). MACD 히스토그램은 양수면 초록, 음수면 빨강이다.

```
┌──────┬───────────┬──────┬───────┬─────────┬─────────┬─────────┬──────────┐
│ 종목 │      가격 │ 등락 │   RSI │    MACD │  시그널 │  히스토 │ 미국시각 │
├──────┼───────────┼──────┼───────┼─────────┼─────────┼─────────┼──────────┤
│ TSLA │  379.5600 │-0.13%│ 50.26 │ -0.1509 │ -0.1020 │ -0.0489 │ 15:20:13 │
│ SOXL │  145.9265 │+0.02%│ 59.68 │ +0.6265 │ +0.5696 │ +0.0569 │ 15:20:13 │
└──────┴───────────┴──────┴───────┴─────────┴─────────┴─────────┴──────────┘
```

Git Bash 에서 표가 깨지면 `winpty python kis_us.py watch ...` 로 실행한다.

### RSI · MACD 줄 단위

```bash
python kis_us.py rsi TSLA SOXL             # 바뀔 때마다 한 줄씩
python kis_us.py rsi TSLA --once           # 분봉 값만 보고 끝내기
python kis_us.py rsi TSLA --min 1 --period 9
```

RSI 가 0.1 이상 바뀌거나 MACD 히스토그램 부호가 바뀔 때만 찍는다 (`--step` 으로 바꾼다).

### 어떻게 계산하나

분봉 120개로 값을 잡아 두고, 체결이 올 때마다 진행 중인 봉의 종가를 바꿔서 다시 계산한다.
시간이 다음 봉으로 넘어가면 새 봉을 붙인다.

- **RSI** — Webull 등 대부분의 차트와 같은 와일더 방식이다. 처음 14개는 오른 폭·내린 폭을
  산술평균하고, 그 뒤로는 `(앞 평균 × 13 + 이번 값) / 14` 로 이어 간다.
- **MACD** — 12·26 봉 지수이동평균의 차이가 MACD 선, 그 9봉 지수이동평균이 시그널,
  둘의 차이가 히스토그램이다. Webull 은 히스토그램을 두 배로 그리기도 하는데 부호는 같다.

프리장 봉도 들어가니, 차트에서 연장 거래 시간을 켜 둔 것과 견줘야 값이 맞는다.

## 알아둘 것

- **실시간 연결은 앱키 하나에 하나뿐이다.** `watch` 를 켜 둔 채 다른 창에서 `live` 나 `rsi` 를 켜면
  `ALREADY IN USE appkey` 로 거절된다. 종목은 한 연결에 여러 개 넣으면 된다.
- **거래소는 알아서 찾는다.** 종목만 적으면 나스닥 → 뉴욕 → 아멕스 순서로 찾아보고
  `kis_exchange.json` 에 적어 둔다. 한 번 찾은 종목은 다시 찾지 않는다.
- **접근토큰은 24시간 쓴다.** 새 토큰은 1분에 한 번만 받을 수 있어서 `kis_token.json` 에
  두고 다시 쓴다.
- **실시간 데이터는 맨 앞에 구독 키가 한 칸 더 붙어 온다.** 공식 예제의 필드 목록에는 이 칸이
  빠져 있어서, 그대로 쓰면 값이 한 칸씩 밀린다. 여기서는 `RSYM` 을 맨 앞에 넣어 맞췄다.
- Git Bash 에서도 한글이 깨지지 않게 출력을 UTF-8 로 내보낸다.

## 라이선스

MIT
