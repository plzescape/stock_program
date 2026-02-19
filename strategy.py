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

    # 가격: 전체 우상향 + 마지막 봉 강함
    price_ok = (
        c3["close"] > c1["close"] and
        c3["close"] >= c2["close"]
    )

    # 거래량: 마지막 1분 폭증
    vol_ok = (
        c3["volume"] >= max(c1["volume"], c2["volume"]) * 1.5 and
        c3["volume"] >= 1000
    )

    # 캔들 몸통 비율 (윗꼬리 제거)
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
    # print("is_entry_candidate called with code:", code, len(candles), "candles")
    if len(candles) < 3:
        return False

    c1, c2, c3 = candles[2], candles[1], candles[0]  # 오래된→최신

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


def is_entry_candidate_VER2(candles, logger=None, code=None) -> bool:
    """
    브레이크아웃 진입 전략 VER3 (실전 로그 기반 재설계)

    [기존 VER2의 문제점]
    1. 직전 1봉 고점만 비교 → 단기 반등봉도 '돌파'로 인식
    2. MA20 기울기 판단 너무 단순 (5봉 비교, 상승폭 0원도 통과)
    3. 진입 종가가 MA20에서 이미 너무 멀어 추가 상승 여력 부족
       → SL 종목 평균 MA갭 1.76% (TP1 목표 2% → 여력 0.24%뿐)
    4. 거래량 폭증이 '매집봉'인지 '천장봉'인지 구분 불가
       → 거량 18~30배 종목이 줄줄이 SL

    [VER3 개선 방향]
    A. 진입 시점 MA갭 상한선 도입: 종가가 MA20 대비 1.5% 이내여야 진입
       → 이미 많이 오른 종목 차단, 남은 여력 확보
    B. 다봉 고점 돌파: 최근 5봉 고점 모두 돌파해야 진입
       → 단기 반등이 아닌 실제 저항 돌파 확인
    C. MA20 기울기 최소 상승률 도입: 5봉 대비 최소 0.3% 이상 상승
       → 횡보 구간 '상향 판정' 차단
    D. 거래량 이상 급등 차단: 20배 이상 폭증 시 '천장봉' 의심 → 제외
       → 398120(30.5배), 330730(19.5배), 000210(18.8배) 모두 SL이었음
    E. 연속 상승 확인: c1이 상승봉 + c2도 양봉 → 추세 연속성
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

    # ── A. MA20 기울기 (최소 0.3% 이상 상승) ──────────────────────────
    ma20_slope_pct = (ma20_now - ma20_prev) / ma20_prev * 100
    trend_ok = (
        c1['close'] > ma20_now and      # 정배열
        ma20_slope_pct >= 0.3           # 실질적 우상향 (횡보 제외)
    )

    # ── B. 진입 시점 MA갭 상한: MA20 대비 +0.5% ~ +1.5% ───────────────
    # - 0.5% 미만: 아직 MA20 돌파 초기, 추세 불확실
    # - 1.5% 초과: 이미 많이 올라서 TP1(+2%)까지 남은 여력 0.5%뿐
    ma_gap_pct = (c1['close'] - ma20_now) / ma20_now * 100
    ma_gap_ok = 0.5 <= ma_gap_pct <= 1.5

    # ── C. 다봉 고점 돌파: 최근 5봉(c2~c6) 고점 모두 돌파 ────────────
    prev_5_high = max(c['high'] for c in candles[1:6])
    price_ok = (
        c1['close'] > prev_5_high and   # 5봉 고점 저항 돌파
        c1['close'] > c1['open']        # 양봉
    )

    # ── D. 거래량: 5봉 평균 대비 3~15배 (20배 초과는 천장봉 의심 차단) ──
    prev_5_candles = candles[1:6]
    avg_vol = sum(c['volume'] for c in prev_5_candles) / 5
    vol_ratio = c1['volume'] / avg_vol if avg_vol > 0 else 0
    vol_ok = (
        3.0 <= vol_ratio <= 15.0 and    # 폭증은 OK, 과도한 폭등은 차단
        c1['volume'] >= 5000
    )

    # ── E. 캔들 강도: 몸통 70% 이상 (윗꼬리 짧은 장대양봉) ─────────────
    candle_range = c1['high'] - c1['low']
    body_size    = c1['close'] - c1['open']
    strength_ok  = (body_size / candle_range) >= 0.7 if candle_range > 0 else False

    # ── F. 직전봉(c2)도 양봉: 추세 연속성 확인 ──────────────────────────
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
                f"vol={vol_ok}({vol_ratio:.1f}배) "
                f"strength={strength_ok} "
                f"c2_bull={c2_bull}"
            )

    return is_valid


def get_entry_signal_data(candles) -> dict | None:
    """
    is_entry_candidate_VER2(VER3)와 동일한 조건으로 전략 지표를 계산하여 반환.
    디스코드 알림용.
    """
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
    눌림목 진입 전략:
    상승 추세(MA20 우상향) 중 조정을 받다가 MA20 부근에서 반등하는 시점에 진입.

    TP2 도달 종목 분석 기반 최적화:
    1. MA20 우상향 (추세 유지)
    2. 최근 고점 대비 조정 (-1% ~ -10%)
    3. MA20 근접 (±3.5% 이내) — 단, TP2 도달 종목은 모두 2% 이내였음
       → near_ma 조건을 2단계로: STRONG(≤2%) / NORMAL(2~3.5%)
    4. 반등 양봉 + 캔들강도 ≥ 60% (TP2 도달 평균 ~70%)
    5. 반등봉 거래량 ≥ 조정 구간 평균 (거래량 회복)
    6. 위짜 필터, 지지선 붕괴 필터 (F1~F3)
    7. 최근 10봉 최고점이 MA20보다 2% 이상 위 (실제 상승세 확인)
    """
    if len(candles) < 25:
        if logger:
            logger.info(f"[PULLBACK_SKIP] {code} 데이터 부족 (필요:25, 현재:{len(candles)})")
        return False

    c1 = candles[0]   # 직전 완성봉 (반등 봉)
    c2 = candles[1]   # 전전 봉 (조정 구간)

    # --- MA20 계산 ---
    ma20_now = sum(c['close'] for c in candles[0:20]) / 20
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20

    # 1. MA20 우상향 + 가격 MA20 위
    price_above_ma = c1['close'] > ma20_now
    trend_rising = ma20_now > ma20_prev
    trend_ok = trend_rising and price_above_ma

    # 최근 10봉 내 고점이 MA20보다 2% 이상 위여야 실제 상승 모멘텀 확인
    recent_10_high = max(c['high'] for c in candles[0:10])
    high_above_ma = recent_10_high > ma20_now * 1.02

    # --- 최근 10봉 내 고점 ---
    recent_high = max(c['high'] for c in candles[0:10])

    # 2. 고점 대비 조정폭 (-1% ~ -10%)
    pullback_pct = (c1['close'] - recent_high) / recent_high
    pullback_ok = -0.10 <= pullback_pct <= -0.01

    # 3. MA20 근접 (±3.5% 이내)
    #    TP2 도달 종목 기준: 0.81%(241520), 2.05%(023760) → 강할수록 좋음
    ma_distance = abs(c1['close'] - ma20_now) / ma20_now
    near_ma_ok = ma_distance <= 0.035
    near_ma_strong = ma_distance <= 0.020   # 2% 이내 = 강한 지지 신호

    # 4-1. 반등 양봉
    bounce_ok = (
        c1['close'] > c2['close'] and
        c1['close'] > c1['open']
    )

    # 4-2. 캔들 강도 ≥ 60% (TP2 도달 종목 평균 ~70%)
    candle_range = c1['high'] - c1['low']
    body_size = c1['close'] - c1['open']
    strength_pct = (body_size / candle_range) if candle_range > 0 else 0
    strength_ok = strength_pct >= 0.60

    # 5. 거래량 조건: 반등봉 ≥ 조정 구간 평균
    pullback_vols = [c['volume'] for c in candles[1:5]]
    avg_pullback_vol = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 1
    vol_recovery = c1['volume'] >= avg_pullback_vol * 1.0
    vol_ok = vol_recovery and c1['volume'] >= 1000

    # --- 가짜 신호 필터 (F1~F3) ---
    # F1: 하락 추세 위장 필터 (최근 5봉 중 3봉 이상 음봉 + 단조 하락)
    recent_5 = candles[0:5]
    bear_count = sum(1 for c in recent_5 if c['close'] < c['open'])
    closes_5 = [c['close'] for c in recent_5]
    monotonic_down = all(closes_5[i] <= closes_5[i+1] for i in range(len(closes_5)-1))
    f1_fake_trend = (bear_count >= 3 and monotonic_down)

    # F2: 위꼬리 과다 필터 (위꼬리 > 몸통 * 2 → 매도세 강함)
    upper_wick = c1['high'] - c1['close']
    f2_fake_wick = (candle_range > 0 and upper_wick > body_size * 2)

    # F3: 지지선 붕괴 필터 (조정 저점이 MA20보다 5% 이상 아래)
    pullback_low = min(c['low'] for c in candles[0:5])
    f3_support_broken = (pullback_low < ma20_now * 0.95)

    # --- 최종 판정 ---
    # near_ma_strong이면 MA거리 필터 완화 혜택
    # near_ma_strong 아닌 경우: 캔들강도, 거래량 조건 모두 충족해야 함
    base_ok = trend_ok and high_above_ma and pullback_ok and near_ma_ok and bounce_ok and vol_ok
    filters_ok = not f1_fake_trend and not f2_fake_wick and not f3_support_broken

    if near_ma_strong:
        # MA20 바로 근처 = 지지 신뢰도 높음 → 캔들강도 조건 약간 완화(0.50)
        final_strength_ok = strength_pct >= 0.50
    else:
        # MA20에서 좀 떨어진 경우 → 캔들강도 엄격(0.60)
        final_strength_ok = strength_ok

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
                f"vol={vol_ok} "
                f"f1_fake_trend={f1_fake_trend} f2_fake_wick={f2_fake_wick} f3_support_broken={f3_support_broken}"
            )

    return is_valid


