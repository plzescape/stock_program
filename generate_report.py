"""
자동매매 일일 보고서 생성기 v3
────────────────────────────────────────────────────
v3 변경사항:
  - 진입전략(BREAKOUT / PULLBACK / FLAG) 파싱 추가
  - 거래내역 시트: 종목코드 옆에 '진입전략' 컬럼 추가 (색상 구분)
  - 요약 시트: 청산유형 테이블 아래 '진입전략별 집계' 테이블 추가
────────────────────────────────────────────────────
Usage:
  python generate_report.py                          # trade.log -> 자동매매_보고서.xlsx
  python generate_report.py logs/trade.log
  python generate_report.py trade.log report.xlsx
────────────────────────────────────────────────────
시트 구성:
  1. 요약 & 차트  -- KPI / 청산유형 테이블 / 진입전략별 집계 / 원형 차트 3개
  2. 거래내역     -- 종목별 ATR/손절/TP1/2/트레일링 세부 타임라인 + 진입전략
"""

import re
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import PieChart, Reference
from openpyxl.chart.series import DataPoint


# ──────────────────────────────────────────────
# 1. 로그 파싱
# ──────────────────────────────────────────────
def _parse_cn(token: str):
    m = re.match(r'^(.+?)\(([\dA-Za-z]{5,7})\)$', token.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r'^code=([\dA-Za-z]{5,7})$', token.strip())
    if m:
        return m.group(1), m.group(1)
    return None, None


