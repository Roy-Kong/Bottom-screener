"""
strategy_backtest_2022.py — 2022년 월별 시점 매매전략 백테스트.

매월 첫 실제 거래일에 스크리닝 → 다음 영업일 시가 매수 가정 → 최대 30영업일
추적, +5%/+7%/+10% 각각 독립적으로 도달 여부·소요영업일수(고가 기준 터치)를
기록하고, 미도달 시 그 30일간 최대낙폭(저가 기준)을 기록한다.

[시점 무결성] 스크리닝에 쓰는 모든 데이터(OHLCV 130영업일, 펀더멘털 5년
밴드, 공매도 13주, 매집 20일 등)는 screener.py의 collect_* 함수들이 애초에
anchor(스크리닝 기준일) 이전(포함) 날짜만 조회하도록 만들어져 있어 미래
데이터가 섞일 수 없다. 매수·추적 단계(다음 영업일 시가부터 30영업일)만
스크리닝 시점보다 미래의 실제 시세를 쓰는데, 이는 "전략이 그 시점에 판단한
뒤 실제로 어떻게 됐는지"를 관찰하는 것이므로 미래 정보 누설이 아니다.

[생존편향] get_universe(asof)는 pykrx get_market_ticker_list(asof)로 그
날짜에 실제 상장돼 있던 종목만 받는다 — 상장폐지 종목도 폐지 전 시점
스크리닝에는 정상적으로 포함되고, 폐지 이후 시점부터는 자동으로 유니버스에서
빠진다(생존편향이 구조적으로 없음). 다만 pykrx의 상장폐지 종목 이력 데이터
자체가 완전한지는 이 프로젝트에서 별도 검증하지 않았다는 한계는 남는다.

[종합점수] "기존 로직 7개 바닥신호+5개 턴어라운드신호, 현재 가중치"를
문자 그대로 재사용한다 — signals.py의 BOTTOM_WEIGHTS(7개, 합계100)와
TURNAROUND_WEIGHTS 중 confirmed_turnaround 게이트에 실제 쓰이는 5개
(PRICE_GROUP 3개 + FLOW_GROUP 2개, 합계84)를 하나의 weights dict로 합쳐
signals.composite_score()에 그대로 넘긴다. 가중치 숫자를 새로 만들지 않고
기존 두 그룹의 값을 그대로 쓴다. RSI 반등·MACD 골든크로스(참고용, 게이트
미사용 신호)는 이 종합에서 제외한다 — screener.py가 confirmed_turnaround
판정에서 빼는 것과 같은 이유(PRICE_GROUP과 원리가 겹침).

[매수 불가 종목] 다음 영업일 시가가 전일 종가 대비 +28.9% 이상이면(±30%
상하한가 근처) 상한가 갭으로 실제 체결이 어려웠을 가능성이 높다고 보고
매수 후보에서 제외한다. 그 다음 영업일 시세 자체가 없으면(장기 거래정지·
상장폐지 등 추정) 마찬가지로 제외한다. 두 경우 모두 결과에 제외 사유와
개수를 남긴다(11번 요구사항).

펀더멘털 5년 밴드는 라이브 대신 DB(data/YYYYMMDD.db, 2017~ daily_fundamental
백필분)에서 읽어 속도를 높인다 — OHLCV/공매도/매집/지수/유니버스는 DB가
그만큼의 과거 구간을 안 갖고 있어(daily_prices 등은 2022-01~만 있음) 라이브
pykrx로 수집한다.

사용법:
    python strategy_backtest_2022.py --months 2022-01 \\
        --out backtests/backtest_2022_test.csv [--max-runtime-min 50]
    --months는 콤마구분 YYYY-MM 목록(예: 2022-01,2022-02,...,2022-12).
"""
from __future__ import annotations
import argparse
import csv
import sys
import time
import datetime as dt
from statistics import median
from pathlib import Path

from pykrx import stock
import screener as scr
import signals as sg
import db_reader as dbr

GATE_TURNAROUND_KEYS = scr.PRICE_GROUP + scr.FLOW_GROUP  # 5개(가격계열3+수급계열2)
MERGED_WEIGHTS = dict(sg.BOTTOM_WEIGHTS)
for _k in GATE_TURNAROUND_KEYS:
    MERGED_WEIGHTS[_k] = sg.TURNAROUND_WEIGHTS[_k]

SCORE_THRESHOLD = 65.0
TARGETS = [5.0, 7.0, 10.0]
MAX_TRACK_DAYS = 30
UPPER_LIMIT_GAP_RATIO = 1.289  # 전일종가 대비 시가 이 비율 이상이면 상한가 갭 추정


