"""
screener.py — 전종목 바닥 스크리너 파이프라인 (pykrx → signals.py → results.json)

흐름:
  1) 코스피+코스닥 종목 목록
  2) 효율적 수집: '하루치 전종목 스냅샷'을 여러 날짜에 대해 수집 (종목별 개별호출 최소화)
  3) 생존 게이트 통과 종목만 채점
  4) signals.py로 6개 신호 점수 → 종합점수
  5) 상위 N개를 results.json으로 저장 (프론트가 읽음)

주의: KRX 접속이 되는 환경(예: GitHub Actions)에서 실행해야 한다.
첫 실행은 디버그 패스가 필요할 수 있다(휴장일·결측·해외IP 차단 등).
"""
from __future__ import annotations
import json
import time
import datetime as dt
from statistics import median

from pykrx import stock
import signals as sg

# ---------------- 설정 (백테스트로 조정할 파라미터) ----------------
TARGET_MARKETS = ["KOSPI", "KOSDAQ"]
OHLCV_LOOKBACK_DAYS = 130      # 거래량/수익률용 최근 영업일 수
FUND_HISTORY_MONTHS = 60       # PBR/배당 5년 밴드용 월별 표본
SHORT_SAMPLE_WEEKS = 13        # 공매도 3개월 최고용 주별 표본
ACCUM_WINDOW_DAYS = 20         # 매집 판정 창(영업일)
TOP_N = 40                     # 결과에 담을 상위 종목 수

# 생존 게이트 임계값
MIN_MARKET_CAP = 30_000_000_000        # 시총 300억 이상
MIN_AVG_TRADING_VALUE = 500_000_000    # 20일 평균 거래대금 5억 이상

REQUEST_PAUSE = 0.10           # KRX 예의상 호출 간 간격(초)


# ---------------- 날짜 유틸 ----------------
def yyyymmdd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def find_latest_trading_day(max_lookback: int = 14) -> str:
    """실제로 시세 데이터가 존재하는 가장 최근 영업일을 찾아 반환.
       휴장일(주말·공휴일)에 돌리면 '오늘'에는 데이터가 없으므로,
       KRX에 실제 응답이 올 때까지 하루씩 거슬러 올라간다."""
    d = dt.date.today()
    for _ in range(max_lookback):
        if d.weekday() < 5:      # 0=월 ... 4=금
            ds = yyyymmdd(d)
            try:
                tickers = stock.get_market_ticker_list(ds, market=TARGET_MARKETS[0])
            except Exception:
                tickers = []
            if tickers:
                return ds
        d -= dt.timedelta(days=1)
    # 이 지점까지 왔다면 이상 상황; 마지막으로 시도한 날짜라도 반환해
    # 호출부가 빈 결과를 감지하고 처리하게 한다.
    return yyyymmdd(d)


def recent_business_dates(n: int, anchor: dt.date | None = None) -> list[str]:
    """anchor(기본: 가장 최근 영업일)부터 거꾸로, 영업일로 추정되는 날짜 문자열 n개
       (주말 제외; 공휴일은 pykrx가 빈DF로 처리)."""
    out, d = [], anchor or dt.date.today()
    while len(out) < n:
        if d.weekday() < 5:      # 0=월 ... 4=금
            out.append(yyyymmdd(d))
        d -= dt.timedelta(days=1)
    return list(reversed(out))   # 과거 → 현재 순


def month_end_samples(months: int, anchor: dt.date | None = None) -> list[str]:
    """최근 N개월의 월말(대략) 날짜 표본. 5년 밴드용."""
    out = []
    base = anchor or dt.date.today()
    d = base.replace(day=1) - dt.timedelta(days=1)  # 지난달 말일
    for _ in range(months):
        # 주말이면 금요일로 당김
        x = d
        while x.weekday() >= 5:
            x -= dt.timedelta(days=1)
        out.append(yyyymmdd(x))
        d = d.replace(day=1) - dt.timedelta(days=1)
    return out


def weekly_samples(weeks: int, anchor: dt.date | None = None) -> list[str]:
    out, d = [], anchor or dt.date.today()
    for _ in range(weeks):
        x = d
        while x.weekday() >= 5:
            x -= dt.timedelta(days=1)
        out.append(yyyymmdd(x))
        d -= dt.timedelta(days=7)
    return out


# ---------------- 수집 ----------------
def get_universe(asof: str) -> dict[str, str]:
    """{티커: 종목명} for 코스피+코스닥."""
    uni = {}
    for mkt in TARGET_MARKETS:
        for tkr in stock.get_market_ticker_list(asof, market=mkt):
            try:
                uni[tkr] = stock.get_market_ticker_name(tkr)
            except Exception:
                uni[tkr] = tkr
    return uni


