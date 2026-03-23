"""
discord_notify.py — 디스코드 알림 + 전송 로깅

[알림 종류]
  notify_buy_fill        매수 체결
  notify_tp1_fill        TP1 체결
  notify_tp2_fill        TP2 체결
  notify_stop_loss       손절 청산
  notify_profit_safe     본절보호 청산
  notify_trail_stop      트레일링 스탑 청산
  notify_time_stop       타임스탑 청산
  notify_force_liquidation 강제청산(전일잔고/15:20)

[로깅 구조]
  discord.log — 전송 시도/성공/실패 전부 기록
  형식: [DISCORD_SEND] / [DISCORD_OK] / [DISCORD_FAIL] / [DISCORD_SKIP]

[누락 원인 추적 포인트]
  1. WEBHOOK_URL 미설정 → DISCORD_SKIP
  2. 네트워크 오류 → DISCORD_FAIL (상세 에러)
  3. 4xx/5xx 응답 → DISCORD_FAIL (응답 코드)
  4. 타임아웃 → DISCORD_FAIL (timeout)
  5. 예외 → DISCORD_FAIL (traceback)
"""

import os
import json
import logging
import traceback
import urllib.request
import urllib.error
from datetime import datetime

# ══════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════

# 웹훅 URL — 실제 URL로 교체하거나 환경변수로 관리
WEBHOOK_URL: str = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1465022221994037339/mOln_VqVzMAQRInrUic86Ysr482ZDIjMFB6EoUR9U3dUpMkqTw-Keyf0InVpUwE2IkTu"
)

# 전송 타임아웃 (초)
SEND_TIMEOUT: int = 5

# 재시도 횟수 (일시적 네트워크 오류 대응)
MAX_RETRY: int = 2

# ══════════════════════════════════════════════════════
# 로거 설정 — discord.log 전용
# ══════════════════════════════════════════════════════

def _get_logger() -> logging.Logger:
    logger = logging.getLogger("discord_notify")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "discord.log")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# ══════════════════════════════════════════════════════
# 핵심 전송 함수
# ══════════════════════════════════════════════════════

def _send(payload: dict, alert_type: str, label: str) -> bool:
    """
    웹훅 전송 + 로깅.
    모든 성공/실패를 discord.log에 기록한다.

    alert_type : 알림 종류 (예: "매수", "손절", "TP1" ...)
    label      : 종목명(코드) 등 식별자
    반환       : 성공=True, 실패=False
    """
    log = _get_logger()

    if not WEBHOOK_URL:
        log.warning(
            f"[DISCORD_SKIP] {alert_type} | {label} | "
            f"WEBHOOK_URL 미설정 — discord_notify.py의 WEBHOOK_URL을 설정하세요"
        )
        return False

    data = json.dumps(payload).encode("utf-8")

    for attempt in range(1, MAX_RETRY + 1):
        try:
            log.info(
                f"[DISCORD_SEND] {alert_type} | {label} | "
                f"시도={attempt}/{MAX_RETRY} payload_len={len(data)}"
            )

            req = urllib.request.Request(
                WEBHOOK_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
                status = resp.status
                if status in (200, 204):
                    log.info(
                        f"[DISCORD_OK] {alert_type} | {label} | "
                        f"HTTP={status} 시도={attempt}"
                    )
                    return True
                else:
                    body = resp.read(200).decode("utf-8", errors="ignore")
                    log.error(
                        f"[DISCORD_FAIL] {alert_type} | {label} | "
                        f"HTTP={status} body={body!r} 시도={attempt}"
                    )

        except urllib.error.HTTPError as e:
            body = e.read(200).decode("utf-8", errors="ignore") if e.fp else ""
            log.error(
                f"[DISCORD_FAIL] {alert_type} | {label} | "
                f"HTTPError={e.code} reason={e.reason} body={body!r} 시도={attempt}"
            )
        except urllib.error.URLError as e:
            log.error(
                f"[DISCORD_FAIL] {alert_type} | {label} | "
                f"URLError={e.reason} 시도={attempt}"
            )
        except TimeoutError:
            log.error(
                f"[DISCORD_FAIL] {alert_type} | {label} | "
                f"Timeout={SEND_TIMEOUT}초 초과 시도={attempt}"
            )
        except Exception:
            log.error(
                f"[DISCORD_FAIL] {alert_type} | {label} | "
                f"예외발생 시도={attempt}\n{traceback.format_exc()}"
            )

    log.error(
        f"[DISCORD_GIVE_UP] {alert_type} | {label} | "
        f"{MAX_RETRY}회 전부 실패"
    )
    return False


def _label(code: str, name: str) -> str:
    return f"{name}({code})"


def _pnl_str(entry: int, exit_price: int) -> str:
    if entry <= 0:
        return "N/A"
    pct = (exit_price - entry) / entry * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


# ══════════════════════════════════════════════════════
# 알림 함수들
# ══════════════════════════════════════════════════════

def notify_buy_fill(
    code: str,
    name: str,
    qty: int,
    price: int,
    tp1_price: int = 0,
    tp2_price: int = 0,
    signal_data: dict | None = None,
) -> bool:
    """매수 체결 알림"""
    label = _label(code, name)
    lines = [
        f"📈 **매수 체결** | {name} `{code}`",
        f"체결가: **{price:,}원** × {qty:,}주",
        f"매수금액: {price * qty:,}원",
    ]
    if tp1_price:
        lines.append(f"TP1 목표: {tp1_price:,}원")
    if tp2_price:
        lines.append(f"TP2 목표: {tp2_price:,}원")
    if signal_data:
        for k, v in signal_data.items():
            lines.append(f"{k}: {v}")

    payload = {"content": "\n".join(lines)}
    return _send(payload, "매수", label)


def notify_tp1_fill(
    code: str,
    name: str,
    qty: int,
    price: int,
    entry_price: int,
    remain_qty: int,
) -> bool:
    """TP1 체결 알림"""
    label = _label(code, name)
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🎯 **TP1 체결** | {name} `{code}`",
        f"체결가: **{price:,}원** | 수익률: {pnl}",
        f"매도수량: {qty:,}주 | 잔여: {remain_qty:,}주",
        f"매수가: {entry_price:,}원",
    ]
    payload = {"content": "\n".join(lines)}
    return _send(payload, "TP1", label)


