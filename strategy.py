"""
strategy.py — BREAKOUT 손절/진입 개선 (2026-03-03 보고서 기반)

[v4 변경사항 — BREAKOUT 전략 집중 개선]

문제 진단 (26_03_03 보고서):
  - 손절 11건 / 18건 (61%) → 총손실 -457,915원
  - 0분~1분 즉시손절 다수: 에이치엠넥스(9초), 국전약품(51초), 한일사료(14초)
    → 거짓 돌파에 조급 진입한 결과
  - TP1 도달률: 18건 중 2건 (11%) → TP 목표가 너무 멀어서 수익 실현 실패
  - 평균 RR 설계 2.2x이나 실제 승률 22% → 손익비 미충족

개선 내용:
  1. _check_momentum_candle() [신규]
     - 진입 전 "진짜 돌파"를 캔들로 확인 (거짓 돌파 필터)
     - TYPE-A: 장대 양봉 — 현재봉 몸통 ≥ 직전 3봉 평균 몸통 × 1.5배
     - TYPE-B: 3연속 양봉 — 직전 3봉 모두 양봉
     - 둘 중 하나 충족 시 통과 / 미충족 시 진입 차단

  2. is_entry_candidate_VER2() 수정
     - 모멘텀 캔들 필터(I) 추가 → 즉시손절 케이스 차단
     - 캔들강도 기준: 60% → 65% 강화 (조급 진입 방지)

  3. calc_breakout_stops() [신규]
     - 손절: ATR×1.5 vs 저항선 직하단(-0.3%) 중 더 타이트한 값 선택
       → 목표: 평균 손절폭 -1.67% → -1.2% 이하
     - TP1: ATR×3.0 → ATR×2.0 (빠른 절반 익절 → TP 도달률 향상)
     - TP2: ATR×5.0 (트레일링 스탑 위임 → 추세 끝까지 보유)
     - 2단계 분할 익절 시스템 (안정 확보 + 수익 극대화 동시)

  4. get_breakout_position_size() [신규]
     - 계좌 1% 손실 한도 기반 수량 산출
     - 손절폭에 따라 수량 자동 조정

PULLBACK / FLAG: 변경 없음
"""

from datetime import datetime, time

MIN_VOL_RATIO = 2.0
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
                     lookback: int = 20,
                     min_body_ratio: float = 0.40) -> list:
    """
    매수 오더블록 자동 탐지.
    정의: 음봉 몸통이 직전 양봉 몸통 전체를 감싸는 구간 = 매수 오더블록(지지구간)
    candles: [최신→과거]
    반환: [{'ob_low', 'ob_high', 'age', 'engulf_candle_low'}, ...]
    """
    orderblocks = []
    n = min(len(candles) - 1, lookback)

    for i in range(1, n):
        curr = candles[i]
        prev = candles[i + 1]

        prev_body_lo = min(prev['open'], prev['close'])
        prev_body_hi = max(prev['open'], prev['close'])
        if prev['close'] <= prev['open']:
            continue

        curr_body_lo = min(curr['open'], curr['close'])
        curr_body_hi = max(curr['open'], curr['close'])
        if curr['close'] >= curr['open']:
            continue

        if not (curr_body_lo <= prev_body_lo and curr_body_hi >= prev_body_hi):
            continue

        curr_range = curr['high'] - curr['low']
        curr_body  = curr_body_hi - curr_body_lo
        if curr_range > 0 and (curr_body / curr_range) < min_body_ratio:
            continue

        if (prev_body_hi - prev_body_lo) <= 0:
            continue

        orderblocks.append({
            'ob_low':            prev_body_lo,
            'ob_high':           prev_body_hi,
            'age':               i,
            'engulf_candle_low': curr['low'],
        })

    return orderblocks


def _near_orderblock(cur_price: float,
                     orderblocks: list,
                     proximity: float = 0.015,
                     max_age: int = 15) -> tuple:
    """
    현재가가 오더블록 구간 내부 또는 ±proximity% 이내인지 확인.
    반환: (근접여부, ob_low, ob_high, 손절기준가, age)
    """
    for ob in orderblocks:
        if ob['age'] > max_age:
            continue
        ob_low  = ob['ob_low']
        ob_high = ob['ob_high']
        mid     = (ob_low + ob_high) / 2.0
        in_zone = (ob_low <= cur_price <= ob_high)
        near    = (abs(cur_price - mid) / mid <= proximity)
        if in_zone or near:
            return True, ob_low, ob_high, ob['engulf_candle_low'], ob['age']
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

