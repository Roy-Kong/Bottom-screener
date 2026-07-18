"""
db_reader.py — market_data.db에서 backtest.py가 쓰는 형태로 데이터를 읽어온다.

DB에는 필터링 전 원본이 그대로 들어있다(db.py/market_data_collector.py 참고).
그래서 screener.py가 라이브 수집 시 적용하는 ±30% 상하한가 필터를 여기서
'조회 시점에' 재적용한다 — 필터 로직이 나중에 바뀌어도 DB를 다시 채울 필요
없이 이 파일만 고치면 된다는 게 DB를 "원본만" 저장하기로 한 취지다.

종목 유니버스·업종 매핑·지수(코스피/코스닥/업종) 시계열은 의도적으로 DB에
없다 — 요청받은 4개 테이블은 종목별 원본 신호 입력값이 목적이고, 이런
메타데이터/지수 데이터는 매번 몇 번의 벌크 호출로 충분히 빠르게 가져올 수
있어 캐싱 이득이 크지 않다. 그래서 이 부분은 backtest.py에서 여전히 pykrx를
직접 호출한다."""
from __future__ import annotations
import sqlite3
import datetime as dt

import screener as scr


def find_trading_day_on_or_before_db(conn: sqlite3.Connection, target: dt.date) -> str | None:
    """DB만으로 target 이전(포함) 가장 최근 실제 거래일을 찾는다 — pykrx 호출 없이
       기준일을 정할 수 있어 완전히 오프라인으로 백테스트를 돌릴 수 있게 해준다."""
    ds = scr.yyyymmdd(target)
    row = conn.execute("SELECT MAX(date) FROM daily_prices WHERE date <= ?", (ds,)).fetchone()
    return row[0] if row and row[0] else None


def load_ohlcv_matrix_from_db(conn: sqlite3.Connection, dates: list[str]) -> dict[str, dict[str, tuple]]:
    """{date: {ticker: (close, volume)}} — screener.collect_ohlcv_matrix과 동일한 형태.
       DB는 원본 그대로라 여기서 screener.py와 똑같은 ±30% 상하한가 필터를
       재적용한다(수집 시점이 아니라 조회 시점에 필터링)."""
    if not dates:
        return {}
    placeholders = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT date, ticker, close, volume FROM daily_prices WHERE date IN ({placeholders})",
        dates).fetchall()
    by_date: dict[str, list[tuple]] = {}
    for d, tkr, close, vol in rows:
        by_date.setdefault(d, []).append((tkr, close, vol))

    matrix: dict[str, dict[str, tuple]] = {}
    last_close: dict[str, float] = {}
    for d in sorted(by_date.keys()):
        day: dict[str, tuple] = {}
        for tkr, close, vol in by_date[d]:
            if close is None or close <= 0:
                continue
            prev = last_close.get(tkr)
            if prev is not None and prev > 0:
                ratio = close / prev
                if ratio > scr.MAX_DAILY_MOVE_RATIO or ratio < 1 / scr.MAX_DAILY_MOVE_RATIO:
                    continue
            day[tkr] = (close, vol or 0.0)
            last_close[tkr] = close
        if day:
            matrix[d] = day
    return matrix


def load_fundamental_history_from_db(conn: sqlite3.Connection, dates: list[str]) -> dict[str, list[dict]]:
    """{ticker: [{date,PBR,DIV,DPS,EPS,BPS}, ...]} — dates가 주어진 순서(보통
       month_end_samples의 최신→과거 순)를 그대로 유지해서 반환한다. fund_hist[t][0]이
       최신 표본이어야 한다는 screener.py 쪽 가정과 맞춰야 하기 때문."""
    if not dates:
        return {}
    placeholders = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT date, ticker, pbr, div, dps, eps, bps FROM daily_fundamental WHERE date IN ({placeholders})",
        dates).fetchall()
    by_ticker: dict[str, dict[str, dict]] = {}
    for d, tkr, pbr, div, dps, eps, bps in rows:
        by_ticker.setdefault(tkr, {})[d] = {
            "date": d, "PBR": pbr or 0.0, "DIV": div or 0.0,
            "DPS": dps or 0.0, "EPS": eps or 0.0, "BPS": bps or 0.0,
        }
    hist: dict[str, list[dict]] = {}
    for tkr, date_map in by_ticker.items():
        hist[tkr] = [date_map[d] for d in dates if d in date_map]
    return hist


def load_short_max_from_db(conn: sqlite3.Connection, dates: list[str]) -> dict[str, float]:
    """collect_short_max 대응 — 주간 표본들 중 종목별 최댓값."""
    if not dates:
        return {}
    placeholders = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT ticker, MAX(short_ratio) FROM daily_short WHERE date IN ({placeholders}) GROUP BY ticker",
        dates).fetchall()
    return {tkr: ratio for tkr, ratio in rows if ratio is not None}


def load_short_current_from_db(conn: sqlite3.Connection, date: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT ticker, short_ratio FROM daily_short WHERE date = ?", (date,)).fetchall()
    return {tkr: ratio for tkr, ratio in rows if ratio is not None}


def load_market_cap_from_db(conn: sqlite3.Connection, date: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT ticker, market_cap FROM daily_prices WHERE date = ?", (date,)).fetchall()
    return {tkr: mc for tkr, mc in rows if mc is not None}


def load_accumulation_from_db(conn: sqlite3.Connection, dates: list[str]) -> dict[str, float]:
    """collect_accumulation 대응 — 주어진 날짜 구간의 기관+외국인 순매수 합계."""
    if not dates:
        return {}
    placeholders = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT ticker, SUM(inst_foreign_net_buy) FROM daily_investor_flow "
        f"WHERE date IN ({placeholders}) GROUP BY ticker",
        dates).fetchall()
    return {tkr: val for tkr, val in rows if val is not None}


def date_range_inclusive(all_dates_sorted: list[str], fromdate: str, todate: str) -> list[str]:
    """screener.py의 collect_accumulation(fromdate, todate) 호출(날짜 범위)과
       동등하게 동작하도록, 실제 존재하는 날짜 목록에서 [fromdate, todate] 구간만 자른다."""
    return [d for d in all_dates_sorted if fromdate <= d <= todate]
