"""
backtest.py — 단일 과거 시점 기준 가벼운 백테스트.

screener.py의 데이터 수집/채점 함수는 원래부터 "오늘"이 아니라 anchor 날짜를
받아서 동작하도록 짜여 있다(초기 세션에서 고친 부분). 그래서 이 스크립트는
screener.py를 그대로 임포트해서 anchor만 과거 날짜로 바꿔 재사용한다 — 로직을
복제하지 않는다(복제하면 나중에 screener.py가 바뀔 때 백테스트만 stale해짐).

주의: 바닥 신호 7개 + 생존 게이트만 재현한다(턴어라운드 신호는 "최근" 상대적
움직임을 보는 신호라 이 용도에 안 맞아서 제외 — 요청대로).

사용법: python backtest.py [YYYYMMDD] [top_n]
  기본값: 20221031, 10
"""
from __future__ import annotations
import sys
import time
import datetime as dt
from statistics import median

from pykrx_import import import_pykrx_stock
stock = import_pykrx_stock()
import screener as scr
import signals as sg
import db_reader as dbr


def find_trading_day_on_or_before(target: dt.date, max_lookback: int = 14) -> str:
    """target 날짜 자체 또는 그 이전 중 실제 시세가 있는 가장 최근 영업일.
       find_latest_trading_day와 동일한 방식이지만 '오늘'이 아니라 임의의
       과거 날짜에서 시작한다 — 미래 데이터가 섞이지 않도록 반드시 target
       이전(또는 당일)으로만 거슬러 올라간다."""
    d = target
    for _ in range(max_lookback):
        if d.weekday() < 5:
            ds = scr.yyyymmdd(d)
            try:
                tickers = stock.get_market_ticker_list(ds, market="KOSPI")
            except Exception:
                tickers = []
            if tickers:
                return ds
        d -= dt.timedelta(days=1)
    return scr.yyyymmdd(d)


def price_on_or_after(code: str, target_date_str: str, is_index: bool = False,
                      window_days: int = 10) -> tuple[float | None, str | None]:
    """target_date_str(YYYYMMDD) 이후 첫 거래일의 종가와 그 실제 날짜.
       순방향 수익률 계산용 — target 당일이 휴장일이어도 며칠 안에서 다음
       거래일을 찾는다."""
    d = dt.datetime.strptime(target_date_str, "%Y%m%d").date()
    end = d + dt.timedelta(days=window_days)
    fromdate, todate = scr.yyyymmdd(d), scr.yyyymmdd(end)
    try:
        if is_index:
            df = stock.get_index_ohlcv(fromdate, todate, code)
        else:
            df = stock.get_market_ohlcv_by_date(fromdate, todate, code)
    except Exception:
        return None, None
    if df is None or df.empty:
        return None, None
    close = float(df.iloc[0]["종가"])
    actual_date = df.index[0]
    try:
        actual_date_str = actual_date.strftime("%Y%m%d")
    except AttributeError:
        actual_date_str = str(actual_date)
    return close, actual_date_str


