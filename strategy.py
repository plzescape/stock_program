"""
strategy.py — 오더블록(Order Block) 통합 (2026-03-03)

[변경 내역]
- _find_orderblock(): 음봉이 양봉 몸통 전체를 감싸는 오더블록 자동 탐지
- _near_orderblock(): 현재가가 오더블록 구간 근처인지 확인
- is_pullback_entry() VER5: 오더블록 지지 확인 필터 추가
  K-1. OB 구간 근접 = EMA 지지 + OB 지지 이중 확인 (확신도 상승)
  K-2. OB 하단(음봉 저점) 이탈 시 즉시 진입 차단 (폐기된 오더블록)
  K-3. EMA 거리 NORMAL(2~3.5%)이면 OB 필수 / STRONG(≤2%)이면 OB 없어도 허용
- get_pullback_signal_data(): 오더블록 구간 정보 디스코드 알림 추가
- BREAKOUT / FLAG 전략 변경 없음
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
    보고서: RSI 55 이상 = 상승 모멘텀.
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
    보고서: MACD 파란선 > 빨간선 = 골든크로스 상태.
    """
    prices = list(reversed(closes))
    need = slow + signal
    if len(prices) < need:
        return None, None, None

    # fast/slow EMA 동시 계산
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
    candles: [최신→과거] (parse_1min 그대로).
    최소 35봉 필요 (MACD 26+9=35).
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

    # MA20 기울기: 현재 EMA20 vs 5봉 전 EMA20
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
    보고서: "수평 구간을 박스 형태로 표시, 두 가지 근거가 겹치는 자리 노림"

    candles: [최신→과거]
    window: 피벗 판정을 위한 좌우 봉 수 (2 = 좌2봉·우2봉보다 극단적)
    tolerance: 같은 구간으로 묶는 가격 폭 (기본 ±2%)
    min_touches: 구간으로 인정할 최소 피벗 수 (기본 2회 터치)
    반환: {'resistance': [(lo,hi),...], 'support': [(lo,hi),...]}
    """
    highs, lows = [], []
    n = len(candles)
    for i in range(window, n - window):
        h = candles[i]['high']
        l = candles[i]['low']
        # 피벗 고점
        if (all(h >= candles[i - j]['high'] for j in range(1, window + 1)) and
                all(h >= candles[i + j]['high'] for j in range(1, window + 1))):
            highs.append(h)
        # 피벗 저점
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
    proximity: 박스 중심 대비 ±N% 이내면 근접으로 판정 (기본 ±1.5%)
    반환: (근접여부, 박스하단, 박스상단)
    """
    for lo, hi in boxes.get(side, []):
        mid = (lo + hi) / 2.0
        if abs(cur_price - mid) / mid <= proximity:
            return True, lo, hi
    return False, 0, 0


# ==================================================
# ── 오더블록 유틸 (신규 추가) ──
# ==================================================

def _find_orderblock(candles: list,
                     lookback: int = 20,
                     min_body_ratio: float = 0.40) -> list:
    """
    매수 오더블록 자동 탐지 — 보고서 기반

    정의: "음봉 몸통이 직전 양봉 몸통 전체를 감싸는 현상 발생 시,
          그 직전 양봉의 몸통 구간이 매수 오더블록(지지구간)이 된다"

    candles: [최신→과거] 순서 (parse_1min 그대로)
    lookback: 탐색할 최대 봉 수
    min_body_ratio: 감싸는 음봉의 최소 몸통비율 (노이즈 제거)

    반환: [{'ob_low', 'ob_high', 'age', 'engulf_candle_low'}, ...]
      - ob_low/ob_high : 양봉 몸통 범위 (오더블록 지지구간)
      - age            : 몇 봉 전에 형성됐는지 (1=가장 최근)
      - engulf_candle_low : 감싸는 음봉의 저점 = 손절 기준가
    """
    orderblocks = []
    n = min(len(candles) - 1, lookback)

    for i in range(1, n):
        curr = candles[i]       # 현재 봉 (i봉 전)
        prev = candles[i + 1]   # 이전 봉 (더 과거)

        # 이전 봉 = 양봉
        prev_body_lo = min(prev['open'], prev['close'])
        prev_body_hi = max(prev['open'], prev['close'])
        if prev['close'] <= prev['open']:
            continue

        # 현재 봉 = 음봉
        curr_body_lo = min(curr['open'], curr['close'])
        curr_body_hi = max(curr['open'], curr['close'])
        if curr['close'] >= curr['open']:
            continue

        # 감싸는 조건: 음봉 몸통이 양봉 몸통 전체 포함
        if not (curr_body_lo <= prev_body_lo and curr_body_hi >= prev_body_hi):
            continue

        # 최소 몸통 비율 필터
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
            'engulf_candle_low': curr['low'],  # 음봉 저점 = 오더블록 폐기(손절) 기준
        })

    return orderblocks


