# Refactored Kiwoom OpenAPI+ engine
# - Multi-position (MAX 5)
# - PositionState based
# - Safe scan resume/stop
# - Per-position STOP / TP / TRAIL

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer
from collections import deque
import time as pytime

from config import (
    IS_REAL, ACCOUNT_NO, MAX_REENTRY_RETRIES, QTY, MAX_BUY_AMOUNT, BUY_MODE, REENTRY_DELAY_SEC,
    STOP_LOSS_RATE, TP1_RATE, TP1_RATIO, TP2_RATE, TP2_RATIO,
    CANDLE_SL_ENABLED, EMERGENCY_SL_RATE,
    TRAIL_GAP,
    MAX_TRADES_PER_DAY, CONDITION_INTERVAL_MIN,
    CONDITION_NAME, MOCK_ACCOUNT_NO, SCAN_TR_DELAY_MS,
    TIME_STOP_SEC, TIME_STOP_MAX_LOSS, SELL_COOLDOWN_SEC, VOL_AVG_MIN, VOL_CHECK_TICKS,
    MAX_POSITIONS, TOTAL_BUDGET,
    BUY_FILL_TIMEOUT_SEC, CANCEL_RETRY_COOLDOWN_SEC, MAX_CANCEL_RETRIES, FORCE_ABANDON_TIMEOUT, SCAN_CODE_COOLDOWN_SEC
)
from logger_util import setup_logger
from strategy import is_market_time, is_entry_candidate, is_entry_candidate_VER2, get_entry_signal_data, is_pullback_entry, get_pullback_signal_data, is_flag_entry, get_flag_signal_data


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
    last_pnl_log_ts: float = 0.0

    # ⭐ Time Stop용
    entry_ts: float = field(default_factory=lambda: pytime.time())
    time_stop_done: bool = False

    # ⭐ 최근 매도 시도 시간 기록
    last_sell_attempt_ts: float = 0.0

    # ⭐ 거래량 추적
    recent_volumes: deque = field(
        default_factory=lambda: deque(maxlen=VOL_CHECK_TICKS)
    )
    peak_avg_vol: float = 0.0     # 진입~TP1 구간 최고 평균거래량
    tp1_done_ts: float = 0.0      # TP1 체결 시각 (보호시간용)
    tp2_done_ts: float = 0.0      # TP2 체결 시각 (트레일링 구간 보호용)
    entry_type: str = ""          # 진입 전략 타입 (BREAKOUT/PULLBACK/FLAG)
    flag_stop_price: int = 0      # FLAG 전용 손절가 (기준봉 시가, 0이면 미사용)

    # ⭐ 완성봉 기준 손절용 — 틱에서 1분봉을 직접 합산
    sl_candle_minute: int = -1     # 현재 쌓고 있는 분봉의 minute(-1=미초기화)
    sl_candle_open:   int = 0      # 현재 분봉 시가
    sl_candle_high:   int = 0      # 현재 분봉 고가
    sl_candle_low:    int = 0      # 현재 분봉 저가
    sl_candle_last:   int = 0      # 현재 분봉 마지막 체결가(완성 시 종가)

