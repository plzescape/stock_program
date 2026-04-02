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
def _send(embed: dict, alert_type: str = "", label: str = ""):
    """embed dict 전송. 실패해도 매매에 영향 없도록 예외 처리."""
    log = _get_logger()

    if not DISCORD_WEBHOOK_URL:
        log.warning(f"[DISCORD_SKIP] {alert_type} | {label} | WEBHOOK_URL 미설정")
        return False

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
                    signal_data=None, **kwargs):
    fields = [
        {"name": "종목",     "value": f"{name} ({code})",       "inline": False},
        {"name": "수량",     "value": f"{qty:,}주",              "inline": True},
        {"name": "체결가",   "value": _fmt_price(price),         "inline": True},
        {"name": "매수금액", "value": _fmt_price(price * qty),   "inline": True},
    ]
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
    _send(embed, "매수", f"{name}({code})")


# ══════════════════════════════════════════════════════
# 3) 손절
# ══════════════════════════════════════════════════════
def notify_stop_loss(code: str, name: str, qty: int,
                     price: int, entry_price: int,
                     atr: float = 0, **kwargs):
    pnl_rate   = (price - entry_price) / entry_price * 100
    pnl_amount = (price - entry_price) * qty
    embed = {
        "title": "❌ 손절",
        "color": COLOR_RED,
        "fields": [
            {"name": "종목",     "value": f"{name} ({code})", "inline": False},
            {"name": "매도가",   "value": _fmt_price(price),  "inline": True},
            {"name": "수량",     "value": f"{qty:,}주",        "inline": True},
            {"name": "손해율",   "value": f"{pnl_rate:.2f}%", "inline": True},
            {"name": "손해금액", "value": f"{pnl_amount:,}원","inline": True},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "손절", f"{name}({code})")


# ══════════════════════════════════════════════════════
# 4) TP1 부분익절
# ══════════════════════════════════════════════════════
def notify_tp1_fill(code: str, name: str, qty: int,
                    price: int, entry_price: int, remain_qty: int,
                    tp2_target: int = 0, atr: float = 0, **kwargs):
    pnl_rate   = (price - entry_price) / entry_price * 100
    pnl_amount = (price - entry_price) * qty
    # TP2 목표가 표시: 전달된 값이 없거나 0이면 "갱신 중" 표시
    tp2_str = _fmt_price(tp2_target) if tp2_target > 0 else "갱신 중"
    embed = {
        "title": "💰 TP1 부분익절",
        "color": COLOR_GREEN,
        "fields": [
            {"name": "종목",      "value": f"{name} ({code})",   "inline": False},
            {"name": "TP1 체결가", "value": _fmt_price(price),    "inline": True},
            {"name": "수량",      "value": f"{qty:,}주",           "inline": True},
            {"name": "수익률",    "value": f"+{pnl_rate:.2f}%",   "inline": True},
            {"name": "손익금액",  "value": f"+{pnl_amount:,}원",  "inline": True},
            {"name": "잔여수량",  "value": f"{remain_qty:,}주",   "inline": True},
            {"name": "TP2 목표가","value": tp2_str,               "inline": True},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "TP1", f"{name}({code})")


# ══════════════════════════════════════════════════════
# 5) TP2 익절
# ══════════════════════════════════════════════════════
def notify_tp2_fill(code: str, name: str, qty: int,
                    price: int, entry_price: int, remain_qty: int,
                    trail_target: int = 0, atr: float = 0, **kwargs):
    pnl_rate   = (price - entry_price) / entry_price * 100
    pnl_amount = (price - entry_price) * qty
    trail_str  = _fmt_price(trail_target) if trail_target > 0 else "트레일링 추적 중"
    embed = {
        "title": "🚀 TP2 익절",
        "color": COLOR_GREEN,
        "fields": [
            {"name": "종목",        "value": f"{name} ({code})",   "inline": False},
            {"name": "TP2 체결가",  "value": _fmt_price(price),    "inline": True},
            {"name": "수량",        "value": f"{qty:,}주",           "inline": True},
            {"name": "수익률",      "value": f"+{pnl_rate:.2f}%",  "inline": True},
            {"name": "손익금액",    "value": f"+{pnl_amount:,}원", "inline": True},
            {"name": "잔여수량",    "value": f"{remain_qty:,}주",  "inline": True},
            {"name": "트레일링 기준","value": trail_str,            "inline": True},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "TP2", f"{name}({code})")


# ══════════════════════════════════════════════════════
# 6) 본절보호
# ══════════════════════════════════════════════════════
def notify_profit_safe(code: str, name: str, qty: int,
                       price: int, entry_price: int,
                       atr: float = 0, **kwargs):
    pnl_rate   = (price - entry_price) / entry_price * 100
    pnl_amount = (price - entry_price) * qty
    embed = {
        "title": "🛡 본절 보호 매도",
        "color": COLOR_YELLOW,
        "fields": [
            {"name": "종목",     "value": f"{name} ({code})",    "inline": False},
            {"name": "매도가",   "value": _fmt_price(price),     "inline": True},
            {"name": "수량",     "value": f"{qty:,}주",           "inline": True},
            {"name": "수익률",   "value": f"{pnl_rate:+.2f}%",   "inline": True},
            {"name": "손익금액", "value": f"{pnl_amount:+,}원",  "inline": True},
            {"name": "",         "value": "TP1 이후 하락 방어",  "inline": False},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "본절보호", f"{name}({code})")


# ══════════════════════════════════════════════════════
# 7) 트레일링 청산
# ══════════════════════════════════════════════════════
def notify_trail_stop(code: str, name: str, qty: int,
                      price: int, entry_price: int,
                      atr: float = 0, **kwargs):
    pnl_rate   = (price - entry_price) / entry_price * 100
    pnl_amount = (price - entry_price) * qty
    embed = {
        "title": "📉 트레일링 청산",
        "color": COLOR_PURPLE,
        "fields": [
            {"name": "종목",     "value": f"{name} ({code})",   "inline": False},
            {"name": "매도가",   "value": _fmt_price(price),    "inline": True},
            {"name": "수량",     "value": f"{qty:,}주",          "inline": True},
            {"name": "수익률",   "value": f"{pnl_rate:+.2f}%",  "inline": True},
            {"name": "손익금액", "value": f"{pnl_amount:+,}원", "inline": True},
            {"name": "",         "value": "고점 대비 하락 청산","inline": False},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "트레일링", f"{name}({code})")


def notify_mini_trail_stop(code: str, name: str, qty: int,
                           price: int, entry_price: int,
                           atr: float = 0, **kwargs):
    pnl_rate   = (price - entry_price) / entry_price * 100
    pnl_amount = (price - entry_price) * qty
    embed = {
        "title": "🎯 미니 트레일링 익절",
        "color": 0x00CED1,   # 청록색 — 타임스탑/트레일링과 구분
        "fields": [
            {"name": "종목",     "value": f"{name} ({code})",             "inline": False},
            {"name": "매도가",   "value": _fmt_price(price),              "inline": True},
            {"name": "수량",     "value": f"{qty:,}주",                    "inline": True},
            {"name": "수익률",   "value": f"{pnl_rate:+.2f}%",            "inline": True},
            {"name": "손익금액", "value": f"{pnl_amount:+,}원",           "inline": True},
            {"name": "",         "value": "수익 보호 트레일링 (TP1 전)", "inline": False},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed, "미니트레일", f"{name}({code})")


# ══════════════════════════════════════════════════════
# 8) 타임스탑 / 거래량급감
# ══════════════════════════════════════════════════════
def notify_time_stop(code: str, name: str, qty: int,
                     price: int, entry_price: int,
                     reason: str = "TIME_STOP", atr: float = 0, **kwargs):
    pnl_rate   = (price - entry_price) / entry_price * 100
    pnl_amount = (price - entry_price) * qty
    title      = "⏱ 타임스탑 청산" if "VOL" not in reason else "📉 거래량 급감 청산"
    embed = {
        "title": title,
        "color": COLOR_YELLOW,
        "fields": [
            {"name": "종목",     "value": f"{name} ({code})",   "inline": False},
            {"name": "매도가",   "value": _fmt_price(price),    "inline": True},
            {"name": "수량",     "value": f"{qty:,}주",          "inline": True},
            {"name": "수익률",   "value": f"{pnl_rate:+.2f}%",  "inline": True},
            {"name": "손익금액", "value": f"{pnl_amount:+,}원", "inline": True},
        ] + ([{"name": "ATR", "value": f"{atr:.1f}원", "inline": True}] if atr else []),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    alert_type = "거래량급감" if "VOL" in reason else "타임스탑"
    _send(embed, alert_type, f"{name}({code})")


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
    _send(embed, "강제청산", f"{name}({code})")


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
