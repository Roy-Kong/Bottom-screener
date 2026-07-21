"""strategies/base_breakout/base_breakout.py — "바닥권 횡보 후 장대양봉 돌파" 전략.

[매수 조건 — 공통]
최근 SCORE_LOOKBACK_DAYS(60영업일) 안에 바닥스크리너 종합점수(signals.py
BOTTOM_WEIGHTS 7개 신호, 표준 가중치 — screener.py/strategy_backtest_2022.py와
동일 계산식)가 SCORE_THRESHOLD(50점) 이상이었던 적이 있는 종목 중, 오늘
장대양봉(전일종가 대비 +5%↑)과 대량거래(오늘 거래량이 오늘을 제외한 이전
10영업일 평균의 2.5배↑)가 동시에 발생하면 다음 영업일 시가에 매수한다.
하루 최대 MAX_NEW_BUYS_PER_DAY(5)종목까지, 후보가 더 많으면 거래량 배율
(오늘거래량/이전10일평균) 내림차순으로 5위까지만 채택.

[매도 조건 — 3가지 방식, 별도 백테스트로 각각 비교]
  A) 고정보유: 매수 후 60영업일 뒤 시가에 매도(도중 매도조건 없음)
  B) 추세추종: 종가가 20일 이동평균 아래로 마감하는 첫날, 다음 영업일 시가에 매도
  C) 트레일링스톱: 보유 중 최고가(매일 고가 기준 누적 최고) 대비 -10% 하락하면
     그 시점(그날, 목표가에 지정가 체결 가정)에 매도

[비용] 매수 시 0.33% 수수료(매도 수수료 없음), 슬리피지 0.2%는 매수·매도
양쪽 다 불리한 방향으로 적용(매수가는 0.2% 비싸게, 매도가는 0.2% 싸게 체결).

[시점 무결성] 모든 신호 계산은 해당 날짜 이전(포함) 데이터만 쓴다 — 이전에
portfolio_simulation.py에서 전체 기간 matrix를 그대로 넘겨 미래 데이터가
섞였던 룩어헤드 버그를 겪은 뒤로, 이 스크립트는 종목별 시계열을 항상 그
날짜까지만 윈도잉해서 만든다(_ticker_series_upto 참고).

[성능 설계] 전체 기간(2022-07~2026-06, 약 1000영업일)에 대해 매일 전종목
바닥점수를 다 계산하면 몇 시간이 걸린다(portfolio_simulation.py가 125일에
13~38분 걸렸던 것으로 추정). 그 대신:
  1) 저렴한 1차 필터(장대양봉+대량거래, 순수 OHLCV 산술)를 먼저 전종목·
     전체기간에 돌려서 후보를 추린다.
  2) 그 후보들에 대해서만 "최근 60일 중 한 번이라도 50점 이상"을 확인하는데,
     이때만 바닥점수(비용이 큰 계산)를 쓴다 — 그것도 (종목,날짜) 조합을
     메모이제이션해서 겹치는 60일 윈도우 사이의 중복 계산을 피한다.
OHLCV/공매도/매집/시가총액은 전체 기간을 한 번에 벌크 프리로드해서, 필요한
때마다 로컬 dict 조회만 하고 DB 파일을 반복해서 열지 않는다.

strategies/base_breakout/에 전략 코드, backtests/base_breakout/에 결과를
각각 둔다(저장소는 하나, 폴더로만 구분). screener.py/signals.py/db_reader.py/
index_db.py/snapshot_cache.py는 함수만 가져다 쓰고 전혀 수정하지 않는다.

사용법:
    python strategies/base_breakout/base_breakout.py --start 20220701 --end 20231229 \\
        --exit A --out backtests/base_breakout/train_A.csv
"""
from __future__ import annotations
import argparse
import csv
import sqlite3
import sys
import time
import datetime as dt
from pathlib import Path
from statistics import median

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import screener as scr
import signals as sg
import db_reader as dbr
import db
import snapshot_cache

# ---------------- 전략 파라미터 ----------------
BREAKOUT_RETURN_PCT = 0.05      # 장대양봉: 전일종가 대비 +5%↑
VOLUME_MULTIPLE = 2.5           # 대량거래: 오늘 거래량 / 이전10일평균 >= 2.5
VOLUME_LOOKBACK_DAYS = 10       # 대량거래 판정용 이전 영업일 수(오늘 제외)
SCORE_LOOKBACK_DAYS = 60        # "최근 60영업일 중 한 번이라도"의 60
SCORE_THRESHOLD = 50.0          # 바닥스크리너 종합점수 컷라인

START_CAPITAL = 100_000_000.0
MAX_POSITION_PCT = 0.20         # 종목당 최대 포트폴리오 20%
MAX_NEW_BUYS_PER_DAY = 5        # 하루 최대 신규매수 종목 수
BUY_FEE_PCT = 0.0033            # 매수 시에만 (매도 수수료 없음)
SLIPPAGE_PCT = 0.002            # 매수·매도 양쪽 다, 불리한 방향

