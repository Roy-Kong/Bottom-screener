"""backfill_index.py — 코스피/코스닥/업종 지수 시가·고가·저가·종가를
data/index_history.sqlite(daily_index 테이블)에 채운다. 종목 데이터(db.py)와
달리 지수는 개수가 적어(코스피+코스닥 2개, 업종지수 수십 개) 전종목처럼
하루 단위로 쪼개 수집할 필요가 없다 — pykrx get_index_ohlcv(from, to, code)가
지정한 구간 전체를 한 번의 호출로 반환하므로, 연도 단위로만 나눠서 호출한다
(KRX 응답 쪽 극단적으로 긴 구간 처리를 방어하기 위한 보수적 조치일 뿐, 실측상
반드시 필요한 건 아니다).

daily_index 테이블은 index_code를 범용 문자열로 받게 설계돼 있어(index_db.py)
코스피/코스닥과 업종지수를 스키마 변경 없이 같은 테이블에 같이 담는다.
--sector-codes를 안 주면 코스피/코스닥만, 주면 그 코드들도 같이 백필한다.

사용법:
    python backfill_index.py --start 2022-01-01 --end 2026-07-20
    python backfill_index.py --start 2022-01-01 --sector-codes 1016,1017,...
  --end 생략 시 오늘."""
from __future__ import annotations
import argparse
import sys
import time
import datetime as dt

from pykrx_import import import_pykrx_stock
stock = import_pykrx_stock()
import screener as scr
import index_db


def year_chunks(start: dt.date, end: dt.date) -> list[tuple[str, str]]:
    """[start, end]를 연도 경계로 쪼갠 (from, to) YYYYMMDD 문자열 쌍 목록."""
    chunks = []
    cur = start
    while cur <= end:
        year_end = dt.date(cur.year, 12, 31)
        chunk_end = min(year_end, end)
        chunks.append((scr.yyyymmdd(cur), scr.yyyymmdd(chunk_end)))
        cur = dt.date(cur.year + 1, 1, 1)
    return chunks


def fetch_and_store(mkt: str, code: str, fromdate: str, todate: str) -> int:
    try:
        df = stock.get_index_ohlcv(fromdate, todate, code)
    except Exception as e:
        print(f"  [지수백필] {mkt}({code}) {fromdate}~{todate} 조회 실패: {e}")
        return 0
    if df is None or df.empty:
        return 0
    rows = []
    for idx_date, row in df.iterrows():
        try:
            d = idx_date.strftime("%Y%m%d")
        except AttributeError:
            d = str(idx_date)
        o = row.get("시가")
        h = row.get("고가")
        l = row.get("저가")
        c = row.get("종가")
        if c is None:
            continue
        rows.append((d, code, float(o) if o is not None else None,
                     float(h) if h is not None else None,
                     float(l) if l is not None else None, float(c)))
    if rows:
        conn = index_db.get_connection()
        index_db.upsert_index(conn, rows)
        conn.close()
    return len(rows)


def run(start_str: str, end_str: str, sector_codes: list[str] | None = None) -> None:
    start = dt.datetime.strptime(start_str, "%Y-%m-%d").date()
    end = dt.datetime.strptime(end_str, "%Y-%m-%d").date() if end_str else dt.date.today()
    targets = dict(scr.MARKET_INDEX_CODE)  # {"KOSPI": "1001", "KOSDAQ": "2001"}
    for code in (sector_codes or []):
        targets[f"SECTOR:{code}"] = code
    print(f"[지수백필] {scr.yyyymmdd(start)} ~ {scr.yyyymmdd(end)}, 대상 {len(targets)}개: {list(targets.keys())}")

    total = 0
    for label, code in targets.items():
        for fromdate, todate in year_chunks(start, end):
            n = fetch_and_store(label, code, fromdate, todate)
            print(f"  {label}({code}) {fromdate}~{todate}: {n}행")
            total += n
            time.sleep(scr.REQUEST_PAUSE)

    print(f"[지수백필] 완료 — 총 {total}행 저장(업서트)")
    for label, code in targets.items():
        dates = index_db.existing_dates(code)
        if dates:
            print(f"  {label}({code}): {len(dates)}일, {dates[0]}~{dates[-1]}")
        else:
            print(f"  {label}({code}): 저장된 데이터 없음")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="코스피/코스닥/업종 지수 OHLC 백필")
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default="")
    p.add_argument("--sector-codes", default="",
                    help="콤마구분 업종지수코드(예: 1016,1017,...). 비우면 코스피/코스닥만.")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    codes = [c.strip() for c in args.sector_codes.split(",") if c.strip()]
    run(args.start, args.end, sector_codes=codes)
