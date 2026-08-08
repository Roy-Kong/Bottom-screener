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
import statistics

import screener as scr
import db
import index_db


def find_trading_day_on_or_before_db(target: dt.date) -> str | None:
    """target 이전(포함) 가장 최근 실제 거래일을 찾는다 — pykrx 호출 없이 기준일을
       정할 수 있다.

       파일이 '존재'하는 것과 '그 날 실제 거래가 있었던 것'은 다르다 — 휴장일도
       빈 스키마만 있는 파일을 만들어 "이미 확인함" 표시로 남기므로(db.py 참고),
       파일 존재만으로 판정하면 최신 후보가 하필 휴장일 스텁일 때 그 날짜를
       거래일로 오판한다(2026-07 사고: 같은 종류의 판정 오류가 라이브 경로
       find_first_trading_day_of_month 등에서도 있었음, screener.has_real_trading_data
       참고). daily_prices에 실제 행이 있는 파일이 나올 때까지 거슬러 올라간다."""
    ds = scr.yyyymmdd(target)
    candidates = [d for d in db.existing_dates() if d <= ds]
    for d in reversed(candidates):
        rows = _read_day(d, "daily_prices", "COUNT(*)")
        if rows and rows[0][0] > 0:
            return d
    return None


def _read_day(date: str, table: str, columns: str) -> list[tuple]:
    path = db.daily_db_path(date)
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(f"SELECT {columns} FROM {table}").fetchall()
    except sqlite3.OperationalError:
        return []
    except sqlite3.DatabaseError:
        # "file is not a database" — 커밋은 됐지만 git lfs pull 없이 체크아웃만 돼
        # 실제 LFS 내용이 아니라 포인터 스텁 텍스트만 있는 상태(db.py의
        # table_collected()가 같은 상황을 같은 이유로 잡는 것과 동일한 케이스).
        # 호출부가 이 날짜를 "DB에 없음"으로 보고 라이브 폴백하게 빈 리스트 반환.
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


def load_ohlcv_matrix_from_db_full(dates: list[str]) -> dict[str, dict[str, tuple]]:
    """{date: {ticker: (open, high, low, close, volume)}} — load_ohlcv_matrix_from_db와
       같은 ±30% 상하한가 필터를 쓰지만, 2022-2026 OHLC 백필(open/high/low 컬럼)
       완료 이후로 5-tuple 전체를 반환한다. screener.series_for_ticker는 5-tuple을
       받으면 그대로 쓰고 2-tuple을 받으면 open=high=low=close로 패딩하므로, 이
       함수를 쓰면 is_trading_halted가 (라이브 recheck 없이도) 정상 동작한다 —
       strategy_backtest_2022.py는 기존 2-tuple 버전을 그대로 쓰고 있어(그 스크립트를
       건드리지 않기로 한 결정) 이건 별도 추가 함수다."""
    matrix: dict[str, dict[str, tuple]] = {}
    last_close: dict[str, float] = {}
    for d in sorted(dates):
        rows = _read_day(d, "daily_prices", "ticker, open, high, low, close, volume")
        if not rows:
            continue
        day: dict[str, tuple] = {}
        for tkr, o, h, l, close, vol in rows:
            if close is None or close <= 0:
                continue
            prev = last_close.get(tkr)
            if prev is not None and prev > 0:
                ratio = close / prev
                if ratio > scr.MAX_DAILY_MOVE_RATIO or ratio < 1 / scr.MAX_DAILY_MOVE_RATIO:
                    continue
            day[tkr] = (o or 0.0, h or 0.0, l or 0.0, close, vol or 0.0)
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


SIGNAL_HISTORY_MONTHS = 24  # accumulation/volume_dryness 자기 히스토리 percentile용 표본 개월수


def _trading_dates_window_from_db(end: dt.date, calendar_days_back: int) -> list[str]:
    """end 이전 calendar_days_back일 이내, 실제 거래(daily_prices에 행이 있음)가
       있었던 날짜 목록(오름차순) — find_trading_day_on_or_before_db와 같은 이유로
       파일 존재만이 아니라 행 존재까지 확인한다(휴장일 빈 스텁 스키마 제외)."""
    start_ds = scr.yyyymmdd(end - dt.timedelta(days=calendar_days_back))
    end_ds = scr.yyyymmdd(end)
    out = []
    for d in db.existing_dates():
        if not (start_ds <= d <= end_ds):
            continue
        rows = _read_day(d, "daily_prices", "COUNT(*)")
        if rows and rows[0][0] > 0:
            out.append(d)
    return out