MAX_HOLD_TRADING_DAYS_A = 60    # 방식 A: 고정보유 영업일수
MA_WINDOW_B = 20                # 방식 B: 이동평균 기간
TRAILING_STOP_PCT_C = -0.10     # 방식 C: 최고가 대비 낙폭

DB_COVERAGE_START = "20220103"


# ==================== 사전 로딩 ====================

def load_all_month_snapshots() -> dict[str, dict]:
    """cache/anchor_snapshots/에 있는 모든 월초 앵커를 {"YYYY-MM": snapshot}로 로드.
       strategy_backtest_2022.py가 2022-07~2026-06 48개월치를 이미 만들어놓음."""
    out = {}
    for p in sorted(snapshot_cache.ANCHOR_DIR.glob("*.json")):
        a = p.stem
        snap = snapshot_cache.load_anchor_snapshot(a)
        ym = f"{a[:4]}-{a[4:6]}"
        out[ym] = snap
    return out


def governing_month(date_str: str) -> str:
    return f"{date_str[:4]}-{date_str[4:6]}"


def preload_all(start: str, end: str, month_snaps: dict[str, dict]) -> dict:
    """전체 구간(신호 계산에 필요한 lookback 버퍼 포함)을 한 번에 벌크 로드.
       이후 스코어링은 전부 이 dict들에서 순수 in-memory 조회만 한다(DB 재오픈 없음)."""
    anchor0 = dt.datetime.strptime(start, "%Y%m%d").date()
    ohlcv_lookback_start = scr.recent_business_dates(scr.OHLCV_LOOKBACK_DAYS, anchor0)[0]
    # 60일-이력 체크가 훨씬 더 과거 날짜의 자체 130일 lookback까지 필요로 하므로
    # (그 날짜 자체가 DB_COVERAGE_START 근처면 자연히 데이터 부족으로 생존게이트에서
    # 걸러짐 — 새로 처리할 필요 없음, screen_and_score의 len(closes)>=60 게이트와 동일 원칙),
    # 최대한 당겨서 DB_COVERAGE_START까지로 lookback_start를 잡는다.
    lookback_start = max(DB_COVERAGE_START, ohlcv_lookback_start)

    all_calendar_dates = scr.recent_business_dates(2000, dt.datetime.strptime(end, "%Y%m%d").date())
    needed_dates = sorted(d for d in all_calendar_dates if lookback_start <= d <= end)

    print(f"  OHLCV/공매도/시가총액/매집 로딩 ({lookback_start}~{end}, {len(needed_dates)}일 요청)…")
    matrix, short_by_date, market_cap_by_date, accum_by_date = _bulk_load_day_files(needed_dates)
    print(f"    {len(matrix)}개 실제 거래일 확보")

    # 60일-이력 체크가 day보다 최대 SCORE_LOOKBACK_DAYS(60영업일, 약 3개월) 전
    # 달까지도 건드릴 수 있어서, 실제 요청 구간의 월 목록보다 여유를 좀 둔다.
    relevant_months = sorted(m for m in month_snaps if f"{lookback_start[:4]}-{lookback_start[4:6]}" <= m <= f"{end[:4]}-{end[4:6]}")

    print("  코스피·코스닥·업종지수 로딩…")
    market_idx_by_date = dbr.load_market_index_from_db(lookback_start, end)
    sector_codes_needed: set[str] = set()
    for ym in relevant_months:
        sector_codes_needed.update(month_snaps[ym]["sector_map"].values())
    sector_idx_by_date = dbr.load_sector_index_from_db(sector_codes_needed, lookback_start, end)

    print(f"  펀더멘털 5년 밴드(월 단위, {len(relevant_months)}개월)…")
    fund_hist_by_month = _bulk_load_fundamental_by_month(relevant_months)

    return {
        "matrix": matrix, "short_by_date": short_by_date, "market_cap_by_date": market_cap_by_date,
        "accum_by_date": accum_by_date, "market_idx_by_date": market_idx_by_date,
        "sector_idx_by_date": sector_idx_by_date, "fund_hist_by_month": fund_hist_by_month,
    }


