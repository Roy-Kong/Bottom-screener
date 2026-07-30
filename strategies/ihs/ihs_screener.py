"""strategies/ihs/ihs_screener.py — 역헤드앤숄더(Inverse Head & Shoulders) 패턴 검출.

IHS_SPEC.md 기준 구현. 이 모듈은 signals.py/screener.py의 바닥/턴어라운드
12개 시그널 체계와 **완전히 독립**이다(사용자 지시) — 섞지 않고, 역헤드앤숄더
패턴 하나만으로 스크리닝·시뮬레이션하는 별도 도구를 만든다.

[핵심 원칙 — 룩어헤드 방지]
프랙탈 피봇은 오른쪽으로 order개 봉이 더 있어야 확정된다. as_of가 주어지면
그 시점까지만 자르고, 잘린 데이터의 마지막 인덱스 기준 order개 이내의
피봇은 전부 버린다 — "오른쪽 어깨가 형성된 당일"이 아니라 "order일 뒤"에
신호가 뜨는 게 의도된 동작이다(detect_ihs 참고).

detect_ihs()는 순수 함수(DB/파일 IO 없음) — 백테스트에서 수만 번 불릴 수
있어서 계산만 한다. DB 조회는 scan_universe()에서만 한다.

가격 비교(어깨대칭·넥라인기울기 등)는 전부 로그 스케일(ln)로 한다 — 저가주와
고가주 간 스케일 왜곡 방지.
"""
from __future__ import annotations
import math
import sys
import datetime as dt
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ==================== 설정 ====================

@dataclass
class IHSConfig:
    # 피봇 검출
    order: int = 5                     # 프랙탈 윈도우 반경(2*order+1)
    min_swing_atr: float = 1.0         # 인접 피봇 최소 진폭(ATR14 배수) — 스펙에 기본값 미명시, 1.0으로 가정(문서화)
    atr_window: int = 14

    # 패턴 기간/기하
    min_pattern_days: int = 25
    max_pattern_days: int = 220
    head_prominence: float = 0.030
    shoulder_sym_tol: float = 0.12
    time_sym_min: float = 0.40
    time_sym_max: float = 2.50
    neckline_slope_tol: float = 0.30
    min_depth: float = 0.08
    max_depth: float = 0.80
    prior_lookback: int = 60
    prior_decline: float = 0.12

    # 상태/돌파
    breakout_buffer: float = 0.005
    retest_tol: float = 0.02           # 스펙에 기본값 미명시, 2%로 가정(문서화)
    breakout_max_age: int = 15

    # 매매 레벨
    stop_neck_buffer: float = 0.97     # min(RS, neck(last)*0.97)

    # 스코어 가중치(합 1.0)
    w_shoulder_sym: float = 0.20
    w_time_sym: float = 0.10
    w_head_prominence: float = 0.15
    w_pattern_depth: float = 0.15
    w_neckline_flat: float = 0.10
    w_breakout_vol: float = 0.20
    w_prior_decline: float = 0.10
    forming_discount: float = 0.85

    # 스코어 정규화용 "만점 도달 배수"(스펙이 정규화 곡선을 안 정해줘서, 각
    # 항목의 최소통과기준 대비 몇 배에서 만점(1.0)을 주는지 — 문서화된 가정)
    head_prominence_saturate_mult: float = 3.0   # head_prominence*3 이상이면 1.0
    breakout_vol_saturate_ratio: float = 2.0      # 거래대금비율 2.0배 이상이면 1.0
    prior_decline_saturate_mult: float = 2.0      # prior_decline*2 이상이면 1.0

    # DB 컬럼 매핑
    date_col: str = "date"
    ticker_col: str = "ticker"
    open_col: str = "open"
    high_col: str = "high"
    low_col: str = "low"
    close_col: str = "close"
    volume_col: str = "volume"

    # scan_universe 유동성/품질 필터
    min_avg_trading_value: float = 500_000_000     # 20일 평균 거래대금 5억원
    min_close: float = 1000.0
    min_bars: int = 150
    liquidity_window: int = 20


# ==================== 결과 ====================

