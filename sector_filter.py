"""
sector_filter.py  ―  인포스틱(인포스탁) 섹터 기반 강세 종목 필터

【사용 방법】
  1. 로그인 완료 후 SectorFilter.initialize() 호출
       → GetThemeGroupList() 로 전체 섹터 ID·이름 확보 (동기, 즉시 반환)
       → 각 섹터별 GetThemeGroupCode(id) 로 구성 종목 로드

  2. 장중 주기적으로 refresh_sector_status() 호출 (권장 5~10분)
       → OPT90001 TR 요청 → 섹터별 등락률 수신 → 강세 섹터 갱신
       (OPT90001 은 조건검색이 아닌 일반 TR, CommRqData 로 요청)

  3. kiwoom_api._on_receive_tr_data 에서 rq_name == "RQ_SECTOR" 일 때
       sector_filter.on_tr_sector(screen_no) 를 호출

  4. 진입 후보 종목에 대해 classify_code() 로 분류
       SECTOR_HOT  : 강세 섹터 종목 → 완화된 진입 조건 적용
       SECTOR_ONLY : 섹터 종목(강세 아님) → 기존 조건
       NORMAL      : 섹터 미포함   → 기존 조건
"""

from __future__ import annotations
import time as pytime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiwoom_api import KiwoomAPI

# ── 설정값 ────────────────────────────────────────────
# 강세 섹터 판정 기준
HOT_SECTOR_MIN_RISE  = 1.5    # 섹터 평균 등락률 (%) 이상이어야 강세
HOT_SECTOR_TOP_N     = 5      # 상위 N개 섹터만 강세로 인정
HOT_SECTOR_MIN_CODES = 2      # 구성 종목 2개 이상인 섹터만 인정

# SECTOR_HOT 전용 완화 진입 조건
HOT_MA_SLOPE_MIN  = 0.3    # MA20 기울기 완화 (일반 0.5%)
HOT_MA_GAP_MIN    = 0.3    # MA갭 최소 완화 (일반 0.5%)
HOT_MA_GAP_MAX    = 2.0    # MA갭 최대 완화 (일반 1.5%)
HOT_VOL_MIN       = 3.0    # 거래량 완화 (일반 4배)
HOT_VOL_MAX       = 15.0
HOT_STRENGTH_MIN  = 0.65   # 캔들강도 완화 (일반 0.70)
HOT_PB_MA_DIST    = 0.040  # 풀백 MA거리 완화 (일반 3.5%)

# OPT90001 TR
RQ_SECTOR       = "RQ_SECTOR"
TR_SECTOR       = "OPT90001"
SECTOR_SCREEN   = "8100"

# OPT90001 갱신 최소 간격 (초) - TR 제한 방어
SECTOR_REFRESH_MIN_INTERVAL = 60


@dataclass
class SectorInfo:
    sector_id:   str
    sector_name: str
    codes:       set  = field(default_factory=set)
    change_rate: float = 0.0   # OPT90001 에서 수신한 등락률

    def is_hot(self) -> bool:
        return (
            len(self.codes) >= HOT_SECTOR_MIN_CODES
            and self.change_rate >= HOT_SECTOR_MIN_RISE
        )