def _bulk_load_fundamental_by_month(relevant_months: list[str]) -> dict[str, dict[str, list[dict]]]:
    """{"YYYY-MM": {ticker: [{date,PBR,DIV,DPS,EPS,BPS}, ...]}} — db_reader.
       load_fundamental_history_from_db(month_end_samples(60,anchor))를 월별로
       48번 따로 부르면, 인접한 달끼리 60개월 표본이 59개월 겹쳐서 같은 날짜
       파일을 최대 48번씩 다시 연다(관측: 151초). 여기서는 전체 필요한 월말
       날짜의 합집합만 한 번씩 읽고, 각 월은 그 공유 풀에서 자기 표본 목록
       순서대로 슬라이싱한다."""
    samples_by_month: dict[str, list[str]] = {}
    all_dates: set[str] = set()
    for ym in relevant_months:
        y, m = int(ym[:4]), int(ym[5:7])
        samples = scr.month_end_samples(scr.FUND_HISTORY_MONTHS, dt.date(y, m, 1))
        samples_by_month[ym] = samples
        all_dates.update(samples)

    raw_by_date: dict[str, dict[str, dict]] = {}
    for d in sorted(all_dates):
        path = db.daily_db_path(d)
        if not path.exists():
            raw_by_date[d] = {}
            continue
        try:
            conn = sqlite3.connect(str(path))
        except sqlite3.DatabaseError:
            raw_by_date[d] = {}
            continue
        try:
            rows = conn.execute("SELECT ticker, pbr, div, dps, eps, bps FROM daily_fundamental").fetchall()
        except sqlite3.OperationalError:
            rows = []
        except sqlite3.DatabaseError:
            rows = []
        finally:
            conn.close()
        raw_by_date[d] = {
            tkr: {"date": d, "PBR": pbr or 0.0, "DIV": div or 0.0, "DPS": dps or 0.0, "EPS": eps or 0.0, "BPS": bps or 0.0}
            for tkr, pbr, div, dps, eps, bps in rows
        }

    out: dict[str, dict[str, list[dict]]] = {}
    for ym, samples in samples_by_month.items():
        by_ticker: dict[str, list[dict]] = {}
        for d in samples:  # 최신→과거 순서 유지(had_dividend_cut 등이 이 순서에 의존)
            for tkr, entry in raw_by_date.get(d, {}).items():
                by_ticker.setdefault(tkr, []).append(entry)
        out[ym] = by_ticker
    return out


def _bulk_load_day_files(dates: list[str]) -> tuple[dict, dict, dict, dict]:
    """날짜 파일(data/YYYYMMDD.db) 하나당 딱 한 번만 연결해서 daily_prices
       (open/high/low/close/volume/market_cap)·daily_short·daily_investor_flow를
       전부 한 번에 읽는다. db_reader.py의 개별 함수(load_ohlcv_matrix_from_db_full/
       load_short_current_from_db/load_market_cap_from_db/load_accumulation_from_db)를
       그대로 여러 번 호출하면 같은 파일을 3~4번씩 다시 여는데, 이 환경(로컬 디스크
       I/O가 유독 느림 — 이전 세션에서 git 관련해서도 확인된 문제)에서는 파일 열기
       자체가 병목이라(141일에 180초) 파일당 1커넥션으로 합쳐서 4배 가까이 줄인다.
       ±30% 상하한가 필터는 load_ohlcv_matrix_from_db_full과 동일하게 재적용한다."""
    matrix: dict[str, dict[str, tuple]] = {}
    short_by_date: dict[str, dict[str, float]] = {}
    market_cap_by_date: dict[str, dict[str, float]] = {}
    accum_by_date: dict[str, dict[str, float]] = {}
    last_close: dict[str, float] = {}

    for d in sorted(dates):
        path = db.daily_db_path(d)
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(str(path))
        except sqlite3.DatabaseError:
            continue
        try:
            try:
                price_rows = conn.execute(
                    "SELECT ticker, open, high, low, close, volume, market_cap FROM daily_prices").fetchall()
            except sqlite3.OperationalError:
                price_rows = []
            try:
                short_rows = conn.execute("SELECT ticker, short_ratio FROM daily_short").fetchall()
            except sqlite3.OperationalError:
                short_rows = []
            try:
                flow_rows = conn.execute(
                    "SELECT ticker, inst_foreign_net_buy FROM daily_investor_flow").fetchall()
            except sqlite3.OperationalError:
                flow_rows = []
        except sqlite3.DatabaseError:
            conn.close()
            continue
        finally:
            conn.close()

        day_ohlcv: dict[str, tuple] = {}
        day_mc: dict[str, float] = {}
        for tkr, o, h, l, close, vol, mc in price_rows:
            if close is None or close <= 0:
                continue
            prev = last_close.get(tkr)
            if prev is not None and prev > 0:
                ratio = close / prev
                if ratio > scr.MAX_DAILY_MOVE_RATIO or ratio < 1 / scr.MAX_DAILY_MOVE_RATIO:
                    continue
            day_ohlcv[tkr] = (o or 0.0, h or 0.0, l or 0.0, close, vol or 0.0)
            last_close[tkr] = close
            if mc is not None:
                day_mc[tkr] = mc
        if day_ohlcv:
            matrix[d] = day_ohlcv
        market_cap_by_date[d] = day_mc
        short_by_date[d] = {tkr: r for tkr, r in short_rows if r is not None}
        accum_by_date[d] = {tkr: v for tkr, v in flow_rows if v is not None}

    return matrix, short_by_date, market_cap_by_date, accum_by_date


# ==================== 종목별 시계열(윈도잉 — 룩어헤드 방지) ====================