Status = Literal["forming", "breakout", "retest", "failed"]


@dataclass
class IHSPattern:
    ticker: str
    status: Status
    score: float
    d_ls: str
    d_head: str
    d_rs: str
    d_breakout: str | None
    ls: float
    head: float
    rs: float
    p1: float
    p2: float
    neckline_now: float
    close: float
    depth: float
    shoulder_sym: float
    time_sym: float
    neck_slope: float
    breakout_vol_ratio: float | None
    prior_decline: float
    days_since_breakout: int | None
    target: float
    stop: float
    rr: float
    gap_warning: bool = False          # 패턴 구간 내 5%+ 갭(미수정주가 의심)
    missing_bar_ratio: float = 0.0     # 패턴 구간 내 결측(거래정지 추정) 비율


# ==================== 피봇(스윙) 검출 ====================

@dataclass
class _Pivot:
    idx: int
    price: float
    kind: str  # "low" | "high"


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int) -> np.ndarray:
    """단순(비지수) ATR — 전일종가 기준 True Range의 rolling mean.
       cumsum 기반으로 완전히 벡터화(종목당 detect_ihs 호출이 스캔/시뮬레이션에서
       수만 번 반복되므로, 봉당 numpy 함수 호출 오버헤드가 지배적이던 이전
       파이썬 루프 버전 대비 크게 빠르다 — 프로파일링으로 확인한 실제 병목)."""
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    if n > 1:
        prev_close = close[:-1]
        tr[1:] = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)))
    csum = np.concatenate(([0.0], np.cumsum(tr)))  # csum[i] = sum(tr[0:i])

    atr = np.full(n, np.nan)
    if n >= window:
        idx = np.arange(window - 1, n)
        atr[idx] = (csum[idx + 1] - csum[idx + 1 - window]) / window
    # 초반 구간(window 미만)은 그때까지의 확장 평균으로 채워서 NaN 전파 방지
    early_n = min(window - 1, n)
    if early_n > 0:
        early_idx = np.arange(early_n)
        atr[early_idx] = csum[early_idx + 1] / (early_idx + 1)
    return atr


def _find_fractals(high: np.ndarray, low: np.ndarray, order: int) -> list[_Pivot]:
    """윈도우 2*order+1, center=True 롤링 min/max와 일치하는 봉을 저점/고점 후보로.
       numpy sliding_window_view로 벡터화(pandas rolling의 Series 생성
       오버헤드도 없어서 detect_ihs가 스캔/시뮬레이션에서 수만 번 불릴 때
       더 빠르다 — 프로파일링으로 확인)."""
    n = len(high)
    win = 2 * order + 1
    if n < win:
        return []
    low_windows = np.lib.stride_tricks.sliding_window_view(low, win)
    high_windows = np.lib.stride_tricks.sliding_window_view(high, win)
    roll_min = low_windows.min(axis=1)   # roll_min[k] <-> 원본 인덱스 k+order
    roll_max = high_windows.max(axis=1)

    pivots: list[_Pivot] = []
    for k in range(len(roll_min)):
        i = k + order
        if low[i] == roll_min[k]:
            pivots.append(_Pivot(i, float(low[i]), "low"))
        if high[i] == roll_max[k]:
            pivots.append(_Pivot(i, float(high[i]), "high"))
    pivots.sort(key=lambda p: p.idx)
    return pivots


def _alternate(pivots: list[_Pivot]) -> list[_Pivot]:
    """같은 타입이 연속되면 더 극단적인 것만 남겨 저-고-저-고로 정규화."""
    if not pivots:
        return []
    out: list[_Pivot] = [pivots[0]]
    for p in pivots[1:]:
        last = out[-1]
        if p.kind == last.kind:
            if p.kind == "low":
                if p.price < last.price:
                    out[-1] = p
            else:
                if p.price > last.price:
                    out[-1] = p
        else:
            out.append(p)
    return out


