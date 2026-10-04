#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_candles.py — 백테스트용 분봉 데이터 수집기 (키움 OpenAPI+)

OPT10080(분봉차트조회)으로 종목별 분봉을 연속조회하여
candle_data/YYYY-MM-DD/<code>.json 으로 저장한다.

반드시 32비트 파이썬으로 실행 (키움 OCX는 32비트 전용):
    C:\\Users\\user\\AppData\\Local\\Programs\\Python\\Python311-32\\python.exe collect_candles.py

Usage:
    python collect_candles.py                      # 조건검색식 종목 수집
    python collect_candles.py --codes 405100,318160
    python collect_candles.py --codes-file codes.txt
    python collect_candles.py --days 10            # 최대 10영업일치

키움 제약:
  - TR 요청은 초당 5회 제한 → 요청 간 딜레이 필수 (기본 3.6초)
  - OPT10080 1회 응답 최대 900봉, prev_next=2 로 연속조회
  - 과거 데이터 보관 기간은 종목/서버 상황에 따라 다름
"""

import os, sys, json, time, argparse
from datetime import datetime
from collections import defaultdict

import qt_bootstrap  # noqa: F401  (PyQt5 보다 먼저 - 한글 경로 대응)
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QEventLoop, QTimer

from config import CANDLE_INTERVAL_MIN, CONDITION_NAMES

TR_DELAY_SEC = 3.6      # 초당 5회 제한 대비 여유 (1200회/시간 제한도 고려)
MAX_PAGES    = 12       # 종목당 최대 연속조회 페이지 (900봉 × 12 ≈ 넉넉)
OUT_ROOT     = 'candle_data'


class CandleCollector(QAxWidget):
    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")

        self.login_loop = None
        self.tr_loop    = None
        self.cond_loop  = None

        self.rows        = []       # 누적 캔들 (최신→과거)
        self.has_next    = False
        self.cur_code    = None
        self.conditions  = {}       # name → index

        self.OnEventConnect.connect(self._on_login)
        self.OnReceiveTrData.connect(self._on_tr)
        self.OnReceiveConditionVer.connect(self._on_cond_ver)
        self.OnReceiveTrCondition.connect(self._on_tr_condition)

    # ── 로그인 ──
    def login(self):
        self.dynamicCall("CommConnect()")
        self.login_loop = QEventLoop()
        self.login_loop.exec_()

    def _on_login(self, err):
        if err == 0:
            print("[로그인] 성공")
        else:
            print(f"[로그인] 실패 err={err}")
            sys.exit(1)
        if self.login_loop:
            self.login_loop.exit()

    # ── 조건검색식 ──
    def load_conditions(self):
        self.dynamicCall("GetConditionLoad()")
        self.cond_loop = QEventLoop()
        self.cond_loop.exec_()

    def _on_cond_ver(self, ret, msg):
        raw = self.dynamicCall("GetConditionNameList()")
        for item in raw.split(';'):
            if '^' in item:
                idx, name = item.split('^')
                self.conditions[name] = int(idx)
        print(f"[조건식] {len(self.conditions)}개 로드")
        if self.cond_loop:
            self.cond_loop.exit()

    # ── 시장 전체 종목 ──
    def get_market_codes(self, market):
        """market: '0'=코스피, '10'=코스닥"""
        raw = self.dynamicCall("GetCodeListByMarket(QString)", market)
        return [c for c in raw.split(';') if c]

    def is_tradable(self, code):
        """
        백테스트에 무의미한 종목 제외.
        ETF/ETN/스팩/리츠/우선주 는 전략이 절대 잡지 않으므로
        수집 시간(종목당 40초)을 낭비할 이유가 없다.
        """
        from candle_recorder import fix_korean
        name = fix_korean(
            self.dynamicCall("GetMasterCodeName(QString)", code).strip())
        if not name:
            return False, ''
        bad = ('ETN', 'ETF', '스팩', '리츠', 'KODEX', 'TIGER',
               'KBSTAR', 'ARIRANG', 'HANARO', 'PLUS', 'ACE ', 'SOL ')
        if any(b in name.upper() or b in name for b in bad):
            return False, name
        if code[-1] != '0':          # 우선주(5/7/9 등)
            return False, name
        return True, name

    def get_condition_codes(self, name):
        if name not in self.conditions:
            print(f"[조건식] '{name}' 없음 — 스킵")
            return []
        self._cond_codes = []
        self.dynamicCall("SendCondition(QString, QString, int, int)",
                         "9100", name, self.conditions[name], 0)
        self.cond_loop = QEventLoop()
        QTimer.singleShot(5000, self.cond_loop.quit)   # 5초 타임아웃
        self.cond_loop.exec_()
        return self._cond_codes

    def _on_tr_condition(self, screen, code_list, cond_name, cond_idx, prev_next):
        self._cond_codes = [c for c in code_list.split(';') if c]
        print(f"[조건식] '{cond_name}' → {len(self._cond_codes)}종목")
        if self.cond_loop:
            self.cond_loop.exit()

    # ── 분봉 조회 ──
    def fetch_candles(self, code, max_pages=MAX_PAGES):
        """OPT10080 연속조회 → 캔들 리스트(최신→과거)"""
        self.rows     = []
        self.cur_code = code
        prev_next = 0

        for page in range(max_pages):
            self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
            self.dynamicCall("SetInputValue(QString, QString)",
                             "틱범위", str(CANDLE_INTERVAL_MIN))
            self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")
            self.dynamicCall("CommRqData(QString, QString, int, QString)",
                             "RQ_CANDLE", "OPT10080", prev_next, "9200")

            self.tr_loop = QEventLoop()
            QTimer.singleShot(10000, self.tr_loop.quit)   # 10초 타임아웃
            self.tr_loop.exec_()

            if not self.has_next:
                break
            prev_next = 2
            time.sleep(TR_DELAY_SEC)

        return self.rows

    def _on_tr(self, screen, rq_name, tr_code, record, prev_next, *args):
        if tr_code != "OPT10080" or rq_name != "RQ_CANDLE":
            return

        cnt = self.dynamicCall("GetRepeatCnt(QString, QString)", tr_code, rq_name)

        def gd(i, field):
            return self.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                tr_code, rq_name, i, field).strip()

        for i in range(cnt):
            try:
                o = abs(int(gd(i, "시가")))
                h = abs(int(gd(i, "고가")))
                l = abs(int(gd(i, "저가")))
                c = abs(int(gd(i, "현재가")))
                v = abs(int(gd(i, "거래량")))
                ts = gd(i, "체결시간")        # YYYYMMDDHHMMSS
            except (ValueError, TypeError):
                continue
            if o == 0 or c == 0 or len(ts) < 12:
                continue
            self.rows.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v})

        self.has_next = (str(prev_next) == '2')
        if self.tr_loop:
            self.tr_loop.exit()


def glob_days(out_root):
    """저장 폴더 안의 날짜 디렉터리 목록"""
    if not os.path.isdir(out_root):
        return []
    return [os.path.join(out_root, d) for d in os.listdir(out_root)
            if os.path.isdir(os.path.join(out_root, d))]


def coverage(out_root, min_span=40, min_ratio=0.95):
    """
    종목별 수집 상태를 파일 이름만으로 판정한다 (JSON 은 열지 않음).

    반환: (complete, present)
      present : 파일이 하루라도 있는 종목
      complete: 매일치가 갖춰진 종목. [첫 날짜, 마지막 날짜] 구간의 거래일 중
                파일 보유율 ≥ min_ratio 이고 구간이 min_span 거래일 이상.

    실전 녹화(candle_recorder)는 조건검색에 걸린 날에만 파일을 만든다.
    "파일이 있다"로 건너뛰면 녹화로 며칠치만 있는 급등주가 영원히
    일괄수집되지 않으므로 complete 기준으로 건너뛴다.
    """
    dates = sorted(os.path.basename(d) for d in glob_days(out_root))
    didx = {d: k for k, d in enumerate(dates)}
    by_code = defaultdict(set)
    for d in dates:
        for fn in os.listdir(os.path.join(out_root, d)):
            if fn.endswith('.json'):
                by_code[fn[:-5]].add(d)

    complete = set()
    for code, ds in by_code.items():
        lo, hi = didx[min(ds)], didx[max(ds)]
        span = hi - lo + 1
        if span >= min_span and len(ds) / span >= min_ratio:
            complete.add(code)
    return complete, set(by_code)


def save_by_day(code, name, rows, out_root=OUT_ROOT, keep_days=None):
    """캔들(최신→과거)을 날짜별로 분리해 저장. 반환: 저장된 날짜 수"""
    by_day = defaultdict(list)
    for r in rows:
        ts = r['ts']                      # YYYYMMDDHHMMSS
        day = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        by_day[day].append({'t': ts[8:12], 'o': r['o'], 'h': r['h'],
                            'l': r['l'], 'c': r['c'], 'v': r['v']})

    days = sorted(by_day.keys(), reverse=True)
    if keep_days:
        days = days[:keep_days]

    saved = 0
    for day in days:
        bars = sorted(by_day[day], key=lambda b: b['t'])   # 과거→최신
        if len(bars) < 40:            # 장중 데이터가 너무 적으면 스킵
            continue
        ddir = os.path.join(out_root, day)
        os.makedirs(ddir, exist_ok=True)
        with open(os.path.join(ddir, f"{code}.json"), 'w', encoding='utf-8') as f:
            json.dump({'code': code, 'name': name, 'date': day,
                       'interval_min': CANDLE_INTERVAL_MIN,
                       'candles': bars}, f, ensure_ascii=False)
        saved += 1
    return saved


def _ask(prompt, choices):
    """
    한 글자 선택. 콘솔에서는 배치의 choice 처럼 키 하나만 누르면 넘어간다.
    (Enter 를 기다리게 하면 창이 멈춘 것처럼 보인다)
    파이프로 입력이 들어오는 경우에는 한 줄씩 읽는다.
    """
    getch = None
    try:
        if sys.stdin.isatty():
            import msvcrt
            getch = msvcrt.getwch
    except Exception:
        getch = None

    while True:
        print(prompt, end='', flush=True)
        if getch:
            ch = getch()
            if ch == '\x03':
                raise KeyboardInterrupt
            ans = ch.strip().upper()
            print(ans)
        else:
            line = sys.stdin.readline()
            if not line:
                return None
            ans = line.strip().upper()
        if ans in choices:
            return ans
        print(f"  {'/'.join(choices)} 중에서 고르세요.")


def run_menu(args):
    """1_Collect_Data.bat 의 대화형 메뉴. 취소하면 False."""
    line = "=" * 60
    print(f"{line}\n  분봉 데이터 수집\n{line}\n")
    print("  [확인] main.py 가 종료되어 있습니까?")
    print("         키움은 PC당 세션 1개만 허용하고 TR 제한도 계좌 단위라,")
    print("         실행 중이면 자동매매의 스캔/손절 주문이 밀립니다.\n")
    if _ask("  main.py 를 종료했습니다. 계속할까요 [Y/N]: ", ('Y', 'N')) != 'Y':
        return False

    complete, present = coverage(args.out)
    print(f"\n{line}\n  수집 범위 선택\n{line}")
    print(f"  현재: 매일치 완료 {len(complete)}종목 / 녹화로 일부만 {len(present - complete)}종목\n")
    print("  [1] 확장 수집 - 매일치가 없는 코스닥 종목을 추가로 받음  [권장]")
    print("      실전 녹화로 며칠치만 있는 급등주를 먼저 채우고, 그다음 새 종목.")
    print("      완료된 종목은 건너뛰므로 여러 번 돌리면 계속 늘어납니다.")
    print("      중간에 끊겨도 다시 [1]을 고르면 이어서 받습니다.\n")
    print("  [2] 빠른 수집 - 조건검색식 종목만, 약 20분")
    print("      오늘 급등주만. 종목이 적어 표본은 부족합니다.\n")
    print("  [3] 다시 받기 - 코스닥 앞쪽부터 지정 개수, 기존 파일 덮어씀")
    print("      데이터가 망가졌을 때만 사용하세요.\n")
    sel = _ask("  선택 [1/2/3]: ", ('1', '2', '3'))
    if sel is None:
        return False

    if sel == '2':
        args.market = None
        print("\n[빠른 수집] 조건검색식 종목")
    else:
        args.market = 'kosdaq'
        args.pages = 12
        args.skip_existing = (sel == '1')
        args.ask_limit = True
        print(f"\n[{'확장 수집' if sel == '1' else '다시 받기'}] 코스닥 / 종목당 12페이지 = 약 83영업일")
        if sel == '3':
            print("  * 기존 파일을 덮어씁니다.")
    print("  * 중간에 끊어도 그때까지 모은 건 저장됩니다.\n")
    return True


def main():
    ap = argparse.ArgumentParser(description='백테스트용 분봉 수집기')
    ap.add_argument('--codes', help='쉼표구분 종목코드 (예: 405100,318160)')
    ap.add_argument('--codes-file', help='종목코드 파일 (한 줄에 하나)')
    ap.add_argument('--days', type=int, default=None, help='종목당 최대 저장 영업일')
    ap.add_argument('--out', default=OUT_ROOT, help='저장 폴더')
    ap.add_argument('--market', choices=['kosdaq', 'kospi', 'all'],
                    help='시장 전체 종목 수집 (조건검색식 대신)')
    ap.add_argument('--limit', type=int, help='수집 종목 수 상한')
    ap.add_argument('--pages', type=int, default=MAX_PAGES,
                    help=f'종목당 연속조회 페이지 (기본 {MAX_PAGES}, 1페이지=약 7영업일)')
    ap.add_argument('--skip-existing', action='store_true',
                    help='매일치가 갖춰진 종목은 건너뜀 (확장 수집 / 중단 후 이어받기). '
                         '녹화로 며칠치만 있는 종목을 먼저, 그다음 새 종목 순서로 받는다')
    ap.add_argument('--ask-limit', action='store_true',
                    help='로그인 전에 이번에 받을 종목 수를 물어본다')
    ap.add_argument('--menu', action='store_true',
                    help='대화형 메뉴 (1_Collect_Data.bat 용)')
    args = ap.parse_args()

    if args.menu:
        # 한글 안내/입력을 배치 파일에 두면 UTF-8 배치의 cmd 버그로
        # 줄이 중간부터 명령으로 실행된다. 메뉴는 파이썬에서 처리한다.
        if not run_menu(args):
            print("\n취소했습니다.")
            return

    if args.ask_limit:
        # 배치 파일의 set /p 는 UTF-8 한글 줄과 섞이면 cmd 가 줄을 잘못 읽는다.
        # 입력은 파이썬에서 받는다.
        default = args.limit or 200
        per_stock_sec = args.pages * TR_DELAY_SEC + 2
        while True:
            try:
                raw = input(f"  이번에 받을 종목 수 (숫자 입력 후 Enter, 그냥 Enter 면 {default}) "
                            f"[코스닥 전체 약 1600, 시간당 약 {int(3600 // per_stock_sec)}종목]: ").strip()
            except EOFError:
                raw = ''
            if not raw:
                args.limit = default
                break
            if raw.isdigit() and int(raw) > 0:
                args.limit = int(raw)
                break
            print("  [오류] 1 이상의 숫자만 입력하세요.")
        print(f"  → {args.limit}종목, 최대 약 {args.limit * per_stock_sec / 3600:.1f}시간 "
              f"(데이터가 짧은 종목은 더 빨리 끝남)\n")

    print("[로그인] 키움 접속 중... 자동로그인이 켜져 있으면 창 없이 바로 접속됩니다.",
          flush=True)
    app = QApplication(sys.argv)
    col = CandleCollector()
    col.login()

    # ── 종목 리스트 결정 ──
    codes = []
    if args.codes:
        codes = [c.strip() for c in args.codes.split(',') if c.strip()]
    elif args.codes_file:
        with open(args.codes_file, encoding='utf-8') as f:
            codes = [ln.strip() for ln in f if ln.strip()
                     and not ln.startswith('#')]
    elif args.market:
        markets = {'kosdaq': ['10'], 'kospi': ['0'], 'all': ['10', '0']}[args.market]
        raw = []
        for m in markets:
            raw.extend(col.get_market_codes(m))
        print(f"[수집] {args.market} 전체 {len(raw)}종목 -> 선별 중...")
        for c in raw:
            ok, _ = col.is_tradable(c)
            if ok:
                codes.append(c)
        print(f"[수집] ETF/ETN/스팩/우선주 제외 후 {len(codes)}종목")
    else:
        col.load_conditions()
        seen = set()
        for cname in CONDITION_NAMES:
            for c in col.get_condition_codes(cname):
                if c not in seen:
                    seen.add(c)
                    codes.append(c)
            time.sleep(1.0)
        print(f"[수집] 조건검색식 {len(CONDITION_NAMES)}개 → 중복제거 {len(codes)}종목")

    if args.skip_existing:
        complete, present = coverage(args.out)
        before = len(codes)
        # 조건검색에 실제로 걸렸던 급등주(녹화로 일부만 있음)가 검증에 가장 중요하므로 먼저 받는다
        partial = [c for c in codes if c in present and c not in complete]
        new = [c for c in codes if c not in present]
        codes = partial + new
        print(f"[수집] 대상 {before}종목 중 매일치 완료 {before - len(codes)}종목 제외 "
              f"→ 남은 {len(codes)}종목 (녹화로 일부만 있음 {len(partial)} + 새 종목 {len(new)})")

    if args.limit:
        codes = codes[:args.limit]
        print(f"[수집] 이번 실행 {len(codes)}종목")

    if not codes:
        print("[수집] 대상 종목 없음")
        return

    est_min = len(codes) * args.pages * TR_DELAY_SEC / 60
    print(f"[수집] {len(codes)}종목 × 최대 {args.pages}페이지 "
          f"(약 {args.pages*7}영업일, 예상 {est_min/60:.1f}시간)")

    total_days = 0
    for n, code in enumerate(codes, 1):
        from candle_recorder import fix_korean
        name = fix_korean(
            col.dynamicCall("GetMasterCodeName(QString)", code).strip()) or code
        try:
            rows = col.fetch_candles(code, max_pages=args.pages)
        except Exception as e:
            print(f"  [{n}/{len(codes)}] {name}({code}) 실패: {e}")
            time.sleep(TR_DELAY_SEC)
            continue

        if not rows:
            print(f"  [{n}/{len(codes)}] {name}({code}) 데이터 없음")
            time.sleep(TR_DELAY_SEC)
            continue

        saved = save_by_day(code, name, rows, args.out, args.days)
        total_days += saved
        print(f"  [{n}/{len(codes)}] {name}({code}) "
              f"{len(rows)}봉 → {saved}일 저장")
        time.sleep(TR_DELAY_SEC)

    print(f"\n[완료] 종목-일 {total_days}건 저장 → {args.out}/")
    if args.skip_existing:
        complete, present = coverage(args.out)
        print(f"[현황] 매일치 완료 {len(complete)}종목 / 녹화로 일부만 {len(present - complete)}종목")
    print("[다음] 2_Run_Analysis.bat 또는 4_Entry_Research.bat 을 실행하세요")


if __name__ == '__main__':
    main()
