"""list_backtest_dates.py — anchor 날짜 기준으로 백테스트(source=db)가 실제로
필요로 하는 data/*.db 파일 목록을 콤마로 구분해서 표준출력에 찍는다.
backtest.yml이 이 출력을 `git lfs pull --include=`에 그대로 넣어서, 전체
히스토리를 받지 않고 필요한 날짜만 선택적으로 받아온다.

날짜 계산은 db_reader.needed_dates_for_backtest가 하는데, 이는 screener.py의
recent_business_dates/month_end_samples/weekly_samples를 그대로 재사용한다 —
즉 backtest.py가 실제로 읽는 창(window)과 항상 같은 로직을 쓰므로, 여기서
계산한 목록과 실제 필요한 날짜가 어긋날 일이 없다.

사용법: python list_backtest_dates.py YYYYMMDD
"""
from __future__ import annotations
import sys
import datetime as dt

import db_reader as dbr

if __name__ == "__main__":
    anchor_str = sys.argv[1]
    anchor = dt.datetime.strptime(anchor_str, "%Y%m%d").date()
    dates = dbr.needed_dates_for_backtest(anchor)
    print(",".join(f"data/{d}.db" for d in dates))