def parse_log(path: str) -> list[dict]:
    """trade.log 파싱 -> 거래 세션 리스트"""

    re_ts     = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    re_atr    = re.compile(r"\[ATR_CALC\]\s+(.+?\([\dA-Za-z]{5,7}\))\s+ATR=([\d.]+)원")
    re_atr_old= re.compile(r"\[ATR_CALC\]\s+code=([\dA-Za-z]{5,7})\s+ATR=([\d.]+)원")
    re_buy    = re.compile(r"\[BUY_FILL_NEW\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+price=(\d+)$")
    re_qty    = re.compile(r"\[BUY_DONE\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+잔여=(\d+)/(\d+)")
    re_tp     = re.compile(
        r"\[TP_TARGET_SET\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+ATR=([\d.]+)원\s+손절=(\d+)\s+TP1=(\d+)\s+TP2\(초기\)=(\d+)"
    )
    re_tp1    = re.compile(r"\[TP1_FILLED\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+잔여=(\d+)주")
    re_tp2    = re.compile(r"\[TP2_FILLED\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+잔여=(\d+)주")
    re_trail  = re.compile(r"\[TRAIL_STOP\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+")
    re_sell   = re.compile(r"\[ORDER_TRY\]\s+방향=SELL\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+수량=(\d+)주\s+사유=(\S+)")
    re_done   = re.compile(r"\[SELL_DONE\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+전량매도완료")
    re_psafe  = re.compile(r"\[PROFIT_SAFEGUARD\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+본절보호")
    re_emerg  = re.compile(r"\[STOP_LOSS_EMERGENCY\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+비상 손절 현재가:(\d+)\s+pnl=([-\d.]+)")
    re_candle = re.compile(r"\[STOP_LOSS_CANDLE\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+완성봉 ATR손절 종가:(\d+)\s+손절기준:\d+\s+pnl=([-\d.]+)")
    re_time   = re.compile(r"\[TIME_STOP\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+보유=\d+초\s+매도수량=\d+주\s+pnl=([-\d.]+)")
    re_vol    = re.compile(r"\[VOL_TIME_STOP\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+구간=\S+\s+매도수량=\d+주.*pnl=([-\d.]+)")
    # 매도 CHEJAN: price + qty (가중평균 체결가 계산용)
    re_chejan     = re.compile(r"\[CHEJAN\]\s+-매도\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+price=(\d+)(?:\s+qty=(\d+))?")
    # 매수 CHEJAN: price + qty (가중평균 매수가 재계산용)
    re_chejan_buy = re.compile(r"\[CHEJAN\]\s+\+매수\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+price=(\d+)(?:\s+qty=(\d+))?")
    # [FIX4] 전일잔고 청산: LEFTOVER_SELL → 세션 생성용
    # 형식: [LEFTOVER_SELL] 종목명(코드) 전일잔고 qty=N entry=N entry_ts=...
    re_leftover = re.compile(
        r"\[LEFTOVER_SELL\]\s+(.+?\([\dA-Za-z]{5,7}\))\s+전일잔고\s+qty=(\d+)\s+entry=(\d+)"
    )
    # 형식: [ENTRY_QTY] 종목명(코드) 현재가=N 전략=BREAKOUT|PULLBACK|FLAG ...
    re_entry_qty = re.compile(
        r"\[ENTRY_QTY\]\s+(.+?\([\dA-Za-z]{5,7}\)|code=[\dA-Za-z]{5,7})\s+현재가=\d+\s+전략=(BREAKOUT|PULLBACK|FLAG)"
    )

    def extract_code(token: str):
        m = re.match(r'^(.+?)\(([\dA-Za-z]{5,7})\)$', token.strip())
        if m:
            return m.group(1).strip(), m.group(2)
        m = re.match(r'^code=([\dA-Za-z]{5,7})$', token.strip())
        if m:
            return m.group(1), m.group(1)
        return token, token

    def ts(line):
        m = re_ts.match(line)
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None

    sessions: dict[str, dict] = {}
    closed: list[dict] = []

    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip()
            t = ts(line)

            # ── 진입전략 감지: ENTRY_QTY 로그 파싱 ──
            m = re_entry_qty.search(line)
            if m:
                _, code = extract_code(m.group(1))
                sessions.setdefault(code, {})["entry_strategy"] = m.group(2)
                continue

            # [FIX4] 전일잔고 청산 세션 생성
            # LEFTOVER_SELL → 전일 매수 포지션을 당일 청산하는 케이스
            # 형식: [LEFTOVER_SELL] 종목명(코드) 전일잔고 qty=N entry=N
            m = re_leftover.search(line)
            if m:
                name, code = extract_code(m.group(1))
                qty_val    = int(m.group(2))
                entry_val  = int(m.group(3))
                s = sessions.setdefault(code, {})
                s["name"]            = name
                s["entry_price"]     = entry_val
                s["qty"]             = qty_val
                s["entry_time"]      = t
                s["entry_strategy"]  = "LEFTOVER"
                s["exit_type"]       = "전일잔고청산"
                continue

            # ATR_CALC
            m = re_atr.search(line) or re_atr_old.search(line)
            if m:
                if re_atr.search(line):
                    name, code = extract_code(m.group(1))
                else:
                    code = m.group(1); name = code
                sessions.setdefault(code, {})
                sessions[code]["atr"]      = float(m.group(2))
                sessions[code]["name"]     = name
                sessions[code]["atr_time"] = t
                continue

            # BUY_FILL_NEW price
            m = re_buy.search(line)
            if m:
                name, code = extract_code(m.group(1))
                s = sessions.setdefault(code, {})
                s["entry_price"] = int(m.group(2))
                s["entry_time"]  = t
                s.setdefault("name", name)
                continue

            # 매수 CHEJAN → 가중평균 매수가 추적
            # [FIX] qty는 누적 체결수량 → 증분(delta)으로 계산
            m = re_chejan_buy.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    bp  = int(m.group(2))
                    bq  = int(m.group(3)) if m.group(3) else 1
                    s2  = sessions[code]
                    prev_order_qty = s2.get("_buy_order_qty", 0)
                    if bq > prev_order_qty:
                        delta = bq - prev_order_qty
                        s2["_buy_price_sum"] = s2.get("_buy_price_sum", 0) + bp * delta
                        s2["_buy_qty_sum"]   = s2.get("_buy_qty_sum",   0) + delta
                    else:
                        s2["_buy_price_sum"] = s2.get("_buy_price_sum", 0) + bp * bq
                        s2["_buy_qty_sum"]   = s2.get("_buy_qty_sum",   0) + bq
                    s2["_buy_order_qty"] = bq
                    s2["entry_price"]    = s2["_buy_price_sum"] // s2["_buy_qty_sum"]
                continue

            # BUY_DONE qty
            m = re_qty.search(line)
            if m:
                name, code = extract_code(m.group(1))
                s = sessions.setdefault(code, {})
                s["qty"] = int(m.group(2))
                s.setdefault("name", name)
                s.setdefault("entry_time", t)
                continue

            # TP_TARGET_SET
            m = re_tp.search(line)
            if m:
                _, code = extract_code(m.group(1))
                s = sessions.setdefault(code, {})
                s["stop_loss"] = int(m.group(3))
                s["tp1"]       = int(m.group(4))
                s["tp2"]       = int(m.group(5))
                continue

            # TP1
            m = re_tp1.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code].setdefault("events", []).append({"type": "TP1", "time": t})
                continue

            # TP2
            m = re_tp2.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code].setdefault("events", []).append({"type": "TP2", "time": t})
                continue

            # TRAIL_STOP
            m = re_trail.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code].setdefault("events", []).append({"type": "TRAIL", "time": t})
                continue

            # PROFIT_SAFEGUARD
            m = re_psafe.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code].setdefault("events", []).append({"type": "PROFIT_SAFE", "time": t})
                continue

            # 비상손절 pnl
            m = re_emerg.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code]["exit_pnl_pct"]       = float(m.group(3))
                    sessions[code]["exit_reason_detail"]  = "비상손절(-2.5%)"
                continue

            # ATR 완성봉 손절
            m = re_candle.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code]["exit_pnl_pct"]       = float(m.group(3))
                    sessions[code]["exit_reason_detail"]  = "ATR손절(완성봉)"
                continue

            # 타임스탑 pnl
            m = re_time.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code]["exit_pnl_pct"] = float(m.group(2))
                continue

            # 거래량급감
            m = re_vol.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code]["exit_pnl_pct"]       = float(m.group(2))
                    sessions[code]["exit_reason_detail"]  = "거래량급감청산"
                continue

            # 매도체결가 추적 → 가중평균 체결가 계산
            # [FIX] CHEJAN qty는 주문 내 누적 체결수량
            # 같은 주문 내에서 qty가 증가하는 동안은 증분(delta)으로 계산
            # 새 주문(TP2, TRAIL 등)이 시작되면 qty가 다시 작은 값으로 리셋됨
            m = re_chejan.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sp  = int(m.group(2))
                    sq  = int(m.group(3)) if m.group(3) else 1
                    s2  = sessions[code]
                    prev_order_qty = s2.get("_sell_order_qty", 0)
                    if sq > prev_order_qty:
                        # 같은 주문 내 연속 체결 → 증분만 합산
                        delta = sq - prev_order_qty
                        s2["_sell_price_sum"] = s2.get("_sell_price_sum", 0) + sp * delta
                        s2["_sell_qty_sum"]   = s2.get("_sell_qty_sum",   0) + delta
                    else:
                        # qty 리셋 = 새 매도 주문 시작 (TP2, TRAIL 등)
                        s2["_sell_price_sum"] = s2.get("_sell_price_sum", 0) + sp * sq
                        s2["_sell_qty_sum"]   = s2.get("_sell_qty_sum",   0) + sq
                    s2["_sell_order_qty"] = sq
                    s2["exit_price"] = s2["_sell_price_sum"] // s2["_sell_qty_sum"]
                continue

            # ORDER_TRY SELL
            m = re_sell.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code]["sell_reason"] = m.group(3)
                    sessions[code]["sell_qty"]    = int(m.group(2))
                    sessions[code]["sell_time"]   = t
                continue

            # SELL_DONE -> 세션 종료
            m = re_done.search(line)
            if m:
                name, code = extract_code(m.group(1))
                if code not in sessions:
                    continue
                s = sessions.pop(code)
                s["code"] = code
                s.setdefault("name", name)
                s.setdefault("exit_price", s.get("entry_price", 0))
                s["exit_time"] = t

                # 청산유형
                reason = s.get("sell_reason", "")
                if "STOP_LOSS" in reason:
                    s["exit_type"] = "손절"
                elif "TIME_STOP" in reason or "VOL_TIME_STOP" in reason:
                    s["exit_type"] = "타임스탑"
                elif "MINI_TRAIL_STOP" in reason:
                    s["exit_type"] = "미니트레일"
                elif "TRAIL_STOP" in reason:
                    s["exit_type"] = "트레일링"
                elif "PROFIT_SAFE" in reason:
                    s["exit_type"] = "본절보호"
                elif "LEFTOVER_LIQUIDATION" in reason:
                    s["exit_type"] = "전일잔고청산"
                else:
                    evts = [e["type"] for e in s.get("events", [])]
                    if "TP2" in evts:
                        s["exit_type"] = "TP2+트레일링"
                    elif "TP1" in evts:
                        s["exit_type"] = "TP1+타임스탑"
                    else:
                        s["exit_type"] = reason or "기타"

                # ── 손익 계산 (수수료/세금 반영) ─────────────────────
                # 실제 손익 = (매도금액 - 매수금액) - 거래비용
                #
                # [FIX] 키움 모의투자 실제 수수료율 역산 결과 (2026-03-16 검증)
                # HTS 수수료+세세금 컬럼값 vs 실제 손익 차감액 비교 → 0.4505%
                # 구성: 수수료(매수 0.015% + 매도 0.015%) + 증권거래세 + 농특세
                # 모의투자 특성상 세율이 단순 합산되어 (매수+매도) × 0.4505%로 근사
                # 기존 0.246%는 실제의 절반 수준으로 손익이 과대계상되는 오류
                #
                # 전일잔고청산(LEFTOVER): 매수 수수료는 전일에 이미 차감됨
                # → 오늘 매도분 수수료만 적용: sell_amt × 0.4505%
                ep  = s.get("entry_price", 0)
                xp  = s.get("exit_price", ep)
                qty = s.get("qty", 0)

                buy_amt   = ep * qty
                sell_amt  = xp * qty
                gross_pnl = sell_amt - buy_amt        # 세전 손익

                is_leftover = (s.get("entry_strategy") == "LEFTOVER")

                # ━━ 수수료 계산 ━━
                # 모의투자: 키움 모의 고정 0.4505% 왕복 (거래세 없음)
                # 실전투자: 매수 0.019960% + 매도 0.019960% + 증권거래세 0.18%
                #   (키움 실전 기본 수수료 0.015% + 유관기관비 약 0.00396%)
                USE_REAL_FEE = False  # True=실전 수수료, False=모의 수수료

                if is_leftover:
                    if USE_REAL_FEE:
                        total_fee = round(sell_amt * (0.00015 + 0.0018))  # 매도만
                    else:
                        total_fee = round(sell_amt * 0.004505)             # 모의 매도만
                else:
                    if USE_REAL_FEE:
                        buy_fee  = round(buy_amt  * 0.00015)               # 매수 수수료
                        sell_fee = round(sell_amt * (0.00015 + 0.0018))    # 매도 수수료+거래세
                        total_fee = buy_fee + sell_fee
                    else:
                        total_fee = round((buy_amt + sell_amt) * 0.004505) # 모의 왕복

                net_pnl       = gross_pnl - total_fee
                s["pnl_amt"]   = net_pnl
                s["pnl_pct"]   = net_pnl / (ep * qty) if (ep and qty) else 0
                s["gross_pnl"] = gross_pnl
                s["total_fee"] = total_fee

                closed.append(s)

    return closed


