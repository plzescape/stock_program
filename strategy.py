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