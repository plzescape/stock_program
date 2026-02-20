from datetime import datetime, time

def is_market_time() -> bool:
    # 한국장 기준: 09:00 ~ 15:30
    now = datetime.now().time()
    return time(9, 0) <= now <= time(15, 30)


#   ===========================
#   전략: 1분봉 기준 급등 캔들 진입
#   ===========================
def is_entry_candidate_1min(candles, logger=None, code=None) -> bool:
    if len(candles) < 3:
        if logger:
            logger.info(
                f"[ENTRY_SKIP] {code} len={len(candles)} (<3)"
            )
        return False

    c1, c2, c3 = candles[2], candles[1], candles[0]

    price_ok = (
        c3["close"] > c1["close"] and
        c3["close"] >= c2["close"]
    )

    vol_ok = (
        c3["volume"] >= max(c1["volume"], c2["volume"]) * 1.5 and
        c3["volume"] >= 1000
    )

    body = abs(c3["close"] - c3["open"])
    range_ = c3["high"] - c3["low"]
    body_ratio = body / range_ if range_ > 0 else 0
    body_ok = body_ratio >= 0.6

    if logger and code:
        logger.info(
            f"[ENTRY_1MIN] code={code} "
            f"price_ok={price_ok} vol_ok={vol_ok} body_ok={body_ok} "
            f"close={c1['close']},{c2['close']},{c3['close']} "
            f"vol={c1['volume']},{c2['volume']},{c3['volume']}"
        )

    return price_ok and vol_ok and body_ok


#   ===========================
#   전략: 3일 연속 거래량 및 가격 상승 종목 진입
#   ===========================
def is_entry_candidate(candles, logger=None, code=None) -> bool:
    """candles: 최신봉이 index 0 (최근 3개 필요)"""
    if len(candles) < 3:
        return False

    c1, c2, c3 = candles[2], candles[1], candles[0]

    vol_ok = c1["volume"] < c2["volume"] < c3["volume"]
    price_ok = c1["close"] < c2["close"] < c3["close"]

    if logger and code:
        logger.info(
            f"[ENTRY_CHECK] code={code} "
            f"close={c1['close']},{c2['close']},{c3['close']} "
            f"vol={c1['volume']},{c2['volume']},{c3['volume']} "
            f"vol_ok={vol_ok} price_ok={price_ok}"
        )

    return vol_ok and price_ok


