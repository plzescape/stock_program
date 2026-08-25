#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deep_analysis.py — 전체 거래일 통합 손실 원인 분석기
Usage: python deep_analysis.py [output_html]
"""

import re, os, sys, json
from datetime import datetime
from collections import defaultdict

# ─── 파싱 패턴 (analyze_trades.py 와 동일) ──────────────────────────────────
TS = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})'
P = {
    'ENTRY_QTY':  re.compile(TS + r'.*\[ENTRY_QTY\] (.+?)\((\w+)\) 현재가=(\d+) 전략=(\w+)'),
    'ATR_CALC':   re.compile(TS + r'.*\[ATR_CALC\] .+?\((\w+)\) ATR=([0-9.]+)원'),
    'BUY_FILL1':  re.compile(TS + r'.*\[BUY_FILL_NEW\] .+?\((\w+)\) entry=(\d+) total_qty=(\d+)'),
    'TP_TARGET':  re.compile(TS + r'.*\[TP_TARGET_SET\] .+?\((\w+)\) ATR=[0-9.]+원 손절=(\d+) TP1=(\d+) TP2\(초기\)=\d+ 본절보호=(\d+)'),
    'CANDLE':     re.compile(TS + r'.*\[CANDLE_CLOSED\] .+?\((\w+)\) O:(\d+) H:(\d+) L:(\d+) C:(\d+) pnl=([0-9.eE+\-]+)'),
    'ATR_MULTS':  re.compile(r'SL배수=([0-9.]+), TP배수=([0-9.]+)'),
    'TP_FALLBACK':re.compile(TS + r'.*\[TP_TARGET_SET_FALLBACK\] .+?\((\w+)\).*손절=(\d+) TP1=(\d+) TP2=(\d+)'),
    'SL':         re.compile(TS + r'.*\[STOP_LOSS_CANDLE\] .+?\((\w+)\)'),
    'SL_EMERG':   re.compile(TS + r'.*\[STOP_LOSS_EMERGENCY\] .+?\((\w+)\)'),
    'TP1_TRG':    re.compile(TS + r'.*\[TP1_TRIGGER\] .+?\((\w+)\)'),
    'TP1_FILL':   re.compile(TS + r'.*\[TP1_FILLED\] .+?\((\w+)\) 잔여=(\d+)주 고점=(\d+) TP2갱신=(\d+)'),
    'TP2_TRG':    re.compile(TS + r'.*\[TP2_TRIGGER\] .+?\((\w+)\)'),
    'TP2_FILL':   re.compile(TS + r'.*\[TP2_FILLED\] .+?\((\w+)\)'),
    'SAFE':       re.compile(TS + r'.*\[PROFIT_SAFEGUARD\] .+?\((\w+)\)'),
    'TRAIL':      re.compile(TS + r'.*\[TRAIL_STOP\] .+?\((\w+)\)'),
    'TIME_STOP':  re.compile(TS + r'.*\[TIME_STOP\] .+?\((\w+)\)'),
    'MINI_TRAIL': re.compile(TS + r'.*\[MINI_TRAIL_STOP\] .+?\((\w+)\)'),
    'FORCE_LIQ':  re.compile(TS + r'.*\[FORCE_LIQUIDATION\] .+?\((\w+)\)'),
    'CHEJAN_BUY': re.compile(TS + r'.*\[CHEJAN\] \+매수 .+?\((\w+)\) price=(\d+) qty=(\d+)'),
    'CHEJAN_SEL': re.compile(TS + r'.*\[CHEJAN\] -매도 .+?\((\w+)\) price=(\d+) qty=(\d+)'),
    'SELL_DONE':  re.compile(TS + r'.*\[SELL_DONE\] .+?\((\w+)\)'),
    'ORDER_SELL': re.compile(TS + r'.*\[ORDER_TRY\] 방향=SELL .+?\((\w+)\) 수량=(\d+)주 사유=(\w+)'),
    'CANCEL':     re.compile(TS + r'.*\[CANCEL_SEND\] .+?\((\w+)\)'),
    'PNL_CHECK':  re.compile(TS + r'.*\[PNL_CHECK\] .+?\((\w+)\) .+?pnl=([0-9.eE+\-]+)'),
    'NEVER_ROSE': re.compile(TS + r'.*\[NEVER_ROSE\] .+?\((\w+)\)'),
    'ENTRY_SKIP': re.compile(TS + r'.*\[ENTRY_SKIP_(\w+)\] .+?\((\w+)\)'),
}

STRAT_KO = {'BREAKOUT':'돌파','PULLBACK':'눌림','FLAG':'깃발'}
EXIT_KO = {
    'STOP_LOSS':'ATR손절','SL_EMERG':'긴급손절','TIME_STOP':'시간손절',
    'PROFIT_SAFE':'본절보호','TRAIL_STOP':'트레일링','MINI_TRAIL':'미니트레일',
    'TP2_FILL':'TP2달성','TP1_FILL':'TP1후청산','FORCE_LIQ':'강제청산',
    'UNKNOWN':'미분류','INCOMPLETE':'미완료',
}

def avg_fill(fills):
    if not fills: return 0, 0
    total_amt = total_qty = 0
    prev = 0
    for _, price, qty in fills:
        if qty < prev: prev = 0
        delta = qty - prev
        if delta > 0:
            total_amt += price * delta
            total_qty += delta
        prev = qty
    return (total_amt / total_qty if total_qty else 0), total_qty

def parse_date(folder_name):
    m = re.search(r'Report_(\d{2})(\d{2})(\d{2})', folder_name)
    if m:
        yy, mm, dd = m.groups()
        return f'20{yy}-{mm}-{dd}'
    return folder_name

def _apply_atr_fallback(t):
    """TP_TARGET_SET 로그가 없을 때 ATR로 SL/TP 추정"""
    if t['sl'] == 0 and t['atr'] > 0 and t['entry_price'] > 0:
        ep = t['entry_price']
        t['sl']   = int(ep - t['atr'] * t.get('atr_sl_mult', 2.0))
        t['tp1']  = int(ep + t['atr'] * t.get('atr_tp_mult', 1.2))
        t['tp2']  = t['tp1']
        t['safe'] = int(ep - t['atr'] * 1.0)


def parse_log(path, date_label):
    active = {}
    done   = []

    def new_trade(ts, name, code, strat):
        return dict(
            date=date_label, code=code, name=name, strategy=strat,
            entry_time=ts, entry_price=0, qty=0,
            atr=0, atr_sl_mult=2.0, atr_tp_mult=1.2,
            sl=0, tp1=0, tp2=0, safe=0,
            candles=[], events=[], sell_reasons=[],
            exit_reason=None, exit_time=None,
            buy_fills=[], sell_fills=[],
            sl_retry_count=0, cancel_count=0,
            tp1_done=False, tp2_done=False,
            peak_pnl=0.0,
        )

    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip()

            m = P['ENTRY_QTY'].search(line)
            if m:
                ts, name, code, _, strat = m.groups()
                if code not in active:
                    active[code] = new_trade(ts, name, code, strat)
                continue

            m = P['ATR_CALC'].search(line)
            if m:
                ts, code, atr = m.groups()
                if code in active:
                    active[code]['atr'] = float(atr)
                    mm = P['ATR_MULTS'].search(line)
                    if mm:
                        active[code]['atr_sl_mult'] = float(mm.group(1))
                        active[code]['atr_tp_mult'] = float(mm.group(2))
                continue

            m = P['BUY_FILL1'].search(line)
            if m:
                ts, code, entry, qty = m.groups()
                if code in active:
                    active[code]['entry_price'] = int(entry)
                    active[code]['qty'] = int(qty)
                continue

            m = P['CHEJAN_BUY'].search(line)
            if m:
                ts, code, price, qty = m.groups()
                if code in active: active[code]['buy_fills'].append((ts, int(price), int(qty)))
                continue

            m = P['TP_TARGET'].search(line)
            if m:
                ts, code, sl, tp1, safe = m.groups()
                if code in active:
                    t = active[code]
                    t['sl'], t['tp1'], t['tp2'], t['safe'] = int(sl), int(tp1), int(tp1), int(safe)
                continue

            m = P['TP_FALLBACK'].search(line)
            if m:
                ts, code, sl, tp1, tp2 = m.groups()
                if code in active:
                    t = active[code]
                    t['sl'], t['tp1'], t['tp2'] = int(sl), int(tp1), int(tp2)
                continue

            m = P['CANDLE'].search(line)
            if m:
                ts, code, o, h, l, c, pnl = m.groups()
                if code in active:
                    pf = float(pnl)
                    active[code]['candles'].append(
                        {'t': ts, 'o': int(o), 'h': int(h), 'l': int(l), 'c': int(c), 'pnl': pf})
                    if pf > active[code]['peak_pnl']:
                        active[code]['peak_pnl'] = pf
                continue

            m = P['PNL_CHECK'].search(line)
            if m:
                ts, code, pnl = m.groups()
                if code in active:
                    pf = float(pnl)
                    if pf > active[code]['peak_pnl']:
                        active[code]['peak_pnl'] = pf
                continue

            # 손절 retry / cancel 카운트
            m = P['ORDER_SELL'].search(line)
            if m:
                ts, code, qty, reason = m.groups()
                if code in active:
                    active[code]['sell_reasons'].append(reason)
                    if 'RETRY' in reason:
                        active[code]['sl_retry_count'] += 1
                continue

            m = P['CANCEL'].search(line)
            if m:
                ts, code = m.groups()
                if code in active: active[code]['cancel_count'] += 1
                continue

            for key, reason in [('SL','STOP_LOSS'),('SL_EMERG','SL_EMERG'),
                                  ('TIME_STOP','TIME_STOP'),('SAFE','PROFIT_SAFE'),
                                  ('TRAIL','TRAIL_STOP'),('MINI_TRAIL','MINI_TRAIL'),
                                  ('FORCE_LIQ','FORCE_LIQ')]:
                m = P[key].search(line)
                if m:
                    ts, code = m.groups()
                    if code in active:
                        t = active[code]
                        if not t['exit_reason']:
                            t['exit_reason'] = reason
                        t['events'].append({'k': key, 't': ts})
                    break

            m = P['TP1_FILL'].search(line)
            if m:
                ts, code, remain, high, tp2 = m.groups()
                if code in active:
                    active[code]['tp1_done'] = True
                    active[code]['tp2'] = int(tp2)
                    active[code]['events'].append({'k': 'TP1_FILL', 't': ts})
                continue

            m = P['TP2_FILL'].search(line)
            if m:
                ts, code = m.groups()
                if code in active:
                    active[code]['tp2_done'] = True
                    active[code]['exit_reason'] = active[code]['exit_reason'] or 'TP2_FILL'
                continue

            m = P['CHEJAN_SEL'].search(line)
            if m:
                ts, code, price, qty = m.groups()
                if code in active: active[code]['sell_fills'].append((ts, int(price), int(qty)))
                continue

            m = P['SELL_DONE'].search(line)
            if m:
                ts, code = m.groups()
                if code in active:
                    t = active.pop(code)
                    t['exit_time'] = ts
                    if not t['exit_reason']:
                        t['exit_reason'] = 'UNKNOWN'
                    ap, _ = avg_fill(t['sell_fills'])
                    abp, _ = avg_fill(t['buy_fills'])
                    t['avg_sell'] = ap
                    t['avg_buy']  = abp if abp else t['entry_price']
                    ep = t['avg_buy'] or t['entry_price']
                    t['pnl_pct'] = (ap - ep) / ep * 100 if ep and ap else 0
                    t['pnl_raw'] = int((ap - ep) * t['qty']) if ep and ap else 0
                    # 진입 후 경과 분
                    try:
                        fmt = '%Y-%m-%d %H:%M:%S,%f'
                        et = datetime.strptime(t['entry_time'], fmt)
                        xt = datetime.strptime(t['exit_time'], fmt)
                        t['hold_min'] = (xt - et).total_seconds() / 60
                    except:
                        t['hold_min'] = 0
                    _apply_atr_fallback(t)
                    done.append(t)

    # 미완료 거래
    for code, t in active.items():
        t['exit_time'] = t['candles'][-1]['t'] if t['candles'] else t['entry_time']
        t['exit_reason'] = t['exit_reason'] or 'INCOMPLETE'
        t['avg_sell'] = 0
        t['avg_buy']  = t['entry_price']
        t['pnl_pct']  = t['candles'][-1]['pnl'] * 100 if t['candles'] else 0
        t['pnl_raw']  = 0
        t['hold_min'] = 0
        _apply_atr_fallback(t)
        done.append(t)

    return done


# ─── 통합 분석 ───────────────────────────────────────────────────────────────
def analyze_all(all_trades):
    """전체 거래 통합 분석 → 문제점 dict 반환"""

    completed = [t for t in all_trades if t['exit_reason'] != 'INCOMPLETE']
    losses    = [t for t in completed if t['pnl_pct'] < 0]
    wins      = [t for t in completed if t['pnl_pct'] >= 0]

    # ── 1. 손절 지연 분석 ──────────────────────────────────────────────────
    sl_delayed = [t for t in losses if t['sl_retry_count'] >= 2]
    sl_delay_data = []
    for t in sl_delayed:
        n = t['sl_retry_count']
        # SL 첫 발동 ~ SELL_DONE 시간
        sl_ev = next((e for e in t['events'] if e['k'] == 'SL'), None)
        if sl_ev:
            try:
                fmt = '%Y-%m-%d %H:%M:%S,%f'
                t1 = datetime.strptime(sl_ev['t'], fmt)
                t2 = datetime.strptime(t['exit_time'], fmt)
                delay_sec = (t2 - t1).total_seconds()
            except:
                delay_sec = 0
        else:
            delay_sec = 0
        # SL 가격 vs 실제 체결가
        sl_slippage = (t['avg_sell'] - t['sl']) / t['sl'] * 100 if t['sl'] and t['avg_sell'] else 0
        sl_delay_data.append({
            't': t, 'retries': n, 'delay_sec': delay_sec,
            'sl_slippage_pct': sl_slippage,
        })

    # ── 2. 고점 매수 분석 ──────────────────────────────────────────────────
    pump_and_dump = []
    for t in completed:
        if not t['candles']: continue
        first_candle_pnl = t['candles'][0]['pnl'] * 100
        # 진입 후 첫 봉이 -0.5% 이하이면 고점 추격 의심
        if first_candle_pnl < -0.5:
            pump_and_dump.append({'t': t, 'first_pnl': first_candle_pnl})
        # 또는: peak가 매우 낮고 (1% 미만) 결과가 손실
        elif t['peak_pnl'] * 100 < 0.3 and t['pnl_pct'] < 0:
            pump_and_dump.append({'t': t, 'first_pnl': first_candle_pnl})

    # ── 3. TP1 근접 미달 (TP 목표가 너무 높음) ────────────────────────────
    tp_miss = []
    for t in losses:
        if not t['tp1'] or not t['entry_price']: continue
        ep = t['entry_price']
        tp1_gap = (t['tp1'] - ep) / ep * 100
        peak = t['peak_pnl'] * 100
        if peak > 0.5 and peak >= tp1_gap * 0.7:
            tp_miss.append({'t': t, 'tp1_gap': tp1_gap, 'peak_pnl': peak,
                            'miss_by': tp1_gap - peak})

    # ── 4. 전략별 승률 ────────────────────────────────────────────────────
    by_strat = defaultdict(lambda: {'wins': 0, 'losses': 0, 'total_pnl': 0})
    for t in completed:
        s = t['strategy']
        if t['pnl_pct'] >= 0:
            by_strat[s]['wins'] += 1
        else:
            by_strat[s]['losses'] += 1
        by_strat[s]['total_pnl'] += t['pnl_raw']

    # ── 5. 청산 사유별 분석 ───────────────────────────────────────────────
    by_exit = defaultdict(lambda: {'count': 0, 'total_pnl': 0, 'trades': []})
    for t in completed:
        er = t['exit_reason'] or 'UNKNOWN'
        by_exit[er]['count'] += 1
        by_exit[er]['total_pnl'] += t['pnl_raw']
        by_exit[er]['trades'].append(t)

    # ── 6. 본절보호 후 손실 ───────────────────────────────────────────────
    safe_loss = [t for t in completed
                 if t['exit_reason'] == 'PROFIT_SAFE' and t['pnl_pct'] < 0]

    # ── 7. NEVER_ROSE — 진입 후 목표가의 절반도 못 가고 손절 ─────────────
    never_rose = [t for t in losses if t['peak_pnl'] * 100 < 0.2]

    # ── 8. 보유 시간 분석 ─────────────────────────────────────────────────
    avg_win_hold  = sum(t['hold_min'] for t in wins) / len(wins) if wins else 0
    avg_loss_hold = sum(t['hold_min'] for t in losses) / len(losses) if losses else 0

    # ── 9. 진입 시각 분포 (손실 집중 구간) ───────────────────────────────
    loss_by_hour = defaultdict(int)
    win_by_hour  = defaultdict(int)
    for t in completed:
        try:
            h = int(t['entry_time'][11:13])
            if t['pnl_pct'] < 0: loss_by_hour[h] += 1
            else: win_by_hour[h] += 1
        except: pass

    # ── 10. 슬리피지 분석 ─────────────────────────────────────────────────
    slippage_data = []
    for t in completed:
        if not t['entry_price'] or not t['buy_fills']: continue
        avg_bp, _ = avg_fill(t['buy_fills'])
        slip = (avg_bp - t['entry_price']) / t['entry_price'] * 100 if t['entry_price'] else 0
        slippage_data.append({'t': t, 'slip': slip})

    return {
        'completed': completed, 'losses': losses, 'wins': wins,
        'sl_delayed': sl_delay_data,
        'pump_and_dump': pump_and_dump,
        'tp_miss': tp_miss,
        'by_strat': dict(by_strat),
        'by_exit': dict(by_exit),
        'safe_loss': safe_loss,
        'never_rose': never_rose,
        'avg_win_hold': avg_win_hold,
        'avg_loss_hold': avg_loss_hold,
        'loss_by_hour': dict(loss_by_hour),
        'win_by_hour': dict(win_by_hour),
        'slippage_data': slippage_data,
    }


# ─── HTML 생성 ───────────────────────────────────────────────────────────────
def pct_bar(val, max_val, color, width=180):
    w = int(abs(val) / max(abs(max_val), 0.01) * width)
    return f'<div style="display:inline-block;width:{w}px;height:10px;background:{color};vertical-align:middle;border-radius:2px"></div>'

def fmt_won(v):
    return f'{int(v):+,}원'

def issue_block(title, level, body):
    colors = {'danger': '#e74c3c', 'warn': '#e67e22', 'info': '#3498db', 'ok': '#27ae60'}
    icons  = {'danger': '🔴', 'warn': '🟠', 'info': '🔵', 'ok': '🟢'}
    c = colors.get(level, '#888')
    icon = icons.get(level, '•')
    return f"""
