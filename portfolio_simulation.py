"""portfolio_simulation.py — 2022년 하반기(7월~12월) 일별 스크리닝 포트폴리오
시뮬레이션. strategy_backtest_2022.py(월 1회 앵커 백테스트)와는 성격이 완전히
다른 로직(매일 스크리닝 + 최대 10종목 동시 보유 + ±5% 익절/손절)이라 별도
스크립트로 만들었다 — strategy_backtest_2022.py/screener.py/signals.py/
db_reader.py는 이 스크립트가 함수만 가져다 쓰고 전혀 수정하지 않는다.

[규칙 요약]
- 시작자본 1억원, 최대 10종목, 슬롯당 "그 시점 포트폴리오 평가액의 10%"
- 첫날(2022-07-01) 예외: 그날 시가 기준으로 그날 스크리닝 통과 상위 10개를
  바로 매수(이후 정규 슬롯 보충은 전부 "오늘 스크리닝 → 다음 거래일 시가
  매수" 방식이라 하루 시차가 있지만, 최초 매수만 부트스트랩으로 당일 처리
  — 사용자 지시 원문 "그날 첫 영업일 시가 기준... 매수"를 그대로 따름).
- 보유 종목: 그날 고가가 매수가 대비 +5% 이상 또는 저가가 -5% 이하면 매도.
  둘 다 해당하면 보수적으로 -5%(손절) 우선 체결. 매도가는 정확히 목표가(±5%)
  — 갭으로 더 크게 움직였어도 그 목표가에 체결됐다고 가정(장중 지정가 체결 가정).
- 빈 슬롯이 생긴 날은 그날 기준 65점 이상(바닥 신호 7개, 기존 가중치) 종목
  중 미보유·쿨다운 아닌 종목을 점수순으로 슬롯 수만큼 뽑아 다음 거래일 시가에
  매수. 부족하면 남은 슬롯은 이월(다음 거래일에 다시 그날 기준으로 재스캔).
- 한 번 매도한 종목은 매도일로부터 30영업일 동안 재매수 후보에서 제외.
- 65점 계산은 이미 검증된 로직(signals.BOTTOM_WEIGHTS 7개 신호, 생존게이트
  OHLC=0/거래정지 필터) 그대로 재사용. 턴어라운드 5개 신호도 같이 계산해서
  confirmed_turnaround/watching 상태를 매매로그에 참고용으로 남긴다(매수
  선정 자체에는 안 씀 — screener.py/strategy_backtest_2022.py와 동일한 원칙).

[순수 계산 — 라이브 pykrx 호출 없음]
유니버스/업종매핑은 그 달의 월초 앵커 캐시(cache/anchor_snapshots/, 기존
strategy_backtest_2022.py 48개월 실행분)를 그 달 전체 영업일에 재사용한다
(업종 구성은 하루이틀 사이 바뀌지 않으므로 근사로 충분). OHLCV/펀더멘털/
공매도/시가총액/매집은 로컬 DB(data/*.db), 코스피·코스닥·업종 지수는
data/index_history.sqlite에서 읽는다 — 이번 세션에 이 스크립트를 위해
구축한 것.

사용법: python portfolio_simulation.py
  [--start 20220701] [--end 20221229] [--out backtests/portfolio_sim_2022h2.csv]
"""
from __future__ import annotations
import argparse
import csv
import sys
import time
import datetime as dt
from pathlib import Path
from statistics import median

sys.stdout.reconfigure(encoding="utf-8")

import screener as scr
import signals as sg
import db_reader as dbr
import snapshot_cache
import signal_score_cache

SIM_START_DEFAULT = "20220701"
SIM_END_DEFAULT = "20221229"   # 2022-12-30은 원본 daily_prices 자체가 결측(휴일 아님, 실측 확인) — 스킵
START_CAPITAL = 100_000_000.0
N_SLOTS = 10
SLOT_PCT = 0.10
SCORE_THRESHOLD = 65.0          # strategy_backtest_2022.SCORE_THRESHOLD와 동일 값
TAKE_PROFIT_PCT = 0.05
STOP_LOSS_PCT = -0.05
COOLDOWN_TRADING_DAYS = 30