# ===========================
# 전략: 브레이크아웃 진입 VER3
# ===========================
def is_entry_candidate_VER2(candles, logger=None, code=None) -> bool:
    """
    브레이크아웃 진입 전략 VER3 (실전 로그 기반 재설계)

    [변경 이력]
    VER1→VER2: MA20 추세 추종 도입
    VER2→VER3: 실전 로그 분석 기반 전면 재설계
      - SL 종목 5/5개 차단: MA갭 상한 + 거량 상한 필터
      - 기울기 0.3% → 0.5%: TIME_STOP 종목 전부 0.3%대 기울기였음
      - 거량 3배 → 4배: 신호 품질 강화
      - 직전1봉 고점 → 5봉 고점 돌파: 진짜 저항선 돌파 확인
      - 거량 20배 초과 차단 → 15배로 조정: 천장봉 방지

    [조건 요약]
    A. MA20 기울기 ≥ 0.5% (5봉 대비 실질 우상향)
    B. MA갭 0.5% ~ 1.5% (너무 이른/늦은 진입 차단)
    C. 최근 5봉 고점 돌파 + 양봉
    D. 거래량 4 ~ 15배 (약한 신호 및 천장봉 차단)
    E. 캔들 강도 ≥ 70% (장대양봉)
    F. 직전봉(c2)도 양봉 (추세 연속성)
    """
    if len(candles) < 25:
        if logger:
            logger.info(f"[STRATEGY_SKIP] {code} 데이터 부족 (필요:25, 현재:{len(candles)})")
        return False

    c1 = candles[0]   # 직전 완성봉 (진입 기준봉)
    c2 = candles[1]   # 전전 완성봉

    # MA20 계산
    ma20_now  = sum(c['close'] for c in candles[0:20]) / 20
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20   # 5봉 전 시점 MA20

    # ── A. MA20 기울기 ≥ 0.3% ────────────────────────────────────────
    # 0.5%는 실전에서 통과율 6.7%로 너무 낮음 → 0.3%로 완화
    # 0.3% 미만은 진짜 횡보로 판단
    ma20_slope_pct = (ma20_now - ma20_prev) / ma20_prev * 100
    trend_ok = (
        c1['close'] > ma20_now and      # 정배열 (종가 > MA20)
        ma20_slope_pct >= 0.3           # 0.5% → 0.3%로 완화
    )

    # ── B. MA갭 0.5% ~ 1.5% ───────────────────────────────────────────
    # 0.5% 미만: 추세 불확실 초기 단계
    # 1.5% 초과: 이미 많이 올라 TP1(+2%)까지 여력 0.5%뿐 → SL 위험
    ma_gap_pct = (c1['close'] - ma20_now) / ma20_now * 100
    ma_gap_ok = 0.5 <= ma_gap_pct <= 1.5

    # ── C. 최근 5봉 고점 돌파 + 양봉 ────────────────────────────────
    # 직전 1봉만 보던 기존 조건 → 단기 반등봉도 통과하는 문제 개선
    prev_5_high = max(c['high'] for c in candles[1:6])
    price_ok = (
        c1['close'] > prev_5_high and   # 5봉 저항선 돌파
        c1['close'] > c1['open']        # 양봉
    )

    # ── D. 거래량 3 ~ 15배 ────────────────────────────────────────────
    # 4배: 실전 통과율 2.5%로 너무 낮음 → 3배로 완화
    # 15배 초과: 천장봉(세력 털기) 의심 → 유지
    prev_5_candles = candles[1:6]
    avg_vol = sum(c['volume'] for c in prev_5_candles) / 5
    vol_ratio = c1['volume'] / avg_vol if avg_vol > 0 else 0
    vol_ok = (
        3.0 <= vol_ratio <= 15.0 and    # 4배 → 3배로 완화
        c1['volume'] >= 5000
    )

    # ── E. 캔들 강도 ≥ 70% (장대양봉, 윗꼬리 짧음) ──────────────────
    candle_range = c1['high'] - c1['low']
    body_size    = c1['close'] - c1['open']
    strength_ok  = (body_size / candle_range) >= 0.7 if candle_range > 0 else False

    # ── F. 직전봉(c2) 양봉 (추세 연속성) ─────────────────────────────
    c2_bull = c2['close'] > c2['open']

    # ── 최종 판정 ──────────────────────────────────────────────────────
    is_valid = trend_ok and ma_gap_ok and price_ok and vol_ok and strength_ok and c2_bull

    if logger:
        if is_valid:
            logger.info(
                f"[ENTRY_CONFIRMED] {code} | "
                f"종가:{c1['close']} | "
                f"MA20추세:상향({ma20_now:.1f}, 기울기+{ma20_slope_pct:.2f}%) | "
                f"MA갭:{ma_gap_pct:.2f}% | "
                f"5봉고점돌파:{prev_5_high} | "
                f"거래량:{c1['volume']}(평균의 {vol_ratio:.1f}배)"
            )
        else:
            logger.info(
                f"[ENTRY_CHECK] {code} "
                f"trend={trend_ok}(slope={ma20_slope_pct:.2f}%) "
                f"ma_gap={ma_gap_ok}({ma_gap_pct:.2f}%) "
                f"price={price_ok}(5봉고점={prev_5_high}) "
                f"vol={vol_ok}({vol_ratio:.1f}배) "  # 4배→3배 완화                f"strength={strength_ok} "
                f"c2_bull={c2_bull}"
            )

    return is_valid


def get_entry_signal_data(candles) -> dict | None:
    """브레이크아웃 진입 시 디스코드 알림용 지표"""
    if len(candles) < 25:
        return None

    c1 = candles[0]
    c2 = candles[1]

    ma20_now  = sum(c['close'] for c in candles[0:20]) / 20
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20

    ma20_slope_pct = (ma20_now - ma20_prev) / ma20_prev * 100
    ma_gap_pct = (c1['close'] - ma20_now) / ma20_now * 100

    prev_5_candles = candles[1:6]
    avg_vol = sum(c['volume'] for c in prev_5_candles) / 5
    vol_ratio = c1['volume'] / avg_vol if avg_vol > 0 else 0

    prev_5_high = max(c['high'] for c in candles[1:6])
    candle_range = c1['high'] - c1['low']
    body_size = c1['close'] - c1['open']
    candle_strength = (body_size / candle_range * 100) if candle_range > 0 else 0

    return {
        "vol_ratio": vol_ratio,
        "trend": f"MA20 상승 (기울기+{ma20_slope_pct:.2f}%)",
        "breakout": f"5봉고점({prev_5_high}) 돌파" if c1['close'] > prev_5_high else "5봉고점 돌파 미달",
        "candle_strength": candle_strength,
        "ma_gap_pct": ma_gap_pct,
    }


