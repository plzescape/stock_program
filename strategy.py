"""
strategy.py — BREAKOUT / PULLBACK 지표 재설계 (2026-04-15 분석 기반)

[VER8 — 04-13/14 데이터 기반 근본 재설계]

진단 요약:
  - NEVER_ROSE(진입 즉시 하락) 47% → 지표 자체에 방향 예측력 없음
  - 승리 평균 +0.26% / 손실 평균 -2.62% → 기댓값 음수 구조
  - RSI/MACD: 1분봉에서 노이즈, 통과해도 손절 반복
  - 캔들강도: "현재 강함" 측정, "이후 오를지" 구분 불가

BREAKOUT VER8:
  삭제: 캔들강도 조건, 신호봉 진행률 조건
  추가: 3연속 양봉 필수 (지속 매수세 증거)
  추가: 급등 경과시간 ≤ 10봉 (에너지 소진 방지)
  변경: 당일상승 상한 20% → 15%

PULLBACK VER8:
  삭제: RSI, MACD, EMA근접 조건, 오더블록 조건
  추가: 갭상승 필수 (시초가 > EMA20 × 1.02)
  추가: 눌림 구간 거래량 감소 확인 (급등봉 대비 50% 미만)
  추가: 반등봉 거래량 급증 (눌림 평균 2배 이상)
"""

from datetime import datetime, time

MIN_VOL_RATIO = 3.0   # 급등주 진입: 최소 3배 이상 거래량 급증 ← 1.5→3.0 강화
MAX_VOL_RATIO = 15.0


# ==================================================
# ── 시장 시간 ──
# ==================================================
def is_market_time() -> bool:
    now = datetime.now().time()
    return time(9, 0) <= now <= time(15, 30)


# ==================================================
# ── 공통 지표 계산 유틸 ──
# ==================================================

def _calc_ema(closes: list, period: int):
    """
    EMA 계산.
    closes: [최신→과거] 순서 (parse_1min 그대로).
    """
    if len(closes) < period:
        return None
    prices = list(reversed(closes))
    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def _calc_rsi(closes: list, period: int = 14):
    """
    RSI 계산.
    closes: [최신→과거] 순서.
    """
    prices = list(reversed(closes))
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1 + avg_gain / avg_loss))


