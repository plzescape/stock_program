from __future__ import annotations

from datetime import datetime, time
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer

from config import (
    IS_REAL, ACCOUNT_NO, QTY,
    STOP_LOSS_RATE, TP1_RATE, TP1_RATIO, TP2_RATE, TP2_RATIO,
    TRAIL_START_RATE, TRAIL_GAP,
    MAX_TRADES_PER_DAY, CONDITION_INTERVAL_MIN,
    CONDITION_NAME, CONDITION_INDEX,
    SCAN_MAX_CODES, SCAN_TR_DELAY_MS, MOCK_ACCOUNT_NO
)
from logger_util import setup_logger
from strategy import is_market_time, is_entry_candidate


class KiwoomAPI(QAxWidget):
    """키움 OpenAPI+ (OCX) 래퍼 + 모의투자 자동매매용 엔진"""

    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")

        # ===== 로그 =====
        self.log_system = setup_logger("system", "system.log")
        self.log_trade  = setup_logger("trade",  "trade.log")
        self.log_signal = setup_logger("signal", "signal.log")

        # ===== 이벤트 연결 =====
        self.OnEventConnect.connect(self._on_event_connect)
        self.OnReceiveConditionVer.connect(self._on_receive_condition_ver)
        self.OnReceiveRealCondition.connect(self._on_receive_real_condition)   # 🔥 변경
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveRealData.connect(self._on_receive_real_data)
        self.OnReceiveChejanData.connect(self._on_receive_chejan_data)
        self.OnReceiveMsg.connect(self._on_receive_msg)

        # ===== 루프 =====
        self._login_loop: QEventLoop | None = None
        self._cond_loop: QEventLoop | None = None
        self._tr_loop: QEventLoop | None = None

        # ===== TR 상태 =====
        self._last_tr_rqname = None
        self._last_tr_trcode = None

        # ===== 조건검색 상태 =====
        self.watchlist: list[str] = []
        self._scan_queue: list[str] = []
        self._scan_running = False

        self.realtime_condition_started = False   # 🔥 추가

        # ===== 주문/포지션 상태 =====
        self.position = None
        self.entry_price = None
        self.highest_price = 0
        self.total_qty = QTY
        self.remain_qty = 0

        self.tp1_done = False
        self.tp2_done = False
        self.trailing_active = False

        self.ordering = False
        self._pending_buy_code = None
        self._pending_buy_qty = 0

        # ===== 일일 제한 =====
        self._today = datetime.now().date()
        self.daily_trade_count = 0
        self.traded_today: set[str] = set()

        # ===== 자동매매 상태 =====
        self.auto_trade_enabled = False
        self.self_check_retry_count = 0

    # -------------------------------------------------
    # 로그인 / 조건검색 로드
    # -------------------------------------------------
    def login(self):
        self.dynamicCall("CommConnect()")
        self._login_loop = QEventLoop()
        self._login_loop.exec_()

    def _on_event_connect(self, err_code):
        self.log_system.info(f"[LOGIN] err_code={err_code}")
        self._login_loop.exit()

    def load_conditions(self):
        # 조건검색식 사용하려면 필수
        self.dynamicCall("GetConditionLoad()")
        self._cond_loop = QEventLoop()
        self._cond_loop.exec_()

    def _on_receive_condition_ver(self, ret, msg):
        self.log_system.info(f"[CONDITION_LOAD] ret={ret} msg={msg}")
        self._cond_loop.exit()

    # -------------------------------------------------
    # 🔥 실시간 조건검색 시작 (딱 1회)
    # -------------------------------------------------
    def start_realtime_condition(self):
        if self.realtime_condition_started:
            return

        self.log_system.info("[CONDITION] start realtime condition")
        self.dynamicCall(
            "SendCondition(QString, QString, int, int)",
            "9000", CONDITION_NAME, CONDITION_INDEX, 1
        )
        self.realtime_condition_started = True

    # -------------------------------------------------
    # 🔥 실시간 조건 편입 / 이탈
    # -------------------------------------------------
    def _on_receive_real_condition(self, code, event_type, cond_name, cond_index):
        if event_type == "I":
            self.log_signal.info(f"[REAL_CONDITION_IN] {code}")

            if (
                not self.auto_trade_enabled or
                self.position is not None or
                self.ordering or
                code in self.traded_today or
                code in self._scan_queue
            ):
                return

            self._scan_queue.append(code)

            if not self._scan_running:
                self._scan_running = True
                QTimer.singleShot(0, self._scan_next)

        elif event_type == "D":
            self.log_signal.info(f"[REAL_CONDITION_OUT] {code}")
            
    # ---------------------------
    # TR: OPT10080 (3분봉) 요청/응답
    # ---------------------------
    def request_3min_candle_blocking(self, code: str):
        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "틱범위", "3")
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")

        self._last_tr_rqname = "RQ_3MIN"
        self._last_tr_trcode = "OPT10080"

        self.dynamicCall("CommRqData(QString, QString, int, QString)",
                         "RQ_3MIN", "OPT10080", 0, "9100")

        self._tr_loop = QEventLoop()
        self._tr_loop.exec_()
        self._tr_loop = None

    def _on_receive_tr_data(self, screen_no, rqname, trcode, record_name, prev_next,
                            data_len, err_code, msg1, msg2):
        # 우리가 기다리던 TR이면 loop 종료
        if self._tr_loop is not None and rqname == self._last_tr_rqname and trcode == self._last_tr_trcode:
            if self._tr_loop:
                self._tr_loop.exit()

    def parse_3min(self) -> list[dict]:
        # 최신봉이 index 0이 되도록 3개 추출
        rows = self.dynamicCall("GetRepeatCnt(QString, QString)", "OPT10080", "RQ_3MIN")
        candles = []
        for i in range(min(int(rows), 3)):
            close = self._to_int_abs(self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                "OPT10080", "RQ_3MIN", i, "현재가"
            ))
            volume = self._to_int(self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                "OPT10080", "RQ_3MIN", i, "거래량"
            ))
            candles.append({"close": close, "volume": volume})
        return candles

    @staticmethod
    def _to_int(x) -> int:
        try:
            return int(str(x).strip() or "0")
        except Exception:
            return 0

    @staticmethod
    def _to_int_abs(x) -> int:
        try:
            return abs(int(str(x).strip() or "0"))
        except Exception:
            return 0
    # ---------------------------
    # 스캔 루프 (조건검색 결과 종목을 순차 TR로 체크)
    # ---------------------------
    def _scan_next(self):
        try:
            if self.position or self.ordering:
                self._scan_running = False
                return

            if not self._scan_queue:
                self._scan_running = False
                return

            code = self._scan_queue.pop(0)

            self.request_3min_candle_blocking(code)
            candles = self.parse_3min()

            if is_entry_candidate(candles, self.log_signal, code):
                self.log_signal.info(f"[ENTRY_PICK] code={code}")
                self.buy_market(code, self.total_qty)
                return
            else:
                QTimer.singleShot(SCAN_TR_DELAY_MS, self._scan_next)

        except Exception as e:
            self.log_system.error(f"[SCAN_ERR] {e}")
            self._scan_running = False

    # ---------------------------
    # 주문 (모의투자/실계좌 공통: SendOrder 사용)
    # ---------------------------
    def buy_market(self, code: str, qty: int):
        if self.ordering or self.position is not None:
            return
        if qty <= 0:
            return

        self.ordering = True
        self._pending_buy_code = code
        self._pending_buy_qty = qty

        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            "BUY", "9200", self.get_account(),
            1, code, qty, 0, "03", ""
        )
        self.log_trade.info(f"[BUY_ORDER] code={code} qty={qty} ret={ret}")

        if ret != 0:
            self.ordering = False
            self._pending_buy_code = None
            self._pending_buy_qty = 0
            self.log_system.error(f"[BUY_FAIL] ret={ret}")

    def sell_market(self, code: str, qty: int, reason: str):
        if qty <= 0:
            return
        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            "SELL", "9201", self.get_account(),
            2, code, qty, 0, "03", ""
        )
        self.log_trade.info(f"[SELL_ORDER] code={code} qty={qty} reason={reason} ret={ret}")
        if ret != 0:
            self.log_system.error(f"[SELL_FAIL] ret={ret} reason={reason}")

    # ---------------------------
    # 체결(chejan) 이벤트: 상태 업데이트
    # ---------------------------
    def _on_receive_chejan_data(self, gubun, item_cnt, fid_list):
        # gubun: 0(주문/체결), 1(잔고), 4(파생잔고) - 보통 0만으로도 충분
        if str(gubun) != "0":
            return

        raw_code = str(self.dynamicCall("GetChejanData(int)", 9001)).strip()
        code = raw_code.replace("A", "").strip()
        filled_price = self._to_int_abs(self.dynamicCall("GetChejanData(int)", 910))  # 체결가
        filled_qty = self._to_int(self.dynamicCall("GetChejanData(int)", 911))       # 체결량

        # 체결가/체결량이 0이면 이벤트만 온 경우가 많아 무시
        if filled_price <= 0 or filled_qty <= 0 or not code:
            return

        # 매수 체결 처리
        if self._pending_buy_code == code and self._pending_buy_qty > 0:
            # 포지션 확정
            if self.position is None:
                self.position = code
                self.entry_price = filled_price
                self.highest_price = filled_price
                self.remain_qty = 0
                self.tp1_done = False
                self.tp2_done = False
                self.trailing_active = False

                # 실시간 등록 (체결 후)
                self.register_real(code)

            self.remain_qty += filled_qty
            self.log_trade.info(f"[BUY_FILLED] code={code} price={filled_price} qty={filled_qty} remain={self.remain_qty}")

            # 전량 체결로 간주되면 ordering 해제
            if self.remain_qty >= self._pending_buy_qty:
                self.ordering = False
                self._pending_buy_code = None
                self._pending_buy_qty = 0
                self.daily_trade_count += 1

            return

        # 매도 체결 처리 (간단 버전: remain_qty 감소)
        if self.position == code:
            self.remain_qty = max(0, self.remain_qty - filled_qty)
            self.log_trade.info(f"[SELL_FILLED] code={code} price={filled_price} qty={filled_qty} remain={self.remain_qty}")

            if self.remain_qty == 0:
                self.traded_today.add(code)
                self._clear_position()

    def _clear_position(self):
        self.log_system.info(f"[POS_CLEAR] code={self.position}")
        self.position = None
        self.entry_price = None
        self.highest_price = 0
        self.remain_qty = 0
        self.tp1_done = False
        self.tp2_done = False
        self.trailing_active = False
        self.ordering = False
        self._pending_buy_code = None
        self._pending_buy_qty = 0

    # ---------------------------
    # 실시간: 손절/분할익절/트레일링
    # ---------------------------
    def register_real(self, code: str):
        # FID 10: 현재가
        self.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            "9300", code, "10", "0"
        )

    def _on_receive_real_data(self, code, real_type, data):
        if real_type != "주식체결":
            return
        if self.position != code or self.entry_price is None:
            return
        if self.remain_qty <= 0:
            return

        current = self._to_int_abs(self.dynamicCall("GetCommRealData(QString, int)", code, 10))
        if current <= 0:
            return

        # 최고가 갱신
        if current > self.highest_price:
            self.highest_price = current

        entry = self.entry_price

        # 1) 손절 최우선
        if current <= entry * (1 - STOP_LOSS_RATE):
            self.log_trade.info(f"[STOP_LOSS] code={code} price={current} entry={entry}")
            self.sell_market(code, self.remain_qty, "STOP")
            return

        # 2) 1차 익절
        if (not self.tp1_done) and current >= entry * (1 + TP1_RATE):
            qty = max(1, int(self.total_qty * TP1_RATIO))
            qty = min(qty, self.remain_qty)
            self.tp1_done = True
            self.log_trade.info(f"[TP1] code={code} price={current} qty={qty}")
            self.sell_market(code, qty, "TP1")
            return

        # 3) 2차 익절 + 트레일링 활성화
        if self.tp1_done and (not self.tp2_done) and current >= entry * (1 + TP2_RATE):
            qty = max(1, int(self.total_qty * TP2_RATIO))
            qty = min(qty, self.remain_qty)
            self.tp2_done = True
            self.trailing_active = True
            self.log_trade.info(f"[TP2] code={code} price={current} qty={qty} trailing_on=1")
            self.sell_market(code, qty, "TP2")
            return

        # 4) 트레일링 (남은 물량)
        if self.trailing_active and current >= entry * (1 + TRAIL_START_RATE):
            stop_price = int(self.highest_price * (1 - TRAIL_GAP))
            if current <= stop_price:
                self.log_trade.info(f"[TRAIL_STOP] code={code} cur={current} high={self.highest_price} stop={stop_price}")
                self.sell_market(code, self.remain_qty, "TRAIL")
                return

    # ---------------------------
    # 메시지(키움 서버 메시지)
    # ---------------------------
    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        self.log_system.info(f"[MSG] [{screen_no}] {msg}")

    # ---------------------------
    # 셀프 체크 (매매 전 상태 점검)
    # ---------------------------
    def self_check(self, phase: str) -> bool:
        try:
            self.log_system.info(f"[SELF_CHECK_START] phase={phase}")

            # 1. 계좌
            acc = self.get_account()
            if not acc:
                raise RuntimeError("계좌 없음")

            # 2. 상태
            if self.position is not None:
                raise RuntimeError("포지션 잔존")

            if self.ordering:
                raise RuntimeError("ordering 상태")

            self.log_system.info(f"[SELF_CHECK_OK] phase={phase}")
            self.self_check_retry_count = 0
            return True

        except Exception as e:
            self.log_system.error(
                f"[SELF_CHECK_FAIL] phase={phase} reason={e}"
            )
            self.auto_trade_enabled = False
            self.self_check_retry_count += 1
            return False    
        
    # 테스트용 더미 조건검색 결과
    def test_realtime_condition_in(self):
        fake_codes = ["005930", "000660", "035420"]
        for code in fake_codes:
            self._on_receive_real_condition(
                code=code,
                event_type="I",
                cond_name="TEST",
                cond_index=0
            )