"""
strategy.py — 보고서 기반 전면 재구성 + 패턴 확장 (2026-02-22)

[보고서 적용 항목]
1. EMA 2단계 정배열 필터 (EMA20 > EMA60)
   - "20, 50, 100 EMA 정배열 상태에서만 매수"
   - "100 EMA 이탈 시 매수 절대 금지"
2. RSI(14) 모멘텀 확인
   - BREAKOUT: RSI ≥ 55 / PULLBACK: RSI 45~70
3. MACD 방향 확인
   - BREAKOUT: MACD > 시그널 / PULLBACK: MACD ≥ 0
4. 돌파 후 리테스트 전략 (토니 몬타나)
   - 저항선 돌파 후 지지 확인 구간에서 PULLBACK 진입

[2차 추가 항목 - GPT/보고서 권고]
5. 지지/저항 박스 구간 자동 탐지 (_find_sr_boxes)
   - "구간을 박스 형태로 표시, 두 가지가 겹치는 자리를 노림"
   - 피벗 고/저점 클러스터링으로 수평 구간 자동 생성
   - PULLBACK: EMA 근접 + SR 박스 근접 시 확신도 상승 (추가 확인 근거)
   - BREAKOUT: SR 저항 박스 돌파 확인으로 신호 품질 강화
6. 깃발 패턴 (FLAG) 진입 전략 — 신규 entry_type
   - "기준봉 세우고 짧게 횡보, 재돌파 시 다음 파동 기대"
   - 기준봉(강한 양봉+거래량) + 횡보 5~15봉(범위 축소+거래량 감소)
     + 박스 상단 재돌파 + 거래량 재증가 시 진입
   - BREAKOUT/PULLBACK과 병렬로 동작 (entry_type = "FLAG")
"""

