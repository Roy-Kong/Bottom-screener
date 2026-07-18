"""update_db_daily.py — daily.yml에서 매일 실행. 그날(가장 최근 영업일) 원본
데이터만 market_data.db에 추가한다. backfill.py와 수집 로직(market_data_collector)을
공유하므로 두 경로가 어긋나지 않는다."""
from __future__ import annotations

import screener as scr
import db
import market_data_collector as collector


def run() -> None:
    asof = scr.find_latest_trading_day()
    print(f"[DB갱신] 기준일 {asof}")
    conn = db.get_connection()
    if db.date_already_collected(conn, asof):
        print(f"[DB갱신] {asof}는 이미 DB에 있음, 건너뜀")
        conn.close()
        return
    day_data = collector.collect_day(asof)
    db.save_day(conn, asof, day_data)
    conn.close()
    print(f"[DB갱신] {asof} 저장 완료: 종가 {len(day_data['daily_prices'])}건, "
          f"펀더멘털 {len(day_data['daily_fundamental'])}건, "
          f"공매도 {len(day_data['daily_short'])}건, "
          f"수급 {len(day_data['daily_investor_flow'])}건")


if __name__ == "__main__":
    run()