<div style="border-left:4px solid {c};background:var(--card);border-radius:0 6px 6px 0;
            padding:14px 16px;margin:10px 0">
  <div style="font-weight:700;font-size:.95rem;margin-bottom:8px">{icon} {title}</div>
  <div style="font-size:.85rem;line-height:1.7;color:var(--text)">{body}</div>
</div>"""

def make_html(R, date_labels):
    completed = R['completed']
    losses    = R['losses']
    wins      = R['wins']
    n = len(completed)
    total_pnl = sum(t['pnl_raw'] for t in completed)
    wr = len(wins) / n * 100 if n else 0

    # ── 전략별 표 ──
    strat_rows = ''
    for s, d in sorted(R['by_strat'].items()):
        tot = d['wins'] + d['losses']
        wr_s = d['wins'] / tot * 100 if tot else 0
        pnl_cls = 'pos' if d['total_pnl'] >= 0 else 'neg'
        strat_rows += (f'<tr><td>{STRAT_KO.get(s,s)}</td><td>{tot}</td>'
                       f'<td style="color:#27ae60">{d["wins"]}</td>'
                       f'<td style="color:#e74c3c">{d["losses"]}</td>'
                       f'<td>{wr_s:.0f}%</td>'
                       f'<td class="{pnl_cls}">{fmt_won(d["total_pnl"])}</td></tr>\n')

    # ── 청산 사유별 표 ──
    exit_rows = ''
    for er, d in sorted(R['by_exit'].items(), key=lambda x: x[1]['total_pnl']):
        pnl_cls = 'pos' if d['total_pnl'] >= 0 else 'neg'
        avg = d['total_pnl'] / d['count'] if d['count'] else 0
        exit_rows += (f'<tr><td>{EXIT_KO.get(er,er)}</td><td>{d["count"]}</td>'
                      f'<td class="{pnl_cls}">{fmt_won(d["total_pnl"])}</td>'
                      f'<td class="{pnl_cls}">{fmt_won(avg)}</td></tr>\n')

    # ── 손절 지연 상세 ──
    sl_rows = ''
    for d in sorted(R['sl_delayed'], key=lambda x: -x['delay_sec'])[:10]:
        t = d['t']
        slip_cls = 'neg' if d['sl_slippage_pct'] < 0 else ''
        sl_rows += (f'<tr><td>{t["date"]}</td><td>{t["name"]}</td>'
                    f'<td>{d["retries"]}회</td>'
                    f'<td>{d["delay_sec"]:.0f}초</td>'
                    f'<td class="{slip_cls}">{d["sl_slippage_pct"]:+.2f}%</td>'
                    f'<td class="neg">{t["pnl_pct"]:+.2f}%</td></tr>\n')

    # ── TP 미달 상세 ──
    tp_rows = ''
    for d in sorted(R['tp_miss'], key=lambda x: -x['peak_pnl'])[:10]:
        t = d['t']
        tp_rows += (f'<tr><td>{t["date"]}</td><td>{t["name"]}</td>'
                    f'<td>{d["tp1_gap"]:.2f}%</td>'
                    f'<td>{d["peak_pnl"]:.2f}%</td>'
                    f'<td class="neg">{d["miss_by"]:.2f}%p</td>'
                    f'<td class="neg">{t["pnl_pct"]:+.2f}%</td></tr>\n')

    # ── 전체 거래 목록 ──
    all_rows = ''
    for t in sorted(completed, key=lambda x: x['entry_time']):
        pc = t['pnl_pct']
        cls = 'pos' if pc >= 0 else 'neg'
        er = EXIT_KO.get(t['exit_reason'], t['exit_reason'])
        retry_warn = f' <span style="color:#e67e22;font-size:.75rem">[재시도×{t["sl_retry_count"]}]</span>' if t['sl_retry_count'] >= 2 else ''
        all_rows += (f'<tr><td>{t["date"]}</td><td>{t["name"]}</td>'
                     f'<td>{STRAT_KO.get(t["strategy"],t["strategy"])}</td>'
                     f'<td>{t["entry_time"][11:16]}</td>'
                     f'<td>{int(t["hold_min"])}분</td>'
                     f'<td class="{cls}">{pc:+.2f}%</td>'
                     f'<td class="{cls}">{fmt_won(t["pnl_raw"])}</td>'
                     f'<td>{er}{retry_warn}</td></tr>\n')

    # ── 시간대별 차트 데이터 ──
    hours = list(range(9, 16))
    loss_h = [R['loss_by_hour'].get(h, 0) for h in hours]
    win_h  = [R['win_by_hour'].get(h, 0) for h in hours]
    max_h  = max(max(loss_h), max(win_h), 1)
    hour_bars = ''
    for i, h in enumerate(hours):
        lv = loss_h[i]
        wv = win_h[i]
        lb = int(lv / max_h * 80)
        wb = int(wv / max_h * 80)
        hour_bars += (f'<div style="display:flex;align-items:center;gap:6px;margin:4px 0;font-size:.8rem">'
                      f'<span style="width:30px;text-align:right">{h}시</span>'
                      f'<div style="width:{wb}px;height:14px;background:#27ae60;border-radius:2px"></div>'
                      f'<div style="width:{lb}px;height:14px;background:#e74c3c;border-radius:2px"></div>'
                      f'<span style="color:var(--muted)">'
                      f'수익{wv} 손실{lv}</span>'
                      f'</div>\n')

    # ── 슬리피지 평균 ──
    pos_slips = [d['slip'] for d in R['slippage_data'] if d['slip'] > 0.05]
    avg_slip = sum(pos_slips) / len(pos_slips) if pos_slips else 0

    # ── 이슈 섹션 ──
    issues_html = ''

    # 이슈 1: 손절 재시도 지연
    if R['sl_delayed']:
        cnt = len(R['sl_delayed'])
        avg_delay = sum(d['delay_sec'] for d in R['sl_delayed']) / cnt
        avg_extra_loss = sum(abs(d['sl_slippage_pct']) for d in R['sl_delayed']) / cnt
        issues_html += issue_block(
            f'[코드 버그] 손절 지정가 재시도 지연 — {cnt}건, 평균 {avg_delay:.0f}초 지연',
            'danger',
            f"""<b>현상:</b> STOP_LOSS_CANDLE 발동 후 지정가 주문이 체결되지 않아 평균 {avg_delay:.0f}초 동안 {cnt}건에서 재시도 반복.<br>