from datetime import datetime, time


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
    prices = list(reversed(closes))   # 과거→최신
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

    macd_line   = macd_hist_vals[-1]
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
# ── 지지/저항 박스 구간 유틸 (보고서 2차 추가) ──
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
# 전략 1: BREAKOUT (돌파 진입)
# ==================================================
def is_entry_candidate_VER2(candles, logger=None, code=None) -> bool:
    """
    돌파 진입 전략 VER4 (보고서 기반 재구성)

    [조건 요약]
    A. EMA 정배열: 종가 > EMA20 > EMA60
       (보고서: 20/50/100 EMA 정배열 + 100EMA 이탈 금지)
    B. RSI14 ≥ 55 — 상승 모멘텀 확인
       (보고서: "RSI가 55 위로 회복하는 패턴")
    C. MACD > 시그널 — 골든크로스 방향
       (보고서: "MACD 파란선이 빨간 시그널 선 상향 돌파")
    D. MA20 기울기 ≥ 0.3% — 단기 추세 우상향
    E. 5봉 고점 돌파 + 양봉
    F. 거래량 3~15배
    G. 캔들강도 ≥ 60%
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

    # A. EMA 정배열
    ema_aligned = (c1['close'] > ema20 > ema60)

    # B. RSI ≥ 55
    rsi_ok = rsi14 >= 55

    # C. MACD 골든크로스 상태
    macd_ok = macd_l > macd_s

    # D. MA20 기울기
    trend_ok = slope >= 0.3

    # E. 5봉 고점 돌파 + 양봉
    prev_5_high = max(c['high'] for c in candles[1:6])
    price_ok = (c1['close'] > prev_5_high and c1['close'] > c1['open'])

    # F. 거래량 3~15배
    avg_vol   = sum(c['volume'] for c in candles[1:6]) / 5
    vol_ratio = c1['volume'] / avg_vol if avg_vol > 0 else 0
    vol_ok    = (3.0 <= vol_ratio <= 15.0 and c1['volume'] >= 5000)

    # G. 캔들강도 ≥ 60%
    rng       = c1['high'] - c1['low']
    body      = c1['close'] - c1['open']
    str_ok    = (body / rng) >= 0.60 if rng > 0 else False

    # H. SR 박스: 저항 구간 돌파 확인 (보조 근거, 없으면 패스)
    #    보고서: "저항이 지지로 바뀌는 구간을 돌파한 자리"
    sr_boxes = _find_sr_boxes(candles)
    near_resist, r_lo, r_hi = _near_sr_box(c1['close'], sr_boxes, 'resistance', proximity=0.025)
    sr_breakout_ok = (not near_resist) or (c1['close'] > r_hi)
    # → 저항박스가 없거나, 있어도 상단을 돌파한 경우만 통과
    #   저항박스 한복판에 걸려있으면 차단 (아직 안 뚫린 저항)

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
# 전략 2: PULLBACK (눌림목 반등 — 리테스트)
# ==================================================
def is_pullback_entry(candles, logger=None, code=None) -> bool:
    """
    눌림목 진입 전략 VER4 (보고서 기반 재구성)

    [보고서 토니 몬타나 전략 적용]
    - 돌파 후 저항→지지 전환 구간 리테스트에서 진입
    - 지지구간이 깨지면 진입 않음

    [조건 요약]
    A. EMA 정배열: EMA20 > EMA60
       (100 EMA 하향 이탈 = EMA60 이탈로 대체 차단)
    B. RSI 45~70 — 눌림목 구간 모멘텀
    C. MACD ≥ 0 — 전반적 상향 방향성
    D. 최근 10봉 고점 대비 -1% ~ -10% 조정
    E. EMA20 근접 ≤ 3.5% (리테스트 지지구간)
    F. 최근 10봉 고점 > EMA20 × 1.02 (돌파 확인)
    G. 반등 양봉 (지지 확인 후 반등봉)
    H. 캔들강도: STRONG(≤2%)≥40%, NORMAL(2~3.5%)≥50%
    I. 반등 거래량 ≥ 조정평균 × 1.0 AND ≥ 3000주
    J. F1~F3 가짜 신호 필터
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

    # A. EMA 정배열 (100EMA 이탈 차단)
    ema_aligned = (ema20 > ema60)

    # B. RSI 45~70
    rsi_ok = 45 <= rsi14 <= 70

    # C. MACD ≥ 0 (전반적 방향성)
    macd_ok = macd_l >= 0

    # D. 최근 10봉 고점 대비 조정폭
    recent_10_high = max(c['high'] for c in candles[0:10])
    pullback_pct   = (c1['close'] - recent_10_high) / recent_10_high
    pullback_ok    = -0.10 <= pullback_pct <= -0.01

    # E. EMA20 근접 (리테스트 구간)
    ma_distance    = abs(c1['close'] - ema20) / ema20
    near_ma_ok     = ma_distance <= 0.035
    near_ma_strong = ma_distance <= 0.020

    # F. 돌파 확인 (최근 고점이 EMA20 대비 +2% 이상)
    high_above_ma = recent_10_high > ema20 * 1.02

    # G. 반등 양봉
    bounce_ok = (c1['close'] > c2['close'] and c1['close'] > c1['open'])

    # H. 캔들강도
    rng  = c1['high'] - c1['low']
    body = c1['close'] - c1['open']
    str_pct = (body / rng) if rng > 0 else 0
    final_str_ok = (str_pct >= 0.40 if near_ma_strong else str_pct >= 0.50)

    # I. 반등 거래량
    pull_vols    = [c['volume'] for c in candles[1:5]]
    avg_pull_vol = sum(pull_vols) / len(pull_vols) if pull_vols else 1
    vol_ok       = (c1['volume'] >= avg_pull_vol and c1['volume'] >= 3000)

    # J. 가짜 신호 필터
    rec5       = candles[0:5]
    bear_cnt   = sum(1 for c in rec5 if c['close'] < c['open'])
    cls5       = [c['close'] for c in rec5]
    mono_down  = all(cls5[i] <= cls5[i+1] for i in range(len(cls5)-1))
    f1 = (bear_cnt >= 3 and mono_down)
    f2 = (rng > 0 and (c1['high'] - c1['close']) > body * 2)
    f3 = (min(c['low'] for c in candles[0:5]) < ema20 * 0.95)
    filters_ok = not f1 and not f2 and not f3

    # K. SR 박스 지지 근접 확인 (보조 근거 — 보고서 "구간 겹침" 전략)
    #    EMA 근접에 더해 수평 지지박스도 근처면 확신도 상승
    #    없으면 페널티 없음 (박스가 없을 수도 있으므로)
    sr_boxes  = _find_sr_boxes(candles)
    near_supp, s_lo, s_hi = _near_sr_box(c1['close'], sr_boxes, 'support', proximity=0.020)
    near_resist_block, _, _ = _near_sr_box(c1['close'], sr_boxes, 'resistance', proximity=0.010)
    # 저항박스 한복판이면 리테스트가 아니라 저항에 부딪힌 것 → 차단
    sr_ok = not near_resist_block

    # 최종
    base_ok  = (ema_aligned and rsi_ok and macd_ok and
                high_above_ma and pullback_ok and near_ma_ok and
                bounce_ok and vol_ok)
    is_valid = base_ok and final_str_ok and filters_ok and sr_ok

    if logger:
        sr_note = f"SR지지근접:{near_supp}({s_lo}~{s_hi})" if near_supp else "SR지지:없음"
        if is_valid:
            logger.info(
                f"[PULLBACK_CONFIRMED] {code} | "
                f"종가:{c1['close']} EMA20:{ema20:.0f} EMA60:{ema60:.0f} | "
                f"RSI:{rsi14:.1f} MACD:{macd_l:.2f} | "
                f"눌림:{pullback_pct*100:.1f}% | "
                f"MA거리:{ma_distance*100:.2f}%({'STRONG' if near_ma_strong else 'NORMAL'}) | "
                f"반등거래량:{c1['volume']} | "
                f"캔들강도:{str_pct*100:.0f}% | "
                f"{sr_note}"
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
                f"f1_fake_trend={f1} f2_fake_wick={f2} f3_support_broken={f3} "
                f"sr_ok={sr_ok}"
            )

    return is_valid


