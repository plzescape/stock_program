# 키움 REST API 자동매매 엔진
# - 다중 포지션 (최대 MAX_POSITIONS)
# - PositionState 기반 관리
# - 스캔 안전 재개/중단
# - 포지션별 손절 / 익절 / 트레일링
#
# [통신] kiwoom_rest.py (REST + 웹소켓). OCX 는 쓰지 않는다.
# [스레드] 웹소켓 수신·REST 조회는 별도 스레드에서 돌고, 결과는 전부
#          _MainThreadBridge 로 Qt 메인 스레드에 넘겨 처리한다.
#          → 매매 상태(positions / pending_orders)는 OCX 시절처럼
#            한 스레드에서만 바뀌므로 락이 필요 없다.

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time
from PyQt5.QtCore import QEventLoop, QObject, QTimer, Qt, pyqtSignal
from collections import deque
import time as pytime
import traceback

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
    MINI_TRAIL_TRIGGER, MINI_TRAIL_GAP, MINI_TRAIL_GAP_OPEN,
    KIWOOM_EXCHANGE, REST_RATE_PER_SEC, REST_RATE_BURST, REST_TIMEOUT_SEC, WS_SILENCE_RECONNECT_SEC
)
from kiwoom_rest import (
    KiwoomRestClient, KiwoomWebSocket, KiwoomError, KiwoomRateLimitError, KiwoomTransportError,
    load_app_keys, norm_code, to_int,
)
from logger_util import setup_logger
from strategy import is_market_time, is_entry_candidate, is_entry_candidate_VER2, get_entry_signal_data, is_pullback_entry, get_pullback_signal_data, is_flag_entry, get_flag_signal_data, is_no_surge_stock, get_surge_candle_close


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
    tp1_order_ts: float = 0.0     # TP1 주문 전송 시각
                                  #   체결 전에는 tp1_done_ts 가 0이라 경과시간이
                                  #   999초로 계산돼 본절보호 유예가 통째로 무시됐다
                                  #   (261021 푸른로보틱스: TP1 주문 9초 뒤 전량 청산)
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


class _MainThreadBridge(QObject):
    """
    다른 스레드(웹소켓 수신 / REST 조회)에서 Qt 메인 스레드로 함수 호출을 넘긴다.
    OCX 이벤트가 메인 스레드로 들어오던 것과 같은 실행 모델을 유지하기 위함.
    """
    _sig = pyqtSignal(object)

    def __init__(self, log):
        super().__init__()
        self._log = log
        self._sig.connect(self._run, Qt.QueuedConnection)

    def post(self, fn, *args):
        self._sig.emit((fn, args))

    def _run(self, job):
        fn, args = job
        try:
            fn(*args)
        except Exception as e:
            # 슬롯에서 예외가 새면 PyQt5 가 프로세스를 죽인다 → 로그만 남기고 계속
            self._log.error(f"[MAIN_THREAD_ERROR] {getattr(fn, '__name__', fn)}: {e}\n"
                            f"{traceback.format_exc()}")