def _ticker_series_upto(matrix: dict, tkr: str, day: str, lookback_dates: list[str]) -> tuple:
    """day까지(포함)만 잘라낸 종목 시계열. lookback_dates는 scr.recent_business_dates로
       만든 후보 날짜 목록(주말 제외 달력, 실제 거래일은 matrix에 있는 것만)."""
    dates, opens, highs, lows, closes, vols = [], [], [], [], [], []
    for d in lookback_dates:
        if d > day:
            break
        row = matrix.get(d, {}).get(tkr)
        if row is None:
            continue
        o, h, l, c, v = row
        dates.append(d); opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)
    return dates, opens, highs, lows, closes, vols


# ==================== 단일 종목·단일 날짜 바닥 종합점수 ====================

def ticker_bottom_score(tkr: str, day: str, pre: dict, month_snaps: dict[str, dict],
                        score_cache: dict[tuple[str, str], float | None]) -> float | None:
    """day 시점 기준 종목 tkr의 바닥 종합점수(signals.BOTTOM_WEIGHTS 7개 신호,
       표준 가중치). 게이트 미통과·데이터 부족이면 None. (ticker,day) 메모이제이션."""
    key = (tkr, day)
    if key in score_cache:
        return score_cache[key]

    ym = governing_month(day)
    snap = month_snaps.get(ym)
    if snap is None:
        score_cache[key] = None
        return None
    universe = snap["universe"]
    if tkr not in universe:
        score_cache[key] = None
        return None
    sector_map, sector_names, ticker_market = snap["sector_map"], snap["sector_names"], snap["ticker_market"]

    day_date = dt.datetime.strptime(day, "%Y%m%d").date()
    ohlcv_dates = scr.recent_business_dates(scr.OHLCV_LOOKBACK_DAYS, day_date)
    dates, opens, highs, lows, closes, vols = _ticker_series_upto(pre["matrix"], tkr, day, ohlcv_dates)
    if len(closes) < 60 or len(vols) < 120:
        score_cache[key] = None
        return None
    if scr.is_trading_halted(opens, highs, lows, closes, vols):
        score_cache[key] = None
        return None

    last_close = closes[-1]
    avg_trading_value = median(vols[-20:]) * last_close
    if avg_trading_value < scr.MIN_AVG_TRADING_VALUE:
        score_cache[key] = None
        return None
    cur_market_cap = pre["market_cap_by_date"].get(day, {}).get(tkr, 0.0)
    if cur_market_cap < scr.MIN_MARKET_CAP:
        score_cache[key] = None
        return None

    fh_all = pre["fund_hist_by_month"].get(ym, {}).get(tkr, [])
    cur_pbr = fh_all[0]["PBR"] if fh_all else 0.0
    if cur_pbr <= 0:
        score_cache[key] = None
        return None

    split_suspected = scr.has_unadjusted_split_jump(closes)
    rec6to25 = median(vols[-25:-5])
    past120 = median(vols[-120:])
    ret60 = (closes[-1] / closes[-60]) - 1
    ret20_price = (closes[-1] / closes[-20]) - 1

    bench_series, _ = scr.resolve_benchmark_series(tkr, sector_map, pre["sector_idx_by_date"],
                                                    pre["market_idx_by_date"], ticker_market)
    bench_c_latest = bench_series.get(dates[-1])
    bench_c_60ago = bench_series.get(dates[-60])
    idx_ret_t = (bench_c_latest / bench_c_60ago - 1) if bench_c_latest and bench_c_60ago else 0.0

    pbr_series = [r["PBR"] for r in fh_all if r["PBR"] > 0]
    div_series = [r["DIV"] for r in fh_all if r["DIV"] > 0]
    cur_div = fh_all[0]["DIV"] if fh_all else 0.0
    cur_dps = fh_all[0]["DPS"] if fh_all else 0.0
    cur_eps = fh_all[0]["EPS"] if fh_all else 0.0
    bw_series = scr.bollinger_bandwidth_series(closes)
    float_mc = avg_trading_value * 50
    sector_name = sector_names.get(sector_map.get(tkr))
    pbr_caution_sector = scr.is_pbr_caution_sector(sector_name)
    capital_eroding = scr.had_progressive_capital_erosion(fh_all)

    short_max = _short_max_upto(pre["short_by_date"], tkr, day_date)
    short_cur = pre["short_by_date"].get(dates[-1], {}).get(tkr, 0.0)
    accum_20d = _sum_window(pre["accum_by_date"], tkr, dates, 20)

    bottom_weights = dict(sg.BOTTOM_WEIGHTS)
    if pbr_caution_sector:
        bottom_weights["pbr_low"] = bottom_weights["pbr_low"] / 2

    bottom_scores = {
        "volume_dryness": sg.score_volume_dryness(rec6to25, past120),
        "accumulation": sg.score_accumulation(accum_20d, float_mc, ret20_price * 100),
        "short_covering": sg.score_short_covering(short_cur, short_max),
        "pbr_low": None if capital_eroding else sg.score_pbr_low(cur_pbr, pbr_series),
        "dividend_yield": sg.score_dividend_yield(cur_div, div_series, cur_dps, cur_eps, scr.had_dividend_cut(fh_all)),
        "relative_strength": None if split_suspected else sg.score_relative_strength(ret60, idx_ret_t),
        "volatility_squeeze": sg.score_volatility_squeeze(bw_series),
    }
    comp = sg.composite_score(bottom_scores, bottom_weights)
    score_cache[key] = comp["composite"]
    return comp["composite"]


