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

#===== ⭐ BUG-3: 전일 미청산 잔고 처리 =====
def recover_and_liquidate_leftover(api: KiwoomAPI):
    """
    1) OPW00018 잔고조회 TR로 실제 보유 잔고를 positions에 복구
    2) 조회 완료 콜백으로 liquidate_leftover_positions() 자동 호출
       → 전일 잔고(entry_ts < 오늘 09:00)를 즉시 시장가 청산
    """
    print("🔍 전일 미청산 잔고 조회 중 (OPW00018)...")
    api.load_holdings_from_api(
        on_done=api.liquidate_leftover_positions
    )

#===== 강제 청산 예약 =====        
def schedule_force_liquidation(api):
    from config import FORCE_LIQUIDATION_HOUR, FORCE_LIQUIDATION_MIN

    now = datetime.now()
    liquidation_time = datetime.combine(
        now.date(),
        time(FORCE_LIQUIDATION_HOUR, FORCE_LIQUIDATION_MIN)
    )

    delay_ms = max(
        0,
        int((liquidation_time - now).total_seconds() * 1000)
    )

    print(f"⏰ 강제청산 예약: {FORCE_LIQUIDATION_HOUR:02d}:{FORCE_LIQUIDATION_MIN:02d} "
          f"(약 {delay_ms//60000}분 후)")

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

    # 2-1) 장전 자가진단 (필수)
    ok = api.self_check("PRE_MARKET")
    if not ok:
        print("⚠️ 장전 self-check 실패 (재시도는 장 시작 후)")
    else:
        print("🟢 장전 self-check 통과")

    # ⭐ BUG-3: 전일 미청산 잔고 자동 청산
    # - OPW00018 TR로 실제 보유 잔고 조회 → positions 복구 → 전일 잔고 즉시 청산
    # - 장 시작 전(08:00~08:59) 실행하면 조회만 하고, 장 시작 후(09:00~) 청산 발동
    # - 장 시작 직후 바로 청산되도록 09:00 + 5초 시점에 호출
    now = datetime.now()
    market_open = datetime.combine(now.date(), time(9, 0))
    leftover_delay_ms = max(0, int((market_open - now).total_seconds() * 1000)) + 5000

    QTimer.singleShot(leftover_delay_ms, lambda: recover_and_liquidate_leftover(api))
    print(f"⏰ 전일잔고 청산 예약: 09:00:05 (약 {leftover_delay_ms//1000}초 후)")

    # 2-2) 장 시작 후 자동매매 활성화 예약 (잔고청산 이후 10초 뒤)
    QTimer.singleShot(leftover_delay_ms + 10000, lambda: enable_auto_trade(api))
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
