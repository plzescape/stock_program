# ===== 계좌 / 환경 =====
# 모의투자부터 충분히 검증 후 실계좌로 전환하세요.
IS_REAL = False
ACCOUNT_NO = ""  # IS_REAL=True일 때만 사용 (예: "12345678")

# ✅ 모의투자에서 사용할 계좌 (국내주식)
MOCK_ACCOUNT_NO = "8127278511"

# ===== 매매 수량 =====
# BUY_MODE 옵션:
#   "AMOUNT"   → MAX_BUY_AMOUNT 이내로 수량 자동 계산 (1주 > MAX_BUY_AMOUNT이면 스킵)
#   "QTY"      → 무조건 QTY 만큼 매수 (금액 무시)
#   "BOTH"     → MAX_BUY_AMOUNT 이내 + 최대 QTY개 (둘 중 적은 쪽)
BUY_MODE = "BOTH"
QTY = 10000                 # 고정 수량 / 최대 수량
MAX_BUY_AMOUNT = 5000000    # 종목당 최대 매수금액 (원)
TOTAL_BUDGET = 50000000     # 총 투자 한도 (원)

# ===== 분봉 단위 =====
CANDLE_INTERVAL_MIN = 3   # OPT10080 분봉 단위 (1 / 3 / 5분봉). 전략 전체에 적용.
                          # 3분봉: 패턴 노이즈↓, TP 고점 포착률↑, SL 반응 최대 3분 지연

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
ATR_SL_MULT   = 2.0    # 손절 배수 ← 1.5→2.0: SL폭 확대로 슬리피지 완충
ATR_TP_MULT   = 1.5    # 익절 배수 (TP1, TP2 공통) ← 2.0→1.5: TP 달성률 향상 (12분 내 ATR×2 달성 어려움)
ATR_SAFE_MULT = 1.0    # 본절보호 배수 (TP1 후 매수가 이하 보호선) ← 1.5→1.0: 조정 여유 확보
ATR_TRAIL_MULT = 1.5   # 트레일링 스탑 배수 (고점 기준)

# ===== 분할 매도 비율 =====
TP1_RATIO = 0.40        # TP1 도달 시 40% 매도  ← 50%→40% 조정
TP2_RATIO = 0.40        # TP2 도달 시 40% 매도  ← 30%→40% 조정
                        # 구조: TP1=40% + TP2=40% + 잔여=20%(트레일링)

# ===== 하위 호환 유지 (직접 참조하는 코드 없음, 참고용) =====
STOP_LOSS_RATE = 0.010   # 미사용 (ATR 손절로 대체)
TP1_RATE       = 0.02    # 미사용 (ATR TP로 대체)
TP2_RATE       = 0.03    # 미사용 (ATR TP로 대체)
TRAIL_START_RATE = 0.02  # 미사용 (ATR 트레일링으로 대체)
TRAIL_GAP        = 0.010 # 미사용 (ATR 트레일링으로 대체)

# ===== 제한 =====
MAX_TRADES_PER_DAY = 1000
CONDITION_INTERVAL_MIN = 5  # 조건검색 갱신 주기(분)

# ===== 조건검색식 =====
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
    "CHUSAE_INDICATE",
    "급등주_주도주",
    "급등주_눌림목_검색식"
]

# ===== 스캔 설정 =====
SCAN_MAX_CODES = 30        # 조건검색 결과 중 최대 몇 종목만 스캔할지
SCAN_TR_DELAY_MS = 700     # 종목별 TR 요청 간 최소 딜레이(밀리초)
SCAN_CODE_COOLDOWN_SEC = 30  # 같은 종목 재스캔 방지 쿨타임(초)

# ===== 매수/매도 포지션 최대 개수 =====
MAX_POSITIONS = 10

# ===== 자가진단 =====
TIME_STOP_SEC = 900        # 15분 (기존 12분 → 15분: 방향성 확인 시간 확보, TIME_STOP 손절 축소)
TIME_STOP_MAX_LOSS = -0.003  # -0.3%

# ── 미니 트레일링 스탑 (TP1 미달 구간 수익 보호) ──
# TP1에 못 미치더라도 수익이 TRIGGER 이상 올라가면 고점을 추적,
# 고점 대비 GAP 이상 하락 시 즉시 익절
MINI_TRAIL_TRIGGER  = 0.015   # +1.5% 이상 수익 시 활성화
MINI_TRAIL_GAP      = 0.015   # 고점 대비 -1.5% 하락 시 청산 (기존 1.0% → 1.5%: 선도전기·일성건설 5초 조기청산 방지)
MINI_TRAIL_GAP_OPEN = 0.020   # 장 시작 10분(09:00~09:10) 고변동성 구간 완화: -2.0%
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
SELL_PENDING_TIMEOUT_SEC = 15  # SELL 미체결 판정 타임아웃 (기존 30초 → 15초)