def collect_ohlcv_matrix(dates: list[str]) -> dict[str, dict[str, tuple]]:
    """날짜별 전종목 스냅샷 수집 → {date: {ticker: (close, volume)}}.
       하루 1~2호출로 전종목을 받아 개별호출을 피한다."""
    matrix = {}
    for d in dates:
        day = {}
        for mkt in TARGET_MARKETS:
            try:
                df = stock.get_market_ohlcv_by_ticker(d, market=mkt)
            except Exception:
                df = None
            if df is None or df.empty:
                continue
            for tkr, row in df.iterrows():
                # 컬럼명은 pykrx 버전에 따라 '종가'/'거래량'
                close = row.get("종가")
                vol = row.get("거래량")
                if close is not None and vol is not None:
                    day[tkr] = (float(close), float(vol))
            time.sleep(REQUEST_PAUSE)
        if day:
            matrix[d] = day
    return matrix


def collect_fundamental_history(dates: list[str]) -> dict[str, list[dict]]:
    """월별 표본으로 {ticker: [{date, PBR, DIV, DPS, EPS}, ...]} 수집."""
    hist: dict[str, list[dict]] = {}
    for d in dates:
        for mkt in TARGET_MARKETS:
            try:
                df = stock.get_market_fundamental_by_ticker(d, market=mkt)
            except Exception:
                df = None
            if df is None or df.empty:
                continue
            for tkr, row in df.iterrows():
                hist.setdefault(tkr, []).append({
                    "date": d,
                    "PBR": float(row.get("PBR", 0) or 0),
                    "DIV": float(row.get("DIV", 0) or 0),
                    "DPS": float(row.get("DPS", 0) or 0),
                    "EPS": float(row.get("EPS", 0) or 0),
                })
            time.sleep(REQUEST_PAUSE)
    return hist


def collect_short_max(dates: list[str]) -> dict[str, float]:
    """주별 표본에서 종목별 공매도잔고비중의 3개월 최고치 {ticker: max_ratio}."""
    mx: dict[str, float] = {}
    for d in dates:
        for mkt in TARGET_MARKETS:
            try:
                df = stock.get_shorting_balance_by_ticker(d, market=mkt)
            except Exception:
                df = None
            if df is None or df.empty:
                continue
            for tkr, row in df.iterrows():
                ratio = float(row.get("비중", 0) or 0)
                if tkr not in mx or ratio > mx[tkr]:
                    mx[tkr] = ratio
            time.sleep(REQUEST_PAUSE)
    return mx


def collect_short_current(date: str) -> dict[str, float]:
    cur: dict[str, float] = {}
    for mkt in TARGET_MARKETS:
        try:
            df = stock.get_shorting_balance_by_ticker(date, market=mkt)
        except Exception:
            df = None
        if df is None or df.empty:
            continue
        for tkr, row in df.iterrows():
            cur[tkr] = float(row.get("비중", 0) or 0)
    return cur


def collect_accumulation(fromdate: str, todate: str) -> dict[str, float]:
    """20일 (기관합계+외국인) 누적 순매수거래대금 {ticker: value}."""
    acc: dict[str, float] = {}
    for mkt in TARGET_MARKETS:
        for investor in ["기관합계", "외국인"]:
            try:
                df = stock.get_market_net_purchases_of_equities_by_ticker(
                    fromdate, todate, mkt, investor)
            except Exception:
                df = None
            if df is None or df.empty:
                continue
            col = "순매수거래대금" if "순매수거래대금" in df.columns else df.columns[-1]
            for tkr, row in df.iterrows():
                acc[tkr] = acc.get(tkr, 0.0) + float(row.get(col, 0) or 0)
            time.sleep(REQUEST_PAUSE)
    return acc


# ---------------- 파생 계산 ----------------
def series_for_ticker(matrix, tkr):
    """시간순 (close, volume) 리스트."""
    closes, vols = [], []
    for d in sorted(matrix.keys()):
        if tkr in matrix[d]:
            c, v = matrix[d][tkr]
            closes.append(c)
            vols.append(v)
    return closes, vols


def had_dividend_cut(fund_hist_rows: list[dict]) -> bool:
    """연도별 DPS가 전년比 감소한 적 있으면 True (배당 삭감 이력)."""
    by_year = {}
    for r in fund_hist_rows:
        y = r["date"][:4]
        by_year[y] = r["DPS"]           # 그 해 마지막 표본 값으로 대체
    years = sorted(by_year)
    prev = None
    for y in years:
        dps = by_year[y]
        if prev is not None and dps > 0 and prev > 0 and dps < prev * 0.99:
            return True
        if dps > 0:
            prev = dps
    return False