def find_first_trading_day_of_month(year: int, month: int, max_lookahead: int = 10) -> str | None:
    """그 달의 실제 첫 거래일(YYYYMMDD). 1일이 휴장이면 다음 거래일로 넘어간다."""
    d = dt.date(year, month, 1)
    for _ in range(max_lookahead):
        if d.weekday() < 5:
            ds = scr.yyyymmdd(d)
            try:
                tickers = stock.get_market_ticker_list(ds, market="KOSPI")
            except Exception:
                tickers = []
            if tickers:
                return ds
        d += dt.timedelta(days=1)
    return None


def find_next_trading_day(after: dt.date, max_lookahead: int = 14) -> str | None:
    """after보다 엄격히 나중인 첫 실제 거래일(YYYYMMDD)."""
    d = after + dt.timedelta(days=1)
    for _ in range(max_lookahead):
        if d.weekday() < 5:
            ds = scr.yyyymmdd(d)
            try:
                tickers = stock.get_market_ticker_list(ds, market="KOSPI")
            except Exception:
                tickers = []
            if tickers:
                return ds
        d += dt.timedelta(days=1)
    return None


def screen_and_score(anchor_date: dt.date, asof: str) -> list[dict]:
    """screener.py 채점 로직(바닥7+턴어라운드5, 병합 종합점수) 재현."""
    universe, ticker_market = scr.get_universe(asof)
    sector_map, sector_names = scr.get_sector_index_map(asof)

    ohlcv_dates = scr.recent_business_dates(scr.OHLCV_LOOKBACK_DAYS, anchor_date)
    matrix = scr.collect_ohlcv_matrix(ohlcv_dates)
    latest_date = sorted(matrix.keys())[-1] if matrix else asof

    fund_hist = dbr.load_fundamental_history_from_db(
        scr.month_end_samples(scr.FUND_HISTORY_MONTHS, anchor_date))

    short_max = scr.collect_short_max(scr.weekly_samples(scr.SHORT_SAMPLE_WEEKS, anchor_date))
    short_cur = scr.collect_short_current(latest_date)
    market_cap = scr.collect_market_cap(latest_date)

    accum_from = ohlcv_dates[-scr.ACCUM_WINDOW_DAYS] if len(ohlcv_dates) >= scr.ACCUM_WINDOW_DAYS else ohlcv_dates[0]
    accum = scr.collect_accumulation(accum_from, latest_date)
    accum_recent5_from = ohlcv_dates[-5] if len(ohlcv_dates) >= 5 else ohlcv_dates[0]
    accum_recent5 = scr.collect_accumulation(accum_recent5_from, latest_date)
    accum_prior15_from = ohlcv_dates[-20] if len(ohlcv_dates) >= 20 else ohlcv_dates[0]
    accum_prior15_to = ohlcv_dates[-6] if len(ohlcv_dates) >= 6 else ohlcv_dates[0]
    accum_prior15 = scr.collect_accumulation(accum_prior15_from, accum_prior15_to)

    market_idx_by_date: dict[str, dict[str, float]] = {}
    for mkt, code in scr.MARKET_INDEX_CODE.items():
        try:
            idx = stock.get_index_ohlcv(ohlcv_dates[0], latest_date, code)
            market_idx_by_date[mkt] = scr.index_close_by_date(idx)
        except Exception:
            market_idx_by_date[mkt] = {}

    sector_codes_needed = set(sector_map.values())
    sector_idx_by_date = scr.collect_sector_index_ohlcv(sector_codes_needed, ohlcv_dates[0], latest_date)

    out = []
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

        weights = dict(MERGED_WEIGHTS)
        if pbr_caution_sector:
            weights["pbr_low"] = weights["pbr_low"] / 2

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
            recent20_avg_vol = sum(vols[-20:]) / 20
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
                "volume_surge": sg.score_volume_surge(recent5_avg_vol, recent20_avg_vol),
                "ma_breakout": None if split_suspected else sg.score_ma_breakout(closes[-1], ma20, ma60),
                "short_term_breakout": None if split_suspected else sg.score_short_term_breakout(closes[-1], high60),
                "relative_strength_accel": sg.score_relative_strength_accel(
                    stock_ret_recent10, index_ret_recent10, stock_ret_prior10, index_ret_prior10),
                "accumulation_accel": sg.score_accumulation_accel(net_buy_recent5_avg, net_buy_prior15_avg),
            }

        all_scores = {**bottom_scores, **turnaround_scores}
        comp = sg.composite_score(all_scores, weights)
        if comp["composite"] is None or comp["composite"] < SCORE_THRESHOLD:
            continue

        out.append({
            "ticker": tkr, "name": name, "score": comp["composite"],
            "breakdown": comp["breakdown"], "anchor_close": last_close,
        })
    return out


