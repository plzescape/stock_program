# -*- coding: utf-8 -*-
"""
tick_recorder.py - 조건검색 종목 체결/호가 녹화 (연구용 데이터 적재)

분봉 OHLCV 만으로는 단타 진입 우위가 나오지 않았다 (entry_research.py).
우위가 있다면 체결강도·호가잔량·체결 방향 같은 미시 정보에 있을 가능성이 커서,
모의투자 중에 조건검색에 걸린 종목의 웹소켓 실시간 데이터를 그대로 쌓아 둔다.

[저장]
  tick_data/YYYY-MM-DD/<code>.trade.csv   주식체결(0B) 한 틱 한 줄
  tick_data/YYYY-MM-DD/<code>.book.csv    호가잔량(0D) 종목당 최대 1초 1줄
  tick_data/YYYY-MM-DD/events.csv         조건검색 편입/이탈 등 이벤트
  지난 날짜 폴더의 csv 는 다음 실행 때 .csv.gz 로 압축한다.

[값]
  키움이 준 문자열을 그대로 저장한다 (부호 포함). 해석은 분석 쪽에서 한다.
  0B 거래량(15)은 +면 매수체결, -면 매도체결이다.

[설계 원칙] candle_recorder 와 같다. 이 모듈의 어떤 실패도 매매를 막으면 안 된다.
  호출부(메인 스레드)는 큐에 넣기만 하고, 파일 쓰기는 전용 스레드가 한다.
"""

import gzip
import os
import queue
import shutil
import threading
import time
from datetime import datetime

OUT_ROOT = 'tick_data'

# (컬럼명, 0B FID)
TRADE_FIELDS = [
    ('exch_time', '20'), ('price', '10'), ('vol', '15'), ('cum_vol', '13'),
    ('cum_amt', '14'), ('strength', '228'), ('ask1', '27'), ('bid1', '28'),
    ('chg_rate', '12'), ('open', '16'), ('high', '17'), ('low', '18'),
]
# (컬럼명, 0D FID)
BOOK_FIELDS = [
    ('exch_time', '21'), ('ask1', '41'), ('bid1', '51'), ('ask1_qty', '61'),
    ('bid1_qty', '71'), ('ask_total', '121'), ('bid_total', '125'),
]


class TickRecorder:
    def __init__(self, out_root=OUT_ROOT, log=None, book_interval_sec=1.0):
        self.out_root = out_root
        self.log = log
        self.book_interval = book_interval_sec
        self._q = queue.Queue(maxsize=200_000)
        self._last_book = {}            # code → 마지막 호가 기록 시각 (메인 스레드 전용)
        self._dropped = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='tick-recorder', daemon=True)
        self._thread.start()
        threading.Thread(target=self._compress_old_days, name='tick-compress', daemon=True).start()

    # ── 메인 스레드에서 호출 (큐에 넣기만) ──────────────────
    def record_trade(self, code, values):
        self._put('trade', code, [str(values.get(f, '')) for _, f in TRADE_FIELDS])

    def record_book(self, code, values):
        now = time.time()
        if now - self._last_book.get(code, 0.0) < self.book_interval:
            return
        self._last_book[code] = now
        self._put('book', code, [str(values.get(f, '')) for _, f in BOOK_FIELDS])

    def record_event(self, code, kind, detail=''):
        self._put('events', code, [kind, str(detail).replace(',', ' ')])

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _put(self, kind, code, row):
        try:
            self._q.put_nowait((datetime.now(), kind, code, row))
        except queue.Full:
            self._dropped += 1      # 디스크가 막혀도 매매는 계속

    # ── 쓰기 스레드 ─────────────────────────────────────────
    def _run(self):
        files = {}                  # (day, name) → file
        last_flush = time.time()
        while not (self._stop.is_set() and self._q.empty()):
            try:
                ts, kind, code, row = self._q.get(timeout=1.0)
            except queue.Empty:
                ts = None
            try:
                if ts is not None:
                    day = ts.strftime('%Y-%m-%d')
                    name = 'events' if kind == 'events' else f'{code}.{kind}'
                    f = files.get((day, name))
                    if f is None:
                        f = self._open(day, name, kind)
                        files[(day, name)] = f
                    stamp = ts.strftime('%H%M%S.%f')[:10]
                    lead = [stamp, code] if kind == 'events' else [stamp]
                    f.write(','.join(lead + row) + '\n')
                if time.time() - last_flush >= 2.0:
                    for fh in files.values():
                        fh.flush()
                    last_flush = time.time()
                    # 날짜가 바뀌면 지난 파일은 닫는다
                    today = datetime.now().strftime('%Y-%m-%d')
                    for key in [k for k in files if k[0] != today]:
                        files.pop(key).close()
            except Exception as e:
                if self.log:
                    self.log.warning(f'[TICK_REC_FAIL] {type(e).__name__}: {e}')
        for fh in files.values():
            try:
                fh.close()
            except Exception:
                pass

    def _open(self, day, name, kind):
        ddir = os.path.join(self.out_root, day)
        os.makedirs(ddir, exist_ok=True)
        path = os.path.join(ddir, f'{name}.csv')
        new = not os.path.exists(path)
        f = open(path, 'a', encoding='utf-8', newline='')
        if new:
            if kind == 'trade':
                cols = ['recv_time'] + [c for c, _ in TRADE_FIELDS]
            elif kind == 'book':
                cols = ['recv_time'] + [c for c, _ in BOOK_FIELDS]
            else:
                cols = ['recv_time', 'code', 'event', 'detail']
            f.write(','.join(cols) + '\n')
        return f

    def _compress_old_days(self):
        """오늘이 아닌 날짜 폴더의 csv 를 gz 로 압축 (실행 시작 시 1회)"""
        try:
            if not os.path.isdir(self.out_root):
                return
            today = datetime.now().strftime('%Y-%m-%d')
            for day in os.listdir(self.out_root):
                ddir = os.path.join(self.out_root, day)
                if day >= today or not os.path.isdir(ddir):
                    continue
                for fn in os.listdir(ddir):
                    if not fn.endswith('.csv'):
                        continue
                    src = os.path.join(ddir, fn)
                    with open(src, 'rb') as fi, gzip.open(src + '.gz', 'wb') as fo:
                        shutil.copyfileobj(fi, fo)
                    os.remove(src)
        except Exception as e:
            if self.log:
                self.log.warning(f'[TICK_REC_COMPRESS_FAIL] {e}')