def _remove_micro_swings(pivots: list[_Pivot], atr: np.ndarray, min_swing_atr: float) -> list[_Pivot]:
    """인접 피봇 간 진폭이 min_swing_atr*ATR 미만이면 더 작은 쪽(뒤 피봇)을
       제거하고 재교대. 변화 없을 때까지 반복."""
    pivots = list(pivots)
    while True:
        if len(pivots) < 2:
            return pivots
        amplitudes = []
        for i in range(len(pivots) - 1):
            a, b = pivots[i], pivots[i + 1]
            thr_atr = atr[b.idx] if not np.isnan(atr[b.idx]) else atr[a.idx]
            amp = abs(b.price - a.price)
            threshold = min_swing_atr * thr_atr if not np.isnan(thr_atr) else 0.0
            amplitudes.append((amp - threshold, i))
        amplitudes.sort(key=lambda t: t[0])
        worst_slack, worst_i = amplitudes[0]
        if worst_slack >= 0:
            return pivots  # 전부 임계 이상 — 더 지울 게 없음
        # 진폭이 가장 작은(임계 미달이 가장 심한) 쌍에서 뒤 피봇을 제거
        del pivots[worst_i + 1]
        pivots = _alternate(pivots)


def _detect_pivots(df: pd.DataFrame, cfg: IHSConfig) -> list[_Pivot]:
    high = df[cfg.high_col].to_numpy(dtype=float)
    low = df[cfg.low_col].to_numpy(dtype=float)
    close = df[cfg.close_col].to_numpy(dtype=float)
    atr = _atr(high, low, close, cfg.atr_window)
    raw = _find_fractals(high, low, cfg.order)
    alternated = _alternate(raw)
    prev_len = -1
    cleaned = alternated
    while len(cleaned) != prev_len:
        prev_len = len(cleaned)
        cleaned = _remove_micro_swings(cleaned, atr, cfg.min_swing_atr)
        cleaned = _alternate(cleaned)
    return cleaned


# ==================== 패턴 기하 매칭 ====================

def _neck(i: int, i_p1: int, p1: float, i_p2: int, p2: float) -> float:
    if i_p2 == i_p1:
        return p1
    return p1 + (p2 - p1) / (i_p2 - i_p1) * (i - i_p1)