def get_pullback_signal_data(candles) -> dict | None:
    """PULLBACK 진입 시 디스코드 알림용 지표"""
    if len(candles) < 35:
        return None
    ind = _calc_indicators(candles)
    if ind is None:
        return None

    c1   = candles[0]
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

    return {
        "vol_ratio":       vol_ratio,
        "trend":           f"EMA정배열 리테스트({pullback_pct*100:.1f}%)",
        "breakout":        f"지지 확인({ma_strength})",
        "candle_strength": strength,
        "rsi":             ind["rsi14"],
        "macd":            f"{ind['macd_line']:.2f}",
        "ma_strength":     ma_strength,
    }


# ==================================================
# 전략 3: FLAG (깃발 패턴) — 신규 entry_type
# ==================================================
def is_flag_entry(candles, logger=None, code=None) -> bool:
    """
    깃발 패턴 진입 전략 (보고서 2차 추가)

    보고서: "세력들이 기준봉을 세우고 기간 조정을 거쳐 짧게 횡보,
            박스 상단 재돌파 시 다음 파동 기대"

    [조건 요약]
    1. 기준봉 (candles[1+flag_len]):
       - 양봉 + 상승폭 ≥ 1.0%
       - 캔들강도 ≥ 60%
       - 거래량 직전5봉 평균 대비 ≥ 3배
    2. 횡보 구간 (candles[1 : 1+flag_len], flag_len=3~15봉):
       - 고저 범위 ≤ 기준봉 몸통의 60% (좁은 박스)
       - 평균 거래량 ≤ 기준봉의 60% (거래량 수렴)
    3. 재돌파봉 (candles[0], 최신 완성봉):
       - 횡보 박스 상단 돌파 + 양봉
       - 거래량 ≥ 횡보 평균의 2배 (재폭발)
    4. EMA 정배열: EMA20 > EMA60 (전체 추세 확인)

    손절: 기준봉 시가 (보고서: "세력이 기준봉 시가 아래를 절대 허용 안 함")
    """
    MIN_FLAG = 3
    MAX_FLAG = 15
    need = MAX_FLAG + 25   # 기준봉 + 배경봉 충분히 확보

    if len(candles) < need:
        if logger:
            logger.info(f"[FLAG_SKIP] {code} 데이터 부족(필요:{need} 현재:{len(candles)})")
        return False

    # EMA 정배열 사전 체크
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

    c0 = candles[0]  # 재돌파봉 (최신 완성봉)

    for flag_len in range(MIN_FLAG, MAX_FLAG + 1):
        if 1 + flag_len + 5 >= len(candles):
            break

        flag_candles = candles[1 : 1 + flag_len]      # 횡보 구간
        base         = candles[1 + flag_len]           # 기준봉 후보
        prev_base    = candles[1 + flag_len + 1 : 1 + flag_len + 6]  # 기준봉 이전 5봉

        # ── 기준봉 조건 ─────────────────────────────────────
        base_body  = base['close'] - base['open']
        base_range = base['high'] - base['low']
        base_rise  = base_body / base['open'] if base['open'] > 0 else 0
        base_str   = base_body / base_range if base_range > 0 else 0

        avg_vol_before  = (sum(c['volume'] for c in prev_base) / len(prev_base)
                           if prev_base else 1)
        base_vol_ratio  = base['volume'] / avg_vol_before if avg_vol_before > 0 else 0

        is_base = (
            base_body   > 0     and   # 양봉
            base_rise   >= 0.010 and  # 1% 이상 상승
            base_str    >= 0.60  and  # 캔들강도 60%
            base_vol_ratio >= 3.0     # 거래량 3배 이상
        )
        if not is_base:
            continue

        # ── 횡보 구간 조건 ───────────────────────────────────
        flag_highs = [c['high']   for c in flag_candles]
        flag_lows  = [c['low']    for c in flag_candles]
        flag_vols  = [c['volume'] for c in flag_candles]

        box_range        = max(flag_highs) - min(flag_lows)
        flag_range_ratio = box_range / base_body if base_body > 0 else 999
        avg_flag_vol     = sum(flag_vols) / len(flag_vols) if flag_vols else 1
        vol_shrink       = avg_flag_vol / base['volume']

        is_flag = (
            flag_range_ratio <= 0.60 and   # 횡보폭 ≤ 기준봉 몸통의 60%
            vol_shrink       <= 0.60        # 거래량 수렴 (60% 이하)
        )
        if not is_flag:
            continue

        # ── 재돌파 조건 ─────────────────────────────────────
        box_top     = max(flag_highs)
        rebreak_ok  = (
            c0['close'] > box_top and          # 박스 상단 돌파
            c0['close'] > c0['open'] and       # 양봉
            c0['volume'] >= avg_flag_vol * 2.0  # 거래량 2배 이상 재폭발
        )
        if not rebreak_ok:
            continue

        # ── SR 박스: 재돌파가 저항 한복판에 걸리면 차단 ────
        sr_boxes = _find_sr_boxes(candles)
        _, _, r_hi = _near_sr_box(c0['close'], sr_boxes, 'resistance', proximity=0.010)
        if r_hi and c0['close'] < r_hi:
            # 저항 박스 안에 있으면 아직 돌파 미완성
            if logger:
                logger.info(
                    f"[FLAG_CHECK] {code} "
                    f"flag_len={flag_len} 재돌파 감지됐으나 저항박스({r_hi}) 미돌파 → 스킵"
                )
            continue

        # ── 전부 통과 ────────────────────────────────────────
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
        base_rise = base_body / base['open'] if base['open'] > 0 else 0
        avg_vol_before = (sum(c['volume'] for c in prev_base) / len(prev_base)
                          if prev_base else 1)
        base_vol_ratio = base['volume'] / avg_vol_before if avg_vol_before > 0 else 0
        if base_vol_ratio < 3.0 or base_rise < 0.01:
            continue

        flag_highs = [c['high']   for c in flag_candles]
        flag_vols  = [c['volume'] for c in flag_candles]
        avg_flag_vol = sum(flag_vols) / len(flag_vols) if flag_vols else 1
        box_top    = max(flag_highs)

        if c0['close'] > box_top and c0['volume'] >= avg_flag_vol * 2.0:
            return {
                "vol_ratio":       c0['volume'] / avg_flag_vol,
                "trend":           f"깃발 재돌파(기준봉+{base_rise*100:.1f}%)",
                "breakout":        f"박스상단({box_top}) 돌파",
                "candle_strength": base_body / (base['high'] - base['low']) * 100
                                   if (base['high'] - base['low']) > 0 else 0,
                "flag_len":        flag_len,
                "base_stop":       base['open'],   # 손절 기준봉 시가
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
