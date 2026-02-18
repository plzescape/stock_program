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
    고신뢰도 + MA20 추세 추종 전략
    - candles: 이미 kiwoom_api에서 candles[1:]로 슬라이싱되어 넘어온 완성봉 리스트
    - candles[0]: 가장 최근 완성봉
    """
    # print("is_entry_candidate_VER2 called with code:", code, len(candles), "candles")
    # 1. 이동평균선 계산을 위해 최소 25개 이상의 데이터가 필요함
    if len(candles) < 25:
        if logger:
            logger.info(f"[STRATEGY_SKIP] {code} 데이터 부족 (필요:25, 현재:{len(candles)})")
        return False

    # --- 데이터 정의 ---
    c1 = candles[0]          # 직전 완성봉 (기준)
    c2 = candles[1]          # 전전 완성봉
    prev_5_candles = candles[1:6]  # 최근 5개 봉 (평균 거래량용)

    # 2. 이동평균선(MA20) 계산
    # 최근 20개 완성봉의 종가 평균
    ma20_now = sum(c['close'] for c in candles[0:20]) / 20
    # 5봉 전 시점의 MA20 (기울기 확인용)
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20

    # 3. [추가] MA20 정배열 및 추세 조건
    # - 현재 주가가 MA20 위에 있어야 함 (정배열 초입/유지)
    # - MA20의 수치 자체가 5봉 전보다 높아야 함 (우상향 추세)
    trend_ok = (
        c1['close'] > ma20_now and
        ma20_now > ma20_prev
    )

    # 4. 가격 돌파 조건 (직전 고점 돌파 및 양봉)
    price_ok = (
        c1['close'] > c2['high'] and 
        c1['close'] > c1['open']
    )

    # 5. 거래량 조건 (과거 5봉 평균 대비 3배 폭증)
    avg_vol = sum(c['volume'] for c in prev_5_candles) / len(prev_5_candles)
    vol_ok = (
        c1['volume'] > avg_vol * 3.0 and
        c1['volume'] >= 5000
    )

    # 6. 캔들 강도 (윗꼬리가 짧은 장대양봉)
    candle_range = c1['high'] - c1['low']
    body_size = c1['close'] - c1['open']
    strength_ok = (body_size / candle_range) >= 0.7 if candle_range > 0 else False

    # --- 최종 판정 ---
    is_valid = trend_ok and price_ok and vol_ok and strength_ok

    if logger and is_valid:
        logger.info(
            f"[ENTRY_CONFIRMED] {code} | "
            f"종가:{c1['close']} | "
            f"MA20추세:상향({ma20_now:.1f}) | "
            f"거래량:{c1['volume']}(평균의 {c1['volume']/avg_vol:.1f}배)"
        )

    return is_valid


def get_entry_signal_data(candles) -> dict | None:
    """
    is_entry_candidate_VER2와 동일한 조건으로 전략 지표를 계산하여 반환.
    디스코드 알림용. 진입 조건 불충분이면 None 반환.
    """
    if len(candles) < 25:
        return None

    c1 = candles[0]
    c2 = candles[1]
    prev_5_candles = candles[1:6]

    ma20_now = sum(c['close'] for c in candles[0:20]) / 20
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20

    avg_vol = sum(c['volume'] for c in prev_5_candles) / len(prev_5_candles)
    vol_ratio = c1['volume'] / avg_vol if avg_vol > 0 else 0

    candle_range = c1['high'] - c1['low']
    body_size = c1['close'] - c1['open']
    candle_strength = (body_size / candle_range * 100) if candle_range > 0 else 0

    trend = "MA20 상승" if ma20_now > ma20_prev else "MA20 하락"
    breakout = "직전 고점 돌파" if c1['close'] > c2['high'] else "돌파 미달"

    return {
        "vol_ratio": vol_ratio,
        "trend": trend,
        "breakout": breakout,
        "candle_strength": candle_strength,
    }


# ===========================
# 전략: 눌림목 진입 (MA20 지지 반등) - 개선판
# ===========================
def is_pullback_entry(candles, logger=None, code=None) -> bool:
    """
    눌림목 진입 전략 (개선판):

    핵심 철학:
    - 상승 추세 중 조정이 온 뒤, 지지 구간(MA20 or 직전 저점)에서 반등이 확인될 때 진입
    - 진입 조건은 완화하되, 거짓 눌림(하락 추세 착각) 필터를 강화

    조건 구성:
    1. [추세] MA20 우상향 + 최근 고점이 MA20보다 충분히 위 (상승세 확인)
    2. [조정] 고점 대비 -1% ~ -10% 눌림 (기존보다 완화)
    3. [지지] MA20 or 최근 저점 근접 (±3.5% 이내, 기존보다 완화)
    4. [반등] 직전 완성봉 기준 양봉 + 전봉 대비 종가 상승
    5. [캔들] 몸통 비율 ≥ 40% (기존 50%에서 완화, 반등 초기 포착)
    6. [거래량] 조정 구간 대비 반등봉 거래량 ≥ 1.0배 (회복 확인, 기존보다 완화)

    거짓 눌림 필터 (신규 추가):
    F1. 최근 5봉 중 음봉이 3개 이상이면서 종가가 점진 하락 → 하락 추세로 판정, 제외
    F2. 반등봉 윗꼬리가 몸통의 2배 초과 → 매도 압력 강한 페이크 반등, 제외
    F3. 조정 저점이 MA20보다 5% 이상 아래 → 지지선 이탈, 눌림목 아닌 하락 추세, 제외
    """
    if len(candles) < 25:
        if logger:
            logger.info(f"[PULLBACK_SKIP] {code} 데이터 부족 (필요:25, 현재:{len(candles)})")
        return False

    c1 = candles[0]   # 직전 완성봉 (반등 봉)
    c2 = candles[1]   # 전전 봉

    # --- MA20 계산 ---
    ma20_now  = sum(c['close'] for c in candles[0:20]) / 20
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20

    # ──────────────────────────────────────────
    # 1. 추세 조건
    # ──────────────────────────────────────────
    # MA20 우상향 (기울기 양수)
    ma_rising = ma20_now > ma20_prev

    # 최근 10봉 고점이 MA20보다 최소 2% 위 → 실질적인 상승 추세 존재 확인
    recent_high = max(c['high'] for c in candles[0:10])
    high_above_ma = (recent_high - ma20_now) / ma20_now >= 0.02

    trend_ok = ma_rising and high_above_ma

    # ──────────────────────────────────────────
    # 2. 조정 폭 조건 (-1% ~ -10%)
    # ──────────────────────────────────────────
    pullback_pct = (c1['close'] - recent_high) / recent_high
    pullback_ok = -0.10 <= pullback_pct <= -0.01

    # ──────────────────────────────────────────
    # 3. 지지선 근접 조건 (MA20 ±3.5% 이내)
    # ──────────────────────────────────────────
    ma_distance = abs(c1['close'] - ma20_now) / ma20_now
    near_ma_ok = ma_distance <= 0.035

    # ──────────────────────────────────────────
    # 4. 반등 양봉
    # ──────────────────────────────────────────
    bounce_ok = (
        c1['close'] > c2['close'] and   # 전봉 대비 종가 상승
        c1['close'] > c1['open']         # 양봉
    )

    # ──────────────────────────────────────────
    # 5. 캔들 강도 (몸통 ≥ 40%)
    # ──────────────────────────────────────────
    candle_range = c1['high'] - c1['low']
    body_size    = c1['close'] - c1['open']
    strength_ok  = (body_size / candle_range) >= 0.4 if candle_range > 0 else False

    # ──────────────────────────────────────────
    # 6. 거래량 조건 (조정 구간 대비 반등봉 회복)
    # ──────────────────────────────────────────
    pullback_vols = [c['volume'] for c in candles[1:5]]
    avg_pullback_vol = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 1

    # 반등봉 거래량이 조정 평균 이상 (기존보다 완화: 1.0배)
    vol_recovery = c1['volume'] >= avg_pullback_vol * 1.0 and c1['volume'] >= 1000

    # ──────────────────────────────────────────
    # [거짓 눌림 필터 F1] 최근 5봉 연속 하락 추세
    # ──────────────────────────────────────────
    recent5 = candles[0:5]
    bearish_count = sum(1 for c in recent5 if c['close'] < c['open'])  # 음봉 개수
    closes5 = [c['close'] for c in recent5]
    # 종가가 단조 감소(점진 하락)인지 확인
    is_falling = all(closes5[i] <= closes5[i+1] for i in range(len(closes5)-1))  # 인덱스0이 최신
    fake_downtrend = (bearish_count >= 3 and is_falling)

    # ──────────────────────────────────────────
    # [거짓 눌림 필터 F2] 반등봉 윗꼬리 과다
    # 윗꼬리 = high - close, 몸통의 2배 초과면 매도세 강함
    # ──────────────────────────────────────────
    upper_wick = c1['high'] - c1['close']
    fake_wick = (upper_wick > body_size * 2.0) if body_size > 0 else False

    # ──────────────────────────────────────────
    # [거짓 눌림 필터 F3] 조정 저점이 MA20보다 5% 이상 아래 → 지지 이탈
    # ──────────────────────────────────────────
    recent_low = min(c['low'] for c in candles[0:10])
    support_broken = (ma20_now - recent_low) / ma20_now > 0.05

    # ──────────────────────────────────────────
    # 최종 판정
    # ──────────────────────────────────────────
    filter_ok = not fake_downtrend and not fake_wick and not support_broken

    is_valid = (
        trend_ok and
        pullback_ok and
        near_ma_ok and
        bounce_ok and
        strength_ok and
        vol_recovery and
        filter_ok
    )

    if logger:
        if is_valid:
            logger.info(
                f"[PULLBACK_CONFIRMED] {code} | "
                f"종가:{c1['close']} MA20:{ma20_now:.0f} | "
                f"눌림:{pullback_pct*100:.1f}% | "
                f"MA거리:{ma_distance*100:.2f}% | "
                f"반등거래량:{c1['volume']} | "
                f"캔들강도:{body_size/candle_range*100:.0f}%"
            )
        else:
            logger.info(
                f"[PULLBACK_CHECK] {code} "
                f"trend={trend_ok}(rising={ma_rising},high_above={high_above_ma}) "
                f"pullback={pullback_ok}({pullback_pct*100:.1f}%) "
                f"near_ma={near_ma_ok}({ma_distance*100:.2f}%) "
                f"bounce={bounce_ok} strength={strength_ok} vol={vol_recovery} "
                f"f1_fake_trend={fake_downtrend} f2_fake_wick={fake_wick} f3_support_broken={support_broken}"
            )

    return is_valid


def get_pullback_signal_data(candles) -> dict | None:
    """눌림목 진입 시 디스코드 알림용 지표 (개선판)"""
    if len(candles) < 25:
        return None

    c1 = candles[0]

    ma20_now  = sum(c['close'] for c in candles[0:20]) / 20
    ma20_prev = sum(c['close'] for c in candles[5:25]) / 20

    recent_high = max(c['high'] for c in candles[0:10])
    pullback_pct = (c1['close'] - recent_high) / recent_high

    ma_distance = abs(c1['close'] - ma20_now) / ma20_now

    pullback_vols = [c['volume'] for c in candles[1:5]]
    avg_pullback_vol = sum(pullback_vols) / len(pullback_vols) if pullback_vols else 1
    vol_ratio = c1['volume'] / avg_pullback_vol if avg_pullback_vol > 0 else 0

    candle_range = c1['high'] - c1['low']
    body_size    = c1['close'] - c1['open']
    candle_strength = (body_size / candle_range * 100) if candle_range > 0 else 0

    return {
        "vol_ratio": vol_ratio,
        "trend": "MA20 상승" if ma20_now > ma20_prev else "MA20 하락",
        "breakout": f"눌림목 반등 ({pullback_pct*100:.1f}%) MA거리:{ma_distance*100:.1f}%",
        "candle_strength": candle_strength,
    }