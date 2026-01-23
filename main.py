import sys
from PyQt5.QtWidgets import QApplication
from kiwoom_api import KiwoomAPI
from config import CONDITION_INTERVAL_MIN

def main():
    app = QApplication(sys.argv)
    api = KiwoomAPI()
    api.login()

    api.run_condition_cycle()
    api.cond_timer.start(CONDITION_INTERVAL_MIN * 60 * 1000)

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
