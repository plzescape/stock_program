#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
entry_research.py - 진입 전략 후보 검증기 (연구용, 실매매 코드와 분리)

backtest.py 가 "지금 전략이 돈이 되는가"를 본다면, 이 파일은
"어떤 진입 조건이 애초에 우위가 있는가"를 본다. 청산 최적화가 아니라
진입 자체의 기댓값을 확인하는 것이 목적이다.

[원칙]
  - 신호봉 i 마감 → 다음 봉 시가 체결. 종목-일당 첫 신호 1회.
  - 전날 봉 연결, 정규장(09:00~15:30) 봉만 사용.
  - 표본 분할: IS(선택용) / OOS(확인용). IS 에서 좋아 보여도 OOS 에서
    무너지면 우연이다. 둘 다 비용 차감 후 플러스여야 "통과".
  - 대조군: 임의 진입(세전 0 근처여야 함), 미래참조 치트(크게 플러스여야 함).
    대조군이 기대와 다르면 도구에 버그가 있는 것이다.

[데이터 편향 주의]
  candle_recorder(실전 녹화) 파일은 조건검색에 걸린 날에만 생긴다.
  "다음 날 파일이 있다 = 다음 날도 급등했다"가 되므로 오버나잇 검증에는
  매일 빠짐없이 수집된 종목(--clean-only 와 같은 기준)만 쓴다.

Usage:
    python entry_research.py                 # 장중 후보 + 오버나잇 + 대조군
    python entry_research.py --clean-only    # 장중 검증도 매일수집 종목만
    python entry_research.py --split 2026-07-01