def _check_candidate(dates: list[str], closes: np.ndarray, lows: np.ndarray, highs: np.ndarray,
                     values: np.ndarray, ls: _Pivot, p1: _Pivot, head: _Pivot, p2: _Pivot, rs: _Pivot,
                     cfg: IHSConfig) -> dict | None:
    """LS-P1-HEAD-P2-RS 5개 피봇 조합이 스펙 2.3의 전부를 만족하는지 확인.
       통과하면 기하 정보 dict, 아니면 None."""
    period = rs.idx - ls.idx
    if not (cfg.min_pattern_days <= period <= cfg.max_pattern_days):
        return None

    ln = math.log
    head_prom_ls = ln(ls.price / head.price)
    head_prom_rs = ln(rs.price / head.price)
    head_prominence = min(head_prom_ls, head_prom_rs)
    if head_prominence < cfg.head_prominence:
        return None

    # 최저가 확인: [LS,RS] 구간 저가 최솟값이 HEAD여야 함(다른 프랙탈-미검출
    # 지점이 더 낮으면 이 패턴은 무효)
    seg_low_min = lows[ls.idx:rs.idx + 1].min()
    if seg_low_min < head.price:
        return None

    shoulder_sym = abs(ln(rs.price / ls.price))
    if shoulder_sym > cfg.shoulder_sym_tol:
        return None

    time_left = head.idx - ls.idx
    time_right = rs.idx - head.idx
    if time_left <= 0 or time_right <= 0:
        return None
    time_sym = time_right / time_left
    if not (cfg.time_sym_min <= time_sym <= cfg.time_sym_max):
        return None

    neck_slope = abs(ln(p2.price / p1.price))
    if neck_slope > cfg.neckline_slope_tol:
        return None

    neck_at_head = _neck(head.idx, p1.idx, p1.price, p2.idx, p2.price)
    if neck_at_head <= 0:
        return None
    depth = (neck_at_head - head.price) / neck_at_head
    if not (cfg.min_depth <= depth <= cfg.max_depth):
        return None

    neck_at_ls = _neck(ls.idx, p1.idx, p1.price, p2.idx, p2.price)
    neck_at_rs = _neck(rs.idx, p1.idx, p1.price, p2.idx, p2.price)
    if not (ls.price < neck_at_ls and rs.price < neck_at_rs):
        return None

    prior_start = max(0, ls.idx - cfg.prior_lookback)
    if prior_start >= ls.idx:
        return None
    prior_high = highs[prior_start:ls.idx].max() if ls.idx > prior_start else None
    if prior_high is None or prior_high <= 0:
        return None
    prior_decline = (prior_high - ls.price) / prior_high
    if prior_decline < cfg.prior_decline:
        return None

    # 결측(거래정지 추정) 비율 — 패턴 구간 인덱스가 연속 정수라 결측은
    # 이 표현에서 이미 "거래일이 아예 없는 날"로 나타나지 않으므로(입력
    # df가 실제 존재하는 거래일만 담고 있다는 전제), 여기서는 캘린더 기준
    # 결측 비율을 별도 계산하지 않고 0으로 둔다 — scan_universe 단계에서
    # 원본 캘린더 대비 결측을 판단(호출부 책임).
    gap_warning = False
    for i in range(max(ls.idx, 1), rs.idx + 1):
        if closes[i - 1] > 0:
            ratio = closes[i] / closes[i - 1]
            if ratio >= 1.05 or ratio <= 0.95:
                gap_warning = True
                break

    return {
        "period": period, "head_prominence": head_prominence, "shoulder_sym": shoulder_sym,
        "time_sym": time_sym, "neck_slope": neck_slope, "depth": depth,
        "neck_at_ls": neck_at_ls, "neck_at_rs": neck_at_rs, "neck_at_head": neck_at_head,
        "prior_decline": prior_decline, "gap_warning": gap_warning,
    }


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score(geo: dict, cfg: IHSConfig, breakout_vol_ratio: float | None, is_forming: bool) -> float:
    s_shoulder = _clamp01(1 - geo["shoulder_sym"] / cfg.shoulder_sym_tol) if cfg.shoulder_sym_tol > 0 else 0.0
    time_span = max(cfg.time_sym_max - 1, 1 - cfg.time_sym_min, 1e-9)
    s_time = _clamp01(1 - abs(geo["time_sym"] - 1) / time_span)
    s_head = _clamp01(geo["head_prominence"] / (cfg.head_prominence * cfg.head_prominence_saturate_mult))
    depth_range = max(cfg.max_depth - cfg.min_depth, 1e-9)
    s_depth = _clamp01((geo["depth"] - cfg.min_depth) / depth_range)
    s_neck = _clamp01(1 - geo["neck_slope"] / cfg.neckline_slope_tol) if cfg.neckline_slope_tol > 0 else 0.0
    s_vol = _clamp01(breakout_vol_ratio / cfg.breakout_vol_saturate_ratio) if breakout_vol_ratio else 0.0
    s_prior = _clamp01(geo["prior_decline"] / (cfg.prior_decline * cfg.prior_decline_saturate_mult))

    total = (cfg.w_shoulder_sym * s_shoulder + cfg.w_time_sym * s_time + cfg.w_head_prominence * s_head +
             cfg.w_pattern_depth * s_depth + cfg.w_neckline_flat * s_neck + cfg.w_breakout_vol * s_vol +
             cfg.w_prior_decline * s_prior) * 100
    if is_forming:
        total *= cfg.forming_discount
    return round(total, 2)


# ==================== 메인 검출 함수(순수 함수) ====================

