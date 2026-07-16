"""
screener.py — 전종목 바닥 스크리너 파이프라인 (pykrx → signals.py → results.json)

흐름:
  1) 코스피+코스닥 종목 목록
  2) 효율적 수집: '하루치 전종목 스냅샷'을 여러 날짜에 대해 수집 (종목별 개별호출 최소화)
  3) 생존 게이트 통과 종목만 채점: 바닥 신호 7개 + 턴어라운드 신호 5개(별도 합성점수).
     둘 다 signals.py의 BOTTOM_WEIGHTS/TURNAROUND_WEIGHTS로 가중평균 —
     이 가중치는 백테스트로 검증된 값이 아니라 신호의 증거 직접성에 따른
     초기 추정값이며, 나중에 조정될 수 있다.
  4) bottom_score < 60은 제외. 나머지는 강한 턴어라운드 신호(≥50점) 2개 이상이면
     confirmed_turnaround, 아니면 watching으로 분류
  5) 상위 N개를 results.json으로 저장 (프론트가 읽음)

주의: KRX 접속이 되는 환경(예: GitHub Actions)에서 실행해야 한다.
첫 실행은 디버그 패스가 필요할 수 있다(휴장일·결측·해외IP 차단 등).

참고: 신용잔고(개별 종목)는 KRX 공식/비공식 API, 네이버금융(구/신 페이지 모두)
어디에도 무료로 종목별 조회할 방법을 찾지 못해 신호에서 제외했다.
"""
from __future__ import annotations
import json
import time
import datetime as dt
from statistics import median

from pykrx import stock
import signals as sg
import explain as ex

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

# 데이터 검증: 종가<=0 또는 전일 대비 이 배율을 넘는 하루 등락은 KRX 상하한가(±30%)
# 밖이라 정상 거래로는 불가능 — 결측/장애/미조정 액면분할 등 데이터 이상으로 보고 제외
MAX_DAILY_MOVE_RATIO = 1.32

# 최종 분류 임계값
BOTTOM_SCORE_THRESHOLD = 60            # 바닥 종합점수 이 값 미만이면 결과에서 제외
TURNAROUND_STRONG_THRESHOLD = 50       # 개별 턴어라운드 신호가 이 값 이상이면 "강함"으로 침
TURNAROUND_MIN_STRONG_SIGNALS = 2      # 강한 턴어라운드 신호가 이 개수 이상이면 confirmed_turnaround

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


MARKET_NAME_KO = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}


def get_sector_index_map(asof: str) -> dict[str, str]:
    """{티커: 업종지수코드}. 코스피/코스닥 각 시장의 전체 지수 목록에서
       이름에 시장명("코스피"/"코스닥")이 들어간 것(코스피 200, 코스피 대형주,
       코스닥 150 소재 등 사이즈·스타일·서브 지수)은 제외하고, 이름에 시장명이
       없는 것(화학·전기전자·유통 같은 순수 업종지수)만 남긴다.
       종목 개수만큼이 아니라 지수 개수(수십 개)만큼만 호출하는 벌크 방식."""
    ticker_to_sector: dict[str, str] = {}
    for mkt in TARGET_MARKETS:
        market_name = MARKET_NAME_KO[mkt]
        try:
            idx_codes = stock.get_index_ticker_list(asof, market=mkt)
        except Exception:
            continue
        for code in idx_codes:
            try:
                name = stock.get_index_ticker_name(code)
            except Exception:
                continue
            if market_name in name:      # 사이즈/스타일/서브 지수 — 순수 업종 아님
                continue
            try:
                members = stock.get_index_portfolio_deposit_file(code)
            except Exception:
                continue
            for tkr in members:
                ticker_to_sector[tkr] = code
    return ticker_to_sector


def collect_sector_index_ohlcv(sector_codes: set[str], fromdate: str, todate: str) -> dict[str, dict[str, float]]:
    """{업종지수코드: {날짜: 종가}}. 코스피 전체지수("1001")와 같은 방식으로,
       매핑에 실제로 쓰이는 업종지수만 전체 구간 히스토리를 받는다."""
    out: dict[str, dict[str, float]] = {}
    for code in sector_codes:
        try:
            df = stock.get_index_ohlcv(fromdate, todate, code)
        except Exception:
            df = None
        out[code] = index_close_by_date(df) if df is not None else {}
        time.sleep(REQUEST_PAUSE)
    return out


