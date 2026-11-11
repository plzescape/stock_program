#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tick_research.py - 녹화 데이터(tick_data/) 진입 후보 검증기 (연구용, 실매매 코드와 분리)

entry_research / swing_research 는 가격·거래량만 봤고 비용을 넘는 우위가 없었다.
여기서는 차트에 없는 정보(체결강도, 매수/매도 체결 방향, 호가 잔량)로 진입 조건을 시험한다.

[체결 가정]
  - 신호: 초 단위로 묶은 체결 데이터의 그 초가 끝난 시점에 판단
  - 매수: 다음 초 첫 체결의 매도1호가(ask1) 로 산다 (시장가 매수 = 스프레드 지불)
  - 매도: 청산 시점의 매수1호가(bid1) 로 판다
  - 비용: 수수료+세금 (스프레드는 위 체결가에 이미 반영되어 슬리피지는 따로 빼지 않음)
  - 진입 09:05~15:00, 등락률 28% 이상(상한가 근처)은 제외
  - 같은 종목·같은 가설은 진입 후 COOLDOWN_SEC 동안 재진입 금지
  - 청산 시점까지 녹화가 끊긴 종목(최대 종목 수 초과로 녹화 해제 등)은 제외

[판정]
  - 날짜로 IS(앞) / OOS(뒤) 를 나눈다. 둘 다 비용 차감 후 플러스 & 각 MIN_N 건 이상 → 1차 통과
  - t군집 = 종목·날짜별 평균으로 묶은 t값 (같은 종목에서 연달아 나온 신호는 독립이 아님)
  - ★유력 = 1차 통과 + OOS t군집 ≥ 2 + 날짜별로 봐도 대부분의 날 플러스
  - 대조군: 임의 진입(≈ 비용만큼 마이너스), 미래참조 치트(크게 플러스). 어긋나면 도구 버그.
  - 녹화 일수가 적으면(< 20일) 결과는 참고용. 며칠의 장세가 전부를 좌우한다.

Usage:
    python tick_research.py
    python tick_research.py --split 2026-11-15     # 이 날짜 이전 IS / 이후 OOS
    python tick_research.py --days 2026-11-03,2026-11-05