def detect_ihs(df: pd.DataFrame, cfg: IHSConfig, ticker: str = "", as_of: str | None = None) -> list[IHSPattern]:
    """df: date_col 기준 오름차순 정렬된 OHLCV(그 종목 전체 히스토리 가능).
       as_of가 주어지면 그 날짜까지만 잘라서 사용한다(룩어헤드 방지) —
       df.loc[:as_of] 방식이 아니라 date_col 문자열 비교로 자른다(인덱스가
       날짜가 아닐 수 있어서). 순수 계산만 한다 — DB/파일 IO 없음."""
    if as_of is not None:
        df = df[df[cfg.date_col] <= as_of]
    df = df.reset_index(drop=True)
    n = len(df)
    if n < 2 * cfg.order + 1:
        return []

    dates = df[cfg.date_col].astype(str).tolist()
    opens = df[cfg.open_col].to_numpy(dtype=float)
    highs = df[cfg.high_col].to_numpy(dtype=float)
    lows = df[cfg.low_col].to_numpy(dtype=float)
    closes = df[cfg.close_col].to_numpy(dtype=float)
    vols = df[cfg.volume_col].to_numpy(dtype=float)
    values = closes * vols  # 거래대금(상한가 왜곡 방지용 — 스펙 5.2)

    pivots = _detect_pivots(df, cfg)

    # 룩어헤드 차단: i > last - order 인 피봇은 전부 폐기(오른쪽에 order개
    # 봉이 아직 안 쌓여서 확정 안 된 프랙탈)
    last = n - 1
    pivots = [p for p in pivots if p.idx <= last - cfg.order]

    lows_only = [p for p in pivots if p.kind == "low"]
    if len(pivots) < 5:
        return []

    out: list[IHSPattern] = []
    seen_ls_idx: set[int] = set()
    for i in range(len(pivots) - 4):
        window = pivots[i:i + 5]
        kinds = [p.kind for p in window]
        if kinds != ["low", "high", "low", "high", "low"]:
            continue
        ls, p1, head, p2, rs = window
        if ls.idx in seen_ls_idx:
            continue  # 같은 LS로 시작하는 패턴은 가장 처음(가장 이른) 것만
        geo = _check_candidate(dates, closes, lows, highs, values, ls, p1, head, p2, rs, cfg)
        if geo is None:
            continue
        seen_ls_idx.add(ls.idx)

        # ---- 상태 판정 ----
        breakout_idx = None
        for i2 in range(rs.idx + 1, n):
            neck_i2 = _neck(i2, p1.idx, p1.price, p2.idx, p2.price)
            if closes[i2] > neck_i2 * (1 + cfg.breakout_buffer):
                breakout_idx = i2
                break

        neck_now = _neck(last, p1.idx, p1.price, p2.idx, p2.price)
        close_now = closes[last]
        status: Status
        d_breakout = None
        breakout_vol_ratio = None
        days_since_breakout = None

        if breakout_idx is None:
            age = last - rs.idx
            post_rs_min = lows[rs.idx:last + 1].min()
            if age > cfg.max_pattern_days / 2 or post_rs_min < head.price:
                continue  # 폐기(오래 미확정 또는 헤드 아래로 재하락)
            status = "forming"
        else:
            d_breakout = dates[breakout_idx]
            days_since_breakout = last - breakout_idx
            if days_since_breakout > cfg.breakout_max_age:
                continue  # 돌파 오래돼서 스크리닝 결과 제외
            neck_at_breakout = _neck(breakout_idx, p1.idx, p1.price, p2.idx, p2.price)
            avg_val_before = values[max(0, breakout_idx - cfg.liquidity_window):breakout_idx]
            avg_val_before = avg_val_before[avg_val_before > 0]
            breakout_vol_ratio = float(values[breakout_idx] / avg_val_before.mean()) if len(avg_val_before) else None

            if close_now < neck_now * (1 - cfg.retest_tol):
                continue  # 폐기: 돌파 후 재하락 실패
            elif close_now > neck_now * (1 + cfg.retest_tol):
                status = "breakout"
            else:
                status = "retest"

        geo_forming = (status == "forming")
        score = _score(geo, cfg, breakout_vol_ratio, geo_forming)

        target = neck_now + (geo["neck_at_head"] - head.price)
        stop = min(rs.price, neck_now * cfg.stop_neck_buffer)
        rr = (target - close_now) / (close_now - stop) if (close_now - stop) != 0 else float("nan")

        out.append(IHSPattern(
            ticker=ticker, status=status, score=score,
            d_ls=dates[ls.idx], d_head=dates[head.idx], d_rs=dates[rs.idx], d_breakout=d_breakout,
            ls=ls.price, head=head.price, rs=rs.price, p1=p1.price, p2=p2.price,
            neckline_now=round(neck_now, 4), close=float(close_now),
            depth=round(geo["depth"], 4), shoulder_sym=round(geo["shoulder_sym"], 4),
            time_sym=round(geo["time_sym"], 4), neck_slope=round(geo["neck_slope"], 4),
            breakout_vol_ratio=round(breakout_vol_ratio, 3) if breakout_vol_ratio else None,
            prior_decline=round(geo["prior_decline"], 4), days_since_breakout=days_since_breakout,
            target=round(target, 2), stop=round(stop, 2),
            rr=round(rr, 3) if rr == rr and math.isfinite(rr) else None,
            gap_warning=geo["gap_warning"],
        ))
    return out


