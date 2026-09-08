#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py - 전략 백테스트 엔진

실제 strategy.py 진입 함수를 그대로 호출하고, kiwoom_api.py 청산 로직을
분봉 단위로 재현한다. 라이브와 같은 코드를 쓰므로 "백테스트는 되는데
실전은 안 되는" 괴리를 최소화한다.

Usage:
    python backtest.py                          # candle_data/ 전체 백테스트
    python backtest.py --data candle_data       # 데이터 폴더 지정
    python backtest.py --sweep tp                # ATR_TP_MULT 스윕
    python backtest.py --sweep sl                # ATR_SL_MULT 스윕
    python backtest.py --sweep tp1ratio          # TP1_RATIO 스윕
    python backtest.py --html backtest.html      # HTML 리포트 생성

데이터 형식 (collect_candles.py 가 생성):
    candle_data/YYYY-MM-DD/<code>.json
    {"code","name","date","interval_min","candles":[{t,o,h,l,c,v}, ...]}
    candles 는 과거→최신 순서 (시간 오름차순)
"""

import os, sys, json, argparse, statistics
from datetime import datetime
from collections import defaultdict

import config
import strategy
from strategy import (
    is_entry_candidate_VER2, is_pullback_entry, is_flag_entry,
    is_no_surge_stock, get_surge_candle_close,
)

# ─── 거래 비용 (한국 주식) ────────────────────────────────────────────────────
# 매수 수수료 + 매도 수수료 + 증권거래세. 브로커/시장에 따라 조정.
FEE_BUY_PCT   = 0.00015   # 매수 수수료 0.015%
FEE_SELL_PCT  = 0.00015   # 매도 수수료 0.015%
TAX_SELL_PCT  = 0.0015    # 증권거래세 0.15% (매도 시에만)
SLIPPAGE_PCT  = 0.0010    # 편도 슬리피지 0.10% (시장가 체결 가정)

WARMUP_BARS   = 35        # 전략 지표 계산에 필요한 최소 봉 수
WINDOW_BARS   = 60        # parse_candle 이 실제로 넘기는 최대 봉 수와 동일


# ─── 캔들 데이터 ─────────────────────────────────────────────────────────────
def load_candle_data(root):
    """candle_data/ 전체를 읽어 [(date, code, name, bars), ...] 반환"""
    if not os.path.isdir(root):
        return []
    out = []
    for date_dir in sorted(os.listdir(root)):
        dpath = os.path.join(root, date_dir)
        if not os.path.isdir(dpath):
            continue
        for fn in sorted(os.listdir(dpath)):
            if not fn.endswith('.json'):
                continue
            try:
                with open(os.path.join(dpath, fn), 'r', encoding='utf-8') as f:
                    d = json.load(f)
            except Exception as e:
                print(f"[WARN] {fn} 로드 실패: {e}")
                continue
            bars = d.get('candles') or []
            if len(bars) < WARMUP_BARS + 2:
                continue
            out.append((d.get('date', date_dir), d.get('code', fn[:-5]),
                        d.get('name', ''), bars))
    return out


def to_strategy_candles(bars, i):
    """
    bars(과거→최신) 의 index i 까지를 strategy.py 형식으로 변환.
    strategy.py 는 candles[0] 이 최신봉인 '최신→과거' 순서를 기대한다.
    kiwoom 의 parse_candle 이 최대 60봉만 넘기므로 동일하게 제한한다.
    """
    lo = max(0, i - WINDOW_BARS + 1)
    win = bars[lo:i + 1]
    return [{'open': b['o'], 'high': b['h'], 'low': b['l'],
             'close': b['c'], 'volume': b['v']} for b in reversed(win)]


def bar_minute(bar):
    """캔들 t('0905' 또는 '090500') → 자정 기준 분"""
    t = str(bar.get('t', '')).zfill(4)
    try:
        return int(t[:2]) * 60 + int(t[2:4])
    except ValueError:
        return -1


# ─── 진입 판정 ───────────────────────────────────────────────────────────────
def check_entry(bars, i):
    """
    bar i 가 막 마감된 시점의 진입 판정.
    kiwoom_api.py 와 동일한 우선순위: PULLBACK → FLAG → BREAKOUT
    반환: 전략명 또는 None
    """
    completed = to_strategy_candles(bars, i)
    if len(completed) < 20:
        return None

    # FLAG 의 live_candle: 다음 봉의 시가 = 우리가 실제로 행동하는 시점의 현재가.
    # 시가만 사용하므로 미래 정보(고가/저가/종가)는 쓰지 않는다.
    live = None
    if i + 1 < len(bars):
        nb = bars[i + 1]
        live = {'open': nb['o'], 'high': nb['o'], 'low': nb['o'],
                'close': nb['o'], 'volume': 0}

    if is_pullback_entry(completed, None, None):
        return 'PULLBACK'
    if is_flag_entry(completed, None, None, live_candle=live):
        return 'FLAG'
    if is_entry_candidate_VER2(completed, None, None):
        return 'BREAKOUT'
    return None


def passes_pre_entry_filters(bars, i, entry_type, entry_price):
    """
    진입 신호 이후 kiwoom_api.py 가 적용하는 사전 필터.
    반환: (통과여부, 차단사유)
    """
    completed = to_strategy_candles(bars, i)

    # ── ATR 최소값 필터 ──
    atr = calc_atr(completed, config.ATR_PERIOD)
    if atr <= 0:
        return False, 'ATR_ZERO'
    ratio = atr / entry_price if entry_price > 0 else 0
    if atr < config.ATR_MIN_VALUE or ratio < config.ATR_MIN_RATIO:
        return False, 'ATR_MIN'

    # ── 급등봉 없는 종목 ──
    if is_no_surge_stock(completed):
        return False, 'NO_SURGE'

    # ── BREAKOUT 고점추격 / 에너지소진 (kiwoom_api.py PUMP_SKIP) ──
    if entry_type == 'BREAKOUT':
        sc = get_surge_candle_close(completed)
        if sc > 0 and entry_price > 0:
            r = entry_price / sc
            if r > 1.02:
                return False, 'PUMP_SKIP'
            if r < 0.98:
                return False, 'ENERGY_SPENT'

    return True, ''


def calc_atr(candles, period):
    """kiwoom_api.calc_atr 과 동일 (candles[0]=최신)"""
    if len(candles) < period + 1:
        return 0.0
    win = candles[:period + 1]
    trs = []
    for i in range(period):
        cur, prev = win[i], win[i + 1]
        trs.append(max(cur['high'] - cur['low'],
                       abs(cur['high'] - prev['close']),
                       abs(cur['low'] - prev['close'])))
    return sum(trs) / len(trs)


# ─── 청산 시뮬레이션 ─────────────────────────────────────────────────────────
def simulate_exit(bars, entry_idx, entry_price, atr, pessimistic=True):
    """
    entry_idx 봉의 시가에 진입한 뒤의 청산을 분봉 단위로 시뮬레이션.

    kiwoom_api.py 청산 로직 재현:
      - 완성봉 종가 ≤ ATR 손절가       → STOP_LOSS
      - 봉 저가 ≤ 진입가×(1-2.5%)      → SL_EMERG
      - 봉 고가 ≥ TP1                  → TP1 부분매도 (TP1_RATIO)
      - TP1 후 종가 < 본절보호가        → PROFIT_SAFE
      - TP1 후 고가 ≥ TP2              → TP2 부분매도 (TP2_RATIO)
      - TP2 후 최고가 - ATR×TRAIL      → TRAIL_STOP
      - 미달 구간 +1.5% 후 고점 -1.5%   → MINI_TRAIL
      - 15분 경과 & pnl < -0.3%        → TIME_STOP
      - 15:20                          → FORCE_LIQ

    한 봉 안에서 손절/익절이 동시 충족되면 pessimistic=True 일 때 손절을
    먼저 적용한다 (분봉만으로는 선후를 알 수 없으므로 보수적 가정).

    반환: dict(exit_reason, exit_price, bars_held, mae_pct, mfe_pct)
    """
    sl_price   = entry_price - atr * config.ATR_SL_MULT
    tp1_price  = entry_price + atr * config.ATR_TP_MULT
    safe_price = entry_price - atr * config.ATR_SAFE_MULT
    emerg_price = entry_price * (1 - config.EMERGENCY_SL_RATE) \
        if config.EMERGENCY_SL_RATE > 0 else 0

    remain      = 1.0      # 잔여 비중
    realized    = 0.0      # 실현 손익 (가중, 진입가 대비 비율)
    tp1_done    = False
    tp2_done    = False
    tp2_price   = 0.0
    peak        = entry_price
    mae         = 0.0
    mfe         = 0.0
    entry_min   = bar_minute(bars[entry_idx])
    liq_min     = config.FORCE_LIQUIDATION_HOUR * 60 + config.FORCE_LIQUIDATION_MIN

    def close_out(price, reason, idx):
        pnl = realized + remain * (price - entry_price) / entry_price
        return {'exit_reason': reason, 'exit_price': price,
                'bars_held': idx - entry_idx, 'gross_pct': pnl * 100,
                'mae_pct': mae * 100, 'mfe_pct': mfe * 100}

    for i in range(entry_idx, len(bars)):
        b = bars[i]
        hi, lo, cl = b['h'], b['l'], b['c']
        cur_min = bar_minute(b)

        mae = min(mae, (lo - entry_price) / entry_price)
        mfe = max(mfe, (hi - entry_price) / entry_price)
        peak = max(peak, hi)

        # ── 강제청산 ──
        if cur_min >= liq_min:
            return close_out(cl, 'FORCE_LIQ', i)

        # ── 손절 계열 (보수적: 먼저 평가) ──
        if pessimistic:
            if emerg_price > 0 and lo <= emerg_price:
                return close_out(emerg_price, 'SL_EMERG', i)
            if i > entry_idx and cl <= sl_price:
                return close_out(cl, 'STOP_LOSS', i)
            if tp1_done and cl < safe_price:
                return close_out(cl, 'PROFIT_SAFE', i)

        # ── TP1 ──
        if not tp1_done and hi >= tp1_price:
            realized += config.TP1_RATIO * (tp1_price - entry_price) / entry_price
            remain   -= config.TP1_RATIO
            tp1_done  = True
            tp2_price = peak + atr * config.ATR_TP_MULT

        # ── TP2 ──
        elif tp1_done and not tp2_done and tp2_price > 0 and hi >= tp2_price:
            realized += config.TP2_RATIO * (tp2_price - entry_price) / entry_price
            remain   -= config.TP2_RATIO
            tp2_done  = True

        # ── 트레일링 (TP2 이후) ──
        if tp2_done:
            trail = peak - atr * config.ATR_TRAIL_MULT
            if lo <= trail:
                return close_out(trail, 'TRAIL_STOP', i)

        # ── 미니 트레일링 (TP1 미달 구간) ──
        if not tp1_done:
            gain = (peak - entry_price) / entry_price
            if gain >= config.MINI_TRAIL_TRIGGER:
                gap = config.MINI_TRAIL_GAP_OPEN if entry_min < 9 * 60 + 10 \
                    else config.MINI_TRAIL_GAP
                mt = peak * (1 - gap)
                if lo <= mt:
                    return close_out(mt, 'MINI_TRAIL', i)

        # ── 낙관적 순서일 때 손절 평가 ──
        if not pessimistic:
            if emerg_price > 0 and lo <= emerg_price:
                return close_out(emerg_price, 'SL_EMERG', i)
            if i > entry_idx and cl <= sl_price:
                return close_out(cl, 'STOP_LOSS', i)
            if tp1_done and cl < safe_price:
                return close_out(cl, 'PROFIT_SAFE', i)

        # ── 시간 손절 ──
        held_min = cur_min - entry_min
        if held_min >= config.TIME_STOP_SEC / 60:
            if (cl - entry_price) / entry_price < config.TIME_STOP_MAX_LOSS:
                return close_out(cl, 'TIME_STOP', i)

        if remain <= 0.001:
            return close_out(cl, 'TP2_FILL' if tp2_done else 'TP1_FILL', i)

    return close_out(bars[-1]['c'], 'EOD', len(bars) - 1)


# ─── 백테스트 실행 ───────────────────────────────────────────────────────────
def run_backtest(dataset, pessimistic=True, verbose=False):
    """dataset: [(date, code, name, bars)] → (trades, skip_counts)"""
    trades = []
    skips  = defaultdict(int)

    for date, code, name, bars in dataset:
        i = WARMUP_BARS
        traded = False          # traded_today: 종목당 1회 (라이브와 동일)
        while i < len(bars) - 2 and not traded:
            etype = check_entry(bars, i)
            if not etype:
                i += 1
                continue

            entry_price = bars[i + 1]['o']
            if entry_price <= 0:
                i += 1
                continue

            ok, why = passes_pre_entry_filters(bars, i, etype, entry_price)
            if not ok:
                skips[why] += 1
                i += 1
                continue

            completed = to_strategy_candles(bars, i)
            atr = calc_atr(completed, config.ATR_PERIOD)

            fill = entry_price * (1 + SLIPPAGE_PCT)
            r = simulate_exit(bars, i + 1, fill, atr, pessimistic)

            cost_pct = (FEE_BUY_PCT + FEE_SELL_PCT + TAX_SELL_PCT
                        + SLIPPAGE_PCT) * 100
            net = r['gross_pct'] - cost_pct

            trades.append({
                'date': date, 'code': code, 'name': name, 'strategy': etype,
                'entry_time': bars[i + 1].get('t', ''), 'entry_price': fill,
                'atr': atr, 'net_pct': net, **r,
            })
            traded = True
            if verbose:
                print(f"  {date} {name}({code}) {etype} "
                      f"{net:+.2f}% [{r['exit_reason']}]")
        # while
    return trades, skips


# ─── 성과 지표 ───────────────────────────────────────────────────────────────
def metrics(trades):
    if not trades:
        return {'n': 0}
    pnl = [t['net_pct'] for t in trades]
    wins = [p for p in pnl if p > 0]
    loss = [p for p in pnl if p <= 0]

    gross_win  = sum(wins)
    gross_loss = abs(sum(loss))
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')

    # 최대 낙폭 (거래 순서대로 누적)
    eq, peak, mdd = 0.0, 0.0, 0.0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)

    return {
        'n': len(trades),
        'win_rate': len(wins) / len(trades) * 100,
        'avg_win': statistics.mean(wins) if wins else 0,
        'avg_loss': statistics.mean(loss) if loss else 0,
        'expectancy': statistics.mean(pnl),
        'total': sum(pnl),
        'pf': pf,
        'mdd': mdd,
        'avg_bars': statistics.mean([t['bars_held'] for t in trades]),
        'avg_mfe': statistics.mean([t['mfe_pct'] for t in trades]),
        'avg_mae': statistics.mean([t['mae_pct'] for t in trades]),
    }


def print_report(trades, skips, title='BACKTEST'):
    m = metrics(trades)
    print(f"\n{'='*66}\n  {title}\n{'='*66}")
    if m['n'] == 0:
        print("  거래 없음 - 필터가 모든 신호를 차단했거나 데이터 부족")
        if skips:
            print("\n  사전필터 차단:")
            for k, v in sorted(skips.items(), key=lambda x: -x[1]):
                print(f"    {k:<14} {v:>5}건")
        return

    print(f"  거래수        {m['n']:>8}")
    print(f"  승률          {m['win_rate']:>7.1f}%")
    print(f"  기댓값        {m['expectancy']:>+7.3f}%   <- 거래당 평균 (비용 차감 후)")
    print(f"  누적손익      {m['total']:>+7.2f}%")
    print(f"  평균수익      {m['avg_win']:>+7.2f}%")
    print(f"  평균손실      {m['avg_loss']:>+7.2f}%")
    print(f"  Profit Factor {m['pf']:>7.2f}")
    print(f"  최대낙폭      {m['mdd']:>+7.2f}%")
    print(f"  평균보유      {m['avg_bars']:>7.1f}봉")
    print(f"  평균 MFE/MAE  {m['avg_mfe']:>+6.2f}% / {m['avg_mae']:+.2f}%")

    by_strat = defaultdict(list)
    for t in trades:
        by_strat[t['strategy']].append(t)
    print(f"\n  ── 전략별 ──")
    print(f"  {'전략':<10} {'건수':>5} {'승률':>7} {'기댓값':>9}")
    for s, ts in sorted(by_strat.items(), key=lambda x: -len(x[1])):
        sm = metrics(ts)
        print(f"  {s:<10} {sm['n']:>5} {sm['win_rate']:>6.1f}% "
              f"{sm['expectancy']:>+8.3f}%")

    by_exit = defaultdict(list)
    for t in trades:
        by_exit[t['exit_reason']].append(t)
    print(f"\n  ── 청산사유별 ──")
    print(f"  {'사유':<14} {'건수':>5} {'비중':>7} {'평균손익':>10}")
    for r, ts in sorted(by_exit.items(), key=lambda x: -len(x[1])):
        avg = statistics.mean([t['net_pct'] for t in ts])
        print(f"  {r:<14} {len(ts):>5} {len(ts)/m['n']*100:>6.1f}% {avg:>+9.2f}%")

    if skips:
        print(f"\n  ── 사전필터 차단 ──")
        for k, v in sorted(skips.items(), key=lambda x: -x[1]):
            print(f"  {k:<14} {v:>5}건")


# ─── 파라미터 스윕 ───────────────────────────────────────────────────────────
SWEEPS = {
    'tp':       ('ATR_TP_MULT',   [0.8, 1.0, 1.2, 1.5, 2.0, 2.5]),
    'sl':       ('ATR_SL_MULT',   [1.0, 1.5, 2.0, 2.5, 3.0]),
    'tp1ratio': ('TP1_RATIO',     [0.3, 0.4, 0.55, 0.7, 1.0]),
    'trail':    ('ATR_TRAIL_MULT',[1.0, 1.5, 2.0, 2.5]),
    'safe':     ('ATR_SAFE_MULT', [0.5, 1.0, 1.5, 2.0]),
    'vol':      ('BREAKOUT_VOL_RATIO_MIN', [2.0, 3.0, 4.0, 5.0, 7.0]),
    'daysurge': ('BREAKOUT_DAY_SURGE_MAX', [10.0, 15.0, 20.0, 25.0]),
}


def run_sweep(dataset, key, pessimistic=True):
    if key not in SWEEPS:
        print(f"[ERROR] 알 수 없는 스윕: {key}. 가능: {', '.join(SWEEPS)}")
        return
    attr, values = SWEEPS[key]
    original = getattr(config, attr)

    print(f"\n{'='*66}\n  파라미터 스윕: {attr}\n{'='*66}")
    print(f"  {attr:>16} {'거래':>6} {'승률':>8} {'기댓값':>10} {'PF':>8} {'MDD':>9}")
    print(f"  {'-'*62}")
    best = None
    for v in values:
        setattr(config, attr, v)
        trades, _ = run_backtest(dataset, pessimistic)
        m = metrics(trades)
        if m['n'] == 0:
            print(f"  {v:>16} {0:>6}  {'-':>7} {'-':>9} {'-':>7} {'-':>8}")
            continue
        mark = ''
        if best is None or m['expectancy'] > best[1]:
            best, mark = (v, m['expectancy']), ''
        print(f"  {v:>16} {m['n']:>6} {m['win_rate']:>7.1f}% "
              f"{m['expectancy']:>+9.3f}% {m['pf']:>7.2f} {m['mdd']:>+8.2f}%")
    setattr(config, attr, original)
    if best:
        print(f"\n  최고 기댓값: {attr}={best[0]} ({best[1]:+.3f}%)")
        print(f"  현재 설정값: {attr}={original}")
        print(f"\n  [!] 표본이 작으면 이 '최적값'은 과최적화(overfitting)입니다.")
        print(f"    거래 100건 미만이면 참고만 하세요.")


# ─── 자체 검증 ───────────────────────────────────────────────────────────────
def selftest():
    """청산 시뮬레이션의 손익 계산을 알려진 정답과 대조한다."""
    saved = {k: getattr(config, k) for k in
             ('ATR_SL_MULT', 'ATR_TP_MULT', 'ATR_SAFE_MULT', 'TP1_RATIO',
              'TP2_RATIO', 'ATR_TRAIL_MULT', 'EMERGENCY_SL_RATE')}
    config.ATR_SL_MULT, config.ATR_TP_MULT, config.ATR_SAFE_MULT = 2.0, 1.2, 1.0
    config.TP1_RATIO, config.TP2_RATIO = 0.55, 0.40
    config.ATR_TRAIL_MULT, config.EMERGENCY_SL_RATE = 1.5, 0.025

    def bar(t, o, h, l, c, v=1000):
        return {'t': t, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}

    # 진입가 1000, ATR 10 -> SL 980 / TP1 1012 / 본절 990 / 긴급 975
    cases = [
        ('STOP_LOSS',
         [bar('1000',1000,1000,1000,1000), bar('1003',1000,1002,978,979)],
         -2.10),
        ('SL_EMERG',
         [bar('1000',1000,1000,1000,1000), bar('1003',1000,1002,970,1000)],
         -2.50),
        ('FORCE_LIQ',
         [bar('1500',1000,1000,1000,1000), bar('1520',1000,1005,995,1003)],
         +0.30),
        ('PROFIT_SAFE',
         [bar('1000',1000,1000,1000,1000), bar('1003',1000,1015,1000,1010),
          bar('1006',1010,1010,982,985)],
         (0.55*12 + 0.45*(-15)) / 10),
        ('TRAIL_STOP',
         [bar('1000',1000,1000,1000,1000), bar('1003',1000,1015,1005,1014),
          bar('1006',1014,1030,1012,1028), bar('1009',1028,1030,1010,1016)],
         (0.55*12 + 0.40*27 + 0.05*15) / 10),
    ]

    ok = True
    print("[자체검증] 청산 손익 계산")
    for want_reason, bars, want_pct in cases:
        r = simulate_exit(bars, 0, 1000, 10.0)
        good = (r['exit_reason'] == want_reason
                and abs(r['gross_pct'] - want_pct) < 0.001)
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {want_reason:<12} "
              f"got={r['exit_reason']:<12} {r['gross_pct']:+.4f}% "
              f"(기대 {want_pct:+.4f}%)")

    for k, v in saved.items():
        setattr(config, k, v)
    print(f"[자체검증] {'전체 통과' if ok else '실패 있음'}")
    return ok


# ─── 메인 ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description='전략 백테스트 엔진')
    ap.add_argument('--selftest', action='store_true',
                    help='청산 로직 자체 검증 후 종료')
    ap.add_argument('--data', default='candle_data', help='캔들 데이터 폴더')
    ap.add_argument('--sweep', help=f"파라미터 스윕: {', '.join(SWEEPS)}")
    ap.add_argument('--optimistic', action='store_true',
                    help='한 봉 내 익절을 손절보다 먼저 평가 (낙관적)')
    ap.add_argument('--verbose', action='store_true', help='개별 거래 출력')
    ap.add_argument('--csv', help='거래 내역 CSV 저장 경로')
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    print(f"[백테스트] 데이터 로딩: {args.data}/")
    dataset = load_candle_data(args.data)
    if not dataset:
        print(f"""