# ===========================
# 전략: 눌림목 진입 (MA20 지지 반등)
# ===========================
def is_pullback_entry(candles, logger=None, code=None) -> bool:
    """
    눌림목 진입 전략 VER3 (TP2 도달 종목 & 실전 로그 기반 최적화)

    [변경 이력]
    VER1: 기본 눌림목 조건
    VER2: MA거리 완화, F1~F3 필터 추가, near_ma 2단계 도입
    VER3 (오늘 로그 기반 추가 수정):
      - 반등거래량 최솟값 1000 → 3000 (016590: 1002주로 TIME_STOP 발생)
      - NORMAL 구간 캔들강도 60% → 65% (060370: 50%로 SL 발생)
      - NORMAL 구간 자체를 더 까다롭게: vol_ok도 1.2배 이상 요구

    [조건 요약]
    1. MA20 우상향 + 가격 > MA20
    2. 최근 10봉 고점 대비 -1% ~ -10% 조정
    3. MA20 거리 ≤ 3.5% (STRONG: ≤2%, NORMAL: 2~3.5%)
    4. 반등 양봉 + 캔들강도:
       - STRONG 구간: ≥ 55%
       - NORMAL 구간: ≥ 65% (더 엄격)
    5. 반등거래량 ≥ 조정 평균 × 1.0 AND 절댓값 ≥ 3000주
    6. 최근 10봉 최고점 > MA20 × 1.02 (실제 상승 모멘텀 확인)
    7. F1~F3 가짜 신호 필터
    """
    if len(candles) < 25:
        if logger:
            logger.info(f"[PULLBACK_SKIP] {code} 데이터 부족 (필요:25, 현재:{len(candles)})")
        return False

    c1 = candles[0]   # 직전 완성봉 (반등 봉)
    c2 = candles[1]   # 전전 봉 (조정 구간)

    # MA20 계산
    ma20_now  = sum(c['close'] for c in candles[0:20]) / 20
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20

    # 1. MA20 우상향 + 가격 > MA20
    price_above_ma = c1['close'] > ma20_now
    trend_rising   = ma20_now > ma20_prev
    trend_ok       = trend_rising and price_above_ma

    # 최근 10봉 고점이 MA20 대비 2% 이상 위 (실제 상승 모멘텀)
    recent_10_high = max(c['high'] for c in candles[0:10])
    high_above_ma  = recent_10_high > ma20_now * 1.02

    # 2. 고점 대비 조정폭 (-1% ~ -10%)
    recent_high  = max(c['high'] for c in candles[0:10])
    pullback_pct = (c1['close'] - recent_high) / recent_high
    pullback_ok  = -0.10 <= pullback_pct <= -0.01

    # 3. MA20 근접 (≤ 3.5%)
    ma_distance   = abs(c1['close'] - ma20_now) / ma20_now
    near_ma_ok    = ma_distance <= 0.035
    near_ma_strong = ma_distance <= 0.020   # 2% 이내 = 강한 지지

    # 4-1. 반등 양봉
    bounce_ok = (
        c1['close'] > c2['close'] and
        c1['close'] > c1['open']
    )

    # 4-2. 캔들 강도
    candle_range = c1['high'] - c1['low']
    body_size    = c1['close'] - c1['open']
    strength_pct = (body_size / candle_range) if candle_range > 0 else 0

    # STRONG 구간(≤2%): 40% 이상 / NORMAL 구간(2~3.5%): 50% 이상
    # ⬇️ 추가 완화: 실전 로그에서 최고 통과 케이스가 52%인 NORMAL, 42%인 STRONG
    # STRONG 50%→40%, NORMAL 55%→50%
    if near_ma_strong:
        final_strength_ok = strength_pct >= 0.40   # 55%→50%→40%
    else:
        final_strength_ok = strength_pct >= 0.50   # 65%→55%→50%

    # 5. 반등 거래량
    # 최솟값 1000 → 3000 (016590: 1002주로 TIME_STOP, 거래량 너무 적었음)
    pullback_vols   = [c['volume'] for c in candles[1:5]]
    avg_pullback_vol = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 1

    vol_recovery = c1['volume'] >= avg_pullback_vol * 1.0
    vol_ok       = vol_recovery and c1['volume'] >= 3000   # 1000 → 3000

    # --- 가짜 신호 필터 (F1~F3) ---
    # F1: 하락 추세 위장 필터 (최근 5봉 중 3봉 이상 음봉 + 단조 하락)
    recent_5   = candles[0:5]
    bear_count = sum(1 for c in recent_5 if c['close'] < c['open'])
    closes_5   = [c['close'] for c in recent_5]
    monotonic_down = all(closes_5[i] <= closes_5[i+1] for i in range(len(closes_5)-1))
    f1_fake_trend  = (bear_count >= 3 and monotonic_down)

    # F2: 위꼬리 과다 필터 (위꼬리 > 몸통 × 2 → 매도세 강함)
    upper_wick  = c1['high'] - c1['close']
    f2_fake_wick = (candle_range > 0 and upper_wick > body_size * 2)

    # F3: 지지선 붕괴 필터 (조정 저점이 MA20 대비 5% 이상 아래)
    pullback_low      = min(c['low'] for c in candles[0:5])
    f3_support_broken = (pullback_low < ma20_now * 0.95)

    filters_ok = not f1_fake_trend and not f2_fake_wick and not f3_support_broken

    # --- 최종 판정 ---
    base_ok  = trend_ok and high_above_ma and pullback_ok and near_ma_ok and bounce_ok and vol_ok
    is_valid = base_ok and final_strength_ok and filters_ok

    if logger:
        if is_valid:
            logger.info(
                f"[PULLBACK_CONFIRMED] {code} | "
                f"종가:{c1['close']} MA20:{ma20_now:.0f} | "
                f"눌림:{pullback_pct*100:.1f}% | "
                f"MA거리:{ma_distance*100:.2f}% ({'STRONG' if near_ma_strong else 'NORMAL'}) | "
                f"반등거래량:{c1['volume']} | "
                f"캔들강도:{strength_pct*100:.0f}%"
            )
        else:
            logger.info(
                f"[PULLBACK_CHECK] {code} "
                f"trend={trend_ok}(rising={trend_rising},high_above={high_above_ma}) "
                f"pullback={pullback_ok}({pullback_pct*100:.1f}%) "
                f"near_ma={near_ma_ok}({ma_distance*100:.2f}%) "
                f"bounce={bounce_ok} strength={final_strength_ok}({strength_pct*100:.0f}%) "
                f"vol={vol_ok}({c1['volume']}주) "
                f"f1_fake_trend={f1_fake_trend} f2_fake_wick={f2_fake_wick} f3_support_broken={f3_support_broken}"
            )

    return is_valid