GATE_TURNAROUND_KEYS = scr.PRICE_GROUP + scr.FLOW_GROUP

MONTH_ANCHORS = ["20220701", "20220801", "20220901", "20221003", "20221101", "20221201"]


# ---------------- 사전 로딩 (라이브 호출 없이 로컬 DB/캐시 일괄 로드) ----------------

def load_month_snapshots() -> dict[str, dict]:
    """{"YYYY-MM": snapshot} — cache/anchor_snapshots/의 월초 앵커 6개를 읽는다."""
    out = {}
    for a in MONTH_ANCHORS:
        snap = snapshot_cache.load_anchor_snapshot(a)
        if snap is None:
            raise RuntimeError(f"앵커 스냅샷 없음: {a} — strategy_backtest_2022.py를 먼저 그 달에 대해 "
                                f"돌려서 cache/anchor_snapshots/{a}.json을 만들어야 함")
        ym = f"{a[:4]}-{a[4:6]}"
        out[ym] = snap
    return out


def governing_month(date_str: str) -> str:
    return f"{date_str[:4]}-{date_str[4:6]}"


def build_trading_calendar(matrix: dict[str, dict], start: str, end: str) -> list[str]:
    return sorted(d for d in matrix.keys() if start <= d <= end)


# ---------------- 하루치 스코어링 (screen_and_score 로직 재사용, 로컬 DB만) ----------------
#
# score_day()는 두 단계로 나뉜다:
#   1) _compute_raw_scores_for_day: 무거운 부분(DB 읽기 + 신호별 raw 0~100점 계산).
#      가중치·컷라인과 무관하게 항상 같은 결과라, 한 번 계산되면
#      signal_score_cache.py에 저장해서 이후 재실행(가중치·매매조건 실험)에서는
#      건너뛴다.
#   2) combine_scores: 캐시됐거나 방금 계산한 raw 점수를 현재 가중치
#      (signals.BOTTOM_WEIGHTS)·컷라인(SCORE_THRESHOLD)으로 조합 — 여기서부터는
#      DB 접근이 전혀 없는 순수 계산이라, 가중치나 컷라인만 바꿔 재실험할 때는
#      이 함수만 다른 값으로 다시 부르면 몇 초 안에 끝난다.

