"""list_backfill_dates.py — --start~--end 구간의 영업일에 대응하는 data/*.db 파일
목록을 콤마로 구분해서 표준출력에 찍는다. backfill.yml이 Run backfill 스텝 전에
이 출력을 `git lfs pull --include=`에 넣어, 이미 커밋된 파일들의 실제 LFS 내용을
미리 받아온다.

--tables가 표준 4개 테이블 전부(기본값 — 기존 2022~현재 전체 백필과 같은 용도)면
빈 문자열을 찍는다: 그 경로는 backfill.py가 date_file_exists(존재 여부만)로
판단하므로 파일 내용을 열 필요가 없고, 따라서 pull도 필요 없다 — 여기서 매번
전체 구간을 pull하면 daily.yml/backfill.yml에서 lfs:true를 뺀 의미가 없어진다
(Git LFS 무료 대역폭 한도를 다시 순식간에 넘길 수 있음).

--tables가 일부만 지정된 경우(예: daily_fundamental만)만 실제 날짜 목록을 찍는다
— 이 경로는 backfill.py가 db.table_collected로 파일을 열어 테이블별 수집 여부를
확인해야 하므로, 대상 구간에 이미 커밋된 파일은 실제 내용이 있어야 한다.

사용법: python list_backfill_dates.py START(YYYY-MM-DD) END(YYYY-MM-DD) TABLES(콤마구분)
"""
from __future__ import annotations
import sys
import datetime as dt

from backfill import business_days
import db

if __name__ == "__main__":
    start = dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    end = dt.datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
    tables = [t.strip() for t in sys.argv[3].split(",") if t.strip()]

    if set(tables) == set(db.ALL_TABLES):
        print("")
    else:
        dates = [d.strftime("%Y%m%d") for d in business_days(start, end)]
        print(",".join(f"data/{d}.db" for d in dates))
