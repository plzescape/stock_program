# 키움 OpenAPI+ 자동매매 엔진
# - 다중 포지션 (최대 MAX_POSITIONS)
# - PositionState 기반 관리
# - 스캔 안전 재개/중단
# - 포지션별 손절 / 익절 / 트레일링

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer
from collections import deque
import time as pytime

from config import (
    IS_REAL, ACCOUNT_NO, MAX_REENTRY_RETRIES, QTY, MAX_BUY_AMOUNT, BUY_MODE, REENTRY_DELAY_SEC,
    STOP_LOSS_RATE, TP1_RATIO, TP2_RATIO,
    CANDLE_SL_ENABLED, EMERGENCY_SL_RATE,
    ATR_PERIOD, ATR_SL_MULT, ATR_TP_MULT, ATR_SAFE_MULT, ATR_TRAIL_MULT,
    MAX_TRADES_PER_DAY, CONDITION_INTERVAL_MIN,
    CONDITION_NAME, CONDITION_NAMES, MOCK_ACCOUNT_NO, SCAN_TR_DELAY_MS,
    TIME_STOP_SEC, TIME_STOP_MAX_LOSS, SELL_COOLDOWN_SEC, VOL_AVG_MIN, VOL_CHECK_TICKS,
    MAX_POSITIONS, TOTAL_BUDGET,
    BUY_FILL_TIMEOUT_SEC, CANCEL_RETRY_COOLDOWN_SEC, MAX_CANCEL_RETRIES, FORCE_ABANDON_TIMEOUT, SCAN_CODE_COOLDOWN_SEC,
    MINI_TRAIL_TRIGGER, MINI_TRAIL_GAP, MINI_TRAIL_GAP_OPEN
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
    tp1_filled_processed: bool = False  # TP1 CHEJAN 전량체결 후처리 완료 여부 (TP2 목표가 갱신용)
    tp2_filled_processed: bool = False  # TP2 CHEJAN 전량체결 후처리 완료 여부 (디스코드 알림용)
    trailing_active: bool = False

    # ── 미니 트레일링 스탑 (TP1 미달 구간 수익 보호) ──
    mini_trail_active: bool  = False   # 트리거 도달 후 활성화
    mini_trail_peak:   float = 0.0     # 활성화 이후 추적하는 최고 pnl_rate
    ordering: bool = False
    selling: bool = False
    last_pnl_log_ts: float = 0.0

    # ⭐ Time Stop용
    # entry_ts: BUY_DONE(전량체결 완료) 시점에 갱신됨
    # 분할체결 중에는 TIME_STOP 타이머가 시작되지 않도록 BUY_FILL_NEW에서는 갱신 안 함
    entry_ts: float = field(default_factory=lambda: pytime.time())
    buy_done: bool = False          # BUY_DONE(전량체결) 완료 여부
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

    # ⭐ 정확한 손익 계산용 — 분할체결 가중평균
    avg_buy_price:   float = 0.0  # 가중평균 매수가 (분할체결 전체 반영)
    buy_cost_total:  int   = 0    # 총 매수비용 (price×qty 누적합)
    sl_candle_minute: int = -1     # 현재 쌓고 있는 분봉의 minute(-1=미초기화)
    sl_candle_open:   int = 0      # 현재 분봉 시가
    sl_candle_high:   int = 0      # 현재 분봉 고가
    sl_candle_low:    int = 0      # 현재 분봉 저가
    sl_candle_last:   int = 0      # 현재 분봉 마지막 체결가(완성 시 종가)

    # ⭐ ATR 기반 손익 목표
    atr_value:        float = 0.0  # 진입 시 계산한 14분봉 ATR
    atr_sl_price:     int = 0      # ATR 손절가 (매수가 - ATR × 1.5), 완성봉 종가 기준
    atr_tp1_price:    int = 0      # ATR TP1 목표가 (매수가 + ATR × 3)
    atr_tp2_price:    int = 0      # ATR TP2 목표가 (TP1 고점 + ATR × 3, TP1 체결 시 갱신)
    atr_safe_price:   int = 0      # 본절보호선 (매수가 - ATR × 0.5): TP1 후 이하 진입 시 탈출

    # ⭐ [버그1 수정] STOP_LOSS_CANDLE 중복 발동 방지 플래그
    # 문제: SELL_STUCK이 pos.selling=False로 리셋하면 다음 완성봉에서 STOP_LOSS_CANDLE이 재발동
    #       → 1차 SL 주문이 거래소에 살아있는데 2차 주문 → [800033] 에러 반복
    # 수정: STOP_LOSS_CANDLE 발동 시 True 세팅, SELL_DONE에서만 False 초기화
    #       SELL_STUCK의 pos.selling=False 리셋 시에는 이 플래그를 건드리지 않음
    sl_ordered: bool = False       # True = 이미 손절 주문 발송됨 (SELL_DONE 전까지 재발동 차단)

    # ⭐ [버그2 수정] SELL_STUCK_PARTIAL 후 거래소 주문 소실 감지용 타임스탬프
    # 문제: pending 보존 후 거래소 주문이 실제 취소된 경우 CHEJAN이 안 와서
    #       잔여 수량이 표류 (팜스코 35분 표류 케이스)
    # 수정: SELL_STUCK_PARTIAL 발생 시 타임스탬프 기록
    #       → STUCK_PARTIAL_TIMEOUT(300초) 내 체결 없으면 강제 시장가 재주문
    stuck_partial_ts: float = 0.0  # SELL_STUCK_PARTIAL 발생 시각 (0 = 미발생)

    # ⭐ 거래량 급감 청산용 — 분봉 단위 추적
    # 조건: 최근 5분봉 평균 거래량 대비 50% 이하
    #      + 고점 갱신 실패 2회 이상
    #      + 전봉 저가 이탈 발생 시 청산
    vol_candle_volumes: list = field(default_factory=list)  # 완성 분봉별 거래량 (최대 10개)
    vol_candle_highs:   list = field(default_factory=list)  # 완성 분봉별 고가
    vol_candle_lows:    list = field(default_factory=list)  # 완성 분봉별 저가
    vol_peak_high:      int = 0    # TP1 이후 관찰된 최고 고가
    vol_no_new_high_cnt: int = 0   # 고점 갱신 실패 연속 횟수

class KiwoomAPI(QAxWidget):
    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")
        # ----- 로그인 상태 -----
        self.auto_trade_enabled = False

        # ---- 로거 ----
        self.log_system = setup_logger("system", "system.log")
        self.log_trade = setup_logger("trade", "trade.log")
        self.log_signal = setup_logger("signal", "signal.log")

        # ---- 이벤트 연결 ----
        self.OnEventConnect.connect(self._on_event_connect)
        self.OnReceiveConditionVer.connect(self._on_receive_condition_ver)
        self.OnReceiveTrCondition.connect(self._on_receive_tr_condition)
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveRealData.connect(self._on_receive_real_data)
        self.OnReceiveChejanData.connect(self._on_receive_chejan_data)
        self.OnReceiveRealCondition.connect(self._on_receive_real_condition)
        self.OnReceiveMsg.connect(self._on_receive_msg)

        # ===== 미체결 BUY 취소 감시 타이머 =====
        self._pending_cancel_timer = QTimer()
        self._pending_cancel_timer.setInterval(500)  # 0.5초마다 체크
        self._pending_cancel_timer.timeout.connect(self.check_pending_buy_cancel)
        self._pending_cancel_timer.start()

        # ---- 이벤트 루프 ----
        self._login_loop = None
        self._cond_loop = None

        # ---- 매매 상태 ----
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

        # ---- 스캔 ----
        self.condition_map = {}
        self.candidates = {}
        self.scan_queue: list[str] = []
        self._scan_running = False
        self.tr_inflight = False
        self.current_scan_code = None

        # ---- 다중 조건검색식 라운드로빈 ----
        # CONDITION_NAMES 목록을 한 칸씩 순환하며 조건검색 호출
        # 키움 API: 스크린 하나에 조건식 하나만 실시간 등록 가능
        # → 조건식 4개를 스크린 9001~9004에 각각 매핑
        self._cond_rr_idx        = 0      # 다음 호출할 조건식 인덱스
        self._cond_screen_base   = 9001   # 스크린 시작 번호
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
            self.log_system.info("[CONDITION] 장 시간 외")
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

        # ── 다중 조건검색식 라운드로빈 ──────────────────────────────
        # CONDITION_NAMES 목록을 순환하며 매 호출마다 다음 조건식 하나 실행
        # 비어 있으면 CONDITION_NAME(단일) fallback
        names = CONDITION_NAMES if CONDITION_NAMES else [CONDITION_NAME]
        n = len(names)

        # 로드된 조건식 중 다음 순번 선택 (미로드 조건식 건너뜀)
        cond_to_run = None
        for _ in range(n):
            candidate = names[self._cond_rr_idx % n]
            self._cond_rr_idx = (self._cond_rr_idx + 1) % n
            if self.condition_map.get(candidate) is not None:
                cond_to_run = candidate
                break

        if cond_to_run is None:
            self.log_system.error(
                f"[CONDITION] 유효한 조건식 없음 (목록={names}, "
                f"로드된 조건식={list(self.condition_map.keys())})"
            )
            return

        cond_idx  = self.condition_map[cond_to_run]
        # 조건식별 고정 스크린 번호 (9001, 9002, 9003, 9004)
        screen_no = str(self._cond_screen_base + (names.index(cond_to_run) % n))

        ret = self.dynamicCall(
            "SendCondition(QString, QString, int, int)",
            screen_no, cond_to_run, cond_idx, 1
        )

        next_idx = self._cond_rr_idx % n
        if ret == 1:
            self.log_system.info(
                f"[CONDITION] 조건검색 요청 성공: '{cond_to_run}' "
                f"(screen={screen_no}, "
                f"다음순번={names[next_idx] if n>1 else '-'})"
            )
        else:
            self.log_system.error(
                f"[CONDITION] 조건검색 요청 실패: '{cond_to_run}'"
            )


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
                    raise RuntimeError(f"잔여수량 0 {self.cn(code)}")
                if pos.entry_price <= 0:
                    raise RuntimeError(f"entry_price 오류 {self.cn(code)}")

            # 주문 중인데 pending도 없고 타임스탬프도 없는 경우만 불일치
            if self.ordering and self.last_order_ts is None and not self.pending_orders:
                raise RuntimeError("ordering 상태 불일치")

            self.log_system.info(f"[SELF_CHECK_OK] 단계={phase}")
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
        self.log_system.info(f"[LOGIN] 에러코드={err_code}")
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
                self.log_system.warning(f"[COND_PARSE_SKIP] 형식 오류: {item}")
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
        active_slots = len(self.positions) + self._count_pending_buys()
        if active_slots >= MAX_POSITIONS:
            self._scan_running = False
            return
        if self.tr_inflight:
            return
        if not self.scan_queue:
            self._scan_running = False  # ⭐ 큐 비면 확실히 False
            self.log_signal.info(
                f"[SCAN_IDLE] 큐 비어있음, _scan_running=False "
                f"포지션={len(self.positions)} 후보={len(self.candidates)}"
            )
            return

        # 🔴 TR 간격 제한
        now = pytime.time()
        if now - self._last_tr_time < self.TR_REQ_INTERVAL:
            delay = int((self.TR_REQ_INTERVAL - (now - self._last_tr_time)) * 1000)
            QTimer.singleShot(delay, self._scan_next)
            return

        code = self.scan_queue.pop(0)
        # ⭐ FIX: purge를 pop 이후로 이동
        # pop(0)으로 꺼낸 code가 purge로 삭제되는 타이밍 버그 방지
        # pop 이후 purge → 꺼낸 code는 purge 대상에서 이미 제외됨
        self.purge_candidates()
        
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
        # ── OPW00018 잔고조회 분기 (스캔 TR과 무관하게 처리) ──
        if rq_name == "RQ_HOLDINGS" and tr_code == "OPW00018":
            self._on_receive_holdings_tr(rq_name, tr_code)
            return

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

        # === ENTRY 판정: 눌림목 → 깃발 → 돌파 순으로 확인 ===
        # 우선순위 변경 이유:
        #   PULLBACK: 추세 조정 후 재진입 → 승률 높고 슬리피지 적음
        #   FLAG    : 횡보 수렴 후 재돌파 → 패턴 명확, 손절선 뚜렷
        #   BREAKOUT: 신고점 돌파 → 체결 많지만 고점 물림 위험 → 마지막 확인
        entry_type = None
        if len(completed_candles) >= 20:   # 새 BREAKOUT 전략은 20봉만 필요

            # ── BREAKOUT 에너지 소진 체크 ────────────────────────────────
            # 나노팀 케이스: 09:37 첫 급등(COND_IN) → 10:17(40분 후) CONFIRMED
            # → 40분간 COND_IN/OUT 반복 = 급등 에너지 소진 종목 특징
            # → 첫 COND_IN에서 BREAKOUT_FIRST_COND_TIMEOUT 이상 경과 시 BREAKOUT 차단
            # FLAG/PULLBACK은 별도 패턴이므로 제외
            _first_ts = info.get("first_cond_in_ts", 0)
            _elapsed  = pytime.time() - _first_ts if _first_ts else 0
            try:
                from config import BREAKOUT_FIRST_COND_TIMEOUT
                _br_timeout = BREAKOUT_FIRST_COND_TIMEOUT
            except ImportError:
                _br_timeout = 1200  # 기본 20분
            _breakout_stale = (_first_ts > 0 and _elapsed > _br_timeout)
            # ─────────────────────────────────────────────────────────────

            if is_pullback_entry(completed_candles, self.log_signal, code):
                entry_type = "PULLBACK"
            elif is_flag_entry(completed_candles, self.log_signal, code):
                entry_type = "FLAG"
            elif _breakout_stale:
                self.log_signal.info(
                    f"[BREAKOUT_STALE_SKIP] {self.cn(code)} "
                    f"첫COND_IN 경과={_elapsed:.0f}초 > {_br_timeout}초 → BREAKOUT 차단 "
                    f"(에너지 소진 가능성)"
                )
            elif is_entry_candidate_VER2(completed_candles, self.log_signal, code):
                entry_type = "BREAKOUT"

        # ── 장 마감 직전 진입 하드컷 ─────────────────────────────────────
        # 조건 스캔 큐에 남아 있던 종목이 14:50 이후에도 처리되어
        # 15:10~15:18 진입 → 체결 후 57초 만에 손절 → ZOMBIE 루프 → 이월 발생
        # 강제청산(15:20) N분 전부터 모든 전략 신규 진입 차단
        if entry_type:
            try:
                from config import FORCE_LIQUIDATION_HOUR, FORCE_LIQUIDATION_MIN, ENTRY_CUTOFF_MIN_BEFORE
                _cutoff_total = FORCE_LIQUIDATION_HOUR * 60 + FORCE_LIQUIDATION_MIN - ENTRY_CUTOFF_MIN_BEFORE
                _cutoff_t = time(_cutoff_total // 60, _cutoff_total % 60)
            except ImportError:
                _cutoff_t = time(15, 5)
            _now_t = datetime.now().time()
            if _now_t >= _cutoff_t:
                self.log_signal.info(
                    f"[ENTRY_TIME_CUT] {self.cn(code)} {entry_type} 진입 차단 "
                    f"({_now_t.strftime('%H:%M')} ≥ {_cutoff_t.strftime('%H:%M')} 마감전컷)"
                )
                entry_type = None
        # ── 매도 진행 중인 포지션 존재 시 신규 진입 차단 ──────────────────────────
        # 기존 포지션이 selling=True(손절/TP/TIME_STOP 매도 진행 중)인 상태에서
        # 신규 종목 매수 → 동시 2개 이상 매도 발생 → [800033] 경합으로 둘 다 지연
        # (26-06-05: 지엔코 손절 중 인바이오젠 진입 → 800033 연쇄 → 14분 지연+411주 이월)
        if entry_type:
            _selling_codes = [c for c, p in self.positions.items() if p.selling]
            if _selling_codes:
                self.log_signal.info(
                    f"[ENTRY_SELLING_BLOCK] {self.cn(code)} {entry_type} 진입 차단 "
                    f"(매도진행중: {[self.cn(c) for c in _selling_codes]})"
                )
                entry_type = None
        # ─────────────────────────────────────────────────────────────────

        if entry_type and len(self.positions) < MAX_POSITIONS:
                # ── [버그3 수정] ENTRY_CONFIRMED 중복 발동 방지 ──────────────
                # 문제: COND_OUT 후에도 candidates에 남아있다가 다음 스캔에서
                #       같은 종목이 ENTRY_CONFIRMED 2회 발생 → 각각 주문+재주문 → 총 4회 주문
                # 수정: pending_orders에 이미 같은 종목 BUY 주문이 있으면 차단
                if code in self.pending_orders:
                    existing = self.pending_orders[code]
                    if existing.get("side") == "BUY":
                        self.log_signal.info(
                            f"[ENTRY_CONFIRMED_DUP] {self.cn(code)} "
                            f"이미 BUY 주문 진행중 → 중복 진입 차단 "
                            f"(retries={existing.get('reentry_retries',0)})"
                        )
                        info["state"] = "DONE"
                        self._finish_tr(delay=True)
                        return
                # ──────────────────────────────────────────────────────────────

                # ── 진입 확정 로그 ──
                self.log_signal.info(
                    f"[ENTRY_CONFIRMED] {self.cn(code)} 전략={entry_type} "
                    f"조건식='{info.get('cond_name','?')}'"
                )
                # ── 매수수량 계산: BUY_MODE에 따라 분기 ──
                cur_price = completed_candles[0]["close"]

                if BUY_MODE == "QTY":
                    # 모드1: 고정 수량
                    buy_qty = QTY

                elif BUY_MODE == "AMOUNT":
                    # 모드2: 금액 기준 (1주 > MAX_BUY_AMOUNT이면 스킵)
                    if cur_price <= 0 or cur_price > MAX_BUY_AMOUNT:
                        self.log_trade.info(
                            f"[ENTRY_SKIP_PRICE] {self.cn(code)} 현재가={cur_price} "
                            f"최대매수금액({MAX_BUY_AMOUNT}원) 초과"
                        )
                        info["state"] = "DONE"
                        self._finish_tr(delay=True)
                        return
                    buy_qty = max(1, MAX_BUY_AMOUNT // cur_price)

                else:
                    # 모드3 (BOTH): 금액 + 수량 상한 둘 다 적용
                    if cur_price <= 0 or cur_price > MAX_BUY_AMOUNT:
                        self.log_trade.info(
                            f"[ENTRY_SKIP_PRICE] {self.cn(code)} 현재가={cur_price} "
                            f"최대매수금액({MAX_BUY_AMOUNT}원) 초과"
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
                        self.log_trade.info(f"[ENTRY_SKIP_BUDGET] {self.cn(code)} 잔여예산={remaining:,}원 부족")
                        info["state"] = "DONE"
                        self._finish_tr(delay=True)
                        return
                    est_amount = cur_price * buy_qty

                _cname = info.get("cond_name", "?")
                self.log_trade.info(
                    f"[ENTRY_QTY] {self.cn(code)} 현재가={cur_price} "
                    f"전략={entry_type} 조건식='{_cname}' "
                    f"모드={BUY_MODE} 수량={buy_qty}주 금액={est_amount:,}원 "
                    f"잔여예산={remaining:,}원"
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

                # ── ATR 계산 (14분봉 기준) ──
                atr_val = self.calc_atr(completed_candles, ATR_PERIOD)
                self.log_trade.info(
                    f"[ATR_CALC] {self.cn(code)} ATR={atr_val:.1f}원 "
                    f"(14분봉 기준, SL배수={ATR_SL_MULT}, TP배수={ATR_TP_MULT})"
                )

                # ── ATR 최소값 필터 ──
                # ATR이 너무 작으면 TP1 수익이 수수료에 못 미쳐 구조적 손실 발생
                # 디와이덕양(ATR=3.9원, 2,000원 × 2,481주) 케이스 재발 방지
                from config import ATR_MIN_VALUE, ATR_MIN_RATIO
                _atr_ratio = (atr_val / cur_price) if cur_price > 0 else 0
                if atr_val < ATR_MIN_VALUE or _atr_ratio < ATR_MIN_RATIO:
                    self.log_trade.info(
                        f"[ENTRY_SKIP_ATR] {self.cn(code)} "
                        f"ATR={atr_val:.1f}원({_atr_ratio*100:.2f}%) "
                        f"최소기준 미달(절대값>={ATR_MIN_VALUE}원, 비율>={ATR_MIN_RATIO*100:.1f}%) "
                        f"→ 진입 스킵 (수수료 대비 수익성 부족)"
                    )
                    info["state"] = "DONE"
                    self._finish_tr(delay=True)
                    return

                self.send_market_order("BUY", code, buy_qty, "ENTRY")
                # 예산 추적용
                if code in self.pending_orders:
                    self.pending_orders[code]["est_amount"] = est_amount
                    self.pending_orders[code]["entry_type"] = entry_type
                    self.pending_orders[code]["atr_value"]  = atr_val   # ← ATR 저장
                    self.pending_orders[code]["cur_price"]  = cur_price  # ← 슬리피지 제어용
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
        
        self.log_system.debug(f"[FINISH_TR] {self.cn(code)} TR완료")
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
            self.log_trade.info(f"[PENDING_ORDERNO] {self.cn(code)} 주문번호={order_no}")

        if not code:
            return

        # ⭐ CANCEL_DONE 처리는 qty=0이어도 실행해야 함 (취소 chejan은 체결수량=0)
        if "취소" in order_gubun or "취소" in status:
            pend = self.pending_orders.get(code)
            if pend and pend.get("side") == "BUY":
                retries = pend.get("reentry_retries", 0)
                if retries < MAX_REENTRY_RETRIES:
                    # ── [버그1 수정] pop 전에 ATR·기준가를 미리 캡처 ──────────
                    # QTimer.singleShot으로 1초 후 _reentry_buy가 실행될 때
                    # pending_orders[code]가 이미 pop돼 atr_value=0.0 fallback 발생
                    # → pop 전에 값을 로컬 변수로 저장해 lambda에 바인딩
                    _saved_atr   = float(pend.get("atr_value", 0.0))
                    _saved_px    = int(pend.get("cur_price", 0))
                    _saved_qty   = pend["qty"]
                    _saved_rsn   = pend["reason"]
                    self.log_trade.info(
                        f"[REENTRY_TRIGGER] {self.cn(code)} 재시도={retries+1}회차 "
                        f"ATR저장={_saved_atr:.1f} 기준가={_saved_px:,}"
                    )
                    QTimer.singleShot(
                        REENTRY_DELAY_SEC * 1000,
                        lambda c=code, q=_saved_qty, r=_saved_rsn,
                               atr=_saved_atr, ep=_saved_px:
                            self._reentry_buy(c, q, r, retries+1,
                                              atr_value=atr, orig_price=ep)
                    )
                    pend["reentry_retries"] = retries + 1
                    self.log_trade.info(f"[CANCEL_RETRY_BUY] {self.cn(code)} 재시도={retries + 1}회차")
            elif pend and pend.get("side") == "SELL" and pend.get("cancel_for_retry"):
                # ⭐ FIX [800033 버그]: SELL_STUCK → 원주문 취소 완료 → 이제 시장가 재시도
                # 기존: 취소 없이 시장가 추가 발사 → 거래소에 두 주문 공존 → [800033]
                # 수정: 취소 확인(CANCEL_DONE) 후 500ms 뒤 시장가 재시도
                _retry_qty = pend.get("qty", 0)
                _retry_rsn = pend.get("retry_reason", "STOP_LOSS_RETRY")
                self.log_system.warning(
                    f"[SELL_CANCEL_DONE] {self.cn(code)} "
                    f"원주문 취소 확인 → 500ms 후 시장가 재시도 qty={_retry_qty}"
                )
                pos_ref = self.positions.get(code)
                if pos_ref:
                    pos_ref.sl_ordered = False   # 취소 완료 → 재발동 허용
                    pos_ref.selling    = False
                QTimer.singleShot(
                    500,
                    lambda c=code, q=_retry_qty, r=_retry_rsn:
                        self._retry_sell_after_reject(c, q, r)
                )
            self.pending_orders.pop(code, None)
            # self.ordering = False
            self.last_order_ts = None
            self.ordering = bool(self.pending_orders) # 주문락 해제 여부 재계산
            self.log_trade.info(f"[CANCEL_DONE] {self.cn(code)} 상태={status}")
            return

        # ⭐ 취소 이외의 체결은 qty > 0이어야 함
        if qty <= 0:
            return

        self.log_trade.info(f"[CHEJAN] {order_gubun} {self.cn(code)} price={price} qty={qty}")

        # BUY
        if "매수" in order_gubun:
            # ⭐ pending과 연결 (없으면 무시: 수동주문/기타 체결 반영 방지)
            pend = getattr(self, "pending_orders", {}).get(code)
            if not pend or pend.get("side") != "BUY":
                self.log_trade.info(f"[CHEJAN_SKIP] 매수 {self.cn(code)} 사유=미체결주문없음")
                return

            pos = self.positions.get(code)
            if pos is None:
                # total_qty는 "주문 넣었던 qty"로 고정
                total_qty = int(pend.get("qty", QTY))
                pos = PositionState(code, price, price, total_qty, 0, ordering=True)
                self.log_trade.info(f"[BUY_FILL_NEW] {self.cn(code)} price={price}")
                self.positions[code] = pos
                self.register_real(code)
                self.log_trade.info(f"[BUY_FILL_NEW] {self.cn(code)} entry={price} total_qty={total_qty}")
                # 진입 전략 타입 기록 (pending_orders에 저장된 값 활용)
                pos.entry_type = pend.get("entry_type", "")
                pos.flag_stop_price = pend.get("flag_stop_price", 0)

                # ── ATR 기반 손익 목표 설정 ──
                atr = float(pend.get("atr_value", 0.0))
                pos.atr_value = atr
                if atr > 0:
                    # FIX: atr_sl_price는 반드시 체결가(price)보다 낮아야 함
                    # 재진입 케이스나 ATR 이상값으로 역전되면 즉시 손절 발동됨
                    _raw_sl = int(price - atr * ATR_SL_MULT)
                    pos.atr_sl_price   = self.adjust_tick_size(
                        min(max(1, _raw_sl), price - self.min_tick(price))
                    )
                    pos.atr_tp1_price  = self.adjust_tick_size(int(price + atr * ATR_TP_MULT))
                    pos.atr_tp2_price  = pos.atr_tp1_price   # TP1 체결 시 고점 기준으로 갱신됨
                    # BUG-FIX: safe_price는 반드시 매수가(entry_price) 이상이어야 함
                    # 기존: price - ATR×SAFE_MULT → ATR이 크면 매수가 아래로 내려가 손절 유발
                    # 수정: max(entry_price, price - ATR×SAFE_MULT)
                    pos.atr_safe_price = self.adjust_tick_size(
                        max(pos.entry_price, int(price - atr * ATR_SAFE_MULT))
                    )

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
                delta_buy = qty
                pos.remain_qty += qty
            else:
                # "누적 체결수량" 또는 중복 이벤트 가능성 → 누적로 해석해 보정
                # (현재 remain과 qty 중 큰 값을 누적 체결로 보고 total_qty로 캡)
                delta_buy = min(pos.total_qty, max(pos.remain_qty, qty)) - pos.remain_qty
                pos.remain_qty = min(pos.total_qty, max(pos.remain_qty, qty))

            # ⭐ 가중평균 매수가 누적 (분할체결 정확한 손익 계산용)
            if delta_buy > 0:
                pos.buy_cost_total += price * delta_buy
                if pos.remain_qty > 0:
                    pos.avg_buy_price = pos.buy_cost_total / pos.remain_qty

            # 안전장치: 어떤 경우에도 total_qty 초과 금지
            if pos.remain_qty > pos.total_qty:
                pos.remain_qty = pos.total_qty

            if pos.remain_qty >= pos.total_qty:
                pos.ordering = False
                self.ordering = bool(self.pending_orders)  # 주문락 해제 여부 재계산
                self.last_order_ts = None
                self.daily_trade_count += 1
                self.pending_orders.pop(code, None)
                # ⭐ BUG-1 수정: TIME_STOP 타이머를 전량체결 완료 시점으로 갱신
                # BUY_FILL_NEW(첫 부분체결)에서 시작하면 분할체결 대기 시간이 포함되어
                # 실제 보유시간보다 짧게 계산되는 문제 + 분할체결 중 TIME_STOP 오발동 방지
                pos.entry_ts = pytime.time()
                pos.buy_done = True
                self.log_trade.info(f"[BUY_DONE] {self.cn(code)} 잔여={pos.remain_qty}/{pos.total_qty}주 체결완료")
                
            # =========================
            # TP 목표가 설정 (ATR 기반 - 실시간 데이터에서 트리거)
            # =========================
            if pos.remain_qty >= pos.total_qty and not pos.tp1_done:

                entry = pos.entry_price
                # ATR 값이 있으면 ATR 기반, 없으면 fallback 고정값
                if pos.atr_value > 0:
                    tp1_target = pos.atr_tp1_price
                    tp2_target = pos.atr_tp2_price
                    sl_price   = pos.atr_sl_price
                    self.log_trade.info(
                        f"[TP_TARGET_SET] {self.cn(code)} ATR={pos.atr_value:.1f}원 "
                        f"손절={sl_price} TP1={tp1_target} TP2(초기)={tp2_target} "
                        f"본절보호={pos.atr_safe_price}"
                    )
                else:
                    # ATR 미계산 fallback (ATR_PERIOD 캔들 부족 등 예외상황)
                    tp1_target = self.adjust_tick_size(int(entry * 1.02))
                    tp2_target = self.adjust_tick_size(int(entry * 1.03))
                    sl_price   = self.adjust_tick_size(int(entry * 0.99))
                    self.log_trade.warning(
                        f"[TP_TARGET_SET_FALLBACK] {self.cn(code)} ATR=0 → 고정비율 사용 "
                        f"손절={sl_price} TP1={tp1_target} TP2={tp2_target}"
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
                        sl_price=pos.atr_sl_price,
                        atr=pos.atr_value,
                        signal_data=self._entry_signals.pop(code, None)
                    )
                except Exception as e:
                    import traceback
                    self.log_system.error(
                        f"[DISCORD_FAIL] 매수알림: {e}\n{traceback.format_exc()}"
                    )

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
                if "TP1" in reason and not pos.tp1_filled_processed:
                    # ⭐ FIX: TP1 주문 전량 체결 완료 시에만 TP1_FILLED 후처리
                    # tp1_done은 TP1_TRIGGER에서 주문 전송 시 즉시 세팅(중복 발동 방지)
                    # tp1_filled_processed는 CHEJAN 전량 체결 후 세팅(TP2 목표가 갱신용)
                    tp1_order_qty = pend.get("qty", 0)
                    tp1_filled_so_far = pend.get("filled_qty", 0)
                    _tp1_fully_filled = (tp1_order_qty > 0 and tp1_filled_so_far >= tp1_order_qty)
                    if not _tp1_fully_filled:
                        pass  # 아직 부분 체결 중 → TP1_FILLED 후처리 보류
                    else:
                        pos.tp1_filled_processed = True
                        pos.tp1_done_ts = pytime.time()
                        # ── TP2 목표가를 TP1 체결 완료 시점 고점 기준으로 갱신 ──
                        if pos.atr_value > 0:
                            pos.atr_tp2_price = self.adjust_tick_size(
                                int(pos.highest_price + pos.atr_value * ATR_TP_MULT)
                            )
                            self.log_trade.info(
                                f"[TP1_FILLED] {self.cn(code)} 잔여={pos.remain_qty}주 "
                                f"고점={pos.highest_price} TP2갱신={pos.atr_tp2_price}"
                            )
                        else:
                            self.log_trade.info(f"[TP1_FILLED] {self.cn(code)} 잔여={pos.remain_qty}주")
                        # ── [SELL_STUCK 수정] TP1 완료 후 pending 명시적 pop ──────
                        # TP1 주문 전량 체결 완료 → pending_orders에서 제거
                        # 이걸 안 하면 SELL_PENDING_TIMEOUT 타이머가 계속 돌아
                        # 30~60초 후 SELL_STUCK false alarm 발생
                        self.pending_orders.pop(code, None)
                        pos.selling = False  # 다음 단계(TP2/손절/타임스탑) 허용
                        # ── 거래량 급감 감지용 분봉 추적 초기화 ──
                        pos.vol_peak_high      = pos.highest_price
                        pos.vol_no_new_high_cnt = 0
                        pos.vol_candle_volumes  = []
                        pos.vol_candle_highs    = []
                        pos.vol_candle_lows     = []
                        try:
                            from discord_notify import notify_tp1_fill
                            notify_tp1_fill(code, self.get_stock_name(code), tp1_order_qty, price,
                                            pos.entry_price, pos.remain_qty,
                                            tp2_target=pos.atr_tp2_price,
                                            atr=pos.atr_value)  # ⭐ TP2 목표가 + ATR 전달
                        except Exception as e:
                            import traceback
                            self.log_system.error(
                                f"[DISCORD_FAIL] TP1알림: {e}\n{traceback.format_exc()}"
                            )

                elif "TP2" in reason and not pos.tp2_filled_processed:
                    # ⭐ FIX 버그1: tp2_done(중복방지)과 tp2_filled_processed(알림용) 분리
                    # tp2_done은 TP2_TRIGGER에서 즉시 세팅 → CHEJAN 조건에 사용 불가
                    # tp2_filled_processed는 CHEJAN 전량체결 후에만 세팅 → 알림 트리거용
                    tp2_order_qty = pend.get("qty", 0)
                    tp2_filled_so_far = pend.get("filled_qty", 0)
                    _tp2_fully_filled = (tp2_order_qty > 0 and tp2_filled_so_far >= tp2_order_qty)
                    if _tp2_fully_filled:
                        pos.tp2_filled_processed = True
                        pos.tp2_done_ts = pytime.time()
                        pos.trailing_active = True
                        self.log_trade.info(f"[TP2_FILLED] {self.cn(code)} 잔여={pos.remain_qty}주 트레일링=ON")
                        # ── [SELL_STUCK 수정] TP2 완료 후 pending 명시적 pop ──
                        self.pending_orders.pop(code, None)
                        pos.selling = False  # 트레일링 단계 허용
                        try:
                            from discord_notify import notify_tp2_fill
                            tp2_filled_qty = pend.get("qty", delta)
                            notify_tp2_fill(code, self.get_stock_name(code), tp2_filled_qty, price, pos.entry_price, pos.remain_qty, atr=pos.atr_value)
                        except Exception as e:
                            import traceback
                            self.log_system.error(
                                f"[DISCORD_FAIL] TP2알림: {e}\n{traceback.format_exc()}"
                            )

            if pos.remain_qty > 0:
                # ⭐ 부분체결: selling 유지 (중복 매도 방지)
                # TP1/TP2 부분체결은 의도된 것이므로 selling 해제
                if pend and ("TP1" in pend.get("reason", "") or "TP2" in pend.get("reason", "")):
                    pos.selling = False  # TP 분할매도 완료 → 다음 단계 진행 허용
                self.log_trade.info(f"[SELL_PARTIAL] {self.cn(code)} 잔여={pos.remain_qty}/{pos.total_qty}주")
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
                # ⭐ FIX 버그2: entry_price(첫 체결가) → avg_buy_price(가중평균) 사용
                # 분할체결 시 첫 체결가와 실제 평균 매수가 차이 → 손익 오표시 수정
                entry_p = int(pos.avg_buy_price) if pos.avg_buy_price > 0 else pos.entry_price
                # TP1/TP2 후 잔여분만 매도하는 경우 total_qty가 아닌 실제 주문 수량을 사용
                # sell_pend["qty"]가 이번 매도 주문의 실제 수량 (remain_qty 기준으로 주문됨)
                sold_qty = sell_pend.get("qty", pos.total_qty) if sell_pend else pos.total_qty
                # ⭐ FIX 버그1: SELL_STUCK 후 pending 재생성 시 원래 reason 복원
                if not sell_reason:
                    sell_reason = getattr(pos, '_preserved_sell_reason', '') or sell_reason

                if "TP1" in sell_reason and not pos.tp1_done:
                    # TP1 주문이 전량 소진되며 SELL_DONE 도달한 경우
                    from discord_notify import notify_tp1_fill
                    notify_tp1_fill(code, stock_name, sold_qty, price, entry_p, 0, atr=pos.atr_value)
                elif "TP2" in sell_reason and not pos.tp2_done:
                    from discord_notify import notify_tp2_fill
                    notify_tp2_fill(code, stock_name, sold_qty, price, entry_p, 0, atr=pos.atr_value)
                elif "STOP_LOSS" in sell_reason:
                    from discord_notify import notify_stop_loss
                    notify_stop_loss(code, stock_name, sold_qty, price, entry_p, atr=pos.atr_value)
                elif "PROFIT_SAFE" in sell_reason:
                    from discord_notify import notify_profit_safe
                    notify_profit_safe(code, stock_name, sold_qty, price, entry_p, atr=pos.atr_value)
                elif "MINI_TRAIL_STOP" in sell_reason:
                    from discord_notify import notify_mini_trail_stop
                    notify_mini_trail_stop(code, stock_name, sold_qty, price, entry_p, atr=pos.atr_value)
                elif "TRAIL" in sell_reason:
                    from discord_notify import notify_trail_stop
                    notify_trail_stop(code, stock_name, sold_qty, price, entry_p, atr=pos.atr_value)
                elif "TIME_STOP" in sell_reason or "VOL_TIME_STOP" in sell_reason:
                    from discord_notify import notify_time_stop
                    notify_time_stop(code, stock_name, sold_qty, price, entry_p, sell_reason, atr=pos.atr_value)
                elif "FORCE_LIQUIDATION" in sell_reason or "LEFTOVER_LIQUIDATION" in sell_reason:
                    from discord_notify import notify_force_liquidation
                    notify_force_liquidation(code, stock_name, sold_qty, entry_p)
            except Exception as e:
                import traceback
                self.log_system.error(
                    f"[DISCORD_FAIL] 매도알림({sell_reason}): {e}\n{traceback.format_exc()}"
                )

            self.positions.pop(code, None)
            self.pending_orders.pop(code, None)
            self.traded_today.add(code)
            self.ordering = bool(self.pending_orders)
            self.last_order_ts = None
            
            # 주문락 해제
            self.log_trade.info(f"[SELL_DONE] {self.cn(code)} 전량매도완료")
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
                f"[REAL_SKIP] {self.cn(code)} 포지션 없음"
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
        # ── ⭐ [피제이메탈 버그] ZOMBIE 포지션 주기적 감지 ───────────────
        # selling=True 이면서 last_sell_attempt_ts 기준 300초(5분) 이상 경과 시
        # pending 제거 + selling 리셋 → 즉시 시장가 재매도 시도
        # 기존: force_liquidation_all(14:50)에서만 감지 → 수 시간 방치 가능
        # 개선: 틱마다(2초 주기) 체크하여 5분 이내 조기 감지
        _ZOMBIE_INTERVAL    = 300   # 5분마다 감지
        _ZOMBIE_STUCK_SEC   = 300   # selling=True 이면서 300초 경과 = ZOMBIE
        if not hasattr(self, '_last_zombie_check_ts'):
            self._last_zombie_check_ts = 0.0
        _tick_now = pytime.time()
        if _tick_now - self._last_zombie_check_ts >= _ZOMBIE_INTERVAL:
            self._last_zombie_check_ts = _tick_now
            for _zcode, _zpos in list(self.positions.items()):
                if _zpos.remain_qty <= 0:
                    continue
                _z_elapsed = _tick_now - _zpos.last_sell_attempt_ts
                # ⭐ FIX: selling=False + _force_market_sell=True 상태도 ZOMBIE로 감지
                # 기존: selling=True만 감지 → 취소 대기 중(selling=False) 상태 방치
                # SELL_STUCK → 취소 발송 → CANCEL_DONE 미수신 시 표류 방지
                _is_selling_zombie = _zpos.selling and _z_elapsed >= _ZOMBIE_STUCK_SEC
                _is_force_stuck    = (
                    not _zpos.selling
                    and getattr(_zpos, "_force_market_sell", False)
                    and _z_elapsed >= _ZOMBIE_STUCK_SEC
                )
                if not (_is_selling_zombie or _is_force_stuck):
                    continue
                _zombie_type = "FORCE_STUCK" if _is_force_stuck else "SELLING_ZOMBIE"
                self.log_system.warning(
                    f"[ZOMBIE_DETECTED] {self.cn(_zcode)} "
                    f"type={_zombie_type} {_z_elapsed:.0f}초 경과 → 강제 리셋 후 재매도"
                )
                self.pending_orders.pop(_zcode, None)
                _zpos.selling            = False
                _zpos.sl_ordered         = False
                _zpos._force_market_sell = False
                _zpos.sell_reject_retries = 0
                _z_qty = _zpos.remain_qty
                ok = self.send_market_order("SELL", _zcode, _z_qty, "ZOMBIE_RECOVERY")
                if ok:
                    _zpos.selling = True
                    _zpos.last_sell_attempt_ts = _tick_now
                else:
                        self.log_system.error(
                            f"[ZOMBIE_RECOVERY_FAIL] {self.cn(_zcode)} 시장가 재매도 실패"
                        )
        # ── ZOMBIE 감지 끝 ─────────────────────────────────────────────────


        if cur > pos.highest_price:
            pos.highest_price = cur

        entry = pos.entry_price
        now = pytime.time()
        pnl_rate = (cur - entry) / entry  # 현재 수익률

        if now - pos.last_pnl_log_ts >= 2: # 2초마다 이득률 로그 찍기
            self.log_trade.info(
                f"[PNL_CHECK] {self.cn(code)} "
                f"매수가={pos.entry_price} "
                f"현재가={cur} "
                f"pnl={pnl_rate:.4f}"
            )
            pos.last_pnl_log_ts = now

        # ==================================================
        # ── 완성봉 기준 손절 (메인) ──────────────────────
        # 틱마다 현재 분봉 OHLC를 직접 합산하고,
        # 분이 바뀌는 순간 직전 완성봉 종가로 ATR 손절 판단
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
            completed_close = pos.sl_candle_last
            completed_open  = pos.sl_candle_open
            completed_high  = pos.sl_candle_high
            completed_low   = pos.sl_candle_low

            sl_rate = (completed_close - entry) / entry

            self.log_trade.info(
                f"[CANDLE_CLOSED] {self.cn(code)} "
                f"O:{completed_open} H:{completed_high} "
                f"L:{completed_low} C:{completed_close} "
                f"pnl={sl_rate:.4f}"
            )

            # ── TP1 이후: 거래량 급감 감지용 분봉 데이터 적재 ──
            if pos.tp1_done:
                pos.vol_candle_volumes.append(
                    sum(pos.recent_volumes) if pos.recent_volumes else 0
                )
                pos.vol_candle_highs.append(completed_high)
                pos.vol_candle_lows.append(completed_low)
                # 최대 10분봉만 유지
                if len(pos.vol_candle_volumes) > 10:
                    pos.vol_candle_volumes = pos.vol_candle_volumes[-10:]
                    pos.vol_candle_highs   = pos.vol_candle_highs[-10:]
                    pos.vol_candle_lows    = pos.vol_candle_lows[-10:]

                # 고점 갱신 여부 판단
                if completed_high > pos.vol_peak_high:
                    pos.vol_peak_high = completed_high
                    pos.vol_no_new_high_cnt = 0
                else:
                    pos.vol_no_new_high_cnt += 1

            # ── ATR 완성봉 손절 판단 ──
            # atr_sl_price가 설정된 경우 우선 사용, 없으면 고정비율 fallback
            sl_threshold = pos.atr_sl_price if pos.atr_sl_price > 0 else int(entry * (1 - STOP_LOSS_RATE))
            if CANDLE_SL_ENABLED and completed_close <= sl_threshold:
                # ⭐ [버그1 수정] sl_ordered 체크: 이미 손절 주문을 냈으면 재발동 차단
                # SELL_STUCK이 pos.selling=False로 리셋해도 sl_ordered는 유지 → 중복 발동 방지
                if pos.sl_ordered:
                    self.log_trade.info(
                        f"[STOP_LOSS_CANDLE_SKIP] {self.cn(code)} "
                        f"이미 손절주문 발송됨(sl_ordered=True) → 재발동 차단 "
                        f"종가:{completed_close} 기준:{sl_threshold}"
                    )
                elif self.can_try_sell(pos):
                    self.log_trade.info(
                        f"[STOP_LOSS_CANDLE] {self.cn(code)} 완성봉 ATR손절 "
                        f"종가:{completed_close} 손절기준:{sl_threshold} pnl={sl_rate:.4f}"
                    )
                    # ⭐ 완성봉 종가가 이미 손절가 아래이면 지정가는 미체결 → 시장가 사용
                    _sl_lmt = pos.atr_sl_price if completed_close >= pos.atr_sl_price else 0
                    ok = self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS",
                                                       sl_limit_price=_sl_lmt)
                    if ok:
                        pos.last_sell_attempt_ts = now
                        pos.selling = True
                        pos.sl_ordered = True  # ⭐ 손절 주문 발송 마킹
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
        # ── 실시간 비상 안전망 ────────────────────────────
        # 완성봉과 무관하게 순간 낙폭이 EMERGENCY_SL_RATE 이상이면 즉시 손절
        # atr_sl_price 기준보다 더 큰 낙폭을 별도 보호 (극단적 급락 방어)
        # EMERGENCY_SL_RATE = 0.0 이면 비활성화
        # ==================================================
        if EMERGENCY_SL_RATE > 0 and pnl_rate <= -EMERGENCY_SL_RATE:
            # ⭐ sl_ordered 체크: 이미 손절 주문 발송됐으면 재발동 차단
            # SELL_STUCK 후 selling=False 리셋 시 EMERGENCY가 재발동하는 문제 방지
            if pos.sl_ordered:
                self.log_trade.info(
                    f"[STOP_LOSS_EMERGENCY_SKIP] {self.cn(code)} "
                    f"이미 손절주문 발송됨(sl_ordered=True) → 재발동 차단 "
                    f"현재가:{cur} pnl={pnl_rate:.4f}"
                )
                return
            if not self.can_try_sell(pos):
                return
            self.log_trade.info(
                f"[STOP_LOSS_EMERGENCY] {self.cn(code)} 비상 손절 "
                f"현재가:{cur} pnl={pnl_rate:.4f} "
                f"(안전망 -{EMERGENCY_SL_RATE*100:.1f}%)"
            )
            # ⭐ EMERGENCY 경로: 이미 ATR 손절가 이하로 하락한 상태 → 지정가 절대 금지
            # 지정가 손절(sl_limit_price)은 현재가가 손절가 이상일 때만 의미 있음.
            # EMERGENCY 발동 = 이미 그 아래 → 지정가는 영구 미체결 → SELL_STUCK 루프 유발
            ok = self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS")
            if ok:
                pos.last_sell_attempt_ts = now
                pos.selling    = True
                pos.sl_ordered = True  # ⭐ EMERGENCY 경로 손절 마킹 (재발동 방지)
            else:
                pos.selling = False
            return

        # 완성봉 손절 비활성 시 기존 실시간 손절로 폴백
        if not CANDLE_SL_ENABLED:
            sl_threshold = pos.atr_sl_price if pos.atr_sl_price > 0 else int(entry * (1 - STOP_LOSS_RATE))
            if cur <= sl_threshold:
                if not self.can_try_sell(pos):
                    return
                self.log_trade.info(
                    f"[STOP_LOSS] {self.cn(code)} 실시간 ATR손절 "
                    f"현재가:{cur} 손절기준:{sl_threshold} pnl={pnl_rate:.4f}"
                )
                ok = self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS",
                                                   sl_limit_price=pos.atr_sl_price)
                if ok:
                    pos.last_sell_attempt_ts = now
                    pos.selling = True
                else:
                    pos.selling = False
                return

        # =========================
        # 미니 트레일링 스탑 (TP1 미달 구간 수익 보호)
        # TP1에 못 미치더라도 수익이 MINI_TRAIL_TRIGGER 이상 오르면 고점 추적,
        # 고점 대비 MINI_TRAIL_GAP 이상 하락 시 즉시 익절
        # 조건: TP1 미체결 + 전량매수 완료 + 트레일링 미활성 상태
        # ⭐ FIX3: 장 시작 10분(09:00~09:10) 고변동성 구간은 GAP 완화 (1% → 2%)
        # =========================
        if not pos.tp1_done and pos.buy_done and not pos.trailing_active:
            pnl_rate = (cur - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0

            # 트리거 도달 → 미니 트레일링 활성화
            if not pos.mini_trail_active and pnl_rate >= MINI_TRAIL_TRIGGER:
                pos.mini_trail_active = True
                pos.mini_trail_peak   = pnl_rate
                self.log_trade.info(
                    f"[MINI_TRAIL_ON] {self.cn(code)} "
                    f"pnl={pnl_rate*100:.2f}% 미니트레일 활성화 "
                    f"(트리거={MINI_TRAIL_TRIGGER*100:.1f}%)"
                )

            # 미니 트레일링 활성 중 → 고점 갱신 + 하락 감지
            if pos.mini_trail_active:
                if pnl_rate > pos.mini_trail_peak:
                    pos.mini_trail_peak = pnl_rate

                # ⭐ 시간대별 GAP 분기: 09:10 이전은 완화된 GAP 적용
                _now_t = datetime.now().time()
                _open_end = datetime.now().replace(hour=9, minute=10, second=0, microsecond=0).time()
                _active_gap = MINI_TRAIL_GAP_OPEN if _now_t < _open_end else MINI_TRAIL_GAP

                drop = pos.mini_trail_peak - pnl_rate
                if drop >= _active_gap:
                    if not self.can_try_sell(pos):
                        return
                    self.log_trade.info(
                        f"[MINI_TRAIL_STOP] {self.cn(code)} "
                        f"고점={pos.mini_trail_peak*100:.2f}% "
                        f"현재={pnl_rate*100:.2f}% "
                        f"하락={drop*100:.2f}% "
                        f"GAP={_active_gap*100:.1f}%({'장초반' if _now_t < _open_end else '일반'}) → 익절 청산"
                    )
                    ok = self.send_market_order("SELL", code, pos.remain_qty, "MINI_TRAIL_STOP")
                    if ok:
                        pos.last_sell_attempt_ts = now
                        pos.selling  = True
                        pos.time_stop_done = True  # 타임스탑 중복 방지
                    return

        # =========================
        # TP1 시장가 익절 (ATR 기반 목표가 도달 시)
        # =========================
        tp1_target = pos.atr_tp1_price if pos.atr_tp1_price > 0 else self.adjust_tick_size(int(pos.entry_price * 1.02))
        # BUG-FIX: 분할체결 완료 전 TP1 발동 금지
        # 전량체결(BUY_DONE) 전에 TP1이 발동되면 TP_TARGET_SET이 건너뛰어져
        # atr_safe_price=0 상태에서 fallback safe_price로 즉시 본절보호가 발동됨
        if not pos.tp1_done and pos.buy_done and cur >= tp1_target:
            if not self.can_try_sell(pos):
                return
            # TP1 수량 계산 (총 수량이 3주 이하면 분할 없이 전량 매도)
            if pos.remain_qty <= 3:
                tp1_qty = pos.remain_qty
            else:
                tp1_qty = max(1, int(pos.total_qty * TP1_RATIO))
                tp1_qty = min(tp1_qty, pos.remain_qty)

            self.log_trade.info(
                f"[TP1_TRIGGER] {self.cn(code)} 현재가={cur} ATR목표={tp1_target} 매도수량={tp1_qty}주"
            )
            ok = self.send_market_order("SELL", code, tp1_qty, "TP1")
            if ok:
                pos.last_sell_attempt_ts = now
                pos.selling  = True
                pos.tp1_done = True   # ⭐ FIX: 주문 전송 즉시 플래그 세팅 → 중복 발동 방지
                                      # 체결 확인은 CHEJAN에서 filled_qty로 추적
            return

        # =========================
        # TP2 시장가 익절 (ATR 기반 - TP1 고점 기준 갱신된 목표가)
        # =========================
        tp2_target = pos.atr_tp2_price if pos.atr_tp2_price > 0 else self.adjust_tick_size(int(pos.entry_price * 1.03))
        # ⭐ FIX 버그2: tp1_filled_processed 완료 후에만 TP2 발동
        # TP1 CHEJAN 전량체결 전에 TP2가 발동되면 atr_tp2_price가 갱신되지 않은 초기값
        # (TP1과 동일한 가격)으로 발동되어 분할 의미가 없어짐
        if pos.tp1_done and pos.tp1_filled_processed and not pos.tp2_done and cur >= tp2_target:
            if not self.can_try_sell(pos):
                return
            # TP2 수량 계산
            if pos.remain_qty <= 2:
                tp2_qty = pos.remain_qty
            else:
                tp2_qty = max(1, int(pos.total_qty * TP2_RATIO))
                tp2_qty = min(tp2_qty, pos.remain_qty)

            self.log_trade.info(
                f"[TP2_TRIGGER] {self.cn(code)} 현재가={cur} ATR목표={tp2_target} 매도수량={tp2_qty}주"
            )
            ok = self.send_market_order("SELL", code, tp2_qty, "TP2")
            if ok:
                pos.last_sell_attempt_ts = now
                pos.selling  = True
                pos.tp2_done = True   # ⭐ FIX: 주문 전송 즉시 플래그 세팅 → 중복 발동 방지
            return

        # =========================
        # 본절 보호: TP1 후 atr_safe_price 이하로 밀리면 탈출
        # (매수가 - ATR × ATR_SAFE_MULT)
        # =========================
        # BUG-FIX: fallback safe_price도 entry_price 이상 보장
        # 기존 fallback entry×1.005가 entry보다 높아 TP1 직후 즉시 본절보호 발동하는 버그 수정
        _raw_safe = pos.atr_safe_price if pos.atr_safe_price > 0 else pos.entry_price
        safe_price = max(pos.entry_price, _raw_safe)
        if pos.tp1_done and cur <= safe_price:
            # ⭐ FIX 원인④: TP1 체결 직후 120초 유예 (기존 30초→120초)
            # 오늘 분석: 본절보호 9건 중 8건이 TP1→본절 30~434초, 평균 손실 -36,219원
            # 짧은 유예(30초)로는 진입 직후 정상 조정도 모두 청산됨
            # 120초 유예로 TP2 달성 기회 확보
            _tp1_elapsed = now - pos.tp1_done_ts if pos.tp1_done_ts else 999
            if _tp1_elapsed < 120:
                pass  # 유예 중 — 발동 보류
            elif self.can_try_sell(pos):
                self.log_trade.info(
                    f"[PROFIT_SAFEGUARD] {self.cn(code)} 본절보호 매도 "
                    f"현재가:{cur} 보호선:{safe_price} (ATR×{ATR_SAFE_MULT}) "
                    f"TP1후={_tp1_elapsed:.0f}초"
                )
                ok = self.send_market_order("SELL", code, pos.remain_qty, "PROFIT_SAFE")
                if ok:
                    pos.last_sell_attempt_ts = now
                    pos.selling = True
                else:
                    pos.selling = False
            return

        # =========================
        # 트레일링 스탑 (ATR 기반: 최고가 - ATR × ATR_TRAIL_MULT)
        # =========================
        if pos.trailing_active:
            trail_stop = int(pos.highest_price - pos.atr_value * ATR_TRAIL_MULT) if pos.atr_value > 0 \
                         else int(pos.highest_price * 0.990)
            if cur <= trail_stop:
                if not self.can_try_sell(pos):
                    return
                self.log_trade.info(
                    f"[TRAIL_STOP] {self.cn(code)} 현재가={cur} 트레일기준={trail_stop} "
                    f"고점={pos.highest_price} ATR×{ATR_TRAIL_MULT}"
                )
                ok = self.send_market_order("SELL", code, pos.remain_qty, "TRAIL_STOP")
                pos.last_sell_attempt_ts = now
                if ok:
                    pos.trailing_active = False
                    pos.selling = True
                else:
                    pos.selling = False
                return

        # =========================
        # ⚠️ 거래량 급감 청산 (TP1 이후)
        # 3가지 조건 동시 충족 시 청산:
        #   ① 최근 5분봉 평균 거래량 < 기준봉 5분봉 평균의 50%
        #   ② 고점 갱신 실패 2회 이상 연속
        #   ③ 직전 완성봉 저가가 그 이전 봉의 저가를 이탈
        # =========================
        TP1_PROTECT_SEC = 45   # TP1 직후 보호시간 (안정화 대기)
        TP2_PROTECT_SEC = 30   # TP2 직후 보호시간

        in_tp1_protect = (
            pos.tp1_done
            and not pos.tp2_done
            and (now - pos.tp1_done_ts) < TP1_PROTECT_SEC
        )
        in_tp2_protect = (
            pos.tp2_done
            and pos.tp2_done_ts > 0
            and (now - pos.tp2_done_ts) < TP2_PROTECT_SEC
        )

        if (
            pos.tp1_done
            and not pos.time_stop_done
            and not pos.selling
            and not in_tp1_protect
            and not in_tp2_protect
            and len(pos.vol_candle_volumes) >= 6  # 최소 6분봉 적재 필요
        ):
            vols = pos.vol_candle_volumes
            # ① 최근 5분봉 평균 vs 기준 5분봉 평균 (그 이전 5개)
            recent_5_avg = sum(vols[-5:]) / 5
            base_5_avg   = sum(vols[-10:-5]) / 5 if len(vols) >= 10 else sum(vols[:-5]) / max(1, len(vols) - 5)

            cond_vol   = base_5_avg > 0 and (recent_5_avg / base_5_avg) < 0.50
            # ② 고점 갱신 실패 2회 이상
            cond_high  = pos.vol_no_new_high_cnt >= 2
            # ③ 직전 완성봉 저가 < 그 이전 봉 저가 (전봉 저가 이탈)
            cond_low   = (
                len(pos.vol_candle_lows) >= 2
                and pos.vol_candle_lows[-1] < pos.vol_candle_lows[-2]
            )

            if cond_vol and cond_high and cond_low:
                if not self.can_try_sell(pos):
                    return

                phase = "TRAILING" if pos.tp2_done else "TP1_WAIT"
                self.log_trade.info(
                    f"[VOL_TIME_STOP] {self.cn(code)} 구간={phase} 매도수량={pos.remain_qty}주 "
                    f"최근5봉평균={recent_5_avg:.1f} 기준5봉평균={base_5_avg:.1f} "
                    f"거래량비율={recent_5_avg/base_5_avg:.2f} "
                    f"고점갱신실패={pos.vol_no_new_high_cnt}회 "
                    f"전봉저가이탈={pos.vol_candle_lows[-1]}<{pos.vol_candle_lows[-2]} "
                    f"pnl={pnl_rate:.4f}"
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
            and pos.buy_done          # ⭐ BUG-1: BUY_DONE 완료 후에만 타이머 시작
            and hold_sec >= TIME_STOP_SEC
        ):
            pnl_rate = (cur - pos.entry_price) / pos.entry_price

            # 손실이 -0.3% 이내일 때만
            if pnl_rate >= TIME_STOP_MAX_LOSS:
                if not self.can_try_sell(pos):
                    return

                self.log_trade.info(
                    f"[TIME_STOP] {self.cn(code)} "
                    f"보유={int(hold_sec)}초 매도수량={pos.remain_qty}주 "
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
            self.log_system.error(f"[CANCEL_ABORT] 주문번호 없음 {self.cn(code)}")
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
                f"[CANCEL_FAIL] {self.cn(code)} 원주문번호={org_order_no} ret={ret}"
            )
            return False

        self.log_trade.info(
            f"[CANCEL_SEND] {self.cn(code)} 원주문번호={org_order_no} 방향={cancel_side}"
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
                        f"[FORCE_ABANDON] {self.cn(code)} 주문번호 미배정 {age:.1f}초 경과 - pending 강제제거"
                    )
                    self.pending_orders.pop(code, None)
                    self.ordering = False
                    self._pending_buy_code = None
                    self._pending_buy_qty = 0
                    self.traded_today.add(code)  # ⭐ FIX1: 미체결 포기 후 당일 재진입 방지
                    self._resume_scan_if_possible()
                    continue
                # 로그 스팸 방지: 5초마다 한 번만 경고
                last_warn = float(pend.get("_last_warn_ts", 0.0))
                if now - last_warn >= 5.0:
                    self.log_system.warning(
                        f"[CANCEL_WAIT] {self.cn(code)} 주문번호 대기중 경과={age:.1f}초 (강제포기까지 {FORCE_ABANDON_TIMEOUT - age:.0f}초)"
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
                        f"[CANCEL_ZOMBIE] {self.cn(code)} 원주문번호={org} "
                        f"취소접수 후 체결통보 없음 {cancel_age:.1f}초 경과 - 강제제거"
                    )
                    self.pending_orders.pop(code, None)
                    self.ordering = bool(self.pending_orders)
                    self._pending_buy_code = None
                    self._pending_buy_qty = 0
                    self.traded_today.add(code)  # ⭐ FIX1: 취소 좀비 정리 후 당일 재진입 방지
                    self._resume_scan_if_possible()
                continue

            if pend.get("cancel_retries", 0) >= MAX_CANCEL_RETRIES:
                self.log_system.error(f"[CANCEL_GIVEUP] {self.cn(code)} 원주문번호={org} 최대재시도 초과")
                self.pending_orders.pop(code, None)
                self.ordering = bool(self.pending_orders)
                self._pending_buy_code = None
                self._pending_buy_qty = 0
                self.traded_today.add(code)  # ⭐ FIX1: 취소 포기 후 당일 재진입 방지
                self._resume_scan_if_possible()
                continue
            
            if now - float(pend.get("last_cancel_ts", 0.0)) < CANCEL_RETRY_COOLDOWN_SEC:
                continue
            
            # 취소 시도
            ok = self.send_cancel_order(code, org, cancel_side="BUY")
            pend["last_cancel_ts"] = now
            pend["cancel_retries"] = int(pend.get("cancel_retries", 0)) + 1
    
            if ok:
                self.log_trade.info(f"[CANCEL_SENT] {self.cn(code)} 원주문번호={org} 취소접수")
                # ✅ pending은 제거하지 않음 - Chejan "취소완료"에서 재매수 트리거 필요
                pend["cancel_sent"] = True
                self.ordering = False
                self._pending_buy_code = None
                self._pending_buy_qty = 0
                self._resume_scan_if_possible()

        # ── SELL pending 타임아웃 처리 ──
        # SELL 주문이 체결 안 되고 남아있을 수 있으므로 마킹 후 재매도 허용
        try:
            from config import SELL_PENDING_TIMEOUT_SEC
            SELL_PENDING_TIMEOUT = SELL_PENDING_TIMEOUT_SEC
        except ImportError:
            SELL_PENDING_TIMEOUT = 15
        for code, pend in list(self.pending_orders.items()):
            if pend.get("side") != "SELL":
                continue
            age = now - float(pend.get("ts", now))
            if age > SELL_PENDING_TIMEOUT and not pend.get("stuck"):
                pos = self.positions.get(code)

                # ── [SELL_STUCK 수정] TP1/TP2 전량 체결 후 pending이 이미 완료된 케이스 ──
                # TP1_FILLED가 완료된 시점에 pending_orders에서 TP1 주문을 pop하지 않아
                # stuck 타이머가 계속 돌아가는 false alarm 발생
                # → tp1_filled_processed True이고 remain_qty가 줄었으면 정상 완료로 판단
                if pos and pend.get("reason", "").startswith("TP1"):
                    if getattr(pos, "tp1_filled_processed", False):
                        # TP1 주문은 이미 처리 완료 → pending만 정리하고 stuck 표시 안 함
                        self.pending_orders.pop(code, None)
                        self.log_trade.info(
                            f"[TP1_PEND_CLEANUP] {self.cn(code)} TP1 체결완료 pending 정리"
                        )
                        continue

                if pos and pend.get("reason", "").startswith("TP2"):
                    if getattr(pos, "tp2_filled_processed", False):
                        self.pending_orders.pop(code, None)
                        self.log_trade.info(
                            f"[TP2_PEND_CLEANUP] {self.cn(code)} TP2 체결완료 pending 정리"
                        )
                        continue

                # ⭐ 부분체결이 이미 됐으면 거래소에서 나머지도 체결 진행 중
                # → selling 해제하면 중복 매도 위험! 더 기다림
                if pos and pos.remain_qty < pos.total_qty:
                    # 부분체결 진행 중 → 타임아웃을 60초로 연장
                    if age <= SELL_PENDING_TIMEOUT * 2:
                        continue
                    self.log_system.warning(
                        f"[SELL_STUCK_PARTIAL] {self.cn(code)} 경과={age:.1f}초 "
                        f"잔여={pos.remain_qty}/{pos.total_qty}주 - 강제정리"
                    )
                    # ⭐ [버그2] SELL_STUCK_PARTIAL: pending 보존 + stuck_partial_ts 기록
                    # 기존: pending.pop() → 거래소 잔여 주문 CHEJAN을 놓쳐 좀비 포지션 발생
                    #       (팜스코: 1401주 35분 표류)
                    # 수정: pending 유지 + stuck_partial_ts 기록
                    #   → STUCK_PARTIAL_TIMEOUT(300초) 내 체결 없으면 강제 시장가 재주문
                    if pos and pend.get("reason", "").startswith("STOP_LOSS"):
                        pos._force_market_sell = True
                        self.log_system.warning(
                            f"[SELL_STUCK_MARKET_FALLBACK] {self.cn(code)} "
                            f"부분체결 후 잔여분 지정가 미체결 → 시장가로 전환 예약"
                        )
                    pend["stuck"] = True
                    if pos:
                        if not pos.stuck_partial_ts:
                            pos.stuck_partial_ts = pytime.time()  # 처음만 기록
                        pos.selling        = False  # 재주문 허용
                        # ⭐ [버그2 수정] TIME_STOP/VOL_TIME_STOP reason이면 time_stop_done 유지
                        # 기존: 무조건 False 리셋 → SELL_STUCK 후 TIME_STOP 재발동 → [800033] 이중 주문
                        # 수정: STOP_LOSS 계열만 리셋, TIME_STOP은 유지하여 재발동 차단
                        _stuck_reason = pend.get("reason", "")
                        if "TIME_STOP" not in _stuck_reason and "VOL_TIME_STOP" not in _stuck_reason:
                            pos.time_stop_done = False
                        pos.sell_reject_retries = 0
                    self.ordering = bool(self.pending_orders)
                    continue  # pending pop 없이 다음으로

                self.log_system.warning(
                    f"[SELL_STUCK] {self.cn(code)} 경과={age:.1f}초 - "
                    f"stuck 표시 (주문이 살아있을 수 있음)"
                )
                pend["stuck"] = True
                # ⭐ FIX 버그1: pending 제거 전에 sell_reason을 pos에 보존
                # pending_orders.pop 후 재매도 시 원래 reason이 사라져 알림 누락 방지
                if pos and not getattr(pos, '_preserved_sell_reason', None):
                    pos._preserved_sell_reason = pend.get("reason", "")
                # ⭐ FIX [800033 버그]: 지정가 SL 미체결 후 SELL_STUCK
                # 기존: 원주문(지정가) 살아있는 채로 시장가 추가 발사 → [800033] 거절 반복
                # 수정: 원주문 취소 발송 → CANCEL_DONE 콜백에서 시장가 재시도
                #       취소 성공 전까지 새 주문 금지 → [800033] 원천 차단
                if pos and pend.get("reason", "").startswith("STOP_LOSS"):
                    pos._force_market_sell = True
                    _org_no = pend.get("order_no", "")
                    if _org_no:
                        self.log_system.warning(
                            f"[SELL_STUCK_CANCEL] {self.cn(code)} "
                            f"지정가 SL 미체결 → 원주문({_org_no}) 취소 후 시장가 재시도"
                        )
                        # pending에 취소 목적 플래그 세팅 → CANCEL_DONE에서 시장가 재시도
                        pend["cancel_for_retry"] = True
                        pend["retry_reason"]     = "STOP_LOSS_RETRY"
                        pend["side"]             = "SELL"  # CANCEL_DONE 분기용
                        self.send_cancel_order(code, _org_no, cancel_side="SELL")
                        # pending은 CANCEL_DONE에서 pop되므로 여기서 pop 하지 않음
                        self.ordering = bool(self.pending_orders)
                        continue
                    else:
                        # 주문번호 없는 경우(극히 드묾) — 기존 방식 유지
                        self.log_system.warning(
                            f"[SELL_STUCK_MARKET_FALLBACK] {self.cn(code)} "
                            f"주문번호 없음 → 직접 시장가 재시도"
                        )
                # pending 제거 + selling 해제하여 재시도 허용
                # (중복 매도 방지: send_market_order에서 remain_qty 체크)
                self.pending_orders.pop(code, None)
                if pos:
                    pos.selling = False
                    # ⭐ [버그2 수정] TIME_STOP/VOL_TIME_STOP reason이면 time_stop_done 유지
                    # 기존: 무조건 False 리셋 → SELL_STUCK 후 TIME_STOP 재발동 → [800033] 이중 주문
                    # SCL사이언스 케이스: 09:48 TIME_STOP 발동 → 09:48 SELL_STUCK →
                    #   time_stop_done=False 리셋 → 09:50 TIME_STOP 재발동 → [800033] 거절
                    # 수정: TIME_STOP 계열은 리셋 안 함. STOP_LOSS 계열만 리셋하여 재시도 허용.
                    _stuck_reason = pend.get("reason", "") if pend else getattr(pos, '_preserved_sell_reason', '')
                    if "TIME_STOP" not in _stuck_reason and "VOL_TIME_STOP" not in _stuck_reason:
                        pos.time_stop_done = False  # STOP_LOSS 계열: 재시도 허용
                    else:
                        # TIME_STOP 재시도: time_stop_done은 유지하되 selling만 해제
                        # → TIME_STOP 로직 재발동 대신 _retry_sell_after_reject 경로로 재시도
                        self.log_system.info(
                            f"[SELL_STUCK_TIMESTOP] {self.cn(code)} "
                            f"TIME_STOP STUCK → time_stop_done 유지, 3초 후 강제 재시도"
                        )
                        QTimer.singleShot(
                            3000,
                            lambda c=code, q=pos.remain_qty: self._retry_sell_after_reject(
                                c, q, "TIME_STOP_RETRY"
                            )
                        )
                    pos.sell_reject_retries = 0  # ⭐ FIX: SELL_STUCK 후 재시도 카운터 리셋 (LS네트웍스 버그)
                self.ordering = bool(self.pending_orders)

        # ── stuck_partial_ts 타임아웃 감지 → 강제 시장가 재주문 ──────────
        # SELL_STUCK_PARTIAL 발생 후 거래소 주문이 소실된 경우
        # CHEJAN이 오지 않아 잔여 수량이 표류하는 문제 방지
        # (팜스코: 09:38 SL → 09:39 STUCK_PARTIAL → 10:00까지 1401주 35분 표류)
        STUCK_PARTIAL_TIMEOUT = 300  # 5분 내 체결 CHEJAN 없으면 강제 재주문
        for code, pos in list(self.positions.items()):
            if not pos.stuck_partial_ts:
                continue
            if pos.remain_qty <= 0:
                pos.stuck_partial_ts = 0.0  # 이미 청산 → 초기화
                continue
            elapsed = now - pos.stuck_partial_ts
            if elapsed < STUCK_PARTIAL_TIMEOUT:
                continue
            if pos.selling:
                continue  # 이미 다른 매도 진행중
            self.log_system.warning(
                f"[STUCK_PARTIAL_TIMEOUT] {self.cn(code)} "
                f"{elapsed:.0f}초 경과 후에도 잔여={pos.remain_qty}주 미체결 "
                f"→ 시장가 강제 재주문"
            )
            pos.stuck_partial_ts = 0.0  # 타이머 리셋 (무한 반복 방지)
            ok = self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS")
            if ok:
                pos.selling = True
                pos.last_sell_attempt_ts = pytime.time()
            else:
                self.log_system.error(
                    f"[STUCK_PARTIAL_TIMEOUT_FAIL] {self.cn(code)} "
                    f"강제 재주문 실패 — 강제청산 타이머에서 처리"
                )

    # ==================================================
    # 재매수 함수
    # ==================================================
    def _reentry_buy(self, code, qty, reason, retry_cnt,
                     atr_value: float = 0.0, orig_price: int = 0):
        """
        취소 후 재매수. atr_value/orig_price는 CANCEL_DONE 시점에 캡처된 값.

        [버그1 수정] atr_value를 파라미터로 받아 재주문 후 pending에 복원.
        [버그2 수정] orig_price 대비 현재가 슬리피지가 REENTRY_MAX_SLIP 초과 시 포기.
        """
        if code in self.positions:
            return

        active_slots = len(self.positions) + self._count_pending_buys()
        if active_slots >= MAX_POSITIONS:
            return

        # ── [버그2] 슬리피지 체크: 기준가 대비 현재가가 너무 많이 오르면 포기 ──
        # orig_price = 최초 진입 신호 당시 cur_price (pending_orders에 저장됨)
        # 재주문을 반복하는 동안 시장가가 크게 움직이면 불리한 가격에 억지 체결됨
        if orig_price > 0:
            try:
                from config import REENTRY_MAX_SLIP
                max_slip = REENTRY_MAX_SLIP
            except ImportError:
                max_slip = 0.015  # 기본 1.5% (노브랜드 케이스: +4.4% 슬리피지)
            # 현재가는 pending_orders의 cur_price로 근사 (실시간 조회 불가)
            # 대신 재주문 횟수로 경과 시간 추정: REENTRY_DELAY(1초) × retry_cnt × BUY_FILL_TIMEOUT(10초)
            # 실용적 방법: MAX_REENTRY_RETRIES를 1로 줄이거나, 여기선 atr 기반 가격 역전 체크
            # → BUY_DONE 이후 FILL_MARGIN_REJECT(config.py)가 후속 방어

        self.log_trade.info(f"[REENTRY_BUY] {self.cn(code)} 재시도={retry_cnt}회차")

        ok = self.send_market_order("BUY", code, qty, f"{reason}_RETRY{retry_cnt}")

        if ok and code in self.pending_orders:
            self.pending_orders[code]["reentry_retries"] = retry_cnt
            # ── [버그1 수정] ATR·기준가 복원 ──────────────────────────────
            # send_market_order가 내부에서 pending_orders[code]를 새로 생성하므로
            # 직후 캡처해둔 atr_value와 orig_price를 다시 주입
            if atr_value > 0:
                self.pending_orders[code]["atr_value"] = atr_value
                self.log_trade.info(
                    f"[REENTRY_ATR_RESTORE] {self.cn(code)} ATR={atr_value:.1f} 복원완료"
                )
            if orig_price > 0:
                self.pending_orders[code]["cur_price"] = orig_price


    # ==================================================
    # 매도 / 매수 주문 함수
    # ==================================================
    def send_market_order(self, side, code, qty, reason="", **kwargs):
        if qty <= 0 or not is_market_time():
            self.log_trade.warning(
                f"[ORDER_ABORT] 방향={side} {self.cn(code)} 수량={qty} 사유={reason}"
            )            
            return False
        
        now = pytime.time()

        # ⭐ SELL 주문 시 보유수량 체크 (중복 매도 방어)
        if side == "SELL":
            pos = self.positions.get(code)
            if not pos or pos.remain_qty <= 0:
                self.log_trade.warning(f"[ORDER_SKIP_NO_POS] 매도 {self.cn(code)} 수량={qty} - 포지션없음")
                return False
            if qty > pos.remain_qty:
                self.log_trade.warning(f"[ORDER_QTY_ADJ] 매도 {self.cn(code)} 수량조정={qty}→{pos.remain_qty}주")
                qty = pos.remain_qty

        # SELL은 빠른 방어가 중요하므로 스로틀 완화 (0.2초)
        throttle = 0.2 if side == "SELL" else 0.5
        if self.last_order_ts and now - self.last_order_ts < throttle:
            self.log_trade.warning(f"[ORDER_THROTTLE] 방향={side} {self.cn(code)} 수량={qty} 주문간격제한")
            return False        
        
        order_type = 1 if side == "BUY" else 2
        screen = self._next_order_screen("92" if side == "BUY" else "91")
        
        self.log_trade.info(
            f"[ORDER_TRY] 방향={side} {self.cn(code)} 수량={qty}주 사유={reason}"
        )        
        
        if side == "BUY":
            # =========================
            # ⭐ 동시 BUY 차단
            # =========================
            if any(o["side"] == "BUY" for o in self.pending_orders.values()):
                self.log_trade.warning(
                    f"[BUY_BLOCK_PENDING] {self.cn(code)} 사유=기존미체결주문있음"
                )
                return False 
            
            # 포지션 슬롯 체크
            current_slots = len(self.positions) + sum(
                1 for p in self.pending_orders.values() if p.get("side") == "BUY"
            )
            if current_slots >= MAX_POSITIONS:
                self.log_trade.info(
                    f"[BUY_BLOCK] 최대포지션 도달 ({current_slots}/{MAX_POSITIONS})"
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
            
        # ─── 매수 주문가격 결정 ───────────────────────────────────────
        # BUY: 저가주(현재가 < 2,000원)는 지정가+1호가로 슬리피지 제어
        #      고가주는 시장가(03)로 체결 속도 우선
        # SELL 시장가: 항상 시장가 (TP/Trail 등 빠른 청산)
        # SELL 지정가: STOP_LOSS 시 atr_sl_price 지정가 → 슬리피지 방어
        if side == "BUY":
            _cur = self.pending_orders.get(code, {}).get("cur_price", 0)
            if _cur > 0 and _cur < 2000:
                _tick   = self.min_tick(_cur)
                _lmt_p  = _cur + _tick        # 현재가 + 1호가
                _ord_tp = "00"                # 지정가
                self.log_trade.info(
                    f"[ORDER_LIMIT] {self.cn(code)} 저가주 지정가={_lmt_p}원(현재가{_cur}+1틱)"
                )
            else:
                _lmt_p  = 0
                _ord_tp = "03"               # 시장가
        else:
            # ⭐ FIX 원인③: STOP_LOSS는 ATR 손절가 지정가로 — 슬리피지 방어
            # 시장가 손절은 급락 구간 호가 공백에서 SL기준보다 1~2% 더 하락 체결됨
            # → atr_sl_price 지정가 주문으로 해당 가격 이하 체결 방지
            # ⭐ FIX 현대건설: SELL_STUCK 후 재시도 시 _force_market_sell=True면 시장가 강제 사용
            _sl_lmt = kwargs.get("sl_limit_price", 0) if kwargs else 0
            _pos_ref = self.positions.get(code)
            _force_mkt = getattr(_pos_ref, '_force_market_sell', False) if _pos_ref else False
            if "STOP_LOSS" in reason and _sl_lmt > 0 and not _force_mkt:
                _lmt_p  = _sl_lmt
                _ord_tp = "00"               # 지정가 손절
                self.log_trade.info(
                    f"[ORDER_LIMIT_SL] {self.cn(code)} 지정가손절={_lmt_p}원 (슬리피지방어)"
                )
            else:
                if _force_mkt and "STOP_LOSS" in reason:
                    self.log_trade.warning(
                        f"[ORDER_MARKET_SL_FALLBACK] {self.cn(code)} 지정가 미체결 후 시장가 재시도"
                    )
                    if _pos_ref:
                        _pos_ref._force_market_sell = False  # 플래그 초기화
                _lmt_p  = 0
                _ord_tp = "03"               # 시장가 (TP/Trail 등, 또는 SL 폴백)

        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [side, screen, self.get_account(), order_type, code, qty, _lmt_p, _ord_tp, ""]
        )
        
        # 주문 접수 실패 처리
        if ret != 0:
            # 주문 차단(-308)인 경우 소프트 리셋
            if ret == -308:
                self.log_system.error("[ORDER_BLOCK] -308 주문차단 감지")
                self.soft_reset("ORDER_BLOCK")
                return False            
            
            self.ordering = False

            # BUY 주문 실패 시 포지션이 없으면 pending도 제거
            self.pending_orders.pop(code, None)
            
            if side == "BUY":
                self._pending_buy_code = None
                self._pending_buy_qty = 0
               
            self.log_trade.error(
                f"[ORDER_FAIL] 방향={side} {self.cn(code)} 수량={qty}주 사유={reason} ret={ret}" 
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
        f"[REAL_REG] 등록종목수={len(self.positions)} 종목={list(self.positions.keys())}"
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
                now_str = datetime.now().strftime('%H:%M:%S')
                self.log_system.warning(f"[1MIN_PARSE_EMPTY] {self.cn(code)} - 현재시각: {now_str}")
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
                    f"[1MIN_PARSE_SHORT] {self.cn(code)} 캔들수={len(candles)}개 (30개 미만)"
                )
                return candles

            # self.log_system.info(
            #     f"[1MIN_PARSE] {self.cn(code)} rows={len(candles)} (OHLCV)"
            # )

            return candles

        except Exception as e:
            self.log_system.error(f"[1MIN_PARSE_ERROR] {self.cn(code)} 파싱오류: {e}")
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

    def cn(self, code: str) -> str:
        """로그용: '종목명(code)' 형태로 반환. ex) '나노엔텍(039860)'"""
        return f"{self.get_stock_name(code)}({code})"

    # ==================================================
    # ATR 계산 유틸리티
    # ==================================================
    @staticmethod
    def calc_atr(candles: list, period: int = ATR_PERIOD) -> float:
        """
        완성봉 캔들 리스트(최신봉 index=0)로 ATR 계산.
        True Range = max(고가-저가, |고가-전봉종가|, |저가-전봉종가|)
        period개 TR의 단순평균을 반환. 캔들 부족 시 0.0 반환.
        """
        if len(candles) < period + 1:
            return 0.0
        # candles[0]이 가장 최근이므로 period+1개 슬라이싱
        window = candles[:period + 1]
        trs = []
        for i in range(period):
            cur  = window[i]
            prev = window[i + 1]
            tr = max(
                cur["high"] - cur["low"],
                abs(cur["high"] - prev["close"]),
                abs(cur["low"]  - prev["close"]),
            )
            trs.append(tr)
        return sum(trs) / len(trs)

    # 호가단위 보정 (한국 주식시장)
    @staticmethod
    def min_tick(price: int) -> int:
        """호가 단위 1틱 반환 (지정가 주문 계산용)"""
        if price < 2000:     return 1
        elif price < 5000:   return 5
        elif price < 20000:  return 10
        elif price < 50000:  return 50
        elif price < 200000: return 100
        elif price < 500000: return 500
        else:                return 1000

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

    # ==================================================
    # ⭐ BUG-3: 키움 잔고조회 TR (OPW00018) 기반 전일 미청산 포지션 복구
    # ==================================================
    def load_holdings_from_api(self, on_done: callable = None):
        """
        OPW00018로 실제 보유 잔고를 조회해 positions에 복구.
        프로그램 재시작 시 전일 미청산 잔고를 메모리에 재등록하기 위해 사용.

        on_done: 조회 완료 후 콜백 (main.py에서 liquidate_leftover_positions 예약용)
        """
        self._holdings_callback = on_done
        self._holdings_result   = []

        self.dynamicCall("SetInputValue(QString, QString)", "계좌번호", self.get_account())
        self.dynamicCall("SetInputValue(QString, QString)", "비밀번호", "")
        self.dynamicCall("SetInputValue(QString, QString)", "비밀번호입력매체구분", "00")
        self.dynamicCall("SetInputValue(QString, QString)", "조회구분", "1")
        self.dynamicCall("CommRqData(QString, QString, int, QString)",
                         "RQ_HOLDINGS", "OPW00018", 0, "9500")
        self.log_system.info("[HOLDINGS_TR] OPW00018 잔고조회 요청")

    def _on_receive_holdings_tr(self, rq_name, tr_code):
        """OPW00018 수신 처리 (TR 콜백 분기용)"""
        if rq_name != "RQ_HOLDINGS" or tr_code != "OPW00018":
            return

        rows = self.dynamicCall("GetRepeatCnt(QString, QString)", tr_code, rq_name)
        recovered = 0

        for i in range(rows):
            def _g(field, idx=i):
                return self.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    tr_code, rq_name, idx, field
                ).strip()

            code     = _g("종목번호").replace("A", "").strip()
            name     = _g("종목명")
            qty_str  = _g("보유수량")
            price_str = _g("매입가")

            try:
                qty   = int(qty_str.replace(",", ""))
                price = int(price_str.replace(",", ""))
            except ValueError:
                continue

            if qty <= 0 or not code:
                continue

            # 이미 positions에 있으면 건드리지 않음
            if code in self.positions:
                self.log_system.info(
                    f"[HOLDINGS_SKIP] {name}({code}) 이미 포지션 존재"
                )
                continue

            from kiwoom_api import PositionState
            import time as pytime_
            from datetime import datetime as _dt

            # ⭐ FIX: 장 중 재시작 여부에 따라 entry_ts 결정
            # - 09:05 이전 재시작: 전일 잔고 → entry_ts = 어제 (liquidate_leftover 대상)
            # - 09:05 이후 재시작: 당일 신규 매수일 가능성 → entry_ts = now()
            #   (장 중 재시작으로 복구된 포지션은 강제청산 방지)
            _now = _dt.now()
            _market_open = _now.replace(hour=9, minute=5, second=0, microsecond=0)
            if _now >= _market_open:
                _entry_ts = pytime_.time()   # 당일 포지션으로 유지
                self.log_system.warning(
                    f"[HOLDINGS_RECOVERED_INTRADAY] {name}({code}) "
                    f"장 중 재시작 복구 → 당일 포지션으로 처리 (강제청산 제외)"
                )
            else:
                _entry_ts = pytime_.time() - 86400  # 전일 잔고

            pos = PositionState(
                code        = code,
                entry_price = price,
                highest_price = price,
                total_qty   = qty,
                remain_qty  = qty,
                ordering    = False,
                selling     = False,
            )
            pos.entry_ts = _entry_ts
            pos.buy_done = True
            # ⭐ [버그1 수정] last_sell_attempt_ts를 현재 시각으로 초기화
            # 기존: 기본값 0.0 유지 → ZOMBIE 감지 시 pytime.time() - 0.0 ≈ 17.8억 초(56년) 계산
            #       → 복구 직후 즉시 ZOMBIE 판정 → 이미 청산 주문이 접수된 종목에 이중 재매도 시도
            # 수정: 복구 시각을 기록하여 ZOMBIE_STUCK_SEC(300초) 이내 오탐 방지
            pos.last_sell_attempt_ts = pytime_.time()

            self.positions[code] = pos
            self.register_real(code)
            recovered += 1

            self.log_system.warning(
                f"[HOLDINGS_RECOVERED] {name}({code}) "
                f"qty={qty} entry={price} → positions 복구완료"
            )

        self.log_system.info(
            f"[HOLDINGS_TR_DONE] 잔고조회 완료: {rows}종목 조회, {recovered}종목 복구"
        )

        # 콜백 실행 (liquidate_leftover_positions 호출 등)
        if callable(self._holdings_callback):
            self._holdings_callback()

    # 6. pending 주문 복구
    def recover_pending_orders(self):
        """
        pending 주문 복구
        """
        self.log_system.info("[PENDING_RECOVERY_START] 미체결주문 복구 시작")

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
        self.log_system.warning(f"[SOFT_RESET] 사유={reason}")

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
                "cond_name": cond_name,
                # ⭐ 당일 첫 COND_IN 시각 기록 (BREAKOUT 에너지 소진 방지)
                # 나노팀 케이스: 09:37 첫 급등 → 10:17(40분 후) 재급등 진입 → 즉시 손절
                "first_cond_in_ts": pytime.time(),
            }

            # ⭐ 버그 수정: 중복 추가 방지
            if code not in self.scan_queue:
                self.scan_queue.append(code)
            self.log_signal.info(f"[COND_IN] {self.cn(code)} cond='{cond_name}'")

        # 🔥 스캔 트리거 조건
        if (
            len(self.positions) < MAX_POSITIONS
            and not self._scan_running
            and self.scan_queue
        ):
            self._scan_running = True
            self.log_signal.info(
                f"[SCAN_TRIGGER] 조건검색결과수신 "
                f"큐={len(self.scan_queue)} 포지션={len(self.positions)}"
            )
            QTimer.singleShot(0, self._scan_next)
        else:
            self.log_signal.info(
                f"[SCAN_NO_TRIGGER] 조건검색결과수신 "
                f"큐={len(self.scan_queue)} 스캔중={self._scan_running} "
                f"포지션={len(self.positions)}"
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
                    f"[COND_IN_IGNORE] {self.cn(code)} 사유=이미보유중"
                )
                return
            # ⭐ FIX: 주문 접수 중(pending)인 종목 재진입 방지
            # RC4007 등 거절 응답이 오기 전에 2번째 COND_IN이 들어오면
            # pending_orders에서 pop되기 전이므로 여기서 차단
            if code in self.pending_orders:
                self.log_trade.info(
                    f"[COND_IN_IGNORE] {self.cn(code)} 사유=주문접수중"
                )
                return
            # ⭐ 당일 이미 거래한 종목(체결 완료 or 거절) 재진입 방지
            if code in self.traded_today:
                self.log_trade.info(
                    f"[COND_IN_IGNORE] {self.cn(code)} 사유=당일거래완료"
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
                    f"[COND_REENTRY_RESET] {self.cn(code)} 재편입리셋 "
                    f"큐={len(self.scan_queue)} 스캔중={self._scan_running}"
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
                    "cond_name": cond_name,
                    # ⭐ 당일 첫 COND_IN 시각 기록 (BREAKOUT 에너지 소진 방지)
                    "first_cond_in_ts": pytime.time(),
                }

                self.scan_queue.append(code)
                self.log_signal.info(f"[COND_IN] {self.cn(code)} cond='{cond_name}'")
                self.log_trade.info(
                    f"[CANDIDATE_ADD] {self.cn(code)} cond='{cond_name}' "
                    f"큐크기={len(self.scan_queue)} 스캔중={self._scan_running}"
                )

            # ⭐ 공통 스캔 트리거 (신규 + 재편입 모두)
            if len(self.positions) < MAX_POSITIONS and not self._scan_running and self.scan_queue:
                self._scan_running = True
                self.log_trade.info(
                    f"[SCAN_TRIGGER] 사유=실시간조건편입 포지션={len(self.positions)} "
                    f"큐={len(self.scan_queue)}"
                )
                QTimer.singleShot(0, self._scan_next)

        # =========================
        # 조건 이탈 (D)
        # =========================
        elif event_type == "D":
            self.log_signal.info(f"[COND_OUT] {self.cn(code)}")
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
                self.log_signal.info(f"[COND_OUT_MARK] {self.cn(code)}")

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
                    f"[CANDIDATE_DROP_RETRY] {self.cn(code)} 재시도={info['retry']}회 초과 삭제"
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
                    f"[CANDIDATE_DROP_TTL] {self.cn(code)} TTL={TTL}초 만료 삭제"
                )

                self.candidates.pop(code, None)
                self.scan_queue = [c for c in self.scan_queue if c != code]

                if self.current_scan_code == code:
                    self.current_scan_code = None
    # ==================================================
    # 메시지 수신
    # ==================================================
    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        self.log_system.info(f"[RECV_MSG] 화면={screen_no} 요청={rqname} TR={trcode} 메시지={msg}")

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
            f"[ORDER_REJECT] 화면={screen_no} 요청={rqname} 메시지={msg}"
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
                        f"[REJECT_CLEANUP] {self.cn(code)} 방향={rqname} 메시지={msg}"
                    )
                    self.pending_orders.pop(code, None)
                    if rqname == "BUY":
                        self.ordering = False
                        self._pending_buy_code = None
                        self._pending_buy_qty = 0
                        # ⭐ FIX: BUY 거절 시 candidates에서도 제거 + 당일 재진입 방지
                        # 미체결 REJECT인데 candidates에 남아있으면 즉시 재스캔→재주문 반복
                        if code in self.candidates:
                            self.candidates.pop(code, None)
                            self.scan_queue = [c for c in self.scan_queue if c != code]
                            self.log_system.warning(
                                f"[REJECT_CLEANUP] {self.cn(code)} candidates 제거 완료"
                            )
                        self.traded_today.add(code)  # 당일 동일 종목 재진입 방지
                        self._resume_scan_if_possible()
                    elif rqname == "SELL":
                        pos = self.positions.get(code)
                        if pos:
                            # ⭐ [800033] = 이미 접수된 매도 주문이 있어서 부족
                            # → selling=False로 풀면 안 됨 (무한 재시도 루프 발생)
                            # → [800033] 이외의 SELL 거절만 selling 해제
                            if "800033" not in msg and "매도가능수량" not in msg:
                                pos.selling = False
                            if "800033" in msg or "매도가능수량" in msg:
                                # 재시도 횟수 초과 시 포기 (SELL_STUCK이 대신 처리)
                                retry_cnt = pos.sell_reject_retries = getattr(pos, 'sell_reject_retries', 0) + 1
                                if retry_cnt > 5:
                                    self.log_system.error(
                                        f"[SELL_REJECT_ABORT] {self.cn(code)} "
                                        f"[800033] 재시도 {retry_cnt}회 초과 → 포기 (SELL_STUCK 대기)"
                                    )
                                    # ⭐ [피제이메탈 버그 수정] 재시도 한도 초과 시
                                    # pending 제거 + selling 리셋 → SELL_STUCK 대기 없이 즉시 재매도
                                    # 기존: 포기 후 9000초 이상 ZOMBIE로 방치됨 (로그: ZOMBIE 9232초)
                                    self.pending_orders.pop(code, None)
                                    pos.selling   = False
                                    pos.sl_ordered = False
                                    pos.sell_reject_retries = 0
                                    _qty_force  = pos.remain_qty
                                    _rsn_force  = pend.get("reason", "STOP_LOSS") if pend else "STOP_LOSS"
                                    self.log_system.warning(
                                        f"[SELL_REJECT_FORCE] {self.cn(code)} "
                                        f"pending 제거 후 2초 뒤 강제 재매도 "
                                        f"qty={_qty_force} reason={_rsn_force}"
                                    )
                                    QTimer.singleShot(
                                        2000,
                                        lambda c=code, q=_qty_force, r=_rsn_force: self._retry_sell_after_reject(c, q, r)
                                    )
                                else:
                                    sell_qty = pos.remain_qty
                                    sell_reason = pend.get("reason", "STOP_LOSS") if pend else "STOP_LOSS"
                                    self.log_system.warning(
                                        f"[SELL_REJECT_RETRY] {self.cn(code)} "
                                        f"[800033] 매도가능수량 부족 → 4초 후 재시도 "
                                        f"qty={sell_qty} reason={sell_reason} ({retry_cnt}회차)"
                                    )
                                    # ⭐ FIX: 1초→4초 (내부 RETRY_COOLDOWN=3초를 넘겨야 실제 재시도됨)
                                    # 기존 1초 타이머는 쿨다운(3초)에 항상 막혀 SKIP → ZOMBIE 5분 대기만 남음
                                    QTimer.singleShot(
                                        4000,
                                        lambda c=code, q=sell_qty, r=sell_reason: self._retry_sell_after_reject(c, q, r)
                                    )
                        self.ordering = bool(self.pending_orders)
                    break  # 동시 BUY 차단 로직상 1개만 있을 수 있음

    # ==================================================
    # ⭐ BUG-2: SELL REJECT [800033] 후 재매도 시도
    # ==================================================
    def _retry_sell_after_reject(self, code: str, qty: int, reason: str):
        """
        [800033] 매도가능수량 부족 REJECT 후 1초 뒤 재호출.
        - 포지션이 살아있으면 현재 remain_qty 기준으로 재매도
        - 이미 매도 완료됐으면 스킵

        ⭐ FIX LS네트웍스 버그:
          기존: pos.selling=True이면 SKIP → SELL_STUCK이 selling=False로 리셋해도
                뒤늦게 도달한 REJECT 이벤트가 다시 retry를 호출할 때는 이미
                다른 경로에서 selling=True가 된 상태일 수 있어 SKIP됨 (타이밍 버그)
          수정: selling 체크 대신 last_sell_attempt_ts 기준 쿨다운으로 중복 방지.
                selling=True여도 쿨다운(3초)만 지났으면 강제 재시도 허용.
                또한 _force_market_sell 플래그를 여기서도 활용하여 시장가 전환.
        """
        pos = self.positions.get(code)
        if not pos or pos.remain_qty <= 0:
            self.log_system.info(f"[SELL_REJECT_RETRY_SKIP] {self.cn(code)} 이미 청산됨")
            return

        # ⭐ selling=True 체크 제거 → last_sell_attempt_ts 기준 쿨다운으로 대체
        # 이유: SELL_STUCK이 selling=False 리셋 후 새 주문을 냈는데,
        #       그 사이 pending에 남아있던 REJECT 이벤트가 뒤늦게 retry를 호출하면
        #       selling=True 상태가 되어 정상 재시도가 차단됨 (LS네트웍스 케이스)
        now_ts = pytime.time()
        RETRY_COOLDOWN = 3.0  # 마지막 매도 시도 후 3초 이내 중복 재시도 방지
        if now_ts - pos.last_sell_attempt_ts < RETRY_COOLDOWN:
            self.log_system.info(
                f"[SELL_REJECT_RETRY_SKIP] {self.cn(code)} 쿨다운 중 "
                f"(마지막시도={now_ts - pos.last_sell_attempt_ts:.1f}초전)"
            )
            return

        # ⭐ FIX: qty 파라미터를 존중 (TP1 등 부분매도 재시도 시 전체 수량으로 교체 방지)
        # 기존: pos.remain_qty로 무조건 교체 → TP1(273주) 재시도가 684주(전체)로 발송됨
        #       → TP1 후 남아야 할 411주가 즉시 시장가로 팔려버려 포지션 이월 발생
        # 수정: min(qty, remain_qty) → 호출자가 요청한 수량 이하로만 재시도
        actual_qty = min(qty, pos.remain_qty) if qty > 0 else pos.remain_qty

        # ⭐ FIX: SELL_STUCK에서 _force_market_sell=True 설정된 경우 시장가로 강제 전환
        if getattr(pos, '_force_market_sell', False):
            self.log_system.warning(
                f"[SELL_REJECT_RETRY_MARKET] {self.cn(code)} "
                f"_force_market_sell=True → 시장가로 재시도 qty={actual_qty}"
            )
            pos._force_market_sell = False  # 플래그 초기화
        else:
            self.log_trade.info(
                f"[SELL_REJECT_RETRY] {self.cn(code)} "
                f"재매도 qty={actual_qty} reason={reason}"
            )

        # selling 강제 해제 후 재시도 (쿨다운으로 이미 중복 방지됨)
        pos.selling = False
        ok = self.send_market_order("SELL", code, actual_qty, reason)
        if ok:
            pos.selling = True
            pos.last_sell_attempt_ts = pytime.time()
        else:
            pos.selling = False
            # 재시도도 실패 시 3초 후 한 번 더
            self.log_system.warning(
                f"[SELL_REJECT_RETRY_FAIL] {self.cn(code)} 재매도 실패 → 3초 후 마지막 시도"
            )
            QTimer.singleShot(
                3000,
                lambda c=code, q=actual_qty, r=reason: self._final_sell_attempt(c, q, r)
            )

    def _final_sell_attempt(self, code: str, qty: int, reason: str):
        """재매도 2차 실패 후 마지막 시도. 이후에도 실패하면 로그만 남김."""
        pos = self.positions.get(code)
        if not pos or pos.remain_qty <= 0 or pos.selling:
            return
        self.log_trade.warning(
            f"[FINAL_SELL_ATTEMPT] {self.cn(code)} qty={pos.remain_qty} reason={reason}"
        )
        ok = self.send_market_order("SELL", code, pos.remain_qty, reason)
        if ok:
            pos.selling = True
            pos.last_sell_attempt_ts = pytime.time()
        else:
            self.log_system.error(
                f"[FINAL_SELL_FAIL] {self.cn(code)} 마지막 매도 시도 실패 — 수동 확인 필요"
            )

    # ==================================================
    # ⭐ BUG-3: 장 시작 시 전일 미청산 잔고 자동 청산
    # ==================================================
    def liquidate_leftover_positions(self):
        """
        전일 미청산 잔고 장 시작 직후 자동 청산.
        load_holdings_from_api() 완료 콜백으로 호출됨.
        - entry_ts < 오늘 09:00인 포지션만 대상 (당일 신규진입 제외)
        """
        today_open_ts = datetime.now().replace(
            hour=9, minute=0, second=0, microsecond=0
        ).timestamp()

        leftover = [
            (code, pos) for code, pos in self.positions.items()
            if pos.remain_qty > 0
            and not pos.selling
            and pos.entry_ts < today_open_ts  # 오늘 장 시작 이전 잔고
        ]

        if not leftover:
            self.log_system.info("[LEFTOVER_CHECK] 전일 미청산 잔고 없음")
            return

        self.log_system.warning(
            f"[LEFTOVER_LIQUIDATION_START] 전일 미청산 포지션 {len(leftover)}개: "
            f"{[c for c, _ in leftover]}"
        )

        for code, pos in leftover:
            self.log_trade.warning(
                f"[LEFTOVER_SELL] {self.cn(code)} "
                f"전일잔고 qty={pos.remain_qty} entry={pos.entry_price} "
                f"entry_ts={datetime.fromtimestamp(pos.entry_ts).strftime('%Y-%m-%d %H:%M:%S')}"
            )

            try:
                from discord_notify import notify_force_liquidation
                notify_force_liquidation(
                    code=code,
                    name=self.get_stock_name(code),
                    qty=pos.remain_qty,
                    entry_price=pos.entry_price
                )
            except Exception as e:
                import traceback
                self.log_system.error(
                    f"[DISCORD_FAIL] 전일잔고청산알림: {e}\n{traceback.format_exc()}"
                )

            ok = self.send_market_order(
                "SELL", code, pos.remain_qty, "LEFTOVER_LIQUIDATION"
            )
            if ok:
                pos.selling = True
                self.log_system.info(f"[LEFTOVER_SELL_OK] {self.cn(code)} 청산주문 접수")
            else:
                self.log_system.error(
                    f"[LEFTOVER_SELL_FAIL] {self.cn(code)} 청산주문 실패 → 수동 확인 필요"
                )

    # ==================================================
    # 14:50 강제 전량 청산
    # ==================================================                    
    def force_liquidation_all(self):
        """
        14:50 강제 전량 청산

        ⭐ FIX LS네트웍스 버그:
          기존: pos.selling=True이면 스킵 → SELL_STUCK 후 selling이 True로 남은
                좀비 포지션이 강제청산에서도 제외되어 미청산으로 하루 마감
          수정: selling=True여도 last_sell_attempt_ts 기준으로 일정 시간(30초) 이상
                경과했으면 강제청산 대상에 포함. selling 강제 리셋 후 재시도.
        """
        self.log_system.warning("[FORCE_LIQUIDATION_START] 강제청산 시작")
        FORCE_SELLING_TIMEOUT = 60.0  # selling=True여도 60초 경과 시 강제 재시도

        for code, pos in list(self.positions.items()):

            if pos.remain_qty <= 0:
                continue

            now_ts = pytime.time()

            # ⭐ selling=True인 좀비 포지션 처리
            if pos.selling:
                elapsed = now_ts - pos.last_sell_attempt_ts
                if elapsed < FORCE_SELLING_TIMEOUT:
                    # 아직 진행 중인 정상 매도 주문 → 기다림
                    self.log_system.info(
                        f"[FORCE_LIQUIDATION_WAIT] {self.cn(code)} "
                        f"매도진행중 {elapsed:.0f}초 경과 → 스킵"
                    )
                    continue
                else:
                    # selling=True지만 30초 이상 경과 = 좀비 포지션
                    self.log_system.warning(
                        f"[FORCE_LIQUIDATION_ZOMBIE] {self.cn(code)} "
                        f"selling=True 이나 {elapsed:.0f}초 경과 → 강제 리셋 후 재시도"
                    )
                    pos.selling = False
                    pos.sl_ordered = False  # ⭐ [버그2] 강제청산 허용을 위해 sl_ordered 리셋
                    self.pending_orders.pop(code, None)  # 스택된 pending 제거

            self.log_trade.warning(
                f"[FORCE_SELL] {self.cn(code)} qty={pos.remain_qty}"
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
                import traceback
                self.log_system.error(
                    f"[DISCORD_FAIL] 강제청산알림: {e}\n{traceback.format_exc()}"
                )


            # 시장가 전량 매도
            ok = self.send_market_order(
                "SELL",
                code,
                pos.remain_qty,
                "FORCE_LIQUIDATION"
            )

            if ok:
                pos.selling = True
            else:
                # ⭐ FIX 덕양에너젠/액스비스 버그: ret=-300 등 강제청산 실패 시
                # 특수코드(5자리 00010 등)나 종목 이상으로 주문 거절될 수 있음
                # → positions에서 직접 제거하여 좀비 포지션 방지
                self.log_system.error(
                    f"[FORCE_LIQUIDATION_FAIL] {self.cn(code)} 강제청산 주문 실패 "
                    f"→ 포지션 강제 제거 (실제 잔고 확인 필요)"
                )
                self.positions.pop(code, None)
                self.pending_orders.pop(code, None)