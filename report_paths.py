#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_paths.py - 일별 리포트 폴더 위치 규칙

리포트는 reports/Report_YYMMDD/ 아래에 모은다.
프로젝트 루트에 Report_* 가 흩어져 있던 예전 구조도 계속 읽는다
(옮기지 않은 사람/백업 폴더에서도 분석이 돌아가도록).

쓰는 곳: For_analyzer.bat, run_analysis.py, filter_analysis.py,
        deep_analysis.py, analyze_trades.py
"""

import os
import re
import glob

REPORT_ROOT = 'reports'          # 새 구조의 상위 폴더
PREFIX = 'Report_'


def report_dir(yymmdd, base=None):
    """그날 리포트 폴더 경로 (없으면 만들지 않고 경로만 반환)"""
    base = base or os.getcwd()
    return os.path.join(base, REPORT_ROOT, f'{PREFIX}{yymmdd}')


def ensure_report_dir(yymmdd, base=None):
    path = report_dir(yymmdd, base)
    os.makedirs(path, exist_ok=True)
    return path


def report_dirs(base=None, require=None):
    """
    리포트 폴더 전체를 날짜순으로 반환한다.
    require: 이 파일이 있는 폴더만 (예: 'trade.log')
    새 구조(reports/Report_*)와 예전 구조(./Report_*)를 모두 본다.
    """
    base = base or os.getcwd()
    found = {}
    for pattern in (os.path.join(base, REPORT_ROOT, PREFIX + '*'),
                    os.path.join(base, PREFIX + '*')):
        for path in glob.glob(pattern):
            if not os.path.isdir(path):
                continue
            name = os.path.basename(path)
            if require and not os.path.exists(os.path.join(path, require)):
                continue
            # 같은 날짜가 양쪽에 있으면 새 구조를 우선
            if name not in found or REPORT_ROOT in path:
                found[name] = path
    return [found[k] for k in sorted(found)]


def report_files(filename, base=None):
    """모든 리포트 폴더에서 같은 이름의 파일 경로를 모은다 (예: signal.log)"""
    return [os.path.join(d, filename) for d in report_dirs(base, require=filename)]


def parse_date(folder_path):
    """폴더 경로 → 'YYYY-MM-DD' (못 읽으면 폴더명 그대로)"""
    name = os.path.basename(os.path.normpath(folder_path))
    m = re.search(PREFIX + r'(\d{2})(\d{2})(\d{2})', name)
    if m:
        yy, mm, dd = m.groups()
        return f'20{yy}-{mm}-{dd}'
    return name