def run_backtest(anchor_str: str, top_n: int = 10):
    anchor_target = dt.datetime.strptime(anchor_str, "%Y%m%d").date()
    asof = find_trading_day_on_or_before(anchor_target)
    anchor_date = dt.datetime.strptime(asof, "%Y%m%d").date()
    print(f"[백테스트] 요청 기준일 {anchor_str} → 실제 사용 영업일 {asof}")

    print("1) 종목 유니버스 수집…")
    universe, ticker_market = scr.get_universe(asof)
    print(f"   {len(universe)}개 종목")

    print("1b) 업종지수 매핑 수집…")
    sector_map, sector_names = scr.get_sector_index_map(asof)
    print(f"   {len(sector_map)}개 종목이 업종지수에 매핑됨")

    print("2) OHLCV 스냅샷 수집…")
    ohlcv_dates = scr.recent_business_dates(scr.OHLCV_LOOKBACK_DAYS, anchor_date)
    matrix = scr.collect_ohlcv_matrix(ohlcv_dates)
    latest_date = sorted(matrix.keys())[-1] if matrix else asof
    print(f"   {len(matrix)}개 영업일 확보, 최신={latest_date}")

    print("3) 펀더멘털 히스토리 수집…")
    fund_hist = scr.collect_fundamental_history(scr.month_end_samples(scr.FUND_HISTORY_MONTHS, anchor_date))

    print("4) 공매도 3개월 최고/현재, 시가총액 수집…")
    short_max = scr.collect_short_max(scr.weekly_samples(scr.SHORT_SAMPLE_WEEKS, anchor_date))
    short_cur = scr.collect_short_current(latest_date)
    market_cap = scr.collect_market_cap(latest_date)

    print("5) 매집(20일) 수집…")
    accum_from = ohlcv_dates[-scr.ACCUM_WINDOW_DAYS] if len(ohlcv_dates) >= scr.ACCUM_WINDOW_DAYS else ohlcv_dates[0]
    accum = scr.collect_accumulation(accum_from, latest_date)

    print("6) 지수 수익률(코스피·코스닥)…")
    market_idx_by_date: dict[str, dict[str, float]] = {}
    for mkt, code in scr.MARKET_INDEX_CODE.items():
        try:
            idx = stock.get_index_ohlcv(ohlcv_dates[0], latest_date, code)
            market_idx_by_date[mkt] = scr.index_close_by_date(idx)
        except Exception:
            market_idx_by_date[mkt] = {}

    print("6b) 업종지수 OHLCV 수집…")
    sector_codes_needed = set(sector_map.values())
    sector_idx_by_date = scr.collect_sector_index_ohlcv(sector_codes_needed, ohlcv_dates[0], latest_date)

    print("7) 채점(바닥 7개 신호만, 생존 게이트 포함)…")
    results = []
    for tkr, name in universe.items():
        dates, opens, highs, lows, closes, vols = scr.series_for_ticker(matrix, tkr)
        if len(closes) < 60 or len(vols) < 120:
            continue

        if scr.is_trading_halted(opens, highs, lows, closes, vols):
            continue
        last_close = closes[-1]
        avg_trading_value = median(vols[-20:]) * last_close
        if avg_trading_value < scr.MIN_AVG_TRADING_VALUE:
            continue
        cur_market_cap = market_cap.get(tkr, 0.0)
        if cur_market_cap < scr.MIN_MARKET_CAP:
            continue
        fh = fund_hist.get(tkr, [])
        cur_pbr = fh[0]["PBR"] if fh else 0.0
        if cur_pbr <= 0:
            continue

        split_suspected = scr.has_unadjusted_split_jump(closes)

        rec6to25 = median(vols[-25:-5])
        past120 = median(vols[-120:])
        ret60 = (closes[-1] / closes[-60]) - 1
        ret20_price = (closes[-1] / closes[-20]) - 1

        bench_series, bench_label = scr.resolve_benchmark_series(
            tkr, sector_map, sector_idx_by_date, market_idx_by_date, ticker_market)
        bench_c_latest = bench_series.get(latest_date)
        bench_c_60ago = bench_series.get(dates[-60])
        idx_ret_t = (bench_c_latest / bench_c_60ago - 1) if bench_c_latest and bench_c_60ago else 0.0

        pbr_series = [r["PBR"] for r in fh if r["PBR"] > 0]
        div_series = [r["DIV"] for r in fh if r["DIV"] > 0]
        cur_div = fh[0]["DIV"] if fh else 0.0
        cur_dps = fh[0]["DPS"] if fh else 0.0
        cur_eps = fh[0]["EPS"] if fh else 0.0
        bw_series = scr.bollinger_bandwidth_series(closes)
        float_mc = avg_trading_value * 50

        sector_name = sector_names.get(sector_map.get(tkr))
        pbr_caution_sector = scr.is_pbr_caution_sector(sector_name)
        capital_eroding = scr.had_progressive_capital_erosion(fh)
        bottom_weights = sg.BOTTOM_WEIGHTS
        if pbr_caution_sector:
            bottom_weights = dict(sg.BOTTOM_WEIGHTS)
            bottom_weights["pbr_low"] = bottom_weights["pbr_low"] / 2

        scores = {
            "volume_dryness": sg.score_volume_dryness(rec6to25, past120),
            "accumulation": sg.score_accumulation(accum.get(tkr, 0.0), float_mc, ret20_price * 100),
            "short_covering": sg.score_short_covering(short_cur.get(tkr, 0.0), short_max.get(tkr, 0.0)),
            "pbr_low": None if capital_eroding else sg.score_pbr_low(cur_pbr, pbr_series),
            "dividend_yield": sg.score_dividend_yield(cur_div, div_series, cur_dps, cur_eps, scr.had_dividend_cut(fh)),
            "relative_strength": None if split_suspected else sg.score_relative_strength(ret60, idx_ret_t),
            "volatility_squeeze": sg.score_volatility_squeeze(bw_series),
        }
        comp = sg.composite_score(scores, bottom_weights)
        if comp["composite"] is None or comp["composite"] < scr.BOTTOM_SCORE_THRESHOLD:
            continue

        results.append({
            "ticker": tkr, "name": name, "score": comp["composite"],
            "breakdown": comp["breakdown"], "anchor_close": last_close,
        })

    print(f"완료: {len(results)}개 채점 (기준일 {asof})")
    results.sort(key=lambda x: -x["score"])
    top = results[:top_n]

    print(f"\n[백테스트] 상위 {len(top)}개 종목 (기준일 {asof}):")
    for i, r in enumerate(top, 1):
        bd = ", ".join(f"{k}={v}" for k, v in r["breakdown"].items())
        print(f"  {i}. {r['name']}({r['ticker']}) 종합점수={r['score']} 종가={r['anchor_close']:.0f}")
        print(f"     breakdown: {bd}")

    print(f"\n[백테스트] 순방향 수익률 계산 중 (3/6/12개월)…")
    horizons = [("3M", 91), ("6M", 182), ("12M", 365)]

    kospi_base, _ = price_on_or_after("1001", asof, is_index=True)
    kospi_returns: dict[str, float | None] = {}
    for label, days in horizons:
        target_str = scr.yyyymmdd(anchor_date + dt.timedelta(days=days))
        close, actual = price_on_or_after("1001", target_str, is_index=True)
        kospi_returns[label] = round((close / kospi_base - 1) * 100, 1) if close and kospi_base else None
        print(f"   코스피 {label}: {kospi_returns[label]}% (실제 조회일 {actual})")
        time.sleep(scr.REQUEST_PAUSE)

    stock_rows = []
    for r in top:
        row = {"ticker": r["ticker"], "name": r["name"], "score": r["score"]}
        for label, days in horizons:
            target_str = scr.yyyymmdd(anchor_date + dt.timedelta(days=days))
            close, actual = price_on_or_after(r["ticker"], target_str)
            row[label] = round((close / r["anchor_close"] - 1) * 100, 1) if close else None
            time.sleep(scr.REQUEST_PAUSE)
        stock_rows.append(row)
        print(f"   {row['name']}({row['ticker']}): 3M={row['3M']}% 6M={row['6M']}% 12M={row['12M']}%")

    print(f"\n[백테스트] 요약 (상위 {len(stock_rows)}개 평균 vs 코스피, 기준일 {asof}):")
    for label, _ in horizons:
        vals = [r[label] for r in stock_rows if r[label] is not None]
        if not vals:
            continue
        avg = round(sum(vals) / len(vals), 1)
        excess = round(avg - kospi_returns[label], 1) if kospi_returns[label] is not None else None
        print(f"   {label}: 상위{top_n} 평균 {avg}% vs 코스피 {kospi_returns[label]}% "
              f"(초과수익 {excess:+.1f}%p)" if excess is not None else
              f"   {label}: 상위{top_n} 평균 {avg}% (코스피 데이터 없음)")

    print(f"\n[백테스트] 한계: 이건 단일 시점({asof}) 표본 하나뿐이라 통계적으로 유의하지 않다. "
          f"이 시기 전후 코스피가 전반적으로 반등/하락했는지(시장 국면)와 스크리너의 "
          f"종목 선별력을 이 결과 하나로는 구분할 수 없다 — 여러 시점(하락장 바닥/상승장 "
          f"중턱/횡보장 등 국면이 다른 4~5개 시점)에서 반복해야 스크리너 자체의 효과인지 "
          f"판단할 수 있다.")

    return top, stock_rows, kospi_returns


