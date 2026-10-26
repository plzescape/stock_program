#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_candles.py — 백테스트용 분봉 데이터 수집기 (키움 REST API)

ka10080(주식분봉차트조회)으로 종목별 분봉을 연속조회하여
candle_data/YYYY-MM-DD/<code>.json 으로 저장한다.

REST 전환 후 32비트 파이썬 / OCX 가 필요 없다 (64비트도 가능).
앱키는 kiwoom_keys.json 또는 환경변수 (kiwoom_rest.load_app_keys 참고).

Usage:
    python collect_candles.py                      # 조건검색식 종목 수집
    python collect_candles.py --codes 405100,318160
    python collect_candles.py --codes-file codes.txt
    python collect_candles.py --days 10            # 최대 10영업일치

키움 제약:
  - TR(api-id)별 호출 제한 → 요청 간 딜레이 (기본 1.2초, 429 나면 늘릴 것)
  - ka10080 연속조회는 응답 헤더 cont-yn=Y / next-key 로 이어받음
  - 과거 데이터 보관 기간은 종목/서버 상황에 따라 다름
"""

import os, sys, json, time, argparse, queue
from datetime import datetime
from collections import defaultdict

from config import CANDLE_INTERVAL_MIN, CONDITION_NAMES, IS_REAL, KIWOOM_EXCHANGE
from kiwoom_rest import (KiwoomRestClient, KiwoomWebSocket,
                         load_app_keys, norm_code, to_int)

TR_DELAY_SEC = 1.2      # 페이지 간 딜레이. 클라이언트 자체 제한(초당 1회)과 함께 429 방지
MAX_PAGES    = 12       # 종목당 최대 연속조회 페이지
OUT_ROOT     = 'candle_data'


class CandleCollector:
    def __init__(self):
        appkey, secretkey = load_app_keys(IS_REAL)
        # 수집은 급할 게 없으니 순간 연속호출(burst) 없이 초당 1회로 고정
        self.rest = KiwoomRestClient(appkey, secretkey, IS_REAL,
                                     rate_per_sec=1.0, burst=1,
                                     timeout=10.0, exchange=KIWOOM_EXCHANGE)
        self.conditions = {}       # name → seq
        self.names      = {}       # code → name

    # ── 로그인 ──
    def login(self):
        try:
            self.rest.issue_token()
        except Exception as e:
            print(f"[로그인] 실패: {e}")
            print("         kiwoom_keys.json 앱키 / openapi.kiwoom.com IP 등록을 확인하세요.")
            sys.exit(1)
        print(f"[로그인] 성공 ({'실전' if IS_REAL else '모의'} {self.rest.base_url})")
        self._load_names()

    def _load_names(self):
        for market in ('0', '10'):
            try:
                for r in self.rest.stock_list(market):
                    c = norm_code(r.get('code'))
                    if c:
                        self.names[c] = str(r.get('name', '')).strip()
            except Exception as e:
                print(f"[종목명] 시장 {market} 조회 실패: {e}")
        print(f"[종목명] {len(self.names)}개 로드")

    def get_name(self, code):
        return self.names.get(code, '')

    # ── 시장 전체 종목 ──
    def get_market_codes(self, market):
        """market: '0'=코스피, '10'=코스닥"""
        return [norm_code(r.get('code')) for r in self.rest.stock_list(market)
                if norm_code(r.get('code'))]

    def is_tradable(self, code):
        """
        백테스트에 무의미한 종목 제외.
        ETF/ETN/스팩/리츠/우선주 는 전략이 절대 잡지 않으므로
        수집 시간(종목당 40초)을 낭비할 이유가 없다.
        """
        name = self.get_name(code)
        if not name:
            return False, ''
        bad = ('ETN', 'ETF', '스팩', '리츠', 'KODEX', 'TIGER',
               'KBSTAR', 'ARIRANG', 'HANARO', 'PLUS', 'ACE ', 'SOL ')
        if any(b in name.upper() or b in name for b in bad):
            return False, name
        if code[-1] != '0':          # 우선주(5/7/9 등)
            return False, name
        return True, name

    # ── 조건검색식 (웹소켓) ──
    def collect_condition_codes(self, names, timeout=10.0):
        """
        웹소켓 CNSRLST → CNSRREQ(search_type=0, 일회성)로 조건식별 종목을 받는다.
        반환: 조건식 이름 순서대로 중복 제거한 종목코드 목록
        """
        inbox = queue.Queue()
        ws = KiwoomWebSocket(self.rest, on_message=inbox.put,
                             on_state=lambda s, m: inbox.put({'trnm': f'__{s}', 'msg': m}))
        ws.start()
        try:
            if not self._wait(inbox, '__LOGIN_OK', timeout):
                print("[조건식] 웹소켓 로그인 실패 — 조건검색 수집 불가")
                return []

            ws.condition_list()
            lst = self._wait(inbox, 'CNSRLST', timeout)
            for item in (lst or {}).get('data') or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    self.conditions[str(item[1]).strip()] = str(item[0]).strip()
            print(f"[조건식] {len(self.conditions)}개 로드")

            codes, seen = [], set()
            for name in names:
                seq = self.conditions.get(name)
                if seq is None:
                    print(f"[조건식] '{name}' 없음 — 스킵")
                    continue
                ws.condition_request(seq, realtime=False)
                res = self._wait(inbox, 'CNSRREQ', timeout) or {}
                got = []
                for row in res.get('data') or []:
                    raw = (row.get('jmcode') or row.get('9001') or row.get('stk_cd')
                           if isinstance(row, dict) else row)
                    c = norm_code(raw)
                    if c:
                        got.append(c)
                print(f"[조건식] '{name}' → {len(got)}종목")
                for c in got:
                    if c not in seen:
                        seen.add(c)
                        codes.append(c)
                time.sleep(1.0)
            return codes
        finally:
            ws.stop()

    @staticmethod
    def _wait(inbox, trnm, timeout):
        """inbox 에서 trnm 메시지가 올 때까지 대기. 시간초과면 None"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = inbox.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get('trnm') == trnm:
                return msg
        return None

    # ── 분봉 조회 ──
    def fetch_candles(self, code, max_pages=MAX_PAGES):
        """ka10080 연속조회 → 캔들 리스트(최신→과거)"""
        rows = []
        cont, nkey = 'N', ''

        for page in range(max_pages):
            data, cont, nkey = self.rest.minute_chart(code, str(CANDLE_INTERVAL_MIN), cont, nkey)
            for r in data.get('stk_min_pole_chart_qry') or []:
                o = abs(to_int(r.get('open_pric')))
                h = abs(to_int(r.get('high_pric')))
                l = abs(to_int(r.get('low_pric')))
                c = abs(to_int(r.get('cur_prc')))
                v = abs(to_int(r.get('trde_qty')))
                ts = str(r.get('cntr_tm', '')).strip()   # YYYYMMDDHHMMSS
                if o == 0 or c == 0 or len(ts) < 12:
                    continue
                rows.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v})

            if cont != 'Y' or not nkey:
                break
            time.sleep(TR_DELAY_SEC)

        return rows


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
    print("         REST 호출 제한은 앱키(계좌) 단위라 같이 돌리면")
    print("         자동매매의 스캔/주문이 호출 제한(429)에 걸릴 수 있습니다.\n")
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

    print("[로그인] 키움 REST 접근토큰 발급 중...", flush=True)
    try:
        col = CandleCollector()
    except RuntimeError as e:          # 앱키 없음
        print(f"[로그인] {e}")
        return
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
        codes = col.collect_condition_codes(CONDITION_NAMES)
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
        name = col.get_name(code) or code
        try:
            rows = col.fetch_candles(code, max_pages=args.pages)
        except Exception as e:          # KiwoomError / 통신 오류 → 이 종목만 건너뜀
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
