#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swing_research.py - 일봉 스윙 진입 후보 검증기 (연구용, 실매매 코드와 분리)

entry_research.py 의 일봉판. 3분봉 단타에서는 왕복 비용(0.38%)을 넘는 우위가
없었으므로, 보유 기간을 며칠로 늘려 비용 부담이 상대적으로 작아지는 구간을 본다.

[원칙] (entry_research.py 와 같음)
  - 신호일 i 종가 마감 → 다음날 시가 체결. 같은 종목은 청산 전 재진입 금지.
  - 표본 분할: IS(선택용) / OOS(확인용). 둘 다 비용 차감 후 플러스 & 각 100건 이상이어야 통과.
  - 대조군: 임의 진입(시장 드리프트 수준), 미래참조 치트(크게 플러스). 어긋나면 도구 버그.
[추가 규칙]
  - 다음날 시가가 상한가에 묶여 있으면(시=고=저, +29% 이상 갭) 살 수 없으므로 제외.
    (entry_research 오버나잇 분석: 수익 전부가 '살 수 없는 상한가 잠김'에서 나왔음)
  - 20일 평균 거래대금 LIQ_MIN_AMT 미만 종목 제외 (체결 가능성).
  - 같은 날 신호들은 시장 전체 등락을 같이 탄다 → 날짜별 평균으로 묶은 t값(t_day)을 함께 본다.
    건수가 많아도 t_day 가 작으면 며칠의 시장 흐름에 기댄 결과다.

[데이터]
  daily_data/<code>.json (collect_daily.py, 키움 ka10081) 이 있으면 그걸 쓰고,
  없으면 candle_data/ 3분봉을 매일수집 종목만 일봉으로 합쳐 쓴다.
  둘 다 현재 상장 종목만 있어 상장폐지 종목이 빠진다(생존 편향) → 역추세 결과는 낙관적일 수 있다.

Usage:
    python swing_research.py
    python swing_research.py --source candle        # 3분봉 합성 일봉 강제
    python swing_research.py --split 2025-07-01