def get_pullback_signal_data(candles) -> dict | None:
    """눌림목 진입 시 디스코드 알림용 지표"""
    if len(candles) < 25:
        return None

    c1 = candles[0]
    c2 = candles[1]

    ma20_now  = sum(c['close'] for c in candles[0:20]) / 20
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20

    recent_high  = max(c['high'] for c in candles[0:10])
    pullback_pct = (c1['close'] - recent_high) / recent_high

    ma_distance  = abs(c1['close'] - ma20_now) / ma20_now
    ma_strength  = "STRONG" if ma_distance <= 0.020 else "NORMAL"

    pullback_vols    = [c['volume'] for c in candles[1:5]]
    avg_pullback_vol = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 1
    vol_ratio        = c1['volume'] / avg_pullback_vol if avg_pullback_vol > 0 else 0

    candle_range    = c1['high'] - c1['low']
    body_size       = c1['close'] - c1['open']
    candle_strength = (body_size / candle_range * 100) if candle_range > 0 else 0

    return {
        "vol_ratio":     vol_ratio,
        "trend":         "MA20 상승" if ma20_now > ma20_prev else "MA20 하락",
        "breakout":      f"눌림목 반등 ({pullback_pct*100:.1f}%)",
        "candle_strength": candle_strength,
        "ma_distance":   ma_distance * 100,
        "ma_strength":   ma_strength,
    }
