"""update_db_daily.py — daily.yml에서 매일 실행. 그날(가장 최근 영업일) 원본
데이터만 data/YYYYMMDD.db(하루 1파일)에 새로 만든다. 과거 파일은 절대 다시
열지 않는다(Git LFS 버전 중복 방지 — db.py 모듈 docstring 참고). backfill.py와
수집 로직(market_data_collector)을 공유하므로 두 경로가 어긋나지 않는다."""
from __future__ import annotations

import screener as scr
import db
import index_db
import market_data_collector as collector


def update_index(asof: str) -> None:
    """코스피/코스닥 지수 OHLC를 data/index_history.sqlite에 하루치 추가한다
       (backfill_index.py가 채운 과거 구간이 앞으로 계속 이어지도록, 종목
       데이터처럼 결측이 쌓이지 않게 매일 이 시점에 같이 갱신)."""
    rows = []
    for mkt, code in scr.MARKET_INDEX_CODE.items():
        try:
            df = scr.stock.get_index_ohlcv(asof, asof, code)
        except Exception as e:
            print(f"[DB갱신] 지수 {mkt}({code}) {asof} 조회 실패: {e}")
            continue
        if df is None or df.empty:
            continue
        row = df.iloc[0]
        rows.append((asof, code, float(row.get("시가")) if row.get("시가") is not None else None,
                     float(row.get("고가")) if row.get("고가") is not None else None,
                     float(row.get("저가")) if row.get("저가") is not None else None,
                     float(row.get("종가"))))
    if rows:
        conn = index_db.get_connection()
        index_db.upsert_index(conn, rows)
        conn.close()
        print(f"[DB갱신] 지수 {asof} 저장 완료: {len(rows)}건")


def run() -> None:
    asof = scr.find_latest_trading_day()
    print(f"[DB갱신] 기준일 {asof}")
    update_index(asof)
    if db.date_file_exists(asof):
        print(f"[DB갱신] {asof}는 이미 있음, 건너뜀")
        return
    day_data = collector.collect_day(asof)
    path = db.save_single_day(asof, day_data)
    print(f"[DB갱신] {asof} 저장 완료({path}): 종가 {len(day_data['daily_prices'])}건, "
          f"펀더멘털 {len(day_data['daily_fundamental'])}건, "
          f"공매도 {len(day_data['daily_short'])}건, "
          f"수급 {len(day_data['daily_investor_flow'])}건")


if __name__ == "__main__":
    run()
