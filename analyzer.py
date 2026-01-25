import re
import pandas as pd
from datetime import datetime

LOG_PATH = "logs/trade.log"

# -------------------------------------------------
# 1. 로그 파싱
# -------------------------------------------------
def parse_trade_log(path=LOG_PATH):
    buy_re = re.compile(
        r"\[BUY_FILLED\]\s+code=(\w+)\s+price=(\d+)\s+qty=(\d+)"
    )
    sell_re = re.compile(
        r"\[(STOP_LOSS|TP1|TP2|TRAIL_STOP)\]\s+code=(\w+).*price=(\d+)"
    )

    trades = []
    current = None

    with open(path, encoding="utf-8") as f:
        for line in f:
            ts = line.split("|")[0].strip()
            try:
                ts = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f")
            except:
                continue

            # 매수 체결
            m = buy_re.search(line)
            if m:
                current = {
                    "code": m.group(1),
                    "entry_price": int(m.group(2)),
                    "entry_time": ts,
                    "exit_price": None,
                    "exit_time": None,
                    "exit_type": None,
                }
                continue

            # 매도 (최종 청산 기준)
            m = sell_re.search(line)
            if m and current:
                current["exit_type"] = m.group(1)
                current["exit_price"] = int(m.group(3))
                current["exit_time"] = ts

                trades.append(current)
                current = None

    return pd.DataFrame(trades)

# -------------------------------------------------
# 2. 성과 계산
# -------------------------------------------------
def enrich_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df["return"] = (df["exit_price"] - df["entry_price"]) / df["entry_price"]
    df["holding_sec"] = (df["exit_time"] - df["entry_time"]).dt.total_seconds()
    df["date"] = df["entry_time"].dt.date
    return df

# -------------------------------------------------
# 3. 요약 출력
# -------------------------------------------------
def print_summary(df: pd.DataFrame):
    print("\n===== 전체 요약 =====")
    print("총 매매 수:", len(df))
    print("승률:", f"{(df['return'] > 0).mean():.2%}")
    print("평균 수익률:", f"{df['return'].mean():.2%}")
    print("평균 보유시간(초):", int(df["holding_sec"].mean()))

    print("\n===== 청산 유형 비율 =====")
    print(df["exit_type"].value_counts(normalize=True).round(3))

    print("\n===== 청산 유형별 평균 수익률 =====")
    print(df.groupby("exit_type")["return"].mean().round(4))

    print("\n===== 일자별 성과 =====")
    print(df.groupby("date")["return"].agg(["count", "mean", "sum"]).round(4))

# -------------------------------------------------
# 4. 실행 진입점
# -------------------------------------------------
if __name__ == "__main__":
    df = parse_trade_log()
    if df.empty:
        print("아직 분석할 매매 로그가 없습니다.")
    else:
        df = enrich_metrics(df)
        print_summary(df)