# ──────────────────────────────────────────────
# 2. 스타일 헬퍼
# ──────────────────────────────────────────────
FONT_NAME = "맑은 고딕"

def _font(bold=False, size=10, color="000000"):
    return Font(name=FONT_NAME, bold=bold, size=size, color=color)

def _fill(hex_color):
    return PatternFill("solid", fgColor=hex_color)

def _border():
    s = Side(style="thin", color="CCCCCC")
    return Border(left=s, right=s, top=s, bottom=s)

def _align(h="center", v="center", wrap=False):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)

def _apply_header(ws, row, cols, labels, bg="1F3864", fg="FFFFFF", height=22):
    ws.row_dimensions[row].height = height
    for col, lbl in zip(cols, labels):
        c = ws.cell(row=row, column=col, value=lbl)
        c.font      = _font(bold=True, size=9, color=fg)
        c.fill      = _fill(bg)
        c.alignment = _align(wrap=True)
        c.border    = _border()

def _color_pnl(cell, val):
    if isinstance(val, (int, float)):
        cell.font = _font(color="0070C0" if val >= 0 else "FF0000")


# ──────────────────────────────────────────────
# 3. 거래 상세 시트
# ──────────────────────────────────────────────

# 진입전략 색상
STRATEGY_COLORS = {
    "BREAKOUT": "C55A11",   # 주황
    "PULLBACK": "0070C0",   # 파랑
    "FLAG":     "00B050",   # 초록
}