def _compute_raw_scores_for_day(day: str, universe: dict, sector_map: dict, sector_names: dict,
                                ticker_market: dict, matrix: dict, fund_hist: dict,
                                market_idx_by_date: dict, sector_idx_by_date: dict) -> list[dict]:
    """그날 기본 게이트(생존게이트·거래대금·시총·PBR>0)를 통과한 전 종목의 신호별
       raw 점수(가중치 적용 전, 최종 컷라인 미적용)를 반환한다. 로직은
       strategy_backtest_2022.screen_and_score의 종목별 채점 부분과 동일하다
       (그 파일을 import해서 재사용하지 않는 이유: 그 함수는 앵커별 캐시 로딩/
       라이브 폴백까지 같이 하는 구조라 매일 호출하기엔 안 맞음 — 여기서는
       이미 메모리에 전부 preload된 데이터만 슬라이싱해서 쓴다)."""
    ohlcv_dates = scr.recent_business_dates(scr.OHLCV_LOOKBACK_DAYS, dt.datetime.strptime(day, "%Y%m%d").date())
    db_dates_sorted = [d for d in ohlcv_dates if d in matrix]
    if not db_dates_sorted:
        return []
    latest_date = db_dates_sorted[-1]
    # 성능을 위해 matrix에는 전체 시뮬레이션 기간이 다 preload돼 있지만(run()의
    # 사전 로딩 참고), screener.series_for_ticker는 넘겨받은 matrix의 날짜를
    # 전부 다 쓰지 그날짜(day) 이후를 걸러주지 않는다 — 원래(strategy_backtest_2022.py)는
    # 앵커마다 그 앵커까지의 130일치만 담긴 matrix를 새로 만들어 넘겨서 문제가
    # 없었는데, 여기서는 반드시 day까지만 자른 서브셋을 넘겨야 미래 데이터가
    # 안 섞인다(그냥 matrix를 넘기면 예: 7월 1일 채점에 12월 데이터가 섞여
    # closes[-1]이 7월 1일 종가가 아니게 되는 심각한 시점 누설 버그가 생김).
    windowed_matrix = {d: matrix[d] for d in db_dates_sorted}

    short_max = dbr.load_short_max_from_db(scr.weekly_samples(scr.SHORT_SAMPLE_WEEKS,
                                                                dt.datetime.strptime(day, "%Y%m%d").date()))
    short_cur = dbr.load_short_current_from_db(latest_date)
    market_cap = dbr.load_market_cap_from_db(latest_date)

    accum_from = ohlcv_dates[-scr.ACCUM_WINDOW_DAYS] if len(ohlcv_dates) >= scr.ACCUM_WINDOW_DAYS else ohlcv_dates[0]
    accum_recent5_from = ohlcv_dates[-5] if len(ohlcv_dates) >= 5 else ohlcv_dates[0]
    accum_prior15_from = ohlcv_dates[-20] if len(ohlcv_dates) >= 20 else ohlcv_dates[0]
    accum_prior15_to = ohlcv_dates[-6] if len(ohlcv_dates) >= 6 else ohlcv_dates[0]
    accum = dbr.load_accumulation_from_db(dbr.date_range_inclusive(db_dates_sorted, accum_from, latest_date))
    accum_recent5 = dbr.load_accumulation_from_db(dbr.date_range_inclusive(db_dates_sorted, accum_recent5_from, latest_date))
    accum_prior15 = dbr.load_accumulation_from_db(dbr.date_range_inclusive(db_dates_sorted, accum_prior15_from, accum_prior15_to))

    out = []
    for tkr, name in universe.items():
        dates, opens, highs, lows, closes, vols = scr.series_for_ticker(windowed_matrix, tkr)
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

        bench_series, _ = scr.resolve_benchmark_series(tkr, sector_map, sector_idx_by_date,
                                                        market_idx_by_date, ticker_market)
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

        bottom_scores = {
            "volume_dryness": sg.score_volume_dryness(rec6to25, past120),
            "accumulation": sg.score_accumulation(accum.get(tkr, 0.0), float_mc, ret20_price * 100),
            "short_covering": sg.score_short_covering(short_cur.get(tkr, 0.0), short_max.get(tkr, 0.0)),
            "pbr_low": None if capital_eroding else sg.score_pbr_low(cur_pbr, pbr_series),
            "dividend_yield": sg.score_dividend_yield(cur_div, div_series, cur_dps, cur_eps, scr.had_dividend_cut(fh)),
            "relative_strength": None if split_suspected else sg.score_relative_strength(ret60, idx_ret_t),
            "volatility_squeeze": sg.score_volatility_squeeze(bw_series),
        }

        turnaround_scores = {k: None for k in GATE_TURNAROUND_KEYS}
        if len(closes) >= 21 and len(dates) >= 21:
            recent5_avg_vol = sum(vols[-5:]) / 5
            prior15_avg_vol = sum(vols[-20:-5]) / 15
            ma20 = sum(closes[-20:]) / 20
            ma60 = sum(closes[-60:]) / 60
            high60 = max(closes[-60:])
            stock_ret_recent10 = (closes[-1] / closes[-11]) - 1
            stock_ret_prior10 = (closes[-11] / closes[-21]) - 1
            idx_c1 = bench_series.get(dates[-1])
            idx_c11 = bench_series.get(dates[-11])
            idx_c21 = bench_series.get(dates[-21])
            index_ret_recent10 = (idx_c1 / idx_c11 - 1) if idx_c1 and idx_c11 else None
            index_ret_prior10 = (idx_c11 / idx_c21 - 1) if idx_c11 and idx_c21 else None
            net_buy_recent5_avg = accum_recent5.get(tkr, 0.0) / 5
            net_buy_prior15_avg = accum_prior15.get(tkr, 0.0) / 15
            turnaround_scores = {
                "volume_surge": sg.score_volume_surge(recent5_avg_vol, prior15_avg_vol),
                "ma_breakout": None if split_suspected else sg.score_ma_breakout(closes[-1], ma20, ma60),
                "short_term_breakout": None if split_suspected else sg.score_short_term_breakout(closes[-1], high60),
                "relative_strength_accel": sg.score_relative_strength_accel(
                    stock_ret_recent10, index_ret_recent10, stock_ret_prior10, index_ret_prior10),
                "accumulation_accel": sg.score_accumulation_accel(net_buy_recent5_avg, net_buy_prior15_avg),
            }

        out.append({"ticker": tkr, "name": name, "bottom_scores": bottom_scores,
                    "turnaround_scores": turnaround_scores, "pbr_caution_sector": pbr_caution_sector})

    return out


