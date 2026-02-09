# ==================================================
# 디스코드 매매 알림 모듈
# ==================================================
import requests
from datetime import datetime
import os

# 보안을 위해 환경변수 또는 config 파일에서 관리 권장
# 예: DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1465022221994037339/mOln_VqVzMAQRInrUic86Ysr482ZDIjMFB6EoUR9U3dUpMkqTw-Keyf0InVpUwE2IkTu"
)

# ===== 색상 =====
COLOR_GREEN  = 0x2ECC71   # 매수, 익절
COLOR_RED    = 0xE74C3C   # 손절
COLOR_YELLOW = 0xF1C40F   # 경고
COLOR_BLUE   = 0x3498DB   # 정보
COLOR_PURPLE = 0x9B59B6   # 트레일링
COLOR_ORANGE = 0xE67E22   # 강제청산


def _send(embed: dict):
    """디스코드 웹훅 전송 (실패해도 매매에 영향 없도록 예외 처리)"""
    try:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=3
        )
    except Exception as e:
        print(f"⚠️ Discord 알림 실패: {e}")


def _fmt_price(price: int) -> str:
    return f"{price:,}원"


def _fmt_pnl(pnl_rate: float, pnl_amount: int) -> str:
    sign = "+" if pnl_rate >= 0 else ""
    return f"{sign}{pnl_rate:.2f}% ({sign}{pnl_amount:,}원)"


# ==================================================
# 1) 장 시작 알림
# ==================================================
def notify_market_open(account_no: str, is_real: bool, position_count: int):
    mode = "⚠️ **실전투자**" if is_real else "🟢 **모의투자**"
    embed = {
        "title": "🔔 자동매매 시작",
        "description": "자동매매가 활성화되었습니다.",
        "color": COLOR_BLUE,
        "fields": [
            {"name": "모드", "value": mode, "inline": True},
            {"name": "계좌번호", "value": f"`{account_no}`", "inline": True},
            {"name": "기존 포지션", "value": f"{position_count}개", "inline": True},
        ],
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)


# ==================================================
# 2) 매수 체결 알림
# ==================================================
def notify_buy_fill(
    code: str,
    name: str,
    qty: int,
    price: int,
    tp1_price: int,
    tp2_price: int,
    signal_data: dict | None = None
):
    total_amount = price * qty
    fields = [
        {"name": "종목", "value": f"{name} ({code})", "inline": False},
        {"name": "수량", "value": f"{qty}주", "inline": True},
        {"name": "체결가", "value": _fmt_price(price), "inline": True},
        {"name": "매수금액", "value": _fmt_price(total_amount), "inline": True},
        {"name": "익절 지정가 1", "value": _fmt_price(tp1_price), "inline": True},
        {"name": "익절 지정가 2", "value": _fmt_price(tp2_price), "inline": True},
    ]

    # 전략 지표 (signal_data가 있으면 추가)
    if signal_data:
        indicator_lines = []
        if "vol_ratio" in signal_data:
            indicator_lines.append(f"거래량: 평균 대비 **{signal_data['vol_ratio']:.1f}배**")
        if "trend" in signal_data:
            indicator_lines.append(f"추세: {signal_data['trend']}")
        if "breakout" in signal_data:
            indicator_lines.append(f"돌파: {signal_data['breakout']}")
        if "candle_strength" in signal_data:
            indicator_lines.append(f"캔들강도: **{signal_data['candle_strength']:.0f}%**")
        if indicator_lines:
            fields.append({
                "name": "📈 강세 지표",
                "value": "\n".join(indicator_lines),
                "inline": False
            })

    embed = {
        "title": "🟢 매수 체결",
        "color": COLOR_GREEN,
        "fields": fields,
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)


# ==================================================
# 3) 손절 알림
# ==================================================
def notify_stop_loss(
    code: str,
    name: str,
    qty: int,
    sell_price: int,
    entry_price: int
):
    pnl_rate = (sell_price - entry_price) / entry_price * 100
    pnl_amount = (sell_price - entry_price) * qty

    embed = {
        "title": "❌ 손절",
        "color": COLOR_RED,
        "fields": [
            {"name": "종목", "value": f"{name} ({code})", "inline": False},
            {"name": "매도가", "value": _fmt_price(sell_price), "inline": True},
            {"name": "수량", "value": f"{qty}주", "inline": True},
            {"name": "손해율", "value": f"{pnl_rate:.2f}%", "inline": True},
            {"name": "손해금액", "value": f"{pnl_amount:,}원", "inline": True},
        ],
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)


# ==================================================
# 4) TP1 부분익절 알림
# ==================================================
def notify_tp1_fill(
    code: str,
    name: str,
    qty: int,
    sell_price: int,
    entry_price: int,
    remain_qty: int
):
    pnl_rate = (sell_price - entry_price) / entry_price * 100
    pnl_amount = (sell_price - entry_price) * qty

    embed = {
        "title": "💰 TP1 부분익절",
        "color": COLOR_GREEN,
        "fields": [
            {"name": "종목", "value": f"{name} ({code})", "inline": False},
            {"name": "매도가", "value": _fmt_price(sell_price), "inline": True},
            {"name": "수량", "value": f"{qty}주", "inline": True},
            {"name": "수익률", "value": f"+{pnl_rate:.2f}%", "inline": True},
            {"name": "손익금액", "value": f"+{pnl_amount:,}원", "inline": True},
            {"name": "잔여수량", "value": f"{remain_qty}주", "inline": True},
            {"name": "다음목표", "value": "TP2 / 트레일링 대기", "inline": False},
        ],
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)


