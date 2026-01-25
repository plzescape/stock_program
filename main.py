import sys
from PyQt5.QtWidgets import QApplication
from kiwoom_api import KiwoomAPI
from config import IS_REAL

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

def main():
    app = QApplication(sys.argv)
    api = KiwoomAPI()

    # 1) 로그인
    api.login()

    # 🔍 계좌 테스트 (월요일 아침에 꼭 한 번 실행)
    test_account(api)

    # 2) 조건검색식 로드 (필수)
    api.load_conditions()

    # 3) 08:50 실행해도 09:00 이후 자동으로 조건검색이 돌도록 스케줄러 시작
    api.start_condition_scheduler()

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
