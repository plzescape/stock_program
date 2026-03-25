"""
discord_notify.py — 디스코드 웹훅 알림

[알림 종류]
  notify_market_open       장 시작
  notify_buy_fill          매수 체결
  notify_tp1_fill          TP1 체결
  notify_tp2_fill          TP2 체결
  notify_stop_loss         손절 청산
  notify_profit_safe       본절보호 청산
  notify_trail_stop        트레일링 스탑 청산
  notify_time_stop         타임스탑 청산
  notify_force_liquidation 강제청산

[누락 원인 로깅]
  logs/discord.log 에 모든 전송 시도/성공/실패 기록
"""

import os
import json
import logging
import traceback
from datetime import datetime

# ══════════════════════════════════════════════════════
# 웹훅 URL
# ══════════════════════════════════════════════════════
WEBHOOK_URL = "https://discord.com/api/webhooks/1486049515801673728/X8ZvvaZ2ip1DbQvnMeb56Eelk4cs4PhtkSM_d2JdxZka7Hb448vWG1nSfzE2PsKN0b4V"

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
        log_path = os.path.join(log_dir, "discord.log")
        fh = logging.FileHandler(log_path, encoding="utf-8")
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
# 전송 함수
# ══════════════════════════════════════════════════════
def _send(content, alert_type, label):
    log = _get_logger()

    if not WEBHOOK_URL:
        log.warning(f"[DISCORD_SKIP] {alert_type} | {label} | WEBHOOK_URL 미설정")
        return False

    payload = json.dumps({"content": content})

    try:
        import requests
        log.info(f"[DISCORD_SEND] {alert_type} | {label}")
        resp = requests.post(
            WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        if resp.status_code in (200, 204):
            log.info(f"[DISCORD_OK] {alert_type} | {label} | HTTP={resp.status_code}")
            return True
        else:
            log.error(f"[DISCORD_FAIL] {alert_type} | {label} | HTTP={resp.status_code} body={resp.text[:200]}")
            return False

    except ImportError:
        import urllib.request
        import urllib.error
        try:
            log.info(f"[DISCORD_SEND_URLLIB] {alert_type} | {label}")
            data = payload.encode("utf-8")
            req = urllib.request.Request(
                WEBHOOK_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                if status in (200, 204):
                    log.info(f"[DISCORD_OK] {alert_type} | {label} | HTTP={status}")
                    return True
                else:
                    log.error(f"[DISCORD_FAIL] {alert_type} | {label} | HTTP={status}")
                    return False
        except Exception as e:
            log.error(f"[DISCORD_FAIL] {alert_type} | {label} | {type(e).__name__}: {e}\n{traceback.format_exc()}")
            return False

    except Exception as e:
        log.error(f"[DISCORD_FAIL] {alert_type} | {label} | {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return False


def _label(code, name):
    return f"{name}({code})"


def _pnl_str(entry, exit_price):
    if not entry:
        return "N/A"
    pct = (exit_price - entry) / entry * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


# ══════════════════════════════════════════════════════
# 알림 함수
# ══════════════════════════════════════════════════════

def notify_market_open(account_no="", **kwargs):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"🔔 **자동매매 시작** | {now}",
        "장이 열렸습니다. 조건검색 스캔을 시작합니다.",
    ]
    if account_no:
        lines.append(f"계좌: {account_no}")
    return _send("\n".join(lines), "장시작", "시스템")


def notify_buy_fill(code, name, qty, price, tp1_price=0, tp2_price=0, signal_data=None, **kwargs):
    lines = [
        f"📈 **매수 체결** | {name} `{code}`",
        f"체결가: **{price:,}원** × {qty:,}주  |  매수금액: {price * qty:,}원",
    ]
    if tp1_price:
        lines.append(f"TP1 목표: {tp1_price:,}원  |  TP2 목표: {tp2_price:,}원")
    if signal_data and isinstance(signal_data, dict):
        for k, v in signal_data.items():
            lines.append(f"{k}: {v}")
    return _send("\n".join(lines), "매수", _label(code, name))


def notify_tp1_fill(code, name, qty, price, entry_price, remain_qty, **kwargs):
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🎯 **TP1 체결** | {name} `{code}`",
        f"체결가: **{price:,}원** | 수익률: {pnl}",
        f"매도: {qty:,}주 | 잔여: {remain_qty:,}주 | 매수가: {entry_price:,}원",
    ]
    return _send("\n".join(lines), "TP1", _label(code, name))


def notify_tp2_fill(code, name, qty, price, entry_price, remain_qty, **kwargs):
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🎯 **TP2 체결** | {name} `{code}`",
        f"체결가: **{price:,}원** | 수익률: {pnl}",
        f"매도: {qty:,}주 | 잔여: {remain_qty:,}주 | 매수가: {entry_price:,}원",
    ]
    return _send("\n".join(lines), "TP2", _label(code, name))


def notify_stop_loss(code, name, qty, price, entry_price, **kwargs):
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🛑 **손절 청산** | {name} `{code}`",
        f"청산가: **{price:,}원** | 수익률: {pnl}",
        f"수량: {qty:,}주 | 매수가: {entry_price:,}원",
    ]
    return _send("\n".join(lines), "손절", _label(code, name))


def notify_profit_safe(code, name, qty, price, entry_price, **kwargs):
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🔒 **본절보호 청산** | {name} `{code}`",
        f"청산가: **{price:,}원** | 수익률: {pnl}",
        f"수량: {qty:,}주 | 매수가: {entry_price:,}원",
    ]
    return _send("\n".join(lines), "본절보호", _label(code, name))


def notify_trail_stop(code, name, qty, price, entry_price, **kwargs):
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🔔 **트레일링 청산** | {name} `{code}`",
        f"청산가: **{price:,}원** | 수익률: {pnl}",
        f"수량: {qty:,}주 | 매수가: {entry_price:,}원",
    ]
    return _send("\n".join(lines), "트레일링", _label(code, name))


def notify_time_stop(code, name, qty, price, entry_price, reason="TIME_STOP", **kwargs):
    pnl = _pnl_str(entry_price, price)
    reason_kr = "거래량급감" if "VOL" in reason else "타임스탑"
    lines = [
        f"⏰ **{reason_kr} 청산** | {name} `{code}`",
        f"청산가: **{price:,}원** | 수익률: {pnl}",
        f"수량: {qty:,}주 | 매수가: {entry_price:,}원",
    ]
    return _send("\n".join(lines), reason_kr, _label(code, name))


def notify_force_liquidation(code, name, qty, entry_price, **kwargs):
    lines = [
        f"🗑️ **강제청산** | {name} `{code}`",
        f"매수가: {entry_price:,}원 | 수량: {qty:,}주",
        f"시각: {datetime.now().strftime('%H:%M:%S')}",
    ]
    return _send("\n".join(lines), "강제청산", _label(code, name))
