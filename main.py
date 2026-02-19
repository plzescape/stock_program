import sys
from PyQt5.QtWidgets import QApplication
from kiwoom_api import KiwoomAPI
from config import IS_REAL
from PyQt5.QtCore import QTimer
from datetime import datetime, time
import requests

MAX_SELF_CHECK_RETRY = 10   # 최대 재시도 횟수

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

        # ── 디스코드 장 시작 알림 ──
        try:
            from discord_notify import notify_market_open
            notify_market_open(
                account_no=api.get_account(),
                is_real=IS_REAL,
                position_count=len(api.positions)
            )
        except Exception as e:
            print(f"⚠️ Discord 장시작 알림 실패: {e}")

        api.run_condition_cycle()

        # ⭐ 조건검색 주기 타이머 (CONDITION_INTERVAL_MIN마다 자동 재실행)
        from config import CONDITION_INTERVAL_MIN
        api._cond_cycle_timer = QTimer()
        api._cond_cycle_timer.timeout.connect(api.run_condition_cycle)
        api._cond_cycle_timer.start(CONDITION_INTERVAL_MIN * 60 * 1000)
        print(f"🔄 조건검색 주기 타이머 시작 ({CONDITION_INTERVAL_MIN}분)")
        
        return

#===== 강제 청산 예약 =====        
def schedule_force_liquidation(api):

    now = datetime.now()

    liquidation_time = datetime.combine(
        now.date(),
        time(14, 50)
    )

    delay_ms = max(
        0,
        int((liquidation_time - now).total_seconds() * 1000)
    )

    QTimer.singleShot(
        delay_ms,
        lambda: api.force_liquidation_all()
    )

#===== 메인 함수 =====        
def main():
    app = QApplication(sys.argv)
    api = KiwoomAPI()

    # 1) 로그인
    api.login()

    # 🔍 계좌 테스트 (월요일 아침에 꼭 한 번 실행)
    test_account(api)

    # 2) 조건검색식 로드 및 등록 - 0 : 현재 기준만, 1 : 조건 + 실시간  
    api.load_conditions()

    # 2-0) 섹터 필터 초기화
    # GetThemeGroupList() + GetThemeGroupCode() 로 섹터 구성 종목 로드 (동기, 즉시 완료)
    api.sector_filter.initialize()

    # 2-0-1) 섹터 등락률 주기 갱신 타이머 (OPT90001, 5분 간격)
    # 첫 갱신은 장 시작 1분 후, 이후 5분마다 자동 갱신
    sector_timer = QTimer()
    sector_timer.setInterval(5 * 60 * 1000)  # 5분
    sector_timer.timeout.connect(api.sector_filter.refresh_sector_status)
    api._sector_refresh_timer = sector_timer  # GC 방지

    now = datetime.now()
    market_open = datetime.combine(now.date(), time(9, 0))
    first_refresh_delay = max(60_000, int((market_open - now).total_seconds() * 1000) + 60_000)
    QTimer.singleShot(first_refresh_delay, lambda: (
        api.sector_filter.refresh_sector_status(),
        sector_timer.start()
    ))

    # 2-1) 장전 자가진단 (필수)
    ok = api.self_check("PRE_MARKET")
    if not ok:
        print("⚠️ 장전 self-check 실패 (재시도는 장 시작 후)")
    else:
        print("🟢 장전 self-check 통과")
           
    # 2-2) 장 시작 후 자동매매 활성화 예약
    now = datetime.now()
    market_open = datetime.combine(now.date(), time(9, 0))
    delay_ms = max(0, int((market_open - now).total_seconds() * 1000))

    QTimer.singleShot(delay_ms + 3000,  lambda: enable_auto_trade(api))    
    # enable_auto_trade(api)
    # 2-3) 조건검색 실행
    # api.run_condition_cycle()

    # =========================
    # ⏰ 14:50 강제청산 예약
    # =========================
    schedule_force_liquidation(api)

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
