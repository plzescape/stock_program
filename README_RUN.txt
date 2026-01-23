실행 순서 (모의투자 권장)
1) 키움 OpenAPI+ 설치/로그인 가능 상태 확인
2) Python 3.11 32bit + PyQt5 설치
3) 조건검색식(예: '단타_거래대금_500억')을 HTS에서 만들어둔 뒤,
   config.py의 CONDITION_NAME/INDEX를 맞춘다
4) main.py 실행 (08:50에 켜도 됨)
   - 09:00 이후 조건검색이 수행되고,
     조건검색 결과 종목을 OPT10080(3분봉)으로 순차 스캔하여 진입 신호가 뜨면 시장가 매수(SendOrder)
   - 체결은 OnReceiveChejanData로 확정하며, 체결 후 실시간 감시로 손절/익절/트레일링 수행

로그
- logs/system.log : 시스템/메시지/리셋
- logs/signal.log : 조건검색/진입판단
- logs/trade.log  : 주문/체결/청산

주의
- 모의에서 충분히 검증 후 실계좌로 전환하세요.
- TR 요청이 많으면 제한에 걸릴 수 있어 SCAN_MAX_CODES / SCAN_TR_DELAY_MS 조정 가능.