def get_pullback_signal_data(candles) -> dict | None:
    """눌림목 진입 시 디스코드 알림용 지표"""
    if len(candles) < 25:
        return None

    c1 = candles[0]
    c2 = candles[1]

    ma20_now = sum(c['close'] for c in candles[0:20]) / 20
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20

    recent_high = max(c['high'] for c in candles[0:10])
    pullback_pct = (c1['close'] - recent_high) / recent_high

    ma_distance = abs(c1['close'] - ma20_now) / ma20_now
    ma_strength = "STRONG" if ma_distance <= 0.020 else "NORMAL"

    pullback_vols = [c['volume'] for c in candles[1:5]]
    avg_pullback_vol = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 1
    vol_ratio = c1['volume'] / avg_pullback_vol if avg_pullback_vol > 0 else 0

    candle_range = c1['high'] - c1['low']
    body_size = c1['close'] - c1['open']
    candle_strength = (body_size / candle_range * 100) if candle_range > 0 else 0

    return {
        "vol_ratio": vol_ratio,
        "trend": "MA20 상승" if ma20_now > ma20_prev else "MA20 하락",
        "breakout": f"눌림목 반등 ({pullback_pct*100:.1f}%)",
        "candle_strength": candle_strength,   # 실제 캔들강도 %
        "ma_distance": ma_distance * 100,
        "ma_strength": ma_strength,
    }