"""
discord_notify.py — 디스코드 웹훅 알림 (embed 스타일)

원본 embed UI + 전송 로깅(logs/discord.log) + 자동 재시도 통합
"""

import os
import json
import logging
import traceback
import requests
from datetime import datetime

# ══════════════════════════════════════════════════════
# 웹훅 URL
# ══════════════════════════════════════════════════════
_URL_HARDCODED = "https://discord.com/api/webhooks/1486049515801673728/X8ZvvaZ2ip1DbQvnMeb56Eelk4cs4PhtkSM_d2JdxZka7Hb448vWG1nSfzE2PsKN0b4V"
DISCORD_WEBHOOK_URL: str = os.environ.get("DISCORD_WEBHOOK_URL", _URL_HARDCODED)

# ══════════════════════════════════════════════════════
# 색상
# ══════════════════════════════════════════════════════
COLOR_GREEN  = 0x2ECC71
COLOR_RED    = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_BLUE   = 0x3498DB
COLOR_PURPLE = 0x9B59B6
COLOR_ORANGE = 0xE67E22

# ══════════════════════════════════════════════════════
# 로거 — logs/discord.log
# ══════════════════════════════════════════════════════
def _get_logger():
    logger = logging.getLogger("discord_notify")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    try:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(os.path.join(log_dir, "discord.log"), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(fh)
    except Exception:
        pass
    return logger


# ══════════════════════════════════════════════════════
# 전송 함수 — embed 방식 + 로깅 + 1회 재시도
# ══════════════════════════════════════════════════════
def _send(embed: dict, alert_type: str = "", label: str = "", detail: str = ""):
    """embed dict 전송. 실패해도 매매에 영향 없도록 예외 처리."""
    log = _get_logger()

    if not DISCORD_WEBHOOK_URL:
        log.warning(f"[DISCORD_SKIP] {alert_type} | {label} | WEBHOOK_URL 미설정")
        return False

    if detail:
        log.info(f"[DISCORD_SEND] {alert_type} | {label} | {detail}")
    else:
        log.info(f"[DISCORD_SEND] {alert_type} | {label}")

    for attempt in range(1, 3):  # 최대 2회 시도
        try:
            resp = requests.post(
                DISCORD_WEBHOOK_URL,
                json={"embeds": [embed]},
                timeout=5
            )
            if resp.status_code in (200, 204):
                log.info(f"[DISCORD_OK] {alert_type} | {label} | HTTP={resp.status_code}")
                return True
            else:
                log.error(f"[DISCORD_FAIL] {alert_type} | {label} | HTTP={resp.status_code} body={resp.text[:200]} 시도={attempt}")
        except Exception as e:
            log.error(f"[DISCORD_FAIL] {alert_type} | {label} | {type(e).__name__}: {e} 시도={attempt}")
            if attempt == 2:
                log.error(traceback.format_exc())

    return False


def _fmt_price(price: int) -> str:
    return f"{price:,}원"

def _fmt_pnl(pnl_rate: float, pnl_amount: int) -> str:
    sign = "+" if pnl_rate >= 0 else ""
    return f"{sign}{pnl_rate:.2f}% ({sign}{pnl_amount:,}원)"

def _calc_fee(buy_amt: int, sell_amt: int, is_real: bool = False) -> int:
    """
    거래 수수료 계산.
    모의투자: 왕복 0.4505% 고정
    실전투자: 매수 0.015% + 매도 0.015% + 증권거래세 0.18%
    """
    if is_real:
        return round(buy_amt * 0.00015 + sell_amt * (0.00015 + 0.0018))
    return round((buy_amt + sell_amt) * 0.004505)

def _fmt_pnl_with_fee(gross_pnl: int, fee: int) -> str:
    """세전손익 + 수수료 → 수수료후 손익 표시"""
    net = gross_pnl - fee
    sign = "+" if net >= 0 else ""
    return f"{sign}{net:,}원 (세전{'+' if gross_pnl>=0 else ''}{gross_pnl:,} 수수료-{fee:,})"


# ══════════════════════════════════════════════════════
# 1) 장 시작
# ══════════════════════════════════════════════════════
def notify_market_open(account_no: str = "", is_real: bool = False,
                       position_count: int = 0, **kwargs):
    mode = "⚠️ **실전투자**" if is_real else "🟢 **모의투자**"
    fields = [
        {"name": "모드",       "value": mode,               "inline": True},
        {"name": "계좌번호",   "value": f"`{account_no}`",  "inline": True},
        {"name": "기존 포지션","value": f"{position_count}개","inline": True},
    ]
    embed = {
        "title": "🔔 자동매매 시작",
        "description": "자동매매가 활성화되었습니다.",
        "color": COLOR_BLUE,
        "fields": fields,
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "장시작", "시스템")


# ══════════════════════════════════════════════════════
# 2) 매수 체결
# ══════════════════════════════════════════════════════
def notify_buy_fill(code: str, name: str, qty: int, price: int,
                    tp1_price: int = 0, tp2_price: int = 0,
                    sl_price: int = 0, atr: float = 0,
                    avg_buy_price: float = 0,
                    signal_data=None, **kwargs):
    # [버그1 수정] price = 첫 체결가(entry_price), avg_buy_price = 분할체결 가중평균
    # 분할체결이 많은 경우 두 값이 다를 수 있으므로 둘 다 표시
    display_price = int(avg_buy_price) if avg_buy_price > 0 else price
    fields = [
        {"name": "종목",     "value": f"{name} ({code})",            "inline": False},
        {"name": "평균매수가","value": _fmt_price(display_price),     "inline": True},
        {"name": "수량",     "value": f"{qty:,}주",                   "inline": True},
        {"name": "매수금액", "value": _fmt_price(display_price * qty),"inline": True},
    ]
    # 첫 체결가와 평균가 차이가 있으면 추가 표시
    if avg_buy_price > 0 and abs(avg_buy_price - price) >= 1:
        fields.append({"name": "첫체결가", "value": _fmt_price(price), "inline": True})

    if sl_price:
        fields.append({"name": "손절가",   "value": _fmt_price(sl_price),  "inline": True})
    if tp1_price:
        fields.append({"name": "TP1 목표", "value": _fmt_price(tp1_price), "inline": True})
    if tp2_price:
        fields.append({"name": "TP2 목표", "value": _fmt_price(tp2_price), "inline": True})
    if atr:
        fields.append({"name": "ATR",      "value": f"{atr:.1f}원",        "inline": True})

    if signal_data and isinstance(signal_data, dict):
        indicator_lines = []
        if "vol_ratio"       in signal_data: indicator_lines.append(f"거래량: 평균 대비 **{signal_data['vol_ratio']:.1f}배**")
        if "trend"           in signal_data: indicator_lines.append(f"추세: {signal_data['trend']}")
        if "breakout"        in signal_data: indicator_lines.append(f"돌파: {signal_data['breakout']}")
        if "candle_strength" in signal_data: indicator_lines.append(f"캔들강도: **{signal_data['candle_strength']:.0f}%**")
        if "rsi"             in signal_data: indicator_lines.append(f"RSI: **{signal_data['rsi']:.1f}**")
        if "macd"            in signal_data: indicator_lines.append(f"MACD: {signal_data['macd']}")
        if "flag_len"        in signal_data: indicator_lines.append(f"횡보: {signal_data['flag_len']}봉")
        if "base_stop"       in signal_data: indicator_lines.append(f"손절기준: **{signal_data['base_stop']:,}원**")
        if "orderblock"      in signal_data: indicator_lines.append(f"오더블록: **{signal_data['orderblock']}**")
        if indicator_lines:
            fields.append({"name": "📈 진입 지표", "value": "\n".join(indicator_lines), "inline": False})

    embed = {
        "title": "🟢 매수 체결",
        "color": COLOR_GREEN,
        "fields": fields,
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "매수", f"{name}({code})",
          detail=(
              f"평균매수가={display_price:,}원 수량={qty:,}주 금액={display_price*qty:,}원"
              + (f" 첫체결가={price:,}" if avg_buy_price > 0 and abs(avg_buy_price - price) >= 1 else "")
              + (f" 손절={sl_price:,}" if sl_price else "")
              + (f" TP1={tp1_price:,}" if tp1_price else "")
              + (f" TP2={tp2_price:,}" if tp2_price else "")
              + (f" ATR={atr:.1f}" if atr else "")
          ))


# ══════════════════════════════════════════════════════
# 3) 손절
# ══════════════════════════════════════════════════════
def notify_stop_loss(code: str, name: str, qty: int,
                     price: int, entry_price: int,
                     atr: float = 0, avg_buy_price: float = 0,
                     buy_amount: int = 0, **kwargs):
    # [버그1 수정] avg_buy_price 우선 사용
    avg_p = avg_buy_price if avg_buy_price > 0 else entry_price
    gross_pnl = (price - avg_p) * qty
    pnl_rate  = (price - avg_p) / avg_p * 100 if avg_p > 0 else 0
    # [버그2 수정] 수수료 반영
    sell_amt = price * qty
    buy_amt  = buy_amount if buy_amount > 0 else int(avg_p * qty)
    fee      = _calc_fee(buy_amt, sell_amt)
    net_pnl  = gross_pnl - fee

    embed = {
        "title": "❌ 손절",
        "color": COLOR_RED,
        "fields": [
            {"name": "종목",      "value": f"{name} ({code})",             "inline": False},
            {"name": "평균매수가","value": _fmt_price(int(avg_p)),          "inline": True},
            {"name": "매도가",    "value": _fmt_price(price),               "inline": True},
            {"name": "수량",      "value": f"{qty:,}주",                    "inline": True},
            {"name": "손익(세전)","value": f"{gross_pnl:+,}원 ({pnl_rate:+.2f}%)", "inline": True},
            {"name": "수수료",    "value": f"-{fee:,}원",                   "inline": True},
            {"name": "순손익",    "value": f"{net_pnl:+,}원",               "inline": True},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "손절", f"{name}({code})",
          detail=f"평균매수가={int(avg_p):,} 매도가={price:,} 수량={qty:,}주 세전={gross_pnl:+,}원 수수료={fee:,} 순손익={net_pnl:+,}원({pnl_rate:+.2f}%)"
                 + (f" ATR={atr:.1f}" if atr else ""))


# ══════════════════════════════════════════════════════
# 4) TP1 부분익절
# ══════════════════════════════════════════════════════
def notify_tp1_fill(code: str, name: str, qty: int,
                    price: int, entry_price: int, remain_qty: int,
                    tp2_target: int = 0, atr: float = 0,
                    avg_buy_price: float = 0, buy_amount: int = 0, **kwargs):
    avg_p      = avg_buy_price if avg_buy_price > 0 else entry_price
    gross_pnl  = (price - avg_p) * qty
    pnl_rate   = (price - avg_p) / avg_p * 100 if avg_p > 0 else 0
    sell_amt   = price * qty
    buy_amt    = buy_amount if buy_amount > 0 else int(avg_p * qty)
    fee        = _calc_fee(buy_amt, sell_amt)
    net_pnl    = gross_pnl - fee
    tp2_str    = _fmt_price(tp2_target) if tp2_target > 0 else "갱신 중"
    embed = {
        "title": "💰 TP1 부분익절",
        "color": COLOR_GREEN,
        "fields": [
            {"name": "종목",       "value": f"{name} ({code})",             "inline": False},
            {"name": "평균매수가", "value": _fmt_price(int(avg_p)),          "inline": True},
            {"name": "TP1 체결가", "value": _fmt_price(price),               "inline": True},
            {"name": "수량",       "value": f"{qty:,}주",                    "inline": True},
            {"name": "손익(세전)", "value": f"{gross_pnl:+,}원 ({pnl_rate:+.2f}%)", "inline": True},
            {"name": "수수료",     "value": f"-{fee:,}원",                   "inline": True},
            {"name": "순손익",     "value": f"{net_pnl:+,}원",               "inline": True},
            {"name": "잔여수량",   "value": f"{remain_qty:,}주",             "inline": True},
            {"name": "TP2 목표가", "value": tp2_str,                         "inline": True},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "TP1", f"{name}({code})",
          detail=f"평균매수가={int(avg_p):,} TP1체결={price:,} 수량={qty:,}주 세전={gross_pnl:+,}원 수수료={fee:,} 순손익={net_pnl:+,}원({pnl_rate:+.2f}%) 잔여={remain_qty:,}주"
                 + (f" TP2목표={tp2_target:,}" if tp2_target else "")
                 + (f" ATR={atr:.1f}" if atr else ""))


# ══════════════════════════════════════════════════════
# 5) TP2 익절
# ══════════════════════════════════════════════════════
def notify_tp2_fill(code: str, name: str, qty: int,
                    price: int, entry_price: int, remain_qty: int,
                    trail_target: int = 0, atr: float = 0,
                    avg_buy_price: float = 0, buy_amount: int = 0, **kwargs):
    avg_p     = avg_buy_price if avg_buy_price > 0 else entry_price
    gross_pnl = (price - avg_p) * qty
    pnl_rate  = (price - avg_p) / avg_p * 100 if avg_p > 0 else 0
    sell_amt  = price * qty
    buy_amt   = buy_amount if buy_amount > 0 else int(avg_p * qty)
    fee       = _calc_fee(buy_amt, sell_amt)
    net_pnl   = gross_pnl - fee
    trail_str = _fmt_price(trail_target) if trail_target > 0 else "트레일링 추적 중"
    embed = {
        "title": "🚀 TP2 익절",
        "color": COLOR_GREEN,
        "fields": [
            {"name": "종목",       "value": f"{name} ({code})",             "inline": False},
            {"name": "평균매수가", "value": _fmt_price(int(avg_p)),          "inline": True},
            {"name": "TP2 체결가", "value": _fmt_price(price),               "inline": True},
            {"name": "수량",       "value": f"{qty:,}주",                    "inline": True},
            {"name": "손익(세전)", "value": f"{gross_pnl:+,}원 ({pnl_rate:+.2f}%)", "inline": True},
            {"name": "수수료",     "value": f"-{fee:,}원",                   "inline": True},
            {"name": "순손익",     "value": f"{net_pnl:+,}원",               "inline": True},
            {"name": "잔여수량",   "value": f"{remain_qty:,}주",             "inline": True},
            {"name": "트레일링 기준","value": trail_str,                     "inline": True},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "TP2", f"{name}({code})",
          detail=f"평균매수가={int(avg_p):,} TP2체결={price:,} 수량={qty:,}주 세전={gross_pnl:+,}원 수수료={fee:,} 순손익={net_pnl:+,}원({pnl_rate:+.2f}%) 잔여={remain_qty:,}주"
                 + (f" ATR={atr:.1f}" if atr else ""))


# ══════════════════════════════════════════════════════
# 6) 본절보호
# ══════════════════════════════════════════════════════
def notify_profit_safe(code: str, name: str, qty: int,
                       price: int, entry_price: int,
                       atr: float = 0, avg_buy_price: float = 0,
                       buy_amount: int = 0, **kwargs):
    avg_p     = avg_buy_price if avg_buy_price > 0 else entry_price
    gross_pnl = (price - avg_p) * qty
    pnl_rate  = (price - avg_p) / avg_p * 100 if avg_p > 0 else 0
    sell_amt  = price * qty
    buy_amt   = buy_amount if buy_amount > 0 else int(avg_p * qty)
    fee       = _calc_fee(buy_amt, sell_amt)
    net_pnl   = gross_pnl - fee
    embed = {
        "title": "🛡 본절 보호 매도",
        "color": COLOR_YELLOW,
        "fields": [
            {"name": "종목",       "value": f"{name} ({code})",             "inline": False},
            {"name": "평균매수가", "value": _fmt_price(int(avg_p)),          "inline": True},
            {"name": "매도가",     "value": _fmt_price(price),               "inline": True},
            {"name": "수량",       "value": f"{qty:,}주",                    "inline": True},
            {"name": "손익(세전)", "value": f"{gross_pnl:+,}원 ({pnl_rate:+.2f}%)", "inline": True},
            {"name": "수수료",     "value": f"-{fee:,}원",                   "inline": True},
            {"name": "순손익",     "value": f"{net_pnl:+,}원",               "inline": True},
            {"name": "",           "value": "TP1 이후 하락 방어",            "inline": False},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "본절보호", f"{name}({code})",
          detail=f"평균매수가={int(avg_p):,} 매도가={price:,} 수량={qty:,}주 세전={gross_pnl:+,}원 수수료={fee:,} 순손익={net_pnl:+,}원({pnl_rate:+.2f}%)"
                 + (f" ATR={atr:.1f}" if atr else ""))


# ══════════════════════════════════════════════════════
# 7) 트레일링 청산
# ══════════════════════════════════════════════════════
def notify_trail_stop(code: str, name: str, qty: int,
                      price: int, entry_price: int,
                      atr: float = 0, avg_buy_price: float = 0,
                      buy_amount: int = 0, **kwargs):
    avg_p     = avg_buy_price if avg_buy_price > 0 else entry_price
    gross_pnl = (price - avg_p) * qty
    pnl_rate  = (price - avg_p) / avg_p * 100 if avg_p > 0 else 0
    sell_amt  = price * qty
    buy_amt   = buy_amount if buy_amount > 0 else int(avg_p * qty)
    fee       = _calc_fee(buy_amt, sell_amt)
    net_pnl   = gross_pnl - fee
    embed = {
        "title": "📉 트레일링 청산",
        "color": COLOR_PURPLE,
        "fields": [
            {"name": "종목",       "value": f"{name} ({code})",             "inline": False},
            {"name": "평균매수가", "value": _fmt_price(int(avg_p)),          "inline": True},
            {"name": "매도가",     "value": _fmt_price(price),               "inline": True},
            {"name": "수량",       "value": f"{qty:,}주",                    "inline": True},
            {"name": "손익(세전)", "value": f"{gross_pnl:+,}원 ({pnl_rate:+.2f}%)", "inline": True},
            {"name": "수수료",     "value": f"-{fee:,}원",                   "inline": True},
            {"name": "순손익",     "value": f"{net_pnl:+,}원",               "inline": True},
            {"name": "",           "value": "고점 대비 하락 청산",           "inline": False},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "트레일링", f"{name}({code})",
          detail=f"평균매수가={int(avg_p):,} 매도가={price:,} 수량={qty:,}주 세전={gross_pnl:+,}원 수수료={fee:,} 순손익={net_pnl:+,}원({pnl_rate:+.2f}%)"
                 + (f" ATR={atr:.1f}" if atr else ""))


def notify_mini_trail_stop(code: str, name: str, qty: int,
                           price: int, entry_price: int,
                           atr: float = 0, avg_buy_price: float = 0,
                           buy_amount: int = 0, **kwargs):
    avg_p     = avg_buy_price if avg_buy_price > 0 else entry_price
    gross_pnl = (price - avg_p) * qty
    pnl_rate  = (price - avg_p) / avg_p * 100 if avg_p > 0 else 0
    sell_amt  = price * qty
    buy_amt   = buy_amount if buy_amount > 0 else int(avg_p * qty)
    fee       = _calc_fee(buy_amt, sell_amt)
    net_pnl   = gross_pnl - fee
    embed = {
        "title": "🎯 미니 트레일링 익절",
        "color": 0x00CED1,
        "fields": [
            {"name": "종목",       "value": f"{name} ({code})",             "inline": False},
            {"name": "평균매수가", "value": _fmt_price(int(avg_p)),          "inline": True},
            {"name": "매도가",     "value": _fmt_price(price),               "inline": True},
            {"name": "수량",       "value": f"{qty:,}주",                    "inline": True},
            {"name": "손익(세전)", "value": f"{gross_pnl:+,}원 ({pnl_rate:+.2f}%)", "inline": True},
            {"name": "수수료",     "value": f"-{fee:,}원",                   "inline": True},
            {"name": "순손익",     "value": f"{net_pnl:+,}원",               "inline": True},
            {"name": "",           "value": "수익 보호 트레일링 (TP1 전)",  "inline": False},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "미니트레일", f"{name}({code})",
          detail=f"평균매수가={int(avg_p):,} 매도가={price:,} 수량={qty:,}주 세전={gross_pnl:+,}원 수수료={fee:,} 순손익={net_pnl:+,}원({pnl_rate:+.2f}%)"
                 + (f" ATR={atr:.1f}" if atr else ""))


# ══════════════════════════════════════════════════════
# 8) 타임스탑 / 거래량급감
# ══════════════════════════════════════════════════════
def notify_time_stop(code: str, name: str, qty: int,
                     price: int, entry_price: int,
                     reason: str = "TIME_STOP", atr: float = 0,
                     avg_buy_price: float = 0, buy_amount: int = 0, **kwargs):
    avg_p     = avg_buy_price if avg_buy_price > 0 else entry_price
    gross_pnl = (price - avg_p) * qty
    pnl_rate  = (price - avg_p) / avg_p * 100 if avg_p > 0 else 0
    sell_amt  = price * qty
    buy_amt   = buy_amount if buy_amount > 0 else int(avg_p * qty)
    fee       = _calc_fee(buy_amt, sell_amt)
    net_pnl   = gross_pnl - fee
    title     = "⏱ 타임스탑 청산" if "VOL" not in reason else "📉 거래량 급감 청산"
    embed = {
        "title": title,
        "color": COLOR_YELLOW,
        "fields": [
            {"name": "종목",       "value": f"{name} ({code})",             "inline": False},
            {"name": "평균매수가", "value": _fmt_price(int(avg_p)),          "inline": True},
            {"name": "매도가",     "value": _fmt_price(price),               "inline": True},
            {"name": "수량",       "value": f"{qty:,}주",                    "inline": True},
            {"name": "손익(세전)", "value": f"{gross_pnl:+,}원 ({pnl_rate:+.2f}%)", "inline": True},
            {"name": "수수료",     "value": f"-{fee:,}원",                   "inline": True},
            {"name": "순손익",     "value": f"{net_pnl:+,}원",               "inline": True},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    alert_type = "거래량급감" if "VOL" in reason else "타임스탑"
    _send(embed, alert_type, f"{name}({code})",
          detail=f"평균매수가={int(avg_p):,} 매도가={price:,} 수량={qty:,}주 세전={gross_pnl:+,}원 수수료={fee:,} 순손익={net_pnl:+,}원({pnl_rate:+.2f}%)"
                 + (f" ATR={atr:.1f}" if atr else ""))


# ══════════════════════════════════════════════════════
# 9) 강제청산
# ══════════════════════════════════════════════════════
def notify_force_liquidation(code: str, name: str, qty: int,
                              entry_price: int, **kwargs):
    try:
        from config import FORCE_LIQUIDATION_HOUR, FORCE_LIQUIDATION_MIN
        time_str = f"{FORCE_LIQUIDATION_HOUR:02d}:{FORCE_LIQUIDATION_MIN:02d}"
    except Exception:
        time_str = "강제"
    embed = {
        "title": f"🚨 {time_str} 강제 청산",
        "color": COLOR_ORANGE,
        "fields": [
            {"name": "종목",   "value": f"{name} ({code})",          "inline": False},
            {"name": "수량",   "value": f"{qty:,}주",                  "inline": True},
            {"name": "매수가", "value": _fmt_price(entry_price),      "inline": True},
            {"name": "",       "value": "장 마감 전 강제 시장가 매도","inline": False},
        ],
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "강제청산", f"{name}({code})",
          detail=f"매수가={entry_price:,} 수량={qty:,}주")


# ══════════════════════════════════════════════════════
# 10) 범용 시스템 알림
# ══════════════════════════════════════════════════════
def notify_system(msg: str, level: str = "INFO"):
    color_map = {"INFO": COLOR_BLUE, "WARNING": COLOR_YELLOW, "CRITICAL": COLOR_RED}
    embed = {
        "title": "🚨 AUTO-TRADE ALERT",
        "description": msg,
        "color": color_map.get(level, COLOR_YELLOW),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "시스템", level)
