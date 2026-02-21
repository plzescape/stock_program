"""
strategy.py — 보고서 기반 전면 재구성 (2026-02-20)

[보고서 적용 항목]
1. EMA 2단계 정배열 필터 (EMA20 > EMA60)
   - "20, 50, 100 EMA 정배열 상태에서만 매수"
   - "100 EMA 이탈 시 매수 절대 금지"
2. RSI(14) 모멘텀 확인
   - BREAKOUT: RSI ≥ 55 (상승 모멘텀 진입)
   - PULLBACK: RSI 45~70 (눌림목 반등 구간)
3. MACD 방향 확인
   - BREAKOUT: MACD > 시그널 (골든크로스 상태)
   - PULLBACK: MACD ≥ 0 (전반적 상향 방향)
4. 돌파 후 리테스트 전략 (토니 몬타나)
   - 저항선 돌파 후 지지 확인 구간에서 PULLBACK 진입
5. 손익비 config 권고
   - SL -1.0%, TP1 +2% → 손익비 1:2 (보고서 핵심 원칙)
   - (실제 config.py 값 변경 필요)
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

    is_valid = (ema_aligned and rsi_ok and macd_ok and
                trend_ok and price_ok and vol_ok and str_ok)

    if logger:
        if is_valid:
            logger.info(
                f"[ENTRY_CONFIRMED] {code} | "
                f"종가:{c1['close']} EMA20:{ema20:.0f} EMA60:{ema60:.0f} | "
                f"RSI:{rsi14:.1f} MACD:{macd_l:.2f}>{macd_s:.2f} | "
                f"기울기:{slope:.2f}% | "
                f"5봉고점돌파:{prev_5_high} | "
                f"거래량:{c1['volume']}(평균의 {vol_ratio:.1f}배)"
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
                f"strength={str_ok}"
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

    # 최종
    base_ok  = (ema_aligned and rsi_ok and macd_ok and
                high_above_ma and pullback_ok and near_ma_ok and
                bounce_ok and vol_ok)
    is_valid = base_ok and final_str_ok and filters_ok

    if logger:
        if is_valid:
            logger.info(
                f"[PULLBACK_CONFIRMED] {code} | "
                f"종가:{c1['close']} EMA20:{ema20:.0f} EMA60:{ema60:.0f} | "
                f"RSI:{rsi14:.1f} MACD:{macd_l:.2f} | "
                f"눌림:{pullback_pct*100:.1f}% | "
                f"MA거리:{ma_distance*100:.2f}%({'STRONG' if near_ma_strong else 'NORMAL'}) | "
                f"반등거래량:{c1['volume']} | "
                f"캔들강도:{str_pct*100:.0f}%"
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
                f"f1_fake_trend={f1} f2_fake_wick={f2} f3_support_broken={f3}"
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
# ── 레거시 함수 (kiwoom_api.py import 호환 유지) ──
# ==================================================
def is_entry_candidate_1min(candles, logger=None, code=None) -> bool:
    """레거시 — 미사용"""
    return False


def is_entry_candidate(candles, logger=None, code=None) -> bool:
    """레거시 — 미사용"""
    return False
