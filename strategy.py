from datetime import datetime, time

def is_market_time() -> bool:
    # 한국장 기준: 09:00 ~ 15:30
    now = datetime.now().time()
    return time(9, 0) <= now <= time(15, 30)

def is_entry_candidate(candles, logger=None, code=None) -> bool:
    print("🔥 is_entry_candidate CALLED", code)
    if len(candles) < 3:
        print(f"⛔ NOT ENOUGH CANDLES: {len(candles)}")
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

    # 결과 디버깅        
    print(
        f"[ENTRY_CHECK] code={code} "
        f"price_ok={price_ok} vol_ok={vol_ok}"
    )
    return vol_ok and price_ok