<b>손실 가중:</b> 손절가 대비 평균 <b>{avg_extra_loss:.2f}%</b> 추가 슬리피지 발생.<br>
<b>원인 코드:</b> <code>kiwoom_api.py</code> 손절 주문 로직 — 지정가(sl_limit_price) 주문이 호가창 아래에 걸려 미체결 → 15초 후 취소·재시도.<br>
<code>SELL_PENDING_TIMEOUT_SEC = 15</code>초 × 최대 <code>MAX_CANCEL_RETRIES × MAX_REENTRY_RETRIES</code> = 이론상 최대 90~120초 지연.<br>
<b>수정 방향:</b> 완성봉 종가가 이미 SL 이하일 때는 <b>즉시 시장가</b>로 발주. 지정가는 종가 ≥ SL 이고 직전 유동성이 충분할 때만 사용.
<br><br><b>관련 거래:</b> {'  '.join(f"{d['t']['name']}({d['delay_sec']:.0f}초)" for d in R['sl_delayed'][:5])}"""
        )

    # 이슈 2: 고점 매수 (펌프앤덤프)
    if R['pump_and_dump']:
        cnt = len(R['pump_and_dump'])
        avg_first = sum(d['first_pnl'] for d in R['pump_and_dump']) / cnt
        issues_html += issue_block(
            f'[전략 필터] 고점 추격 매수 — {cnt}건, 진입 첫 봉 평균 {avg_first:.2f}%',
            'danger',
            f"""<b>현상:</b> 진입 직후 첫 완성봉이 이미 마이너스 — 급등 정점에서 매수된 것.<br>
