import re
import pandas as pd

def analyze_trade_log(path="logs/trade.log"):
    trades = []
    current = None

    with open(path, encoding="utf-8") as f:
        for line in f:
            if "[BUY]" in line:
                m = re.search(r"\[BUY\] (\w+) (\d+) x(\d+)", line)
                current = {
                    "code": m.group(1),
                    "entry": int(m.group(2)),
                    "exit": []
                }
            elif "[SELL_ALL]" in line and current:
                avg_exit = current["entry"]
                current["pnl"] = (avg_exit - current["entry"]) / current["entry"]
                trades.append(current)
                current = None

    df = pd.DataFrame(trades)
    print(df)
    if not df.empty:
        print("승률:", (df["pnl"] > 0).mean())
        print("평균 수익률:", df["pnl"].mean())