def _short_max_upto(short_by_date: dict, tkr: str, day_date: dt.date) -> float:
    samples = scr.weekly_samples(scr.SHORT_SAMPLE_WEEKS, day_date)
    best = 0.0
    for d in samples:
        v = short_by_date.get(d, {}).get(tkr)
        if v is not None and v > best:
            best = v
    return best


def _sum_window(by_date: dict, tkr: str, dates_sorted: list[str], n: int) -> float:
    window = dates_sorted[-n:] if len(dates_sorted) >= n else dates_sorted
    return sum(by_date.get(d, {}).get(tkr, 0.0) or 0.0 for d in window)


def had_score_ge_threshold_in_lookback(tkr: str, day: str, pre: dict, month_snaps: dict[str, dict],
                                       score_cache: dict) -> bool:
    """day를 포함해 최근 SCORE_LOOKBACK_DAYS(60)영업일 중 한 번이라도 바닥점수가
       SCORE_THRESHOLD(50) 이상이었는지. 최근 날짜부터 역순으로 확인해서 찾으면
       바로 멈춘다(조기 종료로 평균 계산량 절감)."""
    day_date = dt.datetime.strptime(day, "%Y%m%d").date()
    check_dates = scr.recent_business_dates(SCORE_LOOKBACK_DAYS, day_date)
    for d in reversed(check_dates):  # 최신부터
        s = ticker_bottom_score(tkr, d, pre, month_snaps, score_cache)
        if s is not None and s >= SCORE_THRESHOLD:
            return True
    return False


# ==================== 1차 저비용 필터: 장대양봉 + 대량거래 ====================

def scan_breakout_candidates(trading_days: list[str], pre: dict, month_snaps: dict[str, dict]) -> dict[str, list[dict]]:
    """{date: [{"ticker","name","volume_ratio"}, ...]} — 순수 OHLCV 산술만 쓰는
       저렴한 1차 스캔(그날 유니버스 종목 전체 대상, DB 재오픈 없이 preload된
       matrix만 조회). 60일 이력 체크는 여기서 안 함(2단계에서 후보만 확인)."""
    matrix = pre["matrix"]
    out: dict[str, list[dict]] = {}
    for day in trading_days:
        ym = governing_month(day)
        snap = month_snaps.get(ym)
        if snap is None:
            continue
        universe = snap["universe"]
        day_row = matrix.get(day, {})

        # 날짜 계산은 종목과 무관하므로 하루에 한 번만(종목 루프 밖에서) 수행
        hist_dates = scr.recent_business_dates(VOLUME_LOOKBACK_DAYS + 1, dt.datetime.strptime(day, "%Y%m%d").date())
        hist_dates = [d for d in hist_dates if d < day]  # 오늘 제외 이전 영업일들
        if len(hist_dates) < VOLUME_LOOKBACK_DAYS:
            continue
        hist_dates = hist_dates[-VOLUME_LOOKBACK_DAYS:]
        prev_close_date = hist_dates[-1]
        prev_day_row = matrix.get(prev_close_date, {})

        candidates = []
        for tkr in day_row:
            if tkr not in universe:
                continue
            prior_vols = [matrix[d][tkr][4] for d in hist_dates if d in matrix and tkr in matrix[d]]
            if len(prior_vols) < VOLUME_LOOKBACK_DAYS:
                continue
            prior_avg_vol = sum(prior_vols) / len(prior_vols)
            if prior_avg_vol <= 0:
                continue
            prev_row = prev_day_row.get(tkr)
            if prev_row is None or prev_row[3] <= 0:
                continue
            prev_close = prev_row[3]
            today_open, today_high, today_low, today_close, today_vol = day_row[tkr]
            ret = today_close / prev_close - 1
            vol_ratio = today_vol / prior_avg_vol
            if ret >= BREAKOUT_RETURN_PCT and vol_ratio >= VOLUME_MULTIPLE:
                candidates.append({"ticker": tkr, "name": universe.get(tkr, tkr), "volume_ratio": vol_ratio})
        if candidates:
            out[day] = candidates
    return out


# ==================== 2차 필터 + 매수신호 확정 ====================

