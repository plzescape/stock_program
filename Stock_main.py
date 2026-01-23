import sys
from PyQt5.QtWidgets import QApplication
from kiwoom_api import KiwoomAPI

def main():
    app = QApplication(sys.argv)
    api = KiwoomAPI()

    # 로그인
    api.login()

    # 계좌
    acc = api.get_first_account()
    print("첫 계좌:", acc)

    # 잔고
    bal = api.request_balance_opw00018(acc)

    print("===== 잔고 결과 =====")
    print("요약:", bal["summary"])
    print("보유종목:", bal["items"])

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