# ==================================================
# 5) TP2 익절 알림
# ==================================================
def notify_tp2_fill(
    code: str,
    name: str,
    qty: int,
    sell_price: int,
    entry_price: int,
    remain_qty: int
):
    pnl_rate = (sell_price - entry_price) / entry_price * 100
    pnl_amount = (sell_price - entry_price) * qty

    embed = {
        "title": "🚀 TP2 익절",
        "color": COLOR_GREEN,
        "fields": [
            {"name": "종목", "value": f"{name} ({code})", "inline": False},
            {"name": "매도가", "value": _fmt_price(sell_price), "inline": True},
            {"name": "수량", "value": f"{qty}주", "inline": True},
            {"name": "수익률", "value": f"+{pnl_rate:.2f}%", "inline": True},
            {"name": "손익금액", "value": f"+{pnl_amount:,}원", "inline": True},
            {"name": "잔여수량", "value": f"{remain_qty}주", "inline": True},
            {"name": "", "value": "📈 트레일링 시작", "inline": False},
        ],
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)


# ==================================================
# 6) 본절 보호 매도 알림
# ==================================================
def notify_profit_safe(
    code: str,
    name: str,
    qty: int,
    sell_price: int,
    entry_price: int
):
    pnl_rate = (sell_price - entry_price) / entry_price * 100
    pnl_amount = (sell_price - entry_price) * qty

    embed = {
        "title": "🛡 본절 보호 매도",
        "color": COLOR_YELLOW,
        "fields": [
            {"name": "종목", "value": f"{name} ({code})", "inline": False},
            {"name": "매도가", "value": _fmt_price(sell_price), "inline": True},
            {"name": "수량", "value": f"{qty}주", "inline": True},
            {"name": "수익률", "value": f"{pnl_rate:+.2f}%", "inline": True},
            {"name": "손익금액", "value": f"{pnl_amount:+,}원", "inline": True},
            {"name": "", "value": "TP1 이후 하락 방어", "inline": False},
        ],
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)


# ==================================================
# 7) 트레일링 청산 알림
# ==================================================
def notify_trail_stop(
    code: str,
    name: str,
    qty: int,
    sell_price: int,
    entry_price: int
):
    pnl_rate = (sell_price - entry_price) / entry_price * 100
    pnl_amount = (sell_price - entry_price) * qty

    embed = {
        "title": "📉 트레일링 청산",
        "color": COLOR_PURPLE,
        "fields": [
            {"name": "종목", "value": f"{name} ({code})", "inline": False},
            {"name": "매도가", "value": _fmt_price(sell_price), "inline": True},
            {"name": "수량", "value": f"{qty}주", "inline": True},
            {"name": "수익률", "value": f"{pnl_rate:+.2f}%", "inline": True},
            {"name": "손익금액", "value": f"{pnl_amount:+,}원", "inline": True},
            {"name": "", "value": "고점 대비 하락 청산", "inline": False},
        ],
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)


# ==================================================
# 8) TIME STOP / VOL TIME STOP 알림
# ==================================================
def notify_time_stop(
    code: str,
    name: str,
    qty: int,
    sell_price: int,
    entry_price: int,
    reason: str = "TIME_STOP"
):
    pnl_rate = (sell_price - entry_price) / entry_price * 100
    pnl_amount = (sell_price - entry_price) * qty

    title = "⏱ 타임스탑 청산" if reason == "TIME_STOP" else "📉 거래량 급감 청산"

    embed = {
        "title": title,
        "color": COLOR_YELLOW,
        "fields": [
            {"name": "종목", "value": f"{name} ({code})", "inline": False},
            {"name": "매도가", "value": _fmt_price(sell_price), "inline": True},
            {"name": "수량", "value": f"{qty}주", "inline": True},
            {"name": "수익률", "value": f"{pnl_rate:+.2f}%", "inline": True},
            {"name": "손익금액", "value": f"{pnl_amount:+,}원", "inline": True},
        ],
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)


# ==================================================
# 9) 강제 청산 알림
# ==================================================
def notify_force_liquidation(
    code: str,
    name: str,
    qty: int,
    entry_price: int
):
    embed = {
        "title": "🚨 14:50 강제 청산",
        "color": COLOR_ORANGE,
        "fields": [
            {"name": "종목", "value": f"{name} ({code})", "inline": False},
            {"name": "수량", "value": f"{qty}주", "inline": True},
            {"name": "매수가", "value": _fmt_price(entry_price), "inline": True},
            {"name": "", "value": "장 마감 전 강제 시장가 매도", "inline": False},
        ],
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)


# ==================================================
# 10) 범용 알림 (시스템 메시지, 에러 등)
# ==================================================
def notify_system(msg: str, level: str = "INFO"):
    color_map = {
        "INFO": COLOR_BLUE,
        "WARNING": COLOR_YELLOW,
        "CRITICAL": COLOR_RED,
    }
    embed = {
        "title": "🚨 AUTO-TRADE ALERT",
        "description": msg,
        "color": color_map.get(level, COLOR_YELLOW),
        "footer": {"text": "Kiwoom Auto-Trade"},
        "timestamp": datetime.utcnow().isoformat()
    }
    _send(embed)