# ==================== 전종목 스캔(scan_universe) ====================
#
# [바닥시그널과의 관계] 여기서부터는 signals.py/screener.py의 12개 바닥/턴어라운드
# 시그널 체계를 전혀 참조하지 않는다(사용자 지시: "바닥시그널과 섞지말고
# 역헤드앤숄더만 가지고 스크리닝"). screener.py에서 재사용하는 건 딱 두 개
# 뿐이다: get_universe()(종목명 조회 — 스팩/우선주 이름필터용)와
# is_trading_halted()(거래정지 판정 — DB만으로 계산, 라이브 호출 없음). 둘 다
# 기존 코드 그대로 호출만 하고 리팩터링하지 않는다(스펙 8번 금지사항).

# 스팩/우선주 이름 패턴 — 코드 목록이 아니라 KRX가 실제로 쓰는 명칭 규칙에
# 대한 키워드/정규식 매칭이다(이 프로젝트의 기존 방침: PBR_LOW_CAUTION_KEYWORDS
# 참고, screener.py). 우선주는 "종목명+(숫자)?우(B)?"로 끝나는 관행이 있다 —
# 완벽하지 않은 휴리스틱임을 문서화해둔다. ETF/ETN은 get_universe()가 쓰는
# get_market_ticker_list(market="KOSPI"/"KOSDAQ")가 애초에 ETF/ETN/ELW를
# 별도 시장으로 분류해 포함하지 않으므로 보통 자동으로 빠진다.
import re as _re
_SPAC_NAME_RE = _re.compile(r"스팩")
_PREF_NAME_RE = _re.compile(r"\d?우(B)?$")
_ETF_ETN_NAME_RE = _re.compile(r"ETN|KODEX|TIGER|KBSTAR|ARIRANG|HANARO|KOSEF|SOL |ACE ")


def _is_excluded_name(name: str) -> bool:
    if not name:
        return False
    return bool(_SPAC_NAME_RE.search(name) or _PREF_NAME_RE.search(name) or _ETF_ETN_NAME_RE.search(name))