# 헤더 (col 4에 '진입전략' 삽입 -> 기존 컬럼 1씩 밀림)
DETAIL_HEADERS = [
    "No", "종목명", "종목코드",
    "진입전략",                          # col 4 (신규)
    "매수시각", "매수가", "수량", "매수금액",
    "ATR", "손절기준", "TP1목표", "TP2목표",
    "TP1\n체결", "TP2\n체결", "트레일링\n발동",
    "청산유형", "청산시각", "청산가",
    "손익금액\n(수수료후)", "거래비용\n(원)", "수익률(%)",
    "보유시간"
]

# 컬럼 인덱스 상수 (1-based)
COL_STRATEGY  = 4
COL_ENTRY_T   = 5
COL_ENTRY_P   = 6
COL_QTY       = 7
COL_AMOUNT    = 8
COL_ATR       = 9
COL_SL        = 10
COL_TP1       = 11
COL_TP2       = 12
COL_TP1_HIT   = 13
COL_TP2_HIT   = 14
COL_TRAIL_HIT = 15
COL_EXIT_TYPE = 16
COL_EXIT_T    = 17
COL_EXIT_P    = 18
COL_PNL_AMT   = 19
COL_FEE       = 20   # 거래비용(수수료+세금)
COL_PNL_PCT   = 21
COL_HOLD_TIME = 22