def resolve_benchmark_series(tkr: str, sector_map: dict[str, str],
                             sector_idx_by_date: dict[str, dict[str, float]],
                             market_idx_by_date: dict[str, float]) -> tuple[dict[str, float], str]:
    """종목의 업종지수 날짜별 종가 시계열을 우선 쓰고, 매핑이 없거나 그 업종지수
       데이터를 못 받았으면 코스피 전체 지수로 폴백한다. (label, series) 대신
       (series, label) 순서로 반환 — label은 진단/설명용."""
    code = sector_map.get(tkr)
    if code:
        series = sector_idx_by_date.get(code)
        if series:
            return series, f"sector:{code}"
    return market_idx_by_date, "market:1001"


def collect_ohlcv_matrix(dates: list[str]) -> dict[str, dict[str, tuple]]:
    """날짜별 전종목 스냅샷 수집 → {date: {ticker: (close, volume)}}.
       하루 1~2호출로 전종목을 받아 개별호출을 피한다.

       종가<=0(결측/장애 데이터)이거나 전일 대비 ±30%(KRX 상하한가)를 넘는
       하루 등락은 물리적으로 불가능한 정상 거래이므로, 그 종목의 그 날짜만
       스냅샷에서 제외한다. (액면분할·병합처럼 실제 기업행동으로 인한 가격
       불연속도 같은 방식으로 걸러진다 — 소급 조정은 하지 않고 그냥 배제.
       그 이후 데이터가 계속 이상하면 해당 종목은 자연히 60일 데이터 기준을
       못 채워 채점 대상에서 빠진다.)"""
    matrix = {}
    last_close: dict[str, float] = {}
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
                if close is None or vol is None:
                    continue
                close = float(close)
                vol = float(vol)
                if close <= 0:
                    continue
                prev = last_close.get(tkr)
                if prev is not None and prev > 0:
                    ratio = close / prev
                    if ratio > MAX_DAILY_MOVE_RATIO or ratio < 1 / MAX_DAILY_MOVE_RATIO:
                        continue   # 데이터 이상으로 판단, 이 종목의 이 날짜만 제외
                day[tkr] = (close, vol)
                last_close[tkr] = close
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
    """시간순 (date, close, volume) 병렬 리스트. date는 상대강도 가속 계산 시
       지수 종가와 같은 날짜끼리 짝짓기 위해 필요하다."""
    dates, closes, vols = [], [], []
    for d in sorted(matrix.keys()):
        if tkr in matrix[d]:
            c, v = matrix[d][tkr]
            dates.append(d)
            closes.append(c)
            vols.append(v)
    return dates, closes, vols


def index_close_by_date(idx_df) -> dict[str, float]:
    """지수 OHLCV 데이터프레임 → {YYYYMMDD: 종가}."""
    out: dict[str, float] = {}
    if idx_df is None or idx_df.empty:
        return out
    for idx_date, close in idx_df["종가"].items():
        try:
            key = idx_date.strftime("%Y%m%d")
        except AttributeError:
            key = str(idx_date)
        out[key] = float(close)
    return out


def bollinger_bandwidth_series(closes: list[float], window: int = 20) -> list[float]:
    """일별 볼린저밴드 폭(상대값, (상단-하단)/중앙선) 시계열. 마지막 값이 최신."""
    out = []
    for i in range(window, len(closes) + 1):
        w = closes[i - window:i]
        m = sum(w) / window
        if m <= 0:
            continue
        var = sum((x - m) ** 2 for x in w) / window
        sd = var ** 0.5
        out.append((4 * sd) / m)   # (m+2sd) - (m-2sd) = 4sd, 상대화 위해 /m
    return out


