#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_analysis.py - 통합 분석 실행기

filter_analysis / backtest / 파라미터 스윕을 한 번에 돌리고,
"그래서 뭘 바꿔야 하는가"를 하나의 결론으로 정리한다.

Usage:
    python run_analysis.py              # 분석만 (config.py 안 건드림)
    python run_analysis.py --apply      # 권장값을 config.py 에 실제 반영

--apply 는 표본이 충분할 때만 동작한다. 거래 100건 미만이면 거부한다.
소표본에서 뽑은 '최적값'은 과최적화라서 반영하면 오히려 나빠진다.
"""

import os, sys, re, glob, shutil
from collections import Counter

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import config
import backtest
import filter_analysis as fa

MIN_TRADES_FOR_APPLY = 100   # 이 미만이면 config 반영 거부
MIN_TRADES_FOR_HINT  = 30    # 이 미만이면 방향성 언급조차 안 함


def hr(title=''):
    print(f"\n{'='*72}")
    if title:
        print(f"  {title}")
        print('='*72)


# ─── 1단계: 필터 병목 ────────────────────────────────────────────────────────
def step_filters():
    logs = sorted(glob.glob('Report_*/signal.log'))
    if os.path.exists('logs/signal.log') and os.path.getsize('logs/signal.log') > 0:
        logs.append('logs/signal.log')
    if not logs:
        hr('1단계: 진입 필터 병목')
        print("  signal.log 없음 - 건너뜀")
        return None

    hr('1단계: 진입 필터 병목 (어떤 조건이 진입을 막는가)')
    checks = {'BREAKOUT': [], 'PULLBACK': []}
    flag, conf = Counter(), Counter()
    for p in logs:
        c, fm, cf = fa.parse_signal_log(p)
        for k in checks:
            checks[k].extend(c[k])
        flag.update(fm); conf.update(cf)

    summary = {}
    for strat in ('BREAKOUT', 'PULLBACK'):
        rows = checks[strat]
        if not rows:
            continue
        rows = [(cd, fa.normalize(c)) for cd, c in rows]
        order = list(rows[0][1].keys())
        order, _ = fa.dedupe_aliases(rows, order)
        total = len(rows)

        passed = Counter()
        sole = Counter()
        for _, cs in rows:
            fails = [k for k in order if not cs[k]]
            if len(fails) == 1:
                sole[fails[0]] += 1
            for k in order:
                if cs[k]:
                    passed[k] += 1

        tight = sorted(order, key=lambda x: passed[x])[:3]
        print(f"\n  [{strat}] 실패 {total:,}회")
        for k in tight:
            print(f"    가장 빡빡한 조건: {k:<22} 통과율 {passed[k]/total*100:>5.2f}%")
        if sole:
            top = sole.most_common(3)
            print(f"    단독 차단 (이것만 풀면 진입 후보가 되는 건수):")
            for k, c in top:
                print(f"       {k:<22} {c:>5}회")
        else:
            print(f"    단독 차단 없음 -> 조건 하나만 풀어도 진입 안 늘어남")
        summary[strat] = {'total': total, 'sole': sole, 'tight': tight}

    total_eval = sum(len(v) for v in checks.values()) + sum(flag.values())
    total_conf = sum(conf.values())
    print(f"\n  전체: 평가 {total_eval:,}회 -> 진입 {total_conf}건 "
          f"({total_conf/total_eval*100 if total_eval else 0:.4f}%)")
    return summary


# ─── 2단계: 백테스트 ─────────────────────────────────────────────────────────
def step_backtest():
    hr('2단계: 백테스트 (진입이 실제로 돈이 되는가)')
    ds = backtest.load_candle_data('candle_data')
    if not ds:
        rec = getattr(config, 'RECORD_CANDLES', False)
        print("  candle_data/ 비어 있음 - 백테스트 불가\n")
        if rec:
            print("  RECORD_CANDLES=True 이므로, 평소처럼 main.py 를 돌리면")
            print("  스캔한 분봉이 자동으로 candle_data/ 에 쌓입니다.")
            print("  >> 지금 할 일: 평소대로 자동매매 실행. 며칠 뒤 다시 분석.\n")
            print("  (과거 데이터를 지금 당장 받고 싶으면 main.py 를 끄고")
            print("   1_Collect_Data.bat 으로 백필하세요.)")
        else:
            print("  config.py 의 RECORD_CANDLES 가 False 입니다.")
            print("  True 로 바꾸면 main.py 실행 중 자동 적재됩니다.")
        return None, None

    days = len({d for d, _, _, _ in ds})
    print(f"  데이터: {days}일 / 종목-일 {len(ds)}건")
    trades, skips = backtest.run_backtest(ds)
    backtest.print_report(trades, skips, '백테스트 결과')
    return trades, ds


# ─── 3단계: 파라미터 권장 ────────────────────────────────────────────────────
def sweep_one(ds, key):
    """스윕 실행 후 (현재값, 최적값, 결과표) 반환"""
    attr, values = backtest.SWEEPS[key]
    original = getattr(config, attr)
    rows = []
    for v in values:
        setattr(config, attr, v)
        tr, _ = backtest.run_backtest(ds)
        m = backtest.metrics(tr)
        rows.append((v, m))
    setattr(config, attr, original)

    valid = [(v, m) for v, m in rows if m['n'] > 0]
    if not valid:
        return attr, original, None, rows
    best = max(valid, key=lambda x: x[1]['expectancy'])
    return attr, original, best, rows


def is_robust(rows, best_val):
    """
    최적값의 '이웃'도 좋은 성적이면 안정적(plateau), 혼자만 튀면 과최적화 의심.
    단일 스파이크를 권장하지 않기 위한 최소한의 방어.
    """
    vals = [v for v, m in rows if m['n'] > 0]
    if len(vals) < 3:
        return False
    idx = vals.index(best_val)
    best_e = dict((v, m['expectancy']) for v, m in rows if m['n'] > 0)[best_val]
    neigh = []
    if idx > 0:
        neigh.append(vals[idx-1])
    if idx < len(vals)-1:
        neigh.append(vals[idx+1])
    emap = {v: m['expectancy'] for v, m in rows if m['n'] > 0}
    # 이웃 중 하나라도 최적값의 50% 이상 성적이면 plateau 로 본다
    return any(emap[n] >= best_e * 0.5 for n in neigh) if best_e > 0 else False


def step_recommend(ds, trades):
    hr('3단계: 파라미터 권장')
    n = len(trades) if trades else 0

    if n < MIN_TRADES_FOR_HINT:
        print(f"  거래 {n}건 - 표본이 너무 적어 파라미터 권장을 하지 않습니다.")
        print(f"  최소 {MIN_TRADES_FOR_HINT}건, 신뢰하려면 {MIN_TRADES_FOR_APPLY}건 이상 필요합니다.")
        print(f"\n  지금 필요한 건 파라미터 튜닝이 아니라 데이터입니다.")
        print(f"  collect_candles.py 로 더 많은 날짜/종목을 모으세요.")
        return []

    recs = []
    print(f"  {'파라미터':<18} {'현재':>8} {'권장':>8} {'기댓값개선':>12} {'안정성':>10}")
    print(f"  {'-'*66}")
    for key in backtest.SWEEPS:
        attr, cur, best, rows = sweep_one(ds, key)
        if not best:
            continue
        best_val, best_m = best
        cur_m = next((m for v, m in rows if v == cur), None)
        cur_e = cur_m['expectancy'] if cur_m and cur_m['n'] else None
        if cur_e is None:
            continue
        delta = best_m['expectancy'] - cur_e
        robust = is_robust(rows, best_val)
        tag = '안정' if robust else '불안정'
        mark = '' if best_val == cur else ('  <<' if robust and delta > 0.05 else '')
        print(f"  {attr:<18} {cur:>8} {best_val:>8} "
              f"{delta:>+11.3f}% {tag:>10}{mark}")
        if best_val != cur and robust and delta > 0.05:
            recs.append((attr, cur, best_val, delta))

    if not recs:
        print(f"\n  -> 현재 설정을 바꿀 근거가 없습니다. (개선폭이 작거나 불안정)")
    else:
        print(f"\n  -> 변경 권장 {len(recs)}건:")
        for attr, cur, new, d in recs:
            print(f"     {attr}: {cur} -> {new}  (기댓값 {d:+.3f}%p)")
    return recs


# ─── config.py 반영 ──────────────────────────────────────────────────────────
def apply_to_config(recs, n_trades):
    hr('config.py 반영')
    if n_trades < MIN_TRADES_FOR_APPLY:
        print(f"  거부: 거래 {n_trades}건 < 기준 {MIN_TRADES_FOR_APPLY}건")
        print(f"  소표본에서 뽑은 최적값은 과최적화입니다. 반영하지 않습니다.")
        return False
    if not recs:
        print("  변경할 항목 없음")
        return False

    path = 'config.py'
    shutil.copy(path, path + '.bak')
    with open(path, encoding='utf-8') as f:
        src = f.read()

    for attr, cur, new, _ in recs:
        pat = re.compile(rf'^(\s*{attr}\s*=\s*)([0-9.]+)', re.M)
        if not pat.search(src):
            print(f"  [건너뜀] {attr} 를 config.py 에서 찾지 못함")
            continue
        src = pat.sub(rf'\g<1>{new}', src, count=1)
        print(f"  {attr}: {cur} -> {new}")

    with open(path, 'w', encoding='utf-8') as f:
        f.write(src)
    print(f"  백업: config.py.bak")
    return True


# ─── 메인 ────────────────────────────────────────────────────────────────────
def main():
    apply = '--apply' in sys.argv

    print("="*72)
    print("  통합 분석  -  filter -> backtest -> 파라미터 권장")
    print("="*72)

    step_filters()
    trades, ds = step_backtest()

    recs = []
    if ds:
        recs = step_recommend(ds, trades)

    if apply:
        apply_to_config(recs, len(trades) if trades else 0)
    elif recs:
        hr('다음 단계')
        print("  위 권장값을 실제로 반영하려면:")
        print("      python run_analysis.py --apply")
        print("  (config.py.bak 으로 백업됩니다)")

    hr('요약')
    if not ds:
        print("  현재 단계: 백테스트 데이터 축적 중")
        print("  다음 작업: 평소대로 main.py 실행 (분봉이 자동 적재됨)")
    elif len(trades) < MIN_TRADES_FOR_HINT:
        print(f"  현재 단계: 표본 부족 (거래 {len(trades)}건)")
        print(f"  다음 작업: collect_candles.py 로 데이터 추가 수집")
    elif not recs:
        print(f"  현재 단계: 파라미터는 현 상태 유지가 타당")
        print(f"  다음 작업: 전략 로직 자체 또는 진입 필터 재검토")
    else:
        print(f"  현재 단계: 파라미터 개선 여지 있음 ({len(recs)}건)")
        print(f"  다음 작업: python run_analysis.py --apply")
    print()


if __name__ == '__main__':
    main()