def build_buy_signals(trading_days: list[str], pre: dict, month_snaps: dict[str, dict],
                      score_cache: dict) -> dict[str, list[dict]]:
    """{date: [{"ticker","name","volume_ratio"}, ...]} — 1차(장대양봉+대량거래) +
       2차(최근 60일 중 바닥점수 50점 이상이었던 적 있음) 둘 다 통과한 최종 후보.
       하루 MAX_NEW_BUYS_PER_DAY(5)개까지, 초과하면 거래량배율 내림차순 상위만."""
    raw_candidates = scan_breakout_candidates(trading_days, pre, month_snaps)
    signals: dict[str, list[dict]] = {}
    for day, clist in raw_candidates.items():
        qualified = [c for c in clist
                     if had_score_ge_threshold_in_lookback(c["ticker"], day, pre, month_snaps, score_cache)]
        if not qualified:
            continue
        qualified.sort(key=lambda c: -c["volume_ratio"])
        signals[day] = qualified[:MAX_NEW_BUYS_PER_DAY]
    return signals


# ==================== 포트폴리오 시뮬레이션 (매도방식 A/B/C 공통 엔진) ====================

def _ma20_upto(matrix: dict, tkr: str, day: str, trading_days_sorted: list[str], idx_of: dict[str, int]) -> float | None:
    """day를 포함한 최근 20거래일 종가 이동평균(그 종목 데이터가 있는 날짜만 카운트)."""
    i = idx_of.get(day)
    if i is None:
        return None
    closes = []
    j = i
    while j >= 0 and len(closes) < MA_WINDOW_B:
        d = trading_days_sorted[j]
        row = matrix.get(d, {}).get(tkr)
        if row is not None:
            closes.append(row[3])
        j -= 1
    if len(closes) < MA_WINDOW_B:
        return None
    return sum(closes) / len(closes)


def simulate(exit_method: str, buy_signals: dict[str, list[dict]], trading_days: list[str], pre: dict) -> tuple[list[dict], list[tuple]]:
    """exit_method: "A"(고정60일) / "B"(20일선 이탈) / "C"(고점대비-10% 트레일링).
       매수 실행·수수료·슬리피지·포지션 사이징 로직은 세 방식이 동일하고 매도
       판정만 다르다."""
    matrix = pre["matrix"]
    idx_of = {d: i for i, d in enumerate(trading_days)}

    cash = START_CAPITAL
    holdings: dict[str, dict] = {}
    pending_buys: dict[str, list[dict]] = {}  # buy_date -> [{ticker,name,volume_ratio}]
    trade_log: list[dict] = []
    equity_curve: list[tuple[str, float]] = []

    def portfolio_value(day: str) -> float:
        v = cash
        for tkr, pos in holdings.items():
            row = matrix.get(day, {}).get(tkr)
            price = row[3] if row else pos["buy_price"]
            v += pos["shares"] * price
        return v

    for i, day in enumerate(trading_days):
        # 1) 전날 신호로 정해진 후보를 오늘 시가에 매수 (슬리피지 불리하게, 수수료 매수만)
        for cand in pending_buys.pop(day, []):
            tkr = cand["ticker"]
            if tkr in holdings:
                continue
            row = matrix.get(day, {}).get(tkr)
            if row is None or row[0] <= 0:
                continue
            raw_open = row[0]
            fill_price = raw_open * (1 + SLIPPAGE_PCT)
            base_value = portfolio_value(day)
            invest = base_value * MAX_POSITION_PCT
            fee = invest * BUY_FEE_PCT
            if invest + fee > cash:
                continue  # 현금 부족 — 스킵(이게 사실상 동시보유 종목수를 자연스럽게 제한)
            shares = invest / fill_price
            cash -= (invest + fee)
            holdings[tkr] = {"buy_date": day, "buy_price": fill_price, "shares": shares,
                              "name": cand["name"], "volume_ratio": cand["volume_ratio"],
                              "peak_high": fill_price}

        # 2) 매도 판정 (방식별)
        for tkr in sorted(list(holdings.keys())):
            pos = holdings[tkr]
            row = matrix.get(day, {}).get(tkr)
            if row is None:
                continue
            today_open, today_high, today_low, today_close, _ = row

            sell_today = False
            sell_price = None
            reason = None

            if exit_method == "A":
                buy_idx = idx_of[pos["buy_date"]]
                if i - buy_idx >= MAX_HOLD_TRADING_DAYS_A:
                    sell_today, sell_price, reason = True, today_open, "고정60일만기"

            elif exit_method == "B":
                if pos.get("ma_break_signal_day") is not None:
                    # 이전 날짜에 이탈 신호가 떴으면 오늘 시가에 매도(다음 거래일 시가 규칙)
                    sell_today, sell_price, reason = True, today_open, "20일선이탈"
                else:
                    ma20 = _ma20_upto(matrix, tkr, day, trading_days, idx_of)
                    if ma20 is not None and today_close < ma20:
                        pos["ma_break_signal_day"] = day  # 오늘 발생 → 다음 거래일 시가 매도로 예약

            elif exit_method == "C":
                stop_price = pos["peak_high"] * (1 + TRAILING_STOP_PCT_C)
                if today_low <= stop_price:
                    sell_today, sell_price, reason = True, stop_price, "트레일링스톱-10%"
                else:
                    pos["peak_high"] = max(pos["peak_high"], today_high)

            if sell_today:
                fill_price = sell_price * (1 - SLIPPAGE_PCT)
                proceeds = pos["shares"] * fill_price
                cash += proceeds
                buy_idx = idx_of[pos["buy_date"]]
                trade_log.append({
                    "ticker": tkr, "name": pos["name"], "volume_ratio": round(pos["volume_ratio"], 2),
                    "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 2),
                    "sell_date": day, "sell_price": round(fill_price, 2), "sell_reason": reason,
                    "holding_trading_days": i - buy_idx,
                    "return_pct": round((fill_price / pos["buy_price"] - 1) * 100, 2),
                    "open_position": False,
                })
                del holdings[tkr]

        # 3) 오늘 매수신호 확정 → 다음 거래일 시가 매수용으로 큐잉
        today_signals = buy_signals.get(day)
        if today_signals and i + 1 < len(trading_days):
            next_day = trading_days[i + 1]
            pending_buys.setdefault(next_day, []).extend(today_signals)

        equity_curve.append((day, portfolio_value(day)))

    # ---- 기말 미청산 포지션 마감 평가 ----
    last_day = trading_days[-1]
    for tkr, pos in holdings.items():
        row = matrix.get(last_day, {}).get(tkr)
        last_close = row[3] if row else pos["buy_price"]
        buy_idx = idx_of[pos["buy_date"]]
        trade_log.append({
            "ticker": tkr, "name": pos["name"], "volume_ratio": round(pos["volume_ratio"], 2),
            "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 2),
            "sell_date": None, "sell_price": None, "sell_reason": None,
            "holding_trading_days": len(trading_days) - 1 - buy_idx,
            "return_pct": round((last_close / pos["buy_price"] - 1) * 100, 2),
            "open_position": True,
        })

    return trade_log, equity_curve


