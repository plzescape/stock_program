# Refactored Kiwoom OpenAPI+ engine
# - Multi-position (MAX 3)
# - PositionState based
# - Safe scan resume/stop
# - Per-position STOP / TP / TRAIL

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer
import time as pytime

from config import (
    IS_REAL, ACCOUNT_NO, QTY,
    STOP_LOSS_RATE, TP1_RATE, TP1_RATIO, TP2_RATE, TP2_RATIO,
    TRAIL_GAP,
    MAX_TRADES_PER_DAY, CONDITION_INTERVAL_MIN,
    CONDITION_NAME, MOCK_ACCOUNT_NO, SCAN_TR_DELAY_MS
)
from logger_util import setup_logger
from strategy import is_market_time, is_entry_candidate

MAX_POSITIONS = 3


@dataclass
class PositionState:
    code: str
    entry_price: int
    highest_price: int
    total_qty: int
    remain_qty: int
    tp1_done: bool = False
    tp2_done: bool = False
    trailing_active: bool = False
    ordering: bool = False
    selling: bool = False


class KiwoomAPI(QAxWidget):
    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")

        # ---- logger ----
        self.log_system = setup_logger("system", "system.log")
        self.log_trade = setup_logger("trade", "trade.log")
        self.log_signal = setup_logger("signal", "signal.log")

        # ---- events ----
        self.OnEventConnect.connect(self._on_event_connect)
        self.OnReceiveConditionVer.connect(self._on_receive_condition_ver)
        self.OnReceiveTrCondition.connect(self._on_receive_tr_condition)
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveRealData.connect(self._on_receive_real_data)
        self.OnReceiveChejanData.connect(self._on_receive_chejan_data)
        self.OnReceiveRealCondition.connect(self._on_receive_real_condition)
        self.OnReceiveMsg.connect(self._on_receive_msg)

        # ---- loops ----
        self._login_loop = None
        self._cond_loop = None

        # ---- trading state ----
        self.positions: dict[str, PositionState] = {}
        self.ordering = False
        self._pending_buy_code = None
        self._pending_buy_qty = 0
        self.last_order_ts = None

        # ===== 일일 제한 =====
        self._today = datetime.now().date()
        self.daily_trade_count = 0
        self.traded_today = set()

        # ---- scan ----
        self.condition_map = {}
        self.candidates = {}
        self.scan_queue: list[str] = []
        self._scan_running = False
        self.tr_inflight = False
        self.current_scan_code = None
        self._screen_seq = 0
        self.TR_TIMEOUT_MS = 2000

    # ==================================================
    # Login / condition
    # ==================================================
    def run_condition_cycle(self):
        """
        조건검색 주기 실행
        - MAX_POSITIONS 미만일 때만 실행
        - 일일 거래 횟수 제한
        - 시장 시간 / 진입 시간 방어
        """
        # 일일 리셋
        today = datetime.now().date()
        if today != self._today:
            self._today = today
            self.daily_trade_count = 0
            self.traded_today.clear()

        # 포지션 가득 차면 조건검색 중단
        if len(self.positions) >= MAX_POSITIONS:
            return

        # 일일 거래 제한
        if self.daily_trade_count >= MAX_TRADES_PER_DAY:
            return

        # 시장 시간 방어
        if not is_market_time():
            return

        # 너무 늦은 시간 신규 진입 방지 (14:50 이후 차단)
        t = datetime.now().time()
        if t > time(14, 50):
            return

        # 조건검색 주기 제한
        now = datetime.now()
        if hasattr(self, "_last_condition_run") and self._last_condition_run:
            diff = (now - self._last_condition_run).total_seconds() / 60.0
            if diff < CONDITION_INTERVAL_MIN:
                return
        self._last_condition_run = now

        idx = self.condition_map.get(CONDITION_NAME)
        if idx is None:
            self.log_system.error(f"[CONDITION] not found: {CONDITION_NAME}")
            return

        ret = self.dynamicCall(
            "SendCondition(QString, QString, int, int)",
            "9000", CONDITION_NAME, idx, 1
        )

        if ret == 1:
            self.log_system.info("[CONDITION] SendCondition OK")
        else:
            self.log_system.error("[CONDITION] SendCondition FAILED")


    def self_check(self, phase: str) -> bool:
        """
        매매 전/중 상태 점검
        - 계좌
        - 포지션 상태 일관성
        - 주문 플래그
        """
        try:
            self.log_system.info(f"[SELF_CHECK_START] phase={phase}")

            # 계좌 확인
            acc = self.get_account()
            if not acc:
                raise RuntimeError("계좌 없음")

            # 포지션 수 초과 방지
            if len(self.positions) > MAX_POSITIONS:
                raise RuntimeError("포지션 수 초과")

            # 포지션 상태 검증
            for code, pos in self.positions.items():
                if pos.remain_qty <= 0:
                    raise RuntimeError(f"잔여수량 0 code={code}")
                if pos.entry_price <= 0:
                    raise RuntimeError(f"entry_price 오류 code={code}")

            # 주문 중인데 타임스탬프 없는 경우
            if self.ordering and self.last_order_ts is None:
                raise RuntimeError("ordering 상태 불일치")

            self.log_system.info(f"[SELF_CHECK_OK] phase={phase}")
            return True

        except Exception as e:
            self.log_system.error(
                f"[SELF_CHECK_FAIL] phase={phase} reason={e}"
            )
            return False


