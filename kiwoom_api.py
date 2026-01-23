from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop


class KiwoomAPI(QAxWidget):
    def __init__(self):
        super().__init__()

        # ====== 반드시 제일 먼저 ======
        self.tr_data = {}
        self._expected_rqname = None

        # ====== OpenAPI 컨트롤 ======
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")

        # ====== 이벤트 연결 ======
        self.OnEventConnect.connect(self._on_event_connect)
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveMsg.connect(self._on_receive_msg)

        # ====== 이벤트 루프 ======
        self._login_loop = QEventLoop()
        self._tr_loop = QEventLoop()

    # ================= 로그인 =================
    def login(self):
        self.dynamicCall("CommConnect()")
        self._login_loop.exec_()

    def _on_event_connect(self, err_code):
        print("[OnEventConnect] err_code =", err_code)
        if self._login_loop.isRunning():
            self._login_loop.exit()

    # ================= 기본 API =================
    def get_login_info(self, tag):
        return self.dynamicCall("GetLoginInfo(QString)", tag)

    def get_first_account(self):
        return self.get_login_info("ACCNO").split(";")[0]

    def set_input_value(self, key, value):
        self.dynamicCall("SetInputValue(QString, QString)", key, value)

    def request_tr(self, rqname, trcode, screen_no="2000", prevnext=0):
        self._expected_rqname = rqname
        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname, trcode, prevnext, screen_no
        )
        self._tr_loop.exec_()
        return self.tr_data.get(rqname)

    def get_comm_data(self, trcode, rqname, index, item):
        return self.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            trcode, rqname, index, item
        ).strip()

    def get_repeat_cnt(self, trcode, rqname):
        return self.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)

    # ================= 잔고 조회 =================
    def request_balance_opw00018(self, acc_no, password="0000"):
        self.set_input_value("계좌번호", acc_no)
        self.set_input_value("비밀번호", password)
        self.set_input_value("비밀번호입력매체구분", "00")
        self.set_input_value("조회구분", "2")
        return self.request_tr("RQ_BALANCE", "OPW00018")

    # ================= TR 수신 =================
    def _on_receive_tr_data(self, screen_no, rqname, trcode,
                            record_name, prevnext,
                            data_len, err_code, msg1, msg2):

        print(">>> OnReceiveTrData:", rqname)

        if rqname != self._expected_rqname:
            print(">>> 다른 TR 무시")
            return

        if rqname == "RQ_BALANCE":
            rows = self.get_repeat_cnt(trcode, rqname)

            summary = {
                "예수금": self.get_comm_data(trcode, rqname, 0, "예수금"),
                "총매입금액": self.get_comm_data(trcode, rqname, 0, "총매입금액"),
                "총평가금액": self.get_comm_data(trcode, rqname, 0, "총평가금액"),
                "총평가손익금액": self.get_comm_data(trcode, rqname, 0, "총평가손익금액"),
            }

            items = []
            for i in range(rows):
                items.append({
                    "종목번호": self.get_comm_data(trcode, rqname, i, "종목번호"),
                    "종목명": self.get_comm_data(trcode, rqname, i, "종목명"),
                    "보유수량": self.get_comm_data(trcode, rqname, i, "보유수량"),
                    "현재가": self.get_comm_data(trcode, rqname, i, "현재가"),
                })

            self.tr_data[rqname] = {
                "summary": summary,
                "items": items
            }

        if self._tr_loop.isRunning():
            self._tr_loop.exit()

    # ================= 메시지 =================
    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        print("[MSG]", msg)