def simulate_trade(ticker: str, anchor_close: float, buy_search_from: dt.date) -> dict:
    """다음 영업일 시가 매수 가정 후 최대 30영업일(고가/저가 기준) 추적."""
    window_end = buy_search_from + dt.timedelta(days=60)
    try:
        df = stock.get_market_ohlcv_by_date(scr.yyyymmdd(buy_search_from), scr.yyyymmdd(window_end), ticker)
    except Exception:
        df = None
    if df is None or df.empty:
        return {"excluded": True, "exclude_reason": "매수 대상일 시세 없음(장기거래정지/상장폐지 추정)"}

    open_price = float(df.iloc[0]["시가"])
    buy_date_idx = df.index[0]
    buy_date_str = buy_date_idx.strftime("%Y%m%d") if hasattr(buy_date_idx, "strftime") else str(buy_date_idx)

    if open_price <= 0:
        return {"excluded": True, "exclude_reason": "매수일 시가 데이터 이상(0 이하)"}
    gap_ratio = (open_price / anchor_close) if anchor_close > 0 else None
    if gap_ratio is not None and gap_ratio >= UPPER_LIMIT_GAP_RATIO:
        pct = (gap_ratio - 1) * 100
        return {"excluded": True,
                "exclude_reason": f"상한가 갭 추정(전일종가 대비 시가 {pct:+.1f}%) — 실제 매수 어려웠을 가능성"}

    track_raw = df.iloc[:MAX_TRACK_DAYS]
    # 거래정지/상장폐지로 시가·고가·저가·종가가 0으로 찍히는 구간(메리츠증권 2023-04
    # 사례 — 포괄적 주식교환에 따른 상장폐지 절차로 데이터가 끊김, 실제 -100% 손실이
    # 아님)은 여기서 걸러낸다. 다우데이타(2023-04 SG증권 CFD 사태) 같은 실제 급락은
    # 시가/고가/저가/종가가 전부 정상 값이라 이 필터에 걸리지 않는다 —
    # scr.is_halted_snapshot 참고.
    valid_rows = [row for _, row in track_raw.iterrows()
                  if not scr.is_halted_snapshot(float(row["시가"]), float(row["고가"]),
                                                 float(row["저가"]), float(row["종가"]))]
    if not valid_rows:
        return {"excluded": True, "exclude_reason": "매수 이후 거래정지 추정(추적 데이터 없음)"}

    result: dict = {"excluded": False, "buy_date": buy_date_str, "buy_price": open_price,
                     "n_trading_days_tracked": len(valid_rows)}
    for target in TARGETS:
        target_price = open_price * (1 + target / 100)
        days_to = None
        for i, row in enumerate(valid_rows):
            if float(row["고가"]) >= target_price:
                days_to = i
                break
        result[f"reached_{int(target)}pct"] = days_to is not None
        result[f"days_to_{int(target)}pct"] = days_to

    lowest_pct = None
    for row in valid_rows:
        low_pct = (float(row["저가"]) / open_price - 1) * 100
        if lowest_pct is None or low_pct < lowest_pct:
            lowest_pct = low_pct
    result["max_drawdown_pct"] = round(lowest_pct, 2) if lowest_pct is not None else None
    return result


def write_csv(rows: list[dict], out_path: str) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        print(f"[전략백테스트] 저장할 행이 없습니다: {out_path}")
        return
    all_keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                all_keys.append(k)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[전략백테스트] CSV 저장 완료: {out_path} ({len(rows)}행)")


def print_summary(rows: list[dict], monthly_summaries: list[dict]) -> None:
    bought = [r for r in rows if not r["excluded"]]
    print("\n" + "=" * 60)
    print("[전략백테스트] 월별 요약")
    for m in monthly_summaries:
        print(f"  {m['month']} (기준일 {m['asof']}): 스크리닝 통과(65점+) {m['n_screened']}개, "
              f"매수 {m['n_bought']}개, 매수불가 제외 {m['n_excluded']}개")

    print("\n[전략백테스트] 전체 기간 합산 요약")
    print(f"  총 매수 종목: {len(bought)}개")
    for target in TARGETS:
        key_r, key_d = f"reached_{int(target)}pct", f"days_to_{int(target)}pct"
        reached = [r for r in bought if r[key_r]]
        not_reached = [r for r in bought if not r[key_r]]
        reach_rate = (len(reached) / len(bought) * 100) if bought else 0.0
        avg_days = (sum(r[key_d] for r in reached) / len(reached)) if reached else None
        avg_dd = (sum(r["max_drawdown_pct"] for r in not_reached) / len(not_reached)) if not_reached else None
        line = f"  +{target:.0f}%: 도달률 {reach_rate:.1f}% ({len(reached)}/{len(bought)})"
        line += f", 평균 도달일수 {avg_days:.1f}영업일" if avg_days is not None else ", 도달 종목 없음"
        line += f", 미도달({len(not_reached)}개) 평균 최대낙폭 {avg_dd:.2f}%" if avg_dd is not None else ""
        print(line)
    print("=" * 60)