def build_detail_sheet(ws, trades: list[dict]):
    ws.title = "거래내역"
    ws.sheet_view.showGridLines = True

    col_widths = [5, 16, 10, 11, 18, 9, 7, 13, 8, 9, 9, 9,
                  7, 7, 8, 14, 18, 9, 14, 12, 10, 10]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    _apply_header(ws, 1, range(1, len(DETAIL_HEADERS)+1), DETAIL_HEADERS, height=32)

    for i, t in enumerate(trades, 2):
        ws.row_dimensions[i].height = 18
        events  = t.get("events", [])
        entry_p = t.get("entry_price", 0)
        qty     = t.get("qty", 0)

        has_tp1   = any(e["type"] == "TP1"   for e in events)
        has_tp2   = any(e["type"] == "TP2"   for e in events)
        has_trail = any(e["type"] == "TRAIL" for e in events)

        strategy = t.get("entry_strategy", "-")

        row_data = [
            i - 1,
            t.get("name", t.get("code", "")),
            t.get("code", ""),
            strategy,                                                            # col 4: 진입전략
            t["entry_time"].strftime("%H:%M:%S") if t.get("entry_time") else "",
            entry_p,
            qty,
            entry_p * qty,
            t.get("atr", ""),
            t.get("stop_loss", ""),
            t.get("tp1", ""),
            t.get("tp2", ""),
            "V" if has_tp1   else "",
            "V" if has_tp2   else "",
            "V" if has_trail else "",
            t.get("exit_type", ""),
            t["exit_time"].strftime("%H:%M:%S") if t.get("exit_time") else "",
            t.get("exit_price", ""),
            t.get("pnl_amt", 0),
            t.get("total_fee", 0),          # 거래비용(수수료+세금)
            t.get("pnl_pct", 0),
            "",
        ]

        for col, val in enumerate(row_data, 1):
            c = ws.cell(row=i, column=col, value=val)
            c.font      = _font(size=9)
            c.border    = _border()
            c.alignment = _align(h="center")

        # 보유시간
        if t.get("entry_time") and t.get("exit_time"):
            secs = int((t["exit_time"] - t["entry_time"]).total_seconds())
            ws.cell(row=i, column=COL_HOLD_TIME, value=f"{secs//60}분 {secs%60}초")

        # 숫자 포맷
        for col, fmt in [
            (COL_ENTRY_P, "#,##0"),
            (COL_AMOUNT,  "#,##0"),
            (COL_ATR,     "#,##0.0"),
            (COL_SL,      "#,##0"),
            (COL_TP1,     "#,##0"),
            (COL_TP2,     "#,##0"),
            (COL_EXIT_P,  "#,##0"),
        ]:
            ws.cell(row=i, column=col).number_format = fmt

        pnl_c = ws.cell(row=i, column=COL_PNL_AMT)
        fee_c = ws.cell(row=i, column=COL_FEE)
        pct_c = ws.cell(row=i, column=COL_PNL_PCT)
        pnl_c.number_format = '#,##0;(#,##0);"-"'
        fee_c.number_format = '#,##0'
        pct_c.number_format = '0.00%;(0.00%);"-"'
        _color_pnl(pnl_c, t.get("pnl_amt", 0))
        # 거래비용은 항상 회색 (비용이므로)
        fee_c.font = _font(size=9, color="808080")
        _color_pnl(pct_c, t.get("pnl_amt", 0))

        if i % 2 == 0:
            for col in range(1, len(DETAIL_HEADERS)+1):
                ws.cell(row=i, column=col).fill = _fill("F5F7FA")

        # 진입전략 색상 (신규)
        sc = ws.cell(row=i, column=COL_STRATEGY)
        clr = STRATEGY_COLORS.get(strategy)
        if clr:
            sc.font = _font(bold=True, color=clr)

        # V 색상
        for col in [COL_TP1_HIT, COL_TP2_HIT, COL_TRAIL_HIT]:
            c = ws.cell(row=i, column=col)
            if c.value == "V":
                c.font = _font(bold=True, color="00B050")

        # 청산유형 색상
        ec = ws.cell(row=i, column=COL_EXIT_TYPE)
        ev = ec.value or ""
        if "손절" in ev:
            ec.font = _font(bold=True, color="FF0000")
        elif "TP" in ev or "트레일" in ev:
            ec.font = _font(bold=True, color="0070C0")
        elif "타임" in ev:
            ec.font = _font(bold=True, color="7030A0")

    # 합계행
    last = len(trades) + 1
    sr   = last + 1
    ws.row_dimensions[sr].height = 20
    ws.cell(sr, 2, "합   계").font = _font(bold=True, size=9)
    ws.cell(sr, 2).alignment = _align()

    pnl_letter = get_column_letter(COL_PNL_AMT)
    sc = ws.cell(sr, COL_PNL_AMT, value=f"=SUM({pnl_letter}2:{pnl_letter}{last})")
    sc.font = _font(bold=True, size=10)
    sc.number_format = '#,##0;(#,##0);"-"'
    _color_pnl(sc, sum(t.get("pnl_amt", 0) for t in trades))

    for col in range(1, len(DETAIL_HEADERS)+1):
        ws.cell(sr, col).border    = _border()
        ws.cell(sr, col).fill      = _fill("E8EEF7")
        ws.cell(sr, col).alignment = _align()

    ws.freeze_panes = "A2"


# ──────────────────────────────────────────────
# 4. 요약·차트 시트
# ──────────────────────────────────────────────
COLORS_MAP = {
    "손절":          "FF4444",
    "타임스탑":       "AA66CC",
    "TP1+타임스탑":   "5B9BD5",
    "TP2+트레일링":   "0070C0",
    "트레일링":       "00B0F0",
    "본절보호":       "FFC000",
    "거래량급감청산": "00B050",
    "전일잔고청산":   "A9A9A9",
    "미니트레일":     "00CED1",
    "기타":           "808080",
}