def scan_universe(
    as_of: str | None = None,
    cfg: IHSConfig | None = None,
    tickers: list[str] | None = None,
    statuses: tuple[str, ...] = ("forming", "retest", "breakout"),
    min_score: float = 50.0,
    lookback_trading_days: int = 320,
) -> pd.DataFrame:
    """as_of 시점 기준 전종목(또는 tickers로 지정한 종목만)을 스캔해서
       IHSPattern 조건을 만족하는 결과를 DataFrame으로 반환한다.

       - as_of가 None이면 로컬 DB(data/YYYYMMDD.db)에 있는 가장 최근 날짜를 쓴다.
       - lookback_trading_days: 패턴 탐지에 필요한 최대 구간
         (max_pattern_days=220 + prior_lookback=60 + 안전마진)을 커버하는 기본값.
       - 유동성/품질 필터(스펙 4번): 20일평균거래대금·최소종가·최소봉수·
         스팩/우선주/ETF/ETN 이름필터·관리종목/거래정지(is_trading_halted).
       - 종목 단위 실패는 조용히 삼키지 않고 로깅 + 카운트로 최종 리포트에 남긴다."""
    import db
    import db_reader as dbr
    import screener as scr

    cfg = cfg or IHSConfig()

    all_dates = db.existing_dates()
    if not all_dates:
        print("[IHS scan_universe] data/ 에 DB 파일이 없음", file=sys.stderr)
        return pd.DataFrame()
    if as_of is None:
        as_of = all_dates[-1]
    window_dates = [d for d in all_dates if d <= as_of][-lookback_trading_days:]
    if not window_dates:
        print(f"[IHS scan_universe] as_of={as_of} 이전 DB 파일이 없음", file=sys.stderr)
        return pd.DataFrame()

    matrix = dbr.load_ohlcv_matrix_from_db_full(window_dates)
    if not matrix:
        return pd.DataFrame()
    latest_date = max(matrix)

    universe = list(tickers) if tickers is not None else sorted(matrix[latest_date].keys())

    per_ticker: dict[str, list[tuple]] = {}
    for d in window_dates:
        for tkr, row in matrix.get(d, {}).items():
            per_ticker.setdefault(tkr, []).append((d,) + row)

    excluded_by_name: set[str] = set()
    name_filter_ok = True
    try:
        uni_names, _ticker_market = scr.get_universe(as_of)
        if not uni_names:
            # KRX 미로그인 환경(로컬)에서는 get_universe가 예외 없이 빈 dict를
            # 반환한다 — 이걸 "필터 정상 동작, 0건 제외"로 오인하면 안 되므로
            # 명시적으로 실패로 취급해 보고한다(스펙 8번: 실패를 조용히 삼키지 말 것).
            raise RuntimeError("get_universe가 빈 결과 반환(KRX 미로그인 추정)")
        for tkr in universe:
            if _is_excluded_name(uni_names.get(tkr, "")):
                excluded_by_name.add(tkr)
    except Exception as e:
        name_filter_ok = False
        print(f"[IHS scan_universe] 이름 필터(스팩/우선주) 조회 실패, 건너뜀: {e}", file=sys.stderr)

    results: list[dict] = []
    n_scanned = n_liquidity_filtered = n_halted_filtered = n_name_filtered = n_failed = 0
    failed_tickers: list[str] = []

    for tkr in universe:
        rows = per_ticker.get(tkr)
        if not rows or len(rows) < cfg.min_bars:
            continue
        if tkr in excluded_by_name:
            n_name_filtered += 1
            continue
        rows.sort(key=lambda r: r[0])
        # o/h/l이 0(거래정지 인접 스냅샷 — screener.is_halted_snapshot과 같은
        # 패턴: close는 직전값 유지, o/h/l만 0으로 찍힘) 이하인 봉은 프랙탈
        # min/max를 오염시키므로(0이 항상 "최저가"로 잡힘) 여기서 제거한다.
        rows = [r for r in rows if r[1] > 0 and r[2] > 0 and r[3] > 0 and r[4] > 0]
        if len(rows) < cfg.min_bars:
            continue
        dates_t = [r[0] for r in rows]
        opens = [r[1] for r in rows]
        highs = [r[2] for r in rows]
        lows = [r[3] for r in rows]
        closes = [r[4] for r in rows]
        vols = [r[5] for r in rows]

        if closes[-1] < cfg.min_close:
            n_liquidity_filtered += 1
            continue
        w = min(cfg.liquidity_window, len(closes))
        avg_val = sum(c * v for c, v in zip(closes[-w:], vols[-w:])) / w
        if avg_val < cfg.min_avg_trading_value:
            n_liquidity_filtered += 1
            continue
        if scr.is_trading_halted(opens, highs, lows, closes, vols):
            n_halted_filtered += 1
            continue

        n_scanned += 1
        df_t = pd.DataFrame({
            cfg.date_col: dates_t, cfg.open_col: opens, cfg.high_col: highs,
            cfg.low_col: lows, cfg.close_col: closes, cfg.volume_col: vols,
        })
        try:
            patterns = detect_ihs(df_t, cfg, ticker=tkr, as_of=as_of)
        except Exception as e:
            n_failed += 1
            failed_tickers.append(tkr)
            print(f"  [IHS] {tkr} 검출 실패(건너뜀): {e}", file=sys.stderr)
            continue

        for p in patterns:
            if p.status not in statuses or p.score < min_score:
                continue
            row = asdict(p)
            span_calendar = [dd for dd in window_dates if row["d_ls"] <= dd <= row["d_rs"]]
            span_actual = [dd for dd in dates_t if row["d_ls"] <= dd <= row["d_rs"]]
            row["missing_bar_ratio"] = round(1 - len(span_actual) / len(span_calendar), 4) if span_calendar else 0.0
            results.append(row)

    df_out = pd.DataFrame(results)
    if not df_out.empty:
        df_out = df_out.sort_values("score", ascending=False).reset_index(drop=True)

    print(
        f"[IHS scan_universe] as_of={as_of} 대상={len(universe)} 스코어링={n_scanned} "
        f"유동성제외={n_liquidity_filtered} 거래정지제외={n_halted_filtered} "
        f"이름필터제외={n_name_filtered}(필터정상={name_filter_ok}) 검출실패={n_failed} "
        f"결과행={len(df_out)}", file=sys.stderr,
    )
    if failed_tickers:
        shown = failed_tickers[:20]
        print(f"  [IHS] 실패종목({len(failed_tickers)}개): {shown}{' ...' if len(failed_tickers) > 20 else ''}",
              file=sys.stderr)

    return df_out


