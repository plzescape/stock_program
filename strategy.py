from datetime import datetime, time

def is_market_time() -> bool:
    # 한국장 기준: 09:00 ~ 15:30
    now = datetime.now().time()
    return time(9, 0) <= now <= time(15, 30)

def is_entry_candidate(candles, logger=None, code=None) -> bool:
    """candles: 최신봉이 index 0 (최근 3개 필요)"""
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
