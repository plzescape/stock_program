#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
candle_recorder.py - 라이브 스캔 중 분봉 자동 적재

main.py 가 스캔하면서 이미 OPT10080 으로 받아오는 분봉을 그대로
candle_data/ 에 저장한다. TR 요청을 추가로 하지 않으므로
키움 TR 제한에 전혀 영향을 주지 않는다.

설계 원칙: 이 모듈의 어떤 실패도 실매매를 방해해서는 안 된다.
           호출부는 반드시 try/except 로 감싸고, 여기서도 모든 예외를 삼킨다.
"""

import os, json, threading

OUT_ROOT = 'candle_data'


def fix_korean(s):
    """
    키움 OCX가 준 cp949 바이트를 PyQt5가 latin-1로 디코딩해
    'Áß¾ÓÃ·´Ü¼ÒÀç' 같은 깨진 문자열이 되는 경우를 복구한다.

    안전장치: 원본에 한글이 이미 있으면 건드리지 않고,
              복구 결과에 한글이 생길 때만 교체한다.
    """
    if not s:
        return s
    if any('가' <= ch <= '힣' for ch in s):
        return s                      # 이미 정상
    try:
        fixed = s.encode('latin-1').decode('cp949')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    if any('가' <= ch <= '힣' for ch in fixed):
        return fixed
    return s


_lock  = threading.Lock()
_cache = {}          # (date, code) -> {t: bar}
_dirty = set()       # 저장이 필요한 (date, code)
_names = {}          # code -> name


def record(code, name, candles, interval_min):
    """
    candles: parse_candle 결과 (최신->과거, 'time' 키 필요)
    같은 종목을 반복 스캔하면 봉이 누적 병합된다.
    """
    if not candles:
        return
    try:
        with _lock:
            if name:
                _names[code] = fix_korean(name)
            for c in candles:
                ts = str(c.get('time') or '')
                if len(ts) < 12:          # YYYYMMDDHHMM 최소
                    continue
                day = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
                key = (day, code)
                bars = _cache.setdefault(key, {})
                hm = ts[8:12]
                bar = {'t': hm, 'o': c['open'], 'h': c['high'],
                       'l': c['low'], 'c': c['close'], 'v': c['volume']}
                if bars.get(hm) != bar:
                    bars[hm] = bar
                    _dirty.add(key)
        _flush(interval_min)
    except Exception:
        pass          # 실매매 보호: 어떤 예외도 밖으로 내보내지 않는다


def _flush(interval_min):
    try:
        with _lock:
            pending = list(_dirty)
            _dirty.clear()
        for day, code in pending:
            with _lock:
                bars = dict(_cache.get((day, code), {}))
                name = _names.get(code, '')
            if len(bars) < 20:            # 너무 적으면 아직 저장 안 함
                continue
            ddir = os.path.join(OUT_ROOT, day)
            os.makedirs(ddir, exist_ok=True)
            tmp = os.path.join(ddir, f".{code}.tmp")
            dst = os.path.join(ddir, f"{code}.json")
            payload = {
                'code': code, 'name': name, 'date': day,
                'interval_min': interval_min,
                'candles': [bars[k] for k in sorted(bars)],   # 과거->최신
            }
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, dst)          # 원자적 교체 (읽는 쪽 깨짐 방지)
    except Exception:
        pass


def stats():
    """적재 현황 요약 (디버그용)"""
    with _lock:
        days = {d for d, _ in _cache}
        return {'days': len(days), 'series': len(_cache),
                'bars': sum(len(v) for v in _cache.values())}
