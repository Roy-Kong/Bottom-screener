"""tests/test_ihs.py — IHS_SPEC.md 6번 항목이 요구하는 5개 테스트.

strategies/ihs/ihs_screener.py의 detect_ihs()가 순수 함수(DB/파일 IO 없음)라
전부 인메모리 합성 데이터로만 테스트한다.
"""
from __future__ import annotations
import sys
import random
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "ihs"))

from ihs_screener import detect_ihs, IHSConfig  # noqa: E402


# ==================== 합성 데이터 생성 ====================

def _dates(n: int, start: str = "20220103") -> list[str]:
    d = dt.datetime.strptime(start, "%Y%m%d").date()
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += dt.timedelta(days=1)
    return out


def _lerp_segment(a: float, b: float, n: int) -> list[float]:
    """a에서 b로 n개 구간(첫 점 a 포함 안 함 — 이전 세그먼트의 마지막 점이 a임)."""
    return [a + (b - a) * (i + 1) / n for i in range(n)]


def _build_pre_rs(
    lead_in: int = 15, prior_decline_days: int = 25, leg_days: int = 15,
    prior_high: float = 130.0, ls: float = 80.0, p1: float = 95.0,
    head: float = 65.0, p2: float = 95.0, rs: float = 79.0,
) -> list[float]:
    """LS-P1-HEAD-P2-RS 까지의 종가 시퀀스(RS에서 끝남). p1==p2 기본값이라
       넥라인이 정확히 수평(=95)이 되어 이후 상태전이 테스트에서 임계값
       계산이 쉬워진다."""
    closes: list[float] = [prior_high * 0.9] * lead_in
    closes += _lerp_segment(closes[-1], prior_high, 8)
    closes += _lerp_segment(closes[-1], ls, prior_decline_days)
    closes += _lerp_segment(closes[-1], p1, leg_days)
    closes += _lerp_segment(closes[-1], head, leg_days)
    closes += _lerp_segment(closes[-1], p2, leg_days)
    closes += _lerp_segment(closes[-1], rs, leg_days)
    return closes


def build_ideal_ihs(
    lead_in: int = 15, prior_decline_days: int = 25, leg_days: int = 15,
    post_days: int = 20, prior_high: float = 130.0, ls: float = 80.0, p1: float = 95.0,
    head: float = 65.0, p2: float = 96.0, rs: float = 79.0, breakout_target: float = 105.0,
    base_vol: float = 100_000.0, breakout_vol: float = 400_000.0,
    post_closes: list[float] | None = None,
) -> pd.DataFrame:
    """이상적인 역헤드앤숄더 + 돌파까지 포함한 합성 OHLCV. O=H=L=C로 단순화
       (피봇 검출은 high/low만 쓰므로 이렇게 해도 로직 검증엔 문제없음).
       구조: [lead_in 평탄] -> [prior_high까지 상승] -> [prior_decline_days
       동안 LS까지 하락] -> P1 -> HEAD -> P2 -> RS -> [post_closes가 주어지면
       그대로, 아니면 breakout_target까지 선형 상승]."""
    closes = _build_pre_rs(lead_in, prior_decline_days, leg_days, prior_high, ls, p1, head, p2, rs)
    breakout_search_start = len(closes)  # RS의 인덱스(마지막 pre-RS 종가)
    if post_closes is not None:
        closes = closes + post_closes  # RS 뒤에 이어붙임
    else:
        closes += _lerp_segment(closes[-1], breakout_target, post_days)

    n = len(closes)
    vols = [base_vol] * n
    for i in range(breakout_search_start, min(breakout_search_start + 3, n)):
        vols[i] = breakout_vol

    dates = _dates(n)
    df = pd.DataFrame({
        "date": dates, "ticker": ["TEST"] * n,
        "open": closes, "high": closes, "low": closes, "close": closes, "volume": vols,
    })
    return df


DEFAULT_CFG = IHSConfig()


def _find_breakout_pattern(patterns, min_status="forming"):
    return [p for p in patterns if p.status in ("forming", "retest", "breakout")]


# ==================== 1) 합성 패턴 검출 ====================

def test_synthetic_pattern_detected_as_breakout():
    df = build_ideal_ihs()
    patterns = detect_ihs(df, DEFAULT_CFG, ticker="TEST")
    statuses = [p.status for p in patterns]
    assert "breakout" in statuses, f"이상적 패턴에서 breakout이 검출돼야 함, 실제: {statuses}"
    bp = next(p for p in patterns if p.status == "breakout")
    assert bp.score > 0
    assert bp.head < bp.ls and bp.head < bp.rs, "머리가 양쪽 어깨보다 낮아야 함"
    assert bp.rr is not None


# ==================== 2) 음성 케이스 ====================

def _trend_df(direction: str, n: int = 150, start: float = 50.0, step: float = 0.5,
             seed: int | None = None) -> pd.DataFrame:
    prices = []
    if direction == "up":
        prices = [start + step * i for i in range(n)]
    elif direction == "down":
        prices = [start + step * (n - i) for i in range(n)]
    else:  # random walk
        rng = random.Random(seed)
        p = start
        for _ in range(n):
            p = max(1.0, p + rng.uniform(-1.5, 1.5))
            prices.append(p)
    dates = _dates(n)
    return pd.DataFrame({
        "date": dates, "ticker": ["TEST"] * n,
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100_000.0] * n,
    })


