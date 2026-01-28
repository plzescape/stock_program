from __future__ import annotations

from curses import raw
from datetime import datetime, time
from multiprocessing.util import info
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer

from config import (
    IS_REAL, ACCOUNT_NO, QTY,
    STOP_LOSS_RATE, TP1_RATE, TP1_RATIO, TP2_RATE, TP2_RATIO,
    TRAIL_START_RATE, TRAIL_GAP,
    MAX_TRADES_PER_DAY, CONDITION_INTERVAL_MIN,
    CONDITION_NAME,
    SCAN_MAX_CODES, SCAN_TR_DELAY_MS, MOCK_ACCOUNT_NO
)
from logger_util import setup_logger
from strategy import is_market_time, is_entry_candidate

MAX_RETRY_PER_CODE = 30
RETRY_INTERVAL_MS = 60_000
SCAN_CYCLE_SEC = 60

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
        self.OnReceiveTrCondition.connect(self._on_receive_tr_condition)
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveRealData.connect(self._on_receive_real_data)
        self.OnReceiveChejanData.connect(self._on_receive_chejan_data)
        self.OnReceiveRealCondition.connect(self._on_receive_real_condition)
        self.OnReceiveMsg.connect(self._on_receive_msg)

        # ===== 루프 =====
        self._login_loop: QEventLoop | None = None
        self._cond_loop: QEventLoop | None = None
        self._tr_loop: QEventLoop | None = None

        # ===== TR 상태 =====
        self._last_tr_rqname: str | None = None
        self._last_tr_trcode: str | None = None
        self._last_tr_record: str | None = None

        # ===== 조건검색 맵 =====
        self.condition_map = {}
        self.candidates = {}   # 🔥 상태머신 핵심
        self._scan_running = False
        self._scan_index = 0

        # ===== 조건검색 상태 =====
        self.watchlist: list[str] = []
        self._scan_queue: list[str] = []
        self._scan_running: bool = False
        self._last_condition_run = None  # datetime

        # ===== 주문/포지션 상태 =====
        self.position: str | None = None
        self.entry_price: int | None = None
        self.highest_price: int = 0
        self.total_qty: int = QTY
        self.remain_qty: int = 0

        self.tp1_done: bool = False
        self.tp2_done: bool = False
        self.trailing_active: bool = False

        self.ordering: bool = False
        self._pending_buy_code: str | None = None
        self._pending_buy_qty: int = 0
        self._pending_sell_qty: int = 0

        # ===== 일일 제한 =====
        self._today = datetime.now().date()
        self.daily_trade_count = 0
        self.traded_today: set[str] = set()

        # ===== 자동매매 상태 =====
        self.auto_trade_enabled = False   # 🔥 기본 OFF
        self.self_check_retry_count = 0

        # ===== TR 큐 / 스캔 상태 =====
        self.scan_queue: list[str] = []
        self.tr_inflight: bool = False
        self.current_scan_code: str | None = None   
        self._screen_seq = 0     
        self.TR_INTERVAL_MS = 300       # 실전 안정값
        self.TR_TIMEOUT_MS  = 2000
    # ---------------------------
    # 기본 유틸
    # ---------------------------
    def _reset_daily_if_needed(self):
        if not self.auto_trade_enabled:   # 🔥 추가
            return
        today = datetime.now().date()
        if today != self._today:
            self._today = today
            self.daily_trade_count = 0
            self.traded_today.clear()
            self.log_system.info("[DAILY_RESET] counters cleared")

    def get_account(self) -> str:
        accs = self.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        accounts = [a.strip() for a in accs.split(";") if a.strip()]
    
        if not accounts:
            raise RuntimeError("계좌 없음")
    
        if IS_REAL:
            if not ACCOUNT_NO.strip():
                raise RuntimeError("IS_REAL=True 인데 ACCOUNT_NO 비어 있음")
            return ACCOUNT_NO.strip()
    
        # 🔒 모의투자: 지정한 계좌만 사용
        if MOCK_ACCOUNT_NO not in accounts:
            raise RuntimeError(
                f"🚨 설정된 모의계좌({MOCK_ACCOUNT_NO})가 로그인 계좌 목록에 없음"
            )
    
        return MOCK_ACCOUNT_NO

    def now_can_enter(self) -> bool:
        # 너무 이른/늦은 시간 신규진입 차단 (원하면 조정)
        t = datetime.now().time()
        return time(9, 0) <= t <= time(14, 50)

    # ---------------------------
    # 로그인 / 조건검색 로드
    # ---------------------------
    def login(self):
        self.dynamicCall("CommConnect()")
        self._login_loop = QEventLoop()
        self._login_loop.exec_()

    def _on_event_connect(self, err_code):
        self.log_system.info(f"[LOGIN] err_code={err_code}")
        if self._login_loop:
            self._login_loop.exit()
            self._login_loop = None

    def load_conditions(self):
        # 조건검색식 사용하려면 필수
        self.dynamicCall("GetConditionLoad()")
        self._cond_loop = QEventLoop()
        self._cond_loop.exec_()

    # ---------------------------
    # 조건검색식 로드 완료 이벤트
    # ---------------------------
    def _on_receive_condition_ver(self, ret, msg):
        self.log_system.info(f"[CONDITION_LOAD] ret={ret} msg={msg}")
        list = self.dynamicCall("GetConditionNameList()")
        self.log_system.info(f"[CONDITION_LIST] {list}")
        
        for item in list.split(";"):
            if not item:
                continue
            idx, name = item.split("^")
            self.condition_map[name] = int(idx)

        self.log_system.info(f"[CONDITION_MAP] {self.condition_map}")  
        
        if self._cond_loop:
            self._cond_loop.exit()
            self._cond_loop = None

    # ---------------------------
    # 실시간 조건검색 이벤트
    # ---------------------------
    def _on_receive_real_condition(self, code, event_type, cond_name, cond_index):
        code = code.strip()

        if event_type == "I":   # 조건 진입
            self.log_signal.info(f"[COND_IN] {code}")

            if code not in self.candidates:
               self.candidates: dict[str, dict] = {
                   code: {
                       "state": "NEW",
                       "retry": 0,
                       "last_try": None,
                       "added_at": datetime,
                   }
               }

            # 스캔 중이 아니면 즉시 스캔 재개
            if not self._scan_running:
                self._scan_running = True
                QTimer.singleShot(0, self._scan_next)

        elif event_type == "D": # 조건 이탈
            self.log_signal.info(f"[COND_OUT] {code}")

            if code in self.candidates:
                # 포지션 중이 아니면 제거
                if self.position != code and not self.ordering:
                    self.candidates.pop(code, None)

    # ---------------------------
    # 조건검색 주기 실행
    # ---------------------------
    def run_condition_cycle(self):
        self._reset_daily_if_needed()

        if self.position is not None or self.ordering:
            return
        if not self.now_can_enter():
            return
        if self.daily_trade_count >= MAX_TRADES_PER_DAY:
            return

        now = datetime.now()
        if self._last_condition_run is not None:
            diff_min = (now - self._last_condition_run).total_seconds() / 60.0
            if diff_min < CONDITION_INTERVAL_MIN:
                return

        self._last_condition_run = now
        
        idx = self.condition_map[CONDITION_NAME]
        result = self.dynamicCall(
            "SendCondition(QString, QString, int, int)",
            "9000", CONDITION_NAME, idx, 1
        )
        if result == 1:
            print(f"{CONDITION_NAME} 조건검색 등록 완료")
            self.log_system.info("[CONDITION] SendCondition OK") 
        else :
            self.log_system.error("[CONDITION] SendCondition FAILED")   
            
    def _on_receive_tr_condition(self, screen_no, codes, cond_name, cond_index, next):
        new_codes = [c for c in codes.split(";") if c]

        for code in new_codes:
            if code not in self.candidates:
                self.candidates[code] = {
                    "state": "NEW",
                    "retry": 0,
                    "last_try": None,
                }
                self.scan_queue.append(code)
                self.log_signal.info(f"[COND_IN] {code}")

        # 스캔 시작 트리거
        if not self.tr_inflight:
            QTimer.singleShot(0, self._scan_next)

    # ---------------------------
    # TR: OPT10080 (1분봉) 요청/응답
    # ---------------------------
    def request_1min_candle(self, code: str):
        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "틱범위", "1")
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")

        self._last_tr_rqname = "RQ_1MIN"
        self._last_tr_trcode = "OPT10080"

        screen_no = f"91{self._screen_seq}"
        self._screen_seq += 1

        self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            "RQ_1MIN", "OPT10080", 0, screen_no
        )

    def _on_receive_tr_data(self, screen_no, rqname, trcode, record_name,
                            prev_next, data_len, err_code, msg1, msg2):

        if not self.tr_inflight:
            return
        if rqname != "RQ_1MIN" or trcode != "OPT10080":
            return

        self._tr_timeout_timer.stop()

        code = self.current_scan_code
        candles = self.parse_1min()

        info = self.candidates.get(code)
        if not info:
            self._finish_tr()
            return

        self.log_signal.info(
            f"[1MIN_PARSE] {code} rows={len(candles)}"
        )

        if len(candles) < 3:
            info["retry"] += 1
            info["state"] = "WAIT_DATA"

            # 다음 사이클에 다시 보기
            self.scan_queue.append(code)

            self._finish_tr(delay=True)
            return

        # === 전략 판단 ===
        if is_entry_candidate(candles, self.log_signal, code):
            self.log_signal.info(f"[ENTRY] {code}")
            self.buy_market(code, self.total_qty)
            info["state"] = "DONE"
            self._finish_tr()
            return

        # 조건 불충족 → 다음 봉에서 재검사
        info["state"] = "WAIT_DATA"
        self.scan_queue.append(code)

        self._finish_tr(delay=True)
                

    def parse_1min(self) -> list[dict]:
        # 최신봉이 index 0이 되도록 3개 추출
        # print("1분봉 데이터 파싱")
        rows = self.dynamicCall("GetRepeatCnt(QString, QString)", "OPT10080", "RQ_1MIN")
        
        candles = []
        for i in range(rows):   # ❗ min(rows, 3) 아님
            close = self._to_int_abs(self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                "OPT10080", "RQ_1MIN", i, "현재가"
            ))
            volume = self._to_int(self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                "OPT10080", "RQ_1MIN", i, "거래량"
            ))
            candles.append({"close": close, "volume": volume})
        # print("1분봉 데이터 파싱 완료:", candles)            
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

    def _resume_scan_if_needed(self):
        if self.position or self.ordering:
            return

        if any(info["state"] != "DONE" for info in self.candidates.values()):
            if not self._scan_running:
                self._scan_running = True
                self.log_system.info("[SCAN] resumed")
                QTimer.singleShot(0, self._scan_next)
    # ---------------------------
    # 스캔 루프 (조건검색 결과 종목을 순차 TR로 체크)
    # ---------------------------
    def _scan_next(self):
        # 이미 주문 중이거나 포지션 있으면 중단
        if self.position or self.ordering:
            return

        if self.tr_inflight:
            return

        if not self.scan_queue:
            self.log_signal.info("[SCAN] queue empty, waiting...")
            return

        code = self.scan_queue.pop(0)
        info = self.candidates.get(code)
        if not info:
            QTimer.singleShot(0, self._scan_next)
            return

        self.current_scan_code = code
        self.tr_inflight = True

        self.log_signal.info(
            f"[SCAN] request {code} retry={info['retry']}"
        )

        self.request_1min_candle(code)

        # ⏱ TR 타임아웃
        self._tr_timeout_timer = QTimer()
        self._tr_timeout_timer.setSingleShot(True)
        self._tr_timeout_timer.timeout.connect(self._on_tr_timeout)
        self._tr_timeout_timer.start(self.TR_TIMEOUT_MS)
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
        self._pending_buy_qty = int(qty)
   
        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [
            "BUY",
            "9200",
            self.get_account(),
            1,          # 매수
            code,
            qty,
            0,          # 시장가 → 반드시 0
            "03",       # 시장가
            ""
            ]
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
            [
            "SELL",
            "9100",
            self.get_account(),
            2,          # 매도
            code,
            qty,
            0,          # 시장가 → 반드시 0
            "03",       # 시장가
            ""
            ]
        )


        self.log_trade.info(f"[SELL_ORDER] code={code} qty={qty} reason={reason} ret={ret}")

        if ret != 0:
            self.log_system.error(f"[SELL_FAIL] ret={ret} reason={reason}")

    # ---------------------------
    # 체결(chejan) 이벤트: 상태 업데이트
    # ---------------------------
    def _on_receive_chejan_data(self, gubun, item_cnt, fid_list):
        print(f"체결 이벤트 수신: gubun={gubun} item_cnt={item_cnt} fid_list={fid_list}")
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

        current = self._to_int_abs(
            self.dynamicCall("GetCommRealData(QString, int)", code, 10)
        )
        if current <= 0:
            return

        # 최고가 갱신
        if current > self.highest_price:
            self.highest_price = current

        entry = self.entry_price

        # 1️⃣ 손절 (최우선)
        if current <= entry * (1 - STOP_LOSS_RATE):
            self.log_trade.info(
                f"[STOP] {code} cur={current} entry={entry}"
            )
            self.sell_market(code, self.remain_qty, "STOP")
            return

        # 2️⃣ 1차 익절
        if not self.tp1_done and current >= entry * (1 + TP1_RATE):
            qty = max(1, int(self.total_qty * TP1_RATIO))
            qty = min(qty, self.remain_qty)
            self.tp1_done = True
            self.log_trade.info(
                f"[TP1] {code} cur={current} qty={qty}"
            )
            self.sell_market(code, qty, "TP1")
            return

        # 3️⃣ 2차 익절
        if self.tp1_done and not self.tp2_done and current >= entry * (1 + TP2_RATE):
            qty = max(1, int(self.total_qty * TP2_RATIO))
            qty = min(qty, self.remain_qty)
            self.tp2_done = True
            self.trailing_active = True
            self.log_trade.info(
                f"[TP2] {code} cur={current} qty={qty}"
            )
            self.sell_market(code, qty, "TP2")
            return

        # 4️⃣ 트레일링 스탑 (남은 물량)
        if self.trailing_active:
            stop_price = int(self.highest_price * (1 - TRAIL_GAP))
            if current <= stop_price:
                self.log_trade.info(
                    f"[TRAIL] {code} cur={current} high={self.highest_price}"
                )
                self.sell_market(code, self.remain_qty, "TRAIL")
                return

    def _on_tr_timeout(self):
        code = self.current_scan_code
        self.log_system.error(f"[TR_TIMEOUT] {code}")

        info = self.candidates.get(code)
        if info:
            info["retry"] += 1
            if info["retry"] < 10:
                self.scan_queue.append(code)
            else:
                info["state"] = "DONE"

        self._finish_tr(delay=True)

    def _finish_tr(self, delay=False):
        self.tr_inflight = False
        self.current_scan_code = None

        next_delay = self.TR_INTERVAL_MS if delay else 0
        QTimer.singleShot(next_delay, self._scan_next)

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
        
        