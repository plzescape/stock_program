# Refactored Kiwoom OpenAPI+ engine
# - Multi-position (MAX 5)
# - PositionState based
# - Safe scan resume/stop
# - Per-position STOP / TP / TRAIL

from __future__ import annotations

import code
from dataclasses import dataclass
from datetime import datetime, time
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer
from dataclasses import dataclass, field
from collections import deque
import time as pytime

from config import (
    IS_REAL, ACCOUNT_NO, QTY,
    STOP_LOSS_RATE, TP1_RATE, TP1_RATIO, TP2_RATE, TP2_RATIO,
    TRAIL_GAP,
    MAX_TRADES_PER_DAY, CONDITION_INTERVAL_MIN,
    CONDITION_NAME, MOCK_ACCOUNT_NO, SCAN_TR_DELAY_MS,
    TIME_STOP_SEC, TIME_STOP_MAX_LOSS, SELL_COOLDOWN_SEC, VOL_AVG_MIN, VOL_CHECK_TICKS,
    MAX_POSITIONS
)
from logger_util import setup_logger
from strategy import is_market_time, is_entry_candidate


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
    
    # ⭐ Time Stop용
    entry_ts: float = field(default_factory=lambda: pytime.time())
    time_stop_done: bool = False

    # ⭐ 최근 매도 시도 시간 기록
    last_sell_attempt_ts: float = 0.0

    # ⭐ 추가
    recent_volumes: deque = field(
        default_factory=lambda: deque(maxlen=VOL_CHECK_TICKS)
    )

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
        active_slots = len(self.positions) + self._count_pending_buys()
        
        if active_slots >= MAX_POSITIONS:
            self._scan_running = False
            return
        if self.tr_inflight or not self.scan_queue:
            return

        code = self.scan_queue.pop(0)
        if code in self.positions or code in self.pending_orders
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

    def _on_receive_tr_data(self, screen_no, rq_name, tr_code, record_name, prev_next, data_len, err_code, msg1, msg2):
        if not self.tr_inflight:
            return
        
        if rq_name != "RQ_1MIN" or tr_code != "OPT10080":
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
        
        order_no = str(self.dynamicCall("GetChejanData(int)", 9203)).strip()  # 주문번호
        raw_code = str(self.dynamicCall("GetChejanData(int)", 9001)).strip()
        code = raw_code.replace("A", "").strip()
        qty = int(str(self.dynamicCall("GetChejanData(int)", 911)).strip() or 0)
        price = abs(int(str(self.dynamicCall("GetChejanData(int)", 910)).strip() or 0))
        order_gubun = str(self.dynamicCall("GetChejanData(int)", 905))
        status = str(self.dynamicCall("GetChejanData(int)", 913)).strip()  # 주문상태
        
        pend = self.pending_orders.get(code)
        if pend and order_no and pend.get("order_no") is None:
            pend["order_no"] = order_no
            self.log_trade.info(f"[PENDING_ORDERNO] code={code} order_no={order_no}")        
        
        if not code or qty <= 0:
            return

        self.log_trade.info(f"[CHEJAN] {order_gubun} code={code} price={price} qty={qty}")
        
        # CANCEL_DONE
        if "취소" in order_gubun or "취소" in status:
            self.pending_orders.pop(code, None)
            self.ordering = False
            self.last_order_ts = None
            self.log_trade.info(f"[CANCEL_DONE] code={code} status={status}")
            return

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
                self.log_trade.info(
                    f"[BUY_FILL_NEW] code={code} price={price}"
                )                
                self.positions[code] = pos
                self.register_real(code)
                self.log_trade.info(f"[BUY_FILL_NEW] code={code} entry={price} total_qty={total_qty}")
            pos.remain_qty += qty
            
            if pos.remain_qty >= pos.total_qty:
                pos.ordering = False
                self.ordering = False
                self.last_order_ts = None
                self.daily_trade_count += 1
                self.pending_orders.pop(code, None)
                self.log_trade.info(f"[BUY_DONE] code={code} remain={pos.remain_qty}/{pos.total_qty}")
            return

        # SELL
        if "매도" in order_gubun:
            pos = self.positions.get(code)
            if not pos:
                return
            
            pos.remain_qty = max(0, pos.remain_qty - qty)
            
            if pos.remain_qty > 0:
                # pending이 아직 SELL이면 selling 유지
                pend = getattr(self, "pending_orders", {}).get(code)
                if not pend or pend.get("side") != "SELL":
                    pos.selling = False  # 예외적으로 pending이 없으면 풀어줌
                self.log_trade.info(f"[SELL_PARTIAL] code={code} remain={pos.remain_qty}/{pos.total_qty}")
                return                
            
            # 전량 매도 완료
            pos.selling = False
            self.positions.pop(code, None)
            self.pending_orders.pop(code, None)
            self.traded_today.add(code)
            self.ordering = False
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
        cur = abs(int(self.dynamicCall("GetCommRealData(QString, int)", code, 10) or 0))
        # 거래량 FID = 15
        vol = abs(int(self.dynamicCall("GetCommRealData(QString, int)", code, 15) or 0))
        pos.recent_volumes.append(vol)
        
        if cur <= 0:
            return

        if cur > pos.highest_price:
            pos.highest_price = cur

        entry = pos.entry_price
        now = pytime.time()

        # STOP
        if cur <= entry * (1 - STOP_LOSS_RATE):
            if not self.can_try_sell(pos):
                return
            pos.last_sell_attempt_ts = now
            pos.selling = True
            self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS")
            return

        # TP1
        if not pos.tp1_done and cur >= entry * (1 + TP1_RATE):
            if not self.can_try_sell(pos):
                return  
            
            qty = min(pos.remain_qty, max(1, int(pos.total_qty * TP1_RATIO)))
            pos.tp1_done = True
            pos.last_sell_attempt_ts = now
            pos.selling = True
            self.send_market_order("SELL", code, qty, "TP1")
            return

        # TP2
        if pos.tp1_done and not pos.tp2_done and cur >= entry * (1 + TP2_RATE):
            if not self.can_try_sell(pos):
                return
            
            qty = min(pos.remain_qty, max(1, int(pos.total_qty * TP2_RATIO)))
            pos.tp2_done = True
            pos.trailing_active = True
            pos.last_sell_attempt_ts = now
            pos.selling = True
            self.send_market_order("SELL", code, qty, "TP2")
            return

        # TRAIL
        if pos.trailing_active:
            stop = int(pos.highest_price * (1 - TRAIL_GAP))
            
            if cur <= stop:
                if not self.can_try_sell(pos):
                    return 
                
                pos.trailing_active = False
                pos.selling = True
                self.send_market_order("SELL", code, pos.remain_qty, "TRAIL_STOP")

        # =========================
        # ⚠️ 거래량 급감 즉시 TIME STOP
        # =========================
        if (
            not pos.time_stop_done
            and not pos.selling
            and len(pos.recent_volumes) == VOL_CHECK_TICKS
        ):
            avg_vol = sum(pos.recent_volumes) / VOL_CHECK_TICKS
            pnl_rate = (cur - pos.entry_price) / pos.entry_price
        
            if avg_vol <= VOL_AVG_MIN and pnl_rate >= TIME_STOP_MAX_LOSS:
                if not self.can_try_sell(pos):
                    return
        
                pos.time_stop_done = True
                pos.selling = True
                pos.last_sell_attempt_ts = pytime.time()
        
                self.log_trade.info(
                    f"[VOL_TIME_STOP] code={code} "
                    f"avg_vol={avg_vol:.2f} "
                    f"pnl={pnl_rate:.4f}"
                )
        
                self.send_market_order(
                    side="SELL",
                    code=code,
                    qty=pos.remain_qty,
                    reason="VOL_TIME_STOP"
                )
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
        
                pos.time_stop_done = True
                pos.selling = True
        
                self.log_trade.info(
                    f"[TIME_STOP] code={code} "
                    f"hold={int(hold_sec)}s "
                    f"pnl={pnl_rate:.4f}"
                )
        
                self.send_market_order(
                    side="SELL",
                    code=code,
                    qty=pos.remain_qty,
                    reason="TIME_STOP"
                )
                return

    # ==================================================
    # 주문 취소 함수
    # ==================================================
    def send_cancel_order(self, code: str, org_order_no: str) -> bool:
        """
        원주문번호 기반 BUY 취소
        nOrderType: 3(매수취소), 4(매도취소)
        """
        if not org_order_no:
            self.log_system.error(f"[CANCEL_ABORT] no org_order_no code={code}")
            return False

        pend = self.pending_orders.get(code)

        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [
                "BUY_CANCEL",          # sRQName (아무 문자열 가능)
                "9200",                # sScreenNo
                self.get_account(),    # sAccNo
                3,                     # nOrderType: 매수취소
                code,                  # sCode
                pend["qty"],           # nQty (0으로도 취소 동작하는 경우 많음)
                0,                     # nPrice
                "00",                  # sHogaGb (취소는 보통 "00")
                org_order_no           # sOrgOrderNo
            ]
        )

        self.log_trade.info(f"[CANCEL_SEND] code={code} org={org_order_no} ret={ret}")
        return (ret == 0)

    # ==================================================
    # pending BUY 취소 감시 함수
    # ==================================================
    def check_pending_buy_cancel(self):
        from config import BUY_FILL_TIMEOUT_SEC, CANCEL_RETRY_COOLDOWN_SEC, MAX_CANCEL_RETRIES
    
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
            
            # 주문번호 아직 없으면 취소 불가 → 로그만
            org = pend.get("order_no")
            if not org:
                self.log_system.warning(
                    f"[CANCEL_WAIT] code={code} no order_no yet age={age:.1f}s"
                )
                continue
            
            # 취소 재시도 쿨다운/횟수 제한
            if pend.get("cancel_retries", 0) >= MAX_CANCEL_RETRIES:
                self.log_system.error(f"[CANCEL_GIVEUP] code={code} org={org}")
                # 여기서 pending을 지울지 말지는 선택:
                # - 지우면 더 이상 관리 안 함
                # - 유지하면 계속 경고 남음
                self.pending_orders.pop(code, None)
                continue
            
            if now - float(pend.get("last_cancel_ts", 0.0)) < CANCEL_RETRY_COOLDOWN_SEC:
                continue
            
            # 취소 시도
            ok = self.send_cancel_order(code, org)
            pend["last_cancel_ts"] = now
            pend["cancel_retries"] = int(pend.get("cancel_retries", 0)) + 1
    
            if ok:
                self.log_trade.info(f"[CANCEL_OK] code={code} org={org}")
                # 취소 접수됐으니 ordering/pending 정리(체결 이벤트로도 정리 가능)
                self.ordering = False
                # pending은 Chejan에서 "취소완료" 이벤트를 받으면 지우는게 베스트지만
                # 간단하게는 여기서 제거해도 됨:
                self.pending_orders.pop(code, None)

    # ==================================================
    # 매도 / 매수 주문 함수
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
            # 포지션 슬롯 체크
            current_slots = len(self.positions) + sum(
                1 for p in self.pending_orders.values() if p.get("side") == "BUY"
            )
            if current_slots >= MAX_POSITIONS:
                self.log_trade.info(
                    f"[BUY_BLOCK] max positions reached ({current_slots}/{MAX_POSITIONS})"
                )
                return False            
            
            self._pending_buy_code = code
            self._pending_buy_qty = qty
        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [side, screen, self.get_account(), order_type, code, qty, 0, "03", ""]
        )
        if ret != 0:
            self.ordering = False
            pos = self.positions.get(code)
            if not pos:
                # ⭐ 디버그 로그 (나중에 지워도 됨)
                self.log_signal.debug(
                    f"[REAL_SKIP] code={code} not in positions"
                )                
            return False
        else:
            self.pending_orders[code] = {
                "side": side,
                "qty": qty,
                "ts": pytime.time(),    # 주문 시각
                "reason": reason,
                "order_no": None,       # 주문번호 (체결시 채워짐)
                "cancel_retries": 0,
                "last_cancel_ts": 0.0
            }
        return True

    # ==================================================
    # 기본 유틸리티 함수 모음
    # ==================================================
    # 1. 실시간 등록
    def register_real(self, code):
        self.dynamicCall("SetRealReg(QString, QString, QString, QString)",
                         "9300", code, "10;15", "0")

    # 2. 1분봉 파싱
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

    # 3. 계좌번호 조회
    def get_account(self):
        accs = self.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        accounts = [a for a in accs.split(";") if a]
        if IS_REAL:
            return ACCOUNT_NO
        return MOCK_ACCOUNT_NO

    # 4. 매도 시도 가능 여부
    def can_try_sell(self, pos: PositionState) -> bool:
        now = pytime.time()
        if pos.selling:
            return False
        if now - pos.last_sell_attempt_ts < SELL_COOLDOWN_SEC:
            return False
        return True    

    # 5. 대기 중인 매수 주문 수
    def _count_pending_buys(self) -> int:
        return sum(
            1 for o in self.pending_orders.values()
            if o["side"] == "BUY"
        )    

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
            

    # ==================================================
    # 메시지 수신
    # ==================================================
    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        if "주문완료" in msg:
            self.last_order_ts = pytime.time()
