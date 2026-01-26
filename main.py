import sys
from PyQt5.QtWidgets import QApplication
from kiwoom_api import KiwoomAPI
from config import IS_REAL
from PyQt5.QtCore import QTimer
from datetime import datetime, time
import requests

MAX_SELF_CHECK_RETRY = 10   # 최대 재시도 횟수
SELF_CHECK_RETRY_SEC = 60

#===== 디스코드 웹훅 URL =====
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1465022221994037339/mOln_VqVzMAQRInrUic86Ysr482ZDIjMFB6EoUR9U3dUpMkqTw-Keyf0InVpUwE2IkTu"

#===== 운영자 알림 - 디스코드 =====
def notify_operator(
    msg: str,
    level: str = "WARNING",
    retry_info: str | None = None
):
    """
    level: INFO | WARNING | CRITICAL
    """

    color_map = {
        "INFO": 0x2ECC71,      # 초록
        "WARNING": 0xF1C40F,   # 노랑
        "CRITICAL": 0xE74C3C   # 빨강
    }

    embed = {
        "title": "🚨 AUTO-TRADE ALERT",
        "description": msg,
        "color": color_map.get(level, 0xF1C40F),
        "fields": [],
        "footer": {
            "text": "Kiwoom Auto-Trade Monitor"
        },
        "timestamp": datetime.utcnow().isoformat()
    }

    if retry_info:
        embed["fields"].append({
            "name": "Retry Status",
            "value": retry_info,
            "inline": False
        })

    payload = {
        "embeds": [embed]
    }

    try:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=3
        )
    except Exception as e:
        print("⚠️ Discord 알림 실패:", e)

#===== 계좌 테스트 =====
def test_account(api: KiwoomAPI):
    print("===== 계좌 테스트 =====")
    raw = api.dynamicCall("GetLoginInfo(QString)", "ACCNO")
    accounts = [a.strip() for a in raw.split(";") if a.strip()]
    print("전체 계좌:", accounts)

    selected = api.get_account()
    print("선택된 계좌:", selected)

    if IS_REAL:
        print("⚠️ 실전 모드")
    else:
        print("🟢 모의투자 모드")

#===== 자동매매 활성화 =====
def enable_auto_trade(api: KiwoomAPI):
    if api.auto_trade_enabled:
        return  # 이미 활성화됨
        
    ok = api.self_check("POST_MARKET_OPEN")
    if ok:
        api.auto_trade_enabled = True
        notify_operator(
            "self-check 통과\n자동매매가 활성화되었습니다.",
            level="INFO"
        )
        return

    if api.self_check_retry_count >= MAX_SELF_CHECK_RETRY:
        notify_operator(
            "self-check 재시도 초과\n오늘 자동매매는 실행되지 않습니다.",
            level="CRITICAL"
        )
        return

    notify_operator(
        "self-check 실패\n계좌 / 포지션 / 주문 상태 확인 필요",
        level="WARNING",
        retry_info=f"{api.self_check_retry_count}/{MAX_SELF_CHECK_RETRY}"
    )
        
    print(
        f"⚠️ self-check 실패 "
        f"({api.self_check_retry_count}/{MAX_SELF_CHECK_RETRY}) → 재시도 예정"
    )
    QTimer.singleShot(SELF_CHECK_RETRY_SEC * 1000,  lambda: enable_auto_trade(api))

   
#===== 메인 함수 =====        
def main():
    app = QApplication(sys.argv)
    api = KiwoomAPI()

    # 1) 로그인
    api.login()

    # 🔍 계좌 테스트 (월요일 아침에 꼭 한 번 실행)
    test_account(api)

    # 2) 조건검색식 로드 (필수)
    api.load_conditions()

    # 2-1) 장전 자가진단 (필수)
    ok = api.self_check("PRE_MARKET")
    if not ok:
        print("⚠️ 장전 self-check 실패 (재시도는 장 시작 후)")
    else:
        print("🟢 장전 self-check 통과")

    # 테스트용: 조건검색 결과 바로 받아보기
    # api.test_realtime_condition_in()

    # 2-2) 장 시작 후 자동매매 활성화 예약
    now = datetime.now()
    market_open = datetime.combine(now.date(), time(9, 0))
    delay_ms = max(0, int((market_open - now).total_seconds() * 1000))

    QTimer.singleShot(delay_ms + 3000,  lambda: enable_auto_trade(api))    
    
    # 3) 08:50 실행해도 09:00 이후 자동으로 조건검색이 돌도록 스케줄러 시작
    api.start_realtime_condition()

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