"""

import argparse, bisect, collections, csv, gzip, io, math, os, random, statistics, time

import backtest

ROOT = 'tick_data'
COOLDOWN_SEC = 300
MIN_N = 50
ENTRY_START = 9 * 3600 + 5 * 60
ENTRY_END = 15 * 3600
LIMIT_NEAR = 28.0                 # 등락률(%) 이 이 이상이면 상한가 근처 → 제외
MIN_DAYS_RELIABLE = 20

EXITS = [('1분', 60), ('3분', 180), ('5분', 300), ('10분', 600), ('SL0.7/TP1.5/10분', None)]
SL, TP, SLTP_HOLD = -0.007, 0.015, 600
MAX_HOLD = 600


def cost_pct():
    """수수료 + 세금 (%). 체결가가 실제 호가라서 슬리피지는 넣지 않는다."""
    return (backtest.FEE_BUY_PCT + backtest.FEE_SELL_PCT + backtest.TAX_SELL_PCT) * 100


# ─── 데이터 ──────────────────────────────────────────────────────────────────
def _open(path):
    if path.endswith('.gz'):
        return io.TextIOWrapper(gzip.open(path, 'rb'), encoding='utf-8', newline='')
    return open(path, encoding='utf-8', newline='')


def _num(s, f=float):
    try:
        return f(str(s).replace('+', '').replace(',', '').strip() or 0)
    except ValueError:
        return f(0)


def _sec(hhmmss):
    s = str(hhmmss).strip()[:6]
    if len(s) < 6 or not s.isdigit():
        return None
    return int(s[:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:6])


def _norm(code):
    c = str(code).strip()
    if c[:1] in 'AJQ' and len(c) == 7 and c[1:2].isdigit():
        c = c[1:]
    return c


class Bars:
    """초 단위로 묶은 체결 + 호가잔량. 리스트들은 같은 길이 (초 순서)."""
    __slots__ = ('sec', 'price', 'ask_first', 'bid_last', 'buy', 'sell', 'amt', 'strength',
                 'chg', 'high', 'ask_tot', 'bid_tot', 'cbuy', 'csell', 'camt')


def load_bars(trade_path, book_path):
    rows = collections.OrderedDict()
    with _open(trade_path) as f:
        for r in csv.DictReader(f):
            s = _sec(r.get('exch_time')) or _sec(r.get('recv_time'))
            p = abs(_num(r.get('price'), int))
            if s is None or p <= 0:
                continue
            v = _num(r.get('vol'), int)
            ask, bid = abs(_num(r.get('ask1'), int)), abs(_num(r.get('bid1'), int))
            b = rows.get(s)
            if b is None:
                b = rows[s] = [p, ask or p, bid or p, 0, 0, 0.0, None, 0.0, 0]
            b[0] = p
            b[2] = bid or p
            if v > 0:
                b[3] += v
            else:
                b[4] += -v
            b[5] += p * abs(v)
            st = r.get('strength')
            if st not in (None, ''):
                b[6] = _num(st)
            if r.get('chg_rate') not in (None, ''):
                b[7] = _num(r.get('chg_rate'))
            h = abs(_num(r.get('high'), int))
            if h:
                b[8] = h
    if not rows:
        return None
    book_s, book_a, book_b = [], [], []
    if book_path and os.path.exists(book_path):
        with _open(book_path) as f:
            for r in csv.DictReader(f):
                s = _sec(r.get('exch_time')) or _sec(r.get('recv_time'))
                a, bb = _num(r.get('ask_total'), int), _num(r.get('bid_total'), int)
                if s is None or a <= 0 or bb <= 0:
                    continue
                if book_s and s < book_s[-1]:
                    continue
                book_s.append(s); book_a.append(a); book_b.append(bb)

    x = Bars()
    x.sec = list(rows.keys())
    vals = list(rows.values())
    x.price = [v[0] for v in vals]
    x.ask_first = [v[1] for v in vals]
    x.bid_last = [v[2] for v in vals]
    x.buy = [v[3] for v in vals]
    x.sell = [v[4] for v in vals]
    x.amt = [v[5] for v in vals]
    last_st, st = None, []
    for v in vals:
        last_st = v[6] if v[6] is not None else last_st
        st.append(last_st)
    x.strength = st
    x.chg = [v[7] for v in vals]
    hi, highs = 0, []
    for v in vals:
        hi = max(hi, v[8] or v[0])
        highs.append(hi)
    x.high = highs
    x.ask_tot, x.bid_tot = [], []
    j = -1
    for s in x.sec:
        while j + 1 < len(book_s) and book_s[j + 1] <= s:
            j += 1
        ok = j >= 0 and s - book_s[j] <= 5
        x.ask_tot.append(book_a[j] if ok else 0)
        x.bid_tot.append(book_b[j] if ok else 0)
    # 누적합 (구간 합을 O(1) 로)
    x.cbuy, x.csell, x.camt = [0], [0], [0.0]
    for i in range(len(x.sec)):
        x.cbuy.append(x.cbuy[-1] + x.buy[i])
        x.csell.append(x.csell[-1] + x.sell[i])
        x.camt.append(x.camt[-1] + x.amt[i])
    return x


def load_events(day_dir):
    """종목별 조건검색 편입(COND_IN) 시각"""
    out = collections.defaultdict(list)
    for fn in ('events.csv', 'events.csv.gz'):
        p = os.path.join(day_dir, fn)
        if not os.path.exists(p):
            continue
        with _open(p) as f:
            for r in csv.DictReader(f):
                if r.get('event') == 'COND_IN':
                    s = _sec(r.get('recv_time'))
                    if s is not None:
                        out[_norm(r.get('code'))].append(s)
    return out


# ─── 특징 ────────────────────────────────────────────────────────────────────
class Feat:
    """초 i 시점에서 과거 구간 특징을 구한다 (미래 데이터는 안 씀)"""

    def __init__(self, x):
        self.x = x

    def idx_at(self, sec):
        """sec 이하인 마지막 초의 인덱스"""
        return bisect.bisect_right(self.x.sec, sec) - 1

    def window(self, i, span):
        """(i-span초, i] 구간의 (매수량, 매도량, 거래대금)"""
        x = self.x
        lo = bisect.bisect_right(x.sec, x.sec[i] - span)
        return (x.cbuy[i + 1] - x.cbuy[lo], x.csell[i + 1] - x.csell[lo], x.camt[i + 1] - x.camt[lo])

    def base_amt60(self, i):
        """직전 5분(1분 전부터)의 1분당 평균 거래대금. 녹화 시작 6분 전이면 None"""
        x = self.x
        if x.sec[i] - x.sec[0] < 360:
            return None
        lo = bisect.bisect_right(x.sec, x.sec[i] - 360)
        hi = bisect.bisect_right(x.sec, x.sec[i] - 60)
        return (x.camt[hi] - x.camt[lo]) / 5.0

    def ago(self, arr, i, span):
        j = self.idx_at(self.x.sec[i] - span)
        return arr[j] if j >= 0 else None


def buy_share(b, s):
    return b / (b + s) if b + s > 0 else 0.5


# ─── 가설 ────────────────────────────────────────────────────────────────────
def hyp_signals(x, f, cond_in, rng):
    """이 종목·날짜에서 가설별 신호 인덱스 목록 (쿨다운 전)"""
    sig = collections.defaultdict(list)
    n = len(x.sec)
    cond_sorted = sorted(cond_in)
    ci = 0
    for i in range(n - 1):
        s = x.sec[i]
        if s < ENTRY_START or s >= ENTRY_END or x.chg[i] >= LIMIT_NEAR:
            # 조건 편입 시각이 장 초반/제외 구간이면 그 편입은 버린다
            while ci < len(cond_sorted) and cond_sorted[ci] <= s:
                ci += 1
            continue
        if ci < len(cond_sorted) and cond_sorted[ci] <= s:
            sig['COND_IN (현행 후보 전체)'].append(i)
            while ci < len(cond_sorted) and cond_sorted[ci] <= s:
                ci += 1

        if rng.random() < 0.003:
            sig['대조:임의진입'].append(i)

        b60, s60, a60 = f.window(i, 60)
        share60 = buy_share(b60, s60)
        base = f.base_amt60(i)
        burst = (a60 / base) if base else None
        p0 = x.price[i]
        p60 = f.ago(x.price, i, 60)
        ret60 = (p0 / p60 - 1) if p60 else None
        st, st60 = x.strength[i], f.ago(x.strength, i, 60)

        if st is not None and st60 is not None and st >= 150 and st - st60 >= 10:
            sig['STR150_UP 체결강도150↑+60초+10'].append(i)
        if st is not None and st60 is not None and st >= 200 and st - st60 >= 20:
            sig['STR200_UP 체결강도200↑+60초+20'].append(i)
        if burst and burst >= 3 and share60 >= 0.70 and a60 >= 5e7:
            sig['BUY70_BURST 매수체결70%+대금3배'].append(i)
        if burst and burst >= 5 and ret60 is not None and ret60 >= 0.01:
            sig['BURST5_UP 대금5배+60초+1%'].append(i)
        if burst and burst >= 2 and share60 >= 0.65 and p0 >= x.high[i]:
            sig['NEWHIGH_BUY 당일고가+매수65%'].append(i)
        if burst and burst >= 3 and share60 <= 0.30 and ret60 is not None and ret60 <= -0.015:
            sig['SELLFLUSH_FADE 투매후역추세'].append(i)
        at, bt = x.ask_tot[i], x.bid_tot[i]
        if at and bt:
            if bt >= 2 * at and a60 >= 3e7:
                sig['BOOK_BID2 매수잔량2배'].append(i)
            if at >= 2 * bt and a60 >= 3e7:
                sig['BOOK_ASK2 매도잔량2배'].append(i)
            if at >= 2 * bt and share60 >= 0.65 and burst and burst >= 2:
                sig['ASKWALL_EAT 매도벽+매수체결'].append(i)
        if (st is not None and st60 is not None and st >= 150 and st - st60 >= 10
                and burst and burst >= 3 and share60 >= 0.70):
            sig['COMBO 체결강도↑+매수폭발'].append(i)
    return sig


def breakout_signals(x):
    """
    현행 BREAKOUT(3분봉) 을 녹화 체결로 재현해 '봉 마감 확인' 과 '봉 안에서 미리 진입' 을 비교한다.
    (간이판: 직전 5봉 고점 돌파 + 거래량 배수 + 양봉 + 당일상승 5~10%. EMA·연속양봉 조건은 생략)
      BRK_CLOSE      : 봉 마감 직후 (현행 방식의 최선. 실제로는 분봉 조회 지연이 더 붙는다)
      BRK_CLOSE+10초 : 봉 마감 10초 뒤 (REST 분봉 조회 지연 근사)
      BRK_EARLY_v5   : 봉 진행 중, 가격이 직전 5봉 고점을 넘고 그 봉 거래량이 이미 5배일 때
      BRK_EARLY_v3   : 같은데 거래량 3배 (더 이르다)
    """
    sig = collections.defaultdict(list)
    done = []                         # 완성봉 [open, high, close, vol]
    cur_id, cur = None, None
    early_fired = {}
    pending_close = None              # (마감된 봉 정보로 판정, 10초 뒤 진입할 시각)

    def ok_time(i):
        return ENTRY_START <= x.sec[i] < ENTRY_END and x.chg[i] < LIMIT_NEAR

    for i, s in enumerate(x.sec):
        bid = s // 180
        if bid != cur_id:
            if cur is not None:
                done.append(cur)
                if len(done) >= 6:
                    b, prev = done[-1], done[-6:-1]
                    h5 = max(p[1] for p in prev)
                    v5 = sum(p[3] for p in prev) / 5
                    if (b[2] > h5 and v5 > 0 and b[3] >= 5 * v5 and b[2] > b[0]
                            and 5 <= x.chg[i - 1] <= 10 and ok_time(i)):
                        sig['BRK_CLOSE 봉마감 확인'].append(i)
                        pending_close = (bid * 180 + 10)
            cur_id, cur = bid, [x.price[i], x.price[i], x.price[i], 0]
        cur[1] = max(cur[1], x.price[i])
        cur[2] = x.price[i]
        cur[3] += x.buy[i] + x.sell[i]
        if pending_close is not None and s >= pending_close:
            if s - pending_close <= 5 and ok_time(i):
                sig['BRK_CLOSE+10초 (REST 지연)'].append(i)
            pending_close = None
        if len(done) >= 5 and ok_time(i) and 5 <= x.chg[i] <= 10 and x.price[i] > cur[0]:
            prev = done[-5:]
            h5 = max(p[1] for p in prev)
            v5 = sum(p[3] for p in prev) / 5
            if v5 > 0 and x.price[i] > h5:
                for name, mult in (('BRK_EARLY_v5 봉중 돌파+거래량5배', 5), ('BRK_EARLY_v3 봉중 돌파+거래량3배', 3)):
                    if cur[3] >= mult * v5 and early_fired.get(name) != bid:
                        sig[name].append(i)
                        early_fired[name] = bid
    return sig


# ─── 체결 / 청산 ─────────────────────────────────────────────────────────────
def simulate(x, i, exits):
    """신호 초 i → 다음 초 매수. {청산명: 수익률(%)} 또는 None(체결 불가/데이터 끊김)"""
    if i + 1 >= len(x.sec) or x.sec[i + 1] - x.sec[i] > 5:
        return None
    e = i + 1
    entry = x.ask_first[e]
    if entry <= 0:
        return None
    t0 = x.sec[e]
    if x.sec[-1] < t0 + MAX_HOLD:
        return None                     # 녹화가 청산 전에 끊김
    out = {}
    for name, hold in exits:
        if hold is not None:
            j = bisect.bisect_right(x.sec, t0 + hold) - 1
            out[name] = (x.bid_last[j] / entry - 1) * 100
        else:
            j_end = bisect.bisect_right(x.sec, t0 + SLTP_HOLD) - 1
            r = None
            for j in range(e, j_end + 1):
                q = x.bid_last[j] / entry - 1
                if q <= SL or q >= TP:
                    r = q
                    break
            out[name] = (r if r is not None else x.bid_last[j_end] / entry - 1) * 100
    return out


def cheat_signals(x, cands):
    """미래참조 치트: 5분 뒤 매수1호가가 지금 매도1호가보다 1% 이상 높은 초만 (도구 점검용)"""
    out = []
    for i in cands:
        if i + 1 >= len(x.sec):
            continue
        j = bisect.bisect_right(x.sec, x.sec[i + 1] + 300) - 1
        if x.bid_last[j] >= x.ask_first[i + 1] * 1.01:
            out.append(i)
    return out


def apply_cooldown(x, idxs):
    out, next_ok = [], -1
    for i in idxs:
        if x.sec[i] >= next_ok:
            out.append(i)
            next_ok = x.sec[i] + COOLDOWN_SEC
    return out


# ─── 집계 ────────────────────────────────────────────────────────────────────
def stats(xs, cost):
    """xs: [(day, code, ret%)]"""
    if not xs:
        return dict(n=0, gross=0.0, net=0.0, win=0.0, t=0.0, clusters=0)
    rets = [r for _, _, r in xs]
    cl = collections.defaultdict(list)
    for d, c, r in xs:
        cl[(d, c)].append(r - cost)
    cm = [statistics.mean(v) for v in cl.values()]
    sd = statistics.stdev(cm) if len(cm) > 2 else 0
    t = statistics.mean(cm) / sd * math.sqrt(len(cm)) if sd > 0 else 0.0
    m = statistics.mean(rets)
    return dict(n=len(rets), gross=m, net=m - cost, win=sum(r > cost for r in rets) / len(rets) * 100,
                t=t, clusters=len(cm))


def by_day(xs, cost):
    d = collections.defaultdict(list)
    for day, _, r in xs:
        d[day].append(r - cost)
    return {k: statistics.mean(v) for k, v in sorted(d.items())}


def fmt(s):
    return f"{s['n']:6d} {s['gross']:+7.2f} {s['net']:+7.2f} {s['win']:5.1f}% {s['t']:5.1f}"


def main():
    ap = argparse.ArgumentParser(description='녹화 데이터(tick_data) 진입 후보 검증기')
    ap.add_argument('--root', default=ROOT)
    ap.add_argument('--split', help='이 날짜(YYYY-MM-DD) 이전 IS / 이후 OOS. 생략하면 날짜 수의 절반')
    ap.add_argument('--days', help='쉼표구분 날짜만 사용')
    ap.add_argument('--top', type=int, default=30)
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        print(f"[데이터] {args.root}/ 없음 - main.py 모의투자를 돌리면 쌓인다")
        return
    days = sorted(d for d in os.listdir(args.root) if os.path.isdir(os.path.join(args.root, d)))
    if args.days:
        want = {d.strip() for d in args.days.split(',')}
        days = [d for d in days if d in want]
    if len(days) < 2:
        print(f"[데이터] 녹화 일수 {len(days)}일 - 최소 2일(IS/OOS) 필요")
        return
    split = args.split or days[(len(days) + 1) // 2]
    is_days = [d for d in days if d < split]
    oos_days = [d for d in days if d >= split]
    if not is_days or not oos_days:
        print(f"[분할] {split} 기준으로 IS 또는 OOS 가 비어 있음: {days}")
        return
    cost = cost_pct()
    exits = EXITS

    t0 = time.time()
    print(f"[데이터] {args.root}/ {len(days)}일: {', '.join(days)}")
    print(f"[분할] IS {is_days} / OOS {oos_days}")
    print(f"[체결] 다음 초 매도1호가 매수 → 매수1호가 매도   [비용] 수수료+세금 {cost:.2f}%   "
          f"[쿨다운] {COOLDOWN_SEC}초", flush=True)

    agg = collections.defaultdict(list)       # (hyp, exit, 'IS'|'OOS') → [(day, code, ret)]
    agg_all = collections.defaultdict(list)   # (hyp, exit) → 전체 (날짜별 표용)
    skipped = collections.Counter()
    n_codes = 0
    for day in days:
        ddir = os.path.join(args.root, day)
        events = load_events(ddir)
        files = sorted(fn for fn in os.listdir(ddir) if '.trade.csv' in fn)
        part = 'IS' if day < split else 'OOS'
        for fn in files:
            code = fn.split('.')[0]
            book = os.path.join(ddir, fn.replace('.trade.', '.book.'))
            if not os.path.exists(book):
                book = book[:-3] if book.endswith('.gz') else book + '.gz'
            x = load_bars(os.path.join(ddir, fn), book)
            if x is None or len(x.sec) < 30:
                skipped['체결부족'] += 1
                continue
            n_codes += 1
            f = Feat(x)
            rng = random.Random(f"{day}:{code}")
            sig = hyp_signals(x, f, events.get(code, []), rng)
            sig.update(breakout_signals(x))
            cands = [i for i in range(len(x.sec) - 1)
                     if ENTRY_START <= x.sec[i] < ENTRY_END and x.chg[i] < LIMIT_NEAR]
            sig['대조:미래참조 치트'] = cheat_signals(x, cands)
            for hyp, idxs in sig.items():
                for i in apply_cooldown(x, idxs):
                    res = simulate(x, i, exits)
                    if res is None:
                        skipped['청산전 녹화끊김/체결불가'] += 1
                        continue
                    for ex, r in res.items():
                        agg[(hyp, ex, part)].append((day, code, r))
                        agg_all[(hyp, ex)].append((day, code, r))
        print(f"  {day}: {len(files)}종목 처리 ({time.time() - t0:.0f}초)", flush=True)
    print(f"[계산] {time.time() - t0:.0f}초  종목·날짜 {n_codes}개   제외: {dict(skipped)}\n")

    head = (f"{'후보':34s} {'청산':>16s} |   IS n     세전      순    승률 t군집 |"
            f"  OOS n     세전      순    승률 t군집 | 판정")
    rows = []
    for hyp, ex in {(h, e) for h, e, _ in agg}:
        i_ = stats(agg[(hyp, ex, 'IS')], cost)
        o_ = stats(agg[(hyp, ex, 'OOS')], cost)
        daily = by_day(agg_all[(hyp, ex)], cost)
        pos_days = sum(v > 0 for v in daily.values())
        first = i_['n'] >= MIN_N and o_['n'] >= MIN_N and i_['net'] > 0 and o_['net'] > 0
        strong = first and o_['t'] >= 2 and i_['t'] > 0 and pos_days >= max(2, math.ceil(len(daily) * 0.75))
        rows.append((hyp, ex, i_, o_, daily, first, strong))

    def line(r):
        hyp, ex, i_, o_, _, first, strong = r
        mark = '★유력' if strong else ('1차통과' if first else '')
        return f"{hyp:34s} {ex:>16s} | {fmt(i_)} | {fmt(o_)} | {mark}"

    print("== 대조군 (도구 점검: 임의진입 ≈ 비용만큼 마이너스, 치트는 크게 플러스여야 정상) ==")
    print(head)
    for r in sorted((r for r in rows if r[0].startswith('대조') and r[1] == '5분'), key=lambda r: r[0]):
        print(line(r))
    print()
    cand = [r for r in rows if not r[0].startswith('대조')]
    cand.sort(key=lambda r: -r[2]['net'])
    n_hyp = len({r[0] for r in cand})
    print(f"== 후보 (IS 순수익 상위 {args.top} / 전체 {len(cand)}개 조합 = 가설 {n_hyp} × 청산 {len(exits)}) ==")
    print(head)
    for r in cand[:args.top]:
        print(line(r))

    print("\n== 돌파 진입 시점 비교 (전체 기간 합산: 봉 마감 확인 vs 봉 안에서 미리) ==")
    print(f"{'후보':34s} {'청산':>16s} |    n     세전      순    승률 t군집 | 날짜별 순수익")
    order = {e: k for k, (e, _) in enumerate(exits)}
    for r in sorted((r for r in rows if r[0].startswith('BRK_')), key=lambda r: (r[0], order[r[1]])):
        a = stats(agg_all[(r[0], r[1])], cost)
        print(f"{r[0]:34s} {r[1]:>16s} | {fmt(a)} | "
              + '  '.join(f"{d[5:]} {v:+.2f}" for d, v in r[4].items()))

    print("\n== 날짜별 순수익(%) - 1차 통과 조합 (며칠에만 몰렸는지 확인) ==")
    firsts = [r for r in cand if r[5]]
    if not firsts:
        print("  (1차 통과 없음)")
    for r in firsts[:15]:
        print(f"  {r[0]:34s} {r[1]:>16s} | " + '  '.join(f"{d[5:]} {v:+.2f}" for d, v in r[4].items()))

    print("\n== 결론 ==")
    print(f"  조합 {len(cand)}개 시험 → 1차 통과(IS/OOS 순수익 > 0, 각 {MIN_N}건 이상) {len(firsts)}개")
    strong = [r for r in cand if r[6]]
    if strong:
        print(f"  ★유력 {len(strong)}개 (OOS t군집 ≥ 2, 날짜 대부분 플러스):")
        for r in strong:
            print("   " + line(r))
    else:
        print("  유력 후보 없음.")
    if len(days) < MIN_DAYS_RELIABLE:
        print(f"\n  [주의] 녹화 {len(days)}일 - {MIN_DAYS_RELIABLE}일 미만이라 결과는 참고용이다.")
        print("         며칠의 장세가 전부를 좌우할 수 있고, 조합이 많아 우연히 통과하는 것도 생긴다.")


if __name__ == '__main__':
    main()