<b>원인:</b> BREAKOUT 전략의 <code>signal.log</code> 체크 타이밍 vs 실제 주문 체결 사이 가격 상승.<br>
스캔→분봉 요청→파싱→주문까지 평균 1~3초 지연 동안 가격이 올라간 뒤 체결.<br>
<b>관련 코드:</b><br>
- <code>strategy.py: is_breakout_entry()</code> — 완성봉 기준으로만 판단, 라이브 틱 가격 미반영<br>
- <code>kiwoom_api.py</code> 체결 루프 — 스캔 트리거 후 요청 딜레이 <code>SCAN_TR_DELAY_MS = 700ms</code><br>
<b>수정 방향:</b> 진입 직전 현재가 vs 급등봉 종가 이격 체크 추가 (예: 현재가 > 급등봉 종가 × 1.01이면 스킵).
<br><br><b>관련 거래:</b> {'  '.join(f"{d['t']['name']}({d['first_pnl']:+.1f}%)" for d in R['pump_and_dump'][:6])}"""
        )

    # 이슈 3: TP 목표 미달
    if R['tp_miss']:
        cnt = len(R['tp_miss'])
        avg_miss = sum(d['miss_by'] for d in R['tp_miss']) / cnt
        avg_tp_gap = sum(d['tp1_gap'] for d in R['tp_miss']) / cnt
        issues_html += issue_block(
            f'[설정] ATR×TP 목표 과다 — {cnt}건이 TP1에 {avg_miss:.2f}%p 미달 후 손절',
            'warn',
            f"""<b>현상:</b> TP1에 {avg_miss:.2f}%p 부족하게 올라가다가 돌아서 손절 당함.<br>
