# -*- coding: utf-8 -*-
"""
kiwoom_rest.py — 키움 REST API / WebSocket 통신 계층

키움 OpenAPI+ OCX 를 대체한다. 매매 로직은 kiwoom_api.py 에 그대로 있고,
여기서는 "요청 보내고 응답 받기"만 담당한다.
Qt 에 의존하지 않으므로 collect_candles.py 같은 단독 스크립트에서도 쓴다.

[구성]
  KiwoomRestClient : 접근토큰(au10001) 발급/갱신, TR 호출, 연속조회, 호출 제한
  KiwoomWebSocket  : 주문체결(00)·주식체결(0B)·조건검색(CNSR*) 수신,
                     PING 응답, 끊기면 자동 재접속 + 실시간 등록 복구

[OCX → REST 대응표]
  CommConnect                → issue_token() + 웹소켓 LOGIN
  OPT10080 (분봉)            → ka10080  /api/dostk/chart
  OPW00018 (잔고)            → kt00018  /api/dostk/acnt
  OPT10075 (미체결)          → ka10075  /api/dostk/acnt
  SendOrder 매수/매도/취소   → kt10000 / kt10001 / kt10003  /api/dostk/ordr
  GetMasterCodeName          → ka10099  /api/dostk/stkinfo (시작 시 한 번 받아 캐시)
  SetRealReg (주식체결)      → 웹소켓 REG type=0B
  OnReceiveChejanData        → 웹소켓 REAL type=00 (FID 번호는 OCX 와 동일)
  GetConditionLoad           → 웹소켓 CNSRLST
  SendCondition(실시간)      → 웹소켓 CNSRREQ search_type=1, 편입/이탈은 REAL type=02

[주의]
  필드명과 호출 제한은 키움 REST API 가이드(https://openapi.kiwoom.com) 기준이다.
  키움이 바꿀 수 있으므로 모의투자에서 logs/system.log 로 먼저 확인할 것.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

import requests
import websocket   # websocket-client


# ==================================================
# 앱키 로드
# ==================================================
KEYS_FILE = "kiwoom_keys.json"


def load_app_keys(is_real: bool) -> tuple[str, str]:
    """
    앱키/시크릿키를 읽는다. 코드나 config.py 에 직접 적지 않는다(깃에 올라감).

    우선순위
      1) 환경변수 KIWOOM_APP_KEY / KIWOOM_SECRET_KEY
      2) 프로젝트 폴더의 kiwoom_keys.json (.gitignore 대상)
            {"mock": {"appkey": "...", "secretkey": "..."},
             "real": {"appkey": "...", "secretkey": "..."}}
    모의투자와 실전투자는 앱키가 따로 발급된다.
    """
    env_key = os.environ.get("KIWOOM_APP_KEY", "").strip()
    env_sec = os.environ.get("KIWOOM_SECRET_KEY", "").strip()
    if env_key and env_sec:
        return env_key, env_sec

    section = "real" if is_real else "mock"
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), KEYS_FILE)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sec = data.get(section) or {}
        key, secret = str(sec.get("appkey", "")).strip(), str(sec.get("secretkey", "")).strip()
        if key and secret:
            return key, secret

    raise RuntimeError(
        f"키움 REST 앱키가 없습니다. {KEYS_FILE} 의 \"{section}\" 항목 또는 "
        f"환경변수 KIWOOM_APP_KEY / KIWOOM_SECRET_KEY 를 설정하세요. "
        f"(kiwoom_keys.example.json 참고)"
    )


# ==================================================
# 값 변환 유틸
# ==================================================
def to_int(val) -> int:
    """키움 문자열 숫자("+12345", "-1,234", "") → int. 변환 불가면 0."""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().replace(",", "")
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return 0


def norm_code(code) -> str:
    """'A005930' / '005930_NX' / ' 005930 ' → '005930'"""
    s = str(code or "").strip()
    if s[:1] in ("A", "J", "Q") and s[1:7].isdigit():
        s = s[1:]
    for suffix in ("_NX", "_AL"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


# ==================================================
# 예외
# ==================================================
class KiwoomError(Exception):
    """키움 서버가 return_code != 0 으로 거절함 (요청은 처리되지 않았음)"""

    def __init__(self, code, msg, data=None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.data = data or {}


class KiwoomRateLimitError(KiwoomError):
    """호출 제한(HTTP 429 / return_code 5)이 재시도 후에도 풀리지 않음"""


class KiwoomTransportError(Exception):
    """
    네트워크 오류. maybe_sent=True 이면 서버가 요청을 받았을 수도 있다.
    주문에서 이 경우는 '접수됐을 수 있음'으로 다뤄야 한다 (재전송 금지).
    """

    def __init__(self, msg, maybe_sent: bool):
        super().__init__(msg)
        self.maybe_sent = maybe_sent


# ==================================================
# 호출 제한 (TR 별 토큰 버킷)
# ==================================================
class _TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate = max(0.01, float(rate))
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(wait)


# ==================================================
# REST 클라이언트
# ==================================================
class KiwoomRestClient:
    REAL_URL = "https://api.kiwoom.com"
    MOCK_URL = "https://mockapi.kiwoom.com"

    # return_code 의미 (키움 응답 기준)
    RC_AUTH_FAIL  = 3     # 토큰 무효/만료 ("8005:Token이 유효하지 않습니다")
    RC_RATE_LIMIT = 5     # 허용된 요청 개수 초과

    def __init__(self, appkey: str, secretkey: str, is_real: bool,
                 rate_per_sec: float = 1.0, burst: int = 2,
                 timeout: float = 5.0, exchange: str = "KRX", log=None):
        self.appkey = appkey
        self.secretkey = secretkey
        self.is_real = is_real
        self.base_url = self.REAL_URL if is_real else self.MOCK_URL
        self.timeout = timeout
        self.exchange = exchange
        self.log = log

        self._rate = rate_per_sec
        self._burst = burst
        self._buckets: dict[str, _TokenBucket] = {}
        self._buckets_lock = threading.Lock()

        self._token = None
        self._token_exp = 0.0
        self._token_lock = threading.Lock()
        self._local = threading.local()     # 스레드별 requests.Session

    # ── 내부 ─────────────────────────────────────────
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            self._local.session = s
        return s

    def _bucket(self, api_id: str) -> _TokenBucket:
        with self._buckets_lock:
            b = self._buckets.get(api_id)
            if b is None:
                b = _TokenBucket(self._rate, self._burst)
                self._buckets[api_id] = b
            return b

    def _warn(self, msg):
        if self.log:
            self.log.warning(msg)

    # ── 접근토큰 (au10001) ───────────────────────────
    def issue_token(self) -> str:
        with self._token_lock:
            return self._issue_token_locked()

    def _issue_token_locked(self) -> str:
        try:
            resp = self._session().post(
                self.base_url + "/oauth2/token",
                json={"grant_type": "client_credentials",
                      "appkey": self.appkey, "secretkey": self.secretkey},
                headers={"Content-Type": "application/json;charset=UTF-8"},
                timeout=self.timeout,
            )
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise KiwoomTransportError(f"토큰 발급 통신 실패: {e}", maybe_sent=False)

        token = data.get("token") or data.get("access_token")
        if to_int(data.get("return_code", 0)) != 0 or not token:
            raise KiwoomError(data.get("return_code"), data.get("return_msg", "토큰 발급 실패"), data)

        exp = 0.0
        exp_dt = str(data.get("expires_dt", "")).strip()
        if exp_dt:
            try:
                exp = datetime.strptime(exp_dt, "%Y%m%d%H%M%S").timestamp()
            except ValueError:
                exp = 0.0
        if not exp:
            exp = time.time() + 23 * 3600     # 만료시각 미제공 시 보수적으로 23시간
        self._token, self._token_exp = token, exp
        return token

    def get_token(self) -> str:
        """유효한 토큰. 만료 10분 전이면 새로 발급한다."""
        with self._token_lock:
            if not self._token or time.time() >= self._token_exp - 600:
                self._issue_token_locked()
            return self._token

    def invalidate_token(self):
        with self._token_lock:
            self._token = None
            self._token_exp = 0.0

    # ── TR 호출 ──────────────────────────────────────
    def request(self, api_id: str, path: str, body: dict,
                cont_yn: str = "N", next_key: str = "", retries: int = 3):
        """
        TR 1회 호출. 반환: (응답 dict, cont_yn, next_key)
        - 호출 제한(429 / rc=5) : 대기 후 재시도 (서버가 처리하지 않은 요청이라 안전)
        - 토큰 무효(401 / rc=3) : 토큰 재발급 후 1회 재시도
        - 그 외 rc != 0         : KiwoomError
        - 네트워크 오류         : KiwoomTransportError (재시도하지 않음 — 주문 중복 방지)
        """
        token_retried = False
        for attempt in range(retries + 1):
            self._bucket(api_id).acquire()
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {self.get_token()}",
                "api-id": api_id,
                "cont-yn": cont_yn,
                "next-key": next_key,
            }
            try:
                resp = self._session().post(self.base_url + path, json=body,
                                            headers=headers, timeout=self.timeout)
            except requests.exceptions.ConnectTimeout as e:
                raise KiwoomTransportError(f"{api_id} 접속 시간초과: {e}", maybe_sent=False)
            except requests.RequestException as e:
                raise KiwoomTransportError(f"{api_id} 통신 오류: {e}", maybe_sent=True)

            try:
                data = resp.json()
            except ValueError:
                data = {}

            rc = to_int(data.get("return_code", 0))
            msg = str(data.get("return_msg", "")).strip()

            if resp.status_code == 429 or rc == self.RC_RATE_LIMIT:
                if attempt < retries:
                    wait = 1.1 * (attempt + 1)
                    self._warn(f"[REST_RATE_LIMIT] {api_id} 호출 제한 → {wait:.1f}초 후 재시도 ({attempt + 1}회차)")
                    time.sleep(wait)
                    continue
                raise KiwoomRateLimitError(rc or 429, msg or "호출 제한 초과", data)

            if (resp.status_code == 401 or rc == self.RC_AUTH_FAIL) and not token_retried:
                token_retried = True
                self._warn(f"[REST_TOKEN_RETRY] {api_id} 토큰 무효 → 재발급 후 재시도 ({msg})")
                self.invalidate_token()
                continue

            if rc != 0:
                raise KiwoomError(rc, msg or f"HTTP {resp.status_code}", data)
            if resp.status_code >= 400 or not data:
                raise KiwoomError(resp.status_code, msg or f"HTTP {resp.status_code} 응답 오류", data)

            return (data,
                    resp.headers.get("cont-yn", "N") or "N",
                    resp.headers.get("next-key", "") or "")

        raise KiwoomRateLimitError(429, "호출 제한 초과", {})

    def request_all(self, api_id: str, path: str, body: dict, list_key: str,
                    max_pages: int = 20) -> list:
        """연속조회(cont-yn=Y)를 끝까지 따라가며 list_key 배열을 합쳐 반환."""
        rows: list = []
        cont, nkey = "N", ""
        for _ in range(max_pages):
            data, cont, nkey = self.request(api_id, path, body, cont, nkey)
            rows.extend(data.get(list_key) or [])
            if cont != "Y" or not nkey:
                return rows
        self._warn(f"[REST_PAGE_LIMIT] {api_id} {max_pages}페이지 초과 - 이후 생략")
        return rows

    # ── 시세 ─────────────────────────────────────────
    def minute_chart(self, code: str, tic_scope: str, cont_yn: str = "N", next_key: str = ""):
        """ka10080 주식분봉차트 → (응답, cont_yn, next_key). 봉 배열 키: stk_min_pole_chart_qry (최신→과거)"""
        return self.request("ka10080", "/api/dostk/chart",
                            {"stk_cd": code, "tic_scope": str(tic_scope), "upd_stkpc_tp": "1"},
                            cont_yn, next_key)

    def stock_list(self, market: str) -> list:
        """ka10099 종목정보 리스트. market: '0'=코스피, '10'=코스닥. 항목: code, name, marketCode ..."""
        return self.request_all("ka10099", "/api/dostk/stkinfo", {"mrkt_tp": market}, "list")

    # ── 계좌 ─────────────────────────────────────────
    def holdings(self) -> list:
        """kt00018 계좌평가잔고내역 (합산). 항목: stk_cd, stk_nm, rmnd_qty, trde_able_qty, pur_pric ..."""
        return self.request_all("kt00018", "/api/dostk/acnt",
                                {"qry_tp": "1", "dmst_stex_tp": self.exchange},
                                "acnt_evlt_remn_indv_tot")

    def open_orders(self) -> list:
        """ka10075 미체결 (전 종목). 항목: ord_no, stk_cd, oso_qty, io_tp_nm(주문구분) ..."""
        return self.request_all("ka10075", "/api/dostk/acnt",
                                {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"},
                                "oso")

    # ── 주문 ─────────────────────────────────────────
    def send_order(self, side: str, code: str, qty: int, trde_tp: str, price: int = 0) -> str:
        """
        kt10000 매수 / kt10001 매도. 반환: 주문번호
        trde_tp: '0'=보통(지정가), '3'=시장가
        """
        api_id = "kt10000" if side == "BUY" else "kt10001"
        body = {
            "dmst_stex_tp": self.exchange,
            "stk_cd": code,
            "ord_qty": str(int(qty)),
            "ord_uv": str(int(price)) if trde_tp == "0" else "",
            "trde_tp": trde_tp,
            "cond_uv": "",
        }
        data, _, _ = self.request(api_id, "/api/dostk/ordr", body)
        return str(data.get("ord_no", "")).strip()

    def cancel_order(self, code: str, orig_ord_no: str, qty: int = 0) -> str:
        """kt10003 취소. qty=0 이면 잔량 전부 취소. 반환: 취소 주문번호"""
        body = {
            "dmst_stex_tp": self.exchange,
            "orig_ord_no": str(orig_ord_no),
            "stk_cd": code,
            "cncl_qty": str(int(qty)),
        }
        data, _, _ = self.request("kt10003", "/api/dostk/ordr", body)
        return str(data.get("ord_no", "")).strip()


# ==================================================
# 웹소켓 클라이언트
# ==================================================
class KiwoomWebSocket:
    """
    전용 스레드에서 수신한다. on_message(dict) / on_state(state, detail) 는
    그 스레드에서 호출되므로, 받는 쪽이 자기 스레드로 넘겨서 처리해야 한다.
    (kiwoom_api.py 는 Qt 메인 스레드로 넘긴다)

    on_state 의 state: "LOGIN_OK" | "LOGIN_FAIL" | "DISCONNECTED"
    """

    REAL_URL = "wss://api.kiwoom.com:10000/api/dostk/websocket"
    MOCK_URL = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"

    def __init__(self, rest: KiwoomRestClient, on_message, on_state=None,
                 log=None, silence_sec: float = 180.0, cond_stex_tp: str = "K"):
        self.rest = rest
        self.url = self.REAL_URL if rest.is_real else self.MOCK_URL
        self.on_message = on_message
        self.on_state = on_state
        self.log = log
        self.silence_sec = silence_sec
        self.cond_stex_tp = cond_stex_tp

        self._ws = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._logged_in = False
        self._stop = threading.Event()
        self._thread = None

        # 재접속 시 복구할 등록 상태
        self._regs: dict[str, dict] = {}       # grp_no → {"types": [..], "items": [..]}
        self._cond_realtime: list[str] = []    # 실시간 등록한 조건식 seq

    # ── 공개 API ─────────────────────────────────────
    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kiwoom-ws", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def send(self, payload: dict) -> bool:
        """로그인된 상태면 전송. 실패/미접속이면 False (등록류는 재접속 시 복구됨)."""
        ws = self._ws
        if ws is None or not self._logged_in:
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as e:
            self._log("warning", f"[WS_SEND_FAIL] {payload.get('trnm')} {e}")
            return False

    def register(self, grp_no: str, types: list, items: list) -> bool:
        """실시간 등록 (기존 등록 유지). 접속 전이면 기억해 두었다가 로그인 후 등록."""
        with self._state_lock:
            reg = self._regs.setdefault(grp_no, {"types": [], "items": []})
            for t in types:
                if t not in reg["types"]:
                    reg["types"].append(t)
            for it in items:
                if it not in reg["items"]:
                    reg["items"].append(it)
        return self.send({"trnm": "REG", "grp_no": grp_no, "refresh": "1",
                          "data": [{"item": list(items), "type": list(types)}]})

    def condition_list(self) -> bool:
        return self.send({"trnm": "CNSRLST"})

    def condition_request(self, seq: str, realtime: bool = True) -> bool:
        if realtime:
            with self._state_lock:
                if seq not in self._cond_realtime:
                    self._cond_realtime.append(seq)
        return self.send({"trnm": "CNSRREQ", "seq": str(seq),
                          "search_type": "1" if realtime else "0",
                          "stex_tp": self.cond_stex_tp, "cont_yn": "N", "next_key": ""})

    def condition_clear(self, seq: str) -> bool:
        with self._state_lock:
            if seq in self._cond_realtime:
                self._cond_realtime.remove(seq)
        return self.send({"trnm": "CNSRCLR", "seq": str(seq)})

    # ── 내부 ─────────────────────────────────────────
    def _log(self, level, msg):
        if self.log:
            getattr(self.log, level)(msg)

    def _emit_state(self, state, detail=""):
        if self.on_state:
            try:
                self.on_state(state, detail)
            except Exception as e:
                self._log("error", f"[WS_STATE_CB_ERROR] {e}")

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect_and_login()
                backoff = 1.0
                self._restore()
                self._recv_loop()
            except Exception as e:
                if not self._stop.is_set():
                    self._log("warning", f"[WS_ERROR] {type(e).__name__}: {e}")
            finally:
                was_logged_in = self._logged_in
                self._logged_in = False
                ws, self._ws = self._ws, None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                if was_logged_in and not self._stop.is_set():
                    self._emit_state("DISCONNECTED", "웹소켓 연결 끊김")

            if self._stop.wait(backoff):
                break
            self._log("info", f"[WS_RECONNECT] {backoff:.0f}초 대기 후 재접속")
            backoff = min(backoff * 2, 30.0)

    def _connect_and_login(self):
        self._ws = websocket.create_connection(self.url, timeout=10)
        with self._send_lock:
            self._ws.send(json.dumps({"trnm": "LOGIN", "token": self.rest.get_token()}))

        deadline = time.time() + 10
        while time.time() < deadline:
            raw = self._ws.recv()
            data = json.loads(raw) if raw else {}
            trnm = data.get("trnm")
            if trnm == "PING":
                with self._send_lock:
                    self._ws.send(raw)
                continue
            if trnm != "LOGIN":
                continue
            if to_int(data.get("return_code", 0)) != 0:
                # 토큰 문제일 가능성이 높으니 다음 재접속은 새 토큰으로
                self.rest.invalidate_token()
                self._emit_state("LOGIN_FAIL", str(data.get("return_msg", "")))
                raise RuntimeError(f"LOGIN 거절: {data.get('return_msg')}")
            self._logged_in = True
            self._log("info", "[WS_LOGIN_OK] 웹소켓 로그인 성공")
            self._emit_state("LOGIN_OK")
            return
        raise RuntimeError("LOGIN 응답 시간초과")

    def _restore(self):
        """재접속 후 실시간 등록 / 실시간 조건검색 복구"""
        with self._state_lock:
            regs = {g: {"types": list(r["types"]), "items": list(r["items"])}
                    for g, r in self._regs.items()}
            conds = list(self._cond_realtime)
        for grp_no, r in regs.items():
            if r["types"] and r["items"]:
                self.send({"trnm": "REG", "grp_no": grp_no, "refresh": "1",
                           "data": [{"item": r["items"], "type": r["types"]}]})
        if conds:
            # 조건식은 세션 단위 등록이라 목록 조회부터 다시 해야 한다
            self.send({"trnm": "CNSRLST"})
            time.sleep(0.5)
            for seq in conds:
                self.send({"trnm": "CNSRREQ", "seq": seq, "search_type": "1",
                           "stex_tp": self.cond_stex_tp, "cont_yn": "N", "next_key": ""})
                time.sleep(0.3)
            self._log("info", f"[WS_RESTORE] 실시간 조건검색 재등록 seq={conds}")

    def _recv_loop(self):
        self._ws.settimeout(5)
        last_rx = time.time()
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                if time.time() - last_rx > self.silence_sec:
                    raise RuntimeError(f"{self.silence_sec:.0f}초 동안 수신 없음 → 재접속")
                continue
            if not raw:
                raise RuntimeError("서버가 연결을 닫음")
            last_rx = time.time()
            try:
                data = json.loads(raw)
            except ValueError:
                self._log("warning", f"[WS_BAD_JSON] {raw[:200]!r}")
                continue

            # PING 은 받은 그대로 돌려보내야 서버가 연결을 유지한다
            if data.get("trnm") == "PING":
                with self._send_lock:
                    self._ws.send(raw)
                continue

            try:
                self.on_message(data)
            except Exception as e:
                self._log("error", f"[WS_MSG_CB_ERROR] {type(e).__name__}: {e}")
