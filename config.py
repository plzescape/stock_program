# ===== 계좌 / 환경 =====
# 모의투자부터 충분히 검증 후 실계좌로 전환하세요.
IS_REAL = False
ACCOUNT_NO = ""  # IS_REAL=True일 때만 사용 (예: "12345678")

# ✅ 모의투자에서 사용할 계좌 (국내주식)
MOCK_ACCOUNT_NO = "8120446011"

# ===== 매매 수량 =====
# BUY_MODE 옵션:
#   "AMOUNT"   → MAX_BUY_AMOUNT 이내로 수량 자동 계산 (1주 > MAX_BUY_AMOUNT이면 스킵)
#   "QTY"      → 무조건 QTY 만큼 매수 (금액 무시)
#   "BOTH"     → MAX_BUY_AMOUNT 이내 + 최대 QTY개 (둘 중 적은 쪽)
BUY_MODE = "BOTH"
QTY = 10000                 # 고정 수량 / 최대 수량
MAX_BUY_AMOUNT = 2000000    # 종목당 최대 매수금액 (원)
TOTAL_BUDGET = 20000000     # 총 투자 한도 (원)

# ===== 완성봉 손절 설정 =====
# CANDLE_SL_ENABLED: True면 "완성봉 종가 기준" ATR 손절 (메인)
#   → 1분봉이 닫히는 순간의 종가가 atr_sl_price 이하면 손절
#   → 순간 낙폭(급등락 노이즈)에 흔들리지 않음
# EMERGENCY_SL_RATE: 실시간 보조 안전망 (틱 단위)
#   → 순간 -EMERGENCY_SL_RATE 이하면 완성봉 무관 즉시 손절
#   → 0.0으로 설정하면 비활성화 (완성봉 손절만)
CANDLE_SL_ENABLED    = True
EMERGENCY_SL_RATE    = 0.025     # 실시간 안전망: -2.5% (ATR 손절과 별도 극단적 낙폭 방어) 비활성화 하려면 0.0으로 설정

# ===== ATR 기반 손절 / 익절 배수 =====
# ATR 계산: 진입 직전 14분봉 캔들 기준
#   손절가  = 매수가 - ATR × ATR_SL_MULT    (완성봉 종가가 이하면 손절)
#   TP1     = 매수가 + ATR × ATR_TP_MULT    (도달 시 TP1_RATIO 매도)
#   TP2     = TP1 체결 시점 고점 + ATR × ATR_TP_MULT  (TP1 후 고점 기준 재설정)
#   본절보호 = 매수가 - ATR × ATR_SAFE_MULT  (TP1 후 여기 이하로 밀리면 즉시 탈출)
#   트레일링: TP2 후 최고가 - ATR × ATR_TRAIL_MULT 이하 시 청산
ATR_PERIOD    = 14      # ATR 계산에 사용할 분봉 수
ATR_SL_MULT   = 1.5    # 손절 배수
ATR_TP_MULT   = 3.0    # 익절 배수 (TP1, TP2 공통)
ATR_SAFE_MULT = 1.5    # 본절보호 배수 (TP1 후 매수가 이하 보호선)
ATR_TRAIL_MULT = 1.5   # 트레일링 스탑 배수 (고점 기준)

# ===== 분할 매도 비율 =====
TP1_RATIO = 0.5         # TP1 도달 시 50% 매도
TP2_RATIO = 0.3         # TP2 도달 시 30% 매도 (나머지 트레일링)

# ===== 하위 호환 유지 (직접 참조하는 코드 없음, 참고용) =====
STOP_LOSS_RATE = 0.010   # 미사용 (ATR 손절로 대체)
TP1_RATE       = 0.02    # 미사용 (ATR TP로 대체)
TP2_RATE       = 0.03    # 미사용 (ATR TP로 대체)
TRAIL_START_RATE = 0.02  # 미사용 (ATR 트레일링으로 대체)
TRAIL_GAP        = 0.010 # 미사용 (ATR 트레일링으로 대체)

# ===== 제한 =====
MAX_TRADES_PER_DAY = 1000
CONDITION_INTERVAL_MIN = 30  # 조건검색 갱신 주기(분)

# ===== 조건검색식 =====
# 조건검색식 등록해놓은 것 : "AUTO_CANDI_MOMENTUM", "AUTO_CANDI_VOL_SPIKE", "AUTO_CANDI_BREAKOUT", "분봉급등주", "DANTA_1", "DANTA_2, CHUSAE_INDICATE"
# CONDITION_NAME: 단일 fallback (CONDITION_NAMES가 비어 있을 때 사용)
CONDITION_NAME = "분봉급등주"

# CONDITION_NAMES: 라운드로빈으로 순환할 조건검색식 목록
# - 매 주기(CONDITION_INTERVAL_MIN)마다 목록에서 하나씩 순서대로 호출
# - 각 조건식은 담당 전략에 특화된 종목을 유입시키는 역할
#   "분봉급등주"          → BREAKOUT (급등 돌파)
#   "급등주_주도주"        → BREAKOUT (주도주 돌파)
#   "급등주_눌림목_검색식"  → PULLBACK (눌림 반등)
#   "상승_깃발_패턴"       → FLAG     (횡보 수렴 재돌파)
# - 키움 API 제약: 스크린 하나에 조건식 하나만 실시간 등록 가능
#   → 조건식 4개를 스크린 9001~9004에 각각 고정 매핑
CONDITION_NAMES = [
    "분봉급등주",
    "급등주_주도주",
    "급등주_눌림목_검색식",
    "상승_깃발_패턴",
]

# ===== 스캔 설정 =====
SCAN_MAX_CODES = 30        # 조건검색 결과 중 최대 몇 종목만 스캔할지
SCAN_TR_DELAY_MS = 700     # 종목별 TR 요청 간 최소 딜레이(밀리초)
SCAN_CODE_COOLDOWN_SEC = 30  # 같은 종목 재스캔 방지 쿨타임(초)

# ===== 매수/매도 포지션 최대 개수 =====
MAX_POSITIONS = 10

# ===== 자가진단 =====
TIME_STOP_SEC = 600        # 10분
TIME_STOP_MAX_LOSS = -0.003  # -0.3%
SELL_COOLDOWN_SEC = 3

# 거래량 기반 Time Stop
VOL_CHECK_TICKS = 20        # 최근 20틱
VOL_AVG_MIN = 50            # 평균 거래량 50 이하 = 사실상 정지

# ===== 주문 체결 타임아웃 =====
BUY_FILL_TIMEOUT_SEC = 10     # 체결 대기 최대 8초 (너 환경에 맞게 5~15초 추천)
CANCEL_RETRY_COOLDOWN_SEC = 2
MAX_CANCEL_RETRIES = 2
MAX_REENTRY_RETRIES = 2   # 취소 후 재매수 횟수
REENTRY_DELAY_SEC = 1     # 재매수 딜레이
FORCE_ABANDON_TIMEOUT = 30  # 주문번호 없이 30초 경과 시 강제 포기

# (TP 지정가 제거됨 - 모두 시장가 매도)
# ===== 강제청산 시각 =====
FORCE_LIQUIDATION_HOUR = 15
FORCE_LIQUIDATION_MIN  = 20