평균 TP1 목표: 진입가 대비 <b>+{avg_tp_gap:.2f}%</b>.<br>
<b>원인:</b> <code>config.py: ATR_TP_MULT = 1.5</code> — 1분봉 기준 설계였으나 3분봉으로 전환 후
단일 봉의 변동 폭이 더 크기 때문에 ATR도 크게 계산됨 → TP 목표가 더 멀어짐.<br>
<b>수정 방향:</b> ATR_TP_MULT를 1.0~1.2로 줄이거나, TP1 체결 후 후속 TP2를 상향 재설정하는
현재 구조 자체를 유지하되 TP1 비율(TP1_RATIO)을 60~70%로 늘려 손실 방어.
<br><br><b>관련 거래:</b> {'  '.join(f"{d['t']['name']}(목표{d['tp1_gap']:.1f}% 최대{d['peak_pnl']:.1f}%)" for d in R['tp_miss'][:5])}"""
        )

    # 이슈 4: NEVER_ROSE
    if R['never_rose']:
        cnt = len(R['never_rose'])
        issues_html += issue_block(
            f'[전략 필터] 진입 후 전혀 안 오름 (NEVER_ROSE) — {cnt}건',
            'warn',
            f"""<b>현상:</b> 진입 이후 peak 수익이 +0.2% 미만 — 사실상 올라간 적이 없음.<br>