def run(months: list[str], out_csv: str, max_runtime_min: int) -> None:
    t0 = time.time()
    deadline = t0 + max_runtime_min * 60
    rows: list[dict] = []
    monthly_summaries: list[dict] = []

    for ym in months:
        if time.time() > deadline:
            print(f"[전략백테스트] 시간 제한({max_runtime_min}분) 도달 — 남은 달은 다음 실행으로 미룹니다: "
                  f"{ym}부터 재개 필요")
            break
        year, month = int(ym[:4]), int(ym[5:7])
        asof = find_first_trading_day_of_month(year, month)
        if asof is None:
            print(f"[전략백테스트] {ym}: 첫 거래일을 찾지 못함 — 건너뜀")
            continue
        anchor_date = dt.datetime.strptime(asof, "%Y%m%d").date()
        print(f"\n=== {ym} 스크리닝 기준일 {asof} ===")
        t_month = time.time()
        candidates = screen_and_score(anchor_date, asof)
        print(f"  스크리닝 통과(65점 이상): {len(candidates)}개, 소요 {(time.time() - t_month) / 60:.1f}분")

        next_day_str = find_next_trading_day(anchor_date)
        if next_day_str is None:
            print(f"  다음 영업일을 찾지 못함 — {ym} 매매 스킵")
            continue
        next_day = dt.datetime.strptime(next_day_str, "%Y%m%d").date()

        n_bought = n_excluded = 0
        for c in candidates:
            trade = simulate_trade(c["ticker"], c["anchor_close"], next_day)
            row: dict = {
                "screening_month": ym, "screening_date": asof,
                "ticker": c["ticker"], "name": c["name"], "composite_score": c["score"],
            }
            row.update({f"signal_{k}": v for k, v in c["breakdown"].items()})
            if trade.get("excluded"):
                n_excluded += 1
                row["excluded"] = True
                row["exclude_reason"] = trade["exclude_reason"]
                row["buy_date"] = None
                row["buy_price"] = None
                for target in TARGETS:
                    row[f"reached_{int(target)}pct"] = None
                    row[f"days_to_{int(target)}pct"] = None
                row["max_drawdown_pct"] = None
            else:
                n_bought += 1
                row["excluded"] = False
                row["exclude_reason"] = ""
                row["buy_date"] = trade["buy_date"]
                row["buy_price"] = round(trade["buy_price"], 2)
                for target in TARGETS:
                    row[f"reached_{int(target)}pct"] = trade[f"reached_{int(target)}pct"]
                    row[f"days_to_{int(target)}pct"] = trade[f"days_to_{int(target)}pct"]
                row["max_drawdown_pct"] = trade["max_drawdown_pct"]
            rows.append(row)
            time.sleep(scr.REQUEST_PAUSE)

        monthly_summaries.append({"month": ym, "asof": asof, "n_screened": len(candidates),
                                   "n_bought": n_bought, "n_excluded": n_excluded})
        print(f"  매수 {n_bought}개, 매수불가 제외 {n_excluded}개, 이번 달 총 소요 "
              f"{(time.time() - t_month) / 60:.1f}분 (누적 {(time.time() - t0) / 60:.1f}분)")

    write_csv(rows, out_csv)
    print_summary(rows, monthly_summaries)
    print(f"\n[전략백테스트] 전체 소요시간: {(time.time() - t0) / 60:.1f}분")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="2022년 월별 매매전략 백테스트")
    p.add_argument("--months", required=True, help="콤마구분 YYYY-MM 목록, 예: 2022-01,2022-02")
    p.add_argument("--out", default="backtests/backtest_2022.csv")
    p.add_argument("--max-runtime-min", type=int, default=50)
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    months_arg = [m.strip() for m in args.months.split(",") if m.strip()]
    run(months_arg, args.out, args.max_runtime_min)
