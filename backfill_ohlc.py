"""
backfill_ohlc.py — 이미 close/volume/market_cap이 채워진 daily_prices 기존
행에 open/high/low만 채워 넣는다(2단계 백필). 기존 backfill.py는 "테이블
전체가 없으면 새로 INSERT"가 목적이라 여기 용도(이미 있는 행에 새 컬럼값만
병합)와 다르므로 별도 스크립트로 뺐다.

pykrx의 OHLCV 조회(get_market_ohlcv_by_ticker)는 원래 시가/고가/저가/종가를
한 번에 반환하는데, 예전 수집 때는 종가·거래량만 뽑아 저장하고 나머지는
버렸다(market_data_collector.py는 이미 수정해서 앞으로는 처음부터 6개 값을
같이 저장함 — 이 스크립트는 "이미 저장된 과거 구간"만 대상).

collected_marker에 table_name="daily_prices_ohlc"로 진행 상황을 남겨 이어서
실행 가능(db.table_collected가 임의 문자열 table_name을 그대로 받아준다).

사용법:
    python backfill_ohlc.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] \\
                             [--max-runtime-min 50]
    --start 생략 시 2022-01-01, --end 생략 시 오늘.
"""
from __future__ import annotations
import argparse
import sys
import time
import datetime as dt

from pykrx import stock
import screener as scr
import db

MARKER = "daily_prices_ohlc"


def business_days(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def mark_ohlc_done(ds: str) -> None:
    path = db.daily_db_path(ds)
    if not path.exists():
        return
    conn = db.get_connection(path)
    conn.execute(
        "INSERT OR REPLACE INTO collected_marker (date, table_name) VALUES (?,?)", (ds, MARKER))
    conn.commit()
    conn.close()


def backfill_one_day(ds: str) -> int:
    """그 날짜의 시가/고가/저가를 라이브로 받아 기존 daily_prices 행에 병합.
       반환값: 병합한 종목 수(0이면 휴장 추정 또는 그 날짜 파일 자체가 없음)."""
    path = db.daily_db_path(ds)
    if not path.exists():
        return 0

    rows: list[tuple] = []
    for mkt in scr.TARGET_MARKETS:
        try:
            df = stock.get_market_ohlcv_by_ticker(ds, market=mkt)
        except Exception:
            df = None
        if df is not None and not df.empty:
            for tkr, row in df.iterrows():
                open_ = row.get("시가")
                high = row.get("고가")
                low = row.get("저가")
                if open_ is None or high is None or low is None:
                    continue
                rows.append((float(open_), float(high), float(low), ds, tkr))
        time.sleep(scr.REQUEST_PAUSE)

    if rows:
        conn = db.get_connection(path)
        db.upsert_ohlc_only(conn, rows)
        conn.commit()
        conn.close()
    mark_ohlc_done(ds)
    return len(rows)


def run(start_str: str, end_str: str, max_runtime_min: int) -> None:
    start = dt.datetime.strptime(start_str, "%Y-%m-%d").date()
    end = dt.datetime.strptime(end_str, "%Y-%m-%d").date()

    all_days = list(business_days(start, end))
    todo = [d for d in all_days if not db.table_collected(d.strftime("%Y%m%d"), MARKER)]

    print(f"[OHLC백필] 기간 {start_str}~{end_str}: 영업일 후보 {len(all_days)}일 중 미완료 {len(todo)}일")
    if not todo:
        print("[OHLC백필] 이미 전부 완료됨.")
        return

    t0 = time.time()
    deadline = t0 + max_runtime_min * 60
    done = holidays = errors = 0
    for d in todo:
        if time.time() > deadline:
            print(f"[OHLC백필] 시간 제한({max_runtime_min}분) 도달 — 중단. "
                  f"워크플로우를 다시 실행하면 여기부터 이어집니다.")
            break
        ds = d.strftime("%Y%m%d")
        try:
            n = backfill_one_day(ds)
        except Exception as e:
            print(f"  {ds}: 오류({e}) — 이번엔 건너뛰고 다음 실행에 재시도")
            errors += 1
            continue
        if n > 0:
            done += 1
        else:
            holidays += 1
        if (done + holidays) % 20 == 0:
            elapsed = (time.time() - t0) / 60
            print(f"  진행: {done + holidays}/{len(todo)}일 처리 ({ds}까지, "
                  f"실거래일 {done}·휴장/누락추정 {holidays}), 경과 {elapsed:.1f}분")

    remaining = len(todo) - done - holidays
    print(f"\n[OHLC백필] 이번 실행 요약: 실거래일 {done}일 병합, 휴장/누락추정 {holidays}일, "
          f"오류(재시도 대기) {errors}일, 남은 미완료 약 {max(remaining, 0)}일")
    if remaining > 0 or errors > 0:
        print("[OHLC백필] 아직 안 끝났습니다 — 워크플로우를 다시 실행해 이어가세요.")
    else:
        print(f"[OHLC백필] {start_str}~{end_str} 구간 완료.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="daily_prices 기존 행에 open/high/low 병합 백필")
    p.add_argument("--start", default="2022-01-01", help="시작일 YYYY-MM-DD (기본: 2022-01-01)")
    p.add_argument("--end", default=dt.date.today().strftime("%Y-%m-%d"),
                    help="종료일 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--max-runtime-min", type=int, default=50, help="이번 실행 최대 시간(분)")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    run(args.start, args.end, args.max_runtime_min)