매수 직후부터 하락 일변도이거나, 조건식 편입 후 급등 소진 종목 진입.<br>
<b>원인 코드:</b> <code>config.py: BREAKOUT_FRESH_CANDLES_MAX = 5봉(15분)</code>으로 줄였지만
여전히 급등 이후 에너지 소진 종목이 15분 내에 재편입되는 케이스 존재.<br>
<code>BREAKOUT_FIRST_COND_TIMEOUT = 900초</code>(15분)도 같은 취지이나 두 조건이 서로 독립적으로
작동해 하나가 통과시키면 다른 하나는 의미 없음.<br>
<b>수정 방향:</b> 급등봉 이후 가격이 <b>급등봉 종가의 98% 이상</b>을 유지하고 있을 때만 진입
(현재 가격 / 급등봉 종가 ≥ 0.98). 내려온 종목 재진입 차단.
<br><br><b>관련 거래:</b> {'  '.join(f"{t['name']}({t['pnl_pct']:+.1f}%)" for t in R['never_rose'][:8])}"""
        )

    # 이슈 5: 본절보호 손실
    if R['safe_loss']:
        cnt = len(R['safe_loss'])
        issues_html += issue_block(
            f'[설정] TP1 후 본절보호 매도 시 오히려 손실 — {cnt}건',
            'warn',
            f"""<b>현상:</b> TP1 체결 성공 후 잔여 포지션이 본절 이하로 밀려 PROFIT_SAFEGUARD 발동.