# ==================================================
# Login / condition
    # ==================================================
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
        self.dynamicCall("GetConditionLoad()")
        self._cond_loop = QEventLoop()
        self._cond_loop.exec_()

    def _on_receive_condition_ver(self, ret, msg):
        conds = self.dynamicCall("GetConditionNameList()")
        for item in conds.split(";"):
            if not item:
                continue
            idx, name = item.split("^")
            self.condition_map[name] = int(idx)
        if self._cond_loop:
            self._cond_loop.exit()
            self._cond_loop = None

    # ==================================================
    # Scan control
    # ==================================================
    def _scan_next(self):
        if len(self.positions) >= MAX_POSITIONS:
            self._scan_running = False
            return
        if self.tr_inflight:
            return
        if not self.scan_queue:
            self._scan_running = False
            return

        code = self.scan_queue.pop(0)
        if code in self.positions:
            QTimer.singleShot(0, self._scan_next)
            return

        self.current_scan_code = code
        self.tr_inflight = True
        self.request_1min(code)

        self._tr_timeout_timer = QTimer()
        self._tr_timeout_timer.setSingleShot(True)
        self._tr_timeout_timer.timeout.connect(self._on_tr_timeout)
        self._tr_timeout_timer.start(self.TR_TIMEOUT_MS)

    def _resume_scan_if_possible(self):
        if len(self.positions) >= MAX_POSITIONS:
            return
        if not self.scan_queue:
            return
        if not self._scan_running:
            self._scan_running = True
            QTimer.singleShot(0, self._scan_next)

    # ==================================================
    # TR
    # ==================================================
    def request_1min(self, code):
        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "틱범위", "1")
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")
        screen = f"91{self._screen_seq}"
        self._screen_seq += 1
        self.dynamicCall("CommRqData(QString, QString, int, QString)",
                         "RQ_1MIN", "OPT10080", 0, screen)

    def _on_receive_tr_data(self, *args):
        if not self.tr_inflight:
            return
        
        if hasattr(self, "_tr_timeout_timer"):
            self._tr_timeout_timer.stop()
        
        code = self.current_scan_code
        candles = self.parse_1min()
        self.tr_inflight = False
        self.current_scan_code = None

        info = self.candidates.get(code)
        if info is None:
            self._finish_tr(delay=True)
            return

        # === ENTRY 성공 ===
        if len(candles) >= 3 and is_entry_candidate(candles, self.log_signal, code):
            if len(self.positions) < MAX_POSITIONS:
                self.send_market_order("BUY", code, QTY, "ENTRY")
            info["state"] = "DONE"
            self._finish_tr(delay=True)
            return
        
        # === ENTRY 실패 ===
        info["retry"] += 1
        info["last_try"] = datetime.now()
        info["state"] = "WAIT"
        
        # ⭐ 핵심: 다시 큐에 넣기 (지연 없이 바로 다음 스캔)
        self.scan_queue.append(code)    
        
        print("Finishing TR for code:", code)
        self._finish_tr(delay=True)

    def _on_tr_timeout(self):
        self.tr_inflight = False
        self.current_scan_code = None
        QTimer.singleShot(0, self._scan_next)

    def _finish_tr(self, delay=True):
        self.tr_inflight = False
        self.current_scan_code = None
        QTimer.singleShot(
            SCAN_TR_DELAY_MS if delay else 0,
            self._scan_next
        )

    # ---------------------------
    # 체결(chejan) 이벤트: 상태 업데이트
    # ---------------------------
    def _on_receive_chejan_data(self, gubun, item_cnt, fid_list):
        if str(gubun) != "0":
            return
        code = str(self.dynamicCall("GetChejanData(int)", 9001)).replace("A", "").strip()
        qty = int(str(self.dynamicCall("GetChejanData(int)", 911)).strip() or 0)
        price = abs(int(str(self.dynamicCall("GetChejanData(int)", 910)).strip() or 0))
        if qty <= 0:
            return
        order_gubun = str(self.dynamicCall("GetChejanData(int)", 905))

        # BUY
        if "매수" in order_gubun:
            pos = self.positions.get(code)
            if pos is None:               
                pos = PositionState(code, price, price, QTY, 0, ordering=True)
                self.log_trade.info(
                    f"[BUY_FILL_NEW] code={code} price={price}"
                )                
                self.positions[code] = pos
                self.register_real(code)
            pos.remain_qty += qty
            if pos.remain_qty >= pos.total_qty:
                pos.ordering = False
                self.ordering = False
                self.last_order_ts = None
                self.daily_trade_count += 1
            return

        # SELL
        if "매도" in order_gubun:
            pos = self.positions.get(code)
            if not pos:
                return
            pos.remain_qty = max(0, pos.remain_qty - qty)
            pos.selling = False
            if pos.remain_qty == 0:
                self.positions.pop(code, None)
                self.traded_today.add(code)
                self._resume_scan_if_possible()

    # ==================================================
    # Real-time (STOP / TP / TRAIL)
    # ==================================================
    def _on_receive_real_data(self, code, real_type, data):
        if real_type != "주식체결":
            return
        pos = self.positions.get(code)
        if not pos or pos.remain_qty <= 0:
            return
        cur = abs(int(self.dynamicCall("GetCommRealData(QString, int)", code, 10) or 0))
        if cur <= 0:
            return

        if cur > pos.highest_price:
            pos.highest_price = cur

        entry = pos.entry_price

        # STOP
        if cur <= entry * (1 - STOP_LOSS_RATE) and not pos.selling:
            pos.selling = True
            self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS")
            return

        # TP1
        if not pos.tp1_done and cur >= entry * (1 + TP1_RATE) and not pos.selling:
            qty = min(pos.remain_qty, max(1, int(pos.total_qty * TP1_RATIO)))
            pos.tp1_done = True
            pos.selling = True
            self.send_market_order("SELL", code, qty, "TP1")
            return

        # TP2
        if pos.tp1_done and not pos.tp2_done and cur >= entry * (1 + TP2_RATE) and not pos.selling:
            qty = min(pos.remain_qty, max(1, int(pos.total_qty * TP2_RATIO)))
            pos.tp2_done = True
            pos.trailing_active = True
            pos.selling = True
            self.send_market_order("SELL", code, qty, "TP2")
            return

        # TRAIL
        if pos.trailing_active and not pos.selling:
            stop = int(pos.highest_price * (1 - TRAIL_GAP))
            if cur <= stop:
                pos.trailing_active = False
                pos.selling = True
                self.send_market_order("SELL", code, pos.remain_qty, "TRAIL_STOP")

    # ==================================================
    # Order wrapper
    # ==================================================
    def send_market_order(self, side, code, qty, reason=""):
        if qty <= 0 or not is_market_time():
            self.log_trade.warning(
                f"[ORDER_ABORT] side={side} code={code} qty={qty} reason={reason}"
            )            
            return False
        
        order_type = 1 if side == "BUY" else 2
        screen = "9200" if side == "BUY" else "9100"
        self.ordering = True
        self.last_order_ts = pytime.time()
        
        self.log_trade.info(
            f"[ORDER_TRY] side={side} code={code} qty={qty} reason={reason}"
        )        
        
        if side == "BUY":
            self._pending_buy_code = code
            self._pending_buy_qty = qty
        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [side, screen, self.get_account(), order_type, code, qty, 0, "03", ""]
        )
        if ret != 0:
            self.ordering = False
            pos = self.positions.get(code)
            if pos:
                pos.selling = False
            return False
        return True

    # ==================================================
    # Utils
    # ==================================================
    def register_real(self, code):
        self.dynamicCall("SetRealReg(QString, QString, QString, QString)",
                         "9300", code, "10", "0")

    def parse_1min(self):
        rows = self.dynamicCall("GetRepeatCnt(QString, QString)", "OPT10080", "RQ_1MIN")
        candles = []
        for i in range(min(rows, 3)):
            close = abs(int(self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                "OPT10080", "RQ_1MIN", i, "현재가"
            ) or 0))
            vol = int(self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                "OPT10080", "RQ_1MIN", i, "거래량"
            ) or 0)
            candles.append({"close": close, "volume": vol})
        return candles

    def get_account(self):
        accs = self.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        accounts = [a for a in accs.split(";") if a]
        if IS_REAL:
            return ACCOUNT_NO
        return MOCK_ACCOUNT_NO

    def _on_receive_tr_condition(
        self,
        screen_no: str,
        codes: str,
        cond_name: str,
        cond_index: int,
        next: int
    ):
        """
        조건검색 결과 수신 (일괄)
        - codes: '005930;000660;...' 형태
        """

        new_codes = [c for c in codes.split(";") if c]

        for code in new_codes:
            # 이미 포지션 있으면 무시
            if code in self.positions:
                continue

            # 이미 후보에 있으면 무시
            if code in self.candidates:
                continue

            # 후보 등록
            self.candidates[code] = {
                "state": "NEW",
                "retry": 0,
                "added_at": datetime.now(),
            }

            self.scan_queue.append(code)
            self.log_signal.info(f"[COND_IN] {code}")

        # 🔥 스캔 트리거 조건
        if (
            len(self.positions) < MAX_POSITIONS
            and not self._scan_running
            and self.scan_queue
        ):
            self._scan_running = True
            self.log_signal.info("[SCAN_TRIGGER] by TR_CONDITION")
            QTimer.singleShot(0, self._scan_next)


    def _on_receive_real_condition(self, code, event_type, cond_name, cond_index):
        code = code.strip()
        # =========================
        # 조건 진입 (I)
        # =========================
        if event_type == "I":  # 조건 진입
            if code in self.positions:
                self.log_trade.info(
                    f"[COND_IN_IGNORE] code={code} reason=already_position"
                )
                return
            if code in self.candidates:
                self.log_trade.info(
                    f"[COND_IN_IGNORE] code={code} reason=already_candidate"
                )                
                return

            self.candidates[code] = {
                "state": "NEW",
                "retry": 0,
                "last_try": None,
                "added_at": datetime.now(),
            }
            
            self.scan_queue.append(code)
            self.log_signal.info(f"[COND_IN] {code}")
            self.log_trade.info(
                f"[CANDIDATE_ADD] code={code} queue_size={len(self.scan_queue)}"
            )

            if len(self.positions) < MAX_POSITIONS and not self._scan_running:
                self._scan_running = True
                self.log_trade.info(
                    f"[SCAN_TRIGGER] reason=REAL_CONDITION positions={len(self.positions)}"
                )                
                QTimer.singleShot(0, self._scan_next)

        # =========================
        # 조건 이탈 (D)
        # =========================
        elif event_type == "D":
            self.log_signal.info(f"[COND_OUT] {code}")
            # 🔴 이미 포지션 보유 중이면 건드리지 않음
            if code in self.positions:
                return

            # 1️⃣ scan_queue에서 제거
            if code in self.scan_queue:
                self.scan_queue = [c for c in self.scan_queue if c != code]

            # 2️⃣ candidates에서 제거
            if code in self.candidates:
                self.candidates.pop(code, None)

            # 3️⃣ 현재 TR 대상이면 안전 해제
            if self.current_scan_code == code:
                self.current_scan_code = None
                self.tr_inflight = False            
            


    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        if "주문완료" in msg:
            self.last_order_ts = pytime.time()