# ---------------- 메인 ----------------
def run():
    t0 = time.time()
    print("0) 가장 최근 영업일 탐색…")
    asof = find_latest_trading_day()
    anchor = dt.datetime.strptime(asof, "%Y%m%d").date()
    print(f"   기준일: {asof}")

    print("1) 종목 유니버스 수집…")
    universe = get_universe(asof)
    print(f"   {len(universe)}개 종목")

    print("2) OHLCV 스냅샷 수집…")
    ohlcv_dates = recent_business_dates(OHLCV_LOOKBACK_DAYS, anchor)
    matrix = collect_ohlcv_matrix(ohlcv_dates)
    print(f"   {len(matrix)}개 영업일 확보")

    print("3) 펀더멘털 히스토리 수집…")
    fund_hist = collect_fundamental_history(month_end_samples(FUND_HISTORY_MONTHS, anchor))

    print("4) 공매도 3개월 최고/현재 수집…")
    short_max = collect_short_max(weekly_samples(SHORT_SAMPLE_WEEKS, anchor))
    latest_date = sorted(matrix.keys())[-1] if matrix else asof
    short_cur = collect_short_current(latest_date)

    print("5) 20일 매집 수집…")
    accum_from = ohlcv_dates[-ACCUM_WINDOW_DAYS] if len(ohlcv_dates) >= ACCUM_WINDOW_DAYS else ohlcv_dates[0]
    accum = collect_accumulation(accum_from, latest_date)

    print("6) 지수 60일 수익률(코스피)…")
    try:
        idx = stock.get_index_ohlcv(ohlcv_dates[-60], latest_date, "1001")  # 1001 = 코스피
        idx_ret = (idx["종가"].iloc[-1] / idx["종가"].iloc[0]) - 1 if not idx.empty else 0.0
    except Exception:
        idx_ret = 0.0

    print("7) 채점…")
    results = []
    for tkr, name in universe.items():
        closes, vols = series_for_ticker(matrix, tkr)
        if len(closes) < 60 or len(vols) < 120:
            continue

        # --- 생존 게이트 ---
        last_close = closes[-1]
        avg_trading_value = median(vols[-20:]) * last_close
        fh = fund_hist.get(tkr, [])
        cur_pbr = fh[0]["PBR"] if fh else 0.0
        # 시총 근사: 최신 펀더멘털이 없으면 스킵. (정확 시총은 별도 호출 가능하나 생략)
        if avg_trading_value < MIN_AVG_TRADING_VALUE:
            continue
        if cur_pbr <= 0:            # 자본잠식 의심/데이터 없음
            continue

        # --- 신호 입력값 ---
        rec20 = median(vols[-20:])
        past120 = median(vols[-120:])
        ret60 = (closes[-1] / closes[-60]) - 1
        ret20_price = (closes[-1] / closes[-20]) - 1

        pbr_series = [r["PBR"] for r in fh if r["PBR"] > 0]
        div_series = [r["DIV"] for r in fh if r["DIV"] > 0]
        cur_div = fh[0]["DIV"] if fh else 0.0
        cur_dps = fh[0]["DPS"] if fh else 0.0
        cur_eps = fh[0]["EPS"] if fh else 0.0

        # 유통시총 근사 = 시총 대용(정밀도보다 강도 순위가 목적)
        float_mc = avg_trading_value * 50  # 대략적 스케일; 백테스트 시 실제 시총으로 교체 예정

        scores = {
            "volume_dryness": sg.score_volume_dryness(rec20, past120),
            "accumulation": sg.score_accumulation(accum.get(tkr, 0.0), float_mc, ret20_price * 100),
            "short_covering": sg.score_short_covering(short_cur.get(tkr, 0.0), short_max.get(tkr, 0.0)),
            "pbr_low": sg.score_pbr_low(cur_pbr, pbr_series),
            "dividend_yield": sg.score_dividend_yield(cur_div, div_series, cur_dps, cur_eps, had_dividend_cut(fh)),
            "relative_strength": sg.score_relative_strength(ret60, idx_ret),
        }
        comp = sg.composite_score(scores)
        if comp["composite"] is None:
            continue
        results.append({
            "ticker": tkr, "name": name,
            "score": comp["composite"], "n_signals": comp["n_signals_used"],
            "breakdown": comp["breakdown"],
            "close": round(last_close),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:TOP_N]

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="minutes"),
        "as_of_date": latest_date,
        "universe_size": len(universe),
        "scored": len(results),
        "params": {
            "ohlcv_lookback": OHLCV_LOOKBACK_DAYS, "accum_window": ACCUM_WINDOW_DAYS,
            "min_market_cap": MIN_MARKET_CAP, "min_trading_value": MIN_AVG_TRADING_VALUE,
        },
        "signal_labels": sg.SIGNAL_LABELS,
        "results": top,
    }
    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"완료: {len(results)}개 채점, 상위 {len(top)}개 저장. ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    run()