def run_backtest_from_db(anchor_str: str, top_n: int = 10):
    """run_backtest와 동일한 채점 로직이지만, OHLCV/펀더멘털/공매도/시가총액/매집
       데이터를 pykrx 대신 data/YYYYMMDD.db(하루 1파일)들에서 읽는다. 종목
       유니버스·업종 매핑·지수(코스피/코스닥/업종) 시계열은 db_reader.py 설명대로
       여전히 pykrx를 쓴다(요청받은 4개 테이블에 해당 안 되는 메타데이터라
       캐싱 이득이 적음). 순방향 수익률(3/6/12개월) 계산은 DB에 없는 미래
       구간이라 이 함수에서는 하지 않는다 — 필요하면 run_backtest의 그 부분을
       그대로 재사용."""
    anchor_target = dt.datetime.strptime(anchor_str, "%Y%m%d").date()
    asof = dbr.find_trading_day_on_or_before_db(anchor_target)
    if asof is None:
        raise RuntimeError(f"DB에 {anchor_str} 이전 데이터가 전혀 없습니다 — 백필이 필요합니다.")
    anchor_date = dt.datetime.strptime(asof, "%Y%m%d").date()
    print(f"[백테스트-DB] 요청 기준일 {anchor_str} → 실제 사용 영업일 {asof} (DB 기준)")

    print("1) 종목 유니버스 수집… (라이브 pykrx)")
    universe, ticker_market = scr.get_universe(asof)
    print(f"   {len(universe)}개 종목")

    print("1b) 업종지수 매핑 수집… (라이브 pykrx)")
    sector_map, sector_names = scr.get_sector_index_map(asof)
    print(f"   {len(sector_map)}개 종목이 업종지수에 매핑됨")

    print("2) OHLCV 스냅샷 로딩… (DB)")
    ohlcv_dates = scr.recent_business_dates(scr.OHLCV_LOOKBACK_DAYS, anchor_date)
    matrix = dbr.load_ohlcv_matrix_from_db(ohlcv_dates)
    latest_date = sorted(matrix.keys())[-1] if matrix else asof
    print(f"   {len(matrix)}개 영업일 확보, 최신={latest_date}")

    print("3) 펀더멘털 히스토리 로딩… (DB)")
    fund_hist = dbr.load_fundamental_history_from_db(
        scr.month_end_samples(scr.FUND_HISTORY_MONTHS, anchor_date))

    print("4) 공매도 3개월 최고/현재, 시가총액 로딩… (DB)")
    short_max = dbr.load_short_max_from_db(scr.weekly_samples(scr.SHORT_SAMPLE_WEEKS, anchor_date))
    short_cur = dbr.load_short_current_from_db(latest_date)
    market_cap = dbr.load_market_cap_from_db(latest_date)

    print("5) 매집(20일) 로딩… (DB)")
    accum_from = ohlcv_dates[-scr.ACCUM_WINDOW_DAYS] if len(ohlcv_dates) >= scr.ACCUM_WINDOW_DAYS else ohlcv_dates[0]
    accum_dates = dbr.date_range_inclusive(sorted(matrix.keys()), accum_from, latest_date)
    accum = dbr.load_accumulation_from_db(accum_dates)

    if ohlcv_dates[0] >= dbr.INDEX_COVERAGE_START:
        print("6) 지수 수익률(코스피·코스닥)… (DB: data/index_history.sqlite)")
        market_idx_by_date = dbr.load_market_index_from_db(ohlcv_dates[0], latest_date)
    else:
        print("6) 지수 수익률(코스피·코스닥)… (라이브 pykrx, DB 커버리지 밖)")
        market_idx_by_date: dict[str, dict[str, float]] = {}
        for mkt, code in scr.MARKET_INDEX_CODE.items():
            try:
                idx = stock.get_index_ohlcv(ohlcv_dates[0], latest_date, code)
                market_idx_by_date[mkt] = scr.index_close_by_date(idx)
            except Exception:
                market_idx_by_date[mkt] = {}

    print("6b) 업종지수 OHLCV 수집… (라이브 pykrx)")
    sector_codes_needed = set(sector_map.values())
    sector_idx_by_date = scr.collect_sector_index_ohlcv(sector_codes_needed, ohlcv_dates[0], latest_date)

    print("7) 채점(바닥 7개 신호만, 생존 게이트 포함)…")
    results = []
    for tkr, name in universe.items():
        dates, opens, highs, lows, closes, vols = scr.series_for_ticker(matrix, tkr)
        if len(closes) < 60 or len(vols) < 120:
            continue

        if scr.is_trading_halted(opens, highs, lows, closes, vols):
            continue
        last_close = closes[-1]
        avg_trading_value = median(vols[-20:]) * last_close
        if avg_trading_value < scr.MIN_AVG_TRADING_VALUE:
            continue
        cur_market_cap = market_cap.get(tkr, 0.0)
        if cur_market_cap < scr.MIN_MARKET_CAP:
            continue
        fh = fund_hist.get(tkr, [])
        cur_pbr = fh[0]["PBR"] if fh else 0.0
        if cur_pbr <= 0:
            continue

        split_suspected = scr.has_unadjusted_split_jump(closes)

        rec6to25 = median(vols[-25:-5])
        past120 = median(vols[-120:])
        ret60 = (closes[-1] / closes[-60]) - 1
        ret20_price = (closes[-1] / closes[-20]) - 1

        bench_series, bench_label = scr.resolve_benchmark_series(
            tkr, sector_map, sector_idx_by_date, market_idx_by_date, ticker_market)
        bench_c_latest = bench_series.get(latest_date)
        bench_c_60ago = bench_series.get(dates[-60])
        idx_ret_t = (bench_c_latest / bench_c_60ago - 1) if bench_c_latest and bench_c_60ago else 0.0

        pbr_series = [r["PBR"] for r in fh if r["PBR"] > 0]
        div_series = [r["DIV"] for r in fh if r["DIV"] > 0]
        cur_div = fh[0]["DIV"] if fh else 0.0
        cur_dps = fh[0]["DPS"] if fh else 0.0
        cur_eps = fh[0]["EPS"] if fh else 0.0
        bw_series = scr.bollinger_bandwidth_series(closes)
        float_mc = avg_trading_value * 50

        sector_name = sector_names.get(sector_map.get(tkr))
        pbr_caution_sector = scr.is_pbr_caution_sector(sector_name)
        capital_eroding = scr.had_progressive_capital_erosion(fh)
        bottom_weights = sg.BOTTOM_WEIGHTS
        if pbr_caution_sector:
            bottom_weights = dict(sg.BOTTOM_WEIGHTS)
            bottom_weights["pbr_low"] = bottom_weights["pbr_low"] / 2

        scores = {
            "volume_dryness": sg.score_volume_dryness(rec6to25, past120),
            "accumulation": sg.score_accumulation(accum.get(tkr, 0.0), float_mc, ret20_price * 100),
            "short_covering": sg.score_short_covering(short_cur.get(tkr, 0.0), short_max.get(tkr, 0.0)),
            "pbr_low": None if capital_eroding else sg.score_pbr_low(cur_pbr, pbr_series),
            "dividend_yield": sg.score_dividend_yield(cur_div, div_series, cur_dps, cur_eps, scr.had_dividend_cut(fh)),
            "relative_strength": None if split_suspected else sg.score_relative_strength(ret60, idx_ret_t),
            "volatility_squeeze": sg.score_volatility_squeeze(bw_series),
        }
        comp = sg.composite_score(scores, bottom_weights)
        if comp["composite"] is None or comp["composite"] < scr.BOTTOM_SCORE_THRESHOLD:
            continue

        results.append({
            "ticker": tkr, "name": name, "score": comp["composite"],
            "breakdown": comp["breakdown"], "anchor_close": last_close,
        })

    print(f"완료: {len(results)}개 채점 (기준일 {asof}, DB 소스)")
    results.sort(key=lambda x: -x["score"])
    top = results[:top_n]

    print(f"\n[백테스트-DB] 상위 {len(top)}개 종목 (기준일 {asof}):")
    for i, r in enumerate(top, 1):
        bd = ", ".join(f"{k}={v}" for k, v in r["breakdown"].items())
        print(f"  {i}. {r['name']}({r['ticker']}) 종합점수={r['score']} 종가={r['anchor_close']:.0f}")
        print(f"     breakdown: {bd}")

    return top, asof


if __name__ == "__main__":
    anchor_arg = sys.argv[1] if len(sys.argv) > 1 else "20221031"
    top_n_arg = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    source_arg = sys.argv[3] if len(sys.argv) > 3 else "live"
    if source_arg == "db":
        run_backtest_from_db(anchor_arg, top_n_arg)
    else:
        run_backtest(anchor_arg, top_n_arg)