# (TP 지정가 제거됨 - 모두 시장가 매도)
# ===== 강제청산 시각 =====
FORCE_LIQUIDATION_HOUR = 15
FORCE_LIQUIDATION_MIN  = 20

# 신규 진입 하드컷: 강제청산 N분 전부터 모든 전략 진입 차단
# 예: FORCE_LIQUIDATION=15:20, ENTRY_CUTOFF_MIN_BEFORE=15 → 15:05 이후 진입 차단
# 장 마감 직전 진입은 손절 후 ZOMBIE 루프 + 장 마감 미청산 이월 원인이 됨
ENTRY_CUTOFF_MIN_BEFORE = 10  # 강제청산 10분 전부터 신규 진입 차단
                              # 15분 → 10분: 15:05~15:10 구간(25~30분 여유)도 진입 가능하도록 완화

# ===== ATR 최소값 필터 =====
# 진입 전 ATR이 이 값보다 작으면 스킵
# ATR이 너무 작으면 TP1까지의 수익이 수수료(왕복 약 0.35%)에 못 미침
# 예: 2,000원 × ATR=3.9원 → TP1(ATR×2=7.8원) 수익 0.39% ≒ 수수료와 거의 동일
# 권장값: 매수금액 5,000,000원 기준 수수료 약 17,500원 / 매수수량으로 계산
#   → 500만원, 1주당 1,000원 = 5,000주 → 17,500/5,000 = 3.5원 이상 필요
#   → 여유분(슬리피지·스프레드) 포함해 10원 이상 권장
ATR_MIN_VALUE = 10          # ATR 절대값 하한 (원). 미만이면 진입 스킵
ATR_MIN_RATIO = 0.0035      # ATR/현재가 비율 하한 (0.35%). 미만이면 진입 스킵
                            # 0.5% → 0.35% 완화: 제넥신(0.49%), 한미글로벌(0.47%) 등
                            # 거래량·패턴 완벽한 신호가 비율 0.01~0.09%p 차이로 차단되는 문제 개선
                            # 둘 중 하나라도 미달이면 ENTRY_SKIP_ATR 로그 후 스킵

# ===== BREAKOUT 에너지 소진 방지 =====
# 당일 첫 COND_IN 이후 이 시간(초)이 경과한 종목은 BREAKOUT 진입 차단
# 나노팀 케이스: 09:37 첫 급등 → 10:17(40분 후) 재급등 → 즉시 손절
# → 20분(1,200초) 이상 경과 = 급등 에너지 소진 가능성
BREAKOUT_FIRST_COND_TIMEOUT = 900    # 15분으로 단축 (기존 20분 → NEVER_ROSE 개선)
# ===== BREAKOUT 강화 필터 (로그 분석 기반) =====
# NEVER_ROSE 비율 75~78% 개선 목적
# 급등 경과시간 상한 (봉 수) — 기존 10봉에서 7봉으로 단축
BREAKOUT_FRESH_CANDLES_MAX = 5        # 급등봉 이후 5봉(15분, 3분봉×5) 이내만 허용 ← 1분봉 7봉(7분) 대비 동일 시간대

# 당일 누적 상승 상한 (기존 15% → 12% → 8% → 10%): 8% 상한이 9-10% 구간 유효 종목 차단 확인
BREAKOUT_DAY_SURGE_MAX = 10.0         # 당일 상승 상한 (%)

# EMA20 이격 상한 (기존 5% → 4% → 3.5% → 4.5%): 3.5% 상한이 유효 신호(단석 4.3% 등) 차단 확인
BREAKOUT_EMA_GAP_MAX = 4.5            # EMA20 대비 이격 상한 (%)

# ===== FLAG 강화 필터 (로그 분석 기반) =====
# 재돌파봉 거래량 기준 (기존 횡보평균 2배 → 3배): 약한 돌파 차단
FLAG_REBREAK_VOL_MIN = 3.0            # 재돌파봉 거래량 ≥ 횡보평균 × 이 배수

# 기준봉 최소 상승폭: 약한 깃발 폴 차단 (이랜텍 +1.9% 케이스 방지)
# 폴이 짧으면 재돌파 후 추가 상승 여력 부족 → TP 달성 어려움
FLAG_POLE_MIN_RISE = 3.0              # 기준봉 상승폭 최소값 (%)

# 당일 상승 상한 (기존 없음 → 20%): 고점 추격 진입 차단
FLAG_DAY_RISE_MAX = 20.0              # 당일 상승 상한 (%)

# ===== PULLBACK 강화 필터 =====
# 시각 하드컷 — 기존 11:00 유지
PULLBACK_TIME_CUT_ENABLED = False
PULLBACK_TIME_CUT_STR = "15:00"       # 참고용 (코드에서 직접 사용)

# BREAKOUT 시각 하드컷 — 기존 10:30 유지
BREAKOUT_TIME_CUT_ENABLED = False
BREAKOUT_TIME_CUT_STR = "15:00"       # 참고용