"""

import argparse, collections, statistics, time

import config
import backtest

LIQ_MIN = config.FORCE_LIQUIDATION_HOUR * 60 + config.FORCE_LIQUIDATION_MIN
MIN_N = 100          # IS/OOS 각각 이 건수 미만이면 통과 판정 안 함


# ─── 데이터 ──────────────────────────────────────────────────────────────────
def regular(bars):
    return [b for b in bars if '0900' <= str(b['t']).zfill(4)[:4] <= '1530']


def load(clean_only):
    raw = [(d, c, n, regular(b)) for d, c, n, b in backtest.load_candle_data('candle_data')]
    raw = [x for x in raw if len(x[3]) >= 40]
    dates = sorted({d for d, _, _, _ in raw})
    didx = {d: k for k, d in enumerate(dates)}
    by_code = collections.defaultdict(set)
    for d, c, _, _ in raw:
        by_code[c].add(d)
    clean = set()
    for c, ds in by_code.items():
        lo, hi = didx[min(ds)], didx[max(ds)]
        span = hi - lo + 1
        if span >= 40 and len(ds) / span >= 0.95:
            clean.add(c)
    if clean_only:
        raw = [x for x in raw if x[1] in clean]
    return raw, dates, clean


# ─── 장중 가설 ───────────────────────────────────────────────────────────────
# f: 신호봉 i 마감 시점에 알 수 있는 값만 담는다 (미래 정보 금지)
def build_hypotheses():
    H = {}

    def add(name, fn):
        H[name] = fn

    for vr in (1.5, 3.0):
        for rmax in (8, 15):
            add(f"ORB_vr{vr}_r{rmax}",       # 시가범위(09:00~09:27) 돌파
                lambda f, vr=vr, rmax=rmax: f['k'] >= 10 and f['T'] <= 660 and f['orh']
                and f['c'] > f['orh'] >= f['pc'] and f['vr5'] >= vr
                and 1 <= f['rise'] <= rmax and f['c'] > f['vwap'])
    for R in (5, 8):
        for strict in (False, True):
            add(f"VWAPPB_R{R}_{'strict' if strict else 'loose'}",   # 급등 후 VWAP 눌림
                lambda f, R=R, s=strict: 600 <= f['T'] <= 870 and f['hodrise'] >= R
                and f['l'] <= f['vwap'] * 1.003 and f['c'] > f['vwap'] and f['c'] > f['o']
                and (not s or f['c'] > f['ph']))
    for V in (3.0, 5.0):
        for cp in (0.5, 0.75):
            add(f"HODBRK_v{V}_cp{cp}",       # 당일 고가 돌파 + 거래량
                lambda f, V=V, cp=cp: f['T'] <= 870 and f['k'] >= 3 and f['c'] > f['hod']
                and f['vr5'] >= V and 3 <= f['rise'] <= 12 and f['c'] > f['ema20']
                and f['h'] > f['l'] and (f['c'] - f['l']) / (f['h'] - f['l']) >= cp)
    for dev in (1.0, 1.5):
        add(f"DIPREV_dev{dev}",              # 급등주 VWAP 아래 과매도 반등
            lambda f, dev=dev: 570 <= f['T'] <= 870 and f['hodrise'] >= 5 and f['atr'] > 0
            and f['pc'] < f['vwap'] - dev * f['atr'] and f['c'] > f['o'] and f['c'] > f['ph'])
    for G in (2.0, 4.0):
        add(f"GAPGO_g{G}",                   # 갭상승 후 첫 15분 고가 돌파
            lambda f, G=G: f['gap'] is not None and f['gap'] >= G and 3 <= f['k'] <= 10
            and f['c'] > f['h3'] and f['c'] > f['dopen'] and f['vr5'] >= 1.5)
    for rmin in (4, 8):
        add(f"LATESTR_r{rmin}",              # 14시대 고가권 강세
            lambda f, rmin=rmin: 840 <= f['T'] <= 880 and rmin <= f['rise'] <= 20
            and f['c'] >= 0.98 * max(f['hod'], f['h']) and f['c'] > f['vwap'])
    for nred in (2, 3):
        add(f"FIRSTPB_red{nred}",            # 급등 후 첫 눌림(연속 음봉) 반등
            lambda f, n=nred: 570 <= f['T'] <= 870 and f['hodrise'] >= 5 and f['reds'] >= n
            and f['pl_min'] > f['vwap'] and f['c'] > f['o'] and f['c'] > f['ph'])
    for rmin in (3, 6):
        add(f"EMATREND_r{rmin}",             # 상승 추세 EMA20 지지
            lambda f, rmin=rmin: 570 <= f['T'] <= 870 and f['rise'] >= rmin
            and f['ema20'] > f['ema20_5'] and f['atr'] > 0
            and f['l'] <= f['ema20'] + 0.3 * f['atr'] and f['c'] > f['ema20'] and f['c'] > f['o'])
    return H


EXITS = {                      # (손절 ATR배수, 익절 ATR배수, 최대보유봉)
    '10봉보유':      (None, None, 10),
    'SL1.5+10봉':    (1.5, None, 10),
    'SL1+TP2':       (1.0, 2.0, None),
    'SL2+마감':      (2.0, None, None),
}


def run_exit(o, h, l, c, mins, j0, fill, atr, sl, tp, maxb):
    """보수적 가정: 한 봉에서 손절과 익절이 겹치면 손절 먼저"""
    stop = fill - sl * atr if sl else None
    tgt = fill + tp * atr if tp else None
    for j in range(j0, len(c)):
        if mins[j] >= LIQ_MIN:
            return c[j]
        if stop is not None and l[j] <= stop:
            return min(o[j], stop) if j > j0 else stop
        if tgt is not None and h[j] >= tgt:
            return max(o[j], tgt) if j > j0 else tgt
        if maxb and j - j0 + 1 >= maxb:
            return c[j]
    return c[-1]


def intraday(raw, split, cost):
    H = build_hypotheses()
    prev = backtest.prev_day_index(raw)
    cutoff = backtest.entry_cutoff_minute()
    agg = collections.defaultdict(list)

    for date, code, name, bars in raw:
        part = 'IS' if date < split else 'OOS'
        pb = regular(prev.get((date, code), []))
        full = pb + bars
        off = len(pb)
        o = [b['o'] for b in full]; h = [b['h'] for b in full]; l = [b['l'] for b in full]
        c = [b['c'] for b in full]; v = [b['v'] for b in full]
        mins = [backtest.bar_minute(b) for b in full]
        n = len(full)
        uni = backtest.condition_universe(bars)

        ema = [0.0] * n
        for k in range(n):
            ema[k] = c[k] if k == 0 else c[k] * (2 / 21) + ema[k - 1] * (19 / 21)
        tr = [0.0] * n
        for k in range(1, n):
            tr[k] = max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1]))
        dopen = o[off]
        gap = (dopen - c[off - 1]) / c[off - 1] * 100 if off else None

        # 대조군
        k30 = next((k for k in range(off, n) if mins[k] >= 630), None)
        if k30 is not None and k30 + 10 < n:
            g = (c[k30 + 9] - o[k30]) / o[k30] * 100
            agg[('대조:임의진입 10:30', '-', '10봉보유', part)].append(g)
            if g > 0:
                agg[('대조:미래참조 치트', '-', '10봉보유', part)].append(g)

        pv = vv = 0.0
        hod = 0; orh = None; h3 = None; reds = 0
        done = {nm: [False, False] for nm in H}
        for i in range(off, n - 2):
            k = i - off
            pv += (h[i] + l[i] + c[i]) / 3 * v[i]; vv += v[i]
            if k == 9:
                orh = max(h[off:off + 10])
            if k == 2:
                h3 = max(h[off:off + 3])
            T = mins[i + 1]
            if T >= cutoff:
                break
            if i >= 20:
                s5 = sum(v[i - 5:i])
                f = {
                    'k': k, 'T': T, 'o': o[i], 'h': h[i], 'l': l[i], 'c': c[i],
                    'pc': c[i - 1], 'ph': h[i - 1], 'vwap': pv / vv if vv else c[i],
                    'ema20': ema[i], 'ema20_5': ema[i - 5], 'atr': sum(tr[i - 13:i + 1]) / 14,
                    'vr5': v[i] / (s5 / 5) if s5 > 0 else 0,
                    'rise': (c[i] - dopen) / dopen * 100,
                    'hod': hod if k else h[i],
                    'hodrise': (max(hod, h[i]) - dopen) / dopen * 100 if k else 0,
                    'orh': orh, 'h3': h3 if h3 else float('inf'), 'dopen': dopen, 'gap': gap,
                    'reds': reds, 'pl_min': min(l[max(off, i - 3):i]) if k else 0,
                }
                for nm, fn in H.items():
                    st = done[nm]
                    if st[0] and (st[1] or not uni[k]) or not fn(f):
                        continue
                    fill, atr = o[i + 1], f['atr']
                    if fill <= 0 or atr <= 0:
                        continue
                    outs = {ex: (run_exit(o, h, l, c, mins, i + 1, fill, atr, *p) - fill) / fill * 100
                            for ex, p in EXITS.items()}
                    for gate in ((False, True) if uni[k] else (False,)):
                        if st[gate]:
                            continue
                        st[gate] = True
                        for ex, g in outs.items():
                            agg[(nm, 'ON' if gate else 'OFF', ex, part)].append(g)
            hod = max(hod, h[i]) if k else h[i]
            reds = reds + 1 if c[i] < o[i] else 0
    return agg


# ─── 오버나잇 (강세 마감 → 익일 시가) ────────────────────────────────────────
def overnight(raw, dates, clean, split):
    didx = {d: k for k, d in enumerate(dates)}
    full = {(d, c): b for d, c, _, b in raw
            if c in clean and len(b) >= 100
            and str(b[0]['t']).zfill(4) <= '0903' and str(b[-1]['t']).zfill(4) >= '1520'}
    agg = collections.defaultdict(list)
    for (date, code), b in full.items():
        k = didx[date]
        nb = full.get((dates[k + 1], code)) if k + 1 < len(dates) else None
        if nb is None:
            continue
        s = next((j for j, x in enumerate(b) if str(x['t']).zfill(4) >= '1506'), None)
        if s is None or s + 1 >= len(b) or b[s + 1]['o'] <= 0:
            continue
        fill = b[s + 1]['o']
        rise = (b[s]['c'] - b[0]['o']) / b[0]['o'] * 100
        near = b[s]['c'] >= 0.98 * max(x['h'] for x in b[:s + 1])
        g = (nb[0]['o'] - fill) / fill * 100
        part = 'IS' if date < split else 'OOS'
        agg[('오버나잇:전종목', '매일수집', '익일시가', part)].append(g)
        if near:
            for lo, hi in ((4, 8), (8, 15), (15, 29), (29, 99)):
                if lo <= rise < hi:
                    agg[(f'오버나잇:고가권 {lo}~{hi}%', '매일수집', '익일시가', part)].append(g)
    return agg


# ─── 출력 ────────────────────────────────────────────────────────────────────
def summarize(xs, cost):
    if not xs:
        return 0, 0.0, 0.0, 0.0
    m = statistics.mean(xs)
    return len(xs), m, m - cost, sum(x - cost > 0 for x in xs) / len(xs) * 100


def main():
    ap = argparse.ArgumentParser(description='진입 전략 후보 검증기')
    ap.add_argument('--split', default='2026-07-01', help='이 날짜 이전 IS / 이후 OOS')
    ap.add_argument('--clean-only', action='store_true', help='장중 검증도 매일수집 종목만')
    ap.add_argument('--top', type=int, default=30, help='출력할 상위 행 수')
    args = ap.parse_args()

    t0 = time.time()
    cost = backtest.cost_pct() + backtest.SLIPPAGE_PCT * 100
    raw, dates, clean = load(args.clean_only)
    print(f"[데이터] 종목-일 {len(raw)}  매일수집 종목 {len(clean)}  "
          f"IS < {args.split} <= OOS  왕복비용 {cost:.2f}% ({backtest.cost_desc()})", flush=True)

    agg = intraday(raw, args.split, cost)
    agg.update(overnight(raw, dates, clean, args.split))   # 내부에서 매일수집 종목만 사용
    print(f"[계산] {time.time()-t0:.0f}초\n")

    rows = []
    for key in {k[:3] for k in agg}:
        i_ = summarize(agg[key + ('IS',)], cost)
        o_ = summarize(agg[key + ('OOS',)], cost)
        ok = i_[0] >= MIN_N and o_[0] >= MIN_N and i_[2] > 0 and o_[2] > 0
        rows.append((key, i_, o_, ok))

    hdr = (f"{'후보':<24}{'게이트':>7}{'청산':>11} | {'IS n':>6}{'세전':>8}{'순':>8}{'승률':>7} | "
           f"{'OOS n':>6}{'세전':>8}{'순':>8}{'승률':>7} | 판정")

    def line(r):
        (nm, gate, ex), i_, o_, ok = r
        return (f"{nm:<24}{gate:>7}{ex:>11} | {i_[0]:>6}{i_[1]:>+8.3f}{i_[2]:>+8.3f}{i_[3]:>6.1f}% | "
                f"{o_[0]:>6}{o_[1]:>+8.3f}{o_[2]:>+8.3f}{o_[3]:>6.1f}% | "
                f"{'통과' if ok else ('표본부족' if min(i_[0], o_[0]) < MIN_N else '')}")

    print("== 대조군 (도구 점검) ==")
    print(hdr)
    for r in sorted((r for r in rows if r[0][0].startswith('대조')), key=lambda r: r[0][0]):
        print(line(r))
    print("\n== 오버나잇 (매일수집 종목만, 15:09 매수 → 익일 시가 매도) ==")
    print(hdr)
    for r in sorted((r for r in rows if r[0][0].startswith('오버나잇')), key=lambda r: r[0][0]):
        print(line(r))
    print(f"\n== 장중 후보 (IS 순기댓값 상위 {args.top}, 게이트 ON = 조건검색 근사 유니버스) ==")
    print(hdr)
    intra = [r for r in rows if not r[0][0].startswith(('대조', '오버나잇'))]
    for r in sorted(intra, key=lambda r: -r[1][2])[:args.top]:
        print(line(r))

    passed = [r for r in rows if r[3] and not r[0][0].startswith('대조')]
    print("\n== 결론 ==")
    if passed:
        print(f"  IS/OOS 모두 비용 차감 후 플러스 & 각 {MIN_N}건 이상: {len(passed)}개")
        for r in passed:
            print("   ", line(r))
        print("  -> backtest.py 청산 로직으로 재검증한 뒤 strategy.py 에 옮기세요.")
    else:
        print(f"  통과한 후보가 없습니다 (IS/OOS 모두 순기댓값 > 0, 각 {MIN_N}건 이상).")
        print("  이 데이터와 비용에서는 시험한 진입 조건 중 실매매에 올릴 만한 것이 없습니다.")


if __name__ == '__main__':
    main()