"""

import argparse, collections, json, math, os, statistics, time

import backtest

LIQ_MIN_AMT = 1000          # 20일 평균 거래대금 하한 (백만원 = 10억)
MIN_N = 100
LIMIT_UP = 29.5             # 상한가 근사 (%)


# ─── 데이터 ──────────────────────────────────────────────────────────────────
def load_daily_dir(root):
    out = {}
    for fn in os.listdir(root):
        if not fn.endswith('.json'):
            continue
        try:
            with open(os.path.join(root, fn), encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            continue
        bars = [b for b in d.get('bars', []) if b.get('c', 0) > 0]
        if len(bars) >= 130:
            out[d['code']] = (d.get('name', ''), bars)
    return out


def load_from_candles():
    """3분봉(정규장) → 일봉. 매일수집 종목만 (녹화 파일 편향 제거)"""
    import entry_research as er
    raw, _, clean = er.load(clean_only=True)
    by = collections.defaultdict(list)
    names = {}
    for date, code, name, bars in raw:
        if not bars:
            continue
        names[code] = name
        by[code].append({
            'd': date.replace('-', ''), 'o': bars[0]['o'], 'h': max(b['h'] for b in bars),
            'l': min(b['l'] for b in bars), 'c': bars[-1]['c'], 'v': sum(b['v'] for b in bars),
            'amt': sum(b['c'] * b['v'] for b in bars) / 1e6,
        })
    return {c: (names[c], sorted(bs, key=lambda b: b['d'])) for c, bs in by.items() if len(bs) >= 130}


# ─── 지표 ────────────────────────────────────────────────────────────────────
def sma(xs, n, i):
    return sum(xs[i - n + 1:i + 1]) / n if i >= n - 1 else None


def features(bars):
    """i 일 종가 시점에 알 수 있는 값만"""
    o = [b['o'] for b in bars]; h = [b['h'] for b in bars]; l = [b['l'] for b in bars]
    c = [b['c'] for b in bars]; v = [b['v'] for b in bars]; a = [b.get('amt', 0) for b in bars]
    n = len(c)
    tr = [h[0] - l[0]] + [max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1])) for k in range(1, n)]
    F = [None] * n
    reds = [0] * n                      # i 일까지 연속 하락일 수
    for k in range(1, n):
        reds[k] = reds[k - 1] + 1 if c[k] < c[k - 1] else 0
    for i in range(n):
        if i < 125:
            continue
        ma20 = sma(c, 20, i); ma20_5 = sma(c, 20, i - 5)
        rng = h[i] - l[i]
        F[i] = {
            'o': o[i], 'h': h[i], 'l': l[i], 'c': c[i], 'pc': c[i - 1],
            'ma5': sma(c, 5, i), 'ma20': ma20, 'ma20_5': ma20_5, 'ma60': sma(c, 60, i), 'ma120': sma(c, 120, i),
            'atr': sum(tr[i - 13:i + 1]) / 14,
            'vavg': sum(v[i - 20:i]) / 20, 'v': v[i], 'amt20': sum(a[i - 20:i]) / 20,
            'hi20': max(h[i - 20:i]), 'hi60': max(h[i - 60:i]),
            'ret1': (c[i] / c[i - 1] - 1) * 100, 'ret3': (c[i] / c[i - 3] - 1) * 100,
            'gap': (o[i] / c[i - 1] - 1) * 100,
            'cpos': (c[i] - l[i]) / rng if rng > 0 else 0.5,
            'nr7': rng > 0 and rng <= min(h[k] - l[k] for k in range(i - 6, i + 1)),
            'reds_prev': reds[i - 1],   # 신호일 직전까지 연속 하락일
        }
    return F


# ─── 가설 ────────────────────────────────────────────────────────────────────
def build_hypotheses():
    H = {}
    def add(name, fn):
        H[name] = fn
    for vm in (2.0, 3.0):
        add(f"BRK20_v{vm}",                       # 20일 신고가 돌파 + 거래량
            lambda f, vm=vm: f['c'] > f['hi20'] and f['v'] >= vm * f['vavg'] and f['c'] > f['ma60']
            and f['ret1'] < LIMIT_UP)
    add("BRK60_v2", lambda f: f['c'] > f['hi60'] and f['v'] >= 2 * f['vavg'] and f['ret1'] < LIMIT_UP)
    for r in (5, 10):
        add(f"STRONGCLOSE_r{r}",                  # 강한 양봉 + 고가권 마감 (상한가 제외)
            lambda f, r=r: r <= f['ret1'] < LIMIT_UP and f['cpos'] >= 0.8)
    add("LIMITUP_NEXT", lambda f: f['ret1'] >= LIMIT_UP)        # 상한가 다음날 시가 매수
    for tol in (1.0, 3.0):
        add(f"PB_MA20_t{tol}",                    # 상승추세 20일선 눌림
            lambda f, tol=tol: f['c'] > f['ma60'] and f['ma20'] > f['ma20_5']
            and f['l'] <= f['ma20'] * (1 + tol / 100) and f['c'] >= f['ma20'] and f['reds_prev'] >= 2)
    for d in (10, 15):
        add(f"OVERSOLD3_d{d}",                    # 3일 급락 후 양봉 (역추세)
            lambda f, d=d: f['ret3'] <= -d and f['c'] > f['o'])
    add("DIP_UPTREND", lambda f: f['c'] > f['ma120'] and f['c'] < f['ma5'] * 0.93)
    add("NR7_UPTREND", lambda f: f['nr7'] and f['c'] > f['ma20'] > f['ma60'])
    add("QUIET_UPTREND", lambda f: f['v'] < 0.5 * f['vavg'] and f['c'] > f['ma20'] > f['ma60'])
    add("GAPHOLD_g3", lambda f: f['gap'] >= 3 and f['c'] >= f['o'] and f['c'] > f['ma20'] and f['ret1'] < LIMIT_UP)
    return H


EXITS = {                       # (보유일, 손절 ATR배수, 익절 ATR배수)
    '당일종가':      (1, None, None),
    '3일':           (3, None, None),
    '5일':           (5, None, None),
    '10일':          (10, None, None),
    'SL2ATR+5일':    (5, 2.0, None),
    'SL2+TP3+10일':  (10, 2.0, 3.0),
}


def run_exit(bars, j0, fill, atr, hold, sl, tp):
    """j0=진입일. 반환: (청산가, 청산일 index). 한 날 손절·익절이 겹치면 손절 먼저(보수적)"""
    stop = fill - sl * atr if sl else None
    tgt = fill + tp * atr if tp else None
    last = min(j0 + hold - 1, len(bars) - 1)
    for j in range(j0, last + 1):
        b = bars[j]
        if stop is not None and b['l'] <= stop:
            return (min(b['o'], stop) if j > j0 else stop), j
        if tgt is not None and b['h'] >= tgt:
            return (max(b['o'], tgt) if j > j0 else tgt), j
    return bars[last]['c'], last


def unbuyable(prev_c, b):
    return b['o'] == b['h'] == b['l'] and (b['o'] / prev_c - 1) * 100 >= LIMIT_UP - 1


# ─── 실행 ────────────────────────────────────────────────────────────────────
def research(data, split):
    H = build_hypotheses()
    agg = collections.defaultdict(list)          # (name, exit, part) → [(date, ret%)]
    skipped = collections.Counter()
    for code, (name, bars) in data.items():
        F = features(bars)
        n = len(bars)
        busy = {(nm, ex): -1 for nm in H for ex in EXITS}        # 청산일 index (재진입 금지)
        for i in range(n - 1):
            f = F[i]
            if f is None:
                continue
            j0 = i + 1
            date = bars[j0]['d']
            part = 'IS' if date < split else 'OOS'
            fill = bars[j0]['o']
            liquid = f['amt20'] >= LIQ_MIN_AMT

            # 대조군: 10거래일마다 임의 진입 / 5일 뒤 종가가 오른 경우만 (미래참조)
            if liquid and i % 10 == 0 and j0 + 4 < n and fill > 0:
                g = (bars[j0 + 4]['c'] - fill) / fill * 100
                agg[('대조:임의진입', '5일', part)].append((date, g))
                if g > 0:
                    agg[('대조:미래참조 치트', '5일', part)].append((date, g))

            hits = [nm for nm, fn in H.items() if fn(f)]
            if not hits:
                continue
            if not liquid:
                skipped['거래대금부족'] += len(hits)
                continue
            if fill <= 0 or unbuyable(f['c'], bars[j0]):
                skipped['상한가잠김(매수불가)'] += len(hits)
                continue
            for nm in hits:
                for ex, (hold, sl, tp) in EXITS.items():
                    if busy[(nm, ex)] >= j0 or j0 + hold - 1 >= n:
                        continue
                    px, jx = run_exit(bars, j0, fill, f['atr'], hold, sl, tp)
                    busy[(nm, ex)] = jx
                    agg[(nm, ex, part)].append((date, (px - fill) / fill * 100))
    return agg, skipped


def stats(xs, cost):
    if not xs:
        return dict(n=0, gross=0.0, net=0.0, win=0.0, t=0.0, days=0)
    rets = [r for _, r in xs]
    by_day = collections.defaultdict(list)
    for d, r in xs:
        by_day[d].append(r - cost)
    dm = [statistics.mean(v) for v in by_day.values()]
    t = (statistics.mean(dm) / statistics.stdev(dm) * math.sqrt(len(dm))) if len(dm) > 2 and statistics.stdev(dm) > 0 else 0.0
    m = statistics.mean(rets)
    return dict(n=len(rets), gross=m, net=m - cost, win=sum(r > cost for r in rets) / len(rets) * 100,
                t=t, days=len(dm))


def main():
    ap = argparse.ArgumentParser(description='일봉 스윙 진입 후보 검증기')
    ap.add_argument('--source', choices=['auto', 'daily', 'candle'], default='auto')
    ap.add_argument('--split', help='이 날짜(YYYY-MM-DD) 이전 IS / 이후 OOS. 생략하면 기간의 60%% 지점')
    ap.add_argument('--top', type=int, default=30)
    args = ap.parse_args()

    t0 = time.time()
    use_daily = args.source == 'daily' or (args.source == 'auto' and os.path.isdir('daily_data')
                                            and any(fn.endswith('.json') for fn in os.listdir('daily_data')))
    data = load_daily_dir('daily_data') if use_daily else load_from_candles()
    src = 'daily_data (키움 ka10081 일봉)' if use_daily else 'candle_data 3분봉 → 일봉 합성 (매일수집 종목)'
    dates = sorted({b['d'] for _, bars in data.values() for b in bars})
    if not dates:
        print("[데이터] 없음")
        return
    split = args.split.replace('-', '') if args.split else dates[int(len(dates) * 0.6)]
    cost = backtest.cost_pct() + backtest.SLIPPAGE_PCT * 100
    print(f"[데이터] {src}: {len(data)}종목, {dates[0]}~{dates[-1]} ({len(dates)}거래일)")
    print(f"[분할] IS < {split} <= OOS   [비용] 왕복 {cost:.2f}%   [유동성] 20일 평균 거래대금 ≥ {LIQ_MIN_AMT/100:.0f}억", flush=True)

    agg, skipped = research(data, split)
    print(f"[계산] {time.time() - t0:.0f}초   제외: {dict(skipped)}\n")

    rows = []
    for key in {k[:2] for k in agg}:
        i_ = stats(agg[key + ('IS',)], cost)
        o_ = stats(agg[key + ('OOS',)], cost)
        ok = i_['n'] >= MIN_N and o_['n'] >= MIN_N and i_['net'] > 0 and o_['net'] > 0
        rows.append((key, i_, o_, ok))

    hdr = (f"{'후보':<20}{'청산':>12} | {'IS n':>6}{'세전':>8}{'순':>8}{'승률':>7}{'t일':>6} | "
           f"{'OOS n':>6}{'세전':>8}{'순':>8}{'승률':>7}{'t일':>6} | 판정")

    def line(r):
        (nm, ex), i_, o_, ok = r
        verdict = '통과' if ok else ('표본부족' if min(i_['n'], o_['n']) < MIN_N else '')
        return (f"{nm:<20}{ex:>12} | {i_['n']:>6}{i_['gross']:>+8.2f}{i_['net']:>+8.2f}{i_['win']:>6.1f}%{i_['t']:>6.1f} | "
                f"{o_['n']:>6}{o_['gross']:>+8.2f}{o_['net']:>+8.2f}{o_['win']:>6.1f}%{o_['t']:>6.1f} | {verdict}")

    print("== 대조군 (도구 점검: 임의진입≈시장 드리프트, 치트는 크게 플러스여야 정상) ==")
    print(hdr)
    for r in sorted((r for r in rows if r[0][0].startswith('대조')), key=lambda r: r[0][0]):
        print(line(r))
    cands = [r for r in rows if not r[0][0].startswith('대조')]
    print(f"\n== 후보 (IS 순기댓값 상위 {args.top} / 전체 {len(cands)}개 조합) ==")
    print(hdr)
    for r in sorted(cands, key=lambda r: -r[1]['net'])[:args.top]:
        print(line(r))

    passed = [r for r in cands if r[3]]
    strong = [r for r in passed if r[1]['t'] > 0 and r[2]['t'] >= 2.0]
    print("\n== 결론 ==")
    print(f"  조합 {len(cands)}개 시험 → 1차 통과(IS/OOS 순기댓값 > 0, 각 {MIN_N}건 이상) {len(passed)}개")
    print("  t일 = 날짜별 평균 순수익의 t값. 거래 평균이 플러스여도 t일이 0 이하면")
    print("        수익이 며칠의 큰 움직임에 몰린 것 (대부분의 날은 손해).")
    if strong:
        print(f"\n  ★ 유력 (1차 통과 + IS t일 > 0 + OOS t일 ≥ 2): {len(strong)}개")
        for r in sorted(strong, key=lambda r: -r[2]['t']):
            print("   ", line(r))
        print(f"  * 조합을 {len(cands)}개 시험했으므로 이것도 우연일 수 있다. 다른 기간/시장으로 재확인할 것.")
    else:
        print("\n  유력 후보 없음 (OOS t일 ≥ 2 를 넘는 것이 없음).")
        for r in sorted(passed, key=lambda r: -r[2]['t'])[:10]:
            print("   1차만:", line(r))


if __name__ == '__main__':
    main()