# ==================== 출력 ====================

def write_csv(rows: list[dict], out_path: str) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        print(f"    저장할 행이 없습니다: {out_path}")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"    CSV 저장: {out_path} ({len(rows)}행)")


def compute_stats(trade_log: list[dict], equity_curve: list[tuple]) -> dict:
    closed = [t for t in trade_log if not t["open_position"]]
    open_pos = [t for t in trade_log if t["open_position"]]
    wins = [t for t in closed if t["return_pct"] > 0]
    hold_days = [t["holding_trading_days"] for t in closed]

    max_dd = 0.0
    if equity_curve:
        peak = equity_curve[0][1]
        for _, v in equity_curve:
            peak = max(peak, v)
            dd = (v / peak - 1) * 100
            max_dd = min(max_dd, dd)

    final_value = equity_curve[-1][1] if equity_curve else START_CAPITAL
    return {
        "n_trades": len(trade_log), "n_closed": len(closed), "n_open": len(open_pos),
        "avg_holding_days": round(sum(hold_days) / len(hold_days), 1) if hold_days else None,
        "avg_return_pct": round(sum(t["return_pct"] for t in closed) / len(closed), 2) if closed else None,
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "max_drawdown_pct": round(max_dd, 2),
        "final_value": round(final_value, 0),
        "total_return_pct": round((final_value / START_CAPITAL - 1) * 100, 2),
    }


# ==================== 실행 ====================

def run(start: str, end: str, exit_methods: list[str], out_dir: str, label: str) -> dict[str, dict]:
    t0 = time.time()
    print(f"[base_breakout] {start}~{end}, 매도방식 {exit_methods}")

    print("사전 로딩…")
    month_snaps = load_all_month_snapshots()
    pre = preload_all(start, end, month_snaps)
    trading_days = sorted(d for d in pre["matrix"].keys() if start <= d <= end)
    print(f"  대상 거래일: {len(trading_days)}일 ({trading_days[0]}~{trading_days[-1]})  "
          f"[소요 {(time.time()-t0)/60:.1f}분]")

    print("매수신호 스캔(1차 장대양봉+대량거래 → 2차 60일 이력)…")
    t1 = time.time()
    score_cache: dict = {}
    buy_signals = build_buy_signals(trading_days, pre, month_snaps, score_cache)
    n_signal_days = len(buy_signals)
    n_signals = sum(len(v) for v in buy_signals.values())
    print(f"  매수신호: {n_signal_days}일에 총 {n_signals}건 (하루 최대 {MAX_NEW_BUYS_PER_DAY}개 캡 적용 후)  "
          f"[소요 {(time.time()-t1)/60:.1f}분, 점수캐시 {len(score_cache)}개]")

    all_stats = {}
    for method in exit_methods:
        print(f"매도방식 {method} 시뮬레이션…")
        t2 = time.time()
        trade_log, equity_curve = simulate(method, buy_signals, trading_days, pre)
        stats = compute_stats(trade_log, equity_curve)
        all_stats[method] = stats
        out_path = f"{out_dir}/{label}_{method}.csv"
        write_csv(trade_log, out_path)
        print(f"    거래 {stats['n_trades']}건(청산 {stats['n_closed']}, 미청산 {stats['n_open']}), "
              f"평균보유 {stats['avg_holding_days']}일, 평균수익률 {stats['avg_return_pct']}%, "
              f"승률 {stats['win_rate_pct']}%, MDD {stats['max_drawdown_pct']}%, "
              f"최종평가액 {stats['final_value']:,.0f}원({stats['total_return_pct']:+.2f}%)  "
              f"[소요 {(time.time()-t2)/60:.1f}분]")

    print(f"\n[base_breakout] 전체 소요시간: {(time.time()-t0)/60:.1f}분")
    return all_stats


