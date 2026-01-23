from datetime import datetime

def is_entry_candidate(candles, logger=None, code=None):
    if len(candles) < 3:
        return False

    c1, c2, c3 = candles[2], candles[1], candles[0]
    vol_ok = c1["volume"] < c2["volume"] < c3["volume"]
    price_ok = c1["close"] < c2["close"] < c3["close"]

    if logger and code:
        logger.info(
            f"[ENTRY_CHECK] {code} "
            f"close={c1['close']},{c2['close']},{c3['close']} "
            f"vol={c1['volume']},{c2['volume']},{c3['volume']} "
            f"vol_ok={vol_ok} price_ok={price_ok}"
        )

    return vol_ok and price_ok

def is_market_time():
    now = datetime.now().time()
    return now.hour >= 9 and (now.hour < 14 or (now.hour == 14 and now.minute <= 50))