def is_entry_candidate_VER2(candles, logger=None, code=None) -> bool:
    """
    돌파 진입 전략 VER5 — 모멘텀 캔들 필터 추가 (보고서 기반)

    [VER5 변경사항]
      + 조건 I: 모멘텀 캔들 확인 (_check_momentum_candle)
                TYPE-A 장대 양봉 또는 TYPE-B 3연속 양봉 중 하나 충족 필수
                → 즉시 손절 케이스(0~1분) 원천 차단
      ± 조건 G: 캔들강도 60% → 65% 강화

    [기존 조건 유지]
      A. EMA 정배열: 종가 > EMA20 > EMA60
      B. RSI14 ≥ 55
      C. MACD > 시그널
      D. MA20 기울기 ≥ 0.3%
      E. 5봉 고점 돌파 + 양봉
      F. 거래량 2~15배 (최소 5,000주)
      G. 캔들강도 ≥ 65% (강화)
      H. SR 박스 저항 돌파 확인
      I. 모멘텀 캔들 확인 (신규)
    """
    if len(candles) < 35:
        if logger:
            logger.info(f"[STRATEGY_SKIP] {code} 데이터 부족(필요:35 현재:{len(candles)})")
        return False

    ind = _calc_indicators(candles)
    if ind is None:
        if logger:
            logger.info(f"[STRATEGY_SKIP] {code} 지표 계산 실패")
        return False

    c1 = candles[0]

    ema20  = ind["ema20"]
    ema60  = ind["ema60"]
    rsi14  = ind["rsi14"]
    macd_l = ind["macd_line"]
    macd_s = ind["macd_signal"]
    slope  = ind["ma20_slope_pct"]

    # A. EMA 정배열
    ema_aligned = (c1['close'] > ema20 > ema60)

    # B. RSI
    rsi_ok = rsi14 >= 55

    # C. MACD
    macd_ok = macd_l > macd_s

    # D. MA20 기울기
    trend_ok = slope >= 0.3

    # E. 5봉 고점 돌파
    prev_5_high = max(c['high'] for c in candles[1:6])
    price_ok    = (c1['close'] > prev_5_high and c1['close'] > c1['open'])

    # F. 거래량
    avg_vol   = sum(c['volume'] for c in candles[1:6]) / 5
    vol_ratio = c1['volume'] / avg_vol if avg_vol > 0 else 0
    vol_ok    = (MIN_VOL_RATIO <= vol_ratio <= MAX_VOL_RATIO and c1['volume'] >= 5000)

    # G. 캔들강도 ≥ 65% (기존 60% → 강화)
    rng    = c1['high'] - c1['low']
    body   = c1['close'] - c1['open']
    str_ok = (body / rng) >= 0.65 if rng > 0 else False

    # H. SR 박스 저항 돌파
    sr_boxes       = _find_sr_boxes(candles)
    near_resist, r_lo, r_hi = _near_sr_box(
        c1['close'], sr_boxes, 'resistance', proximity=0.025
    )
    sr_breakout_ok = (not near_resist) or (c1['close'] > r_hi)

    # I. 모멘텀 캔들 확인 (신규)
    momentum_ok, momentum_type = _check_momentum_candle(candles)

    is_valid = (ema_aligned and rsi_ok and macd_ok and trend_ok and
                price_ok and vol_ok and str_ok and
                sr_breakout_ok and momentum_ok)

    if logger:
        str_pct = body / rng * 100 if rng > 0 else 0
        if is_valid:
            logger.info(
                f"[ENTRY_CONFIRMED] {code} | "
                f"종가:{c1['close']} EMA20:{ema20:.0f} EMA60:{ema60:.0f} | "
                f"RSI:{rsi14:.1f} MACD:{macd_l:.2f}>{macd_s:.2f} | "
                f"기울기:{slope:.2f}% | "
                f"5봉고점돌파:{prev_5_high} | "
                f"거래량:{c1['volume']}(평균의 {vol_ratio:.1f}배) | "
                f"SR저항돌파:{sr_breakout_ok}(저항박스:{r_lo}~{r_hi}) | "
                f"모멘텀:{momentum_type}"
            )
        else:
            logger.info(
                f"[ENTRY_CHECK] {code} "
                f"ema={ema_aligned}({ema20:.0f}>{ema60:.0f}) "
                f"rsi={rsi_ok}({rsi14:.1f}) "
                f"macd={macd_ok}({macd_l:.2f}vs{macd_s:.2f}) "
                f"trend={trend_ok}(slope={slope:.2f}%) "
                f"price={price_ok}(5봉고점={prev_5_high}) "
                f"vol={vol_ok}({vol_ratio:.1f}배) "
                f"strength={str_ok}({str_pct:.0f}%≥65%) "
                f"sr_break={sr_breakout_ok} "
                f"momentum={momentum_ok}({momentum_type})"
            )

    return is_valid


