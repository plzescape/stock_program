import sys
from PyQt5.QtWidgets import QApplication
from kiwoom_api import KiwoomAPI
from config import IS_REAL
from PyQt5.QtCore import QTimer
from datetime import datetime, time

MAX_SELF_CHECK_RETRY = 10   # 최대 재시도 횟수
SELF_CHECK_RETRY_SEC = 60

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

def enable_auto_trade(api: KiwoomAPI):
    ok = api.self_check("POST_MARKET_OPEN")
    if ok:
        api.auto_trade_enabled = True
        print("✅ self-check 통과 → 자동매매 활성화")
        return

    if api.self_check_retry_count >= MAX_SELF_CHECK_RETRY:
        print("❌ self-check 재시도 초과 → 오늘은 관망")
        return
    
    print(
        f"⚠️ self-check 실패 "
        f"({api.self_check_retry_count}/{MAX_SELF_CHECK_RETRY}) → 재시도 예정"
    )
    QTimer.singleShot(SELF_CHECK_RETRY_SEC * 1000,  lambda: enable_auto_trade(api))
    
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

    # 2-2) 장 시작 후 자동매매 활성화 예약
    now = datetime.now()
    market_open = datetime.combine(now.date(), time(9, 0))
    delay_ms = max(0, int((market_open - now).total_seconds() * 1000))

    QTimer.singleShot(delay_ms + 3000,  lambda: enable_auto_trade(api))    
    
    # 3) 08:50 실행해도 09:00 이후 자동으로 조건검색이 돌도록 스케줄러 시작
    api.start_condition_scheduler()

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