def _calc_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD(fast, slow, signal) 계산.
    closes: [최신→과거] 순서.
    반환: (macd_line, signal_line, histogram) 또는 (None, None, None)
    """
    prices = list(reversed(closes))
    need = slow + signal
    if len(prices) < need:
        return None, None, None

    k_f = 2.0 / (fast + 1)
    k_s = 2.0 / (slow + 1)
    ema_f = sum(prices[:fast]) / fast
    ema_s = sum(prices[:slow]) / slow
    for p in prices[fast:slow]:
        ema_f = p * k_f + ema_f * (1 - k_f)

    macd_hist_vals = []
    for p in prices[slow:]:
        ema_f = p * k_f + ema_f * (1 - k_f)
        ema_s = p * k_s + ema_s * (1 - k_s)
        macd_hist_vals.append(ema_f - ema_s)

    if len(macd_hist_vals) < signal:
        return None, None, None

    k_sig = 2.0 / (signal + 1)
    sig_val = sum(macd_hist_vals[:signal]) / signal
    for m in macd_hist_vals[signal:]:
        sig_val = m * k_sig + sig_val * (1 - k_sig)
    macd_line = macd_hist_vals[-1]
    signal_line = sig_val
    return macd_line, signal_line, macd_line - signal_line


def _calc_indicators(candles: list):
    """
    전체 지표를 한 번에 계산.
    candles: [최신→과거].
    최소 35봉 필요.
    """
    if len(candles) < 35:
        return None

    closes = [c['close'] for c in candles]

    ema20 = _calc_ema(closes, 20)
    ema60 = _calc_ema(closes, min(60, len(closes)))
    rsi14 = _calc_rsi(closes, 14)
    macd_l, macd_s, macd_h = _calc_macd(closes, 12, 26, 9)

    if any(v is None for v in [ema20, ema60, rsi14, macd_l, macd_s]):
        return None

    ma20_slope_pct = 0.0
    if len(candles) >= 25:
        ma20_prev = sum(c['close'] for c in candles[5:25]) / 20
        if ma20_prev > 0:
            ma20_slope_pct = (ema20 - ma20_prev) / ma20_prev * 100

    return {
        "ema20":          ema20,
        "ema60":          ema60,
        "rsi14":          rsi14,
        "macd_line":      macd_l,
        "macd_signal":    macd_s,
        "macd_hist":      macd_h,
        "ma20_slope_pct": ma20_slope_pct,
    }


# ==================================================
# ── 지지/저항 박스 구간 유틸 ──
# ==================================================

def _find_sr_boxes(candles: list,
                   window: int = 2,
                   tolerance: float = 0.020,
                   min_touches: int = 2) -> dict:
    """
    최근 N봉에서 피벗 고/저점 클러스터링으로 지지/저항 박스 자동 탐지.
    candles: [최신→과거]
    반환: {'resistance': [(lo,hi),...], 'support': [(lo,hi),...]}
    """
    highs, lows = [], []
    n = len(candles)
    for i in range(window, n - window):
        h = candles[i]['high']
        l = candles[i]['low']
        if (all(h >= candles[i - j]['high'] for j in range(1, window + 1)) and
                all(h >= candles[i + j]['high'] for j in range(1, window + 1))):
            highs.append(h)
        if (all(l <= candles[i - j]['low'] for j in range(1, window + 1)) and
                all(l <= candles[i + j]['low'] for j in range(1, window + 1))):
            lows.append(l)

    def _cluster(levels: list) -> list:
        if not levels:
            return []
        levels = sorted(levels)
        boxes, group = [], [levels[0]]
        for lv in levels[1:]:
            if (lv - group[0]) / group[0] <= tolerance:
                group.append(lv)
            else:
                if len(group) >= min_touches:
                    boxes.append((min(group), max(group)))
                group = [lv]
        if len(group) >= min_touches:
            boxes.append((min(group), max(group)))
        return boxes

    return {
        'resistance': _cluster(highs),
        'support':    _cluster(lows),
    }


def _near_sr_box(cur_price: float, boxes: dict,
                 side: str = 'support',
                 proximity: float = 0.015) -> tuple:
    """
    현재가가 지지/저항 박스 근처인지 확인.
    반환: (근접여부, 박스하단, 박스상단)
    """
    for lo, hi in boxes.get(side, []):
        mid = (lo + hi) / 2.0
        if abs(cur_price - mid) / mid <= proximity:
            return True, lo, hi
    return False, 0, 0


# ==================================================
# ── 오더블록 유틸 ──
# ==================================================

def _find_orderblock(candles: list,
                     lookback: int = 30,
                     min_impulse_pct: float = 0.015,
                     min_ob_width_pct: float = 0.002) -> list:
    """
    매수 오더블록(Bullish Order Block) 탐지 — ICT/SMC 정통 정의.

    [정의]
    강한 상승 임펄스(급등) 직전의 마지막 음봉 구간.
    기관/세력이 물량을 모은 수요 구간으로, 가격이 되돌아오면 지지 역할.

    [탐지 조건]
    1. 임펄스: 음봉(OB) 이후 연속 2봉 이상 상승 or 단봉 1.5% 이상 급등
    2. OB 구간: 그 임펄스 직전 마지막 음봉의 [몸통 저가 ~ 몸통 고가]
    3. 유효성 필터:
       - 임펄스 고점이 OB 고가 대비 min_impulse_pct 이상 상승
       - OB 폭(ob_high - ob_low) / ob_low >= min_ob_width_pct (너무 좁은 OB 제외)

    [손절선]
    ob_sl = 음봉의 실제 저가(꼬리 포함) → 이 아래로 내려가면 OB 무효

    candles: [최신→과거] 순서
    반환: [{'ob_low', 'ob_high', 'ob_sl', 'age'}, ...]
    """
    orderblocks = []
    n = min(len(candles) - 2, lookback)

    for i in range(1, n):
        # candles[i] = i봉 전 (OB 후보 음봉)
        # candles[i-1] ... = 더 최근 봉들 (임펄스 구간)
        ob_candle = candles[i]

        # ── OB 후보: 반드시 음봉이어야 함 ──
        if ob_candle['close'] >= ob_candle['open']:
            continue

        ob_body_lo = ob_candle['close']   # 음봉: 종가 < 시가
        ob_body_hi = ob_candle['open']
        ob_sl      = ob_candle['low']     # 손절선 = 음봉 실제 저가

        # OB 폭 필터 (너무 좁으면 의미 없음)
        if ob_body_hi <= 0:
            continue
        if (ob_body_hi - ob_body_lo) / ob_body_hi < min_ob_width_pct:
            continue

        # ── 임펄스 감지: OB 이후(더 최신 봉) 강한 상승이 있어야 함 ──
        # candles[i-1], candles[i-2] 등이 OB 직후 봉들
        impulse_found = False
        impulse_high  = 0.0

        # 방법1: 단봉 급등 (1봉만으로 min_impulse_pct 이상 상승)
        if i >= 1:
            next_c = candles[i - 1]
            rise = (next_c['close'] - ob_body_hi) / ob_body_hi if ob_body_hi > 0 else 0
            if rise >= min_impulse_pct:
                impulse_found = True
                impulse_high  = next_c['high']

        # 방법2: 연속 2봉 이상 상승 (각각 양봉)
        if not impulse_found and i >= 2:
            c1 = candles[i - 1]
            c2 = candles[i - 2]
            if (c1['close'] > c1['open'] and        # 직후봉 양봉
                c2['close'] > c2['open'] and        # 2번째봉 양봉
                c2['close'] > ob_body_hi):          # 2봉 만에 OB 고점 초과
                impulse_found = True
                impulse_high  = max(c1['high'], c2['high'])

        if not impulse_found:
            continue

        # 임펄스 고점이 OB 고가 대비 충분히 높은지 확인
        if impulse_high <= 0:
            continue
        if (impulse_high - ob_body_hi) / ob_body_hi < min_impulse_pct:
            continue

        orderblocks.append({
            'ob_low':  ob_body_lo,   # 음봉 몸통 저가 (= 종가)
            'ob_high': ob_body_hi,   # 음봉 몸통 고가 (= 시가)
            'ob_sl':   ob_sl,        # 손절선 = 음봉 실제 저가
            'age':     i,            # 몇 봉 전 OB인지
        })

    return orderblocks


def _near_orderblock(cur_price: float,
                     orderblocks: list,
                     proximity: float = 0.005,
                     max_age: int = 20) -> tuple:
    """
    현재가가 오더블록 구간 내부 or 하단 proximity% 이내인지 확인.

    [판정 기준]
    - in_zone: ob_low <= cur_price <= ob_high (OB 구간 안에 있음)
    - near_below: ob_low * (1 - proximity) <= cur_price < ob_low
                  (OB 하단 아래 proximity% 이내 → 막 이탈했거나 접근 중)

    기존 "중심점 ±1.5%" 방식은 OB를 이미 크게 벗어난 종목도 near로 처리하는 오류가 있었음.
    → OB 위로 올라간 경우(종가 > ob_high) 는 제외: 이미 OB를 돌파해버린 상태

    반환: (근접여부, ob_low, ob_high, ob_sl, age)
    """
    for ob in sorted(orderblocks, key=lambda x: x['age']):   # 최신 OB 우선
        if ob['age'] > max_age:
            continue
        ob_low  = ob['ob_low']
        ob_high = ob['ob_high']
        ob_sl   = ob['ob_sl']

        in_zone    = (ob_low <= cur_price <= ob_high)
        near_below = (ob_low * (1 - proximity) <= cur_price < ob_low)

        if in_zone or near_below:
            return True, ob_low, ob_high, ob_sl, ob['age']

    return False, 0, 0, 0, 0


# ==================================================
# ── BREAKOUT 전용 헬퍼 함수 (신규 추가) ──
# ==================================================

def _check_momentum_candle(candles: list) -> tuple:
    """
    [보고서 핵심] 모멘텀 캔들 확인 — 거짓 돌파 필터

    보고서 원칙:
      "가격이 저항선을 살짝 넘는 것(작은 균열)과
       압도적인 힘으로 부셔버리는 것(진짜 돌파)은 완전히 다르다"

    두 가지 신호 중 하나 이상 충족 시 True:

      TYPE-A: 장대 양봉 (압도적 단일 캔들)
        현재봉 몸통 ≥ 직전 3봉 평균 몸통 × 1.5배
        → "이전 캔들을 압도하는 거대한 몸통을 가진 하나의 장대 양봉"

      TYPE-B: 3연속 양봉 (지속적 전진)
        직전 3봉이 모두 양봉(종가 > 시가)
        → "크기는 작더라도 멈추지 않고 같은 방향으로 전진하는 세 개의 연속된 양봉"

    candles: [최신→과거]
    반환: (통과여부: bool, 사유: str)
    """
    if len(candles) < 4:
        return False, "데이터부족"

    c0 = candles[0]  # 현재(돌파)봉

    # ── TYPE-A: 장대 양봉 ──
    body0 = c0['close'] - c0['open']
    if body0 > 0:
        prev_bodies = [
            abs(candles[i]['close'] - candles[i]['open'])
            for i in range(1, 4)
        ]
        avg_prev_body = sum(prev_bodies) / len(prev_bodies) if prev_bodies else 0
        if avg_prev_body > 0 and body0 >= avg_prev_body * 1.5:
            return True, "TYPE-A(장대양봉)"

    # ── TYPE-B: 3연속 양봉 ──
    # 현재봉 포함 직전 3봉 모두 양봉
    if all(candles[i]['close'] > candles[i]['open'] for i in range(0, 3)):
        return True, "TYPE-B(3연속양봉)"

    return False, "모멘텀없음(거짓돌파의심)"


def _get_breakout_level(candles: list) -> float:
    """
    돌파 기준가(저항선) 추정 — 손절선 계산에 사용.

    우선순위:
      1. SR 박스 저항 상단 중 현재가 바로 아래 가장 가까운 값
      2. 없으면 직전 5봉 고점 사용

    candles: [최신→과거]
    반환: 저항선 가격 (float)
    """
    sr_boxes = _find_sr_boxes(candles)
    c0 = candles[0]

    resist_levels = []
    for lo, hi in sr_boxes.get('resistance', []):
        # 현재가 아래에 있는 저항 박스 상단만 수집
        if hi <= c0['close']:
            resist_levels.append(hi)

    if resist_levels:
        return max(resist_levels)  # 현재가에 가장 가까운 저항선

    # SR 박스 없으면 5봉 고점 사용
    return max(c['high'] for c in candles[1:6])


def calc_breakout_stops(entry_price: float,
                        atr: float,
                        candles: list,
                        logger=None,
                        code=None) -> dict:
    """
    [보고서 기반] BREAKOUT 손절 / TP 계산 — 개선 버전

    ── 손절 로직 (두 기준 중 더 타이트한 값 선택) ──
      option_A = entry_price - ATR × 1.5     (기존 ATR 기반)
      option_B = 저항선(돌파기준가) × 0.997  (저항선 직하단 -0.3%)
      stop_loss = max(option_A, option_B)    ← entry에 더 가까운 값

    보고서 원칙:
      "방금 뚫고 올라온 저항선 바로 아래에 손절선 설정.
       돌파가 진짜라면 저항→지지 전환되어 이 구간을 딛고 올라온다.
       이 지지선마저 깨면 거짓 돌파이므로 미련 없이 손절."

    ── TP 로직 (2단계 분할 익절 시스템) ──
      tp1 = entry_price + ATR × 2.0   (1차: 빠른 절반 익절 → 심리 안정)
      tp2 = entry_price + ATR × 5.0   (2차: 트레일링 스탑 → 추세 끝까지)

    보고서 원칙:
      "1차 익절 후 손절을 진입가 위로 올려 무적의 포지션 구성.
       남은 절반은 추세 끝날 때까지 자유롭게 보유."

    반환 dict:
      stop_loss  (int)   : 손절가
      tp1        (int)   : 1차 목표가 (절반 익절)
      tp2        (int)   : 2차 목표가 (트레일링 위임)
      sl_pct     (float) : 손절폭 %
      rr1        (float) : TP1 손익비
      rr2        (float) : TP2 손익비
      sl_method  (str)   : 'ATR' / 'SR_LEVEL' / 'MIN_GAP'
    """
    # ── 손절 산출 ──
    sl_atr       = entry_price - atr * 1.5
    breakout_lv  = _get_breakout_level(candles)
    sl_sr        = breakout_lv * 0.997      # 저항선 직하단 -0.3%

    stop_loss  = max(sl_atr, sl_sr)         # 더 타이트한(높은) 값
    sl_method  = "SR_LEVEL" if stop_loss == sl_sr else "ATR"

    # 최소 간격 보장: entry 대비 최소 -0.3% 이상 떨어져야 함
    min_sl = entry_price * 0.997
    if stop_loss > min_sl:
        stop_loss = min_sl
        sl_method = "MIN_GAP"

    stop_loss = int(stop_loss)

    # ── TP 산출 ──
    tp1 = int(entry_price + atr * 2.0)   # 1차: ATR×2 (기존 ×3 → 당김)
    tp2 = int(entry_price + atr * 5.0)   # 2차: ATR×5 (트레일링)

    sl_amt = entry_price - stop_loss
    sl_pct = (stop_loss - entry_price) / entry_price * 100
    rr1    = (tp1 - entry_price) / sl_amt if sl_amt > 0 else 0
    rr2    = (tp2 - entry_price) / sl_amt if sl_amt > 0 else 0

    if logger:
        logger.info(
            f"[BREAKOUT_STOPS] {code} "
            f"매수가={entry_price:,} ATR={atr:.1f} 돌파기준가={breakout_lv:,} | "
            f"손절={stop_loss:,}({sl_pct:.2f}%, {sl_method}) "
            f"[ATR기준={sl_atr:.0f} SR기준={sl_sr:.0f}] | "
            f"TP1={tp1:,}(RR={rr1:.1f}x) TP2={tp2:,}(RR={rr2:.1f}x)"
        )

    return {
        'stop_loss': stop_loss,
        'tp1':       tp1,
        'tp2':       tp2,
        'sl_pct':    sl_pct,
        'rr1':       rr1,
        'rr2':       rr2,
        'sl_method': sl_method,
    }


def get_breakout_position_size(entry_price: float,
                               stop_loss: float,
                               account_balance: float,
                               risk_pct: float = 0.01) -> int:
    """
    [신규] 손절 기준 포지션 사이징.

    1회 최대 손실 = 계좌잔고 × risk_pct (기본 1%)
    수량 = 최대손실금액 ÷ 1주당 손절금액

    예) 계좌 20,000,000원 / 매수가 5,000원 / 손절 4,900원
        → 최대손실 = 200,000원
        → 수량 = 200,000 ÷ 100 = 2,000주

    반환: 수량 (int, 최소 1)
    """
    sl_per_share = entry_price - stop_loss
    if sl_per_share <= 0:
        return 0
    max_loss = account_balance * risk_pct
    qty      = int(max_loss / sl_per_share)
    return max(qty, 1)


# ==================================================
# 전략 1: BREAKOUT (돌파 진입) VER5 — 모멘텀 필터 추가
# ==================================================

def is_entry_candidate_VER2(candles, logger=None, code=None, strict=False) -> bool:
    """
    급등 모멘텀 진입 전략 (BREAKOUT) VER8

    [VER8 변경사항 — 04-13/14 데이터 분석 기반 지표 재설계]

    핵심 문제: 기존 지표들이 "지금 강한 봉"은 잡지만 "이후에도 오를 봉"을 구분 못함
      → NEVER_ROSE(진입 즉시 하락) 47%, 승리 평균 +0.26% vs 손실 평균 -2.62%

    삭제:
      - 캔들강도 조건 (노이즈 — 세력 처분 봉도 강도 높음)
      - 신호봉 진행률 조건 (캔들강도와 중복, 실효성 낮음)

    핵심 변경:
      1. 3연속 양봉(TYPE-B) 필수 — 메인 조건으로 승격
         → 단일 급등봉은 세력 처분 가능, 연속 양봉은 지속 매수세 증거
         → OR 조건(TYPE-A or TYPE-B) → AND 조건(TYPE-B 필수)
      2. 급등 발생 후 경과시간 ≤ 10분 (신호봉 기준으로 근사)
         → 첫 급등봉 이후 10봉 이내에 진입해야 함
         → 이미 30분 지난 종목 = 에너지 소진 가능성
      3. 당일 상승 상한 15%로 축소 (20% → 15%)
         → 코위버(+6.9% EMA이격), 하이스틸 등 이미 너무 오른 종목 차단 강화
      4. EMA20 이격 상한 5% 유지

    유지:
      - 거래량 5배+, ≥ 3000주
      - 당일상승 5~15% (하한 5% 유지)
      - 5봉 신고가 돌파
      - 종가 > EMA20
      - 시각 ≤ 10:30
    """
    from datetime import datetime as _dt

    if len(candles) < 20:
        if logger:
            logger.info(f"[BREAKOUT_SKIP] {code} 데이터 부족(필요:20 현재:{len(candles)})")
        return False

    # ── 진입 시각 하드컷: 10:30 ──────────────────────────────────
    _now = _dt.now().time()
    if _now > time(10, 30):
        if logger:
            logger.info(
                f"[BREAKOUT_SKIP] {code} "
                f"진입시각({_now.strftime('%H:%M')}) > 10:30 하드컷"
            )
        return False

    c0 = candles[0]
    body = c0['close'] - c0['open']

    # A. 양봉
    is_bull = body > 0

    # B. 거래량 급증: 직전 5봉 평균 대비 5배+ AND ≥ 3000주
    avg_vol5  = sum(c['volume'] for c in candles[1:6]) / 5 if len(candles) >= 6 else 0
    vol_ratio = c0['volume'] / avg_vol5 if avg_vol5 > 0 else 0
    vol_ok    = vol_ratio >= 5.0 and c0['volume'] >= 3000

    # C. 5봉 신고가 돌파
    prev5_high = max(c['high'] for c in candles[1:6]) if len(candles) >= 6 else 0
    price_ok   = c0['high'] >= prev5_high

    # D. 당일 누적 상승률 5~15% (상한 20→15% 축소)
    day_open  = candles[-1]['open'] if candles else c0['open']
    day_rise  = (c0['close'] - day_open) / day_open * 100 if day_open > 0 else 0
    surge_ok  = 5.0 <= day_rise <= 15.0

    # E. EMA20 이격 ≤ 5% (추격매수 차단)
    closes    = [c['close'] for c in candles]
    ema20     = _calc_ema(closes, 20)
    ema_ok    = (ema20 is not None) and (c0['close'] > ema20)
    ema_gap   = (c0['close'] - ema20) / ema20 * 100 if ema20 else 0
    ema_gap_ok = ema_gap <= 5.0

    # F. [핵심 신규] 3연속 양봉 필수 — 지속 매수세 증거
    #    candles[0]=현재봉, candles[1]=직전봉, candles[2]=2봉전
    #    세 봉 모두 양봉(종가 > 시가)이어야 함
    if len(candles) >= 3:
        consec_bull = all(candles[i]['close'] > candles[i]['open'] for i in range(3))
    else:
        consec_bull = False

    # G. [핵심 신규] 급등 경과시간 ≤ 10봉 이내
    #    현재봉 기준으로 직전 10봉 중 거래량이 avg_vol5의 3배 이상인 첫 급등봉 탐색
    #    급등봉이 10봉 이전이면 이미 에너지 소진 가능성
    surge_candle_age = 999
    for i in range(1, min(20, len(candles))):
        c = candles[i]
        avg_before = sum(candles[j]['volume'] for j in range(i+1, min(i+6, len(candles)))) / 5
        if avg_before > 0 and c['volume'] / avg_before >= 3.0:
            surge_candle_age = i
            break
    fresh_ok = surge_candle_age <= 10

    is_valid = (is_bull and vol_ok and price_ok and surge_ok and
                ema_ok and ema_gap_ok and consec_bull and fresh_ok)

    if logger:
        rng     = c0['high'] - c0['low']
        str_pct = body / rng * 100 if rng > 0 else 0
        if is_valid:
            logger.info(
                f"[BREAKOUT_CONFIRMED] {code} | "
                f"현재가:{c0['close']} EMA20:{ema20:.0f} EMA이격:{ema_gap:.1f}% | "
                f"거래량:{vol_ratio:.1f}배({c0['volume']:,}주) | "
                f"5봉고점:{prev5_high}(돌파={price_ok}) | "
                f"당일상승:{day_rise:.1f}% | "
                f"3연속양봉:{consec_bull} | "
                f"급등경과:{surge_candle_age}봉"
            )
        else:
            logger.info(
                f"[BREAKOUT_CHECK] {code} "
                f"bull={is_bull} "
                f"vol={vol_ok}({vol_ratio:.1f}배≥5) "
                f"price={price_ok}(5봉고={prev5_high}) "
                f"surge={surge_ok}({day_rise:.1f}%∈[5,15]%) "
                f"ema={ema_ok}({c0['close']}>{(ema20 or 0):.0f}) "
                f"ema_gap={ema_gap_ok}({ema_gap:.1f}%≤5%) "
                f"3연속양봉={consec_bull} "
                f"급등경과={fresh_ok}({surge_candle_age}봉≤10)"
            )

    return is_valid


def get_entry_signal_data(candles) -> dict | None:
    """BREAKOUT 진입 시 디스코드 알림용 지표"""
    if len(candles) < 35:
        return None
    ind = _calc_indicators(candles)
    if ind is None:
        return None
    c1            = candles[0]
    prev_5_high   = max(c['high'] for c in candles[1:6])
    _price_strong = (c1['close'] > prev_5_high)
    _price_near   = (c1['close'] >= prev_5_high * 0.99 and c1['high'] > prev_5_high)
    avg_vol       = sum(c['volume'] for c in candles[1:6]) / 5
    vol_ratio     = c1['volume'] / avg_vol if avg_vol > 0 else 0
    rng           = c1['high'] - c1['low']
    body          = c1['close'] - c1['open']
    strength      = (body / rng * 100) if rng > 0 else 0
    _, momentum_type = _check_momentum_candle(candles)
    return {
        "vol_ratio":       vol_ratio,
        "trend":           f"EMA정배열 기울기{ind['ma20_slope_pct']:+.2f}%",
        "breakout":        f"5봉고점({prev_5_high}) {'돌파' if _price_strong else '근접'}",
        "candle_strength": strength,
        "rsi":             ind["rsi14"],
        "macd":            f"{ind['macd_line']:.2f}>{ind['macd_signal']:.2f}",
        "momentum":        momentum_type,
    }


# ==================================================
# 전략 2: PULLBACK (눌림목 반등 + 오더블록 통합) VER5
# ==================================================

def is_pullback_entry(candles, logger=None, code=None) -> bool:
    """
    눌림목 진입 전략 VER8 — RSI/MACD 제거 + 실질 조건으로 재설계

    [VER8 변경사항 — 04-13/14 데이터 분석 기반]

    핵심 문제:
      - RSI/MACD: 1분봉에서 노이즈, 모두 통과해도 손절 반복
      - "눌림" 구분 불가: 진짜 눌림(세력 재매수 준비) vs 하락 추세 중 반등 구분 못함

    삭제:
      - RSI 조건 (1분봉 단타에서 무의미)
      - MACD 조건 (동일)
      - EMA20 근접 조건 (갭상승 종목은 EMA에서 멀어도 됨)
      - 오더블록 조건 (탐지 정확도 낮음)

    핵심 추가:
      1. 갭상승 종목 필수: 당일 시초가 > 전일 종가 × 1.02 (갭 +2% 이상)
         → 갭 없는 장중 급등 = 세력 처분 가능성 높음
         → 갭상승 후 눌림 = 세력이 올려놓고 재매수 준비 가능성
      2. 눌림 구간 거래량 감소 확인: 눌림 3~5봉 평균 < 급등봉 거래량 × 50%
         → 거래량 감소 = 매도 에너지 약해짐 = 진짜 눌림
         → 거래량 그대로면 지속 매도 = 하락 추세
      3. 반등봉 거래량 급증: 현재봉 거래량 > 눌림 평균 × 2배
         → 거래량 동반 반등 = 세력 재진입 신호

    유지:
      - EMA 정배열 (EMA20 > EMA60)
      - 당일 고점 대비 눌림 -8~-3%
      - 당일 상승 ≥ 3%
      - 반등 양봉 + 캔들강도 ≥ 55%
      - 가짜신호 필터 F1~F3
      - 시각 ≤ 11:00
    """
    from datetime import datetime as _dt

    if len(candles) < 35:
        if logger:
            logger.info(f"[PULLBACK_SKIP] {code} 데이터 부족(필요:35 현재:{len(candles)})")
        return False

    # ── 진입 시각 하드컷: 11:00 ──────────────────────────────────
    _now = _dt.now().time()
    if _now > time(11, 0):
        if logger:
            logger.info(
                f"[PULLBACK_SKIP] {code} "
                f"진입시각({_now.strftime('%H:%M')}) > 11:00 하드컷"
            )
        return False

    c1 = candles[0]
    c2 = candles[1]

    # EMA 계산 (RSI/MACD 제거 — _calc_indicators 대신 직접 계산)
    closes = [c['close'] for c in candles]
    ema20  = _calc_ema(closes, 20)
    ema60  = _calc_ema(closes, min(60, len(closes)))
    if ema20 is None or ema60 is None:
        if logger:
            logger.info(f"[PULLBACK_SKIP] {code} EMA 계산 실패")
        return False

    # A. EMA 정배열
    ema_aligned = (ema20 > ema60)

    # B. [핵심 신규] 갭상승 종목 필수: 시초가 > 전일 종가 × 1.02
    #    candles[-1] = 가장 오래된 봉(당일 첫 봉), open = 시초가
    #    전일 종가는 당일 시초가 바로 직전 봉의 종가로 근사
    #    (정확한 전일 종가가 없으면 candles[-1]의 open을 기준으로)
    day_open   = candles[-1]['open']
    # 전일 종가 근사: 시초가 봉의 open 사용 (갭은 open 자체에 반영됨)
    # 더 정확히 하려면 kiwoom_api에서 prev_close를 넘겨야 하지만
    # 일단 당일 시초가가 EMA20 대비 +2% 이상이면 갭상승으로 간주
    gap_ok = (day_open > ema20 * 1.02)

    # C. 당일 상승 ≥ 3% (갭 포함)
    day_rise_pct = (c1['close'] - day_open) / day_open * 100 if day_open > 0 else 0
    day_rise_ok  = day_rise_pct >= 3.0

    # D. 당일 고점 대비 눌림 -8%~-3%
    day_high        = max(c['high'] for c in candles)
    day_pullback    = (c1['close'] - day_high) / day_high * 100
    day_pullback_ok = -8.0 <= day_pullback <= -3.0

    # E. 반등 양봉
    bounce_ok = (c1['close'] > c2['close'] and c1['close'] > c1['open'])

    # F. 캔들강도 ≥ 55%
    rng     = c1['high'] - c1['low']
    body    = c1['close'] - c1['open']
    str_pct = (body / rng) if rng > 0 else 0
    str_ok  = str_pct >= 0.55

    # G. [핵심 신규] 눌림 구간 거래량 감소 확인
    #    급등봉 탐색: 직전 20봉 중 거래량이 가장 많은 봉
    #    눌림 구간: 급등봉 이후 ~ 현재봉 직전
    surge_idx  = 1
    max_vol    = 0
    for i in range(1, min(20, len(candles))):
        if candles[i]['volume'] > max_vol:
            max_vol   = candles[i]['volume']
            surge_idx = i
    surge_vol = candles[surge_idx]['volume']

    # 눌림 구간: 급등봉(surge_idx) 이전 ~ 현재봉 직전 (candles[1:surge_idx])
    pullback_candles = candles[1:surge_idx] if surge_idx > 1 else []
    if pullback_candles:
        avg_pull_vol = sum(c['volume'] for c in pullback_candles) / len(pullback_candles)
    else:
        avg_pull_vol = surge_vol  # 눌림 구간 없으면 조건 통과 불가하게

    vol_decrease_ok = avg_pull_vol < surge_vol * 0.5  # 눌림 거래량 < 급등봉의 50%

    # H. [핵심 신규] 반등봉 거래량 급증: 현재봉 > 눌림 평균 × 2배
    bounce_vol_ok = (c1['volume'] >= avg_pull_vol * 2.0 and c1['volume'] >= 3000)

    # I. 가짜신호 필터
    rec5      = candles[0:5]
    bear_cnt  = sum(1 for c in rec5 if c['close'] < c['open'])
    cls5      = [c['close'] for c in rec5]
    mono_down = all(cls5[i] <= cls5[i+1] for i in range(len(cls5)-1))
    f1 = (bear_cnt >= 3 and mono_down)
    f2 = (rng > 0 and (c1['high'] - c1['close']) > body * 2)
    f3 = (min(c['low'] for c in candles[0:5]) < ema20 * 0.95)
    filters_ok = not f1 and not f2 and not f3

    # 저항 박스 근처 진입 차단
    sr_boxes = _find_sr_boxes(candles)
    near_resist, _, _ = _near_sr_box(c1['close'], sr_boxes, 'resistance', proximity=0.010)
    sr_ok = not near_resist

    is_valid = (ema_aligned and gap_ok and day_rise_ok and day_pullback_ok and
                bounce_ok and str_ok and vol_decrease_ok and bounce_vol_ok and
                filters_ok and sr_ok)

    if logger:
        if is_valid:
            logger.info(
                f"[PULLBACK_CONFIRMED] {code} | "
                f"종가:{c1['close']} EMA20:{ema20:.0f} EMA60:{ema60:.0f} | "
                f"갭상승:{gap_ok}(시초가{day_open}≥EMA20×1.02={ema20*1.02:.0f}) | "
                f"당일상승:{day_rise_pct:.1f}% | "
                f"당일고점대비:{day_pullback:.1f}% | "
                f"급등봉거래량:{surge_vol:,}(인덱스:{surge_idx}) | "
                f"눌림평균거래량:{avg_pull_vol:.0f} | "
                f"반등거래량:{c1['volume']:,}({c1['volume']/avg_pull_vol:.1f}배) | "
                f"캔들강도:{str_pct*100:.0f}%"
            )
        else:
            logger.info(
                f"[PULLBACK_CHECK] {code} "
                f"ema={ema_aligned}(ema20={ema20:.0f},ema60={ema60:.0f}) "
                f"gap={gap_ok}(시초가{day_open}≥EMA×1.02={ema20*1.02:.0f}) "
                f"day_rise={day_rise_ok}({day_rise_pct:.1f}%≥3) "
                f"day_pullback={day_pullback_ok}({day_pullback:.1f}%∈[-8,-3]) "
                f"bounce={bounce_ok} "
                f"strength={str_ok}({str_pct*100:.0f}%≥55) "
                f"vol_decrease={vol_decrease_ok}(눌림평균{avg_pull_vol:.0f}<급등{surge_vol}×50%) "
                f"bounce_vol={bounce_vol_ok}({c1['volume']:,}≥눌림평균×2={avg_pull_vol*2:.0f}) "
                f"f1={f1} f2={f2} f3={f3} sr_ok={sr_ok}"
            )

    return is_valid


def get_pullback_signal_data(candles) -> dict | None:
    """PULLBACK 진입 시 디스코드 알림용 지표 (오더블록 정보 포함)"""
    if len(candles) < 35:
        return None
    ind = _calc_indicators(candles)
    if ind is None:
        return None

    c1    = candles[0]
    ema20 = ind["ema20"]

    recent_high  = max(c['high'] for c in candles[0:10])
    pullback_pct = (c1['close'] - recent_high) / recent_high
    ma_distance  = abs(c1['close'] - ema20) / ema20
    ma_strength  = "STRONG" if ma_distance <= 0.020 else "NORMAL"
    pull_vols    = [c['volume'] for c in candles[1:5]]
    avg_vol      = sum(pull_vols) / len(pull_vols) if pull_vols else 1
    vol_ratio    = c1['volume'] / avg_vol if avg_vol > 0 else 0
    rng          = c1['high'] - c1['low']
    body         = c1['close'] - c1['open']
    strength     = (body / rng * 100) if rng > 0 else 0

    result = {
        "vol_ratio":       vol_ratio,
        "trend":           f"EMA정배열 리테스트({pullback_pct*100:.1f}%)",
        "breakout":        f"지지 확인({ma_strength})",
        "candle_strength": strength,
        "rsi":             ind["rsi14"],
        "macd":            f"{ind['macd_line']:.2f}",
        "ma_strength":     ma_strength,
    }

    orderblocks = _find_orderblock(candles, lookback=30)
    ob_near, ob_low, ob_high, ob_sl, ob_age = _near_orderblock(
        c1['close'], orderblocks, proximity=0.005, max_age=20
    )
    if ob_near:
        result["orderblock"] = f"OB지지({ob_low:,}~{ob_high:,}, {ob_age}봉전)"
        result["ob_sl"]      = ob_sl

    return result


# ==================================================
# 전략 3: FLAG (깃발 패턴) — 변경 없음
# ==================================================

def is_flag_entry(candles, logger=None, code=None) -> bool:
    """
    깃발 패턴 진입 전략

    [조건 요약]
    1. EMA 정배열: EMA20 > EMA60 (대세 상승 확인)
    2. 기준봉: 양봉 + 상승폭 ≥ 1.0% + 캔들강도 ≥ 60% + 거래량 3배↑
    3. 횡보 구간 (3~15봉): 고저 범위 ≤ 기준봉 몸통 60% + 거래량 수렴 ≤ 60%
    4. 재돌파봉: 박스 상단 돌파 + 양봉 + 거래량 2배↑
       ※ BREAKOUT 필터(ENTRY_CHECK) 미적용 — FLAG는 패턴 자체가 진입 신호

    손절: 기준봉 시가
    """
    MIN_FLAG = 3
    MAX_FLAG = 15
    need = MAX_FLAG + 25

    if len(candles) < need:
        if logger:
            logger.info(f"[FLAG_SKIP] {code} 데이터 부족(필요:{need} 현재:{len(candles)})")
        return False

    closes = [c['close'] for c in candles]
    ema20  = _calc_ema(closes, 20)
    ema60  = _calc_ema(closes, min(60, len(closes)))
    if ema20 is None or ema60 is None or ema20 <= ema60:
        if logger:
            logger.info(
                f"[FLAG_CHECK] {code} "
                f"ema_align=False(ema20={ema20 or 0:.0f},ema60={ema60 or 0:.0f})"
            )
        return False

    c0 = candles[0]

    for flag_len in range(MIN_FLAG, MAX_FLAG + 1):
        if 1 + flag_len + 5 >= len(candles):
            break

        flag_candles = candles[1 : 1 + flag_len]
        base         = candles[1 + flag_len]
        prev_base    = candles[1 + flag_len + 1 : 1 + flag_len + 6]

        base_body  = base['close'] - base['open']
        base_range = base['high'] - base['low']
        base_rise  = base_body / base['open'] if base['open'] > 0 else 0
        base_str   = base_body / base_range if base_range > 0 else 0

        avg_vol_before = (sum(c['volume'] for c in prev_base) / len(prev_base)
                          if prev_base else 1)
        base_vol_ratio = base['volume'] / avg_vol_before if avg_vol_before > 0 else 0

        is_base = (
            base_body      > 0      and
            base_rise      >= 0.010 and
            base_str       >= 0.60  and
            base_vol_ratio >= 3.0
        )
        if not is_base:
            continue

        flag_highs = [c['high']   for c in flag_candles]
        flag_lows  = [c['low']    for c in flag_candles]
        flag_vols  = [c['volume'] for c in flag_candles]

        box_range        = max(flag_highs) - min(flag_lows)
        flag_range_ratio = box_range / base_body if base_body > 0 else 999
        avg_flag_vol     = sum(flag_vols) / len(flag_vols) if flag_vols else 1
        vol_shrink       = avg_flag_vol / base['volume']

        is_flag = (flag_range_ratio <= 0.60 and vol_shrink <= 0.60)
        if not is_flag:
            continue

        box_top    = max(flag_highs)
        rebreak_ok = (
            c0['close'] > box_top        and
            c0['close'] > c0['open']     and
            c0['volume'] >= avg_flag_vol * 2.0
        )
        if not rebreak_ok:
            continue

        # ⭐ FLAG 전용 추가 필터: 재돌파봉 캔들강도 ≥ 50% (너무 약한 봉 제거)
        c0_rng  = c0['high'] - c0['low']
        c0_body = c0['close'] - c0['open']
        c0_str  = c0_body / c0_rng if c0_rng > 0 else 0
        if c0_str < 0.50:
            if logger:
                logger.info(
                    f"[FLAG_CHECK] {code} "
                    f"flag_len={flag_len} 재돌파 감지됐으나 "
                    f"캔들강도({c0_str*100:.0f}%<50%) 불충분 → 스킵"
                )
            continue

        sr_boxes = _find_sr_boxes(candles)
        _, _, r_hi = _near_sr_box(c0['close'], sr_boxes, 'resistance', proximity=0.010)
        if r_hi and c0['close'] < r_hi:
            if logger:
                logger.info(
                    f"[FLAG_CHECK] {code} "
                    f"flag_len={flag_len} 재돌파 감지됐으나 저항박스({r_hi}) 미돌파 → 스킵"
                )
            continue

        if logger:
            logger.info(
                f"[FLAG_CONFIRMED] {code} | "
                f"기준봉:{base['open']}→{base['close']}(+{base_rise*100:.1f}%, "
                f"거래량{base_vol_ratio:.1f}배) | "
                f"횡보:{flag_len}봉(범위비율{flag_range_ratio:.2f}, "
                f"거래량수렴{vol_shrink:.2f}) | "
                f"재돌파:{c0['close']}(박스상단{box_top}, "
                f"거래량{c0['volume']/avg_flag_vol:.1f}배) | "
                f"EMA20:{ema20:.0f}>EMA60:{ema60:.0f} | "
                f"손절기준봉시가:{base['open']}"
            )
        return True

    if logger:
        logger.info(
            f"[FLAG_CHECK] {code} "
            f"flag_len=3~{MAX_FLAG} 탐색 완료 → 패턴 미감지"
        )
    return False


def get_flag_signal_data(candles) -> dict | None:
    """FLAG 진입 시 디스코드 알림용 지표"""
    MIN_FLAG, MAX_FLAG = 3, 15
    need = MAX_FLAG + 25
    if len(candles) < need:
        return None

    closes = [c['close'] for c in candles]
    ema20  = _calc_ema(closes, 20)
    c0     = candles[0]

    for flag_len in range(MIN_FLAG, MAX_FLAG + 1):
        if 1 + flag_len + 5 >= len(candles):
            break
        flag_candles = candles[1 : 1 + flag_len]
        base         = candles[1 + flag_len]
        prev_base    = candles[1 + flag_len + 1 : 1 + flag_len + 6]

        base_body = base['close'] - base['open']
        if base_body <= 0:
            continue
        base_rise      = base_body / base['open'] if base['open'] > 0 else 0
        avg_vol_before = (sum(c['volume'] for c in prev_base) / len(prev_base)
                          if prev_base else 1)
        base_vol_ratio = base['volume'] / avg_vol_before if avg_vol_before > 0 else 0
        if base_vol_ratio < 3.0 or base_rise < 0.01:
            continue

        flag_highs   = [c['high']   for c in flag_candles]
        flag_vols    = [c['volume'] for c in flag_candles]
        avg_flag_vol = sum(flag_vols) / len(flag_vols) if flag_vols else 1
        box_top      = max(flag_highs)

        if c0['close'] > box_top and c0['volume'] >= avg_flag_vol * 2.0:
            return {
                "vol_ratio":       c0['volume'] / avg_flag_vol,
                "trend":           f"깃발 재돌파(기준봉+{base_rise*100:.1f}%)",
                "breakout":        f"박스상단({box_top}) 돌파",
                "candle_strength": base_body / (base['high'] - base['low']) * 100
                                   if (base['high'] - base['low']) > 0 else 0,
                "flag_len":        flag_len,
                "base_stop":       base['open'],
            }
    return None


# ==================================================
# ── 레거시 함수 (kiwoom_api.py import 호환 유지) ──
# ==================================================

def is_entry_candidate_1min(candles, logger=None, code=None) -> bool:
    """레거시 — 미사용"""
    return False


def is_entry_candidate(candles, logger=None, code=None) -> bool:
    """레거시 — 미사용"""
    return False