def _near_orderblock(cur_price: float,
                     orderblocks: list,
                     proximity: float = 0.015,
                     max_age: int = 15) -> tuple:
    """
    현재가가 오더블록 구간 내부 또는 ±proximity% 이내인지 확인.
    max_age 봉 이상 지난 오더블록은 자동 무효화.

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
# 전략 1: BREAKOUT (돌파 진입) — 변경 없음
# ==================================================
def is_entry_candidate_VER2(candles, logger=None, code=None) -> bool:
    """
    돌파 진입 전략 VER4 (보고서 기반 재구성)

    [조건 요약]
    A. EMA 정배열: 종가 > EMA20 > EMA60
    B. RSI14 ≥ 55
    C. MACD > 시그널
    D. MA20 기울기 ≥ 0.3%
    E. 5봉 고점 돌파 + 양봉
    F. 거래량 2~15배
    G. 캔들강도 ≥ 60%
    H. SR 박스 저항 돌파 확인
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
    c2 = candles[1]

    ema20  = ind["ema20"]
    ema60  = ind["ema60"]
    rsi14  = ind["rsi14"]
    macd_l = ind["macd_line"]
    macd_s = ind["macd_signal"]
    slope  = ind["ma20_slope_pct"]

    ema_aligned = (c1['close'] > ema20 > ema60)
    rsi_ok      = rsi14 >= 55
    macd_ok     = macd_l > macd_s
    trend_ok    = slope >= 0.3

    prev_5_high = max(c['high'] for c in candles[1:6])
    price_ok    = (c1['close'] > prev_5_high and c1['close'] > c1['open'])

    avg_vol   = sum(c['volume'] for c in candles[1:6]) / 5
    vol_ratio = c1['volume'] / avg_vol if avg_vol > 0 else 0
    vol_ok    = (MIN_VOL_RATIO <= vol_ratio <= MAX_VOL_RATIO and c1['volume'] >= 5000)

    rng    = c1['high'] - c1['low']
    body   = c1['close'] - c1['open']
    str_ok = (body / rng) >= 0.60 if rng > 0 else False

    sr_boxes = _find_sr_boxes(candles)
    near_resist, r_lo, r_hi = _near_sr_box(c1['close'], sr_boxes, 'resistance', proximity=0.025)
    sr_breakout_ok = (not near_resist) or (c1['close'] > r_hi)

    is_valid = (ema_aligned and rsi_ok and macd_ok and
                trend_ok and price_ok and vol_ok and str_ok and sr_breakout_ok)

    if logger:
        if is_valid:
            logger.info(
                f"[ENTRY_CONFIRMED] {code} | "
                f"종가:{c1['close']} EMA20:{ema20:.0f} EMA60:{ema60:.0f} | "
                f"RSI:{rsi14:.1f} MACD:{macd_l:.2f}>{macd_s:.2f} | "
                f"기울기:{slope:.2f}% | "
                f"5봉고점돌파:{prev_5_high} | "
                f"거래량:{c1['volume']}(평균의 {vol_ratio:.1f}배) | "
                f"SR저항돌파:{sr_breakout_ok}(저항박스:{r_lo}~{r_hi})"
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
                f"strength={str_ok} "
                f"sr_break={sr_breakout_ok}"
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
    return {
        "vol_ratio":       vol_ratio,
        "trend":           f"EMA정배열 기울기{ind['ma20_slope_pct']:+.2f}%",
        "breakout":        f"5봉고점({prev_5_high}) 돌파",
        "candle_strength": strength,
        "rsi":             ind["rsi14"],
        "macd":            f"{ind['macd_line']:.2f}>{ind['macd_signal']:.2f}",
    }


# ==================================================
# 전략 2: PULLBACK (눌림목 반등 + 오더블록 통합) VER5
# ==================================================
def is_pullback_entry(candles, logger=None, code=None) -> bool:
    """
    눌림목 진입 전략 VER5 — 오더블록 통합 (2026-03-03)

    [오더블록 추가 조건 — 보고서 기반]
    K-1. 최근 20봉 내 오더블록 존재 + 현재가 구간 근접(±1.5%) 확인
         → EMA 지지와 동시 충족 시 "이중 지지" 확인으로 진입 확신도 극대화
    K-2. 오더블록 하단(음봉 저점) 이탈 시 즉시 진입 차단
         → 세력이 구간을 버린 상태(오더블록 폐기) = 가짜 반등 필터
    K-3. EMA 거리 NORMAL(2~3.5%) 구간에서는 오더블록 필수
         EMA 거리 STRONG(≤2%) 구간에서는 오더블록 없어도 진입 허용
         (기존 STRONG 진입 로직 하위 호환 유지)

    [기존 조건 요약]
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
    K. 오더블록 지지 확인 (신규)
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

    # A. EMA 정배열
    ema_aligned = (ema20 > ema60)

    # B. RSI 45~70
    rsi_ok = 45 <= rsi14 <= 70

    # C. MACD ≥ 0
    macd_ok = macd_l >= 0

    # D. 최근 10봉 고점 대비 조정폭
    recent_10_high = max(c['high'] for c in candles[0:10])
    pullback_pct   = (c1['close'] - recent_10_high) / recent_10_high
    pullback_ok    = -0.10 <= pullback_pct <= -0.01

    # E. EMA20 근접
    ma_distance    = abs(c1['close'] - ema20) / ema20
    near_ma_ok     = ma_distance <= 0.035
    near_ma_strong = ma_distance <= 0.020

    # F. 돌파 확인
    high_above_ma = recent_10_high > ema20 * 1.02

    # G. 반등 양봉
    bounce_ok = (c1['close'] > c2['close'] and c1['close'] > c1['open'])

    # H. 캔들강도
    rng     = c1['high'] - c1['low']
    body    = c1['close'] - c1['open']
    str_pct = (body / rng) if rng > 0 else 0
    final_str_ok = (str_pct >= 0.40 if near_ma_strong else str_pct >= 0.50)

    # I. 반등 거래량
    pull_vols    = [c['volume'] for c in candles[1:5]]
    avg_pull_vol = sum(pull_vols) / len(pull_vols) if pull_vols else 1
    vol_ok       = (c1['volume'] >= avg_pull_vol and c1['volume'] >= 3000)

    # J. 가짜 신호 필터
    rec5      = candles[0:5]
    bear_cnt  = sum(1 for c in rec5 if c['close'] < c['open'])
    cls5      = [c['close'] for c in rec5]
    mono_down = all(cls5[i] <= cls5[i+1] for i in range(len(cls5)-1))
    f1 = (bear_cnt >= 3 and mono_down)
    f2 = (rng > 0 and (c1['high'] - c1['close']) > body * 2)
    f3 = (min(c['low'] for c in candles[0:5]) < ema20 * 0.95)
    filters_ok = not f1 and not f2 and not f3

    # SR 박스 확인
    sr_boxes = _find_sr_boxes(candles)
    near_supp, s_lo, s_hi = _near_sr_box(c1['close'], sr_boxes, 'support', proximity=0.020)
    near_resist_block, _, _ = _near_sr_box(c1['close'], sr_boxes, 'resistance', proximity=0.010)
    sr_ok = not near_resist_block

    # ──────────────────────────────────────────────────────
    # K. 오더블록 지지 확인 (신규 — 보고서 기반)
    # ──────────────────────────────────────────────────────
    orderblocks = _find_orderblock(candles, lookback=20)
    ob_near, ob_low, ob_high, ob_sl, ob_age = _near_orderblock(
        cur_price   = c1['close'],
        orderblocks = orderblocks,
        proximity   = 0.015,  # ±1.5% 이내면 OB 구간 근접으로 판정
        max_age     = 15      # 15봉 이내 OB만 유효
    )

    # K-2. OB 하단(음봉 저점) 이탈 = OB 폐기 → 즉시 진입 차단
    if ob_near and ob_sl > 0 and c1['close'] < ob_sl:
        if logger:
            logger.info(
                f"[PULLBACK_OB_BROKEN] {code} "
                f"오더블록 하단({ob_sl}) 이탈 → 진입 차단 "
                f"현재가:{c1['close']} OB구간:{ob_low}~{ob_high}"
            )
        return False

    # K-3. EMA 거리 NORMAL이면 OB 필수 (품질 강화)
    #      EMA 거리 STRONG이면 OB 없어도 허용 (기존 로직 하위 호환)
    if not ob_near and not near_ma_strong:
        if logger:
            logger.info(
                f"[PULLBACK_OB_REQUIRED] {code} "
                f"EMA거리:{ma_distance*100:.2f}%(NORMAL) + 오더블록 없음 → 진입 차단"
            )
        return False

    # 최종 판정
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

    # 오더블록 정보 추가 (있을 때만)
    orderblocks = _find_orderblock(candles, lookback=20)
    ob_near, ob_low, ob_high, ob_sl, ob_age = _near_orderblock(
        c1['close'], orderblocks, proximity=0.015, max_age=15
    )
    if ob_near:
        result["orderblock"] = f"OB지지({ob_low:,}~{ob_high:,}, {ob_age}봉전)"
        result["ob_sl"]      = ob_sl  # 참고용 손절가 (ATR 손절과 별도)

    return result


# ==================================================
# 전략 3: FLAG (깃발 패턴) — 변경 없음
# ==================================================
def is_flag_entry(candles, logger=None, code=None) -> bool:
    """
    깃발 패턴 진입 전략

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