최종 손익이 마이너스인 케이스 {cnt}건.<br>
<b>원인:</b> TP1 체결 후 시장가 매도 슬리피지 + 잔여 포지션 본절보호 매도 슬리피지가 합산되면
수수료(왕복 ~0.35%)를 감당 못 하는 케이스 발생.<br>
<code>ATR_SAFE_MULT = 1.0</code>(본절보호선 = 매수가 - ATR×1.0) — ATR이 크면 본절 내에서도
충분히 손실이 날 수 있음.<br>
<b>수정 방향:</b> 본절보호 발동 시 남은 수량에 대한 슬리피지를 감안해 손절가를
<code>entry + commission_rate(0.35%)</code> 이상으로 설정. 즉 실질 본절 = 매수가 + 수수료."""
        )

    # 이슈 6: 전략 BREAKOUT 독점 & FLAG/PULLBACK 미진입
    breakout_n = R['by_strat'].get('BREAKOUT', {}).get('wins', 0) + R['by_strat'].get('BREAKOUT', {}).get('losses', 0)
    flag_n     = R['by_strat'].get('FLAG', {}).get('wins', 0)     + R['by_strat'].get('FLAG', {}).get('losses', 0)
    pull_n     = R['by_strat'].get('PULLBACK', {}).get('wins', 0) + R['by_strat'].get('PULLBACK', {}).get('losses', 0)
    if breakout_n > 0 and (flag_n + pull_n) < breakout_n * 0.3:
        issues_html += issue_block(
            f'[전략 균형] BREAKOUT 편중 — 전체 {n}건 중 BREAKOUT {breakout_n}건 ({breakout_n/n*100:.0f}%)',
            'info',
            f"""<b>현상:</b> FLAG {flag_n}건, PULLBACK {pull_n}건으로 BREAKOUT이 압도적으로 많음.<br>
BREAKOUT은 급등 직후 진입 → 슬리피지·타이밍 실패 리스크가 높음.<br>
<b>원인:</b> 조건검색식이 <code>CHUSAE_INDICATE</code> 위주 — BREAKOUT 종목만 유입.<br>
FLAG/PULLBACK 조건식(<code>급등주_눌림목_검색식</code>, <code>상승_깃발_패턴</code>)이 실제로
충분한 종목을 잡아오지 못하거나, 필터가 너무 엄격해 진입 자체가 안 됨.<br>
<b>수정 방향:</b> FLAG/PULLBACK 조건식 기준 완화 또는 검색 주기(<code>CONDITION_INTERVAL_MIN</code>)
단축. 로그에서 FLAG_CHECK, PULLBACK_CHECK 통과율 통계를 뽑아 어느 조건이 병목인지 확인."""
        )

    # 이슈 7: 시간대 집중 손실
    peak_loss_h = max(R['loss_by_hour'].items(), key=lambda x: x[1]) if R['loss_by_hour'] else (0, 0)
    peak_win_h  = max(R['win_by_hour'].items(),  key=lambda x: x[1]) if R['win_by_hour']  else (0, 0)
    if peak_loss_h[1] >= 2:
        issues_html += issue_block(
            f'[패턴] {peak_loss_h[0]}시 손실 집중 — {peak_loss_h[1]}건',
            'info',
            f"""<b>현상:</b> {peak_loss_h[0]}시대 손실이 {peak_loss_h[1]}건으로 가장 많음.
수익 집중 시간대는 {peak_win_h[0]}시 ({peak_win_h[1]}건).<br>
<b>가설:</b> 장 초반(09:00~09:30) — 변동성 과다, 급등봉 신뢰도 낮음.
점심(11:30~13:00) — 거래량 감소, 추세 지속성 낮음.<br>
<b>수정 방향:</b> 09:00~09:10 구간은 이미 <code>MINI_TRAIL_GAP_OPEN</code>으로 완화 적용 중.
추가로 09:00~09:20 BREAKOUT 진입 자체를 차단하거나 ATR_SL_MULT를 키우는 방안 검토."""
        )

    # 이슈 8: 슬리피지
    if avg_slip > 0.05:
        issues_html += issue_block(
            f'[실행] 매수 슬리피지 평균 +{avg_slip:.2f}% — 진입가보다 높게 체결',
            'info',
            f"""<b>현상:</b> 요청 현재가 대비 실제 체결가가 평균 {avg_slip:.2f}% 높음.<br>
