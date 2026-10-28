#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_daily.py — 스윙 연구용 일봉 수집기 (키움 REST ka10081)

코스피/코스닥 보통주의 수정주가 일봉을 daily_data/<code>.json 으로 저장한다.
swing_research.py 가 이 폴더가 있으면 이 데이터를 쓴다.

Usage:
    python collect_daily.py                  # 전 종목, 최근 3년
    python collect_daily.py --years 5
    python collect_daily.py --codes 005930,000660
    python collect_daily.py --limit 50       # 시험용
    python collect_daily.py --refresh        # 오늘 받은 파일도 다시 받기

[주의]
  - main.py 와 같은 앱키를 쓰므로 장중에는 돌리지 말 것 (호출 제한 공유)
  - 중간에 끊겨도 다시 실행하면 오늘 받은 종목은 건너뛰고 이어서 받는다
  - 현재 상장 종목만 받으므로 상장폐지 종목이 빠진다 (생존 편향).
    폭락 후 반등 같은 역추세 결과는 실제보다 좋게 나올 수 있다.
"""

import argparse, json, os, sys, time
from datetime import datetime, timedelta

from config import IS_REAL, KIWOOM_EXCHANGE
from kiwoom_rest import KiwoomRestClient, load_app_keys, norm_code, to_int

OUT_ROOT = 'daily_data'
PAGE_DELAY_SEC = 1.1
BAD_NAMES = ('ETN', 'ETF', '스팩', '리츠', 'KODEX', 'TIGER', 'KBSTAR', 'RISE ', 'ARIRANG',
             'HANARO', 'PLUS ', 'ACE ', 'SOL ', 'KOSEF', 'TIMEFOLIO', '인버스', '레버리지')


def tradable(code, name):
    """ETF/ETN/스팩/리츠/우선주 제외 (collect_candles.is_tradable 과 같은 기준)"""
    if not name or any(b in name.upper() or b in name for b in BAD_NAMES):
        return False
    return code[-1] == '0'      # 우선주(5/7/9 등) 제외


def fetch_daily(rest, code, since):
    """since(YYYYMMDD) 이후 일봉 (과거→최신)"""
    bars, cont, nkey = [], 'N', ''
    base = datetime.now().strftime('%Y%m%d')
    for _ in range(40):
        data, cont, nkey = rest.daily_chart(code, base, cont, nkey)
        rows = data.get('stk_dt_pole_chart_qry') or []
        oldest = None
        for r in rows:
            d = str(r.get('dt', '')).strip()
            o, h, l, c = (abs(to_int(r.get(k))) for k in ('open_pric', 'high_pric', 'low_pric', 'cur_prc'))
            if len(d) != 8 or c <= 0:
                continue
            oldest = d if oldest is None or d < oldest else oldest
            if d >= since:
                bars.append({'d': d, 'o': o or c, 'h': h or c, 'l': l or c, 'c': c,
                             'v': abs(to_int(r.get('trde_qty'))),
                             'amt': abs(to_int(r.get('trde_prica')))})   # 거래대금(백만원)
        if not rows or (oldest and oldest < since) or cont != 'Y' or not nkey:
            break
        time.sleep(PAGE_DELAY_SEC)
    uniq = {b['d']: b for b in bars}
    return [uniq[d] for d in sorted(uniq)]


def main():
    ap = argparse.ArgumentParser(description='스윙 연구용 일봉 수집기 (ka10081)')
    ap.add_argument('--years', type=float, default=3.0)
    ap.add_argument('--codes', help='쉼표구분 종목코드')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--refresh', action='store_true', help='오늘 받은 파일도 다시 받기')
    ap.add_argument('--out', default=OUT_ROOT)
    args = ap.parse_args()

    rest = KiwoomRestClient(*load_app_keys(IS_REAL), IS_REAL, rate_per_sec=1.0, burst=1,
                            timeout=10.0, exchange=KIWOOM_EXCHANGE)
    try:
        rest.issue_token()
    except Exception as e:
        print(f"[로그인] 실패: {e}")
        sys.exit(1)
    print(f"[로그인] 성공 ({'실전' if IS_REAL else '모의'} {rest.base_url})", flush=True)

    names, markets = {}, {}
    for mk, label in (('0', 'KOSPI'), ('10', 'KOSDAQ')):
        for r in rest.stock_list(mk):
            c = norm_code(r.get('code'))
            if c:
                names[c] = str(r.get('name', '')).strip()
                markets[c] = label

    if args.codes:
        codes = [c.strip() for c in args.codes.split(',') if c.strip()]
    else:
        codes = sorted(c for c in names if tradable(c, names[c]))
        print(f"[대상] 상장 {len(names)}종목 → 보통주 {len(codes)}종목")

    os.makedirs(args.out, exist_ok=True)
    today = datetime.now().date()
    if not args.refresh:
        def fresh(c):
            p = os.path.join(args.out, f'{c}.json')
            return os.path.exists(p) and datetime.fromtimestamp(os.path.getmtime(p)).date() == today
        before = len(codes)
        codes = [c for c in codes if not fresh(c)]
        if before != len(codes):
            print(f"[이어받기] 오늘 받은 {before - len(codes)}종목 건너뜀")
    if args.limit:
        codes = codes[:args.limit]

    since = (datetime.now() - timedelta(days=int(args.years * 365))).strftime('%Y%m%d')
    print(f"[수집] {len(codes)}종목, {since} 이후 일봉", flush=True)
    t0, ok, fail = time.time(), 0, 0
    for n, code in enumerate(codes, 1):
        try:
            bars = fetch_daily(rest, code, since)
        except Exception as e:
            fail += 1
            print(f"  [{n}/{len(codes)}] {names.get(code, code)}({code}) 실패: {e}", flush=True)
            time.sleep(PAGE_DELAY_SEC)
            continue
        if bars:
            with open(os.path.join(args.out, f'{code}.json'), 'w', encoding='utf-8') as f:
                json.dump({'code': code, 'name': names.get(code, ''), 'market': markets.get(code, ''),
                           'bars': bars}, f, ensure_ascii=False)
            ok += 1
        if n % 50 == 0 or n == len(codes):
            el = time.time() - t0
            print(f"  [{n}/{len(codes)}] 저장 {ok} 실패 {fail}  경과 {el/60:.0f}분 "
                  f"남은 약 {el / n * (len(codes) - n) / 60:.0f}분", flush=True)
        time.sleep(PAGE_DELAY_SEC)
    print(f"\n[완료] {ok}종목 저장 → {args.out}/  (실패 {fail})")
    print("[다음] python swing_research.py")


if __name__ == '__main__':
    main()