def combine_scores(raw_entries: list[dict], score_threshold: float = SCORE_THRESHOLD) -> list[dict]:
    """캐시됐거나 방금 계산한 raw 점수 리스트를 현재 가중치(signals.BOTTOM_WEIGHTS)로
       조합해 컷라인 통과 종목만 점수 내림차순으로 반환한다. DB 접근이 전혀
       없는 순수 계산이라, 가중치·컷라인만 바꿔 재실험할 때는 이 함수만 다시
       부르면 된다(score_day 전체를 다시 안 돌려도 됨)."""
    out = []
    for e in raw_entries:
        bottom_weights = dict(sg.BOTTOM_WEIGHTS)
        if e["pbr_caution_sector"]:
            bottom_weights["pbr_low"] = bottom_weights["pbr_low"] / 2
        bottom_comp = sg.composite_score(e["bottom_scores"], bottom_weights)
        if bottom_comp["composite"] is None or bottom_comp["composite"] < score_threshold:
            continue

        turnaround_scores = e["turnaround_scores"]
        price_confirmed = any(turnaround_scores.get(k) is not None and turnaround_scores[k] >= scr.TURNAROUND_STRONG_THRESHOLD
                              for k in scr.PRICE_GROUP)
        flow_confirmed = any(turnaround_scores.get(k) is not None and turnaround_scores[k] >= scr.TURNAROUND_STRONG_THRESHOLD
                             for k in scr.FLOW_GROUP)
        status = "confirmed_turnaround" if (price_confirmed and flow_confirmed) else "watching"

        out.append({"ticker": e["ticker"], "name": e["name"], "score": bottom_comp["composite"], "status": status})

    out.sort(key=lambda c: -c["score"])
    return out


def score_day(day: str, universe: dict, sector_map: dict, sector_names: dict, ticker_market: dict,
              matrix: dict, fund_hist: dict, market_idx_by_date: dict, sector_idx_by_date: dict) -> list[dict]:
    """그날(day) 기준 바닥점수 65점 이상 종목 리스트(점수 내림차순). raw 점수가
       cache/daily_signal_scores/{day}.json에 이미 있으면 DB를 전혀 안 읽고
       그것만 다른 가중치로 재조합한다 — 없으면 계산 후 캐시에 저장한다."""
    raw_entries = signal_score_cache.load_day_scores(day)
    if raw_entries is None:
        raw_entries = _compute_raw_scores_for_day(day, universe, sector_map, sector_names, ticker_market,
                                                   matrix, fund_hist, market_idx_by_date, sector_idx_by_date)
        signal_score_cache.save_day_scores(day, raw_entries)
    return combine_scores(raw_entries, SCORE_THRESHOLD)


# ---------------- 시뮬레이션 본체 ----------------

