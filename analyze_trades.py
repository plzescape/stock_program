#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_trades.py  ─  자동매매 로그 분석기
Usage:
    python analyze_trades.py <log_folder> [output_folder]

    log_folder:    trade.log 가 있는 폴더 (예: logs  또는  Report_260723)
    output_folder: HTML 결과 저장 폴더 (생략 시 log_folder 와 동일)
"""

import re, os, sys, json
from datetime import datetime

# ─── 로그 패턴 ───────────────────────────────────────────────────────────────
TS = r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})'
P = {
    'ENTRY_QTY':  re.compile(TS + r'.*\[ENTRY_QTY\] (.+?)\((\w+)\) 현재가=(\d+) 전략=(\w+)'),
    'ATR_CALC':   re.compile(TS + r'.*\[ATR_CALC\] .+?\((\w+)\) ATR=([0-9.]+)원'),
    'BUY_FILL1':  re.compile(TS + r'.*\[BUY_FILL_NEW\] .+?\((\w+)\) entry=(\d+) total_qty=(\d+)'),
    'TP_TARGET':  re.compile(TS + r'.*\[TP_TARGET_SET\] .+?\((\w+)\) ATR=[0-9.]+원 손절=(\d+) TP1=(\d+) TP2\(초기\)=\d+ 본절보호=(\d+)'),
    'TP_FALLBACK':re.compile(TS + r'.*\[TP_TARGET_SET_FALLBACK\] .+?\((\w+)\).*손절=(\d+) TP1=(\d+) TP2=(\d+)'),
    'ATR_MULTS':  re.compile(r'SL배수=([0-9.]+), TP배수=([0-9.]+)'),
    'CANDLE':     re.compile(TS + r'.*\[CANDLE_CLOSED\] .+?\((\w+)\) O:(\d+) H:(\d+) L:(\d+) C:(\d+) pnl=([0-9.eE+\-]+)'),
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
    'ORDER_SELL': re.compile(TS + r'.*\[ORDER_TRY\] 방향=SELL .+?\((\w+)\) 수량=(\d+)주 사유=(\w+)'),
    'CHEJAN_BUY': re.compile(TS + r'.*\[CHEJAN\] \+매수 .+?\((\w+)\) price=(\d+) qty=(\d+)'),
    'CHEJAN_SEL': re.compile(TS + r'.*\[CHEJAN\] -매도 .+?\((\w+)\) price=(\d+) qty=(\d+)'),
    'SELL_DONE':  re.compile(TS + r'.*\[SELL_DONE\] .+?\((\w+)\)'),
}

EXIT_LABELS = {
    'STOP_LOSS':  ('SL', '🔴 ATR 손절', '#e74c3c'),
    'SL_EMERG':   ('SL', '🔴 긴급 손절', '#c0392b'),
    'TIME_STOP':  ('TM', '🕐 시간 손절', '#e67e22'),
    'PROFIT_SAFE':('PS', '🟡 본절보호',  '#f39c12'),
    'TRAIL_STOP': ('TR', '🟢 트레일링',  '#27ae60'),
    'MINI_TRAIL': ('MT', '🟢 미니트레일','#2ecc71'),
    'TP2_FILL':   ('T2', '🟢 TP2 익절',  '#16a085'),
    'TP1_FILL':   ('T1', '🔵 TP1 익절',  '#2980b9'),
    'FORCE_LIQ':  ('FL', '⚪ 강제청산',  '#7f8c8d'),
    'INCOMPLETE': ('?',  '❓ 미완료',    '#95a5a6'),
}

STRATEGY_KO = {'BREAKOUT': '돌파', 'PULLBACK': '눌림', 'FLAG': '깃발'}


# ─── 파싱 ────────────────────────────────────────────────────────────────────
def avg_fill(fills):
    """CHEJAN qty(누적) 리스트 → (avg_price, total_qty)"""
    if not fills:
        return 0, 0
    total_amt = 0
    total_qty = 0
    prev = 0
    for _, price, qty in fills:
        if qty < prev:   # 새 주문으로 리셋
            prev = 0
        delta = qty - prev
        if delta > 0:
            total_amt += price * delta
            total_qty += delta
        prev = qty
    return (total_amt / total_qty if total_qty else 0), total_qty


def _apply_atr_fallback(t):
    """TP_TARGET_SET 로그가 없을 때 ATR로 SL/TP 추정 (차트 수평선 표시용)"""
    if t['sl'] == 0 and t['atr'] > 0 and t['entry_price'] > 0:
        ep = t['entry_price']
        t['sl']   = int(ep - t['atr'] * t['atr_sl_mult'])
        t['tp1']  = int(ep + t['atr'] * t['atr_tp_mult'])
        t['tp2']  = t['tp1']
        t['safe'] = int(ep - t['atr'] * 1.0)


def parse_trades(path):
    active = {}   # code → trade dict
    done   = []

    def new_trade(ts, name, code, cur_price, strategy):
        return {
            'code': code, 'name': name, 'strategy': strategy,
            'entry_time': ts, 'entry_price': 0, 'qty': 0,
            'atr': 0, 'atr_sl_mult': 2.0, 'atr_tp_mult': 1.2,
            'sl': 0, 'tp1': 0, 'tp2': 0, 'safe': 0,
            'candles': [], 'events': [],
            'exit_reason': None, 'exit_time': None,
            'buy_fills': [], 'sell_fills': [],
        }

    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip()

            m = P['ENTRY_QTY'].search(line)
            if m:
                ts, name, code, cp, strat = m.groups()
                if code not in active:
                    active[code] = new_trade(ts, name, code, int(cp), strat)
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
                    t['events'].append({'t': ts, 'k': 'TP_SET',
                        'label': f'손절 {sl} / TP1 {tp1} / 본절 {safe}'})
                continue

            m = P['TP_FALLBACK'].search(line)
            if m:
                ts, code, sl, tp1, tp2 = m.groups()
                if code in active:
                    t = active[code]
                    t['sl'], t['tp1'], t['tp2'] = int(sl), int(tp1), int(tp2)
                    t['events'].append({'t': ts, 'k': 'TP_SET',
                        'label': f'손절 {sl} / TP1 {tp1} (고정비율)'})
                continue

            m = P['CANDLE'].search(line)
            if m:
                ts, code, o, h, l, c, pnl = m.groups()
                if code in active:
                    active[code]['candles'].append(
                        {'t': ts, 'o': int(o), 'h': int(h), 'l': int(l),
                         'c': int(c), 'pnl': float(pnl)})
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
                        t['events'].append({'t': ts, 'k': key, 'label': EXIT_LABELS.get(reason, ('?','?',''))[1]})
                    break

            m = P['TP1_TRG'].search(line)
            if m:
                ts, code = m.groups()
                if code in active:
                    active[code]['events'].append({'t': ts, 'k': 'TP1_TRG', 'label': '🎯 TP1 도달'})
                continue

            m = P['TP1_FILL'].search(line)
            if m:
                ts, code, remain, high, tp2 = m.groups()
                if code in active:
                    active[code]['tp2'] = int(tp2)
                    active[code]['events'].append(
                        {'t': ts, 'k': 'TP1_FILL',
                         'label': f'✅ TP1 체결 잔여={remain}주 TP2={tp2}'})
                continue

            m = P['TP2_TRG'].search(line)
            if m:
                ts, code = m.groups()
                if code in active:
                    active[code]['events'].append({'t': ts, 'k': 'TP2_TRG', 'label': '🎯 TP2 도달'})
                continue

            m = P['TP2_FILL'].search(line)
            if m:
                ts, code = m.groups()
                if code in active:
                    active[code]['exit_reason'] = active[code]['exit_reason'] or 'TP2_FILL'
                    active[code]['events'].append({'t': ts, 'k': 'TP2_FILL', 'label': '✅ TP2 체결'})
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
        _apply_atr_fallback(t)
        done.append(t)

    return done


# ─── 손실 원인 분석 ──────────────────────────────────────────────────────────
def diagnose(t):
    """거래 데이터 → 손실 원인 텍스트 리스트"""
    diags = []
    entry = t['avg_buy'] or t['entry_price']
    candles = t['candles']
    reason = t['exit_reason']

    if not candles:
        diags.append(('warn', '캔들 데이터 없음 — 진입 직후 청산 또는 데이터 누락'))
        return diags

    # 첫 봉 pnl (진입 직후 방향)
    first_pnl = candles[0]['pnl'] * 100
    # 최대 수익
    max_pnl = max(c['pnl'] * 100 for c in candles)
    # 최저 pnl
    min_pnl = min(c['pnl'] * 100 for c in candles)
    # SL까지 봉 수
    sl_price = t['sl']
    tp1_price = t['tp1']
    total_candles = len(candles)
    final_pnl = t['pnl_pct']

    # 진입 직후 역전 (첫 봉 이미 마이너스)
    if first_pnl < -0.3:
        diags.append(('danger', f'진입 즉시 역전: 첫 봉 마감 {first_pnl:+.2f}% — 고점 추격 또는 슬리피지 과다'))

    # TP1 근접 후 미달
    if max_pnl > 0.3 and tp1_price > 0:
        tp1_gap = (tp1_price - entry) / entry * 100
        if max_pnl >= tp1_gap * 0.85 and final_pnl < 0:
            diags.append(('warn',
                f'TP1({tp1_gap:.2f}%) 근접(최대 {max_pnl:.2f}%) 후 손절 — 고점 포착 실패'))

    # 손절 지연 (STOP_LOSS인데 SL 이하 캔들이 여러 개)
    if reason == 'STOP_LOSS' and sl_price > 0:
        sl_candles = [c for c in candles if c['c'] <= sl_price]
        if len(sl_candles) > 1:
            diags.append(('warn',
                f'손절가 {sl_price} 아래 캔들 {len(sl_candles)}개 — 손절 지연 발생 (재시도 반복)'))

    # 본절보호 후 손실 (PROFIT_SAFE인데 마이너스)
    if reason == 'PROFIT_SAFE' and final_pnl < 0:
        diags.append(('danger',
            f'TP1 체결 후 본절보호 발동했으나 최종 PNL {final_pnl:.2f}% — 슬리피지·수수료 손실'))

    # 지속 하락 패턴
    declining = sum(1 for c in candles if c['c'] < c['o'])
    if declining >= 3 and total_candles >= 4:
        diags.append(('warn',
            f'음봉 {declining}개 / 전체 {total_candles}봉 — 진입 후 지속적 하락 흐름'))

    # 좁은 레인지 횡보 후 하락
    price_range = max(c['h'] for c in candles) - min(c['l'] for c in candles)
    avg_price = entry
    range_pct = price_range / avg_price * 100 if avg_price else 0
    if range_pct < 0.5 and total_candles >= 5 and final_pnl < 0:
        diags.append(('info',
            f'가격 레인지 {range_pct:.2f}% 협소 — 방향성 없이 횡보 후 손절'))

    # 최대 수익 충분했으나 TP 미달
    if max_pnl > 0.8 and tp1_price > 0 and final_pnl < 0:
        tp1_gap = (tp1_price - entry) / entry * 100
        if max_pnl < tp1_gap:
            diags.append(('info',
                f'최대 수익 {max_pnl:.2f}% — TP1 목표 {tp1_gap:.2f}%에 미달, ATR TP 배수 재검토 권장'))

    # 손실 원인별 요약
    reason_note = {
        'STOP_LOSS':   '완성봉 종가가 ATR×SL 기준가 이하 도달 → 손절 트리거',
        'SL_EMERG':    '실시간 틱이 긴급손절 기준(-EMERGENCY_SL_RATE) 초과',
        'TIME_STOP':   '진입 후 15분 내 방향성 미확인 → 시간 손절',
        'PROFIT_SAFE': 'TP1 이후 매수가(본절) 이하 밀림 → 본절보호 매도',
        'TRAIL_STOP':  '고점 대비 ATR×TRAIL 하락 → 트레일링 손절',
        'MINI_TRAIL':  '미니 트레일링 스탑 발동 (TP1 전 소수익 보호)',
        'FORCE_LIQ':   '15:20 장 마감 강제청산',
        'TP1_FILL':    'TP1 분할 매도 후 미청산 잔여 보유 중 청산',
        'TP2_FILL':    'TP2 목표 달성 후 잔여 분 트레일링/청산',
    }.get(reason, '')
    if reason_note:
        diags.append(('info', f'청산 사유: {reason_note}'))

    if not diags:
        diags.append(('ok', '특이 사항 없음 — 전략 정상 작동 범위'))

    return diags


# ─── HTML 생성 ───────────────────────────────────────────────────────────────
CHART_HTML = r"""
<canvas id="chart_{cid}" width="860" height="360" style="width:100%;max-width:860px;height:360px;display:block;margin:0 auto;"></canvas>
<script>
(function(){{
  const candles={candles_json};
  const entry={entry};
  const sl={sl};
  const tp1={tp1};
  const tp2={tp2};
  const entryTs="{entry_ts}";
  const exitTs="{exit_ts}";
  const events={events_json};
  const cvs=document.getElementById('chart_{cid}');
  if(!cvs)return;
  const ctx=cvs.getContext('2d');
  const W=cvs.width,H=cvs.height;
  const PAD={{l:60,r:12,t:20,b:36}};
  const CW=W-PAD.l-PAD.r, CH=H-PAD.t-PAD.b;

  // color scheme
  const dark=window.matchMedia&&window.matchMedia('(prefers-color-scheme:dark)').matches;
  const BG   = dark?'#1a1d23':'#f8f9fa';
  const GRID = dark?'rgba(255,255,255,0.06)':'rgba(0,0,0,0.07)';
  const TEXT  = dark?'#cdd3de':'#2c3e50';
  const BULL  = '#2ecc71';
  const BEAR  = '#e74c3c';
  const SL_C  = '#e74c3c';
  const TP1_C = '#3498db';
  const TP2_C = '#9b59b6';
  const ENT_C = '#f39c12';
  const SAFE_C= '#e67e22';

  if(!candles||candles.length===0){{
    ctx.fillStyle=TEXT; ctx.font='16px sans-serif';
    ctx.fillText('캔들 데이터 없음',W/2-60,H/2); return;
  }}

  const prices=[...candles.map(c=>c.h),...candles.map(c=>c.l),
    entry,sl,tp1,(tp2&&tp2!==tp1?tp2:null)].filter(v=>v>0);
  let pMin=Math.min(...prices), pMax=Math.max(...prices);
  const pad=(pMax-pMin)*0.08; pMin-=pad; pMax+=pad;

  const py=p=>PAD.t+CH-(p-pMin)/(pMax-pMin)*CH;
  const n=candles.length;
  const cw=Math.max(3,Math.floor(CW/n*0.7));
  const cx=i=>PAD.l+Math.round((i+0.5)/n*CW);

  // background
  ctx.fillStyle=BG; ctx.fillRect(0,0,W,H);

  // grid & y-axis labels
  ctx.strokeStyle=GRID; ctx.lineWidth=1;
  const steps=5;
  for(let i=0;i<=steps;i++){{
    const p=pMin+(pMax-pMin)*i/steps;
    const y=py(p);
    ctx.beginPath(); ctx.moveTo(PAD.l,y); ctx.lineTo(W-PAD.r,y); ctx.stroke();
    ctx.fillStyle=TEXT; ctx.font='10px monospace'; ctx.textAlign='right';
    ctx.fillText(Math.round(p).toLocaleString(),PAD.l-4,y+4);
  }}

  // horizontal lines: entry, SL, TP1, TP2
  function hline(price,color,label,dash){{
    if(!price||price<=0)return;
    const y=py(price);
    ctx.save(); ctx.strokeStyle=color; ctx.lineWidth=1.5;
    if(dash)ctx.setLineDash(dash); else ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(PAD.l,y); ctx.lineTo(W-PAD.r,y); ctx.stroke();
    ctx.fillStyle=color; ctx.font='bold 10px sans-serif'; ctx.textAlign='left';
    ctx.fillText(label+' '+Math.round(price).toLocaleString(),PAD.l+4,y-3);
    ctx.restore();
  }}
  hline(entry, ENT_C,  'ENT',[]);
  hline(sl,    SL_C,   'SL', [4,3]);
  hline(tp1,   TP1_C,  'TP1',[4,3]);
  if(tp2&&tp2!==tp1) hline(tp2, TP2_C, 'TP2',[4,3]);

  // candles
  for(let i=0;i<n;i++){{
    const c=candles[i];
    const x=cx(i);
    const bull=c.c>=c.o;
    const col=bull?BULL:BEAR;
    ctx.strokeStyle=col; ctx.lineWidth=1;
    ctx.beginPath();
    ctx.moveTo(x,py(c.h)); ctx.lineTo(x,py(c.l)); ctx.stroke();
    const top=py(Math.max(c.o,c.c));
    const bot=py(Math.min(c.o,c.c));
    const bh=Math.max(1,bot-top);
    ctx.fillStyle=col;
    ctx.fillRect(x-Math.floor(cw/2),top,cw,bh);
  }}

  // event markers on candle x positions
  // Map event timestamps to nearest candle index
  function tsToMin(ts){{
    // "2026-07-23 09:33:52,651" → minutes since midnight
    const parts=ts.split(' ');
    if(parts.length<2)return -1;
    const t=parts[1].split(':');
    return parseInt(t[0])*60+parseInt(t[1]);
  }}
  const candleMins=candles.map(c=>tsToMin(c.t));
  function nearestIdx(ts){{
    const m=tsToMin(ts);
    let best=0,bestD=1e9;
    candleMins.forEach((cm,i)=>{{if(Math.abs(cm-m)<bestD){{bestD=Math.abs(cm-m);best=i;}}}});
    return best;
  }}

  // event dots
  const evColors={{
    'TP1_TRG':'#3498db','TP1_FILL':'#2980b9',
    'TP2_TRG':'#9b59b6','TP2_FILL':'#8e44ad',
    'SL':'#e74c3c','SL_EMERG':'#c0392b',
    'SAFE':'#e67e22','TRAIL':'#27ae60','TIME_STOP':'#e67e22',
    'MINI_TRAIL':'#2ecc71','FORCE_LIQ':'#7f8c8d',
  }};
  events.forEach(ev=>{{
    const idx=nearestIdx(ev.t);
    const x=cx(idx);
    const c=candles[idx];
    const isGood=ev.k.startsWith('TP');
    const y=isGood?py(c.h)-12:py(c.l)+12;
    const col=evColors[ev.k]||'#aaa';
    ctx.fillStyle=col;
    ctx.beginPath();
    if(isGood){{
      // up triangle
      ctx.moveTo(x,y-6);ctx.lineTo(x-5,y+2);ctx.lineTo(x+5,y+2);ctx.closePath();
    }}else{{
      // down triangle
      ctx.moveTo(x,y+6);ctx.lineTo(x-5,y-2);ctx.lineTo(x+5,y-2);ctx.closePath();
    }}
    ctx.fill();
  }});

  // x-axis labels (show every N candles)
  const step=Math.max(1,Math.floor(n/8));
  ctx.fillStyle=TEXT; ctx.font='10px monospace'; ctx.textAlign='center';
  for(let i=0;i<n;i+=step){{
    const ts=candles[i].t;
    const label=ts.substring(11,16);
    ctx.fillText(label,cx(i),H-PAD.b+14);
  }}
}})();
</script>
"""

INDEX_ROW = '<tr class="r-{cls}" onclick="location.href=\'{url}\'">' \
            '<td>{name}</td><td>{code}</td><td>{strategy}</td>' \
            '<td>{entry_time}</td><td>{exit_time}</td>' \
            '<td class="pnl">{pnl_pct}</td><td>{exit_reason}</td></tr>\n'

def fmt_pnl(pct):
    sign = '+' if pct >= 0 else ''
    return f'{sign}{pct:.2f}%'

def fmt_ts(ts):
    return ts[11:19] if ts else '-'

def make_trade_html(t, cid, date_str):
    er = t['exit_reason'] or 'UNKNOWN'
    el = EXIT_LABELS.get(er, ('?', er, '#888'))
    pnl_cls = 'pos' if t['pnl_pct'] >= 0 else 'neg'
    strat_ko = STRATEGY_KO.get(t['strategy'], t['strategy'])
    diags = diagnose(t)

    diag_html = ''
    for dtype, dmsg in diags:
        color = {'danger':'#e74c3c','warn':'#e67e22','info':'#3498db','ok':'#27ae60'}.get(dtype,'#888')
        icon  = {'danger':'⚠️','warn':'⚡','info':'ℹ️','ok':'✅'}.get(dtype,'•')
        diag_html += f'<div class="diag" style="border-left:3px solid {color};padding:6px 10px;margin:4px 0;background:rgba(0,0,0,0.03)">{icon} {dmsg}</div>\n'

    events_for_chart = [e for e in t['events'] if e['k'] not in ('TP_SET',)]
    candles_json = json.dumps(t['candles'])
    events_json  = json.dumps(events_for_chart)

    entry = t['entry_price']
    sl = t['sl']
    tp1 = t['tp1']
    tp2 = t['tp2']

    chart = CHART_HTML.format(
        cid=cid,
        candles_json=candles_json,
        entry=entry, sl=sl, tp1=tp1, tp2=tp2,
        entry_ts=t['entry_time'],
        exit_ts=t['exit_time'] or '',
        events_json=events_json,
    )

    # Event timeline table
    ev_rows = ''
    for e in t['events']:
        ev_rows += f'<tr><td>{fmt_ts(e["t"])}</td><td>{e["label"]}</td></tr>\n'

    # Candle table (last 10)
    cnd_rows = ''
    for c in t['candles']:
        bullbear = '🟢' if c['c'] >= c['o'] else '🔴'
        pnl_str = f'{c["pnl"]*100:+.2f}%'
        pnl_style = 'color:#27ae60' if c['pnl'] >= 0 else 'color:#e74c3c'
        cnd_rows += (f'<tr><td>{fmt_ts(c["t"])}</td>'
                     f'<td>{bullbear}</td>'
                     f'<td>{c["o"]:,}</td><td>{c["h"]:,}</td>'
                     f'<td>{c["l"]:,}</td><td>{c["c"]:,}</td>'
                     f'<td style="{pnl_style}">{pnl_str}</td></tr>\n')

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t['name']} ({t['code']}) — {date_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#f8f9fa;--card:#fff;--text:#2c3e50;--muted:#636e72;--border:#dfe6e9}}
@media(prefers-color-scheme:dark){{:root{{--bg:#1a1d23;--card:#23272f;--text:#cdd3de;--muted:#808894;--border:#2e333d}}}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);padding:16px}}
h1{{font-size:1.3rem;margin-bottom:4px}}
.badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:.8rem;font-weight:700;margin-left:8px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:14px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.grid3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}}
.kv{{font-size:.85rem;margin:4px 0}}
.kv span:first-child{{color:var(--muted);width:90px;display:inline-block}}
.pos{{color:#27ae60;font-weight:700}}
.neg{{color:#e74c3c;font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}}
th{{background:var(--border);padding:6px 8px;text-align:left}}
td{{padding:5px 8px;border-bottom:1px solid var(--border)}}
.back{{display:inline-block;margin-bottom:12px;color:var(--muted);text-decoration:none;font-size:.85rem}}
.section-title{{font-size:.95rem;font-weight:700;margin-bottom:8px;padding-bottom:4px;border-bottom:2px solid var(--border)}}
</style>
<a class="back" href="index.html">← 목록으로</a>
<h1>{t['name']} <small style="color:var(--muted);font-size:.8em">({t['code']})</small>
  <span class="badge" style="background:{el[2]};color:#fff">{el[1]}</span>
  <span class="badge" style="background:#34495e;color:#fff">{strat_ko}</span>
</h1>
<p style="color:var(--muted);font-size:.82rem;margin:4px 0 12px">{date_str}</p>

<div class="card grid3">
  <div>
    <div class="kv"><span>진입시각</span><b>{fmt_ts(t['entry_time'])}</b></div>
    <div class="kv"><span>청산시각</span><b>{fmt_ts(t['exit_time'])}</b></div>
    <div class="kv"><span>보유시간</span><b>{_hold_min(t)} 분</b></div>
  </div>
  <div>
    <div class="kv"><span>평균매수</span><b>{t['avg_buy']:,.0f}원</b></div>
    <div class="kv"><span>평균매도</span><b>{t['avg_sell']:,.0f}원</b></div>
    <div class="kv"><span>수량</span><b>{t['qty']:,}주</b></div>
  </div>
  <div>
    <div class="kv"><span>손익(%)</span><b class="{pnl_cls}">{fmt_pnl(t['pnl_pct'])}</b></div>
    <div class="kv"><span>손익(원)</span><b class="{pnl_cls}">{t['pnl_raw']:+,}원</b></div>
    <div class="kv"><span>ATR</span><b>{t['atr']:.1f}원</b></div>
  </div>
</div>

<div class="card grid3">
  <div>
    <div class="kv"><span>손절가</span><b style="color:#e74c3c">{t['sl']:,}</b></div>
  </div>
  <div>
    <div class="kv"><span>TP1</span><b style="color:#3498db">{t['tp1']:,}</b></div>
  </div>
  <div>
    <div class="kv"><span>TP2</span><b style="color:#9b59b6">{t['tp2']:,}</b></div>
  </div>
</div>

<div class="card">
  <div class="section-title">분봉 차트 (보유 기간)</div>
  {chart}
</div>

<div class="card grid2">
  <div>
    <div class="section-title">손실 원인 분석</div>
    {diag_html}
  </div>
  <div>
    <div class="section-title">이벤트 타임라인</div>
    <table><tr><th>시각</th><th>이벤트</th></tr>{ev_rows}</table>
  </div>
</div>

<div class="card">
  <div class="section-title">분봉 데이터</div>
  <div style="overflow-x:auto">
  <table><tr><th>시각</th><th></th><th>시가</th><th>고가</th><th>저가</th><th>종가</th><th>손익</th></tr>
  {cnd_rows}</table>
  </div>
</div>
"""


