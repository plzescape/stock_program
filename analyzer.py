"""로그(trade.log)를 아주 단순하게 요약합니다.
- pandas가 설치되어 있으면 더 상세 분석으로 확장 가능
"""
import re

def analyze_trade_log(path="logs/trade.log"):
    # BUY:  [BUY] code=005930 price=70000 qty=10
    # SELL: [SELL_PARTIAL] ... / [SELL_ALL] ...
    buy_re = re.compile(r"\[BUY\]\s+code=(\w+)\s+price=(\d+)\s+qty=(\d+)")
    sell_re = re.compile(r"\[(SELL_PARTIAL|SELL_ALL)\]\s+code=(\w+)\s+price=(\d+)\s+qty=(\d+)\s+reason=(\w+)")

    trades = []
    current = None

    with open(path, encoding="utf-8") as f:
        for line in f:
            m = buy_re.search(line)
            if m:
                current = {
                    "code": m.group(1),
                    "entry": int(m.group(2)),
                    "qty": int(m.group(3)),
                    "exits": [],
                }
                continue

            m = sell_re.search(line)
            if m and current:
                current["exits"].append(int(m.group(3)))
                if m.group(1) == "SELL_ALL":
                    avg_exit = sum(current["exits"]) / len(current["exits"])
                    pnl = (avg_exit - current["entry"]) / current["entry"]
                    current["pnl"] = pnl
                    trades.append(current)
                    current = None

    if not trades:
        print("매매 기록이 없습니다.")
        return

    total = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    avg = sum(t["pnl"] for t in trades) / total
    print(f"총 매매: {total}")
    print(f"승률: {wins/total:.2%}")
    print(f"평균 수익률: {avg:.2%}")

if __name__ == "__main__":
    analyze_trade_log()
