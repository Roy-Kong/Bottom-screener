"""date_utils.py — 순수 날짜 계산만 하는 헬퍼. pykrx/DB 등 무거운 의존성이 전혀
없다(2026-07 사고: list_backfill_dates.py가 이 함수 하나 쓰자고 backfill.py를
통째로 import했다가 market_data_collector -> pykrx까지 끌려와서, pykrx가
임포트 시점에 KRX 로그인 메시지를 stdout에 찍는 바람에 `INCLUDES=$(python
list_backfill_dates.py ...)`로 캡처한 값이 오염돼 git lfs pull --include가
전부 깨졌음 — 날짜 계산만 필요한 곳은 이 모듈만 쓰면 그 문제 자체가 안 생긴다)."""
from __future__ import annotations
import datetime as dt


def business_days(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:      # 0=월 ... 4=금
            yield d
        d += dt.timedelta(days=1)
