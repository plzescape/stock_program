from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer
from config import *
from logger_util import setup_logger
from strategy import is_entry_candidate, is_market_time

class KiwoomAPI(QAxWidget):
    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")

        self.log_system = setup_logger("system", "system.log")
        self.log_trade  = setup_logger("trade", "trade.log")
        self.log_signal = setup_logger("signal", "signal.log")

        self.watchlist = []
        self.position = None
        self.entry_price = None
        self.remain_qty = 0
        self.highest_price = 0

        self.tp1_done = False
        self.tp2_done = False
        self.trailing_active = False

        self.OnEventConnect.connect(self._on_event_connect)
        self.OnReceiveTrCondition.connect(self._on_receive_tr_condition)
        self.OnReceiveRealData.connect(self._on_receive_real_data)

        self.cond_timer = QTimer()
        self.cond_timer.timeout.connect(self.run_condition_cycle)

    def login(self):
        self.dynamicCall("CommConnect()")
        self.login_loop = QEventLoop()
        self.login_loop.exec_()

    def _on_event_connect(self, err):
        self.log_system.info(f"[LOGIN] err={err}")
        self.login_loop.exit()

    def run_condition_cycle(self):
        if self.position is not None:
            return
        if not is_market_time():
            return

        self.dynamicCall(
            "SendCondition(QString, QString, int, int)",
            "9000", CONDITION_NAME, CONDITION_INDEX, 0
        )

    def _on_receive_tr_condition(self, screen, codes, name, index, next):
        self.watchlist = [c for c in codes.split(";") if c]
        self.log_signal.info(f"[CONDITION] {self.watchlist}")
        self.try_trade()

    def request_3min_candle(self, code):
        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "틱범위", "3")
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")
        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            "RQ_3MIN", "OPT10080", 0, "9100"
        )

    def parse_3min(self, trcode, rqname):
        rows = self.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)
        candles = []
        for i in range(min(rows, 3)):
            close = abs(int(self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode, rqname, i, "현재가"
            )))
            volume = int(self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode, rqname, i, "거래량"
            ))
            candles.append({"close": close, "volume": volume})
        return candles

    def try_trade(self):
        for code in self.watchlist:
            candles = self.parse_3min("OPT10080", "RQ_3MIN")
            if is_entry_candidate(candles, self.log_signal, code):
                self.buy(code)
                break

    def buy(self, code):
        price = abs(int(self.dynamicCall("GetMasterLastPrice(QString)", code)))
        self.entry_price = price
        self.highest_price = price
        self.remain_qty = QTY
        self.position = code
        self.log_trade.info(f"[BUY] {code} {price} x{QTY}")

    def sell_all(self, code, reason):
        self.log_trade.info(f"[SELL_ALL] {code} reason={reason}")
        self.position = None
        self.remain_qty = 0
        self.trailing_active = False

    def _on_receive_real_data(self, code, real_type, data):
        if self.position != code:
            return

        current = abs(int(self.dynamicCall(
            "GetCommRealData(QString, int)", code, 10
        )))

        if current > self.highest_price:
            self.highest_price = current

        if current <= self.entry_price * (1 - STOP_LOSS_RATE):
            self.sell_all(code, "STOP")
            return

        if self.trailing_active:
            stop_price = self.highest_price * (1 - TRAIL_GAP)
            if current <= stop_price:
                self.sell_all(code, "TRAIL")
