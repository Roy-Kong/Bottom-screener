"""
backfill.py — START~END 기간의 원본 데이터를 data/YYYYMMDD.db(하루 1파일)로
채운다. --tables로 4개 표준 테이블(daily_prices, daily_investor_flow,
daily_short, daily_fundamental) 중 원하는 것만 골라 채울 수 있다 — 예를 들어
가벼운 daily_fundamental만 먼저 과거 구간을 채우고, 무거운 나머지는 나중에
같은 스크립트를 다른 --tables로 다시 돌리는 식. 이미 수집된 (date, table)
조합은 건너뛰므로 여러 번 나눠 실행해도 이어서 진행되고, 서로 다른 --tables
실행끼리도 같은 날짜 파일을 공유하며 충돌하지 않는다. --tables가 표준 4개
전부(기본값)면 기존처럼 db.date_file_exists(존재 여부만)로 판단하고, 일부만
지정하면 db.table_collected로 파일을 열어 테이블별로 정확히 판단한다 — 후자는
대상 구간의 기존 파일을 미리 git lfs pull 받아둬야 하므로 backfill.yml이
list_backfill_dates.py로 그 목록을 계산해 전달한다.

KRX 로그인 세션이 로그인 시점부터 1시간 만에 만료되는 것으로 확인됐다(daily.yml
실행 로그의 "로그인 시간"/"만료 시간" 참고). 큰 백필은 한 세션 안에 못 끝날
가능성이 높아서, --max-runtime-min으로 자체 시간 제한을 두고 그 안에서만
처리한 뒤 깔끔하게 멈춘다.

사용법:
    python backfill.py [--tables daily_prices,daily_short,...] \\
                        [--start YYYY-MM-DD] [--end YYYY-MM-DD] \\
                        [--max-runtime-min 50]
    --tables 생략 시 기본값은 4개 테이블 전부.
    --start 생략 시 2022-01-01, --end 생략 시 오늘.
"""
from __future__ import annotations
import argparse
import sys
import time
import datetime as dt

import db
import market_data_collector as collector


def business_days(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:      # 0=월 ... 4=금
            yield d
        d += dt.timedelta(days=1)


def parse_tables(tables_arg: str) -> list[str]:
    tables = [t.strip() for t in tables_arg.split(",") if t.strip()]
    unknown = [t for t in tables if t not in db.ALL_TABLES]
    if unknown:
        raise ValueError(f"알 수 없는 테이블: {unknown} (허용: {db.ALL_TABLES})")
    return tables


def run(start_str: str, end_str: str, max_runtime_min: int, tables: list[str]) -> None:
    start = dt.datetime.strptime(start_str, "%Y-%m-%d").date()
    end = dt.datetime.strptime(end_str, "%Y-%m-%d").date()

    # 표준 4개 테이블을 전부 요청한 경우(기본값 — 기존 2022~현재 전체 백필과 동일한
    # 용도)는 date_file_exists만으로 판단한다(파일을 열지 않음 — CI의 checkout은
    # LFS 콘텐츠를 안 받아오므로 포인터 스텁 상태인데, 존재 여부만 보면 스텁이어도
    # 문제없다). 테이블을 일부만 요청한 경우(--tables daily_fundamental처럼 이
    # 프레임의 새 용도)만 db.table_collected로 파일을 열어 테이블별로 정확히
    # 판단한다 — 이 경로만 대상 구간의 기존 파일을 미리 git lfs pull 받아둬야
    # 한다(backfill.yml 참고).
    full_set = set(tables) == set(db.ALL_TABLES)
    all_days = list(business_days(start, end))
    todo: list[tuple[dt.date, list[str]]] = []
    for d in all_days:
        ds = d.strftime("%Y%m%d")
        if full_set:
            missing = [] if db.date_file_exists(ds) else list(tables)
        else:
            missing = [t for t in tables if not db.table_collected(ds, t)]
        if missing:
            todo.append((d, missing))

    print(f"[백필] 기간 {start_str}~{end_str}, 테이블 {tables}: "
          f"영업일 후보 {len(all_days)}일 중 미수집 {len(todo)}일")
    if not todo:
        print("[백필] 이미 전부 수집됨. 완료.")
        return

    t0 = time.time()
    deadline = t0 + max_runtime_min * 60
    done = holidays = errors = 0
    for d, missing in todo:
        if time.time() > deadline:
            print(f"[백필] 시간 제한({max_runtime_min}분) 도달 — 중단. "
                  f"워크플로우를 다시 실행하면 여기부터 이어집니다.")
            break
        ds = d.strftime("%Y%m%d")
        try:
            day_data = collector.collect_day(ds, tables=missing)
        except Exception as e:
            print(f"  {ds}: 수집 오류({e}) — 이번엔 건너뛰고 다음 실행에 재시도")
            errors += 1
            continue
        db.save_single_day(ds, day_data, tables=missing)
        if any(day_data.get(t) for t in missing):
            done += 1
        else:
            holidays += 1
        if (done + holidays) % 20 == 0:
            elapsed = (time.time() - t0) / 60
            print(f"  진행: {done + holidays}/{len(todo)}일 처리 ({ds}까지, 실거래일 {done}·휴장추정 {holidays}), "
                  f"경과 {elapsed:.1f}분")

    remaining = len(todo) - done - holidays
    all_files = db.existing_dates()
    print(f"\n[백필] 이번 실행 요약: 테이블 {tables}, 실거래일 {done}일 처리, 휴장 추정 {holidays}일, "
          f"오류(재시도 대기) {errors}일, 남은 미수집 약 {max(remaining, 0)}일")
    print(f"[백필] data/ 아래 총 날짜 파일 수: {len(all_files)}"
          f"{' (' + all_files[0] + '~' + all_files[-1] + ')' if all_files else ''}")
    if remaining > 0 or errors > 0:
        print(f"[백필] 아직 안 끝났습니다 — 워크플로우를 다시 실행해 이어가세요.")
    else:
        print(f"[백필] {start_str}~{end_str} 구간({tables}) 완료.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="원본 시세 데이터 백필 (테이블 선택 + 기간 선택)")
    p.add_argument("--tables", default=",".join(db.ALL_TABLES),
                    help=f"콤마로 구분된 테이블 목록 (기본값: 전체 — {','.join(db.ALL_TABLES)})")
    p.add_argument("--start", default="2022-01-01", help="시작일 YYYY-MM-DD (기본: 2022-01-01)")
    p.add_argument("--end", default=dt.date.today().strftime("%Y-%m-%d"),
                    help="종료일 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--max-runtime-min", type=int, default=50, help="이번 실행 최대 시간(분)")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    run(args.start, args.end, args.max_runtime_min, parse_tables(args.tables))
