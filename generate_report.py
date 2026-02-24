"""
자동매매 일일 보고서 생성기 v2
────────────────────────────────────────────────────
로그 포맷 변경 반영:
  - code={code} → 종목명(code) 형태로 파싱
  - CODE_NAME 하드코딩 매핑 제거
────────────────────────────────────────────────────
Usage:
  python generate_report.py                          # trade.log → 자동매매_보고서.xlsx
  python generate_report.py logs/trade.log
  python generate_report.py trade.log report.xlsx
────────────────────────────────────────────────────
시트 구성:
  ① 요약 & 차트  — KPI / 청산유형 테이블 / 원형 차트 3개
                   (청산유형 건수 / 청산유형 금액 / 이익 vs 손실 총액)
  ② 거래내역     — 종목별 ATR·손절·TP1/2·트레일링 세부 타임라인
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
    """
    '종목명(code)' 또는 'code=종목명(code)' 형태에서 (name, code) 추출.
    구버전 호환: 'code=xxxxxx' (숫자 6자리) 도 처리.
    """
    # 새 포맷: 종목명(039860)
    m = re.match(r'^(.+?)\((\d{6})\)$', token.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # 구버전 code=039860
    m = re.match(r'^code=(\d{6})$', token.strip())
    if m:
        return m.group(1), m.group(1)
    return None, None


def parse_log(path: str) -> list[dict]:
    """trade.log 파싱 → 거래 세션 리스트"""

    # ── 정규식 ──
    re_ts    = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

    # ATR_CALC: [ATR_CALC] 종목명(code) ATR=147.9원 ...
    re_atr   = re.compile(r"\[ATR_CALC\]\s+(.+?\(\d{6}\))\s+ATR=([\d.]+)원")
    # 구버전 ATR: [ATR_CALC] code=xxxxxx ATR=...
    re_atr_old = re.compile(r"\[ATR_CALC\]\s+code=(\d{6})\s+ATR=([\d.]+)원")

    # BUY_FILL_NEW: 새/구 모두 지원
    re_buy   = re.compile(r"\[BUY_FILL_NEW\]\s+(.+?\(\d{6}\)|code=\d{6})\s+price=(\d+)$")
    re_qty   = re.compile(r"\[BUY_DONE\]\s+(.+?\(\d{6}\)|code=\d{6})\s+잔여=(\d+)/(\d+)")
    re_tp    = re.compile(
        r"\[TP_TARGET_SET\]\s+(.+?\(\d{6}\)|code=\d{6})\s+ATR=([\d.]+)원\s+손절=(\d+)\s+TP1=(\d+)\s+TP2\(초기\)=(\d+)"
    )
    re_tp1   = re.compile(r"\[TP1_FILLED\]\s+(.+?\(\d{6}\)|code=\d{6})\s+잔여=(\d+)주")
    re_tp2   = re.compile(r"\[TP2_FILLED\]\s+(.+?\(\d{6}\)|code=\d{6})\s+잔여=(\d+)주")
    re_trail = re.compile(r"\[TRAIL_STOP\]\s+(.+?\(\d{6}\)|code=\d{6})\s+")
    re_sell  = re.compile(r"\[ORDER_TRY\]\s+방향=SELL\s+(.+?\(\d{6}\)|code=\d{6})\s+수량=(\d+)주\s+사유=(\S+)")
    re_done  = re.compile(r"\[SELL_DONE\]\s+(.+?\(\d{6}\)|code=\d{6})\s+전량매도완료")
    re_psafe = re.compile(r"\[PROFIT_SAFEGUARD\]\s+(.+?\(\d{6}\)|code=\d{6})\s+본절보호")
    re_emerg = re.compile(r"\[STOP_LOSS_EMERGENCY\]\s+(.+?\(\d{6}\)|code=\d{6})\s+비상 손절 현재가:(\d+)\s+pnl=([-\d.]+)")
    re_candle= re.compile(r"\[STOP_LOSS_CANDLE\]\s+(.+?\(\d{6}\)|code=\d{6})\s+완성봉 ATR손절 종가:(\d+)\s+손절기준:\d+\s+pnl=([-\d.]+)")
    re_time  = re.compile(r"\[TIME_STOP\]\s+(.+?\(\d{6}\)|code=\d{6})\s+보유=\d+초\s+매도수량=\d+주\s+pnl=([-\d.]+)")
    re_vol   = re.compile(r"\[VOL_TIME_STOP\]\s+(.+?\(\d{6}\)|code=\d{6})\s+구간=\S+\s+매도수량=\d+주.*pnl=([-\d.]+)")
    re_chejan= re.compile(r"\[CHEJAN\]\s+-매도\s+(.+?\(\d{6}\)|code=\d{6})\s+price=(\d+)")

    def extract_code(token: str):
        """'종목명(code)' 또는 'code=xxxxxx' → (name, code)"""
        # 새 포맷: "종목명(039860)"
        m = re.match(r'^(.+?)\((\d{6})\)$', token.strip())
        if m:
            return m.group(1).strip(), m.group(2)
        # 구버전: "code=039860"
        m = re.match(r'^code=(\d{6})$', token.strip())
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

            # ATR_CALC
            m = re_atr.search(line) or re_atr_old.search(line)
            if m:
                if re_atr.search(line):
                    name, code = extract_code(m.group(1))
                else:
                    code = m.group(1); name = code
                sessions.setdefault(code, {})
                sessions[code]["atr"] = float(m.group(2))
                sessions[code]["name"] = name
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
                    sessions[code]["exit_pnl_pct"] = float(m.group(3))
                    sessions[code]["exit_reason_detail"] = "비상손절(-2.5%)"
                continue

            # ATR 완성봉 손절
            m = re_candle.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code]["exit_pnl_pct"] = float(m.group(3))
                    sessions[code]["exit_reason_detail"] = "ATR손절(완성봉)"
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
                    sessions[code]["exit_pnl_pct"] = float(m.group(2))
                    sessions[code]["exit_reason_detail"] = "거래량급감청산"
                continue

            # 매도체결가 추적
            m = re_chejan.search(line)
            if m:
                _, code = extract_code(m.group(1))
                if code in sessions:
                    sessions[code]["exit_price"] = int(m.group(2))
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

            # SELL_DONE → 세션 종료
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
                elif "TRAIL_STOP" in reason:
                    s["exit_type"] = "트레일링"
                elif "PROFIT_SAFE" in reason:
                    s["exit_type"] = "본절보호"
                else:
                    evts = [e["type"] for e in s.get("events", [])]
                    if "TP2" in evts:
                        s["exit_type"] = "TP2+트레일링"
                    elif "TP1" in evts:
                        s["exit_type"] = "TP1+타임스탑"
                    else:
                        s["exit_type"] = reason or "기타"

                # 손익
                ep  = s.get("entry_price", 0)
                xp  = s.get("exit_price", ep)
                qty = s.get("qty", 0)
                if "exit_pnl_pct" in s:
                    s["pnl_pct"] = s["exit_pnl_pct"]
                    s["pnl_amt"] = int(s["pnl_pct"] * ep * qty)
                else:
                    s["pnl_pct"] = (xp - ep) / ep if ep else 0
                    s["pnl_amt"] = (xp - ep) * qty

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
DETAIL_HEADERS = [
    "No", "종목명", "종목코드",
    "매수시각", "매수가", "수량", "매수금액",
    "ATR", "손절기준", "TP1목표", "TP2목표",
    "TP1\n체결", "TP2\n체결", "트레일링\n발동",
    "청산유형", "청산시각", "청산가",
    "손익금액(원)", "수익률(%)",
    "보유시간"
]

def build_detail_sheet(ws, trades: list[dict]):
    ws.title = "거래내역"
    ws.sheet_view.showGridLines = True

    col_widths = [5, 16, 10, 18, 9, 7, 13, 8, 9, 9, 9,
                  7, 7, 8, 14, 18, 9, 14, 10, 10]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    _apply_header(ws, 1, range(1, len(DETAIL_HEADERS)+1), DETAIL_HEADERS, height=32)

    for i, t in enumerate(trades, 2):
        ws.row_dimensions[i].height = 18
        events  = t.get("events", [])
        entry_p = t.get("entry_price", 0)
        qty     = t.get("qty", 0)

        has_tp1   = any(e["type"] == "TP1"         for e in events)
        has_tp2   = any(e["type"] == "TP2"         for e in events)
        has_trail = any(e["type"] == "TRAIL"        for e in events)

        row_data = [
            i - 1,
            t.get("name", t.get("code", "")),
            t.get("code", ""),
            t["entry_time"].strftime("%H:%M:%S") if t.get("entry_time") else "",
            entry_p,
            qty,
            entry_p * qty,
            t.get("atr", ""),
            t.get("stop_loss", ""),
            t.get("tp1", ""),
            t.get("tp2", ""),
            "✔" if has_tp1   else "",
            "✔" if has_tp2   else "",
            "✔" if has_trail else "",
            t.get("exit_type", ""),
            t["exit_time"].strftime("%H:%M:%S") if t.get("exit_time") else "",
            t.get("exit_price", ""),
            t.get("pnl_amt", 0),
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
            ws.cell(row=i, column=20, value=f"{secs//60}분 {secs%60}초")

        # 숫자 포맷
        for col, fmt in [(5,"#,##0"),(7,"#,##0"),(8,"#,##0.0"),
                          (9,"#,##0"),(10,"#,##0"),(11,"#,##0"),(17,"#,##0")]:
            ws.cell(row=i, column=col).number_format = fmt

        pnl_c = ws.cell(row=i, column=18)
        pct_c = ws.cell(row=i, column=19)
        pnl_c.number_format = '#,##0;(#,##0);"-"'
        pct_c.number_format = '0.00%;(0.00%);"-"'
        _color_pnl(pnl_c, t.get("pnl_amt", 0))
        _color_pnl(pct_c, t.get("pnl_amt", 0))

        if i % 2 == 0:
            for col in range(1, len(DETAIL_HEADERS)+1):
                ws.cell(row=i, column=col).fill = _fill("F5F7FA")

        # ✔ 색상
        for col, etype in [(12,"TP1"),(13,"TP2"),(14,"TRAIL")]:
            c = ws.cell(row=i, column=col)
            if c.value == "✔":
                c.font = _font(bold=True, color="00B050")

        # 청산유형 색상
        ec = ws.cell(row=i, column=15)
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

    sc = ws.cell(sr, 18, value=f"=SUM(R2:R{last})")
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
    "기타":           "808080",
}

def _make_pie(ws, data_ref, label_ref, title, colors_order, w=13, h=13):
    pie = PieChart()
    pie.title  = title
    pie.style  = 10
    pie.add_data(data_ref)
    pie.set_categories(label_ref)
    pie.series[0].title = None
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

    # 열 너비: A(여백) B~E(콘텐츠 4열) F(여백) G~H(보조데이터)
    for col, w in zip("ABCDEFGH", [2, 22, 20, 20, 20, 2, 1, 1]):
        ws.column_dimensions[col].width = w

    n        = len(trades)
    det_last = n + 1

    # ── 로고+타이틀 (행 2~3) ──────────────────────────
    ws.merge_cells("B2:E2")
    tc = ws["B2"]
    tc.value     = "📊 자동매매 일일 성과 보고서"
    tc.font      = Font(name=FONT_NAME, bold=True, size=16, color="1F3864")
    tc.alignment = _align(h="left")
    ws.row_dimensions[2].height = 36

    trade_date = trades[0]["entry_time"].strftime("%Y년 %m월 %d일") if trades else ""
    ws.merge_cells("B3:E3")
    sc = ws["B3"]
    sc.value     = f"운용일자: {trade_date}  |  모의투자 (키움증권)"
    sc.font      = _font(size=10, color="595959")
    sc.alignment = _align(h="left")
    ws.row_dimensions[3].height = 16

    ws.row_dimensions[4].height = 8  # 여백

    # ── KPI 박스 (행 5~6) ─────────────────────────────
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

    kpi(2, "총 거래 수",   f"=COUNTA('거래내역'!A2:A{det_last})", "#,##0")
    net_cell = kpi(3, "순 손익 (원)", f"=SUM('거래내역'!R2:R{det_last})", '#,##0;(#,##0);-')
    kpi(4, "승률",
        f"=COUNTIF('거래내역'!R2:R{det_last},\">0\")/COUNTA('거래내역'!A2:A{det_last})",
        "0.0%")
    kpi(5, "평균 수익률",
        f"=AVERAGE('거래내역'!S2:S{det_last})", "0.00%")
    net_val = sum(t.get("pnl_amt", 0) for t in trades)
    net_cell.font = Font(name=FONT_NAME, bold=True, size=14,
                         color="0070C0" if net_val >= 0 else "FF0000")

    ws.row_dimensions[7].height = 10  # 여백

    # ── 청산유형 집계 테이블 (행 8~) ──────────────────
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

    ws.row_dimensions[tbl_end + 1].height = 8  # 여백

    # ── 이익/손실 요약 바 (테이블 직후) ──────────────
    total_profit = sum(t.get("pnl_amt", 0) for t in trades if t.get("pnl_amt", 0) > 0)
    total_loss   = abs(sum(t.get("pnl_amt", 0) for t in trades if t.get("pnl_amt", 0) < 0))
    net          = total_profit - total_loss

    hdr_row = tbl_end + 2
    val_row = tbl_end + 3
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

    # ── 보조 데이터 (G/H열, 차트 소스) ──────────────
    AUX = 7   # G열

    def aux(row, col, val):
        c = ws.cell(row, col, value=val)
        c.font = _font(size=7, color="EEEEEE")
        c.fill = _fill("FFFFFF")

    # 보조1: 건수
    for ro, (et, v) in enumerate(exit_types.items()):
        aux(2 + ro, AUX, et)
        aux(2 + ro, AUX+1, v["count"])
    cnt_s = 2;  cnt_e = 2 + len(exit_types) - 1

    # 보조2: 손익 절댓값
    base2 = cnt_e + 2
    for ro, (et, v) in enumerate(exit_types.items()):
        aux(base2 + ro, AUX, et)
        aux(base2 + ro, AUX+1, abs(v["pnl"]))
    pnl_s = base2; pnl_e = base2 + len(exit_types) - 1

    # 보조3: 이익 vs 손실
    base3 = pnl_e + 2
    aux(base3,   AUX, "총 이익"); aux(base3,   AUX+1, total_profit)
    aux(base3+1, AUX, "총 손실"); aux(base3+1, AUX+1, total_loss)

    # ── 차트 3개 — 가로로 나란히 (차트 행 = val_row + 2) ──
    chart_row = str(val_row + 2)
    ws.row_dimensions[val_row + 1].height = 8  # 여백

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
    ws.add_chart(pie2, f"C{chart_row}")

    pie3 = _make_pie(
        ws,
        Reference(ws, min_col=AUX+1, min_row=base3,   max_row=base3+1),
        Reference(ws, min_col=AUX,   min_row=base3,   max_row=base3+1),
        f"이익 vs 손실  (이익 {total_profit:,}원 / 손실 {total_loss:,}원)",
        ["0070C0", "FF4444"],
    )
    ws.add_chart(pie3, f"D{chart_row}")

# ──────────────────────────────────────────────
# 5. 메인
# ──────────────────────────────────────────────
def main():
    log_path = sys.argv[1] if len(sys.argv) > 1 else "trade.log"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "자동매매_보고서.xlsx"

    print(f"[파싱] {log_path}")
    trades = parse_log(log_path)
    trades = [t for t in trades if t.get("entry_price") and t.get("exit_time")]
    print(f"  → 완성된 거래 {len(trades)}건")

    if not trades:
        print("분석할 거래가 없습니다.")
        return

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
