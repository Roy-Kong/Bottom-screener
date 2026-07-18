"""
backfill.py — START_DATE부터 END_DATE까지 원본 데이터를 market_data.db에 채운다.

KRX 로그인 세션이 로그인 시점부터 1시간 만에 만료되는 것으로 확인됐다(daily.yml
실행 로그의 "로그인 시간"/"만료 시간" 참고). 전체 백필(2022-01~현재, 영업일
1000개+)은 한 세션 안에 못 끝날 가능성이 높아서, MAX_RUNTIME_MIN으로 자체 시간
제한을 두고 그 안에서만 처리한 뒤 깔끔하게 멈춘다. db.date_already_collected로
이미 처리한 날짜는 건너뛰므로, 워크플로우를 여러 번 다시 실행하면 이어서
진행된다(같은 방식으로 몇 번이고 재실행 가능).

사용법: python backfill.py [START=20220101] [END=오늘] [MAX_RUNTIME_MIN=50]
"""
from __future__ import annotations
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


def run(start_str: str, end_str: str, max_runtime_min: int) -> None:
    start = dt.datetime.strptime(start_str, "%Y%m%d").date()
    end = dt.datetime.strptime(end_str, "%Y%m%d").date()
    conn = db.get_connection()

    all_days = list(business_days(start, end))
    todo = [d for d in all_days if not db.date_already_collected(conn, d.strftime("%Y%m%d"))]
    print(f"[백필] 기간 {start_str}~{end_str}: 영업일 후보 {len(all_days)}일 중 미수집 {len(todo)}일")
    if not todo:
        print("[백필] 이미 전부 수집됨. 완료.")
        conn.close()
        return

    t0 = time.time()
    deadline = t0 + max_runtime_min * 60
    done = holidays = errors = 0
    for d in todo:
        if time.time() > deadline:
            print(f"[백필] 시간 제한({max_runtime_min}분) 도달 — 중단. "
                  f"워크플로우를 다시 실행하면 여기부터 이어집니다.")
            break
        ds = d.strftime("%Y%m%d")
        try:
            day_data = collector.collect_day(ds)
        except Exception as e:
            print(f"  {ds}: 수집 오류({e}) — 이번엔 건너뛰고 다음 실행에 재시도")
            errors += 1
            continue
        db.save_day(conn, ds, day_data)
        if day_data["daily_prices"]:
            done += 1
        else:
            holidays += 1
        if (done + holidays) % 20 == 0:
            elapsed = (time.time() - t0) / 60
            print(f"  진행: {done + holidays}/{len(todo)}일 처리 ({ds}까지, 실거래일 {done}·휴장추정 {holidays}), "
                  f"경과 {elapsed:.1f}분")

    conn.close()
    remaining = len(todo) - done - holidays
    counts = db.row_counts(db.get_connection())
    print(f"\n[백필] 이번 실행 요약: 실거래일 {done}일 처리, 휴장 추정 {holidays}일, "
          f"오류(재시도 대기) {errors}일, 남은 미수집 약 {max(remaining, 0)}일")
    print(f"[백필] DB 현재 상태: {counts}")
    if remaining > 0 or errors > 0:
        print(f"[백필] 아직 안 끝났습니다 — 워크플로우를 다시 실행해 이어가세요.")
    else:
        print(f"[백필] {start_str}~{end_str} 구간 완료.")


if __name__ == "__main__":
    start_arg = sys.argv[1] if len(sys.argv) > 1 else "20220101"
    end_arg = sys.argv[2] if len(sys.argv) > 2 else dt.date.today().strftime("%Y%m%d")
    max_runtime_arg = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    run(start_arg, end_arg, max_runtime_arg)