def run(start: str, end: str, out_csv: str) -> None:
    t0 = time.time()
    print(f"[포트폴리오시뮬] {start}~{end}, 시작자본 {START_CAPITAL:,.0f}원, 최대 {N_SLOTS}종목")

    print("사전 로딩: 월별 앵커 스냅샷(유니버스/업종/구 지수)…")
    month_snaps = load_month_snapshots()

    print("사전 로딩: OHLCV 전체 구간(2022-01-03~sim end, 시가/고가/저가/종가/거래량)…")
    anchor0 = dt.datetime.strptime(start, "%Y%m%d").date()
    lookback_start = scr.recent_business_dates(scr.OHLCV_LOOKBACK_DAYS, anchor0)[0]
    all_calendar_dates = scr.recent_business_dates(400, dt.datetime.strptime(end, "%Y%m%d").date())
    needed_dates = [d for d in all_calendar_dates if lookback_start <= d <= end]
    matrix = dbr.load_ohlcv_matrix_from_db_full(needed_dates)
    print(f"  {len(matrix)}개 실제 거래일 확보 (요청 {len(needed_dates)}개 중 — 나머지는 휴일)")

    print("사전 로딩: 코스피·코스닥·업종 지수(data/index_history.sqlite)…")
    market_idx_by_date = dbr.load_market_index_from_db(lookback_start, end)
    sector_codes_needed: set[str] = set()
    for snap in month_snaps.values():
        sector_codes_needed.update(snap["sector_map"].values())
    sector_idx_by_date = dbr.load_sector_index_from_db(sector_codes_needed, lookback_start, end)
    n_sector_with_data = sum(1 for v in sector_idx_by_date.values() if v)
    print(f"  코스피 {len(market_idx_by_date.get('KOSPI', {}))}일, 코스닥 {len(market_idx_by_date.get('KOSDAQ', {}))}일, "
          f"업종지수 {n_sector_with_data}/{len(sector_codes_needed)}개 코드에 데이터 있음")

    print("사전 로딩: 펀더멘털 5년 밴드(월 단위, 6개월분)…")
    fund_hist_by_month: dict[str, dict] = {}
    for ym in month_snaps:
        y, m = int(ym[:4]), int(ym[5:7])
        anchor = dt.date(y, m, 1)
        fund_hist_by_month[ym] = dbr.load_fundamental_history_from_db(
            scr.month_end_samples(scr.FUND_HISTORY_MONTHS, anchor))

    trading_days = build_trading_calendar(matrix, start, end)
    print(f"시뮬레이션 대상 거래일: {len(trading_days)}일 ({trading_days[0]}~{trading_days[-1]})")

    # ---------------- 상태 ----------------
    cash = START_CAPITAL
    holdings: dict[str, dict] = {}          # ticker -> {buy_date, buy_price, shares, name, score, status}
    cooldown_until_idx: dict[str, int] = {}  # ticker -> 재매수 가능해지는 최소 인덱스
    pending_buys: list[dict] = []            # 다음 거래일 시가에 살 후보
    trade_log: list[dict] = []
    equity_curve: list[tuple[str, float]] = []
    empty_slot_days = 0

    def portfolio_value_at(day: str, price_field: str = "close") -> float:
        v = cash
        idx = 0 if price_field == "open" else 3
        for tkr, pos in holdings.items():
            row = matrix.get(day, {}).get(tkr)
            price = row[idx] if row else pos["buy_price"]
            v += pos["shares"] * price
        return v

    for i, day in enumerate(trading_days):
        ym = governing_month(day)
        snap = month_snaps[ym]
        universe, sector_map, sector_names = snap["universe"], snap["sector_map"], snap["sector_names"]
        ticker_market = snap["ticker_market"]
        fund_hist = fund_hist_by_month[ym]

        # 1) 전날 스캔에서 정한 후보를 오늘 시가에 매수 (첫날은 아래 별도 부트스트랩)
        if i > 0 and pending_buys:
            base_value = portfolio_value_at(day, "open")
            for cand in pending_buys:
                tkr = cand["ticker"]
                if tkr in holdings or len(holdings) >= N_SLOTS:
                    continue
                row = matrix.get(day, {}).get(tkr)
                if row is None or row[0] <= 0:
                    continue  # 매수 당일 시세 없음(거래정지 추정) — 이번엔 스킵, 슬롯은 계속 빈 채로 남아 다음날 재스캔
                open_price = row[0]
                invest = base_value * SLOT_PCT
                shares = invest / open_price
                cash -= invest
                holdings[tkr] = {"buy_date": day, "buy_price": open_price, "shares": shares,
                                  "name": cand["name"], "score": cand["score"], "status": cand["status"]}
            pending_buys = []

        # 0) 첫날 부트스트랩: 그날 시가 기준으로 그날 스크리닝 상위 10개 매수
        if i == 0:
            today_candidates = score_day(day, universe, sector_map, sector_names, ticker_market,
                                         matrix, fund_hist, market_idx_by_date, sector_idx_by_date)
            base_value = cash
            for cand in today_candidates[:N_SLOTS]:
                tkr = cand["ticker"]
                row = matrix.get(day, {}).get(tkr)
                if row is None or row[0] <= 0:
                    continue
                open_price = row[0]
                invest = base_value * SLOT_PCT
                shares = invest / open_price
                cash -= invest
                holdings[tkr] = {"buy_date": day, "buy_price": open_price, "shares": shares,
                                  "name": cand["name"], "score": cand["score"], "status": cand["status"]}
            print(f"  [첫날 {day}] 스크리닝 통과 {len(today_candidates)}개 중 상위 {len(holdings)}개 매수")

        # 2) 보유 종목 매도 판정 (오늘 고가/저가 vs 매수가), 티커 오름차순
        for tkr in sorted(list(holdings.keys())):
            pos = holdings[tkr]
            row = matrix.get(day, {}).get(tkr)
            if row is None:
                continue  # 거래정지 추정 — 오늘은 매도판정 스킵(보유 유지)
            _, high, low, close, _ = row
            tp_price = pos["buy_price"] * (1 + TAKE_PROFIT_PCT)
            sl_price = pos["buy_price"] * (1 + STOP_LOSS_PCT)
            hit_tp = high >= tp_price
            hit_sl = low <= sl_price
            if not (hit_tp or hit_sl):
                continue
            if hit_sl:
                sell_price, reason = sl_price, "-5%"
            else:
                sell_price, reason = tp_price, "+5%"
            proceeds = pos["shares"] * sell_price
            cash += proceeds
            buy_idx = trading_days.index(pos["buy_date"])  # 보유일수(영업일) 계산용
            trade_log.append({
                "ticker": tkr, "name": pos["name"], "score": pos["score"], "status": pos["status"],
                "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 2),
                "sell_date": day, "sell_price": round(sell_price, 2), "sell_reason": reason,
                "holding_trading_days": i - buy_idx, "return_pct": round((sell_price / pos["buy_price"] - 1) * 100, 2),
                "open_position": False,
            })
            cooldown_until_idx[tkr] = i + COOLDOWN_TRADING_DAYS
            del holdings[tkr]

        # 3) 빈 슬롯 있으면 오늘 기준 재스캔 → 다음 거래일 시가 매수용으로 큐잉
        open_slots = N_SLOTS - len(holdings)
        empty_slot_days += open_slots
        if open_slots > 0:
            today_candidates = score_day(day, universe, sector_map, sector_names, ticker_market,
                                         matrix, fund_hist, market_idx_by_date, sector_idx_by_date)
            picked = []
            for cand in today_candidates:
                if len(picked) >= open_slots:
                    break
                tkr = cand["ticker"]
                if tkr in holdings:
                    continue
                if cooldown_until_idx.get(tkr, -1) > i:
                    continue
                picked.append(cand)
            pending_buys = picked

        equity_curve.append((day, portfolio_value_at(day, "close")))
        if (i + 1) % 20 == 0 or i == len(trading_days) - 1:
            print(f"  진행 {i+1}/{len(trading_days)} ({day}): 보유 {len(holdings)}개, "
                  f"평가액 {equity_curve[-1][1]:,.0f}원, 누적매매 {len(trade_log)}건, "
                  f"소요 {(time.time()-t0)/60:.1f}분")

    # ---------------- 기말 미청산 포지션 마감 평가 ----------------
    last_day = trading_days[-1]
    for tkr, pos in holdings.items():
        row = matrix.get(last_day, {}).get(tkr)
        last_close = row[3] if row else pos["buy_price"]
        buy_idx = trading_days.index(pos["buy_date"])
        trade_log.append({
            "ticker": tkr, "name": pos["name"], "score": pos["score"], "status": pos["status"],
            "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 2),
            "sell_date": None, "sell_price": None, "sell_reason": None,
            "holding_trading_days": len(trading_days) - 1 - buy_idx,
            "return_pct": round((last_close / pos["buy_price"] - 1) * 100, 2),
            "open_position": True,
        })

    final_value = equity_curve[-1][1] if equity_curve else START_CAPITAL
    write_csv(trade_log, out_csv)
    print_summary(trade_log, equity_curve, final_value, empty_slot_days, len(trading_days))
    print(f"\n[포트폴리오시뮬] 전체 소요시간: {(time.time()-t0)/60:.1f}분")