class KiwoomAPI(QAxWidget):
    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")
        # ----- Login state -----
        self.auto_trade_enabled = False

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

        # ===== pending BUY 취소 감시 타이머 =====
        self._pending_cancel_timer = QTimer()
        self._pending_cancel_timer.setInterval(500)  # 0.5초마다 체크
        self._pending_cancel_timer.timeout.connect(self.check_pending_buy_cancel)
        self._pending_cancel_timer.start()

        # ---- loops ----
        self._login_loop = None
        self._cond_loop = None

        # ---- trading state ----
        self.positions: dict[str, PositionState] = {}
        self.ordering = False
        self._pending_buy_code = None
        self._pending_buy_qty = 0
        self.last_order_ts = None

        # ===== 주문 대기(pending) 관리 =====
        # code -> {
        #   side: "BUY"|"SELL",
        #   qty: int,
        #   ts: float,
        #   reason: str,
        #   order_no: str|None,
        #   cancel_retries: int,
        #   last_cancel_ts: float
        #   "reentry_retries": int
        # }
        self.pending_orders: dict[str, dict] = {}
        
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
        self._order_screen_seq = 0   # 주문 전용 화면번호 시퀀스
        self.TR_TIMEOUT_MS = 2000

        # ===== 스캔 타임스탬프 =====
        self.last_scan_times = {}  # { '005930': 1700000.123 } 형태

        # ===== TR 요청 간격 관리 =====        
        self.TR_REQ_INTERVAL = 1
        self._last_tr_time = 0

        # ===== 진입 신호 데이터 (디스코드 알림용) =====
        # code -> { vol_ratio, trend, breakout, candle_strength }
        self._entry_signals: dict[str, dict] = {}

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

        # # 포지션 가득 차면 조건검색 중단
        if len(self.positions) >= MAX_POSITIONS:
            return

        # # 일일 거래 제한
        if self.daily_trade_count >= MAX_TRADES_PER_DAY:
            return

        # 시장 시간 방어
        if not is_market_time():
            self.log_system.info("[CONDITION] market closed")
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

            # 주문 중인데 pending도 없고 타임스탬프도 없는 경우만 불일치
            if self.ordering and self.last_order_ts is None and not self.pending_orders:
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
            parts = item.split("^")
            if len(parts) != 2:
                self.log_system.warning(f"[COND_PARSE_SKIP] invalid: {item}")
                continue
            idx, name = parts
            self.condition_map[name] = int(idx)
        if self._cond_loop:
            self._cond_loop.exit()
            self._cond_loop = None

    # ==================================================
    # Scan control
    # ==================================================
    def _scan_next(self):
        self.purge_candidates()
        
        active_slots = len(self.positions) + self._count_pending_buys()
        # print("Active slots:", active_slots, "Positions:", len(self.positions), "Pending buys:", self._count_pending_buys())
        if active_slots >= MAX_POSITIONS:
            self._scan_running = False
            return
        if self.tr_inflight:
            return
        if not self.scan_queue:
            self._scan_running = False  # ⭐ 큐 비면 확실히 False
            self.log_signal.info(
                f"[SCAN_IDLE] queue empty, _scan_running=False "
                f"positions={len(self.positions)} candidates={len(self.candidates)}"
            )
            return

        # 🔴 TR 간격 제한
        now = pytime.time()
        if now - self._last_tr_time < self.TR_REQ_INTERVAL:
            delay = int((self.TR_REQ_INTERVAL - (now - self._last_tr_time)) * 1000)
            QTimer.singleShot(delay, self._scan_next)
            return

        code = self.scan_queue.pop(0)
        
    # 🟢 쿨타임 체크 로직 (무한루프 방지 개선)
        last_time = self.last_scan_times.get(code, 0)
        if pytime.time() - last_time < SCAN_CODE_COOLDOWN_SEC:
            self.scan_queue.append(code)   # 다시 큐의 맨 뒤로 보냄
            
            # ⭐ 버그 수정: 큐에 쿨타임 아닌 종목이 있으면 바로 다음 처리
            # 모두 쿨타임이면 무한루프 방지를 위해 1초 대기 후 재시도
            has_ready = any(
                pytime.time() - self.last_scan_times.get(c, 0) >= SCAN_CODE_COOLDOWN_SEC
                for c in self.scan_queue
            )
            QTimer.singleShot(0 if has_ready else 1000, self._scan_next)
            return
        
        if code in self.positions or code in self.pending_orders:
            QTimer.singleShot(0, self._scan_next)
            return

        self.current_scan_code = code
        self.tr_inflight = True
        self.request_1min(code)
        
        # 타임아웃 타이머 관리
        if hasattr(self, "_tr_timeout_timer") and self._tr_timeout_timer.isActive():
            self._tr_timeout_timer.stop()

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
        screen = f"{9000 + (self._screen_seq % 100)}"
        self._screen_seq += 1
         
        self.dynamicCall("CommRqData(QString, QString, int, QString)",
                         "RQ_1MIN", "OPT10080", 0, screen)

    def _on_receive_tr_data(self, screen_no, rq_name, tr_code, record_name, prev_next, data_len, err_code, msg1, msg2):
        if not self.tr_inflight:
            return
        # print(rq_name, tr_code)
        if tr_code != "OPT10080" or rq_name != "RQ_1MIN":
            return
        
        if hasattr(self, "_tr_timeout_timer"):
            self._tr_timeout_timer.stop()
        
        code = self.current_scan_code
        candles = self.parse_1min(code)
        
        self.tr_inflight = False
        self.current_scan_code = None

        info = self.candidates.get(code)
        if info is None:
            self._finish_tr(delay=True)
            return

        # 최신봉 제외한 완성봉들
        completed_candles = candles[1:] if len(candles) > 1 else []

        # === ENTRY 판정: 돌파 / 눌림목 / 깃발 패턴 ===
        entry_type = None
        if len(completed_candles) >= 35:
            if is_entry_candidate_VER2(completed_candles, self.log_signal, code):
                entry_type = "BREAKOUT"
            elif is_pullback_entry(completed_candles, self.log_signal, code):
                entry_type = "PULLBACK"
            elif is_flag_entry(completed_candles, self.log_signal, code):
                entry_type = "FLAG"

        if entry_type and len(self.positions) < MAX_POSITIONS:
                # ── 매수수량 계산: BUY_MODE에 따라 분기 ──
                cur_price = completed_candles[0]["close"]

                if BUY_MODE == "QTY":
                    # 모드1: 고정 수량
                    buy_qty = QTY

                elif BUY_MODE == "AMOUNT":
                    # 모드2: 금액 기준 (1주 > MAX_BUY_AMOUNT이면 스킵)
                    if cur_price <= 0 or cur_price > MAX_BUY_AMOUNT:
                        self.log_trade.info(
                            f"[ENTRY_SKIP_PRICE] code={code} price={cur_price} "
                            f"exceeds MAX_BUY_AMOUNT={MAX_BUY_AMOUNT}"
                        )
                        info["state"] = "DONE"
                        self._finish_tr(delay=True)
                        return
                    buy_qty = max(1, MAX_BUY_AMOUNT // cur_price)

                else:
                    # 모드3 (BOTH): 금액 + 수량 상한 둘 다 적용
                    if cur_price <= 0 or cur_price > MAX_BUY_AMOUNT:
                        self.log_trade.info(
                            f"[ENTRY_SKIP_PRICE] code={code} price={cur_price} "
                            f"exceeds MAX_BUY_AMOUNT={MAX_BUY_AMOUNT}"
                        )
                        info["state"] = "DONE"
                        self._finish_tr(delay=True)
                        return
                    buy_qty = min(QTY, max(1, MAX_BUY_AMOUNT // cur_price))

                # ── 총 투자한도 체크 ──
                remaining = self.get_remaining_budget()
                est_amount = cur_price * buy_qty
                if est_amount > remaining:
                    buy_qty = remaining // cur_price
                    if buy_qty <= 0:
                        self.log_trade.info(f"[ENTRY_SKIP_BUDGET] code={code} remaining={remaining}")
                        info["state"] = "DONE"
                        self._finish_tr(delay=True)
                        return
                    est_amount = cur_price * buy_qty

                self.log_trade.info(
                    f"[ENTRY_QTY] code={code} price={cur_price} "
                    f"type={entry_type} mode={BUY_MODE} qty={buy_qty} amount={est_amount} "
                    f"budget_remaining={remaining}"
                )

                # 전략 지표 저장 (디스코드 알림용)
                if entry_type == "PULLBACK":
                    sig = get_pullback_signal_data(completed_candles)
                elif entry_type == "FLAG":
                    sig = get_flag_signal_data(completed_candles)
                else:
                    sig = get_entry_signal_data(completed_candles)
                if sig:
                    self._entry_signals[code] = sig
                self.send_market_order("BUY", code, buy_qty, "ENTRY")
                # 예산 추적용
                if code in self.pending_orders:
                    self.pending_orders[code]["est_amount"] = est_amount
                    self.pending_orders[code]["entry_type"] = entry_type
                    # FLAG 전용 손절가: signal_data의 base_stop 값
                    if entry_type == "FLAG" and code in self._entry_signals:
                        self.pending_orders[code]["flag_stop_price"] = \
                            int(self._entry_signals[code].get("base_stop", 0))

                info["state"] = "DONE"
                self._finish_tr(delay=True)
                return
        
        # === ENTRY 실패 ===
        info["retry"] += 1
        info["last_try"] = datetime.now()
        info["state"] = "WAIT"
        
        # ⭐ 핵심: 다시 큐에 넣기 (지연 없이 바로 다음 스캔)
        self.scan_queue.append(code)    # 기존
        
        self.last_scan_times[code] = pytime.time()  # 스캔 타임스탬프 기록
        
        self.log_system.debug(f"[FINISH_TR] code={code}")
        self._finish_tr(delay=True) # 조회 속도 제한

    def _on_tr_timeout(self):
        self.tr_inflight = False
        self._scan_running = False
        self.current_scan_code = None
        QTimer.singleShot(0, self._scan_next)

    def _finish_tr(self, delay=True):
        self.tr_inflight = False
        self.current_scan_code = None
        self._scan_running = bool(self.scan_queue)  # ⭐ 큐 상태 기반으로 확실히 갱신
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
        
        order_no = str(self.dynamicCall("GetChejanData(int)", 9203)).strip()  # 주문번호
        raw_code = str(self.dynamicCall("GetChejanData(int)", 9001)).strip()
        code = raw_code.replace("A", "").strip()
        qty = int(str(self.dynamicCall("GetChejanData(int)", 911)).strip() or 0)
        price = abs(int(str(self.dynamicCall("GetChejanData(int)", 910)).strip() or 0))
        order_gubun = str(self.dynamicCall("GetChejanData(int)", 905))
        status = str(self.dynamicCall("GetChejanData(int)", 913)).strip()  # 주문상태
        
        pend = self.pending_orders.get(code)
        pos = self.positions.get(code)

        if pend and order_no and pend.get("order_no") is None:
            pend["order_no"] = order_no
            self.log_trade.info(f"[PENDING_ORDERNO] code={code} order_no={order_no}")

        if not code:
            return

        # ⭐ CANCEL_DONE 처리는 qty=0이어도 실행해야 함 (취소 chejan은 체결수량=0)
        if "취소" in order_gubun or "취소" in status:
            pend = self.pending_orders.get(code)
            if pend and pend.get("side") == "BUY":
                retries = pend.get("reentry_retries", 0)
                if retries < MAX_REENTRY_RETRIES:
                    # 재매수 시도
                    self.log_trade.info(f"[REENTRY_TRIGGER] {code} retry={retries+1}")
                    QTimer.singleShot(
                        REENTRY_DELAY_SEC * 1000,
                        lambda c=code, q=pend["qty"], r=pend["reason"]: self._reentry_buy(c, q, r, retries+1)
                    )
                    pend["reentry_retries"] = retries + 1
                    self.log_trade.info(f"[CANCEL_RETRY_BUY] code={code} retry={retries + 1}")
            self.pending_orders.pop(code, None)
            # self.ordering = False
            self.last_order_ts = None
            self.ordering = bool(self.pending_orders) # 주문락 해제 여부 재계산
            self.log_trade.info(f"[CANCEL_DONE] code={code} status={status}")
            return

        # ⭐ 취소 이외의 체결은 qty > 0이어야 함
        if qty <= 0:
            return

        self.log_trade.info(f"[CHEJAN] {order_gubun} code={code} price={price} qty={qty}")

        # BUY
        if "매수" in order_gubun:
            # ⭐ pending과 연결 (없으면 무시: 수동주문/기타 체결 반영 방지)
            pend = getattr(self, "pending_orders", {}).get(code)
            if not pend or pend.get("side") != "BUY":
                self.log_trade.info(f"[CHEJAN_SKIP] BUY code={code} reason=no_pending")
                return

            pos = self.positions.get(code)
            if pos is None:
                # total_qty는 "주문 넣었던 qty"로 고정
                total_qty = int(pend.get("qty", QTY))
                pos = PositionState(code, price, price, total_qty, 0, ordering=True)
                self.log_trade.info(f"[BUY_FILL_NEW] code={code} price={price}")
                self.positions[code] = pos
                self.register_real(code)
                self.log_trade.info(f"[BUY_FILL_NEW] code={code} entry={price} total_qty={total_qty}")
                # 진입 전략 타입 기록 (pending_orders에 저장된 값 활용)
                pos.entry_type = pend.get("entry_type", "")
                pos.flag_stop_price = pend.get("flag_stop_price", 0)

            # --------------------------------------------------
            # ✅ 체결수량 처리(중요):
            #  - 키움 Chejan FID 911 값이 "이번 체결수량"이 아니라
            #    "누적 체결수량"으로 들어오는 케이스가 있어 remain_qty가 초과될 수 있음.
            #  - remain_qty가 total_qty를 넘어가면 초과 매도(SELL qty>보유) 시도가 발생해
            #    매도 체결이 전혀 안 오는 현상이 생김.
            #  - 아래 로직은 두 케이스(증분/누적)를 모두 안전하게 처리함.
            # --------------------------------------------------
            if pos.remain_qty + qty <= pos.total_qty:
                # 일반적인 "증분 체결수량" 케이스
                pos.remain_qty += qty
            else:
                # "누적 체결수량" 또는 중복 이벤트 가능성 → 누적로 해석해 보정
                # (현재 remain과 qty 중 큰 값을 누적 체결로 보고 total_qty로 캡)
                pos.remain_qty = min(pos.total_qty, max(pos.remain_qty, qty))

            # 안전장치: 어떤 경우에도 total_qty 초과 금지
            if pos.remain_qty > pos.total_qty:
                pos.remain_qty = pos.total_qty

            if pos.remain_qty >= pos.total_qty:
                pos.ordering = False
                self.ordering = bool(self.pending_orders)  # 주문락 해제 여부 재계산
                self.last_order_ts = None
                self.daily_trade_count += 1
                self.pending_orders.pop(code, None)
                self.log_trade.info(f"[BUY_DONE] code={code} remain={pos.remain_qty}/{pos.total_qty}")
                
            # =========================
            # TP 목표가 설정 (시장가 매도 - 실시간 데이터에서 트리거)
            # =========================
            if pos.remain_qty >= pos.total_qty and not pos.tp1_done:

                entry = pos.entry_price
                tp1_target = self.adjust_tick_size(int(entry * (1 + TP1_RATE)))
                tp2_target = self.adjust_tick_size(int(entry * (1 + TP2_RATE)))

                self.log_trade.info(
                    f"[TP_TARGET_SET] code={code} "
                    f"tp1={tp1_target}(+{TP1_RATE*100:.1f}%) "
                    f"tp2={tp2_target}(+{TP2_RATE*100:.1f}%)"
                )

                # ── 디스코드 매수 체결 알림 ──
                try:
                    from discord_notify import notify_buy_fill
                    notify_buy_fill(
                        code=code,
                        name=self.get_stock_name(code),
                        qty=pos.total_qty,
                        price=pos.entry_price,
                        tp1_price=tp1_target,
                        tp2_price=tp2_target,
                        signal_data=self._entry_signals.pop(code, None)
                    )
                except Exception as e:
                    self.log_system.warning(f"[DISCORD_FAIL] BUY notify: {e}")

            return

        # SELL
        if "매도" in order_gubun:
            pos = self.positions.get(code)
            if not pos:
                return

            # ── 체결수량 안전처리 ──
            # 키움 FID 911은 "해당 주문의 누적 체결수량"으로 올 수 있음.
            # pending_orders의 filled_qty로 주문별 누적을 추적하여 증분 계산.
            pend = self.pending_orders.get(code)
            if pend and pend.get("side") == "SELL":
                prev_filled = pend.get("filled_qty", 0)
                if qty > prev_filled:
                    delta = qty - prev_filled
                    pend["filled_qty"] = qty
                else:
                    delta = qty
                    pend["filled_qty"] = prev_filled + qty
            else:
                delta = qty

            pos.remain_qty = max(0, pos.remain_qty - delta)

            # ── TP 체결 상태 반영 (reason 기준) ──
            if pend:
                reason = pend.get("reason", "")
                if "TP1" in reason and not pos.tp1_done:
                    pos.tp1_done = True
                    pos.tp1_done_ts = pytime.time()
                    self.log_trade.info(f"[TP1_FILLED] code={code} remain={pos.remain_qty}")
                    try:
                        from discord_notify import notify_tp1_fill
                        notify_tp1_fill(code, self.get_stock_name(code), delta, price, pos.entry_price, pos.remain_qty)
                    except Exception as e:
                        self.log_system.warning(f"[DISCORD_FAIL] TP1: {e}")

                elif "TP2" in reason and not pos.tp2_done:
                    pos.tp2_done = True
                    pos.tp2_done_ts = pytime.time()
                    pos.trailing_active = True
                    self.log_trade.info(f"[TP2_FILLED] code={code} trailing_active=ON")
                    try:
                        from discord_notify import notify_tp2_fill
                        notify_tp2_fill(code, self.get_stock_name(code), delta, price, pos.entry_price, pos.remain_qty)
                    except Exception as e:
                        self.log_system.warning(f"[DISCORD_FAIL] TP2: {e}")

            if pos.remain_qty > 0:
                # ⭐ 부분체결: selling 유지 (중복 매도 방지)
                # TP1/TP2 부분체결은 의도된 것이므로 selling 해제
                if pend and ("TP1" in pend.get("reason", "") or "TP2" in pend.get("reason", "")):
                    pos.selling = False  # TP 분할매도 완료 → 다음 단계 진행 허용
                self.log_trade.info(f"[SELL_PARTIAL] code={code} remain={pos.remain_qty}/{pos.total_qty}")
                return

            # 전량 매도 완료
            pos.selling = False
            sell_reason = ""
            sell_pend = self.pending_orders.get(code)
            if sell_pend:
                sell_reason = sell_pend.get("reason", "")

            # ── 디스코드 매도 알림 (reason별 분기) ──
            try:
                stock_name = self.get_stock_name(code)
                entry_p = pos.entry_price
                sold_qty = pos.total_qty

                if "STOP_LOSS" in sell_reason:
                    from discord_notify import notify_stop_loss
                    notify_stop_loss(code, stock_name, sold_qty, price, entry_p)
                elif "PROFIT_SAFE" in sell_reason:
                    from discord_notify import notify_profit_safe
                    notify_profit_safe(code, stock_name, sold_qty, price, entry_p)
                elif "TRAIL" in sell_reason:
                    from discord_notify import notify_trail_stop
                    notify_trail_stop(code, stock_name, sold_qty, price, entry_p)
                elif "TIME_STOP" in sell_reason or "VOL_TIME_STOP" in sell_reason:
                    from discord_notify import notify_time_stop
                    notify_time_stop(code, stock_name, sold_qty, price, entry_p, sell_reason)
                elif "FORCE_LIQUIDATION" in sell_reason:
                    from discord_notify import notify_force_liquidation
                    notify_force_liquidation(code, stock_name, sold_qty, entry_p)
            except Exception as e:
                self.log_system.warning(f"[DISCORD_FAIL] SELL notify: {e}")

            self.positions.pop(code, None)
            self.pending_orders.pop(code, None)
            self.traded_today.add(code)
            self.ordering = bool(self.pending_orders)
            self.last_order_ts = None
            
            # 주문락 해제
            self.log_trade.info(f"[SELL_DONE] code={code} fully sold")
            self._resume_scan_if_possible()

    # ==================================================
    # Real-time (STOP / TP / TRAIL)
    # ==================================================
    def _on_receive_real_data(self, code, real_type, data):
        if real_type != "주식체결":
            return
        pos = self.positions.get(code)
        if not pos or pos.remain_qty <= 0:
            self.log_signal.debug(
                f"[REAL_SKIP] code={code} not in positions"
            )            
            return
        cur = int(self.dynamicCall("GetCommRealData(QString, int)", code, 10) or 0)
        cur = abs(cur)
        # 거래량 FID = 15
        vol = abs(int(self.dynamicCall("GetCommRealData(QString, int)", code, 15) or 0))
        pos.recent_volumes.append(vol)

        # 평균거래량 피크 갱신 (진입~TP1 구간만, TP1 후 왜곡 방지)
        if not pos.tp1_done and len(pos.recent_volumes) == VOL_CHECK_TICKS:
            cur_avg = sum(pos.recent_volumes) / VOL_CHECK_TICKS
            if cur_avg > pos.peak_avg_vol:
                pos.peak_avg_vol = cur_avg
        
        if cur <= 0:
            return

        if cur > pos.highest_price:
            pos.highest_price = cur

        entry = pos.entry_price
        now = pytime.time()
        pnl_rate = (cur - entry) / entry  # 현재 수익률

        if now - pos.last_pnl_log_ts >= 2: # 2초마다 이득률 로그 찍기
            self.log_trade.info(
                f"[PNL_CHECK] code={code} "
                f"entry={pos.entry_price} "
                f"current={cur} "
                f"pnl={pnl_rate:.4f}"
            )
            pos.last_pnl_log_ts = now

        # ==================================================
        # ── 완성봉 기준 손절 (메인) ──────────────────────
        # 틱마다 현재 분봉 OHLC를 직접 합산하고,
        # 분이 바뀌는 순간 직전 완성봉 종가로 손절 판단
        # ==================================================
        from datetime import datetime as _dt
        cur_minute = _dt.now().minute

        if pos.sl_candle_minute == -1:
            # 첫 틱: 분봉 초기화
            pos.sl_candle_minute = cur_minute
            pos.sl_candle_open   = cur
            pos.sl_candle_high   = cur
            pos.sl_candle_low    = cur
            pos.sl_candle_last   = cur

        elif cur_minute != pos.sl_candle_minute:
            # ── 분이 바뀜 → 직전 분봉 완성 ─────────────────
            completed_close = pos.sl_candle_last   # 완성봉 종가
            completed_open  = pos.sl_candle_open
            completed_high  = pos.sl_candle_high
            completed_low   = pos.sl_candle_low

            sl_rate = (completed_close - entry) / entry

            self.log_trade.info(
                f"[CANDLE_CLOSED] {code} "
                f"O:{completed_open} H:{completed_high} "
                f"L:{completed_low} C:{completed_close} "
                f"pnl={sl_rate:.4f}"
            )

            # 완성봉 손절 판단
            if CANDLE_SL_ENABLED and sl_rate <= -STOP_LOSS_RATE:
                if self.can_try_sell(pos):
                    self.log_trade.info(
                        f"[STOP_LOSS_CANDLE] {code} 완성봉 손절 "
                        f"종가:{completed_close} pnl={sl_rate:.4f}"
                    )
                    ok = self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS")
                    if ok:
                        pos.last_sell_attempt_ts = now
                        pos.selling = True
                    else:
                        pos.selling = False
                    # 새 분봉 초기화 후 return
                    pos.sl_candle_minute = cur_minute
                    pos.sl_candle_open   = cur
                    pos.sl_candle_high   = cur
                    pos.sl_candle_low    = cur
                    pos.sl_candle_last   = cur
                    return

            # 새 분봉 시작
            pos.sl_candle_minute = cur_minute
            pos.sl_candle_open   = cur
            pos.sl_candle_high   = cur
            pos.sl_candle_low    = cur
            pos.sl_candle_last   = cur

        else:
            # ── 같은 분 내 틱 — 분봉 갱신 ──────────────────
            if cur > pos.sl_candle_high:
                pos.sl_candle_high = cur
            if cur < pos.sl_candle_low:
                pos.sl_candle_low = cur
            pos.sl_candle_last = cur

        # ==================================================
        # ── 실시간 비상 안전망 (EMERGENCY_SL_RATE) ────────
        # 완성봉과 무관하게 순간 낙폭이 너무 크면 즉시 손절
        # CANDLE_SL_ENABLED=True여도 안전망은 항상 동작
        # EMERGENCY_SL_RATE = 0.0 이면 비활성화
        # ==================================================
        if EMERGENCY_SL_RATE > 0 and pnl_rate <= -EMERGENCY_SL_RATE:
            if not self.can_try_sell(pos):
                return
            self.log_trade.info(
                f"[STOP_LOSS_EMERGENCY] {code} 비상 손절 "
                f"현재가:{cur} pnl={pnl_rate:.4f} "
                f"(안전망 -{EMERGENCY_SL_RATE*100:.1f}%)"
            )
            ok = self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS")
            if ok:
                pos.last_sell_attempt_ts = now
                pos.selling = True
            else:
                pos.selling = False
            return

        # 완성봉 손절 비활성 시 기존 실시간 손절로 폴백
        if not CANDLE_SL_ENABLED and pnl_rate <= -STOP_LOSS_RATE:
            if not self.can_try_sell(pos):
                return
            self.log_trade.info(
                f"[STOP_LOSS] {code} 실시간 손절 "
                f"현재가:{cur} pnl={pnl_rate:.4f}"
            )
            ok = self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS")
            if ok:
                pos.last_sell_attempt_ts = now
                pos.selling = True
            else:
                pos.selling = False
            return

        # =========================
        # TP1 시장가 익절 (가격 도달 시)
        # =========================
        tp1_target = self.adjust_tick_size(int(pos.entry_price * (1 + TP1_RATE)))
        if not pos.tp1_done and cur >= tp1_target:
            if not self.can_try_sell(pos):
                return
            # TP1 수량 계산 (총 수량이 3주 이하면 분할 없이 전량 매도)
            if pos.remain_qty <= 3:
                tp1_qty = pos.remain_qty
            else:
                tp1_qty = max(1, int(pos.total_qty * TP1_RATIO))
                tp1_qty = min(tp1_qty, pos.remain_qty)

            self.log_trade.info(
                f"[TP1_TRIGGER] code={code} cur={cur} target={tp1_target} qty={tp1_qty}"
            )
            ok = self.send_market_order("SELL", code, tp1_qty, "TP1")
            if ok:
                pos.last_sell_attempt_ts = now
                pos.selling = True
            return

        # =========================
        # TP2 시장가 익절 (가격 도달 시)
        # =========================
        tp2_target = self.adjust_tick_size(int(pos.entry_price * (1 + TP2_RATE)))
        if pos.tp1_done and not pos.tp2_done and cur >= tp2_target:
            if not self.can_try_sell(pos):
                return
            # TP2 수량 계산
            if pos.remain_qty <= 2:
                tp2_qty = pos.remain_qty
            else:
                tp2_qty = max(1, int(pos.total_qty * TP2_RATIO))
                tp2_qty = min(tp2_qty, pos.remain_qty)

            self.log_trade.info(
                f"[TP2_TRIGGER] code={code} cur={cur} target={tp2_target} qty={tp2_qty}"
            )
            ok = self.send_market_order("SELL", code, tp2_qty, "TP2")
            if ok:
                pos.last_sell_attempt_ts = now
                pos.selling = True
            return

        # 조건: TP1 달성 후 수익률이 0.5% 이하로 밀리면 본절 탈출
        if pos.tp1_done and pnl_rate <= 0.005:
            if self.can_try_sell(pos):
                self.log_trade.info(f"[PROFIT_SAFEGUARD] {code} 본절 매도 (현재가:{cur})")
                ok = self.send_market_order("SELL", code, pos.remain_qty, "PROFIT_SAFE")
                if ok:
                    pos.last_sell_attempt_ts = now
                    pos.selling = True
                else:
                    pos.selling = False
            return

        # TRAIL
        if pos.trailing_active:
            stop = int(pos.highest_price * (1 - TRAIL_GAP))

            if cur <= stop:
                if not self.can_try_sell(pos):
                    return
                ok = self.send_market_order("SELL", code, pos.remain_qty, "TRAIL_STOP")

                pos.last_sell_attempt_ts = now

                if ok:
                    pos.trailing_active = False
                    pos.selling = True
                else:
                    pos.selling = False

                return

        # =========================
        # ⚠️ 거래량 급감 TIME STOP (TP1 익절 후에만)
        # 구간별 보호:
        #   TP1→TP2 구간: 30초 보호 + 임계값 35%
        #   TP2→트레일링 구간: 30초 보호 + 임계값 25% (신규)
        # =========================
        TP1_PROTECT_SEC = 45   # TP1 후 보호시간 (30→45초: 229000 사례로 TP2 도달 기회 확보)
        TP2_PROTECT_SEC = 30   # TP2 후 보호시간 (신규)

        # TP1→TP2 구간 보호: TP1은 됐지만 TP2는 아직인 경우
        in_tp1_to_tp2_run = (
            pos.tp1_done
            and not pos.tp2_done
            and (now - pos.tp1_done_ts) < TP1_PROTECT_SEC
        )

        # TP2→트레일링 구간 보호: TP2 완료 후 초기 안정화 시간
        in_tp2_trailing_protect = (
            pos.tp2_done
            and pos.tp2_done_ts > 0
            and (now - pos.tp2_done_ts) < TP2_PROTECT_SEC
        )

        # 어느 보호 구간에도 속하지 않을 때만 거래량 급감 체크
        if (
            pos.tp1_done
            and not pos.time_stop_done
            and not pos.selling
            and not in_tp1_to_tp2_run
            and not in_tp2_trailing_protect
            and len(pos.recent_volumes) == VOL_CHECK_TICKS
            and pos.peak_avg_vol > 0
        ):
            avg_vol = sum(pos.recent_volumes) / VOL_CHECK_TICKS
            vol_ratio = avg_vol / pos.peak_avg_vol
            pnl_rate = (cur - pos.entry_price) / pos.entry_price

            # TP2 완료 후 트레일링 구간은 더 엄격한 임계값 (25%)
            # TP1만 된 구간은 기존 임계값 (35%)
            vol_threshold = 0.25 if pos.tp2_done else 0.35

            if vol_ratio <= vol_threshold and pnl_rate >= TIME_STOP_MAX_LOSS:
                if not self.can_try_sell(pos):
                    return

                phase = "TRAILING" if pos.tp2_done else "TP1_WAIT"
                self.log_trade.info(
                    f"[VOL_TIME_STOP] code={code} phase={phase} "
                    f"avg_vol={avg_vol:.2f} peak={pos.peak_avg_vol:.2f} "
                    f"ratio={vol_ratio:.2f} threshold={vol_threshold:.2f} "
                    f"pnl={pnl_rate:.4f} tp2_done={pos.tp2_done}"
                )
                ok = self.send_market_order(
                    side="SELL",
                    code=code,
                    qty=pos.remain_qty,
                    reason="VOL_TIME_STOP"
                )

                pos.last_sell_attempt_ts = now

                if ok:
                    pos.time_stop_done = True
                    pos.selling = True
                else:
                    pos.selling = False

                return

        # =========================
        # ⏱ TIME STOP (-0.3% 이내)
        # =========================
        now = pytime.time()
        hold_sec = now - pos.entry_ts
        
        if (
            not pos.time_stop_done
            and hold_sec >= TIME_STOP_SEC
        ):
            pnl_rate = (cur - pos.entry_price) / pos.entry_price
        
            # 손실이 -0.3% 이내일 때만
            if pnl_rate >= TIME_STOP_MAX_LOSS:
                if not self.can_try_sell(pos):
                    return
        
                self.log_trade.info(
                    f"[TIME_STOP] code={code} "
                    f"hold={int(hold_sec)}s "
                    f"pnl={pnl_rate:.4f}"
                )
                
                ok = self.send_market_order(
                    side="SELL",
                    code=code,
                    qty=pos.remain_qty,
                    reason="TIME_STOP"
                )
        
                pos.last_sell_attempt_ts = now
        
                if ok:
                    pos.time_stop_done = True
                    pos.selling = True
                else:
                    pos.selling = False
        
                return

    # ==================================================
    # 주문 화면번호 생성 (고유값)
    # ==================================================
    def _next_order_screen(self, prefix: str = "92") -> str:
        """
        주문마다 고유 화면번호 반환.
        prefix="92" → "9200"~"9299", prefix="91" → "9100"~"9199"
        같은 화면번호를 재사용하면 이전 주문의 OnReceiveMsg/Chejan이
        유실되거나 충돌할 수 있으므로 순환 사용.
        NOTE: 100건 순환 후 재사용됨. 일반적 사용에서는 문제없음.
        """
        base = int(prefix) * 100
        seq = self._order_screen_seq % 100
        self._order_screen_seq += 1
        return str(base + seq)

    # ==================================================
    # 주문 취소 함수
    # ==================================================
    def send_cancel_order(self, code: str, org_order_no: str, cancel_side: str = "SELL") -> bool:

        if not org_order_no:
            self.log_system.error(f"[CANCEL_ABORT] no org_order_no code={code}")
            return False

        order_type = 3 if cancel_side == "BUY" else 4

        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [
                "CANCEL",
                self._next_order_screen("93"),
                self.get_account(),
                order_type,
                code,
                0,      # 취소는 qty 0 허용
                0,
                "00",
                org_order_no
            ]
        )

        if ret != 0:
            self.log_trade.error(
                f"[CANCEL_FAIL] code={code} org={org_order_no} ret={ret}"
            )
            return False

        self.log_trade.info(
            f"[CANCEL_SEND] code={code} org={org_order_no} side={cancel_side}"
        )

        return True
    
    # ==================================================
    # pending BUY 취소 감시 함수
    # ==================================================
    def check_pending_buy_cancel(self):
    
        now = pytime.time()
    
        # positions 슬롯 계산과는 별개로, "체결 지연 BUY"만 취소
        for code, pend in list(self.pending_orders.items()):
            if pend.get("side") != "BUY":
                continue
            
            # 이미 포지션이 잡혀서 체결 진행중이면(부분체결 포함) 취소 정책 선택
            pos = self.positions.get(code)
            if pos and pos.remain_qty > 0:
                # ✅ 정책 1) 부분체결이면 취소 안 하고 둔다
                # 원하면 "잔량 취소"로 바꿀 수 있음
                continue
            
            # 체결 대기 시간 초과?
            age = now - float(pend.get("ts", now))
            if age < BUY_FILL_TIMEOUT_SEC:
                continue
            
            # 주문번호 아직 없으면 취소 불가
            org = pend.get("order_no")
            if not org:
                # ⭐ 강제 포기: order_no 없이 FORCE_ABANDON_TIMEOUT 초과 시 제거
                if age > FORCE_ABANDON_TIMEOUT:
                    self.log_system.error(
                        f"[FORCE_ABANDON] code={code} no order_no after {age:.1f}s - removing from pending"
                    )
                    self.pending_orders.pop(code, None)
                    self.ordering = False
                    self._pending_buy_code = None
                    self._pending_buy_qty = 0
                    self._resume_scan_if_possible()
                    continue
                # 로그 스팸 방지: 5초마다 한 번만 경고
                last_warn = float(pend.get("_last_warn_ts", 0.0))
                if now - last_warn >= 5.0:
                    self.log_system.warning(
                        f"[CANCEL_WAIT] code={code} no order_no yet age={age:.1f}s (abandon in {FORCE_ABANDON_TIMEOUT - age:.0f}s)"
                    )
                    pend["_last_warn_ts"] = now
                continue
            
            # 취소 재시도 쿨다운/횟수 제한
            if pend.get("cancel_sent"):
                # ⭐ 이미 취소 접수 성공한 주문 → Chejan 응답 대기 중, 재시도 불필요
                # 단, Chejan이 안 올 수 있으므로 타임아웃으로 강제 제거
                cancel_age = now - float(pend.get("last_cancel_ts", now))
                if cancel_age > FORCE_ABANDON_TIMEOUT:
                    self.log_system.error(
                        f"[CANCEL_ZOMBIE] code={code} org={org} "
                        f"cancel_sent but no chejan after {cancel_age:.1f}s - force removing"
                    )
                    self.pending_orders.pop(code, None)
                    self.ordering = bool(self.pending_orders)
                    self._pending_buy_code = None
                    self._pending_buy_qty = 0
                    self._resume_scan_if_possible()
                continue

            if pend.get("cancel_retries", 0) >= MAX_CANCEL_RETRIES:
                self.log_system.error(f"[CANCEL_GIVEUP] code={code} org={org}")
                self.pending_orders.pop(code, None)
                self.ordering = bool(self.pending_orders)
                self._pending_buy_code = None
                self._pending_buy_qty = 0
                self._resume_scan_if_possible()
                continue
            
            if now - float(pend.get("last_cancel_ts", 0.0)) < CANCEL_RETRY_COOLDOWN_SEC:
                continue
            
            # 취소 시도
            ok = self.send_cancel_order(code, org, cancel_side="BUY")
            pend["last_cancel_ts"] = now
            pend["cancel_retries"] = int(pend.get("cancel_retries", 0)) + 1
    
            if ok:
                self.log_trade.info(f"[CANCEL_SENT] code={code} org={org}")
                # ✅ pending은 제거하지 않음 - Chejan "취소완료"에서 재매수 트리거 필요
                pend["cancel_sent"] = True
                self.ordering = False
                self._pending_buy_code = None
                self._pending_buy_qty = 0
                self._resume_scan_if_possible()

        # ── SELL pending 타임아웃 처리 ──
        # SELL 주문이 체결 안 되고 남아있을 수 있으므로 마킹 후 재매도 허용
        SELL_PENDING_TIMEOUT = 30  # 30초
        for code, pend in list(self.pending_orders.items()):
            if pend.get("side") != "SELL":
                continue
            age = now - float(pend.get("ts", now))
            if age > SELL_PENDING_TIMEOUT and not pend.get("stuck"):
                pos = self.positions.get(code)
                # ⭐ 부분체결이 이미 됐으면 거래소에서 나머지도 체결 진행 중
                # → selling 해제하면 중복 매도 위험! 더 기다림
                if pos and pos.remain_qty < pos.total_qty:
                    # 부분체결 진행 중 → 타임아웃을 60초로 연장
                    if age <= SELL_PENDING_TIMEOUT * 2:
                        continue
                    self.log_system.error(
                        f"[SELL_STUCK_PARTIAL] code={code} age={age:.1f}s "
                        f"remain={pos.remain_qty}/{pos.total_qty} - force cleanup"
                    )

                self.log_system.error(
                    f"[SELL_STUCK] code={code} age={age:.1f}s - "
                    f"marking stuck (주문이 살아있을 수 있음)"
                )
                pend["stuck"] = True
                # pending 제거 + selling 해제하여 재시도 허용
                # (중복 매도 방지: send_market_order에서 remain_qty 체크)
                self.pending_orders.pop(code, None)
                if pos:
                    pos.selling = False
                    pos.time_stop_done = False  # stuck 후 재시도 가능하도록 리셋
                self.ordering = bool(self.pending_orders)

    # ==================================================
    # 재매수 함수
    # ==================================================
    def _reentry_buy(self, code, qty, reason, retry_cnt):

        # 이미 포지션 생겼으면 중단
        if code in self.positions:
            return

        # 슬롯 초과 방지
        active_slots = len(self.positions) + self._count_pending_buys()
        if active_slots >= MAX_POSITIONS:
            return

        self.log_trade.info(
            f"[REENTRY_BUY] code={code} retry={retry_cnt}"
        )

        ok = self.send_market_order(
            "BUY",
            code,
            qty,
            f"{reason}_RETRY{retry_cnt}"
        )

        if ok:
            # pending 갱신
            if code in self.pending_orders:
                self.pending_orders[code]["reentry_retries"] = retry_cnt


    # ==================================================
    # 매도 / 매수 주문 함수
    # ==================================================
    def send_market_order(self, side, code, qty, reason=""):
        if qty <= 0 or not is_market_time():
            self.log_trade.warning(
                f"[ORDER_ABORT] side={side} code={code} qty={qty} reason={reason}"
            )            
            return False
        
        now = pytime.time()

        # ⭐ SELL 주문 시 보유수량 체크 (중복 매도 방어)
        if side == "SELL":
            pos = self.positions.get(code)
            if not pos or pos.remain_qty <= 0:
                self.log_trade.warning(f"[ORDER_SKIP_NO_POS] SELL code={code} qty={qty} - no position")
                return False
            if qty > pos.remain_qty:
                self.log_trade.warning(f"[ORDER_QTY_ADJ] SELL code={code} qty={qty}→{pos.remain_qty}")
                qty = pos.remain_qty

        # SELL은 빠른 방어가 중요하므로 스로틀 완화 (0.2초)
        throttle = 0.2 if side == "SELL" else 0.5
        if self.last_order_ts and now - self.last_order_ts < throttle:
            self.log_trade.warning(f"[ORDER_THROTTLE] side={side} code={code} qty={qty}")
            return False        
        
        order_type = 1 if side == "BUY" else 2
        screen = self._next_order_screen("92" if side == "BUY" else "91")
        
        self.log_trade.info(
            f"[ORDER_TRY] side={side} code={code} qty={qty} reason={reason}"
        )        
        
        if side == "BUY":
            # =========================
            # ⭐ 동시 BUY 차단
            # =========================
            if any(o["side"] == "BUY" for o in self.pending_orders.values()):
                self.log_trade.warning(
                    f"[BUY_BLOCK_PENDING] code={code} reason=existing_pending"
                )
                return False 
            
            # 포지션 슬롯 체크
            current_slots = len(self.positions) + sum(
                1 for p in self.pending_orders.values() if p.get("side") == "BUY"
            )
            if current_slots >= MAX_POSITIONS:
                self.log_trade.info(
                    f"[BUY_BLOCK] max positions reached ({current_slots}/{MAX_POSITIONS})"
                )
                self.ordering = False
                self.last_order_ts = None
                self._pending_buy_code = None
                self._pending_buy_qty = 0                
                return False            
            
            self._pending_buy_code = code
            self._pending_buy_qty = qty
            self.ordering = True
            self.last_order_ts = pytime.time()            
            
        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [side, screen, self.get_account(), order_type, code, qty, 0, "03", ""]
        )
        
        # 주문 접수 실패 처리
        if ret != 0:
            # 주문 차단(-308)인 경우 소프트 리셋
            if ret == -308:
                self.log_system.error("[ORDER_BLOCK] -308 detected")
                self.soft_reset("ORDER_BLOCK")
                return False            
            
            self.ordering = False

            # BUY 주문 실패 시 포지션이 없으면 pending도 제거
            self.pending_orders.pop(code, None)
            
            if side == "BUY":
                self._pending_buy_code = None
                self._pending_buy_qty = 0
               
            self.log_trade.error(
                f"[ORDER_FAIL] side={side} code={code} qty={qty} reason={reason} ret={ret}" 
            ) 
                           
            return False
        else:
            # 주문 접수 성공
            self.last_order_ts = now # 주문 성공 시 시점 기록
            self.pending_orders[code] = {
                "side": side,
                "qty": qty,
                "ts": pytime.time(),    # 주문 시각
                "reason": reason,
                "order_no": None,       # 주문번호 (체결시 채워짐)
                "cancel_retries": 0,
                "last_cancel_ts": 0.0,
                "reentry_retries": 0,
                "filled_qty": 0         # SELL 주문별 누적 체결수량 추적
            }
        return True

    # ==================================================
    # 기본 유틸리티 함수 모음
    # ==================================================
    # 1. 실시간 등록
    def register_real(self, code):
        fid_list = "10;15"  # 10=현재가, 15=거래량
        
        # codes = list(self.positions.keys())
        # code_str = ";".join(codes)
        
        self.dynamicCall("SetRealReg(QString, QString, QString, QString)",
                         "9400", code, fid_list, "1") # 1: 추가등록, 0: 해제

        self.log_system.info(
        f"[REAL_REG] total={len(self.positions)} codes={self.positions.keys()}"
        )

    # 2. 1분봉 파싱
    def parse_1min(self, code):
        """
        opt10080 1분봉 데이터 파싱
        - OHLCV 포함
        - 최소 30개 이상 확보 (VER2 전략 대응)
        - 최신봉 → 과거봉 순서 유지
        """

        rqname = "RQ_1MIN"
        trcode = "OPT10080"

        candles = []

        try:
            rows = self.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)

            if rows <= 0:
                # 새벽 시간이나 서버 점검 시에는 rows가 0으로 올 수 있습니다.
                # 현재 시간을 같이 찍어주면 '아, 지금은 장외 시간이라 그렇구나'라고 확신할 수 있습니다.
                now_str = datetime.now().strftime('%H:%M:%S')
                self.log_system.warning(f"[1MIN_PARSE_EMPTY] {code} - Time: {now_str}")
                return []

            # ✔ 최소 30개 확보 (여유 두고 60까지 가져와도 OK)
            fetch_cnt = min(rows, 60)

            # ── 파싱 헬퍼: 루프 밖에 정의 (루프마다 재정의 비효율 제거) ──
            def _safe_int(val):
                """거래량 등 항상 양수인 값용"""
                try:
                    return abs(int(val.strip()))
                except:
                    return 0

            def _safe_int_signed(val):
                """시가/고가/저가/현재가용 - 부호 보존 후 호출측에서 abs() 처리"""
                try:
                    return int(val.strip())
                except:
                    return 0

            for i in range(fetch_cnt):

                # ⚠️ 버그 수정: open/high/low/close 모두 abs() 처리하되,
                # 키움 API 특성상 현재가(close)는 음수로 내려오는 하락봉도
                # open과의 비교(close < open → 음봉)로 판별 가능.
                # 시가/고가/저가/현재가는 abs()로 절댓값을 취해 가격으로 사용.
                open_ = abs(_safe_int_signed(
                    self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode, rqname, i, "시가"
                    )
                ))

                high = abs(_safe_int_signed(
                    self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode, rqname, i, "고가"
                    )
                ))

                low = abs(_safe_int_signed(
                    self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode, rqname, i, "저가"
                    )
                ))

                close_raw = _safe_int_signed(
                    self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode, rqname, i, "현재가"
                    )
                )
                close = abs(close_raw)

                # 거래량은 항상 양수
                volume = _safe_int(
                    self.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode, rqname, i, "거래량"
                    )
                )

                # 시가 또는 종가가 0이면 데이터 오류 → 스킵
                if open_ == 0 or close == 0:
                    continue

                candles.append({
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                })

            # ✔ VER2는 25개 이상 필요
            if len(candles) < 30:
                self.log_system.warning(
                    f"[1MIN_PARSE_SHORT] {code} rows={len(candles)} (<30)"
                )
                return candles

            # self.log_system.info(
            #     f"[1MIN_PARSE] {code} rows={len(candles)} (OHLCV)"
            # )

            return candles

        except Exception as e:
            self.log_system.error(f"[1MIN_PARSE_ERROR] {code} {e}")
            return []

    # 3. 계좌번호 조회
    def get_account(self):
        accs = self.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        accounts = [a for a in accs.split(";") if a]
        if IS_REAL:
            return ACCOUNT_NO
        return MOCK_ACCOUNT_NO

    # 3-1. 종목명 조회
    def get_stock_name(self, code: str) -> str:
        try:
            name = self.dynamicCall(
                "GetMasterCodeName(QString)", code
            ).strip()
            return name if name else code
        except Exception:
            return code

    # 4. 호가단위 보정 (한국 주식시장)
    @staticmethod
    def adjust_tick_size(price: int) -> int:
        """주어진 가격을 올바른 호가단위로 올림 보정 (TP 목표가용)"""
        if price < 2000:     tick = 1
        elif price < 5000:   tick = 5
        elif price < 20000:  tick = 10
        elif price < 50000:  tick = 50
        elif price < 200000: tick = 100
        elif price < 500000: tick = 500
        else:                tick = 1000
        return ((price + tick - 1) // tick) * tick

    # 5. 매도 시도 가능 여부
    def can_try_sell(self, pos: PositionState) -> bool:
        now = pytime.time()
        if pos.selling:
            return False
        if now - pos.last_sell_attempt_ts < SELL_COOLDOWN_SEC:
            return False
        return True

    def get_used_budget(self) -> int:
        """현재 포지션 + 미체결 BUY 주문의 총 사용금액"""
        used = 0
        for pos in self.positions.values():
            used += pos.entry_price * pos.total_qty
        for pend in self.pending_orders.values():
            if pend.get("side") == "BUY":
                used += pend.get("est_amount", 0)
        return used

    def get_remaining_budget(self) -> int:
        """잔여 투자 가능 금액"""
        return max(0, TOTAL_BUDGET - self.get_used_budget())    

    # 5. 대기 중인 매수 주문 수
    def _count_pending_buys(self) -> int:
        return sum(
            1 for o in self.pending_orders.values()
            if o["side"] == "BUY"
        )    

    # 6. pending 주문 복구
    def recover_pending_orders(self):
        """
        pending 주문 복구
        """
        self.log_system.info("[PENDING_RECOVERY_START]")

        for code, pend in list(self.pending_orders.items()):

            # 이미 포지션 잡혔으면 제거
            if code in self.positions:
                self.pending_orders.pop(code, None)
                continue

            age = pytime.time() - pend.get("ts", 0)

            # 너무 오래된 BUY → 취소 시도
            if pend["side"] == "BUY" and age > 30:
                if pend.get("order_no"):
                    self.send_cancel_order(code, pend["order_no"], cancel_side="BUY")    

    # ==================================================
    # 소프트 리셋
    # ==================================================
    def soft_reset(self, reason="UNKNOWN"):
        self.log_system.warning(f"[SOFT_RESET] reason={reason}")

        # 주문 상태
        self.ordering = False
        self.last_order_ts = None
        self._pending_buy_code = None
        self._pending_buy_qty = 0

        # TR 상태
        self.tr_inflight = False
        self.current_scan_code = None

        # pending 복구
        self.recover_pending_orders()

        # 실시간 재등록
        for code in self.positions.keys():
            self.register_real(code)

        # scan 재개
        self._resume_scan_if_possible()


    # ==================================================
    # 조건검색 수신
    # ==================================================
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

            # ⭐ 버그 수정: 중복 추가 방지
            if code not in self.scan_queue:
                self.scan_queue.append(code)
            self.log_signal.info(f"[COND_IN] {code}")

        # 🔥 스캔 트리거 조건
        if (
            len(self.positions) < MAX_POSITIONS
            and not self._scan_running
            and self.scan_queue
        ):
            self._scan_running = True
            self.log_signal.info(
                f"[SCAN_TRIGGER] by TR_CONDITION "
                f"queue={len(self.scan_queue)} positions={len(self.positions)}"
            )
            QTimer.singleShot(0, self._scan_next)
        else:
            self.log_signal.info(
                f"[SCAN_NO_TRIGGER] TR_CONDITION "
                f"queue={len(self.scan_queue)} scanning={self._scan_running} "
                f"positions={len(self.positions)}"
            )

    # ==================================================
    # 실시간 조건검색 수신
    # ==================================================
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
                info = self.candidates[code]

                # =========================
                # ⭐ 재편입 처리
                # =========================
                info["retry"] = 0
                info["state"] = "NEW"
                info["last_try"] = None
                info.pop("cond_out_ts", None)

                # scan_queue 없으면 다시 등록 (중복 추가 방지)
                if code not in self.scan_queue:
                    self.scan_queue.append(code)

                self.log_trade.info(
                    f"[COND_REENTRY_RESET] {code} retry_reset "
                    f"queue={len(self.scan_queue)} scanning={self._scan_running}"
                )
                # ⭐ 재편입도 스캔 트리거 (기존 구멍 수정)
                # → return 전에 트리거 체크
            else:
                # 신규 후보 등록
                self.candidates[code] = {
                    "state": "NEW",
                    "retry": 0,
                    "last_try": None,
                    "added_at": datetime.now(),
                }

                self.scan_queue.append(code)
                self.log_signal.info(f"[COND_IN] {code}")
                self.log_trade.info(
                    f"[CANDIDATE_ADD] code={code} queue_size={len(self.scan_queue)} "
                    f"scanning={self._scan_running}"
                )

            # ⭐ 공통 스캔 트리거 (신규 + 재편입 모두)
            if len(self.positions) < MAX_POSITIONS and not self._scan_running and self.scan_queue:
                self._scan_running = True
                self.log_trade.info(
                    f"[SCAN_TRIGGER] reason=REAL_CONDITION positions={len(self.positions)} "
                    f"queue={len(self.scan_queue)}"
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
                # self.candidates.pop(code, None)
                self.candidates[code]["cond_out_ts"] = pytime.time()
                self.log_signal.info(f"[COND_OUT_MARK] {code}")

            # 3️⃣ 현재 TR 대상이면 안전 해제
            # if self.current_scan_code == code:
            #     self.current_scan_code = None
            #     self.tr_inflight = False            
            
    # ==================================================
    # Candidates 정리 (TTL + Retry)
    # ==================================================
    def purge_candidates(self):
        """
        candidates 메모리 정리
        - COND_OUT 이후 TTL 60초 경과 시 삭제
        - retry 15회 초과 시 삭제
        """

        now = pytime.time()
        TTL = 60          # 초
        MAX_RETRY = 15   # 재시도 한도

        for code, info in list(self.candidates.items()):

            # -------------------------
            # 1️⃣ retry 초과 삭제
            # -------------------------
            if info.get("retry", 0) >= MAX_RETRY:
                self.log_signal.info(
                    f"[CANDIDATE_DROP_RETRY] {code} retry={info['retry']}"
                )

                self.candidates.pop(code, None)
                self.scan_queue = [c for c in self.scan_queue if c != code]

                # current_scan_code가 이 종목이면 표시만 지움
                # (tr_inflight는 _on_receive_tr_data/_on_tr_timeout에서만 해제)
                if self.current_scan_code == code:
                    self.current_scan_code = None

                continue

            # -------------------------
            # 2️⃣ COND_OUT TTL 삭제
            # -------------------------
            cond_out_ts = info.get("cond_out_ts")
            if cond_out_ts and now - cond_out_ts >= TTL:

                self.log_signal.info(
                    f"[CANDIDATE_DROP_TTL] {code} ttl={TTL}s"
                )

                self.candidates.pop(code, None)
                self.scan_queue = [c for c in self.scan_queue if c != code]

                if self.current_scan_code == code:
                    self.current_scan_code = None
    # ==================================================
    # 메시지 수신
    # ==================================================
    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        self.log_system.info(f"[RECV_MSG] screen={screen_no} rq={rqname} tr={trcode} msg={msg}")

        if "주문완료" in msg:
            self.last_order_ts = pytime.time()
            return

        # ── 주문 거부/실패 감지 ──
        # 키움 서버가 주문을 거부하면 Chejan 이벤트는 발생하지 않으므로
        # 여기서 pending_orders를 직접 정리해야 무한 대기를 방지할 수 있다.
        reject_keywords = ["거부", "오류", "실패", "제한", "정지", "불가", "초과", "부족"]
        is_reject = any(kw in msg for kw in reject_keywords)

        if not is_reject:
            return

        self.log_system.error(
            f"[ORDER_REJECT] screen={screen_no} rq={rqname} msg={msg}"
        )

        # rqname이 "BUY" 또는 "SELL"인 경우 → send_market_order에서 발송
        # pending_orders 중 해당 side와 매칭되는 항목 정리
        # (screen_no로는 code를 특정할 수 없으므로, 가장 최근 pending을 대상으로 처리)
        if rqname in ("BUY", "SELL"):
            # side가 일치하는 pending을 찾아 제거
            # order_no 유무 관계없이 매칭 (주문번호 배정 후에도 거부될 수 있음)
            for code, pend in list(self.pending_orders.items()):
                if pend.get("side") == rqname:
                    self.log_system.error(
                        f"[REJECT_CLEANUP] code={code} side={rqname} msg={msg}"
                    )
                    self.pending_orders.pop(code, None)
                    if rqname == "BUY":
                        self.ordering = False
                        self._pending_buy_code = None
                        self._pending_buy_qty = 0
                        self._resume_scan_if_possible()
                    elif rqname == "SELL":
                        pos = self.positions.get(code)
                        if pos:
                            pos.selling = False
                        self.ordering = bool(self.pending_orders)
                    break  # 동시 BUY 차단 로직상 1개만 있을 수 있음

    # ==================================================
    # 14:50 강제 전량 청산
    # ==================================================                    
    def force_liquidation_all(self):
        """
        14:50 강제 전량 청산
        """
        self.log_system.warning("[FORCE_LIQUIDATION_START]")

        for code, pos in list(self.positions.items()):

            if pos.remain_qty <= 0:
                continue

            if pos.selling:
                continue

            self.log_trade.warning(
                f"[FORCE_SELL] code={code} qty={pos.remain_qty}"
            )

            # ── 디스코드 강제청산 알림 ──
            try:
                from discord_notify import notify_force_liquidation
                notify_force_liquidation(
                    code=code,
                    name=self.get_stock_name(code),
                    qty=pos.remain_qty,
                    entry_price=pos.entry_price
                )
            except Exception as e:
                self.log_system.warning(f"[DISCORD_FAIL] FORCE_LIQ: {e}")


            # 시장가 전량 매도
            ok = self.send_market_order(
                "SELL",
                code,
                pos.remain_qty,
                "FORCE_LIQUIDATION"
            )

            if ok:
                pos.selling = True                    
