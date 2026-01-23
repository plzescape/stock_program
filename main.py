import sys
from PyQt5.QtWidgets import QApplication
from kiwoom_api import KiwoomAPI

def main():
    app = QApplication(sys.argv)
    api = KiwoomAPI()

    # 1) 로그인
    api.login()

    # 2) 조건검색식 로드 (필수)
    api.load_conditions()

    # 3) 08:50 실행해도 09:00 이후 자동으로 조건검색이 돌도록 스케줄러 시작
    api.start_condition_scheduler()

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