class KiwoomAPI(QObject):
    def __init__(self):
        super().__init__()
        # ----- 로그인 상태 -----
        self.auto_trade_enabled = False

        # ---- 로거 ----
        self.log_system = setup_logger("system", "system.log")
        self.log_trade = setup_logger("trade", "trade.log")
        self.log_signal = setup_logger("signal", "signal.log")

        # ---- REST / 웹소켓 ----
        appkey, secretkey = load_app_keys(IS_REAL)
        self.rest = KiwoomRestClient(
            appkey, secretkey, IS_REAL,
            rate_per_sec=REST_RATE_PER_SEC, burst=REST_RATE_BURST,
            timeout=REST_TIMEOUT_SEC, exchange=KIWOOM_EXCHANGE, log=self.log_system,
        )
        self._bridge = _MainThreadBridge(self.log_system)
        self.ws = KiwoomWebSocket(
            self.rest,
            on_message=lambda d: self._bridge.post(self._on_ws_message, d),
            on_state=lambda s, m: self._bridge.post(self._on_ws_state, s, m),
            log=self.log_system,
            silence_sec=WS_SILENCE_RECONNECT_SEC,
        )
        # 조회 TR 은 메인 스레드를 막지 않도록 워커에서 실행
        self._rest_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kiwoom-rest")
        self._ws_wait_trnm = None       # login()/load_conditions() 가 기다리는 응답
        self._ws_wait_hit = False
        self._ws_wait_loop = None
        self._ws_disconnected_ts = 0.0
        self._stock_names: dict[str, str] = {}     # 종목코드 → 종목명 (ka10099 캐시)
        self._cond_seq_to_name: dict[str, str] = {}
        self._cond_waiting: dict[str, float] = {}    # CNSRREQ 응답 대기 seq → 요청 시각
        self._candle_req_seq = 0        # 늦게 도착한 분봉 응답 무시용
        self._candle_rows: list = []
        self._holdings_attempt = 0

        # ---- 연구용 체결/호가 녹화 ----
        self.recorder = None
        self._rec_codes: dict[str, float] = {}     # 녹화 중 종목 → 마지막 편입 시각 (삽입순)
        try:
            from config import TICK_RECORD_ENABLED
            if TICK_RECORD_ENABLED:
                from tick_recorder import TickRecorder
                self.recorder = TickRecorder(log=self.log_system)
        except Exception as e:
            self.log_system.warning(f"[TICK_REC_DISABLED] {e}")

        # ===== 미체결 BUY 취소 감시 타이머 =====
        self._pending_cancel_timer = QTimer()
        self._pending_cancel_timer.setInterval(500)  # 0.5초마다 체크
        self._pending_cancel_timer.timeout.connect(self.check_pending_buy_cancel)
        self._pending_cancel_timer.start()

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
        # (REST 는 화면번호가 없다. 조건식은 seq 로 구분)
        self._cond_rr_idx        = 0      # 다음 호출할 조건식 인덱스
        # REST 분봉 응답 대기 한도. 호출 제한(429) 재시도 여유를 두어 OCX(2초)보다 길게
        self.TR_TIMEOUT_MS = 4000

        # ===== 스캔 타임스탬프 =====
        self.last_scan_times = {}  # { '005930': 1700000.123 } 형태

        # ===== TR 요청 간격 관리 =====        
        self.TR_REQ_INTERVAL = 1
        self._last_tr_time = 0

        # ===== 진입 신호 데이터 (디스코드 알림용) =====
        # code -> { vol_ratio, trend, breakout, candle_strength }
        self._entry_signals: dict[str, dict] = {}

        # ===== 장 마감 정리 상태 (force_liquidation_all) =====
        # closing=True 동안은 사유가 CLOSE_ 로 시작하는 정리 주문만 허용
        self.closing = False
        self._close_phase = None          # "CANCEL" → "VERIFY" → "DONE"
        self._close_started_ts = 0.0
        self._close_waiting = None        # (tr_code, seq) 응답 대기 중인 TR
        self._close_wait_seq = 0
        self._close_failed: set[str] = set()         # 정리 매도 3회 실패 종목
        self._close_resting_sells: set[str] = set()  # 재확인 시 체결 대기 중인 정리 매도

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

        cond_seq = self.condition_map[cond_to_run]

        # 조건검색 + 실시간 등록 (결과는 _on_ws_message → CNSRREQ 로 들어옴)
        ok = self.ws.condition_request(cond_seq, realtime=True)

        next_idx = self._cond_rr_idx % n
        if ok:
            self.log_system.info(
                f"[CONDITION] 조건검색 요청 성공: '{cond_to_run}' "
                f"(seq={cond_seq}, "
                f"다음순번={names[next_idx] if n>1 else '-'})"
            )
            self._cond_waiting[str(cond_seq)] = pytime.time()
            QTimer.singleShot(self.COND_REPLY_TIMEOUT_MS,
                              lambda s=str(cond_seq), nm=cond_to_run: self._check_cond_reply(s, nm))
        else:
            self.log_system.error(
                f"[CONDITION] 조건검색 요청 실패: '{cond_to_run}' (웹소켓 미접속 - 재접속 시 자동 재등록)"
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
        """접근토큰 발급 → 종목명 캐시 → 웹소켓 로그인 (OCX CommConnect 대체)"""
        self.rest.issue_token()      # 실패하면 예외 → main 에서 종료 (앱키/IP 등록 확인)
        self.log_system.info(
            f"[LOGIN] 접근토큰 발급 완료 ({'실전' if IS_REAL else '모의'} {self.rest.base_url})"
        )
        self.load_stock_names()

        # 주문체결(00)은 종목 구분 없이 계좌 전체를 받는다
        self.ws.register("1", ["00"], [""])
        self.ws.start()
        if self._wait_ws("LOGIN", 15000):
            self.log_system.info("[LOGIN] 웹소켓 로그인 성공")
        else:
            self.log_system.error("[LOGIN] 웹소켓 로그인 실패 (15초) - 백그라운드에서 계속 재접속")

    def _wait_ws(self, trnm: str, timeout_ms: int) -> bool:
        """웹소켓 응답(trnm)이 올 때까지 이벤트 루프를 돌며 기다린다 (OCX QEventLoop 패턴)"""
        self._ws_wait_trnm = trnm
        self._ws_wait_hit = False
        loop = QEventLoop()
        self._ws_wait_loop = loop
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec_()
        self._ws_wait_loop = None
        self._ws_wait_trnm = None
        return self._ws_wait_hit

    def _ws_wait_done(self, trnm: str):
        if self._ws_wait_trnm == trnm and self._ws_wait_loop is not None:
            self._ws_wait_hit = True
            self._ws_wait_loop.quit()

    def load_conditions(self):
        """조건검색식 목록 조회 (OCX GetConditionLoad 대체)"""
        if not self.ws.condition_list() or not self._wait_ws("CNSRLST", 10000):
            self.log_system.error("[CONDITION] 조건검색식 목록 수신 실패 - 재접속 후 자동 재시도")

    def _on_receive_condition_ver(self, data: dict):
        """CNSRLST 응답: data = [[seq, name], ...]"""
        if to_int(data.get("return_code", 0)) != 0:
            self.log_system.error(f"[CONDITION] 목록 조회 실패: {data.get('return_msg')}")
            return
        for item in data.get("data") or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                seq, name = str(item[0]).strip(), str(item[1]).strip()
            elif isinstance(item, dict) and item.get("seq") is not None:
                seq, name = str(item.get("seq")).strip(), str(item.get("name", "")).strip()
            else:
                self.log_system.warning(f"[COND_PARSE_SKIP] 형식 오류: {item}")
                continue
            if not name:
                continue
            self.condition_map[name] = seq
            self._cond_seq_to_name[seq] = name
        self.log_system.info(f"[CONDITION] 조건식 {len(self.condition_map)}개 로드: {self.condition_map}")
        self._ws_wait_done("CNSRLST")

    # ==================================================
    # 웹소켓 수신 (메인 스레드에서 실행됨)
    # ==================================================
    def _on_ws_state(self, state: str, detail: str):
        if state == "LOGIN_OK":
            if self._ws_disconnected_ts:
                down = pytime.time() - self._ws_disconnected_ts
                self.log_system.warning(f"[WS_RECOVERED] 웹소켓 재접속 완료 (끊김 {down:.0f}초)")
                self._ws_disconnected_ts = 0.0
            # 첫 로그인 때 웹소켓이 늦게 붙어 조건식 목록을 못 받았으면 지금 받는다
            if not self.condition_map and self._ws_wait_trnm != "LOGIN":
                self.ws.condition_list()
            self._ws_wait_done("LOGIN")
        elif state == "LOGIN_FAIL":
            self.log_system.error(f"[WS_LOGIN_FAIL] {detail}")
        elif state == "DISCONNECTED":
            self._ws_disconnected_ts = pytime.time()
            self.log_system.error(f"[WS_DISCONNECTED] {detail} - 체결/시세 수신 중단, 자동 재접속 중")
            if self.positions:
                try:
                    from discord_notify import notify_system
                    notify_system(f"키움 웹소켓 끊김 (보유 {len(self.positions)}종목) - 자동 재접속 중",
                                  level="WARNING")
                except Exception as e:
                    self.log_system.error(f"[DISCORD_FAIL] 웹소켓끊김알림: {e}")

    def _on_ws_message(self, data: dict):
        trnm = data.get("trnm")

        if trnm == "REAL":
            for item in data.get("data") or []:
                if not isinstance(item, dict):
                    continue
                rtype = item.get("type")
                values = item.get("values") or {}
                if rtype == "00":                       # 주문체결 (OnReceiveChejanData)
                    self._on_receive_chejan_data(values)
                elif rtype == "0B":                     # 주식체결 (OnReceiveRealData)
                    rcode = norm_code(item.get("item"))
                    if self.recorder and rcode in self._rec_codes:
                        self.recorder.record_trade(rcode, values)
                    self._on_receive_real_data(rcode, values)
                elif rtype == "0D":                     # 호가잔량 (녹화 전용)
                    rcode = norm_code(item.get("item"))
                    if self.recorder and rcode in self._rec_codes:
                        self.recorder.record_book(rcode, values)
                elif rtype == "02":                     # 조건검색 실시간 편입/이탈
                    seq = str(values.get("841", "")).strip()
                    code = norm_code(values.get("9001") or item.get("item"))
                    event = str(values.get("843", "")).strip()
                    name = self._cond_seq_to_name.get(seq, seq)
                    if code and event in ("I", "D"):
                        self._on_receive_real_condition(code, event, name, seq)
            return

        if trnm == "CNSRREQ":
            self._on_condition_result(data)
        elif trnm == "CNSRLST":
            self._on_receive_condition_ver(data)
        elif trnm in ("REG", "REMOVE", "CNSRCLR"):
            if to_int(data.get("return_code", 0)) != 0:
                self.log_system.error(f"[WS_{trnm}_FAIL] {data.get('return_msg')}")
        elif trnm != "LOGIN":
            self.log_system.info(f"[WS_MSG] {str(data)[:300]}")

    COND_REPLY_TIMEOUT_MS = 15000

    def _check_cond_reply(self, seq: str, name: str):
        """CNSRREQ 응답이 안 오면 세션의 조건식 목록이 없는 상태일 수 있다 → CNSRLST 후 재요청"""
        if self._cond_waiting.pop(seq, None) is None:
            return
        self.log_system.error(f"[CONDITION_NO_REPLY] '{name}' (seq={seq}) 응답 없음 → 조건식 목록 재조회 후 재요청")
        self.ws.condition_list()
        QTimer.singleShot(1500, lambda: self.ws.condition_request(seq, realtime=True))

    def _on_condition_result(self, data: dict):
        """CNSRREQ 응답 → OCX OnReceiveTrCondition 과 같은 형태로 넘긴다"""
        seq = str(data.get("seq", "")).strip()
        self._cond_waiting.pop(seq, None)
        name = self._cond_seq_to_name.get(seq, seq)
        if to_int(data.get("return_code", 0)) != 0:
            self.log_system.error(f"[CONDITION] '{name}' 조건검색 실패: {data.get('return_msg')}")
            return
        codes = []
        for row in data.get("data") or []:
            if isinstance(row, dict):
                raw = row.get("jmcode") or row.get("9001") or row.get("stk_cd") or ""
            else:
                raw = row
            c = norm_code(raw)
            if c and c not in codes:
                codes.append(c)
        self.log_system.info(f"[CONDITION_RESULT] '{name}' {len(codes)}종목")
        if str(data.get("cont_yn", "N")).upper() == "Y":
            self.log_system.warning(f"[CONDITION] '{name}' 결과가 한 페이지를 넘음 - 첫 페이지만 사용")
        self._on_receive_tr_condition("", ";".join(codes), name, seq, 0)

    # ==================================================
    # REST 비동기 조회 / 종목명
    # ==================================================
    def _rest_async(self, fn, on_done, on_error):
        """fn() 을 워커 스레드에서 실행하고 결과를 메인 스레드의 on_done / on_error 로 넘긴다"""
        def job():
            try:
                res = fn()
            except Exception as e:
                self._bridge.post(on_error, e)
                return
            self._bridge.post(on_done, res)
        self._rest_pool.submit(job)

    def _record_watch(self, code: str, cond_name: str):
        """조건검색 편입 종목을 녹화 대상으로 등록. 실패해도 매매에는 영향 없음."""
        if not self.recorder:
            return
        try:
            from config import TICK_RECORD_MAX_CODES, TICK_RECORD_ORDERBOOK
            self.recorder.record_event(code, "COND_IN", cond_name)
            if code in self._rec_codes:
                self._rec_codes.pop(code)
                self._rec_codes[code] = pytime.time()     # 최근 편입으로 순서 갱신
                return
            # 0B 는 포지션 감시와 같은 그룹 2 를 쓴다 (그룹을 나누면 같은 틱이 두 번 올 수 있음)
            self.ws.register("2", ["0B"], [code])
            if TICK_RECORD_ORDERBOOK:
                self.ws.register("3", ["0D"], [code])
            self._rec_codes[code] = pytime.time()

            # 상한 초과 → 보유/주문 중이 아닌 가장 오래된 종목부터 해지
            while len(self._rec_codes) > TICK_RECORD_MAX_CODES:
                old = next((c for c in self._rec_codes
                            if c not in self.positions and c not in self.pending_orders), None)
                if old is None:
                    break
                self._rec_codes.pop(old)
                self.ws.unregister("2", ["0B"], [old])
                if TICK_RECORD_ORDERBOOK:
                    self.ws.unregister("3", ["0D"], [old])
                self.recorder.record_event(old, "REC_STOP", "max_codes")
        except Exception as e:
            self.log_system.warning(f"[TICK_REC_WATCH_FAIL] {self.cn(code)} {e}")

    def load_stock_names(self):
        """ka10099 로 코스피/코스닥 종목명을 한 번에 받아 캐시 (OCX GetMasterCodeName 대체)"""
        for market, label in (("0", "코스피"), ("10", "코스닥")):
            try:
                rows = self.rest.stock_list(market)
            except Exception as e:
                self.log_system.warning(f"[STOCK_NAMES] {label} 종목명 조회 실패: {e} (로그에 코드만 표시)")
                continue
            for r in rows:
                c = norm_code(r.get("code"))
                if c:
                    self._stock_names[c] = str(r.get("name", "")).strip()
        self.log_system.info(f"[STOCK_NAMES] 종목명 {len(self._stock_names)}개 캐시")

    # ==================================================
    # Scan control
    # ==================================================
    def _scan_next(self):
        if self.closing:
            self._scan_running = False    # 장 마감 정리 중 → 스캔 중단
            return
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
        self.request_candle(code)
        
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
    def request_candle(self, code):
        """ka10080 분봉 조회 (워커 스레드). 응답은 _on_candle_response 로 들어온다."""
        from config import CANDLE_INTERVAL_MIN
        self._candle_req_seq += 1
        seq = self._candle_req_seq
        self._rest_async(
            lambda: self.rest.minute_chart(code, str(CANDLE_INTERVAL_MIN))[0],
            lambda data, c=code, s=seq: self._on_candle_response(c, s, data),
            lambda err, c=code, s=seq: self._on_candle_error(c, s, err),
        )

    def _on_candle_response(self, code, seq, data):
        # 타임아웃 후 늦게 온 응답 / 다른 종목 응답은 버린다
        if seq != self._candle_req_seq or self.current_scan_code != code:
            self.log_system.debug(f"[CANDLE_STALE] {self.cn(code)} 늦은 응답 무시")
            return
        self._candle_rows = data.get("stk_min_pole_chart_qry") or []
        self._on_receive_tr_data("RQ_CANDLE", "KA10080")

    def _on_candle_error(self, code, seq, err):
        if seq != self._candle_req_seq or self.current_scan_code != code:
            return
        self.log_system.warning(f"[CANDLE_TR_FAIL] {self.cn(code)} {err}")
        if hasattr(self, "_tr_timeout_timer"):
            self._tr_timeout_timer.stop()
        # 일시 오류일 수 있으니 큐 뒤로 돌려 쿨타임 후 재스캔
        if code in self.candidates and code not in self.scan_queue:
            self.scan_queue.append(code)
            self.last_scan_times[code] = pytime.time()
        self._finish_tr(delay=True)

    def _on_receive_tr_data(self, rq_name, tr_code):
        if not self.tr_inflight:
            return
        if tr_code != "KA10080" or rq_name != "RQ_CANDLE":
            return
        
        if hasattr(self, "_tr_timeout_timer"):
            self._tr_timeout_timer.stop()
        
        code = self.current_scan_code
        candles = self.parse_candle(code)

        # ── 백테스트용 분봉 적재 ──────────────────────────────────────
        # 이미 받은 데이터를 저장만 한다. TR 추가 요청 없음 → 키움 제한 무영향.
        # 어떤 실패도 매매를 막지 않도록 전부 삼킨다.
        try:
            from config import RECORD_CANDLES
            if RECORD_CANDLES and candles:
                import candle_recorder
                from config import CANDLE_INTERVAL_MIN as _rec_cim
                candle_recorder.record(code, self.get_stock_name(code),
                                       candles, _rec_cim)
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────

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
            elif is_flag_entry(completed_candles, self.log_signal, code,
                               live_candle=candles[0] if candles else None):
                entry_type = "FLAG"
            elif _breakout_stale:
                self.log_signal.info(
                    f"[BREAKOUT_STALE_SKIP] {self.cn(code)} "
                    f"첫COND_IN 경과={_elapsed:.0f}초 > {_br_timeout}초 → BREAKOUT 차단 "
                    f"(에너지 소진 가능성)"
                )
                # ⭐ FIX: traded_today 미추가 버그 (260710 무한루프)
                # 기존: STALE_SKIP 후 traded_today 미추가 → 동일 종목이 COND_IN될 때마다
                #        큐 재추가 → 스캔 → STALE_SKIP 무한반복 (260710: 태웅 등 8079건 발생)
                # 수정: NO_SURGE_SKIP과 동일하게 traded_today에 추가하여 당일 재스캔 차단
                self.traded_today.add(code)
            elif is_entry_candidate_VER2(completed_candles, self.log_signal, code):
                entry_type = "BREAKOUT"
            else:
                # ⭐ FIX: 999봉 종목(급등봉 미발견) 당일거래완료 마킹
                # 키스트론 케이스(260618): surge_candle_age=999 종목이 하루 수십~수백 회 큐 재편입
                # 급등봉이 없는 종목은 당일 재스캔 가치 없음 → traded_today에 추가
                if is_no_surge_stock(completed_candles):
                    self.log_signal.info(
                        f"[NO_SURGE_SKIP] {self.cn(code)} 20봉 내 급등봉 없음 → 당일거래완료"
                    )
                    self.traded_today.add(code)

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

                # ── ATR 계산 ──
                from config import CANDLE_INTERVAL_MIN as _ATR_CIM
                atr_val = self.calc_atr(completed_candles, ATR_PERIOD)
                self.log_trade.info(
                    f"[ATR_CALC] {self.cn(code)} ATR={atr_val:.1f}원 "
                    f"({ATR_PERIOD}봉/{ATR_PERIOD * _ATR_CIM}분 기준, SL배수={ATR_SL_MULT}, TP배수={ATR_TP_MULT})"
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

                # ── BREAKOUT 급등봉 이격 체크 (고점 추격 / 에너지소진 방지) ──
                # 완성봉 데이터 → 주문 사이 1~3초 지연 동안 가격 급등 후 체결되는 케이스 방지
                if entry_type == "BREAKOUT":
                    _surge_close = get_surge_candle_close(completed_candles)
                    if _surge_close > 0 and cur_price > 0:
                        _ratio = cur_price / _surge_close
                        if _ratio > 1.02:
                            self.log_signal.info(
                                f"[PUMP_SKIP] {self.cn(code)} 고점 추격 차단 "
                                f"현재가={cur_price} 급등봉종가={_surge_close} "
                                f"이격=+{(_ratio-1)*100:.1f}% (>+2%)"
                            )
                            self.traded_today.add(code)
                            info["state"] = "DONE"
                            self._finish_tr(delay=True)
                            return
                        if _ratio < 0.98:
                            self.log_signal.info(
                                f"[ENERGY_SPENT_SKIP] {self.cn(code)} 에너지소진 차단 "
                                f"현재가={cur_price} 급등봉종가={_surge_close} "
                                f"이격={(_ratio-1)*100:.1f}% (<-2%)"
                            )
                            self.traded_today.add(code)
                            info["state"] = "DONE"
                            self._finish_tr(delay=True)
                            return

                self.send_market_order("BUY", code, buy_qty, "ENTRY",
                                       cur_price=cur_price)
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
    def _on_receive_chejan_data(self, values: dict):
        """웹소켓 REAL type=00 (주문체결). FID 번호는 OCX Chejan 과 같다."""
        order_no = str(values.get("9203", "")).strip()  # 주문번호
        code = norm_code(values.get("9001", ""))
        qty = abs(to_int(values.get("911")))            # 체결량 (주문별 누적)
        price = abs(to_int(values.get("910")))          # 체결가
        order_gubun = str(values.get("905", ""))        # 주문구분 (+매수 / -매도 / 매수취소 ...)
        status = str(values.get("913", "")).strip()     # 주문상태 (접수 / 체결 / 확인 ...)
        
        pend = self.pending_orders.get(code)
        pos = self.positions.get(code)

        if pend and order_no and pend.get("order_no") is None:
            pend["order_no"] = order_no
            self.log_trade.info(f"[PENDING_ORDERNO] {self.cn(code)} 주문번호={order_no}")

        if not code:
            return

        # ⭐ CANCEL_DONE 처리는 qty=0이어도 실행해야 함 (취소 chejan은 체결수량=0)
        if "취소" in order_gubun or "취소" in status:
            # 장 마감 정리 중의 취소는 재매수/재매도를 걸지 않는다.
            # (아래 일반 경로는 BUY 취소 시 _reentry_buy 를 예약함)
            # 정리 매도(CLOSE_)의 pending 은 체결 추적용이므로 남긴다.
            if self.closing:
                pend = self.pending_orders.get(code)
                if pend and not str(pend.get("reason", "")).startswith("CLOSE_"):
                    self.pending_orders.pop(code, None)
                self.ordering = bool(self.pending_orders)
                self.last_order_ts = None
                self.log_trade.info(
                    f"[CLOSE_CANCEL_DONE] {self.cn(code)} 구분={order_gubun.strip()} 상태={status}"
                )
                return

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
                else:
                    # ⭐ FIX: 멕아이씨에스·마이크로투나노 버그
                    # MAX_REENTRY_RETRIES 초과 시 traded_today 미마킹 → 오후에도 큐 재편입
                    # 수정: 포기 즉시 당일거래완료로 마킹하여 동일 종목 재진입 차단
                    self.log_trade.warning(
                        f"[REENTRY_GIVEUP] {self.cn(code)} {retries}회 미체결 → 당일거래완료"
                    )
                    self.traded_today.add(code)
                    # ⭐ FIX2: 엠디바이스 버그 — 취소 중 부분체결(1주 등) 레이스 컨디션
                    # CANCEL_DONE 처리 시점에 이미 pos.remain_qty>0이면 포지션이 살아있음
                    # buy_done=False 상태이므로 TP/MINI_TRAIL 비활성 → 수십 분 방치 위험
                    # 수정: 즉시 시장가로 청산
                    _partial_pos = self.positions.get(code)
                    if _partial_pos and _partial_pos.remain_qty > 0 and not _partial_pos.buy_done:
                        self.log_trade.warning(
                            f"[REENTRY_GIVEUP_CLEANUP] {self.cn(code)} "
                            f"포기 시점 부분체결 잔여={_partial_pos.remain_qty}주 → 즉시 시장가 청산"
                        )
                        QTimer.singleShot(
                            500,
                            lambda c=code, q=_partial_pos.remain_qty:
                                self.send_market_order("SELL", c, q, "REENTRY_GIVEUP_CLEANUP")
                        )
                        if _partial_pos:
                            _partial_pos.selling = True
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
            #  키움 Chejan FID 911 은 "그 주문의 누적 체결수량"이다.
            #  매도 경로처럼 pending_orders['filled_qty'] 로 주문별 누적을 추적해
            #  증분을 구한다.
            #
            #  [261021 푸른로보틱스] 기존 코드는 누적합이 주문수량을 넘기 전까지
            #  증분으로 더해서, 누적 252/300/301 을 853주 체결로 오인했다.
            #  그 결과 평균매수가를 5,930원으로 인식(실제 5,913원, +0.27%)했고
            #  손절·익절·본절선이 전부 그 값 기준으로 잡혔다.
            #  22거래 중 13건에서 최대 0.55% 어긋나 있었다.
            # --------------------------------------------------
            prev_filled = int(pend.get("filled_qty", 0))
            if qty > prev_filled:
                # 정상: 누적값이 증가 → 증분만 반영
                delta_buy = qty - prev_filled
                pend["filled_qty"] = qty
            elif qty == prev_filled:
                # 같은 누적값 재통보(중복 이벤트) → 반영하지 않음
                delta_buy = 0
            else:
                # 누적이 줄어들 수는 없다. 분할주문/재주문으로 카운터가 리셋된
                # 경우로 보고 이번 값을 증분으로 취급하되 주문수량을 넘지 않게 캡.
                delta_buy = min(qty, max(0, pos.total_qty - pos.remain_qty))
                pend["filled_qty"] = prev_filled + delta_buy
                self.log_system.warning(
                    f"[CHEJAN_QTY_RESET] {self.cn(code)} 누적체결 감소 "
                    f"{prev_filled}→{qty} 증분={delta_buy}"
                )

            # 주문수량 초과 금지
            delta_buy = max(0, min(delta_buy, pos.total_qty - pos.remain_qty))
            pos.remain_qty += delta_buy

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

                # ⭐ 목표/손절 기준가 보정 (260916 제이티)
                # 손절·익절을 첫 체결가로 잡아두면, 분할체결로 평균단가가 벌어졌을 때
                # 실제 매입가 기준으로는 손절이 더 가까워지고 익절은 더 멀어진다.
                # (제이티: 첫체결 5,140 / 평균 5,112 → 0.55% 어긋남)
                if pos.atr_value > 0 and pos.avg_buy_price > 0:
                    _base = int(round(pos.avg_buy_price))
                    _gap = (abs(_base - pos.entry_price) / pos.entry_price
                            if pos.entry_price else 0)
                    if _gap >= 0.001:          # 0.1% 이상 어긋날 때만 재설정
                        _raw_sl = int(_base - pos.atr_value * ATR_SL_MULT)
                        pos.atr_sl_price = self.adjust_tick_size(
                            min(max(1, _raw_sl), _base - self.min_tick(_base))
                        )
                        pos.atr_tp1_price = self.adjust_tick_size(
                            int(_base + pos.atr_value * ATR_TP_MULT)
                        )
                        if not pos.tp1_done:
                            pos.atr_tp2_price = pos.atr_tp1_price
                        pos.atr_safe_price = self.adjust_tick_size(
                            max(_base, int(_base - pos.atr_value * ATR_SAFE_MULT))
                        )
                        self.log_trade.info(
                            f"[TP_TARGET_REBASE] {self.cn(code)} "
                            f"첫체결={pos.entry_price} 평균={_base} ({_gap*100:+.2f}%) "
                            f"→ 손절={pos.atr_sl_price} TP1={pos.atr_tp1_price} "
                            f"본절={pos.atr_safe_price}"
                        )
                
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
                order_qty   = pend.get("qty", 0)
                if qty > prev_filled:
                    # 누적값이 증가 → 누적으로 해석, 증분 계산
                    delta = qty - prev_filled
                    pend["filled_qty"] = qty
                else:
                    # ⭐ FIX: 파인엠텍 버그 - qty <= prev_filled일 때 증분으로 처리하면
                    # 중복 이벤트(같은 누적값 2회 수신) 시 잔여 수량이 2배 차감됨
                    # → remain_qty 초과 방지: delta = min(qty, remain_qty)
                    delta = min(qty, pos.remain_qty)
                    pend["filled_qty"] = min(prev_filled + delta, order_qty)
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
                elif "REENTRY_GIVEUP_CLEANUP" in sell_reason:
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
    def _on_receive_real_data(self, code, values: dict):
        """웹소켓 REAL type=0B (주식체결)"""
        pos = self.positions.get(code)
        if not pos or pos.remain_qty <= 0:
            self.log_signal.debug(
                f"[REAL_SKIP] {self.cn(code)} 포지션 없음"
            )
            return
        cur = abs(to_int(values.get("10")))   # 현재가 FID = 10
        # 거래량 FID = 15
        vol = abs(to_int(values.get("15")))
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
        # 장 마감 정리 중에는 정리 로직이 매도를 전담 (pending 을 지우면 체결 추적이 깨짐)
        if not self.closing and _tick_now - self._last_zombie_check_ts >= _ZOMBIE_INTERVAL:
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
        from config import CANDLE_INTERVAL_MIN as _CIM
        _now = _dt.now()
        cur_slot = (_now.hour * 60 + _now.minute) // _CIM

        if pos.sl_candle_minute == -1:
            # 첫 틱: 분봉 초기화
            pos.sl_candle_minute = cur_slot
            pos.sl_candle_open   = cur
            pos.sl_candle_high   = cur
            pos.sl_candle_low    = cur
            pos.sl_candle_last   = cur

        elif cur_slot != pos.sl_candle_minute:
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
                    pos.sl_candle_minute = cur_slot
                    pos.sl_candle_open   = cur
                    pos.sl_candle_high   = cur
                    pos.sl_candle_low    = cur
                    pos.sl_candle_last   = cur
                    return

            # 새 분봉 시작
            pos.sl_candle_minute = cur_slot
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
                # 현재가가 이미 SL 아래이면 지정가는 미체결 → 시장가 사용
                _rt_sl_lmt = pos.atr_sl_price if cur >= pos.atr_sl_price else 0
                ok = self.send_market_order("SELL", code, pos.remain_qty, "STOP_LOSS",
                                                   sl_limit_price=_rt_sl_lmt)
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
                # ⭐ FIX: 삼일씨엔에스 버그 — MINI_TRAIL_STOP이 pnl=+0.15%에서 발동 → 수수료 포함 손실
                # 원인: TRIGGER=GAP=1.5%이면 발동 최소 보장 pnl = TRIGGER-GAP = 0%
                # 수정: pnl_rate < 수수료 구간(0.6%)이면 발동 억제 → ATR 손절선에 맡김
                _min_exit_pnl = 0.006  # 왕복 수수료(~0.58%) 감안 최소 +0.6% 이상일 때만 발동
                if drop >= _active_gap and pnl_rate >= _min_exit_pnl:
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
                        pos.selling        = True
                        pos.time_stop_done = True   # 타임스탑 중복 방지
                        pos.tp1_done       = True   # ⭐ FIX: TP1 중복 발동 방지
                        # MINI_TRAIL_STOP은 전량 매도 → TP1/TP2 모두 무의미
                        # tp1_done=True로 세팅하지 않으면, selling이 잠깐 False로 리셋되는
                        # 타이밍에 TP1이 추가 주문을 발송해 [800033] 이중 주문 발생 (일성건설 케이스)
                    return
                elif drop >= _active_gap and pnl_rate < _min_exit_pnl:
                    # 억제 로그는 틱마다 찍히지 않도록 포지션당 1회만
                    if not getattr(pos, 'mini_trail_hold_logged', False):
                        pos.mini_trail_hold_logged = True
                        self.log_trade.info(
                            f"[MINI_TRAIL_HOLD] {self.cn(code)} "
                            f"GAP 충족({drop*100:.2f}%≥{_active_gap*100:.1f}%) 하지만 "
                            f"pnl={pnl_rate*100:.2f}%<최소{_min_exit_pnl*100:.1f}% → 발동 억제 (ATR손절 대기)"
                        )

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
                pos.tp1_order_ts = now   # 체결 전 유예 계산 기준
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
            # ⭐ FIX [261021 푸른로보틱스]: 체결 전에는 tp1_done_ts 가 0이라
            # 999초로 계산돼 유예가 무시됐다. 주문 전송 시각을 기준으로 삼는다.
            _tp1_ref = pos.tp1_done_ts or pos.tp1_order_ts
            _tp1_elapsed = (now - _tp1_ref) if _tp1_ref else 0
            if code in self.pending_orders:
                # TP1 매도 주문이 아직 떠 있다 → 여기서 또 팔면 보유수량을 넘는다
                pass
            elif _tp1_elapsed < 120:
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
            and (now - (pos.tp1_done_ts or pos.tp1_order_ts or now)) < TP1_PROTECT_SEC
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
    # 주문 취소 함수
    # ==================================================
    def _send_cancel_rest(self, code: str, org_order_no: str, qty: int = 0):
        """kt10003 취소 1건. 반환: (성공여부, 실패사유). qty=0 이면 잔량 전부."""
        try:
            self.rest.cancel_order(code, org_order_no, qty)
            return True, ""
        except KiwoomError as e:
            return False, str(e)
        except KiwoomTransportError as e:
            # 취소는 중복으로 가도 거절될 뿐이라 실패로 보고 재시도에 맡긴다
            return False, f"통신오류({e})"

    def send_cancel_order(self, code: str, org_order_no: str, cancel_side: str = "SELL") -> bool:

        if not org_order_no:
            self.log_system.error(f"[CANCEL_ABORT] 주문번호 없음 {self.cn(code)}")
            return False

        ok, err = self._send_cancel_rest(code, org_order_no, 0)   # 0 = 잔량 전부 취소

        if not ok:
            self.log_trade.error(
                f"[CANCEL_FAIL] {self.cn(code)} 원주문번호={org_order_no} 사유={err}"
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
                    if "TIME_STOP" not in _stuck_reason and "VOL_TIME_STOP" not in _stuck_reason and "MINI_TRAIL" not in _stuck_reason:
                        pos.time_stop_done = False  # STOP_LOSS 계열: 재시도 허용
                    else:
                        # TIME_STOP / MINI_TRAIL: 시장가 원주문이 표류 중이므로 재발동 차단
                        # → time_stop_done 유지, 3초 후 _retry_sell_after_reject 경로로 재시도
                        _retry_tag = (
                            "TIME_STOP_RETRY"
                            if ("TIME_STOP" in _stuck_reason or "VOL_TIME_STOP" in _stuck_reason)
                            else "MINI_TRAIL_RETRY"
                        )
                        self.log_system.info(
                            f"[SELL_STUCK_RETRY] {self.cn(code)} "
                            f"{_stuck_reason} STUCK → 재발동 차단, 3초 후 강제 재시도"
                        )
                        QTimer.singleShot(
                            3000,
                            lambda c=code, q=pos.remain_qty, r=_retry_tag: self._retry_sell_after_reject(
                                c, q, r
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

        ok = self.send_market_order("BUY", code, qty, f"{reason}_RETRY{retry_cnt}",
                                    cur_price=orig_price)

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

        # 장 마감 정리 중에는 정리 주문(CLOSE_)만 허용.
        # 실시간 손절/좀비복구/재시도 타이머가 끼어들면 매도가능수량 부족([800033])이나
        # 중복 매도가 난다.
        if self.closing and not str(reason).startswith("CLOSE_"):
            self.log_trade.warning(
                f"[CLOSE_ORDER_BLOCK] 방향={side} {self.cn(code)} 수량={qty} "
                f"사유={reason} - 장 마감 정리 중"
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
            # ⭐ 진입 슬리피지 상한: 신호가 × (1+ENTRY_MAX_SLIP) 지정가
            # 기존에는 pending_orders[code] 에서 cur_price 를 읽었는데, 신규 진입
            # 시점에는 그 항목이 아직 없어서(주문 성공 후 생성) 항상 시장가로 나갔다.
            # 호출부에서 넘긴 cur_price 를 먼저 본다.
            try:
                from config import ENTRY_MAX_SLIP as _max_slip
            except ImportError:
                _max_slip = 0.0
            _cur = int(kwargs.get("cur_price", 0) or
                       self.pending_orders.get(code, {}).get("cur_price", 0) or 0)
            if _cur > 0 and _max_slip > 0:
                _lmt_p  = self.adjust_tick_size(int(_cur * (1 + _max_slip)))
                _lmt_p  = max(_lmt_p, _cur + self.min_tick(_cur))   # 최소 1틱 여유
                _ord_tp = "00"                # 지정가
                self.log_trade.info(
                    f"[ORDER_LIMIT_CAP] {self.cn(code)} 신호가={_cur} "
                    f"지정가={_lmt_p}원 (슬리피지 상한 {_max_slip*100:.2f}%)"
                )
            elif _cur > 0 and _cur < 2000:
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

        # ─── REST 주문 (kt10000 매수 / kt10001 매도) ─────────────────────
        # OCX 와의 차이: 거절 사유가 OnReceiveMsg 로 나중에 오지 않고 응답에 바로 온다.
        # 기존 거절 처리([800033] 재시도 등)를 그대로 쓰기 위해, 서버 거절은
        # OCX 와 같은 순서(접수 → 거절 메시지)로 _on_receive_msg 에 넘긴다.
        #   ret = 0   : 접수 (order_no 확정)
        #   ret = 1   : 서버 거절 → pending 생성 후 거절 메시지 처리
        #   ret = 2   : 통신 오류, 접수됐을 수 있음 → pending(order_no 없음) 으로 체결 통보 대기
        #   ret = -308: 호출 제한 초과 (OCX 주문차단과 같은 처리)
        #   ret = -1  : 전송 실패 (확실히 미접수)
        _trde_tp = "0" if _ord_tp == "00" else "3"     # OCX 00=지정가 → 0, 03=시장가 → 3
        order_no, reject_msg = None, ""
        try:
            order_no = self.rest.send_order(side, code, qty, _trde_tp, _lmt_p) or None
            ret = 0
        except KiwoomRateLimitError as e:
            reject_msg = str(e)
            ret = -308
        except KiwoomError as e:
            reject_msg = str(e)
            ret = 1
        except KiwoomTransportError as e:
            reject_msg = str(e)
            ret = 2 if e.maybe_sent else -1

        if ret == 2:
            self.log_trade.warning(
                f"[ORDER_UNKNOWN] 방향={side} {self.cn(code)} 수량={qty}주 사유={reason} "
                f"응답 없음 - 접수됐을 수 있어 체결통보를 기다림 ({reject_msg})"
            )
        elif ret == 1:
            self.log_trade.error(
                f"[ORDER_REJECTED] 방향={side} {self.cn(code)} 수량={qty}주 사유={reason} 메시지={reject_msg}"
            )

        # 주문 접수 실패 처리
        if ret not in (0, 1, 2):
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
                f"[ORDER_FAIL] 방향={side} {self.cn(code)} 수량={qty}주 사유={reason} ret={ret} {reject_msg}"
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
                "order_no": order_no,   # REST 는 접수 응답에 주문번호가 온다 (통신오류 시 None → 체결통보에서 채움)
                "cancel_retries": 0,
                "last_cancel_ts": 0.0,
                "reentry_retries": 0,
                "filled_qty": 0         # SELL 주문별 누적 체결수량 추적
            }
            if ret == 1:
                # OCX 에서 OnReceiveMsg 가 SendOrder 직후 따로 오던 것과 같은 순서로 처리
                QTimer.singleShot(
                    0, lambda s=side, m=reject_msg, c=code:
                        self._on_receive_msg("", s, "", m, force_reject=True, code=c)
                )
        return True

    # ==================================================
    # 기본 유틸리티 함수 모음
    # ==================================================
    # 1. 실시간 등록
    def register_real(self, code):
        # 0B 주식체결 (10=현재가, 15=거래량 포함). 그룹 2, 기존 등록 유지.
        # 웹소켓이 끊겨 있으면 기억해 두었다가 재접속 시 등록된다.
        self.ws.register("2", ["0B"], [code])

        self.log_system.info(
        f"[REAL_REG] 등록종목수={len(self.positions)} 종목={list(self.positions.keys())}"
        )

    # 2. 분봉 파싱
    def parse_candle(self, code):
        """
        ka10080 분봉 데이터 파싱 (CANDLE_INTERVAL_MIN 단위)
        - OHLCV 포함
        - 최소 30개 이상 확보 (VER2 전략 대응)
        - 최신봉 → 과거봉 순서 유지
        """

        candles = []

        try:
            data_rows = self._candle_rows
            rows = len(data_rows)

            if rows <= 0:
                now_str = datetime.now().strftime('%H:%M:%S')
                self.log_system.warning(f"[CANDLE_PARSE_EMPTY] {self.cn(code)} - 현재시각: {now_str}")
                return []

            # ✔ 최소 30개 확보 (여유 두고 60까지 가져와도 OK)
            fetch_cnt = min(rows, 60)

            for i in range(fetch_cnt):
                row = data_rows[i]

                # ⚠️ 키움은 가격을 부호 붙은 문자열("+12345" / "-12345")로 준다.
                # 시가/고가/저가/현재가는 abs()로 절댓값을 취해 가격으로 사용.
                open_ = abs(to_int(row.get("open_pric")))
                high  = abs(to_int(row.get("high_pric")))
                low   = abs(to_int(row.get("low_pric")))
                close = abs(to_int(row.get("cur_prc")))

                # 거래량은 항상 양수
                volume = abs(to_int(row.get("trde_qty")))

                # 체결시간(YYYYMMDDHHMMSS) — 백테스트 데이터 적재용.
                # 전략 로직은 OHLCV만 사용하므로 필드 추가는 영향 없음.
                ts_raw = str(row.get("cntr_tm", "")).strip()

                # 시가 또는 종가가 0이면 데이터 오류 → 스킵
                if open_ == 0 or close == 0:
                    continue

                candles.append({
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "time": ts_raw,
                })

            # ✔ VER2는 25개 이상 필요
            if len(candles) < 30:
                self.log_system.warning(
                    f"[CANDLE_PARSE_SHORT] {self.cn(code)} 캔들수={len(candles)}개 (30개 미만)"
                )
                return candles

            # self.log_system.info(
            #     f"[1MIN_PARSE] {self.cn(code)} rows={len(candles)} (OHLCV)"
            # )

            return candles

        except Exception as e:
            self.log_system.error(f"[CANDLE_PARSE_ERROR] {self.cn(code)} 파싱오류: {e}")
            return []

    # 3. 계좌번호 조회
    def get_account(self):
        # REST 는 앱키가 계좌에 묶여 있어 주문에 계좌번호를 넣지 않는다 (표시/점검용)
        if IS_REAL:
            return ACCOUNT_NO
        return MOCK_ACCOUNT_NO

    # 3-1. 종목명 조회 (login 때 받은 ka10099 캐시)
    def get_stock_name(self, code: str) -> str:
        return self._stock_names.get(code) or code

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
    # ⭐ BUG-3: 키움 잔고조회 TR (kt00018, OCX OPW00018) 기반 전일 미청산 포지션 복구
    # ==================================================
    def load_holdings_from_api(self, on_done: callable = None):
        """
        kt00018로 실제 보유 잔고를 조회해 positions에 복구.
        프로그램 재시작 시 전일 미청산 잔고를 메모리에 재등록하기 위해 사용.

        on_done: 조회 완료 후 콜백 (main.py에서 liquidate_leftover_positions 예약용)
        """
        self._holdings_callback = on_done
        self._holdings_attempt += 1
        self._rest_async(self.rest.holdings, self._on_receive_holdings_tr, self._on_holdings_error)
        self.log_system.info("[HOLDINGS_TR] kt00018 잔고조회 요청")

    def _on_holdings_error(self, err):
        if self._holdings_attempt < 3:
            self.log_system.warning(
                f"[HOLDINGS_TR_RETRY] 잔고조회 실패 {self._holdings_attempt}회차: {err} → 3초 후 재시도"
            )
            QTimer.singleShot(3000, lambda: self.load_holdings_from_api(self._holdings_callback))
            return
        self._holdings_attempt = 0
        self.log_system.error(f"[HOLDINGS_TR_FAIL] 잔고조회 3회 실패: {err} - 전일 잔고를 HTS 에서 확인하세요")

    def _on_receive_holdings_tr(self, data_rows: list):
        """kt00018 수신 처리"""
        self._holdings_attempt = 0
        rows = len(data_rows)
        recovered = 0

        for row in data_rows:
            code  = norm_code(row.get("stk_cd"))
            name  = str(row.get("stk_nm", "")).strip()
            qty   = to_int(row.get("rmnd_qty"))
            price = abs(to_int(row.get("pur_pric")))
            if code and name:
                self._stock_names.setdefault(code, name)

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
            self._record_watch(code, cond_name)   # 연구용 녹화 (매매 판단과 무관)
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
            self._record_watch(code, cond_name)   # 연구용 녹화 (매매 판단과 무관)
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

                # ⭐ FIX: Qt signal 이중 발생 방지 — 500ms 내 중복 COND_IN 이벤트 차단
                _last_cond_in = info.get("last_cond_in_ts", 0)
                if pytime.time() - _last_cond_in < 0.5:
                    self.log_trade.debug(
                        f"[COND_REENTRY_DEBOUNCE] {self.cn(code)} "
                        f"이중발신 차단 ({(pytime.time()-_last_cond_in)*1000:.0f}ms 이내)"
                    )
                    return
                info["last_cond_in_ts"] = pytime.time()

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
            if self.recorder:
                self.recorder.record_event(code, "COND_OUT", cond_name)
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
    def _on_receive_msg(self, screen_no, rqname, trcode, msg, force_reject=False, code=None):
        """
        주문 거절 처리. REST 에서는 send_market_order 가 거절 응답을 받으면
        force_reject=True, code=종목코드 로 호출한다.
        """
        self.log_system.info(f"[RECV_MSG] 화면={screen_no} 요청={rqname} TR={trcode} 메시지={msg}")

        if not force_reject and "주문완료" in msg:
            self.last_order_ts = pytime.time()
            return

        # ── 주문 거부/실패 감지 ──
        # 키움 서버가 주문을 거부하면 Chejan 이벤트는 발생하지 않으므로
        # 여기서 pending_orders를 직접 정리해야 무한 대기를 방지할 수 있다.
        reject_keywords = ["거부", "오류", "실패", "제한", "정지", "불가", "초과", "부족"]
        is_reject = force_reject or any(kw in msg for kw in reject_keywords)

        if not is_reject:
            return

        self.log_system.error(
            f"[ORDER_REJECT] 화면={screen_no} 요청={rqname} 메시지={msg}"
        )

        # rqname이 "BUY" 또는 "SELL"인 경우 → send_market_order에서 발송
        # pending_orders 중 해당 side와 매칭되는 항목 정리
        # REST 는 거절된 종목(code)을 알기 때문에 그 종목만 정리한다
        if rqname in ("BUY", "SELL"):
            # side가 일치하는 pending을 찾아 제거
            # order_no 유무 관계없이 매칭 (주문번호 배정 후에도 거부될 수 있음)
            _target = code
            for code, pend in list(self.pending_orders.items()):
                if _target and code != _target:
                    continue
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
    # 장 마감 정리 (강제청산 시각에 main.py 가 호출)
    # ==================================================
    def force_liquidation_all(self):
        """
        장 마감 정리. 프로그램 메모리가 아니라 증권사 기준으로 정리한다.

        [배경: KRX 애프터마켓 팝업 / 잔고 이월]
          정규장 미체결 주문이 남은 채 애프터마켓으로 넘어가면 키움이
          "[KRX 애프터마켓 주문 안내]" 팝업을 띄운다. 기존 강제청산은
          프로그램이 기억하는 포지션에만 매도를 내서 아래가 남았다.
            - FORCE_ABANDON / CANCEL_ZOMBIE 가 pending 만 지우고,
              증권사에는 살아 있는 주문
            - 그 주문이 뒤늦게 체결돼 생긴, 프로그램이 모르는 잔고 (CHEJAN_SKIP)
            - 연속 매도가 0.2초 스로틀에 걸려 실패 → 포지션을 목록에서 삭제
          260910 DMS 125주가 다음 날로 이월된 사례.

        [순서]
          1) closing=True → 사유가 CLOSE_ 로 시작하는 정리 주문만 허용, 스캔 중단
          2) ka10075 미체결 전량 조회 → 매수/매도 모두 취소
          3) kt00018 실제 잔고 조회 → 메모리 포지션 대조 → 매매가능수량 전량 시장가 매도
          4) 정규장 종료 전 재확인 (CLOSE_VERIFY_*):
               미체결 매수는 취소, 정리 매도는 동시호가 체결 대기로 두고,
               남은 매매가능수량은 재매도
        """
        if self.closing:
            self.log_system.info("[CLOSE_SKIP] 이미 장 마감 정리 진행 중")
            return
        if not is_market_time():
            self.log_system.warning("[CLOSE_SKIP] 정규장 시간 외 - 정리 주문 불가")
            return

        self.closing = True
        self._close_started_ts = pytime.time()
        self._close_failed = set()
        self._scan_running = False
        self.scan_queue.clear()

        self.log_system.warning(
            f"[FORCE_LIQUIDATION_START] 장 마감 정리 시작 "
            f"포지션={len(self.positions)} pending={len(self.pending_orders)}"
        )
        self._close_query_open_orders("CANCEL")

    # ── 정리용 TR 요청 (재시도 + 응답 타임아웃) ─────────────────────────
    # 연속조회를 끝까지 받으므로 OCX 시절보다 오래 걸릴 수 있어 대기 한도를 15초로 둔다
    CLOSE_TR_TIMEOUT_MS = 15000

    def _close_request_tr(self, tr_code, attempt=1):
        """tr_code: 'KA10075'(미체결) | 'KT00018'(잔고). 응답은 _on_close_tr_done 으로."""
        fn = self.rest.open_orders if tr_code == "KA10075" else self.rest.holdings

        self._close_wait_seq += 1
        seq = self._close_wait_seq
        self._close_waiting = (tr_code, seq)
        self.log_system.info(
            f"[CLOSE_TR] {tr_code} 요청 phase={self._close_phase}"
        )
        QTimer.singleShot(self.CLOSE_TR_TIMEOUT_MS, lambda: self._close_tr_timeout(tr_code, seq))
        self._rest_async(
            fn,
            lambda rows: self._on_close_tr_done(tr_code, seq, rows),
            lambda err: self._on_close_tr_error(tr_code, seq, attempt, err),
        )

    def _on_close_tr_done(self, tr_code, seq, rows):
        if self._close_waiting != (tr_code, seq):
            return                         # 타임아웃 처리 후 늦게 온 응답
        self._close_waiting = None
        if tr_code == "KA10075":
            self._on_close_open_orders_tr(rows)
        else:
            self._on_close_holdings_tr(rows)

    def _on_close_tr_error(self, tr_code, seq, attempt, err):
        if self._close_waiting != (tr_code, seq):
            return
        self._close_waiting = None
        if attempt < 3:
            self.log_system.warning(
                f"[CLOSE_TR_RETRY] {tr_code} {err} {attempt}회차 → 1초 후 재요청"
            )
            QTimer.singleShot(1000, lambda: self._close_request_tr(tr_code, attempt + 1))
            return

        self.log_system.error(f"[CLOSE_TR_FAIL] {tr_code} {err} 3회 실패")
        self._close_tr_fallback(tr_code)

    def _close_tr_timeout(self, tr_code, seq):
        if self._close_waiting != (tr_code, seq):
            return                         # 이미 응답 받음
        self._close_waiting = None
        self.log_system.error(
            f"[CLOSE_TR_TIMEOUT] {tr_code} {self.CLOSE_TR_TIMEOUT_MS // 1000}초 내 응답 없음"
        )
        self._close_tr_fallback(tr_code)

    def _close_tr_fallback(self, tr_code):
        """조회가 안 되면 멈추지 말고 다음 단계로 진행한다."""
        if str(tr_code).upper() == "KA10075":
            # 미체결 조회 실패 → 취소는 못 해도 잔고 기준 매도는 시도
            self._close_resting_sells = set()
            self._close_query_holdings()
        elif self._close_phase == "CANCEL":
            # 잔고 조회 실패 → 메모리 포지션 기준으로라도 매도
            self.log_system.error("[CLOSE_FALLBACK] 잔고 조회 실패 → 메모리 포지션 기준 매도")
            sells = [(c, p.remain_qty) for c, p in self.positions.items() if p.remain_qty > 0]
            self._close_send_sells(sells)
        elif self._close_phase == "VERIFY":
            # 이미 한 번 매도를 냈으므로 메모리 기준 재매도는 중복 위험 → 재확인 종료
            self.log_system.error("[CLOSE_FALLBACK] 재확인 잔고 조회 실패 → 재매도 생략")
            self._close_finish()
        else:
            self._close_phase = "DONE"
            self.log_system.error(
                "[CLOSE_REPORT_FAIL] 최종 잔고 조회 실패 - HTS 에서 잔고를 직접 확인하세요"
            )

    # ── 1) 미체결 조회 → 취소 ───────────────────────────────────────────
    def _close_query_open_orders(self, phase):
        self._close_phase = phase
        self._close_request_tr("KA10075")     # 전 종목 / 매수·매도 전체 미체결

    def _on_close_open_orders_tr(self, data_rows: list):
        orders = []
        for row in data_rows:
            remain   = to_int(row.get("oso_qty"))          # 미체결수량
            gubun    = str(row.get("io_tp_nm", "")).strip() # 주문구분 (+매수 / -매도 ...)
            order_no = str(row.get("ord_no", "")).strip()
            code     = norm_code(row.get("stk_cd"))

            if remain <= 0 or not order_no or not code or "취소" in gubun:
                continue
            if "매수" in gubun:
                side = "BUY"
            elif "매도" in gubun:
                side = "SELL"
            else:
                continue
            orders.append({"code": code, "order_no": order_no, "side": side,
                           "qty": remain, "gubun": gubun})

        self.log_system.warning(
            f"[CLOSE_ORDERS] phase={self._close_phase} 미체결 {len(orders)}건 "
            f"{[(self.cn(o['code']), o['side'], o['qty']) for o in orders]}"
        )

        if self._close_phase == "CANCEL":
            targets = orders
            self._close_resting_sells = set()
        else:
            # 재확인 단계의 매도 미체결 = 정리 매도(동시호가 체결 대기). 취소하지 않는다.
            targets = [o for o in orders if o["side"] == "BUY"]
            waiting = [o for o in orders if o["side"] == "SELL"]
            self._close_resting_sells = {o["code"] for o in waiting}
            if waiting:
                self.log_system.info(
                    f"[CLOSE_VERIFY_SELL_WAIT] 매도 {len(waiting)}건 체결 대기 "
                    f"{[(self.cn(o['code']), o['qty']) for o in waiting]}"
                )

        self._close_send_cancels(targets)

    def _close_send_cancels(self, orders, idx=0):
        from config import CLOSE_ORDER_GAP_MS, CLOSE_CANCEL_SETTLE_SEC

        if idx >= len(orders):
            # 취소 통보(Chejan)가 들어올 시간을 준 뒤 잔고 조회
            settle_ms = CLOSE_CANCEL_SETTLE_SEC * 1000 if orders else 0
            QTimer.singleShot(settle_ms, self._close_query_holdings)
            return

        o = orders[idx]
        ok, err = self._send_cancel_rest(o["code"], o["order_no"], 0)   # 0 = 미체결 잔량 전부
        if ok:
            self.log_trade.warning(
                f"[CLOSE_CANCEL_SEND] {self.cn(o['code'])} {o['gubun']} "
                f"원주문번호={o['order_no']} 미체결={o['qty']}주"
            )
        else:
            self.log_trade.error(
                f"[CLOSE_CANCEL_FAIL] {self.cn(o['code'])} {o['gubun']} "
                f"원주문번호={o['order_no']} 사유={err}"
            )

        QTimer.singleShot(CLOSE_ORDER_GAP_MS,
                          lambda: self._close_send_cancels(orders, idx + 1))

    # ── 2) 실제 잔고 조회 → 대조 → 매도 ────────────────────────────────
    def _close_query_holdings(self):
        self._close_request_tr("KT00018")

    def _on_close_holdings_tr(self, data_rows: list):
        holdings = {}
        for row in data_rows:
            code = norm_code(row.get("stk_cd"))
            hold = to_int(row.get("rmnd_qty"))            # 보유수량
            if not code or hold <= 0:
                continue
            holdings[code] = {
                "hold": hold,
                "sellable": to_int(row.get("trde_able_qty")),  # 매매가능수량
                "price": abs(to_int(row.get("pur_pric"))),     # 매입가
            }

        self.log_system.warning(
            f"[CLOSE_HOLDINGS] phase={self._close_phase} 실제잔고 {len(holdings)}종목 "
            f"{[(self.cn(c), h['hold'], h['sellable']) for c, h in holdings.items()]}"
        )

        # 증권사에 없는 포지션 = 이미 청산됨 → 메모리 정리
        for code in list(self.positions):
            if code not in holdings:
                self.log_system.warning(
                    f"[CLOSE_RECONCILE_GONE] {self.cn(code)} 실제 잔고 없음 → 포지션 제거"
                )
                self.positions.pop(code, None)
                self.pending_orders.pop(code, None)

        # 정규장 종료 후 최종 보고: 주문은 못 내므로 결과만 남긴다
        if self._close_phase == "REPORT":
            self._close_phase = "DONE"
            if not holdings:
                self.log_system.warning("[CLOSE_REPORT_OK] 정규장 종료 후 실제 잔고 없음 - 이월 없음")
                return
            msg = (f"정규장 종료 후에도 실제 잔고 {len(holdings)}종목이 남았습니다 "
                   f"{[(self.cn(c), h['hold']) for c, h in holdings.items()]}. "
                   f"다음 거래일 09:00 전일잔고 청산 대상입니다.")
            self.log_system.error(f"[CLOSE_REPORT_LEFTOVER] {msg}")
            try:
                from discord_notify import notify_system
                notify_system(msg, level="CRITICAL")
            except Exception as e:
                self.log_system.error(f"[DISCORD_FAIL] 장마감잔고알림: {e}")
            return

        sells = []
        for code, h in holdings.items():
            pos = self.positions.get(code)
            if pos is None:
                # 프로그램이 모르던 잔고 (뒤늦은 체결, 수동 주문, 재시작 등)
                pos = PositionState(
                    code=code, entry_price=h["price"], highest_price=h["price"],
                    total_qty=h["hold"], remain_qty=h["hold"],
                    ordering=False, selling=False,
                )
                pos.buy_done = True
                pos.entry_ts = pytime.time()
                pos.last_sell_attempt_ts = pytime.time()
                self.positions[code] = pos
                self.log_system.warning(
                    f"[CLOSE_RECONCILE_UNKNOWN] {self.cn(code)} 프로그램이 모르던 잔고 "
                    f"{h['hold']}주 → 정리 대상 추가"
                )
            elif pos.remain_qty != h["hold"]:
                self.log_system.warning(
                    f"[CLOSE_RECONCILE_QTY] {self.cn(code)} "
                    f"메모리={pos.remain_qty}주 → 실제={h['hold']}주로 보정"
                )
                pos.remain_qty = h["hold"]
                pos.total_qty = max(pos.total_qty, h["hold"])

            if h["sellable"] > 0:
                sells.append((code, h["sellable"]))
            elif code in self._close_resting_sells:
                self.log_system.info(
                    f"[CLOSE_SELL_WAIT] {self.cn(code)} 매매가능수량 0 "
                    f"(보유 {h['hold']}주는 걸려 있는 매도 주문의 체결 대기)"
                )
            else:
                # 걸린 매도가 없는데 매매가능 0 = 취소 반영 지연이거나 필드 누락.
                # 보유수량으로 낸다. 아직 잠겨 있으면 [800033] 재시도 경로가 처리한다.
                self.log_system.warning(
                    f"[CLOSE_SELLABLE_ZERO] {self.cn(code)} 매매가능 0, 걸린 매도 없음 "
                    f"→ 보유수량 {h['hold']}주로 매도"
                )
                sells.append((code, h["hold"]))

        self._close_send_sells(sells)

    def _close_send_sells(self, sells, idx=0, attempt=1):
        from config import CLOSE_ORDER_GAP_MS

        if idx >= len(sells):
            self._close_after_sells()
            return

        code, qty = sells[idx]
        pos = self.positions.get(code)
        reason = ("CLOSE_FORCE_LIQUIDATION" if self._close_phase == "CANCEL"
                  else "CLOSE_FORCE_LIQUIDATION_RETRY")

        ok = False
        if pos and pos.remain_qty > 0:
            # 이전 주문은 1)에서 취소했으므로 상태를 비우고 새로 낸다
            pos.selling = False
            pos.sl_ordered = False
            prev = self.pending_orders.get(code)
            if prev and not str(prev.get("reason", "")).startswith("CLOSE_"):
                self.pending_orders.pop(code, None)
            ok = self.send_market_order("SELL", code, qty, reason)

        if ok:
            self._close_failed.discard(code)
            pos.selling = True
            pos.sl_ordered = True
            pos.last_sell_attempt_ts = pytime.time()
            self.log_trade.warning(
                f"[FORCE_LIQUIDATION] {self.cn(code)} qty={qty} phase={self._close_phase}"
            )
            if self._close_phase == "CANCEL":
                try:
                    from discord_notify import notify_force_liquidation
                    notify_force_liquidation(
                        code=code, name=self.get_stock_name(code),
                        qty=qty, entry_price=pos.entry_price,
                    )
                except Exception as e:
                    import traceback
                    self.log_system.error(
                        f"[DISCORD_FAIL] 강제청산알림: {e}\n{traceback.format_exc()}"
                    )
        elif pos and attempt < 3:
            self.log_system.warning(
                f"[CLOSE_SELL_RETRY] {self.cn(code)} 매도 실패 {attempt}회차 → 1초 후 재시도"
            )
            QTimer.singleShot(1000, lambda: self._close_send_sells(sells, idx, attempt + 1))
            return
        elif pos:
            # 기존 코드는 여기서 포지션을 지워 이월을 숨겼다. 남겨두고 재확인에서 다시 판다.
            self._close_failed.add(code)
            self.log_system.error(
                f"[CLOSE_SELL_FAIL] {self.cn(code)} 매도 3회 실패 - 포지션 유지, 재확인 단계에서 재시도"
            )

        QTimer.singleShot(CLOSE_ORDER_GAP_MS,
                          lambda: self._close_send_sells(sells, idx + 1))

    # ── 3) 재확인 예약 / 종료 ─────────────────────────────────────────
    def _close_after_sells(self):
        if self._close_phase != "CANCEL":
            self._close_finish()
            return

        from config import CLOSE_VERIFY_DELAY_SEC, CLOSE_VERIFY_LATEST

        now = datetime.now()
        hh, mm = map(int, CLOSE_VERIFY_LATEST.split(":"))
        latest = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        target = datetime.fromtimestamp(self._close_started_ts + CLOSE_VERIFY_DELAY_SEC)
        target = min(target, latest)

        if now >= latest:
            self.log_system.warning(
                f"[CLOSE_VERIFY_SKIP] 이미 {CLOSE_VERIFY_LATEST} 이후 - 재확인 생략"
            )
            self._close_finish()
            return

        delay_ms = max(30_000, int((target - now).total_seconds() * 1000))
        fire_at = datetime.fromtimestamp(now.timestamp() + delay_ms / 1000)
        self.log_system.info(f"[CLOSE_VERIFY_SCHEDULED] {fire_at:%H:%M:%S} 재확인")
        QTimer.singleShot(delay_ms, lambda: self._close_query_open_orders("VERIFY"))

    def _close_finish(self):
        """재확인 단계 종료. 매도 실패만 경보하고, 체결 대기는 정규장 종료 후 보고로 넘긴다."""
        from config import CLOSE_REPORT_AT

        waiting = [c for c, p in self.positions.items()
                   if p.remain_qty > 0 and c not in self._close_failed]
        if self._close_failed:
            msg = (f"장 마감 정리 매도 실패 {len(self._close_failed)}종목 "
                   f"{[self.cn(c) for c in self._close_failed]} - HTS 에서 직접 확인하세요")
            self.log_system.error(f"[CLOSE_SELL_FAIL_SUMMARY] {msg}")
            try:
                from discord_notify import notify_system
                notify_system(msg, level="CRITICAL")
            except Exception as e:
                self.log_system.error(f"[DISCORD_FAIL] 장마감정리알림: {e}")
        self.log_system.warning(
            f"[CLOSE_VERIFY_DONE] 체결 대기 {len(waiting)}종목 "
            f"{[self.cn(c) for c in waiting]} 매도실패 {len(self._close_failed)}종목"
        )

        # 동시호가 체결(정규장 종료) 이후 실제 잔고로 최종 보고
        now = datetime.now()
        hh, mm = map(int, CLOSE_REPORT_AT.split(":"))
        report_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delay_ms = max(0, int((report_at - now).total_seconds() * 1000))
        self._close_phase = "REPORT_WAIT"
        self.log_system.info(f"[CLOSE_REPORT_SCHEDULED] {max(now, report_at):%H:%M:%S} 최종 잔고 보고")

        def _report():
            self._close_phase = "REPORT"
            self._close_query_holdings()
        QTimer.singleShot(delay_ms, _report)