class SignalHistorySource:
    """daily_investor_flow(inst_foreign_net_buy)/daily_prices(volume, market_cap)를
       [start, end] 구간 전체에 걸쳐 한 번만 벌크 적재해두고, 그 안의 여러 앵커
       날짜에 대해 반복적으로 accumulation/volume_dryness 자기 히스토리를 뽑아 쓸 수
       있게 한다(파일 재오픈 없이 메모리에서 슬라이싱).

       하루 단위로 앵커가 계속 바뀌는 시뮬레이션(portfolio_simulation.py처럼 날짜별
       재채점)에서 앵커마다 load_signal_history_from_db를 새로 부르면 겹치는 구간을
       매번 다시 읽어 심각하게 느려진다 — 이 클래스는 그 구간을 한 번만 적재해
       재사용한다(이 세션에서 확립된 '한 번 적재, 메모리에서 슬라이싱' 패턴,
       diagnose_all_signals_bias.py에서 검증된 접근과 동일).

       시점 무결성: for_anchor(anchor)는 anchor 이전(포함) 거래일만 쓰는 월별 표본을
       만들므로, 시뮬레이션 중 앵커가 하루씩 전진해도 그 시점엔 아직 존재하지 않았을
       미래 데이터가 히스토리에 섞이지 않는다."""

    def __init__(self, trading_dates: list[str],
                 flow_by_date: dict[str, dict[str, float]],
                 vol_by_date: dict[str, dict[str, float]],
                 mc_by_date: dict[str, dict[str, float]]):
        self.trading_dates = trading_dates
        self.flow_by_date = flow_by_date
        self.vol_by_date = vol_by_date
        self.mc_by_date = mc_by_date

    def for_anchor(
        self, anchor: dt.date, months: int = SIGNAL_HISTORY_MONTHS,
    ) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
        """(accumulation intensity 히스토리, volume_dryness 비율 히스토리) —
           둘 다 {ticker: [월별 표본, 과거→최근순]}. score_accumulation의
           intensity_history / score_volume_dryness의 ratio_history 인자용.

           월별 표본 규칙은 month_end_samples(펀더멘털 5년 밴드와 동일 방식)를
           재사용해 실제 거래일로 스냅한다. 표본 하나당:
             - intensity = 그 거래일 기준 t-9~t-48(최근8일 제외 그 이전 40영업일)
               누적(기관+외국인 순매수)/시가총액
             - vd_ratio = t-9~t-48(accumulation과 동일 40일 창) 거래량 중앙값 /
               직전 120영업일 중앙값
           (score_accumulation/score_volume_dryness의 실시간 계산과 동일한 정의).

           2026-08 바닥-턴어라운드 8일 경계 재설계: 두 신호의 "최근" 측정구간을
           20일/6~25일전에서 동일한 t-9~t-48(40일)로 통일했다 — 턴어라운드
           신호(매집가속·거래량급증)가 보는 "최근8일"과 정확히 맞물려 겹치지
           않게 하기 위함(screener.py 등 라이브 원값 계산과 반드시 같은 창을
           써야 percentile 비교가 사과-사과가 된다)."""
        anchor_ds = scr.yyyymmdd(anchor)
        trading_dates = [d for d in self.trading_dates if d <= anchor_ds]
        if len(trading_dates) < 120:
            return {}, {}

        calendar_samples = scr.month_end_samples(months, anchor)  # 최신→과거 순
        monthly_anchors: list[str] = []
        seen: set[str] = set()
        for ds in reversed(calendar_samples):  # 과거→최근 순으로 뒤집어 처리
            cutoff = scr.yyyymmdd(dt.datetime.strptime(ds, "%Y%m%d").date())
            snapped = None
            for d in reversed(trading_dates):
                if d <= cutoff:
                    snapped = d
                    break
            if snapped and snapped not in seen:
                seen.add(snapped)
                monthly_anchors.append(snapped)

        accum_hist: dict[str, list[float]] = {}
        vd_hist: dict[str, list[float]] = {}
        for ds in monthly_anchors:
            idx = trading_dates.index(ds)
            if idx < 47:
                continue  # 40일(t-9~t-48) 윈도우 미확보(백필 시작 근처)

            win_t9_t48 = trading_dates[idx - 47: idx - 7]  # 40일, accumulation/volume_dryness 공용
            accum_sum: dict[str, float] = {}
            for d in win_t9_t48:
                for t, v in self.flow_by_date.get(d, {}).items():
                    accum_sum[t] = accum_sum.get(t, 0.0) + v
            for t, mc in self.mc_by_date.get(ds, {}).items():
                if mc and mc > 0:
                    accum_hist.setdefault(t, []).append(accum_sum.get(t, 0.0) / mc)

            if idx < 119:
                continue  # 120영업일 윈도우 미확보(백필 시작 근처)
            win120 = trading_dates[idx - 119: idx + 1]
            vol_recent: dict[str, list[float]] = {}
            for d in win_t9_t48:
                for t, v in self.vol_by_date.get(d, {}).items():
                    vol_recent.setdefault(t, []).append(v)
            vol_past: dict[str, list[float]] = {}
            for d in win120:
                for t, v in self.vol_by_date.get(d, {}).items():
                    vol_past.setdefault(t, []).append(v)
            for t, past_list in vol_past.items():
                past_med = statistics.median(past_list)
                if past_med > 0 and t in vol_recent:
                    vd_hist.setdefault(t, []).append(statistics.median(vol_recent[t]) / past_med)

        return accum_hist, vd_hist


