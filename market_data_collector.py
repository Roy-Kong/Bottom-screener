"""
market_data_collector.py — 하루치 원본 데이터(종가·거래량·시가총액·기관+외국인
순매수·공매도잔고비중·PBR/DIV/DPS/EPS/BPS)를 pykrx에서 모아 db.py 스키마에 맞는
행(row) 리스트로 변환한다. backfill.py와 update_db_daily.py가 공유해서 쓴다
(수집 로직을 두 군데서 따로 짜면 나중에 어긋나기 쉬우므로 하나로 통일).

의도적으로 여기서는 아무 필터도 적용하지 않는다(±30% 상하한가 필터, 미조정
분할 방어 등) — DB는 pykrx가 준 값 그대로 저장하고, screener.py/backtest.py가
DB에서 읽어 점수를 계산할 때 그 시점의 필터 로직을 적용한다. 필터링 로직이
바뀌어도 DB를 다시 채울 필요가 없게 하기 위함(db.py 모듈 docstring 참고)."""
from __future__ import annotations
import time

from pykrx import stock
import screener as scr


def collect_day(date: str) -> dict[str, list[tuple]]:
    """{"daily_prices": [...], "daily_fundamental": [...], "daily_short": [...],
        "daily_investor_flow": [...]} 반환. 휴장일이면 daily_prices가 빈 리스트."""
    prices: list[tuple] = []
    fundamentals: list[tuple] = []
    short_rows: list[tuple] = []

    close_vol: dict[str, tuple[float, float]] = {}
    for mkt in scr.TARGET_MARKETS:
        try:
            df = stock.get_market_ohlcv_by_ticker(date, market=mkt)
        except Exception:
            df = None
        if df is not None and not df.empty:
            for tkr, row in df.iterrows():
                close = row.get("종가")
                vol = row.get("거래량")
                if close is None or float(close) <= 0:
                    continue
                close_vol[tkr] = (float(close), float(vol or 0))
        time.sleep(scr.REQUEST_PAUSE)

    mc_map: dict[str, float] = {}
    if close_vol:  # 휴장일이면 시총 호출도 생략
        for mkt in scr.TARGET_MARKETS:
            try:
                df = stock.get_market_cap_by_ticker(date, market=mkt)
            except Exception:
                df = None
            if df is not None and not df.empty:
                for tkr, row in df.iterrows():
                    mc_map[tkr] = float(row.get("시가총액", 0) or 0)
            time.sleep(scr.REQUEST_PAUSE)

    for tkr, (close, vol) in close_vol.items():
        prices.append((date, tkr, close, vol, mc_map.get(tkr)))

    if close_vol:
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

        for mkt in scr.TARGET_MARKETS:
            try:
                df = stock.get_shorting_balance_by_ticker(date, market=mkt)
            except Exception:
                df = None
            if df is not None and not df.empty:
                for tkr, row in df.iterrows():
                    short_rows.append((date, tkr, float(row.get("비중", 0) or 0)))
            time.sleep(scr.REQUEST_PAUSE)

    flow_map: dict[str, float] = {}
    if close_vol:
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
    investor_flow = [(date, tkr, val) for tkr, val in flow_map.items()]

    return {
        "daily_prices": prices,
        "daily_fundamental": fundamentals,
        "daily_short": short_rows,
        "daily_investor_flow": investor_flow,
    }