class SectorFilter:
    """
    인포스탁 섹터 기반 종목 필터.
    조건검색 없이 GetThemeGroupList / GetThemeGroupCode / OPT90001 만 사용.
    """

    def __init__(self, api: "KiwoomAPI"):
        self.api = api
        self.log = api.log_signal

        # sector_id → SectorInfo
        self.sectors: dict[str, SectorInfo] = {}

        # code → sector_id (종목이 속한 섹터)
        self.code_to_sector: dict[str, str] = {}

        # OPT90001 갱신 시각
        self._last_refresh: float = 0.0
        self._refresh_pending: bool = False

    # ─────────────────────────────────────────────────
    # 초기화
    # ─────────────────────────────────────────────────
    def initialize(self):
        """
        로그인 완료 후 1회 호출.
        GetThemeGroupList() + GetThemeGroupCode() 로 섹터 구성 종목 로드.
        (동기 함수 — 이벤트 대기 없음)

        ※ GetThemeGroupList 는 OCX 완전 초기화 전에 호출하면 빈 값을 반환함.
          섹터가 0개면 초기화 실패로 간주하고 _initialized = False 로 남겨둠.
          ensure_initialized() 가 run_condition_cycle / classify_code 직전에
          자동으로 재시도함.
        """
        self._initialized = False
        self._load_sector_codes()
        if len(self.sectors) == 0:
            self.log.warning(
                "[SECTOR] 초기화 실패 — GetThemeGroupList 빈 응답 "
                "(OCX 준비 전 호출 가능성). run_condition_cycle 시 자동 재시도."
            )
        else:
            self._initialized = True
            self.log.info(
                f"[SECTOR] 초기화 완료 | 섹터={len(self.sectors)}개 "
                f"종목={len(self.code_to_sector)}개"
            )

    def ensure_initialized(self):
        """
        섹터 데이터가 아직 비어 있으면 재시도.
        classify_code / run_condition_cycle 직전에 호출.
        """
        if not getattr(self, "_initialized", False):
            self.log.info("[SECTOR] 섹터 데이터 없음 → 재초기화 시도")
            self.sectors.clear()
            self.code_to_sector.clear()
            self._load_sector_codes()
            if len(self.sectors) > 0:
                self._initialized = True
                self.log.info(
                    f"[SECTOR] 재초기화 성공 | 섹터={len(self.sectors)}개 "
                    f"종목={len(self.code_to_sector)}개"
                )
            else:
                self.log.warning("[SECTOR] 재초기화 실패 — 섹터 데이터 여전히 비어있음")

    def _load_sector_codes(self):
        """
        GetThemeGroupList(1) : "섹터ID^섹터명;섹터ID^섹터명;..."
        GetThemeGroupCode(id): "종목코드 종목코드 ..."  (공백 구분)
        """
        raw_list = self.dynamicCall("GetThemeGroupList(int)", 1) or ""

        for item in raw_list.split(";"):
            item = item.strip()
            if not item or "^" not in item:
                continue
            sector_id, sector_name = item.split("^", 1)
            sector_id   = sector_id.strip()
            sector_name = sector_name.strip()
            if not sector_id:
                continue

            # 구성 종목코드
            codes_raw = self.dynamicCall(
                "GetThemeGroupCode(QString)", sector_id
            ) or ""
            codes = {c.strip() for c in codes_raw.split() if c.strip()}

            si = SectorInfo(
                sector_id=sector_id,
                sector_name=sector_name,
                codes=codes,
            )
            self.sectors[sector_id] = si

            for code in codes:
                # 한 종목이 여러 섹터에 속할 수 있으므로 첫 번째 섹터만 등록
                # (필요 시 멀티 섹터 지원으로 확장 가능)
                self.code_to_sector.setdefault(code, sector_id)

        self.log.info(
            f"[SECTOR] GetThemeGroupList 로드 | "
            f"섹터 {len(self.sectors)}개 / 종목 {len(self.code_to_sector)}개"
        )

    def dynamicCall(self, func: str, *args):
        """api.dynamicCall 위임"""
        return self.api.dynamicCall(func, *args)

    # ─────────────────────────────────────────────────
    # OPT90001 요청 (섹터별 등락률 갱신)
    # ─────────────────────────────────────────────────
    def refresh_sector_status(self):
        """
        OPT90001 TR 요청 — 섹터별 등락률 수신.
        kiwoom_api.py 의 TR 플로우와 충돌하지 않도록:
          - tr_inflight 가 False 일 때만 요청
          - 최소 갱신 간격(SECTOR_REFRESH_MIN_INTERVAL) 준수
        """
        now = pytime.time()
        if now - self._last_refresh < SECTOR_REFRESH_MIN_INTERVAL:
            self.log.info(
                f"[SECTOR] refresh 스킵 (간격 미충족, "
                f"경과={now - self._last_refresh:.0f}s)"
            )
            return

        if getattr(self.api, "tr_inflight", False):
            # 다음 refresh 호출 때 다시 시도
            self._refresh_pending = True
            self.log.info("[SECTOR] tr_inflight 중 → refresh 대기")
            return

        self._refresh_pending = False
        self._last_refresh = now

        self.api.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            RQ_SECTOR, TR_SECTOR, 0, SECTOR_SCREEN
        )
        self.log.info("[SECTOR] OPT90001 요청 발송")

    # ─────────────────────────────────────────────────
    # OPT90001 수신 (kiwoom_api._on_receive_tr_data 에서 위임)
    # ─────────────────────────────────────────────────
    def on_tr_sector(self, screen_no: str):
        """
        OPT90001 응답 파싱.
        kiwoom_api._on_receive_tr_data 에서 rq_name == RQ_SECTOR 일 때 호출.

        OPT90001 출력 필드 (멀티데이터):
          테마명, 테마코드, 등락률, 거래량, 전일거래량비, 시가총액
        """
        try:
            rows = self.api.dynamicCall(
                "GetRepeatCnt(QString, QString)", TR_SECTOR, RQ_SECTOR
            )
            if rows <= 0:
                self.log.warning("[SECTOR] OPT90001 rows=0")
                return

            updated = 0
            for i in range(rows):
                def _get(field_name: str) -> str:
                    return (
                        self.api.dynamicCall(
                            "GetCommData(QString, QString, int, QString)",
                            TR_SECTOR, RQ_SECTOR, i, field_name
                        ) or ""
                    ).strip()

                sector_id   = _get("테마코드")
                sector_name = _get("테마명")
                rate_raw    = _get("등락률")

                try:
                    change_rate = float(rate_raw.replace("+", "").replace("%", ""))
                except ValueError:
                    change_rate = 0.0

                if sector_id in self.sectors:
                    self.sectors[sector_id].change_rate = change_rate
                    updated += 1
                else:
                    # 초기 로드에 없던 섹터도 등록 (방어 코드)
                    codes_raw = self.api.dynamicCall(
                        "GetThemeGroupCode(QString)", sector_id
                    ) or ""
                    codes = {c.strip() for c in codes_raw.split() if c.strip()}
                    si = SectorInfo(
                        sector_id=sector_id,
                        sector_name=sector_name,
                        codes=codes,
                        change_rate=change_rate,
                    )
                    self.sectors[sector_id] = si
                    for code in codes:
                        self.code_to_sector.setdefault(code, sector_id)
                    updated += 1

            hot = self.get_hot_sectors()
            self.log.info(
                f"[SECTOR] OPT90001 수신 완료 | "
                f"갱신={updated}섹터 강세={[s.sector_name for s in hot]}"
            )

        except Exception as e:
            self.log.error(f"[SECTOR] on_tr_sector 오류: {e}")

    # ─────────────────────────────────────────────────
    # 강세 섹터 조회
    # ─────────────────────────────────────────────────
    def get_hot_sectors(self) -> list[SectorInfo]:
        """등락률 높은 순 상위 N개 강세 섹터 반환"""
        candidates = [si for si in self.sectors.values() if si.is_hot()]
        candidates.sort(key=lambda s: s.change_rate, reverse=True)
        return candidates[:HOT_SECTOR_TOP_N]

    def get_hot_sector_codes(self) -> set[str]:
        """강세 섹터에 속한 종목코드 전체 집합"""
        codes: set[str] = set()
        for si in self.get_hot_sectors():
            codes |= si.codes
        return codes

    def get_sector_of(self, code: str) -> SectorInfo | None:
        """종목이 속한 섹터 반환"""
        sid = self.code_to_sector.get(code)
        return self.sectors.get(sid) if sid else None

    # ─────────────────────────────────────────────────
    # 종목 분류
    # ─────────────────────────────────────────────────
    def classify_code(self, code: str) -> str:
        """
        SECTOR_HOT  : 강세 섹터 포함 → 완화 조건
        SECTOR_ONLY : 섹터 포함 (강세 아님)
        NORMAL      : 섹터 미포함
        """
        self.ensure_initialized()
        si = self.get_sector_of(code)
        if si is None:
            return "NORMAL"
        if si.is_hot():
            return "SECTOR_HOT"
        return "SECTOR_ONLY"

    # ─────────────────────────────────────────────────
    # SECTOR_HOT 전용 완화 진입 조건
    # ─────────────────────────────────────────────────
    def is_sector_hot_entry(
        self, candles: list, logger=None, code: str = ""
    ) -> str | None:
        """
        강세 섹터 종목 전용 완화 진입 판단.
        BREAKOUT / PULLBACK / None 반환.
        """
        if len(candles) < 25:
            return None

        c1 = candles[0]
        c2 = candles[1]

        ma20_now   = sum(c["close"] for c in candles[0:20]) / 20
        ma20_prev  = sum(c["close"] for c in candles[5:25]) / 20
        slope_pct  = (ma20_now - ma20_prev) / ma20_prev * 100
        ma_gap_pct = (c1["close"] - ma20_now) / ma20_now * 100

        avg_vol   = sum(c["volume"] for c in candles[1:6]) / 5
        vol_ratio = c1["volume"] / avg_vol if avg_vol > 0 else 0

        candle_range = c1["high"] - c1["low"]
        body_size    = c1["close"] - c1["open"]
        strength     = (body_size / candle_range) if candle_range > 0 else 0

        trend_ok    = c1["close"] > ma20_now and slope_pct >= HOT_MA_SLOPE_MIN
        ma_gap_ok   = HOT_MA_GAP_MIN <= ma_gap_pct <= HOT_MA_GAP_MAX
        vol_ok      = HOT_VOL_MIN <= vol_ratio <= HOT_VOL_MAX and c1["volume"] >= 3000
        strength_ok = strength >= HOT_STRENGTH_MIN
        prev_5_high = max(c["high"] for c in candles[1:6])
        price_ok    = c1["close"] > prev_5_high and c1["close"] > c1["open"]
        c2_bull     = c2["close"] > c2["open"]

        # ── BREAKOUT 판단 ──
        if trend_ok and ma_gap_ok and price_ok and vol_ok and strength_ok and c2_bull:
            if logger:
                logger.info(
                    f"[SECTOR_HOT_BREAKOUT] {code} | "
                    f"MA갭:{ma_gap_pct:.2f}% 기울기+{slope_pct:.2f}% "
                    f"거량:{vol_ratio:.1f}배 강도:{strength*100:.0f}%"
                )
            return "BREAKOUT"

        # ── PULLBACK 판단 ──
        recent_high  = max(c["high"] for c in candles[0:10])
        pullback_pct = (c1["close"] - recent_high) / recent_high
        ma_distance  = abs(c1["close"] - ma20_now) / ma20_now
        high_above   = recent_high > ma20_now * 1.02
        pb_ok        = -0.10 <= pullback_pct <= -0.01
        near_ok      = ma_distance <= HOT_PB_MA_DIST
        bounce_ok    = c1["close"] > c2["close"] and c1["close"] > c1["open"]

        if (trend_ok and high_above and pb_ok and near_ok
                and bounce_ok and vol_ok and strength >= 0.55):
            if logger:
                logger.info(
                    f"[SECTOR_HOT_PULLBACK] {code} | "
                    f"눌림:{pullback_pct*100:.1f}% MA거리:{ma_distance*100:.2f}% "
                    f"거량:{vol_ratio:.1f}배 강도:{strength*100:.0f}%"
                )
            return "PULLBACK"

        if logger:
            logger.info(
                f"[SECTOR_HOT_SKIP] {code} "
                f"trend={trend_ok}(slope={slope_pct:.2f}%) "
                f"ma_gap={ma_gap_ok}({ma_gap_pct:.2f}%) "
                f"vol={vol_ok}({vol_ratio:.1f}배) "
                f"strength={strength*100:.0f}%"
            )
        return None

    # ─────────────────────────────────────────────────
    # 상태 요약
    # ─────────────────────────────────────────────────
    def status_summary(self) -> str:
        hot = self.get_hot_sectors()
        hot_codes = self.get_hot_sector_codes()
        since = int(pytime.time() - self._last_refresh)
        return (
            f"섹터={len(self.sectors)}개 "
            f"종목={len(self.code_to_sector)}개 | "
            f"강세섹터={[s.sector_name for s in hot]} | "
            f"강세종목={len(hot_codes)}개 | "
            f"마지막갱신={since}초전"
        )