시장가 주문 특성상 호가 점프 시 의도보다 비싸게 체결됨.<br>
<b>원인:</b> <code>ORDER_TRY → BUY_FILL_NEW</code> 사이 약 0.1~2초 동안 가격 상승.<br>
급등 중인 종목일수록 슬리피지 심화 → 진입 후 즉시 기대 수익이 줄어듦.<br>
<b>수정 방향:</b> ATR 계산 기반 TP 설정 시 슬리피지 버퍼를 포함하거나,
TP1_RATIO를 올려 첫 익절 시 충분한 수익 확보 후 나머지를 트레일링으로 운용."""
        )

    pnl_cls = 'pos' if total_pnl >= 0 else 'neg'

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>통합 손실 원인 분석 — {', '.join(date_labels)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#f0f2f5;--card:#fff;--text:#2c3e50;--muted:#636e72;--border:#dde2e8;--head:#1a2332}}
@media(prefers-color-scheme:dark){{:root{{--bg:#12151c;--card:#1e2330;--text:#c8d0de;--muted:#7a8599;--border:#252c3a;--head:#0d1117}}}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);padding:20px;max-width:1060px;margin:0 auto}}
h1{{font-size:1.35rem;font-weight:700;margin-bottom:2px}}
h2{{font-size:1.05rem;font-weight:700;margin:22px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--border)}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:14px}}
.grid4{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.stat{{text-align:center;padding:10px 6px}}
.stat .val{{font-size:1.7rem;font-weight:700}}
.stat .lbl{{font-size:.73rem;color:var(--muted);margin-top:3px}}
.pos{{color:#27ae60}}.neg{{color:#e74c3c}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}}
th{{background:var(--border);padding:6px 9px;text-align:left;font-size:.78rem}}
td{{padding:5px 9px;border-bottom:1px solid var(--border)}}
tr:last-child td{{border:none}}
code{{background:var(--border);padding:1px 5px;border-radius:3px;font-size:.82rem;font-family:monospace}}
.tag{{display:inline-block;padding:1px 7px;border-radius:10px;font-size:.72rem;font-weight:700;margin-left:4px}}
</style>
<h1>🔬 통합 손실 원인 분석</h1>
<p style="color:var(--muted);font-size:.8rem;margin:3px 0 14px">분석 기간: {' / '.join(date_labels)} &nbsp;|&nbsp; 총 {n}건 완료 거래</p>

<div class="card grid4">
  <div class="stat">
    <div class="val {pnl_cls}">{total_pnl:+,}</div>
    <div class="lbl">총 손익 (원)</div>
  </div>
  <div class="stat">
    <div class="val">{n}건</div>
    <div class="lbl">완료 거래</div>
  </div>
  <div class="stat">
    <div class="val">{wr:.0f}%</div>
    <div class="lbl">승률</div>
  </div>
  <div class="stat">
    <div class="val neg">{len(losses)}건</div>
    <div class="lbl">손실 거래</div>
  </div>
</div>

<h2>⚠️ 발견된 구조적 문제점</h2>
{issues_html}

<div class="grid2">
  <div>
    <h2>전략별 성과</h2>
    <div class="card">
    <table><tr><th>전략</th><th>건수</th><th>수익</th><th>손실</th><th>승률</th><th>손익합</th></tr>
    {strat_rows}</table>
    </div>
  </div>
  <div>
    <h2>청산 사유별 성과</h2>
    <div class="card">
    <table><tr><th>사유</th><th>건수</th><th>손익합</th><th>건당 평균</th></tr>
    {exit_rows}</table>
    </div>
  </div>
</div>

<h2>⏰ 시간대별 거래 분포 (🟢수익 🔴손실)</h2>
<div class="card">{hour_bars}</div>

<h2>⏳ 손절 재시도 지연 거래 상세</h2>
<div class="card">
{"<p style='color:var(--muted);font-size:.85rem'>손절 재시도 2회 이상 거래 없음</p>" if not R['sl_delayed'] else
f'<table><tr><th>날짜</th><th>종목</th><th>재시도</th><th>지연시간</th><th>슬리피지</th><th>손익</th></tr>' + sl_rows + '</table>'}
</div>

<h2>🎯 TP1 근접 미달 거래 상세</h2>
<div class="card">
{"<p style='color:var(--muted);font-size:.85rem'>TP 근접 미달 거래 없음</p>" if not R['tp_miss'] else
f'<table><tr><th>날짜</th><th>종목</th><th>TP1목표</th><th>최대수익</th><th>미달</th><th>손익</th></tr>' + tp_rows + '</table>'}
</div>

<h2>📋 전체 거래 목록</h2>
<div class="card" style="overflow-x:auto">
<table><tr><th>날짜</th><th>종목</th><th>전략</th><th>진입</th><th>보유</th><th>손익%</th><th>손익원</th><th>청산 사유</th></tr>
{all_rows}</table>
</div>
"""


# ─── 메인 ────────────────────────────────────────────────────────────────────
def main():
    base = os.path.dirname(os.path.abspath(__file__))
    out  = sys.argv[1] if len(sys.argv) >= 2 else os.path.join(base, 'deep_analysis.html')

    # Report_* 폴더 자동 수집
    folders = sorted(
        d for d in os.listdir(base)
        if d.startswith('Report_') and os.path.isdir(os.path.join(base, d))
        and os.path.exists(os.path.join(base, d, 'trade.log'))
    )
    if not folders:
        print("[ERROR] Report_* 폴더에 trade.log 가 없습니다.")
        sys.exit(1)

    all_trades  = []
    date_labels = []
    for folder in folders:
        path = os.path.join(base, folder, 'trade.log')
        date_label = parse_date(folder)
        print(f"  읽는 중: {folder}/trade.log", flush=True)
        trades = parse_log(path, date_label)
        all_trades.extend(trades)
        done = [t for t in trades if t['exit_reason'] != 'INCOMPLETE']
        date_labels.append(date_label[5:])  # MM-DD
        pnl = sum(t['pnl_raw'] for t in done)
        print(f"           완료거래 {len(done)}건, 당일 손익 {pnl:+,}원", flush=True)

    print(f"\n[분석] 전체 {len(all_trades)}거래 분석 중...", flush=True)
    R = analyze_all(all_trades)

    html = make_html(R, date_labels)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"[완료] {out}")
    print(f"[완료] 총손익 {sum(t['pnl_raw'] for t in R['completed']):+,}원  "
          f"승률 {len(R['wins'])}/{len(R['completed'])}건 "
          f"({len(R['wins'])/max(len(R['completed']),1)*100:.0f}%)")


if __name__ == '__main__':
    main()
