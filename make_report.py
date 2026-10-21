#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_report.py - 하루치 로그를 리포트 폴더로 정리

For_analyzer.bat 이 하던 일을 파이썬으로 옮긴 것.
  1) reports/Report_YYMMDD/ 생성
  2) 엑셀 성과 리포트 생성 (generate_report.py)
  3) 종목별 차트 분석 HTML 생성 (analyze_trades.py)
  4) logs/*.log 를 리포트 폴더로 이동하고 logs 를 비움
  5) reports/index.html (전체 날짜 모아보기) 갱신

배치 파일에서 날짜 계산과 파일 이동을 하면 UTF-8 한글 출력과 섞여
cmd 가 줄을 잘못 읽는 문제가 있어 여기로 옮겼다.

Usage:
    python make_report.py             # 로그의 마지막 날짜로 정리
    python make_report.py 261021      # 날짜 지정 (YYMMDD)
"""

import os
import re
import sys
import glob
import shutil
import subprocess
from datetime import datetime

import report_paths

LOG_DIR = 'logs'
LOG_FILES = ('trade.log', 'signal.log', 'system.log', 'discord.log')


def log_date():
    """trade.log 의 마지막 기록 날짜(YYMMDD). 없으면 오늘."""
    path = os.path.join(LOG_DIR, 'trade.log')
    last = None
    if os.path.exists(path):
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'(\d{4})-(\d{2})-(\d{2})', line)
                if m:
                    last = m.groups()
    if last:
        return f'{last[0][2:]}{last[1]}{last[2]}'
    return datetime.now().strftime('%y%m%d')


def run(py, script, *args):
    print(f"  $ {script} {' '.join(args)}", flush=True)
    r = subprocess.run([py, '-X', 'utf8', script, *args])
    if r.returncode != 0:
        print(f"  [경고] {script} 실패 (코드 {r.returncode})")
    return r.returncode == 0


def build_index():
    """reports/index.html - 날짜별 요약 모아보기"""
    rows = []
    for folder in report_paths.report_dirs():
        date = report_paths.parse_date(folder)
        name = os.path.basename(folder)
        xlsx = glob.glob(os.path.join(folder, '*.xlsx'))
        gross = cost = net = None
        if xlsx:
            try:
                import openpyxl
                ws = openpyxl.load_workbook(xlsx[0], data_only=True).worksheets[0]
                for r in ws.iter_rows(values_only=True):
                    for v in r:
                        if not isinstance(v, str):
                            continue
                        num = re.sub(r'[^0-9-]', '', v)
                        if v.startswith('세전손익') and num:
                            gross = int(num)
                        elif v.startswith('총비용') and num:
                            cost = int(num)
                        elif v.startswith('순손익') and num:
                            net = int(num)
            except Exception:
                pass
        has_chart = os.path.exists(os.path.join(folder, 'index.html'))
        rows.append({'date': date, 'name': name, 'gross': gross, 'cost': cost,
                     'net': net, 'chart': has_chart,
                     'rel': os.path.relpath(folder, report_paths.REPORT_ROOT)})

    total = sum(r['net'] for r in rows if r['net'] is not None)
    tg = sum(r['gross'] for r in rows if r['gross'] is not None)
    tc = sum(r['cost'] for r in rows if r['cost'] is not None)

    def cell(v):
        if v is None:
            return '<td class="muted">-</td>'
        cls = 'pos' if v > 0 else ('neg' if v < 0 else '')
        return f'<td class="{cls}">{v:+,}</td>'

    body = '\n'.join(
        f'<tr><td>{r["date"]}</td>{cell(r["gross"])}{cell(r["cost"])}{cell(r["net"])}'
        f'<td>{"<a href=" + chr(34) + r["rel"] + "/index.html" + chr(34) + ">차트</a>" if r["chart"] else "<span class=muted>-</span>"}</td>'
        f'<td><a href="{r["rel"]}/">폴더</a></td></tr>'
        for r in reversed(rows))

    html = f"""<!doctype html><meta charset="utf-8">
<title>매매 리포트 모아보기</title>
<style>
 body{{font-family:'Malgun Gothic',sans-serif;background:#f6f7f9;margin:0;padding:24px;color:#222}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#777;font-size:13px;margin-bottom:16px}}
 .cards{{display:flex;gap:12px;margin-bottom:18px;flex-wrap:wrap}}
 .card{{background:#fff;border-radius:10px;padding:14px 18px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 .card .v{{font-size:20px;font-weight:700}} .card .k{{font-size:12px;color:#777}}
 table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;
        box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 th,td{{padding:9px 12px;text-align:right;font-size:13px;border-bottom:1px solid #eee}}
 th:first-child,td:first-child{{text-align:left}}
 th{{background:#fafafa;color:#555;font-weight:600}}
 .pos{{color:#1a7f37}} .neg{{color:#c0392b}} .muted{{color:#bbb}}
 a{{color:#2563eb;text-decoration:none}} a:hover{{text-decoration:underline}}
</style>
<h1>매매 리포트 모아보기</h1>
<div class="sub">{len(rows)}일 · 갱신 {datetime.now():%Y-%m-%d %H:%M}</div>
<div class="cards">
  <div class="card"><div class="k">세전 손익</div><div class="v {'pos' if tg>0 else 'neg'}">{tg:+,}원</div></div>
  <div class="card"><div class="k">수수료·세금</div><div class="v neg">{tc:+,}원</div></div>
  <div class="card"><div class="k">순손익</div><div class="v {'pos' if total>0 else 'neg'}">{total:+,}원</div></div>
</div>
<table>
<tr><th>날짜</th><th>세전</th><th>비용</th><th>순손익</th><th>차트</th><th>원본</th></tr>
{body}
</table>
"""
    os.makedirs(report_paths.REPORT_ROOT, exist_ok=True)
    out = os.path.join(report_paths.REPORT_ROOT, 'index.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    return out, len(rows), total


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base)
    py = sys.executable

    yymmdd = sys.argv[1] if len(sys.argv) > 1 else log_date()
    folder = report_paths.ensure_report_dir(yymmdd)
    xlsx_name = f"{yymmdd[:2]}_{yymmdd[2:4]}_{yymmdd[4:]}-Automated Trading Performance Report.xlsx"
    print(f"[리포트] {yymmdd} → {os.path.relpath(folder, base)}")

    trade_log = os.path.join(LOG_DIR, 'trade.log')
    if not os.path.exists(trade_log) or os.path.getsize(trade_log) == 0:
        print("  [중단] logs/trade.log 가 비어 있습니다. 이미 정리했는지 확인하세요.")
        return 1

    run(py, 'generate_report.py', trade_log, xlsx_name)
    if os.path.exists(xlsx_name):
        shutil.move(xlsx_name, os.path.join(folder, xlsx_name))

    print("  종목별 차트 분석 중...")
    run(py, 'analyze_trades.py', LOG_DIR, folder)

    moved = 0
    for name in LOG_FILES:
        src = os.path.join(LOG_DIR, name)
        if os.path.exists(src):
            shutil.move(src, os.path.join(folder, name))
            moved += 1
        open(src, 'w', encoding='utf-8').close()      # 새 로그 파일 준비
    print(f"  로그 {moved}개 이동 완료")

    out, days, total = build_index()
    print(f"  모아보기 갱신: {os.path.relpath(out, base)} ({days}일, 누적 {total:+,}원)")
    print("=" * 46)
    print("  리포트 생성 및 로그 정리 완료")
    print("=" * 46)
    return 0


if __name__ == '__main__':
    sys.exit(main())