def has_unadjusted_split_jump(closes: list[float], window: int = 60,
                              up_ratio: float = 2.0, down_ratio: float = 0.5) -> bool:
    """최근 window일 구간에 전일 대비 up_ratio배 이상 급등 또는 down_ratio배
       이하 급락한 지점이 있으면 True. pykrx의 전종목 스냅샷 함수는 수정주가를
       제공하지 않고(adjusted 옵션 자체가 없음), collect_ohlcv_matrix의 ±30%
       상하한가 필터가 대부분의 액면분할·무상증자성 불연속을 이미 걸러내지만,
       그 필터를 뚫고 들어온 잔존 이상치를 상대강도·이평선 신호 계산 직전에
       한 번 더 방어한다."""
    recent = closes[-window:]
    for i in range(1, len(recent)):
        if recent[i - 1] <= 0:
            continue
        ratio = recent[i] / recent[i - 1]
        if ratio >= up_ratio or ratio <= down_ratio:
            return True
    return False


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

    print("1b) 업종지수 매핑 수집…")
    sector_map = get_sector_index_map(asof)
    print(f"   {len(sector_map)}개 종목이 업종지수에 매핑됨")

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

    print("5) 매집 수집(20일 / 최근5일 / 이전15일)…")
    accum_from = ohlcv_dates[-ACCUM_WINDOW_DAYS] if len(ohlcv_dates) >= ACCUM_WINDOW_DAYS else ohlcv_dates[0]
    accum = collect_accumulation(accum_from, latest_date)
    # 매집 가속(턴어라운드) 계산용: 최근 5일 vs 그 이전 15일을 별도 수집
    accum_recent5_from = ohlcv_dates[-5] if len(ohlcv_dates) >= 5 else ohlcv_dates[0]
    accum_recent5 = collect_accumulation(accum_recent5_from, latest_date)
    accum_prior15_from = ohlcv_dates[-20] if len(ohlcv_dates) >= 20 else ohlcv_dates[0]
    accum_prior15_to = ohlcv_dates[-6] if len(ohlcv_dates) >= 6 else ohlcv_dates[0]
    accum_prior15 = collect_accumulation(accum_prior15_from, accum_prior15_to)

    print("6) 지수 수익률(코스피)…")
    try:
        # 상대강도 가속(턴어라운드)의 날짜별 조회를 위해 전체 구간을 받아 두되,
        # 기존 60일 상대강도(바닥 신호)는 이전과 동일하게 60일 구간만 사용한다.
        idx = stock.get_index_ohlcv(ohlcv_dates[0], latest_date, "1001")  # 1001 = 코스피
        idx_by_date = index_close_by_date(idx)
        idx_c_latest = idx_by_date.get(latest_date)
        idx_c_60ago = idx_by_date.get(ohlcv_dates[-60])
        idx_ret = (idx_c_latest / idx_c_60ago - 1) if idx_c_latest and idx_c_60ago else 0.0
    except Exception:
        idx_ret = 0.0
        idx_by_date = {}

    print("6b) 업종지수 OHLCV 수집…")
    sector_codes_needed = set(sector_map.values())
    sector_idx_by_date = collect_sector_index_ohlcv(sector_codes_needed, ohlcv_dates[0], latest_date)
    print(f"   업종지수 {len(sector_codes_needed)}개 중 "
          f"{sum(1 for v in sector_idx_by_date.values() if v)}개 데이터 확보")

    print("7) 채점(바닥 7개 + 턴어라운드 5개 신호)…")
    results = []
    outlier_count = 0  # 임시 진단: 60일 수익률 +100% 이상 잔존 이상치 확인용, 확인 후 제거
    split_flag_count = 0  # 임시 진단: ±30% 필터를 뚫은 잔존 분할/증자 의심 종목 수, 확인 후 제거
    for tkr, name in universe.items():
        dates, closes, vols = series_for_ticker(matrix, tkr)
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

        # 상대강도·이평선 신호는 60일 구간 내 미조정 분할/증자성 불연속에 취약하므로,
        # collect_ohlcv_matrix의 ±30% 필터를 뚫고 들어온 잔존 이상치를 여기서 한 번 더 확인
        split_suspected = has_unadjusted_split_jump(closes)
        if split_suspected:
            split_flag_count += 1
            print(f"   [진단:분할의심] {name}({tkr}) 최근 60일 내 전일 대비 2배↑/0.5배↓ 지점 발견 "
                  f"— relative_strength/ma_breakout/short_term_breakout None 처리")

        # --- 신호 입력값 ---
        # 거래량 고갈(①)은 최근 5일을 일부러 제외한 6~25일 전 구간을 본다 —
        # 턴어라운드 신호 '거래량 동반 상승'(⑨, 최근 5일 vs 최근 20일)과
        # 구간이 겹치지 않게 하기 위함. 자세한 이유는 signals.py 참고.
        rec6to25 = median(vols[-25:-5])
        past120 = median(vols[-120:])
        ret60 = (closes[-1] / closes[-60]) - 1
        ret20_price = (closes[-1] / closes[-20]) - 1

        # 상대강도는 코스피 전체가 아니라 그 종목의 업종지수 대비로 본다
        # (매핑 없거나 업종지수 데이터가 없으면 코스피 전체로 폴백).
        bench_series, bench_label = resolve_benchmark_series(tkr, sector_map, sector_idx_by_date, idx_by_date)
        bench_c_latest = bench_series.get(latest_date)
        bench_c_60ago = bench_series.get(ohlcv_dates[-60])
        idx_ret_t = (bench_c_latest / bench_c_60ago - 1) if bench_c_latest and bench_c_60ago else idx_ret

        if ret60 > 1.0:  # 임시 진단: +100% 이상 잔존 이상치 확인용, 확인 후 제거
            outlier_count += 1
            try:
                expected_days = ohlcv_dates.index(dates[-1]) - ohlcv_dates.index(dates[0]) + 1
            except ValueError:
                expected_days = None
            missing = (expected_days - len(dates)) if expected_days is not None else None
            print(f"   [진단:이상치] {name}({tkr}) ret60={ret60:.2f} "
                  f"60일전({dates[-60]})={closes[-60]:.0f} 현재({dates[-1]})={closes[-1]:.0f} "
                  f"보유일수={len(dates)} 예상거래일={expected_days} 결측추정={missing}")

        pbr_series = [r["PBR"] for r in fh if r["PBR"] > 0]
        div_series = [r["DIV"] for r in fh if r["DIV"] > 0]
        cur_div = fh[0]["DIV"] if fh else 0.0
        cur_dps = fh[0]["DPS"] if fh else 0.0
        cur_eps = fh[0]["EPS"] if fh else 0.0
        bw_series = bollinger_bandwidth_series(closes)

        # 유통시총 근사 = 시총 대용(정밀도보다 강도 순위가 목적)
        float_mc = avg_trading_value * 50  # 대략적 스케일; 백테스트 시 실제 시총으로 교체 예정

        # --- 바닥 신호 7개: "매도세 소진·역사적으로 싸다"만 본다 ---
        scores = {
            "volume_dryness": sg.score_volume_dryness(rec6to25, past120),
            "accumulation": sg.score_accumulation(accum.get(tkr, 0.0), float_mc, ret20_price * 100),
            "short_covering": sg.score_short_covering(short_cur.get(tkr, 0.0), short_max.get(tkr, 0.0)),
            "pbr_low": sg.score_pbr_low(cur_pbr, pbr_series),
            "dividend_yield": sg.score_dividend_yield(cur_div, div_series, cur_dps, cur_eps, had_dividend_cut(fh)),
            "relative_strength": None if split_suspected else sg.score_relative_strength(ret60, idx_ret_t),
            "volatility_squeeze": sg.score_volatility_squeeze(bw_series),
        }

        # --- 정규화 전 원본 수치 (설명 문장 생성용, explain.py가 사용) ---
        net_buy_20d = accum.get(tkr, 0.0)
        cur_short = short_cur.get(tkr, 0.0)
        max_short = short_max.get(tkr, 0.0)
        cur_bw = bw_series[-1] if bw_series else None
        raw = {
            "volume_dryness": {"ratio": (rec6to25 / past120) if past120 else None},
            "accumulation": {
                "net_buy_krw": net_buy_20d,
                "intensity_pct": (net_buy_20d / float_mc * 100) if float_mc else None,
                "price_change_pct": ret20_price * 100,
            },
            "short_covering": {
                "current_ratio_pct": cur_short, "max_ratio_3m_pct": max_short,
                "pct_of_max": (cur_short / max_short * 100) if max_short else None,
            },
            "pbr_low": {"pbr": cur_pbr, "percentile": sg.percentile_rank(pbr_series, cur_pbr)},
            "dividend_yield": {"div_pct": cur_div, "percentile": sg.percentile_rank(div_series, cur_div)},
            "relative_strength": {
                "stock_ret_pct": ret60 * 100, "index_ret_pct": idx_ret_t * 100,
                "excess_pct": (ret60 - idx_ret_t) * 100, "benchmark": bench_label,
            },
            "volatility_squeeze": {
                "bandwidth_pct": (cur_bw * 100) if cur_bw is not None else None,
                "percentile": sg.percentile_rank(bw_series, cur_bw) if cur_bw is not None else None,
            },
        }

        comp = sg.composite_score(scores, sg.BOTTOM_WEIGHTS)
        if comp["composite"] is None or comp["composite"] < BOTTOM_SCORE_THRESHOLD:
            continue

        # --- 턴어라운드 신호 5개: "실제로 방향을 틀었는지"만 본다 (바닥 신호와 끝까지 분리) ---
        turnaround_scores = None
        turnaround_comp = None
        turnaround_raw = None
        if len(closes) >= 21 and len(dates) >= 21:
            recent5_avg_vol = sum(vols[-5:]) / 5
            recent20_avg_vol = sum(vols[-20:]) / 20
            ma20 = sum(closes[-20:]) / 20
            ma60 = sum(closes[-60:]) / 60
            high60 = max(closes[-60:])

            stock_ret_recent10 = (closes[-1] / closes[-11]) - 1
            stock_ret_prior10 = (closes[-11] / closes[-21]) - 1
            idx_c1, idx_c11, idx_c21 = (bench_series.get(dates[-1]),
                                        bench_series.get(dates[-11]),
                                        bench_series.get(dates[-21]))
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
            turnaround_comp = sg.composite_score(turnaround_scores, sg.TURNAROUND_WEIGHTS)

            rs_recent10 = (stock_ret_recent10 - index_ret_recent10) if index_ret_recent10 is not None else None
            rs_prior10 = (stock_ret_prior10 - index_ret_prior10) if index_ret_prior10 is not None else None
            turnaround_raw = {
                "volume_surge": {"ratio": (recent5_avg_vol / recent20_avg_vol) if recent20_avg_vol else None},
                "ma_breakout": {
                    "close": closes[-1], "ma20": ma20, "ma60": ma60,
                    "close_vs_ma60_pct": ((closes[-1] - ma60) / ma60 * 100) if ma60 else None,
                    "ma20_vs_ma60_pct": ((ma20 - ma60) / ma60 * 100) if ma60 else None,
                },
                "short_term_breakout": {
                    "close": closes[-1], "high60": high60,
                    "pct_of_high60": (closes[-1] / high60 * 100) if high60 else None,
                },
                "relative_strength_accel": {
                    "rs_recent10_pct": (rs_recent10 * 100) if rs_recent10 is not None else None,
                    "rs_prior10_pct": (rs_prior10 * 100) if rs_prior10 is not None else None,
                    "benchmark": bench_label,
                    "accel_pct": ((rs_recent10 - rs_prior10) * 100)
                        if rs_recent10 is not None and rs_prior10 is not None else None,
                },
                "accumulation_accel": {
                    "recent5_avg_krw": net_buy_recent5_avg, "prior15_avg_krw": net_buy_prior15_avg,
                    "ratio": (net_buy_recent5_avg / net_buy_prior15_avg)
                        if net_buy_prior15_avg and net_buy_prior15_avg > 0 else None,
                },
            }

        # --- 최종 분류: 바닥(≥60점) 중에서 턴어라운드가 확인됐는지 ---
        strong_turnaround_count = sum(
            1 for v in (turnaround_scores or {}).values()
            if v is not None and v >= TURNAROUND_STRONG_THRESHOLD
        )
        status = ("confirmed_turnaround"
                  if strong_turnaround_count >= TURNAROUND_MIN_STRONG_SIGNALS
                  else "watching")

        item = {
            "ticker": tkr, "name": name,
            "score": comp["composite"], "n_signals": comp["n_signals_used"],
            "breakdown": comp["breakdown"],
            "raw": raw,
            "turnaround_score": turnaround_comp["composite"] if turnaround_comp else None,
            "n_turnaround_signals": turnaround_comp["n_signals_used"] if turnaround_comp else 0,
            "turnaround_breakdown": turnaround_comp["breakdown"] if turnaround_comp else None,
            "turnaround_raw": turnaround_raw,
            "status": status,
            "close": round(last_close),
            "split_suspected": split_suspected,
        }
        item["explanation"] = ex.explain_result(item)
        results.append(item)

    print(f"   [진단:이상치] 총 {outlier_count}개 종목이 생존 게이트 통과 종목 중 60일 +100% 이상")
    print(f"   [진단:분할의심] 총 {split_flag_count}개 종목이 ±30% 필터를 뚫은 잔존 분할/증자 의심"
          f"(relative_strength/ma_breakout/short_term_breakout None 처리됨)")

    # confirmed_turnaround를 먼저, 그 다음 watching — 각 그룹 내에서는 바닥 점수 내림차순
    results.sort(key=lambda x: (x["status"] != "confirmed_turnaround", -x["score"]))
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
        "turnaround_signal_labels": sg.TURNAROUND_SIGNAL_LABELS,
        "results": top,
    }
    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"완료: {len(results)}개 채점, 상위 {len(top)}개 저장. ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    run()