def write_csv(rows: list[dict], out_path: str) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        print(f"[포트폴리오시뮬] 저장할 행이 없습니다: {out_path}")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[포트폴리오시뮬] CSV 저장 완료: {out_path} ({len(rows)}행)")


def print_summary(trade_log: list[dict], equity_curve: list[tuple[str, float]],
                  final_value: float, empty_slot_days: int, n_days: int) -> None:
    closed = [t for t in trade_log if not t["open_position"]]
    open_pos = [t for t in trade_log if t["open_position"]]
    wins = [t for t in closed if t["sell_reason"] == "+5%"]

    print("\n" + "=" * 60)
    print("[포트폴리오시뮬] 2022년 하반기 일별 스크리닝 포트폴리오 결과")
    print(f"  시작자본: {START_CAPITAL:,.0f}원 → 최종 평가액: {final_value:,.0f}원 "
          f"({(final_value/START_CAPITAL-1)*100:+.2f}%)")
    print(f"  청산 매매: {len(closed)}건 (+5% 익절 {len(wins)}건, -5% 손절 {len(closed)-len(wins)}건)")
    if closed:
        win_rate = len(wins) / len(closed) * 100
        print(f"  승률(청산 매매 기준): {win_rate:.1f}%")
    print(f"  기말 미청산 보유: {len(open_pos)}종목")

    if equity_curve:
        peak = equity_curve[0][1]
        max_dd = 0.0
        max_dd_date = equity_curve[0][0]
        for d, v in equity_curve:
            peak = max(peak, v)
            dd = (v / peak - 1) * 100
            if dd < max_dd:
                max_dd, max_dd_date = dd, d
        print(f"  최대 낙폭(MDD, 포트폴리오 평가액 기준): {max_dd:.2f}% ({max_dd_date})")

    empty_ratio = empty_slot_days / (n_days * N_SLOTS) * 100
    print(f"  빈 슬롯 비율: {empty_ratio:.1f}% (전체 {n_days}일×{N_SLOTS}슬롯 중 하루 평균 빈 슬롯 일수 비중)")
    print("=" * 60)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="2022년 하반기 일별 포트폴리오 시뮬레이션")
    p.add_argument("--start", default=SIM_START_DEFAULT)
    p.add_argument("--end", default=SIM_END_DEFAULT)
    p.add_argument("--out", default="backtests/portfolio_sim_2022h2.csv")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    run(args.start, args.end, args.out)