def _make_pie(ws, data_ref, label_ref, title, colors_order, w=13, h=14):
    from openpyxl.chart.label import DataLabel, DataLabelList
    from openpyxl.chart.layout import Layout

    pie = PieChart()
    pie.style = 10

    from openpyxl.chart.title import Title
    from openpyxl.chart.text import RichText
    from openpyxl.drawing.text import (RichTextProperties, Paragraph,
                                        ParagraphProperties, RegularTextRun,
                                        CharacterProperties)
    body = RichTextProperties()
    cp   = CharacterProperties(b=True, sz=1100)
    rtr  = RegularTextRun(t=title, rPr=cp)
    para = Paragraph(r=[rtr], pPr=ParagraphProperties(algn="ctr"))
    rt   = RichText(bodyPr=body, p=[para])
    from openpyxl.chart.text import Text
    pie.title = Title(tx=Text(rich=rt))

    pie.add_data(data_ref)
    pie.set_categories(label_ref)
    pie.series[0].title = None

    from openpyxl.chart.legend import Legend
    leg = Legend()
    leg.position = "b"   # 하단 배치 → 제목과 겹침 방지
    pie.legend = leg

    dll = DataLabelList()
    dll.showPercent   = True
    dll.showVal       = False
    dll.showCatName   = False
    dll.showSerName   = False
    dll.showLegendKey = False
    dll.dLblPos = "outEnd"   # 파이 바깥쪽 배치 → 겹침 방지
    from openpyxl.chart.data_source import NumFmt
    dll.numFmt = NumFmt(formatCode="0%", sourceLinked=False)
    pie.series[0].dLbls = dll

    for idx, clr in enumerate(colors_order):
        dp = DataPoint(idx=idx)
        dp.graphicalProperties.solidFill = clr
        pie.series[0].dPt.append(dp)

    pie.width  = w
    pie.height = h
    return pie