def get_entry_signal_data(candles) -> dict | None:
    """BREAKOUT 진입 시 디스코드 알림용 지표"""
    if len(candles) < 35:
        return None
    ind = _calc_indicators(candles)
    if ind is None:
        return None
    c1          = candles[0]
    prev_5_high = max(c['high'] for c in candles[1:6])
    avg_vol     = sum(c['volume'] for c in candles[1:6]) / 5
    vol_ratio   = c1['volume'] / avg_vol if avg_vol > 0 else 0
    rng         = c1['high'] - c1['low']
    body        = c1['close'] - c1['open']
    strength    = (body / rng * 100) if rng > 0 else 0
    _, momentum_type = _check_momentum_candle(candles)
    return {
        "vol_ratio":       vol_ratio,
        "trend":           f"EMA정배열 기울기{ind['ma20_slope_pct']:+.2f}%",
        "breakout":        f"5봉고점({prev_5_high}) 돌파",
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
    눌림목 진입 전략 VER5 — 오더블록 통합 (변경 없음)

    [조건 요약]
    A. EMA 정배열: EMA20 > EMA60
    B. RSI 45~70
    C. MACD ≥ 0
    D. 최근 10봉 고점 대비 -1% ~ -10% 조정
    E. EMA20 근접 ≤ 3.5%
    F. 최근 10봉 고점 > EMA20 × 1.02 (돌파 확인)
    G. 반등 양봉
    H. 캔들강도: STRONG(≤2%) ≥ 40%, NORMAL(2~3.5%) ≥ 50%
    I. 반등 거래량 ≥ 조정평균 × 1.0 AND ≥ 3000주
    J. F1~F3 가짜 신호 필터
    K. 오더블록 지지 확인
    """
    if len(candles) < 35:
        if logger:
            logger.info(f"[PULLBACK_SKIP] {code} 데이터 부족(필요:35 현재:{len(candles)})")
        return False

    ind = _calc_indicators(candles)
    if ind is None:
        if logger:
            logger.info(f"[PULLBACK_SKIP] {code} 지표 계산 실패")
        return False

    c1 = candles[0]
    c2 = candles[1]

    ema20  = ind["ema20"]
    ema60  = ind["ema60"]
    rsi14  = ind["rsi14"]
    macd_l = ind["macd_line"]

    ema_aligned = (ema20 > ema60)
    rsi_ok      = 45 <= rsi14 <= 70
    macd_ok     = macd_l >= 0

    recent_10_high = max(c['high'] for c in candles[0:10])
    pullback_pct   = (c1['close'] - recent_10_high) / recent_10_high
    pullback_ok    = -0.10 <= pullback_pct <= -0.01

    ma_distance    = abs(c1['close'] - ema20) / ema20
    near_ma_ok     = ma_distance <= 0.035
    near_ma_strong = ma_distance <= 0.020

    high_above_ma = recent_10_high > ema20 * 1.02
    bounce_ok     = (c1['close'] > c2['close'] and c1['close'] > c1['open'])

    rng     = c1['high'] - c1['low']
    body    = c1['close'] - c1['open']
    str_pct = (body / rng) if rng > 0 else 0
    final_str_ok = (str_pct >= 0.40 if near_ma_strong else str_pct >= 0.50)

    pull_vols    = [c['volume'] for c in candles[1:5]]
    avg_pull_vol = sum(pull_vols) / len(pull_vols) if pull_vols else 1
    vol_ok       = (c1['volume'] >= avg_pull_vol and c1['volume'] >= 3000)

    rec5      = candles[0:5]
    bear_cnt  = sum(1 for c in rec5 if c['close'] < c['open'])
    cls5      = [c['close'] for c in rec5]
    mono_down = all(cls5[i] <= cls5[i+1] for i in range(len(cls5)-1))
    f1 = (bear_cnt >= 3 and mono_down)
    f2 = (rng > 0 and (c1['high'] - c1['close']) > body * 2)
    f3 = (min(c['low'] for c in candles[0:5]) < ema20 * 0.95)
    filters_ok = not f1 and not f2 and not f3

    sr_boxes = _find_sr_boxes(candles)
    near_supp, s_lo, s_hi = _near_sr_box(c1['close'], sr_boxes, 'support', proximity=0.020)
    near_resist_block, _, _ = _near_sr_box(c1['close'], sr_boxes, 'resistance', proximity=0.010)
    sr_ok = not near_resist_block

    orderblocks = _find_orderblock(candles, lookback=20)
    ob_near, ob_low, ob_high, ob_sl, ob_age = _near_orderblock(
        cur_price   = c1['close'],
        orderblocks = orderblocks,
        proximity   = 0.015,
        max_age     = 15
    )

    if ob_near and ob_sl > 0 and c1['close'] < ob_sl:
        if logger:
            logger.info(
                f"[PULLBACK_OB_BROKEN] {code} "
                f"오더블록 하단({ob_sl}) 이탈 → 진입 차단 "
                f"현재가:{c1['close']} OB구간:{ob_low}~{ob_high}"
            )
        return False

    if not ob_near and not near_ma_strong:
        if logger:
            logger.info(
                f"[PULLBACK_OB_REQUIRED] {code} "
                f"EMA거리:{ma_distance*100:.2f}%(NORMAL) + 오더블록 없음 → 진입 차단"
            )
        return False

    base_ok  = (ema_aligned and rsi_ok and macd_ok and
                high_above_ma and pullback_ok and near_ma_ok and
                bounce_ok and vol_ok)
    is_valid = base_ok and final_str_ok and filters_ok and sr_ok

    if logger:
        sr_note = f"SR지지근접:{near_supp}({s_lo}~{s_hi})" if near_supp else "SR지지:없음"
        ob_note = (
            f"OB지지:{ob_near}({ob_low}~{ob_high} age={ob_age}봉전)"
            if ob_near else "OB:없음/비근접"
        )
        if is_valid:
            logger.info(
                f"[PULLBACK_CONFIRMED] {code} | "
                f"종가:{c1['close']} EMA20:{ema20:.0f} EMA60:{ema60:.0f} | "
                f"RSI:{rsi14:.1f} MACD:{macd_l:.2f} | "
                f"눌림:{pullback_pct*100:.1f}% | "
                f"MA거리:{ma_distance*100:.2f}%({'STRONG' if near_ma_strong else 'NORMAL'}) | "
                f"반등거래량:{c1['volume']} | "
                f"캔들강도:{str_pct*100:.0f}% | "
                f"{sr_note} | {ob_note}"
            )
        else:
            logger.info(
                f"[PULLBACK_CHECK] {code} "
                f"ema={ema_aligned}(ema20={ema20:.0f},ema60={ema60:.0f}) "
                f"rsi={rsi_ok}({rsi14:.1f}) "
                f"macd={macd_ok}({macd_l:.2f}) "
                f"trend=(rising={ema_aligned},high_above={high_above_ma}) "
                f"pullback={pullback_ok}({pullback_pct*100:.1f}%) "
                f"near_ma={near_ma_ok}({ma_distance*100:.2f}%) "
                f"bounce={bounce_ok} "
                f"strength={final_str_ok}({str_pct*100:.0f}%) "
                f"vol={vol_ok}({c1['volume']}주) "
                f"f1={f1} f2={f2} f3={f3} "
                f"sr_ok={sr_ok} "
                f"ob_near={ob_near} ob_invalidated={ob_near and ob_sl > 0 and c1['close'] < ob_sl} "
                f"{ob_note}"
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

    orderblocks = _find_orderblock(candles, lookback=20)
    ob_near, ob_low, ob_high, ob_sl, ob_age = _near_orderblock(
        c1['close'], orderblocks, proximity=0.015, max_age=15
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
    깃발 패턴 진입 전략 (변경 없음)

    [조건 요약]
    1. 기준봉: 양봉 + 상승폭 ≥ 1.0% + 캔들강도 ≥ 60% + 거래량 3배↑
    2. 횡보 구간 (3~15봉): 고저 범위 ≤ 기준봉 몸통 60% + 거래량 수렴
    3. 재돌파봉: 박스 상단 돌파 + 양봉 + 거래량 2배↑
    4. EMA 정배열: EMA20 > EMA60
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