def test_negative_uptrend_no_breakout():
    df = _trend_df("up")
    patterns = detect_ihs(df, DEFAULT_CFG, ticker="TEST")
    bad = [p for p in patterns if p.status in ("breakout", "retest")]
    assert not bad, f"단순 상승추세에서 돌파/리테스트가 검출되면 안 됨: {bad}"


def test_negative_downtrend_no_breakout():
    df = _trend_df("down")
    patterns = detect_ihs(df, DEFAULT_CFG, ticker="TEST")
    bad = [p for p in patterns if p.status in ("breakout", "retest")]
    assert not bad, f"단순 하락추세에서 돌파/리테스트가 검출되면 안 됨: {bad}"


def test_negative_random_walk_false_positive_rate():
    n_seeds = 100
    false_positives = 0
    for seed in range(n_seeds):
        df = _trend_df("random", seed=seed)
        patterns = detect_ihs(df, DEFAULT_CFG, ticker="TEST")
        if any(p.status in ("breakout", "retest") for p in patterns):
            false_positives += 1
    rate = false_positives / n_seeds
    print(f"\n[랜덤워크 오검출률] {false_positives}/{n_seeds} = {rate*100:.1f}%")
    assert rate < 0.15, f"랜덤워크 오검출률이 너무 높음: {rate*100:.1f}%"


# ==================== 3) 룩어헤드 회귀 테스트 ====================

def test_no_lookahead_regression():
    df = build_ideal_ihs()
    all_dates = df["date"].tolist()

    confirmed: dict[tuple, tuple] = {}  # (ticker,d_ls) -> (d_ls,d_head,d_rs) 최초 확정값
    # 패턴이 형성되는 구간부터 끝까지 하루씩 전진
    start_idx = 60
    for i in range(start_idx, len(all_dates)):
        as_of = all_dates[i]
        patterns = detect_ihs(df, DEFAULT_CFG, ticker="TEST", as_of=as_of)
        for p in patterns:
            key = p.d_ls
            triple = (p.d_ls, p.d_head, p.d_rs)
            if key in confirmed:
                assert confirmed[key] == triple, (
                    f"as_of={as_of}에서 LS={key} 패턴의 (d_ls,d_head,d_rs)가 "
                    f"이전 확정값 {confirmed[key]}에서 {triple}로 소급 변경됨"
                )
            else:
                confirmed[key] = triple
    assert confirmed, "회귀 테스트 동안 패턴이 하나도 확정되지 않음 — 테스트 자체가 무의미함"


# ==================== 4) 상태 전이 ====================

def test_state_transition_forming_retest_breakout():
    cfg = DEFAULT_CFG  # order=5, p1==p2==95(수평 넥라인) -> neck(i)=95 항상
    # neck=95, breakout_buffer=0.5% -> 돌파 임계 95.475, retest_tol=2% -> 밴드 [93.1, 96.9]
    # RS(=79) 이후 경로를 날짜별로 직접 제어: day+6까지는 forming(95 밑),
    # day+7에 96.5로 돌파하며 retest 밴드 안 진입, day+9에 105로 확실한 breakout.
    post = [82, 85, 88, 90, 92, 94, 96.5, 94.0, 105.0]
    df = build_ideal_ihs(p1=95.0, p2=95.0, post_closes=post)

    rs_idx = len(_build_pre_rs(p1=95.0, p2=95.0)) - 1
    dates = df["date"].tolist()

    as_of_forming = dates[rs_idx + 6]
    patterns = detect_ihs(df, cfg, ticker="TEST", as_of=as_of_forming)
    statuses = {p.status for p in patterns}
    assert statuses == {"forming"}, f"day+6엔 forming만 있어야 함 (as_of={as_of_forming}): {statuses}"

    as_of_retest = dates[rs_idx + 7]
    patterns = detect_ihs(df, cfg, ticker="TEST", as_of=as_of_retest)
    statuses = {p.status for p in patterns}
    assert "retest" in statuses, f"day+7(96.5, 밴드 내)엔 retest여야 함 (as_of={as_of_retest}): {statuses}"

    as_of_breakout = dates[rs_idx + 9]
    patterns = detect_ihs(df, cfg, ticker="TEST", as_of=as_of_breakout)
    statuses = {p.status for p in patterns}
    assert "breakout" in statuses, f"day+9(105, 밴드 상단 초과)엔 breakout이어야 함 (as_of={as_of_breakout}): {statuses}"


# ==================== 5) 스케일 불변성 ====================

def test_scale_invariance():
    df = build_ideal_ihs()
    df_scaled = df.copy()
    for col in ["open", "high", "low", "close"]:
        df_scaled[col] = df_scaled[col] * 10

    p1 = detect_ihs(df, DEFAULT_CFG, ticker="TEST")
    p2 = detect_ihs(df_scaled, DEFAULT_CFG, ticker="TEST")

    assert len(p1) == len(p2), f"패턴 개수가 스케일에 따라 달라짐: {len(p1)} vs {len(p2)}"
    for a, b in zip(p1, p2):
        assert a.status == b.status
        assert abs(a.score - b.score) < 0.01, f"점수가 스케일 불변이어야 함: {a.score} vs {b.score}"
        assert abs(a.depth - b.depth) < 1e-6
        assert abs(a.shoulder_sym - b.shoulder_sym) < 1e-6


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
