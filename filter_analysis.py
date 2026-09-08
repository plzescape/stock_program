#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filter_analysis.py - 진입 필터 병목 분석기

signal.log 의 *_CHECK 로그를 파싱해 "어떤 조건이 진입을 막고 있는가"를
정량화한다. 백테스트와 달리 지금 있는 로그만으로 즉시 실행 가능하다.

주의: 이 도구는 '진입이 몇 건 늘어나는가'만 답한다.
      '그 진입이 수익인가'는 답하지 못한다 → 그건 backtest.py 의 몫.

Usage:
    python filter_analysis.py                    # Report_*/signal.log 전부
    python filter_analysis.py Report_260826      # 특정 폴더
"""

import re, os, sys, glob
from collections import defaultdict, Counter

# Windows 콘솔(cp949)에서 UnicodeEncodeError 로 죽지 않도록
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# name=True/False 추출. 이름에 괄호/한글이 있어도 공백-= 앞까지 이름으로 본다.
COND_RE  = re.compile(r'([^\s=]+)=(True|False)')
CHECK_RE = re.compile(r'\[(BREAKOUT|PULLBACK)_CHECK\]\s+(\w+)\s+(.*)$')
FLAG_RE  = re.compile(r'\[FLAG_CHECK\]\s+(\w+)\s+(.*)$')
CONFIRM_RE = re.compile(r'\[(?:ENTRY|BREAKOUT|FLAG|PULLBACK)_CONFIRMED\].*?전략=(\w+)')


def parse_signal_log(path):
    """→ (checks, flag_msgs, confirms)"""
    checks    = {'BREAKOUT': [], 'PULLBACK': []}
    flag_msgs = Counter()
    confirms  = Counter()

    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            m = CHECK_RE.search(line)
            if m:
                strat, code, rest = m.groups()
                conds = {n: (v == 'True') for n, v in COND_RE.findall(rest)}
                if conds:
                    checks[strat].append((code, conds))
                continue

            m = FLAG_RE.search(line)
            if m:
                _, msg = m.groups()
                # 수치를 지워 메시지 유형별로 묶는다
                norm = re.sub(r'[\d.]+', 'N', msg).strip()
                flag_msgs[norm] += 1
                continue

            m = CONFIRM_RE.search(line)
            if m:
                confirms[m.group(1)] += 1

    return checks, flag_msgs, confirms


# strategy.py 에서 부정으로 쓰이는 조건: filters_ok = not f1 and not f2 and not f3
# → 로그의 True 는 '차단'을 뜻하므로 통과 판정 시 뒤집어야 한다.
INVERTED = {'f1', 'f2', 'f3'}


def normalize(conds):
    """로그값을 '통과=True' 기준으로 정규화"""
    return {k: ((not v) if k in INVERTED else v) for k, v in conds.items()}


def dedupe_aliases(rows, order):
    """
    값 벡터가 완전히 동일한 조건 = 같은 변수를 두 번 출력한 것.
    (예: BREAKOUT 의 surge/당일상승, ema_gap/EMA이격)
    대표 1개만 남기고 나머지는 별칭으로 묶는다.
    """
    sigs = {}
    for k in order:
        sig = tuple(c[k] for _, c in rows)
        sigs.setdefault(sig, []).append(k)
    keep, alias = [], {}
    for names in sigs.values():
        keep.append(names[0])
        if len(names) > 1:
            alias[names[0]] = names[1:]
    return [k for k in order if k in set(keep)], alias


def analyze_strategy(name, rows):
    if not rows:
        return

    rows  = [(code, normalize(c)) for code, c in rows]
    total = len(rows)
    order = list(rows[0][1].keys())
    order, alias = dedupe_aliases(rows, order)

    passed = Counter()
    for _, conds in rows:
        for k in order:
            if conds[k]:
                passed[k] += 1

    print(f"\n{'='*72}")
    print(f"  {name}  -  실패 평가 {total:,}회  (조건 {len(order)}개)")
    print(f"{'='*72}")
    if alias:
        for rep, dups in alias.items():
            print(f"  * '{rep}' 와 {dups} 는 같은 변수 (로그 중복) -> 1개로 계산")
        print()
    print(f"  {'조건':<24} {'통과':>8} {'통과율':>9}")
    print(f"  {'-'*68}")
    for k in sorted(order, key=lambda x: passed[x]):
        p = passed[k]
        rate = p / total * 100
        inv = ' (역조건)' if k in INVERTED else ''
        print(f"  {k:<24} {p:>8,} {rate:>8.2f}%  {'#'*int(rate/5)}{inv}")

    fail_counts  = Counter()
    sole_blocker = Counter()
    pair_blocker = Counter()
    for _, conds in rows:
        fails = [k for k in order if not conds[k]]
        fail_counts[len(fails)] += 1
        if len(fails) == 1:
            sole_blocker[fails[0]] += 1
        elif len(fails) == 2:
            pair_blocker[tuple(sorted(fails))] += 1

    print(f"\n  ── 실패 조건 개수 분포 ──")
    for n in sorted(fail_counts):
        c = fail_counts[n]
        print(f"  {n}개 실패: {c:>7,}회 ({c/total*100:>5.2f}%)")

    if sole_blocker:
        print(f"\n  ── 단독 차단 (이 조건 하나만 막았다) ──")
        print(f"     완화 시 즉시 진입 후보가 되는 건수")
        for k, c in sole_blocker.most_common(8):
            print(f"     {k:<24} {c:>6,}회")
    if pair_blocker:
        print(f"\n  ── 2개 동시 차단 (상위 5) ──")
        for pair, c in pair_blocker.most_common(5):
            print(f"     {' + '.join(pair):<40} {c:>6,}회")


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else None
    if targets:
        logs = [os.path.join(t, 'signal.log') for t in targets]
        logs = [p for p in logs if os.path.exists(p)]
    else:
        logs = sorted(glob.glob('Report_*/signal.log'))
        if os.path.exists('logs/signal.log') and os.path.getsize('logs/signal.log') > 0:
            logs.append('logs/signal.log')

    if not logs:
        print("[ERROR] signal.log 를 찾을 수 없습니다. Report_* 폴더를 확인하세요.")
        sys.exit(1)

    all_checks = {'BREAKOUT': [], 'PULLBACK': []}
    all_flag   = Counter()
    all_conf   = Counter()

    for p in logs:
        print(f"[읽는 중] {p}")
        c, fm, cf = parse_signal_log(p)
        for k in all_checks:
            all_checks[k].extend(c[k])
        all_flag.update(fm)
        all_conf.update(cf)

    for strat in ('BREAKOUT', 'PULLBACK'):
        analyze_strategy(strat, all_checks[strat])

    if all_flag:
        total_flag = sum(all_flag.values())
        print(f"\n{'='*72}")
        print(f"  FLAG  -  평가 {total_flag:,}회 (비정형 로그 → 메시지 유형별)")
        print(f"{'='*72}")
        for msg, c in all_flag.most_common(10):
            print(f"  {c:>7,}회 ({c/total_flag*100:>5.2f}%)  {msg[:56]}")

    print(f"\n{'='*72}")
    print(f"  진입 성사")
    print(f"{'='*72}")
    total_eval = sum(len(v) for v in all_checks.values()) + sum(all_flag.values())
    total_conf = sum(all_conf.values())
    for s, c in all_conf.most_common():
        print(f"  {s:<12} {c:>4}건")
    print(f"  {'합계':<12} {total_conf:>4}건  /  평가 {total_eval:,}회 "
          f"= {total_conf/total_eval*100 if total_eval else 0:.4f}%")

    print(f"""
{'='*72}
  해석 가이드
{'='*72}
  - *_CHECK 로그는 '실패했을 때만' 기록된다 (통과 시 *_CONFIRMED).
    따라서 위 표본은 전부 진입 실패 건이며, '0개 실패'는 나오지 않는다.
  - 통과율이 극단적으로 낮은 조건 = 사실상 하드 차단기
  - '단독 차단' 건수 = 그 조건만 완화하면 즉시 늘어나는 진입 후보 수
  - '2개 동시 차단'이 대부분이면 조건 하나만 풀어도 진입은 안 늘어난다

  [!] 이 분석은 '진입 횟수'만 말한다. 늘어난 진입이 수익인지는
    backtest.py 로 확인해야 한다. 필터를 푸는 것 자체가 개선은 아니다.
""")


if __name__ == '__main__':
    main()