def build_signal_history_source(
    start: dt.date, end: dt.date, months: int = SIGNAL_HISTORY_MONTHS,
) -> SignalHistorySource:
    """[start, end] 구간 안의 모든 앵커를 지원하도록, start보다 (months개월+120영업일)
       더 이전까지 거슬러 올라간 전체 범위를 한 번만 벌크 적재해 SignalHistorySource를
       만든다. 시뮬레이션 기간 전체를 커버하는 start/end로 한 번 호출해두면, 그 안의
       날짜별 앵커마다 for_anchor()를 호출해도 파일을 다시 읽지 않는다."""
    calendar_days_back = months * 31 + 200
    # 버퍼는 항상 start 기준으로 더 거슬러 올라간다 — start는 "이 구간에서 요청될 가장
    # 이른 앵커"이고, 그 앵커도 자기 히스토리를 온전히 확보해야 하므로. start==end
    # (단일 앵커 호출)여도 이 식은 그대로 성립한다.
    trading_dates = _trading_dates_window_from_db(end, (end - start).days + calendar_days_back)
    if not trading_dates:
        return SignalHistorySource([], {}, {}, {})

    flow_by_date: dict[str, dict[str, float]] = {}
    vol_by_date: dict[str, dict[str, float]] = {}
    mc_by_date: dict[str, dict[str, float]] = {}
    for d in trading_dates:
        flow_rows = _read_day(d, "daily_investor_flow", "ticker, inst_foreign_net_buy")
        flow_by_date[d] = {t: v for t, v in flow_rows if v is not None}
        price_rows = _read_day(d, "daily_prices", "ticker, volume, market_cap")
        vol_by_date[d] = {t: (v or 0.0) for t, v, mc in price_rows}
        mc_by_date[d] = {t: mc for t, v, mc in price_rows if mc}

    return SignalHistorySource(trading_dates, flow_by_date, vol_by_date, mc_by_date)


def load_signal_history_from_db(
    anchor: dt.date, months: int = SIGNAL_HISTORY_MONTHS,
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """단일 앵커용 편의 함수 — anchor 하나만 채점할 때(screener.py 라이브 실행,
       backtest.py 단발 조회 등) build_signal_history_source + for_anchor를 한 번에
       묶어서 호출한다. 앵커가 여러 개(날짜별 반복 시뮬레이션)면 이 함수를 반복 호출하지
       말고 build_signal_history_source를 한 번만 부른 뒤 for_anchor를 재사용할 것
       (portfolio_simulation.py 참고)."""
    source = build_signal_history_source(anchor, anchor, months)
    return source.for_anchor(anchor, months)


INDEX_COVERAGE_START = "20220103"  # daily_index(data/index_history.sqlite) 백필 시작일


def load_market_index_from_db(fromdate: str, todate: str) -> dict[str, dict[str, float]]:
    """{market: {date: close}} — screener.py MARKET_INDEX_CODE(코스피/코스닥)
       기준으로 index_db.load_close_series를 감싼다. resolve_benchmark_series의
       market_idx_by_date 인자와 동일한 모양이라 그대로 대체 가능."""
    out: dict[str, dict[str, float]] = {}
    for mkt, code in scr.MARKET_INDEX_CODE.items():
        out[mkt] = index_db.load_close_series(code, fromdate, todate)
    return out


def load_sector_index_from_db(sector_codes: set[str], fromdate: str, todate: str) -> dict[str, dict[str, float]]:
    """{sector_code: {date: close}} — resolve_benchmark_series의 sector_idx_by_date
       인자와 동일한 모양. backfill_index.py --sector-codes로 채운 업종지수 코드만
       실제 값이 있고, 안 채워진 코드는 빈 dict(그 종목은 자동으로 시장지수
       폴백으로 넘어감 — resolve_benchmark_series 참고)."""
    out: dict[str, dict[str, float]] = {}
    for code in sector_codes:
        out[code] = index_db.load_close_series(code, fromdate, todate)
    return out


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
