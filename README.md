# KIS US Quotes

**한국투자증권 Open API 로 미국주식 분봉과 실시간 체결가를 받아온다.**

<sub>Pulls US stock minute bars and live trades from the Korea Investment & Securities (KIS) Open API. Korean UI.</sub>

---

## 왜 만들었나

미국주식 실시간 시세를 따로 사려면 비싸다. 한국투자증권 계좌가 있으면 Open API 로
미국 정규장·프리장·애프터장 실시간 체결가를 **무료로** 받을 수 있다. 분봉도 같은 키로 받는다.

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

## 알아둘 것

- **거래소는 알아서 찾는다.** 종목만 적으면 나스닥 → 뉴욕 → 아멕스 순서로 찾아보고
  `kis_exchange.json` 에 적어 둔다. 한 번 찾은 종목은 다시 찾지 않는다.
- **접근토큰은 24시간 쓴다.** 새 토큰은 1분에 한 번만 받을 수 있어서 `kis_token.json` 에
  두고 다시 쓴다.
- **실시간 데이터는 맨 앞에 구독 키가 한 칸 더 붙어 온다.** 공식 예제의 필드 목록에는 이 칸이
  빠져 있어서, 그대로 쓰면 값이 한 칸씩 밀린다. 여기서는 `RSYM` 을 맨 앞에 넣어 맞췄다.
- Git Bash 에서도 한글이 깨지지 않게 출력을 UTF-8 로 내보낸다.

## 라이선스

MIT