[데이터 없음] {args.data}/ 에 캔들 데이터가 없습니다.

  먼저 데이터를 수집하세요 (32비트 파이썬 필수):
      C:\\Users\\user\\AppData\\Local\\Programs\\Python\\Python311-32\\python.exe collect_candles.py

  수집기는 키움 OpenAPI+ 로그인이 필요하며, OPT10080 으로
  종목별 {config.CANDLE_INTERVAL_MIN}분봉을 받아 candle_data/ 에 저장합니다.

  엔진만 먼저 검증하려면:
      python backtest.py --selftest
""")
        sys.exit(1)

    days = len({d for d, _, _, _ in dataset})
    print(f"[백테스트] {days}일 / 종목-일 {len(dataset)}건 로드")
    print(f"[설정] 봉={config.CANDLE_INTERVAL_MIN}분  "
          f"SL×{config.ATR_SL_MULT}  TP×{config.ATR_TP_MULT}  "
          f"TP1={config.TP1_RATIO}")
    cost = (FEE_BUY_PCT + FEE_SELL_PCT + TAX_SELL_PCT + SLIPPAGE_PCT) * 100
    print(f"[비용] 왕복 {cost:.3f}% (수수료+거래세+슬리피지)")

    pess = not args.optimistic
    if not pess:
        print("[주의] --optimistic: 한 봉 내 익절 우선. 실제보다 후하게 나옵니다.")

    if args.sweep:
        run_sweep(dataset, args.sweep, pess)
        return

    trades, skips = run_backtest(dataset, pess, args.verbose)
    print_report(trades, skips)

    if args.csv and trades:
        import csv as _csv
        with open(args.csv, 'w', newline='', encoding='utf-8-sig') as f:
            w = _csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
        print(f"\n[저장] {args.csv} ({len(trades)}건)")

    n = len(trades)
    if 0 < n < 100:
        print(f"\n[!] 거래 {n}건 - 통계적 결론을 내기엔 부족합니다 (권장 100건+).")


if __name__ == '__main__':
    main()
