"""
db_reader.py — data/YYYYMMDD.db(하루 1파일)들에서 backtest.py가 쓰는 형태로
데이터를 읽어온다.

각 파일에는 필터링 전 원본이 그대로 들어있다(db.py/market_data_collector.py
참고). 그래서 screener.py가 라이브 수집 시 적용하는 ±30% 상하한가 필터를
여기서 '조회 시점에' 재적용한다 — 필터 로직이 나중에 바뀌어도 DB를 다시
채울 필요 없이 이 파일만 고치면 된다.

하루 1파일이라 SQLite의 ATTACH 개수 제한(기본 10, 최대 125)에 걸릴 수 있는
넓은 날짜range(예: 60개월 펀더멘털 히스토리)를 ATTACH 없이 파일을 하나씩 열고
닫으면서 순회한다 — 파일이 작아 오버헤드가 적다.

종목 유니버스·업종 매핑·지수(코스피/코스닥/업종) 시계열은 의도적으로 여기
없다 — 요청받은 4개 테이블은 종목별 원본 신호 입력값이 목적이고, 이런
메타데이터/지수 데이터는 매번 몇 번의 벌크 호출로 충분히 빠르게 가져올 수
있어 캐싱 이득이 크지 않다. 그래서 이 부분은 backtest.py에서 여전히 pykrx를
직접 호출한다."""
from __future__ import annotations
import sqlite3
import datetime as dt

import screener as scr
import db


def find_trading_day_on_or_before_db(target: dt.date) -> str | None:
    """data/ 파일 목록만으로 target 이전(포함) 가장 최근 실제 거래일을 찾는다 —
       pykrx 호출 없이 기준일을 정할 수 있다."""
    ds = scr.yyyymmdd(target)
    candidates = [d for d in db.existing_dates() if d <= ds]
    return candidates[-1] if candidates else None


def _read_day(date: str, table: str, columns: str) -> list[tuple]:
    path = db.daily_db_path(date)
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(f"SELECT {columns} FROM {table}").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def load_ohlcv_matrix_from_db(dates: list[str]) -> dict[str, dict[str, tuple]]:
    """{date: {ticker: (close, volume)}} — screener.collect_ohlcv_matrix과 동일한 형태.
       원본 그대로라 여기서 screener.py와 똑같은 ±30% 상하한가 필터를 재적용한다."""
    matrix: dict[str, dict[str, tuple]] = {}
    last_close: dict[str, float] = {}
    for d in sorted(dates):
        rows = _read_day(d, "daily_prices", "ticker, close, volume")
        if not rows:
            continue
        day: dict[str, tuple] = {}
        for tkr, close, vol in rows:
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


def load_fundamental_history_from_db(dates: list[str]) -> dict[str, list[dict]]:
    """{ticker: [{date,PBR,DIV,DPS,EPS,BPS}, ...]} — dates가 주어진 순서(보통
       month_end_samples의 최신→과거 순)를 그대로 유지해서 반환한다."""
    by_ticker: dict[str, dict[str, dict]] = {}
    for d in dates:
        rows = _read_day(d, "daily_fundamental", "ticker, pbr, div, dps, eps, bps")
        for tkr, pbr, div, dps, eps, bps in rows:
            by_ticker.setdefault(tkr, {})[d] = {
                "date": d, "PBR": pbr or 0.0, "DIV": div or 0.0,
                "DPS": dps or 0.0, "EPS": eps or 0.0, "BPS": bps or 0.0,
            }
    hist: dict[str, list[dict]] = {}
    for tkr, date_map in by_ticker.items():
        hist[tkr] = [date_map[d] for d in dates if d in date_map]
    return hist


def load_short_max_from_db(dates: list[str]) -> dict[str, float]:
    """collect_short_max 대응 — 주간 표본들 중 종목별 최댓값."""
    best: dict[str, float] = {}
    for d in dates:
        rows = _read_day(d, "daily_short", "ticker, short_ratio")
        for tkr, ratio in rows:
            if ratio is None:
                continue
            if tkr not in best or ratio > best[tkr]:
                best[tkr] = ratio
    return best


def load_short_current_from_db(date: str) -> dict[str, float]:
    rows = _read_day(date, "daily_short", "ticker, short_ratio")
    return {tkr: ratio for tkr, ratio in rows if ratio is not None}


def load_market_cap_from_db(date: str) -> dict[str, float]:
    rows = _read_day(date, "daily_prices", "ticker, market_cap")
    return {tkr: mc for tkr, mc in rows if mc is not None}


def load_accumulation_from_db(dates: list[str]) -> dict[str, float]:
    """collect_accumulation 대응 — 주어진 날짜 구간의 기관+외국인 순매수 합계."""
    total: dict[str, float] = {}
    for d in dates:
        rows = _read_day(d, "daily_investor_flow", "ticker, inst_foreign_net_buy")
        for tkr, val in rows:
            if val is None:
                continue
            total[tkr] = total.get(tkr, 0.0) + val
    return total


def date_range_inclusive(all_dates_sorted: list[str], fromdate: str, todate: str) -> list[str]:
    """screener.py의 collect_accumulation(fromdate, todate) 호출(날짜 범위)과
       동등하게 동작하도록, 실제 존재하는 날짜 목록에서 [fromdate, todate] 구간만 자른다."""
    return [d for d in all_dates_sorted if fromdate <= d <= todate]


def needed_dates_for_backtest(anchor: dt.date) -> list[str]:
    """이 anchor로 백테스트를 돌릴 때 실제로 필요한 날짜들(OHLCV 130일 +
       펀더멘털 60개월 표본 + 공매도 13주 표본, 중복 제거). backtest.yml이
       git lfs pull --include=로 이 날짜들만 선택적으로 받아오는 데 쓴다."""
    dates = set()
    dates.update(scr.recent_business_dates(scr.OHLCV_LOOKBACK_DAYS, anchor))
    dates.update(scr.month_end_samples(scr.FUND_HISTORY_MONTHS, anchor))
    dates.update(scr.weekly_samples(scr.SHORT_SAMPLE_WEEKS, anchor))
    return sorted(dates)
