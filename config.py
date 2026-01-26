# ===== 계좌 / 환경 =====
# 모의투자부터 충분히 검증 후 실계좌로 전환하세요.
IS_REAL = False
ACCOUNT_NO = ""  # IS_REAL=True일 때만 사용 (예: "12345678")

# ✅ 모의투자에서 사용할 계좌 (국내주식)
MOCK_ACCOUNT_NO = "8118694111"

# ===== 매매 수량 =====
QTY = 10  # 분할익절을 쓰려면 10주 이상 권장

# ===== 손절 / 익절 / 트레일링 =====
STOP_LOSS_RATE = 0.01   # -1%

TP1_RATE = 0.01         # +1%
TP1_RATIO = 0.5         # 50% 매도

TP2_RATE = 0.02         # +2%
TP2_RATIO = 0.3         # 30% 매도

TRAIL_START_RATE = 0.02 # +2%부터 트레일링 활성화
TRAIL_GAP = 0.01        # 고점 대비 1% 하락 시 청산

# ===== 제한 =====
MAX_TRADES_PER_DAY = 100
CONDITION_INTERVAL_MIN = 3  # 조건검색 갱신 주기(분)

# ===== 조건검색식 =====
CONDITION_NAME = "단타_거래대금_500억"
CONDITION_INDEX = 0

# ===== 스캔 설정 =====
SCAN_MAX_CODES = 30        # 조건검색 결과 중 최대 몇 종목만 스캔할지
SCAN_TR_DELAY_MS = 250     # 종목별 TR 요청 간 최소 딜레이(밀리초)
