"""
market_data_collector.py — 하루치 원본 데이터(종가·거래량·시가총액·기관+외국인
순매수·공매도잔고비중·PBR/DIV/DPS/EPS/BPS)를 pykrx에서 모아 db.py 스키마에 맞는
행(row) 리스트로 변환한다. backfill.py와 update_db_daily.py가 공유해서 쓴다
(수집 로직을 두 군데서 따로 짜면 나중에 어긋나기 쉬우므로 하나로 통일).

의도적으로 여기서는 아무 필터도 적용하지 않는다(±30% 상하한가 필터, 미조정
분할 방어 등) — DB는 pykrx가 준 값 그대로 저장하고, screener.py/backtest.py가
DB에서 읽어 점수를 계산할 때 그 시점의 필터 로직을 적용한다. 필터링 로직이
바뀌어도 DB를 다시 채울 필요가 없게 하기 위함(db.py 모듈 docstring 참고).

collect_day(date, tables)에서 daily_prices를 요청하지 않으면 휴장일 판정을
OHLCV 호출 없이는 미리 알 수 없으므로, 그 경우 나머지 각 테이블 API를 게이트
없이 그대로 호출한다 — KRX가 휴장일엔 빈 DataFrame을 반환하므로 결과적으로
안전하다(호출 몇 번 낭비될 뿐)."""
from __future__ import annotations
import time

from pykrx_import import import_pykrx_stock
stock = import_pykrx_stock()
import screener as scr
import db

DEFAULT_TABLES = tuple(db.ALL_TABLES)


def collect_day(date: str, tables=DEFAULT_TABLES) -> dict[str, list[tuple]]:
    """요청한 테이블만 pykrx에서 모아 {"daily_prices": [...], ...} 형태로 반환한다
       (요청 안 한 테이블 키는 빠진다). 휴장일이면 해당 테이블들이 빈 리스트."""
    tables = set(tables)
    result: dict[str, list[tuple]] = {t: [] for t in tables}

    ohlcv: dict[str, tuple[float, float, float, float, float]] = {}  # {ticker: (open,high,low,close,vol)}
    checked_holiday = False
    if "daily_prices" in tables:
        for mkt in scr.TARGET_MARKETS:
            try:
                df = stock.get_market_ohlcv_by_ticker(date, market=mkt)
            except Exception:
                df = None
            if df is not None and not df.empty:
                for tkr, row in df.iterrows():
                    close = row.get("종가")
                    if close is None or float(close) <= 0:
                        continue
                    open_ = row.get("시가")
                    high = row.get("고가")
                    low = row.get("저가")
                    vol = row.get("거래량")
                    ohlcv[tkr] = (
                        float(open_) if open_ is not None else 0.0,
                        float(high) if high is not None else 0.0,
                        float(low) if low is not None else 0.0,
                        float(close), float(vol or 0),
                    )
            time.sleep(scr.REQUEST_PAUSE)
        checked_holiday = True

        mc_map: dict[str, float] = {}
        if ohlcv:  # 휴장일이면 시총 호출도 생략
            for mkt in scr.TARGET_MARKETS:
                try:
                    df = stock.get_market_cap_by_ticker(date, market=mkt)
                except Exception:
                    df = None
                if df is not None and not df.empty:
                    for tkr, row in df.iterrows():
                        mc_map[tkr] = float(row.get("시가총액", 0) or 0)
                time.sleep(scr.REQUEST_PAUSE)

        result["daily_prices"] = [
            (date, tkr, o, h, l, c, v, mc_map.get(tkr))
            for tkr, (o, h, l, c, v) in ohlcv.items()
        ]

    skip_rest = checked_holiday and not ohlcv  # daily_prices도 요청했는데 휴장으로 이미 확인됨

    if "daily_fundamental" in tables and not skip_rest:
        fundamentals: list[tuple] = []
        for mkt in scr.TARGET_MARKETS:
            try:
                df = stock.get_market_fundamental_by_ticker(date, market=mkt)
            except Exception:
                df = None
            if df is not None and not df.empty:
                for tkr, row in df.iterrows():
                    fundamentals.append((
                        date, tkr,
                        float(row.get("PBR", 0) or 0), float(row.get("DIV", 0) or 0),
                        float(row.get("DPS", 0) or 0), float(row.get("EPS", 0) or 0),
                        float(row.get("BPS", 0) or 0),
                    ))
            time.sleep(scr.REQUEST_PAUSE)
        result["daily_fundamental"] = fundamentals

    if "daily_short" in tables and not skip_rest:
        short_rows: list[tuple] = []
        for mkt in scr.TARGET_MARKETS:
            try:
                df = stock.get_shorting_balance_by_ticker(date, market=mkt)
            except Exception:
                df = None
            if df is not None and not df.empty:
                for tkr, row in df.iterrows():
                    short_rows.append((date, tkr, float(row.get("비중", 0) or 0)))
            time.sleep(scr.REQUEST_PAUSE)
        result["daily_short"] = short_rows

    if "daily_investor_flow" in tables and not skip_rest:
        flow_map: dict[str, float] = {}
        for mkt in scr.TARGET_MARKETS:
            for investor in ["기관합계", "외국인"]:
                try:
                    df = stock.get_market_net_purchases_of_equities_by_ticker(date, date, mkt, investor)
                except Exception:
                    df = None
                if df is not None and not df.empty:
                    col = "순매수거래대금" if "순매수거래대금" in df.columns else df.columns[-1]
                    for tkr, row in df.iterrows():
                        flow_map[tkr] = flow_map.get(tkr, 0.0) + float(row.get(col, 0) or 0)
                time.sleep(scr.REQUEST_PAUSE)
        result["daily_investor_flow"] = [(date, tkr, val) for tkr, val in flow_map.items()]

    return result
