실행 순서 (모의투자 권장) — 키움 REST API 버전
1) openapi.kiwoom.com 에서 REST API 사용 신청
   - 모의투자 / 실전투자 앱키(appkey)·시크릿키(secretkey)를 각각 발급
   - 이 PC 의 공인 IP 를 허용 IP 로 등록 (등록 안 하면 토큰 발급부터 거절됨)
2) kiwoom_keys.example.json 을 kiwoom_keys.json 으로 복사하고 키 입력
   (kiwoom_keys.json 은 .gitignore 대상 — 절대 커밋하지 말 것)
   또는 환경변수 KIWOOM_APP_KEY / KIWOOM_SECRET_KEY 사용
3) Python 3.11+ (32/64비트 무관) 가상환경에서: pip install -r requirements.txt
   - OCX(OpenAPI+ 설치, 32비트 파이썬, 로그인 창)는 더 이상 필요 없음
4) 조건검색식을 HTS(영웅문)에서 만들어 저장한 뒤,
   config.py 의 CONDITION_NAMES 에 같은 이름을 적는다 (seq 는 자동 매핑)
5) main.py 실행 (08:50에 켜도 됨)
   - 접근토큰(au10001) 발급 → 웹소켓 접속(주문체결 00 등록) → 조건식 목록(CNSRLST)
   - 09:00 이후 조건검색(CNSRREQ, 실시간)이 수행되고,
     조건검색 결과 종목을 ka10080(3분봉)으로 순차 스캔하여 진입 신호가 뜨면 매수(kt10000)
   - 체결은 웹소켓 주문체결(00)로 확정하며, 체결 후 주식체결(0B) 실시간 감시로 손절/익절/트레일링 수행
   - 웹소켓이 끊기면 자동 재접속 + 실시간/조건검색 재등록 (system.log 의 WS_* 로그)

연구용 데이터
- tick_data/YYYY-MM-DD/ : main.py 가 조건검색 편입 종목의 체결(0B)·호가잔량(0D)을 녹화
    <code>.trade.csv  체결 한 틱 한 줄 (체결강도, 누적거래대금, 최우선호가 등. 거래량 +매수/-매도체결)
    <code>.book.csv   호가잔량 종목당 최대 1초 1줄
    events.csv        조건검색 편입/이탈
  지난 날짜는 다음 실행 때 .csv.gz 로 압축. config.py TICK_RECORD_* 로 끄거나 종목 수 조절.
- daily_data/ : collect_daily.py 가 받는 3년치 일봉 (5_Swing_Research.bat)
- swing_research.py : 일봉 스윙 진입 후보 IS/OOS 검증

로그
- logs/system.log : 시스템/메시지/리셋
- logs/signal.log : 조건검색/진입판단
- logs/trade.log  : 주문/체결/청산

리포트 (For_analyzer.bat 실행 시 정리)
- reports/Report_YYMMDD/ : 그날의 엑셀 성과표 + 종목별 차트 + 로그 원본
- reports/index.html     : 날짜별 손익 모아보기 (여기서 각 날짜로 이동)
- 로그는 리포트 폴더로 옮겨지고 logs/ 는 비워집니다

주의
- 모의에서 충분히 검증 후 실계좌로 전환하세요.
- TR 요청이 많으면 제한에 걸릴 수 있어 SCAN_MAX_CODES / SCAN_TR_DELAY_MS 조정 가능.
- REST 호출 제한에 걸리면(system.log 의 REST_RATE_LIMIT) config.py 의
  REST_RATE_PER_SEC 를 낮추거나 SCAN_TR_DELAY_MS 를 늘린다.
- 모의투자 서버(mockapi.kiwoom.com)는 KRX 만 지원한다.