# ==================== 시각화(plot_pattern) ====================

def plot_pattern(df: pd.DataFrame, pattern: IHSPattern, save_path: str, cfg: IHSConfig | None = None) -> str:
    """pattern의 LS~현재 구간(+선행 여유분)을 캔들 근사(종가 라인) + 피봇 마커
       + 넥라인 + 목표가/손절가로 그려 save_path에 PNG로 저장. 파라미터 튜닝은
       숫자가 아니라 이 그림으로 눈으로 맞춘다(스펙 9.4)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cfg = cfg or IHSConfig()
    dates = df[cfg.date_col].astype(str).tolist()
    closes = df[cfg.close_col].to_numpy(dtype=float)

    try:
        i_ls = dates.index(pattern.d_ls)
        i_head = dates.index(pattern.d_head)
        i_rs = dates.index(pattern.d_rs)
    except ValueError:
        i_ls = i_head = i_rs = None

    lead = 20
    start = max(0, (i_ls if i_ls is not None else 0) - lead)
    end_marker = dates.index(pattern.d_breakout) if pattern.d_breakout and pattern.d_breakout in dates else len(dates) - 1
    end = min(len(dates), end_marker + 15)
    xs = list(range(start, end))
    ys = closes[start:end]

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(xs, ys, color="#4dd0e1", linewidth=1.3, label="close")

    if i_ls is not None:
        for i, label, color in [(i_ls, "LS", "#f87171"), (i_head, "HEAD", "#facc15"), (i_rs, "RS", "#f87171")]:
            ax.scatter([i], [closes[i]], color=color, zorder=5)
            ax.annotate(label, (i, closes[i]), textcoords="offset points", xytext=(0, -14), color=color, fontsize=9)

    neck_xs = xs
    neck_ys = [pattern.neckline_now] * len(xs)  # 단순화: 현재 넥라인(수평 근사)로 표시
    ax.plot(neck_xs, neck_ys, color="#9fb0d0", linestyle="--", linewidth=1, label="neckline(now)")
    ax.axhline(pattern.target, color="#4ade80", linestyle=":", linewidth=1, label=f"target {pattern.target:.0f}")
    ax.axhline(pattern.stop, color="#f87171", linestyle=":", linewidth=1, label=f"stop {pattern.stop:.0f}")

    ax.set_title(f"{pattern.ticker} — {pattern.status} (score {pattern.score:.1f})")
    ax.set_facecolor("#111a2e")
    fig.patch.set_facecolor("#0a0f1c")
    ax.tick_params(colors="#9fb0d0")
    for spine in ax.spines.values():
        spine.set_color("#24304b")
    ax.legend(facecolor="#111a2e", labelcolor="#e6edf7", fontsize=8)
    fig.tight_layout()
    fig.savefig(save_path, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    return save_path