def run_train_val(train_start: str, train_end: str, val_start: str, val_end: str,
                  exit_methods: list[str], out_dir: str) -> dict[str, dict[str, dict]]:
    """훈련·검증 구간을 한 번의 사전로딩+매수신호 스캔으로 처리하고(검증기간 초반
       60일 이력 체크가 훈련기간 후반부 점수캐시를 재사용할 수 있어 효율적),
       각 구간은 매매 시뮬레이션에서만 독립적으로 분리한다(각자 1억원부터 새로
       시작 — 훈련 결과 자본이 검증으로 이월되면 진짜 아웃오브샘플 검증이 아니게
       됨). {"train":{"A":stats,...}, "val":{...}} 반환."""
    t0 = time.time()
    print(f"[base_breakout] 훈련 {train_start}~{train_end}, 검증 {val_start}~{val_end}, 매도방식 {exit_methods}")

    print("사전 로딩(훈련+검증 전체 구간 한 번에)…")
    month_snaps = load_all_month_snapshots()
    pre = preload_all(train_start, val_end, month_snaps)
    all_trading_days = sorted(pre["matrix"].keys())
    print(f"  [소요 {(time.time()-t0)/60:.1f}분]")

    print("매수신호 스캔(1차 장대양봉+대량거래 → 2차 60일 이력, 전체 구간 한 번에)…")
    t1 = time.time()
    score_cache: dict = {}
    scan_days = [d for d in all_trading_days if train_start <= d <= val_end]
    buy_signals = build_buy_signals(scan_days, pre, month_snaps, score_cache)
    print(f"  [소요 {(time.time()-t1)/60:.1f}분, 점수캐시 {len(score_cache)}개]")

    results: dict[str, dict[str, dict]] = {"train": {}, "val": {}}
    for label, (s, e) in (("train", (train_start, train_end)), ("val", (val_start, val_end))):
        period_days = [d for d in all_trading_days if s <= d <= e]
        period_signals = {d: v for d, v in buy_signals.items() if s <= d <= e}
        n_sig = sum(len(v) for v in period_signals.values())
        print(f"\n[{label}] {s}~{e}: {len(period_days)}거래일, 매수신호 {len(period_signals)}일 {n_sig}건")
        for method in exit_methods:
            t2 = time.time()
            trade_log, equity_curve = simulate(method, period_signals, period_days, pre)
            stats = compute_stats(trade_log, equity_curve)
            results[label][method] = stats
            out_path = f"{out_dir}/{label}_{method}.csv"
            write_csv(trade_log, out_path)
            print(f"  방식{method}: 거래 {stats['n_trades']}건(청산 {stats['n_closed']}, 미청산 {stats['n_open']}), "
                  f"평균보유 {stats['avg_holding_days']}일, 평균수익률 {stats['avg_return_pct']}%, "
                  f"승률 {stats['win_rate_pct']}%, MDD {stats['max_drawdown_pct']}%, "
                  f"최종평가액 {stats['final_value']:,.0f}원({stats['total_return_pct']:+.2f}%)  "
                  f"[소요 {(time.time()-t2)/60:.1f}분]")

    print(f"\n[base_breakout] 전체 소요시간: {(time.time()-t0)/60:.1f}분")
    return results


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="바닥권 횡보 후 장대양봉 돌파 전략 백테스트")
    p.add_argument("--train-start", default="20220701")
    p.add_argument("--train-end", default="20231229")
    p.add_argument("--val-start", default="20240102")
    p.add_argument("--val-end", default="20260630")
    p.add_argument("--exit", default="A,B,C", help="콤마구분 매도방식(A/B/C 중), 기본 전부")
    p.add_argument("--out-dir", default="backtests/base_breakout")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    methods = [m.strip().upper() for m in args.exit.split(",") if m.strip()]
    run_train_val(args.train_start, args.train_end, args.val_start, args.val_end, methods, args.out_dir)