def notify_tp2_fill(
    code: str,
    name: str,
    qty: int,
    price: int,
    entry_price: int,
    remain_qty: int,
) -> bool:
    """TP2 체결 알림"""
    label = _label(code, name)
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🎯 **TP2 체결** | {name} `{code}`",
        f"체결가: **{price:,}원** | 수익률: {pnl}",
        f"매도수량: {qty:,}주 | 잔여: {remain_qty:,}주",
        f"매수가: {entry_price:,}원",
    ]
    payload = {"content": "\n".join(lines)}
    return _send(payload, "TP2", label)


def notify_stop_loss(
    code: str,
    name: str,
    qty: int,
    price: int,
    entry_price: int,
) -> bool:
    """손절 청산 알림"""
    label = _label(code, name)
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🛑 **손절 청산** | {name} `{code}`",
        f"청산가: **{price:,}원** | 수익률: {pnl}",
        f"매도수량: {qty:,}주 | 매수가: {entry_price:,}원",
    ]
    payload = {"content": "\n".join(lines)}
    return _send(payload, "손절", label)


def notify_profit_safe(
    code: str,
    name: str,
    qty: int,
    price: int,
    entry_price: int,
) -> bool:
    """본절보호 청산 알림"""
    label = _label(code, name)
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🔒 **본절보호 청산** | {name} `{code}`",
        f"청산가: **{price:,}원** | 수익률: {pnl}",
        f"매도수량: {qty:,}주 | 매수가: {entry_price:,}원",
    ]
    payload = {"content": "\n".join(lines)}
    return _send(payload, "본절보호", label)


def notify_trail_stop(
    code: str,
    name: str,
    qty: int,
    price: int,
    entry_price: int,
) -> bool:
    """트레일링 스탑 청산 알림"""
    label = _label(code, name)
    pnl = _pnl_str(entry_price, price)
    lines = [
        f"🔔 **트레일링 청산** | {name} `{code}`",
        f"청산가: **{price:,}원** | 수익률: {pnl}",
        f"매도수량: {qty:,}주 | 매수가: {entry_price:,}원",
    ]
    payload = {"content": "\n".join(lines)}
    return _send(payload, "트레일링", label)


def notify_time_stop(
    code: str,
    name: str,
    qty: int,
    price: int,
    entry_price: int,
    reason: str = "TIME_STOP",
) -> bool:
    """타임스탑 청산 알림"""
    label = _label(code, name)
    pnl = _pnl_str(entry_price, price)
    reason_kr = "거래량급감" if "VOL" in reason else "타임스탑"
    lines = [
        f"⏰ **{reason_kr} 청산** | {name} `{code}`",
        f"청산가: **{price:,}원** | 수익률: {pnl}",
        f"매도수량: {qty:,}주 | 매수가: {entry_price:,}원",
    ]
    payload = {"content": "\n".join(lines)}
    return _send(payload, reason_kr, label)


def notify_force_liquidation(
    code: str,
    name: str,
    qty: int,
    entry_price: int,
) -> bool:
    """강제청산 알림 (전일잔고/15:20 강제청산)"""
    label = _label(code, name)
    lines = [
        f"🗑️ **강제청산** | {name} `{code}`",
        f"매수가: {entry_price:,}원 | 수량: {qty:,}주",
        f"시각: {datetime.now().strftime('%H:%M:%S')}",
    ]
    payload = {"content": "\n".join(lines)}
    return _send(payload, "강제청산", label)


def notify_market_open(account_no: str = "", **kwargs) -> bool:
    """장 시작 알림"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"🔔 **자동매매 시작** | {now}",
        "장이 열렸습니다. 조건검색 스캔을 시작합니다.",
    ]
    if account_no:
        lines.append(f"계좌: {account_no}")
    payload = {"content": "\n".join(lines)}
    return _send(payload, "장시작", "시스템")
