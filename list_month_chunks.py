"""list_month_chunks.py — START~END 구간을 월 단위로 잘라 "YYYY-MM-DD,YYYY-MM-DD"
줄들을 출력한다(각 줄이 한 달치 범위, 첫/마지막 달은 START/END로 잘림).

2026-07: backfill_investor_breakdown.yml이 기본 범위(2022-01-03~오늘, 1150+
영업일) 전체를 한 번에 git lfs pull하려다 대부분 실패해 실제로는 9일만
처리된 사고가 있었다 — 최초 DB 백필도 multi-chunk로 나눠 했던 것처럼, 이
스크립트로 월 단위 청크를 만들어 LFS pull+백필을 청크마다 반복한다.

사용법: python list_month_chunks.py START(YYYY-MM-DD) END(YYYY-MM-DD)
"""
from __future__ import annotations
import sys
import calendar
import datetime as dt

if __name__ == "__main__":
    start = dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    end = dt.datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
    cur = start
    while cur <= end:
        last_day = calendar.monthrange(cur.year, cur.month)[1]
        month_end = dt.date(cur.year, cur.month, last_day)
        chunk_end = min(month_end, end)
        print(f"{cur.isoformat()},{chunk_end.isoformat()}")
        cur = chunk_end + dt.timedelta(days=1)