def build_summary_sheet(ws, ws_detail, trades: list[dict]):
    ws.title = "요약 & 차트"
    ws.sheet_view.showGridLines = False

    for col, w in zip("ABCDEFGHIJK", [2, 22, 22, 22, 22, 22, 22, 22, 22, 1, 1]):
        ws.column_dimensions[col].width = w

    n        = len(trades)
    det_last = n + 1

    # 진입전략 컬럼 삽입으로 손익금액=S열(19), 수익률=T열(20)
    pnl_col = get_column_letter(COL_PNL_AMT)   # S
    pct_col = get_column_letter(COL_PNL_PCT)    # T

    # ── 타이틀 (행 2~3) ──
    ws.merge_cells("B2:F2")
    tc = ws["B2"]
    tc.value     = "자동매매 일일 성과 보고서"
    tc.font      = Font(name=FONT_NAME, bold=True, size=16, color="1F3864")
    tc.alignment = _align(h="left")
    ws.row_dimensions[2].height = 36

    trade_date = trades[0]["entry_time"].strftime("%Y년 %m월 %d일") if trades else ""
    ws.merge_cells("B3:F3")
    sc = ws["B3"]
    sc.value     = f"운용일자: {trade_date}  |  모의투자 (키움증권)"
    sc.font      = _font(size=10, color="595959")
    sc.alignment = _align(h="left")
    ws.row_dimensions[3].height = 16
    ws.row_dimensions[4].height = 8

    # ── KPI 박스 (행 5~6) ──
    def kpi(col, label, formula, fmt="#,##0"):
        ws.row_dimensions[5].height = 26
        ws.row_dimensions[6].height = 32
        lb = ws.cell(5, col, value=label)
        lb.font = _font(bold=True, size=9, color="FFFFFF")
        lb.fill = _fill("1F3864"); lb.alignment = _align(); lb.border = _border()
        vl = ws.cell(6, col, value=formula)
        vl.font = Font(name=FONT_NAME, bold=True, size=14, color="1F3864")
        vl.fill = _fill("EBF0FA"); vl.alignment = _align()
        vl.number_format = fmt; vl.border = _border()
        return vl

    kpi(2, "총 거래 수",
        f"=COUNTA('거래내역'!A2:A{det_last})", "#,##0")
    net_cell = kpi(3, "순 손익 (원)",
        f"=SUM('거래내역'!{pnl_col}2:{pnl_col}{det_last})", '#,##0;(#,##0);-')
    kpi(4, "승률",
        f"=COUNTIF('거래내역'!{pnl_col}2:{pnl_col}{det_last},\">0\")"
        f"/COUNTA('거래내역'!A2:A{det_last})", "0.0%")
    kpi(5, "평균 수익률",
        f"=AVERAGE('거래내역'!{pct_col}2:{pct_col}{det_last})", "0.00%")

    net_val = sum(t.get("pnl_amt", 0) for t in trades)
    net_cell.font = Font(name=FONT_NAME, bold=True, size=14,
                         color="0070C0" if net_val >= 0 else "FF0000")

    ws.row_dimensions[7].height = 10

    # ── 청산유형 집계 테이블 (행 8~) ──
    _apply_header(ws, 8, [2, 3, 4, 5],
                  ["청산유형", "건수", "손익합계(원)", "평균수익률"],
                  bg="2E4057", height=22)

    exit_types: dict[str, dict] = {}
    for t in trades:
        et = t.get("exit_type", "기타")
        if et not in exit_types:
            exit_types[et] = {"count": 0, "pnl": 0.0, "pcts": []}
        exit_types[et]["count"] += 1
        exit_types[et]["pnl"]   += t.get("pnl_amt", 0)
        exit_types[et]["pcts"].append(t.get("pnl_pct", 0))

    tbl_start = 9
    for ro, (etype, v) in enumerate(exit_types.items()):
        r       = tbl_start + ro
        avg_pct = sum(v["pcts"]) / len(v["pcts"]) if v["pcts"] else 0
        ws.row_dimensions[r].height = 18
        for col, val in [(2, etype), (3, v["count"]), (4, v["pnl"]), (5, avg_pct)]:
            c = ws.cell(r, col, value=val)
            c.font = _font(size=9); c.border = _border(); c.alignment = _align()
            if ro % 2 == 1:
                c.fill = _fill("F5F7FA")
        ws.cell(r, 4).number_format = '#,##0;(#,##0);-'
        ws.cell(r, 5).number_format = '0.00%;(0.00%);-'
        _color_pnl(ws.cell(r, 4), v["pnl"])
        _color_pnl(ws.cell(r, 5), v["pnl"])
        ec = ws.cell(r, 2)
        if "손절" in etype:
            ec.font = _font(bold=True, color="FF0000")
        elif "TP" in etype or "트레일" in etype:
            ec.font = _font(bold=True, color="0070C0")
        elif "타임" in etype:
            ec.font = _font(bold=True, color="7030A0")

    tbl_end   = tbl_start + len(exit_types) - 1
    et_colors = [COLORS_MAP.get(et, "808080") for et in exit_types]
    ws.row_dimensions[tbl_end + 1].height = 10  # 여백

    # ── 진입전략별 집계 테이블 (v3 신규) ──────────────
    strat_hdr = tbl_end + 2
    _apply_header(ws, strat_hdr, [2, 3, 4, 5],
                  ["진입전략", "건수", "손익합계(원)", "평균수익률"],
                  bg="2E5E2E", height=22)

    strategy_types: dict[str, dict] = {}
    for t in trades:
        st = t.get("entry_strategy", "미확인")
        if st not in strategy_types:
            strategy_types[st] = {"count": 0, "pnl": 0.0, "pcts": []}
        strategy_types[st]["count"] += 1
        strategy_types[st]["pnl"]   += t.get("pnl_amt", 0)
        strategy_types[st]["pcts"].append(t.get("pnl_pct", 0))

    for ro, (stype, v) in enumerate(strategy_types.items()):
        r       = strat_hdr + 1 + ro
        avg_pct = sum(v["pcts"]) / len(v["pcts"]) if v["pcts"] else 0
        ws.row_dimensions[r].height = 18
        for col, val in [(2, stype), (3, v["count"]), (4, v["pnl"]), (5, avg_pct)]:
            c = ws.cell(r, col, value=val)
            c.font = _font(size=9); c.border = _border(); c.alignment = _align()
            if ro % 2 == 1:
                c.fill = _fill("F5F7FA")
        ws.cell(r, 4).number_format = '#,##0;(#,##0);-'
        ws.cell(r, 5).number_format = '0.00%;(0.00%);-'
        _color_pnl(ws.cell(r, 4), v["pnl"])
        _color_pnl(ws.cell(r, 5), v["pnl"])
        clr = STRATEGY_COLORS.get(stype, "808080")
        ws.cell(r, 2).font = _font(bold=True, color=clr)

    strat_end = strat_hdr + len(strategy_types)
    ws.row_dimensions[strat_end + 1].height = 10  # 여백

    # ── 이익/손실 요약 바 ──
    total_profit = sum(t.get("pnl_amt", 0) for t in trades if t.get("pnl_amt", 0) > 0)
    total_loss   = abs(sum(t.get("pnl_amt", 0) for t in trades if t.get("pnl_amt", 0) < 0))
    net          = total_profit - total_loss

    hdr_row = strat_end + 2
    val_row = strat_end + 3
    ws.row_dimensions[hdr_row].height = 22
    ws.row_dimensions[val_row].height = 26

    _apply_header(ws, hdr_row, [2, 3, 4, 5],
                  ["", "총 이익 (원)", "총 손실 (원)", "순 손익 (원)"],
                  bg="1F3864", height=22)

    summary_fills = {2: "2B5BA8", 3: "0070C0", 4: "C00000",
                     5: "0070C0" if net >= 0 else "C00000"}
    for col, val in [(2, "금액 합계"), (3, total_profit),
                     (4, -total_loss), (5, net)]:
        c = ws.cell(val_row, col, value=val)
        c.font      = _font(bold=True, size=11, color="FFFFFF")
        c.fill      = _fill(summary_fills[col])
        c.alignment = _align(); c.border = _border()
        if col in (3, 4, 5):
            c.number_format = '#,##0;(#,##0);-'

    # ── 거래비용 요약 행 ──
    total_fee_all = sum(t.get("total_fee", 0) for t in trades)
    total_gross   = sum(t.get("gross_pnl", 0) for t in trades)
    fee_row = val_row + 1
    ws.row_dimensions[fee_row].height = 18
    fee_labels = ["거래비용 내역", f"세전손익: {total_gross:+,}원",
                  f"총비용: -{total_fee_all:,}원",
                  f"순손익: {net:+,}원"]
    for col, val in enumerate(fee_labels, 2):
        c = ws.cell(fee_row, col, value=val)
        c.font      = _font(size=8, color="595959")
        c.fill      = _fill("F5F7FA")
        c.alignment = _align(h="center")
        c.border    = _border()

    # ── 보조 데이터 (G/H열, 차트 소스) ──
    AUX = 7

    def aux(row, col, val):
        c = ws.cell(row, col, value=val)
        c.font = _font(size=7, color="EEEEEE")
        c.fill = _fill("FFFFFF")

    for ro, (et, v) in enumerate(exit_types.items()):
        aux(2 + ro, AUX, et)
        aux(2 + ro, AUX+1, v["count"])
    cnt_s = 2; cnt_e = 2 + len(exit_types) - 1

    base2 = cnt_e + 2
    for ro, (et, v) in enumerate(exit_types.items()):
        aux(base2 + ro, AUX, et)
        aux(base2 + ro, AUX+1, abs(v["pnl"]))
    pnl_s = base2; pnl_e = base2 + len(exit_types) - 1

    base3 = pnl_e + 2
    aux(base3,   AUX, "총 이익"); aux(base3,   AUX+1, total_profit)
    aux(base3+1, AUX, "총 손실"); aux(base3+1, AUX+1, total_loss)

    # ── 차트 3개 ──
    # fee_row(거래비용 요약) 다음 행부터 차트 배치
    ws.row_dimensions[fee_row + 1].height = 8   # 여백
    chart_row = str(fee_row + 2)

    pie1 = _make_pie(
        ws,
        Reference(ws, min_col=AUX+1, min_row=cnt_s,  max_row=cnt_e),
        Reference(ws, min_col=AUX,   min_row=cnt_s,  max_row=cnt_e),
        "청산유형 건수 비율", et_colors,
    )
    ws.add_chart(pie1, f"B{chart_row}")

    pie2 = _make_pie(
        ws,
        Reference(ws, min_col=AUX+1, min_row=pnl_s,  max_row=pnl_e),
        Reference(ws, min_col=AUX,   min_row=pnl_s,  max_row=pnl_e),
        "청산유형 손익금액 비율", et_colors,
    )
    ws.add_chart(pie2, f"F{chart_row}")

    pie3 = _make_pie(
        ws,
        Reference(ws, min_col=AUX+1, min_row=base3,   max_row=base3+1),
        Reference(ws, min_col=AUX,   min_row=base3,   max_row=base3+1),
        f"이익 vs 손실  (이익 {total_profit:,}원 / 손실 {total_loss:,}원)",
        ["0070C0", "FF4444"],
    )
    ws.add_chart(pie3, f"J{chart_row}")


# ──────────────────────────────────────────────
# 5. 메인
# ──────────────────────────────────────────────
def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else "trade.log"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "자동매매_보고서.xlsx"

    print(f"[파싱] {log_path}")
    trades = parse_log(log_path)
    trades = [t for t in trades if t.get("entry_price") and t.get("exit_time")]
    print(f"  -> 완성된 거래 {len(trades)}건")

    if not trades:
        print("분석할 거래가 없습니다.")
        return

    # 진입전략 통계 출력
    strat_cnt: dict[str, int] = {}
    for t in trades:
        s = t.get("entry_strategy", "미확인")
        strat_cnt[s] = strat_cnt.get(s, 0) + 1
    print(f"  -> 진입전략: {strat_cnt}")

    wb        = Workbook()
    ws_sum    = wb.active
    ws_detail = wb.create_sheet("거래내역")
    wb.move_sheet("거래내역", offset=1)

    build_detail_sheet(ws_detail, trades)
    build_summary_sheet(ws_sum, ws_detail, trades)

    wb.save(out_path)
    print(f"[저장] {out_path}")


if __name__ == "__main__":
    main()