def _hold_min(t):
    try:
        fmt = '%Y-%m-%d %H:%M:%S,%f'
        et = datetime.strptime(t['entry_time'], fmt)
        xt = datetime.strptime(t['exit_time'], fmt)
        return int((xt - et).total_seconds() / 60)
    except:
        return '-'


def make_index_html(trades, date_str):
    total_pnl = sum(t['pnl_raw'] for t in trades)
    wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    losses = sum(1 for t in trades if t['pnl_pct'] <= 0)
    wr = wins / len(trades) * 100 if trades else 0
    pnl_cls = 'pos' if total_pnl >= 0 else 'neg'

    rows = ''
    for t in sorted(trades, key=lambda x: x['entry_time']):
        er = t['exit_reason'] or 'UNKNOWN'
        el = EXIT_LABELS.get(er, ('?', er, '#888'))
        pc = t['pnl_pct']
        cls = 'pos' if pc >= 0 else 'neg'
        strat_ko = STRATEGY_KO.get(t['strategy'], t['strategy'])
        fname = f"{t['code']}_{t['name']}.html"
        badge = f'<span style="background:{el[2]};color:#fff;padding:2px 7px;border-radius:10px;font-size:.75rem">{el[1]}</span>'
        rows += (f'<tr class="r-{cls}" style="cursor:pointer" onclick="location.href=\'{fname}\'">'
                 f'<td><b>{t["name"]}</b></td><td style="color:#636e72">{t["code"]}</td>'
                 f'<td>{strat_ko}</td>'
                 f'<td>{fmt_ts(t["entry_time"])}</td><td>{fmt_ts(t["exit_time"])}</td>'
                 f'<td class="{cls}" style="font-weight:700">{fmt_pnl(pc)}</td>'
                 f'<td>{badge}</td></tr>\n')

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>매매 분석 — {date_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#f8f9fa;--card:#fff;--text:#2c3e50;--muted:#636e72;--border:#dfe6e9}}
@media(prefers-color-scheme:dark){{:root{{--bg:#1a1d23;--card:#23272f;--text:#cdd3de;--muted:#808894;--border:#2e333d}}}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);padding:20px;max-width:1000px;margin:0 auto}}
h1{{font-size:1.4rem;margin-bottom:4px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:14px}}
.summary{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;text-align:center}}
.stat{{padding:12px}}
.stat .val{{font-size:1.6rem;font-weight:700}}
.stat .lbl{{font-size:.78rem;color:var(--muted);margin-top:2px}}
.pos{{color:#27ae60}}.neg{{color:#e74c3c}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th{{background:var(--border);padding:7px 10px;text-align:left}}
td{{padding:7px 10px;border-bottom:1px solid var(--border)}}
tr:hover td{{background:rgba(52,73,94,.05)}}
</style>
<h1>📊 매매 분석 — {date_str}</h1>
<p style="color:var(--muted);font-size:.82rem;margin:4px 0 14px">총 {len(trades)}건</p>

<div class="card summary">
  <div class="stat">
    <div class="val {pnl_cls}">{total_pnl:+,}</div>
    <div class="lbl">총 손익 (원)</div>
  </div>
  <div class="stat">
    <div class="val">{len(trades)}</div>
    <div class="lbl">총 거래</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#27ae60">{wins}</div>
    <div class="lbl">수익 거래</div>
  </div>
  <div class="stat">
    <div class="val" style="color:#e74c3c">{losses}</div>
    <div class="lbl">손실 거래</div>
  </div>
</div>

<div class="card">
  <table>
    <tr><th>종목명</th><th>코드</th><th>전략</th><th>진입</th><th>청산</th><th>손익</th><th>사유</th></tr>
    {rows}
  </table>
</div>
"""


# ─── 메인 ────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_trades.py <log_folder> [output_folder]")
        sys.exit(1)

    log_folder = sys.argv[1].rstrip('\\/')
    out_folder = sys.argv[2].rstrip('\\/') if len(sys.argv) >= 3 else log_folder

    trade_log = os.path.join(log_folder, 'trade.log')
    if not os.path.exists(trade_log):
        print(f"[ERROR] trade.log 없음: {trade_log}")
        sys.exit(1)

    os.makedirs(out_folder, exist_ok=True)

    # 날짜 추출 (로그 첫 줄에서)
    date_str = '날짜 불명'
    with open(trade_log, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            m = re.match(r'(\d{4}-\d{2}-\d{2})', line)
            if m:
                d = m.group(1)
                date_str = f"{d[2:4]}/{d[5:7]}/{d[8:10]}"
                break

    print(f"[분석] {trade_log} 파싱 중...")
    trades = parse_trades(trade_log)
    print(f"[분석] 거래 {len(trades)}건 발견")

    if not trades:
        print("[분석] 완료된 거래 없음.")
        return

    # 개별 HTML
    for i, t in enumerate(sorted(trades, key=lambda x: x['entry_time'])):
        fname = f"{t['code']}_{t['name']}.html"
        fpath = os.path.join(out_folder, fname)
        html = make_trade_html(t, cid=i, date_str=date_str)
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(html)
        pnl_str = fmt_pnl(t['pnl_pct'])
        er = EXIT_LABELS.get(t['exit_reason'], ('?',t['exit_reason'],''))[1]
        er_ascii = EXIT_LABELS.get(t['exit_reason'], ('?', t['exit_reason'], ''))[0]
        print(f"  [{t['strategy']}] {t['name']}({t['code']}) {pnl_str} [{er_ascii}]  -> {fpath}")

    # index.html
    idx_path = os.path.join(out_folder, 'index.html')
    with open(idx_path, 'w', encoding='utf-8') as f:
        f.write(make_index_html(trades, date_str))
    print(f"\n[완료] index.html → {idx_path}")
    print(f"[완료] 총 손익: {sum(t['pnl_raw'] for t in trades):+,}원")


if __name__ == '__main__':
    main()
