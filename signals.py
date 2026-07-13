"""
signals.py — 바닥 신호 점수 엔진 (순수 함수, 네트워크 불필요)

각 신호는 0~100 점을 반환한다. 높을수록 '바닥에 가깝다'.
데이터 수집(screener.py)과 분리되어 있어 단독으로 테스트 가능하다.

설계 기준(시작 임계값, 백테스트로 조정 예정):
- 동일 가중 평균
- 조건 미충족/데이터 없음 → None (종합 평균에서 제외, 분모 자동 조정)
"""
from __future__ import annotations
import math


# ---------- 공통 유틸 ----------
def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _lin(value: float, at_lo: float, at_hi: float, lo_score: float, hi_score: float) -> float:
    """value가 at_lo일 때 lo_score, at_hi일 때 hi_score가 되도록 선형 매핑 후 clamp."""
    if at_hi == at_lo:
        return _clamp(hi_score)
    t = (value - at_lo) / (at_hi - at_lo)
    return _clamp(lo_score + t * (hi_score - lo_score))


def _valid(*vals) -> bool:
    for v in vals:
        if v is None:
            return False
        try:
            if math.isnan(float(v)):
                return False
        except (TypeError, ValueError):
            return False
    return True


def percentile_rank(history: list[float], value: float) -> float | None:
    """history 안에서 value가 몇 퍼센타일인지 0~100. (이하 비율 * 100)"""
    hist = [h for h in history if _valid(h)]
    if not hist or not _valid(value):
        return None
    below = sum(1 for h in hist if h <= value)
    return 100.0 * below / len(hist)


# ---------- 개별 신호 ----------
def score_volume_dryness(recent20_median_vol: float, past120_median_vol: float) -> float | None:
    """① 거래량 고갈. 최근 20일 중앙값 / 과거 120일 중앙값. 낮을수록 고점.
       비율 1.0 이상 → 0점, 0.3 이하 → 100점."""
    if not _valid(recent20_median_vol, past120_median_vol) or past120_median_vol <= 0:
        return None
    ratio = recent20_median_vol / past120_median_vol
    return _lin(ratio, 1.0, 0.3, 0.0, 100.0)


def score_accumulation(net_buy_value_20d: float, float_market_cap: float,
                       price_change_pct_20d: float) -> float | None:
    """③ 기관·외국인 매집. 20일 누적 순매수액 / 유통시가총액 = 강도.
       주가가 조용할수록(변동 작을수록) 가중. 순매수 음수면 0."""
    if not _valid(net_buy_value_20d, float_market_cap, price_change_pct_20d) or float_market_cap <= 0:
        return None
    if net_buy_value_20d <= 0:
        return 0.0
    intensity = net_buy_value_20d / float_market_cap        # 예: 0.02 = 유통시총의 2% 매집
    base = _lin(intensity, 0.0, 0.02, 0.0, 100.0)           # 2% 이상이면 만점
    dp = abs(price_change_pct_20d)
    quiet = 1.0 if dp <= 5 else _clamp(1 - (dp - 5) / 15, 0, 1)  # 5%↑ 움직이면 감쇠, 20%↑면 0
    return _clamp(base * quiet)


def score_short_covering(current_short_ratio: float, max_short_ratio_3m: float) -> float | None:
    """④ 공매도 잔고 감소. 현재 공매도잔고비중 / 3개월 최고. 낮을수록 고점.
       3개월 최고의 50% 이하로 줄면 100점."""
    if not _valid(current_short_ratio, max_short_ratio_3m) or max_short_ratio_3m <= 0:
        return None
    ratio = current_short_ratio / max_short_ratio_3m
    return _lin(ratio, 1.0, 0.5, 0.0, 100.0)


def score_pbr_low(current_pbr: float, pbr_history_5y: list[float]) -> float | None:
    """⑤ PBR 역사적 저점. 5년 밴드 내 백분위. 낮을수록 고점.
       PBR<=0(자본잠식 의심)은 None → 생존 게이트에서 별도 처리."""
    if not _valid(current_pbr) or current_pbr <= 0:
        return None
    pr = percentile_rank(pbr_history_5y, current_pbr)
    if pr is None:
        return None
    return _clamp(100.0 - pr)   # 최저 PBR → 100


def score_dividend_yield(current_div: float, div_history_5y: list[float],
                         dps: float, eps: float, had_dividend_cut: bool) -> float | None:
    """⑧ 배당수익률(조건부). 무배당·함정(고배당성향/삭감이력)은 None.
       통과 시 5년 배당수익률 밴드 백분위(높을수록 고점=주가 저점)."""
    if not _valid(current_div) or current_div <= 0:
        return None                      # 무배당 → 이 신호 미적용
    if had_dividend_cut:
        return None                      # 최근 배당 삭감 이력 → 함정 배제
    if _valid(dps, eps) and eps > 0:
        payout = dps / eps
        if payout >= 0.8:                # 배당성향 80%+ → 컷 위험, 배제
            return None
    pr = percentile_rank(div_history_5y, current_div)
    if pr is None:
        return None
    return _clamp(pr)                    # 높은 배당수익률 = 높은 백분위 = 고점


def score_relative_strength(stock_return_60d: float, index_return_60d: float) -> float | None:
    """⑥ 상대강도. 60일 수익률의 지수 대비 초과분. 지수보다 잘 버틸수록 고점.
       상대 -20%p → 0점, +20%p → 100점."""
    if not _valid(stock_return_60d, index_return_60d):
        return None
    rel = stock_return_60d - index_return_60d
    return _lin(rel, -0.20, 0.20, 0.0, 100.0)


def score_margin_balance(ratio: float | None, peer_ratios: list[float]) -> float | None:
    """⑦ 신용잔고 낮음 (1차 채점 상위 후보군에 한해 네이버금융에서 수집).
       종목별 5년 밴드가 없어 '그날 후보군 내 상대 순위'로 대체.
       신용잔고율이 낮을수록(레버리지 강제청산 위험이 낮을수록) 고점."""
    if not _valid(ratio):
        return None
    pr = percentile_rank(peer_ratios, ratio)
    if pr is None:
        return None
    return _clamp(100.0 - pr)   # 최저 신용잔고율 → 100


def score_volatility_squeeze(bandwidth_history: list[float]) -> float | None:
    """⑧ 변동성 수축(밴드 스퀴즈). 최근 20일 볼린저밴드폭이 자체 과거 밴드폭
       히스토리 대비 얼마나 좁은지(백분위). 좁을수록(가격이 눌려 다져질수록) 고점."""
    hist = [h for h in bandwidth_history if _valid(h)]
    if not hist:
        return None
    current = bandwidth_history[-1]
    pr = percentile_rank(hist, current)
    if pr is None:
        return None
    return _clamp(100.0 - pr)   # 가장 좁은 밴드 → 100


# ---------- 종합 ----------
SIGNAL_LABELS = {
    "volume_dryness": "거래량 고갈",
    "accumulation": "기관·외국인 매집",
    "short_covering": "공매도 감소",
    "pbr_low": "PBR 역사적 저점",
    "dividend_yield": "배당수익률(조건부)",
    "relative_strength": "상대강도",
    "margin_balance": "신용잔고 낮음",
    "volatility_squeeze": "변동성 수축",
}


def composite_score(signal_scores: dict[str, float | None]) -> dict:
    """None이 아닌 신호만 동일 가중 평균. 결과와 세부 내역 반환."""
    used = {k: v for k, v in signal_scores.items() if v is not None}
    if not used:
        composite = None
    else:
        composite = round(sum(used.values()) / len(used), 1)
    return {
        "composite": composite,
        "n_signals_used": len(used),
        "breakdown": {k: (round(v, 1) if v is not None else None) for k, v in signal_scores.items()},
    }


# ---------- 스모크 테스트 (네트워크 없이 로직 검증) ----------
if __name__ == "__main__":
    print("=== signals.py 스모크 테스트 ===\n")

    # 시나리오 A: 교과서적 바닥 (하이닉스 2022 스타일)
    # 거래량 마름, 외국인 조용히 매집, 공매도 급감, PBR 최저, 지수보다 선방
    a = {
        "volume_dryness": score_volume_dryness(35, 100),          # ratio 0.35
        "accumulation": score_accumulation(2.5e11, 1.0e13, 3.0),  # 2.5% 매집, 주가 +3% (조용)
        "short_covering": score_short_covering(0.4, 1.0),         # 3개월 최고의 40%
        "pbr_low": score_pbr_low(0.9, [2.1, 1.8, 1.5, 1.2, 1.0, 0.95, 1.3, 2.0, 1.7, 1.1]),
        "dividend_yield": score_dividend_yield(5.0, [1.5, 2.0, 2.5, 3.0, 4.0, 5.0], dps=1000, eps=8000, had_dividend_cut=False),
        "relative_strength": score_relative_strength(-0.05, -0.20),  # 지수 -20%인데 종목 -5%
    }
    ra = composite_score(a)
    print("A. 교과서적 바닥 종목")
    for k, v in ra["breakdown"].items():
        print(f"   {SIGNAL_LABELS[k]:<18} {v}")
    print(f"   → 종합 {ra['composite']} (신호 {ra['n_signals_used']}개)\n")

    # 시나리오 B: 고점/과열 (테슬라 스타일 - 프레임 반대편)
    b = {
        "volume_dryness": score_volume_dryness(120, 100),         # 거래 오히려 증가
        "accumulation": score_accumulation(-1e11, 1.0e13, 8.0),   # 순매도 + 주가 급등
        "short_covering": score_short_covering(0.95, 1.0),        # 공매도 안 줄음
        "pbr_low": score_pbr_low(2.0, [2.1, 1.8, 1.5, 1.2, 1.0, 0.95, 1.3, 2.0, 1.7, 1.1]),
        "dividend_yield": score_dividend_yield(0.0, [], dps=0, eps=5000, had_dividend_cut=False),  # 무배당
        "relative_strength": score_relative_strength(0.30, 0.05),
    }
    rb = composite_score(b)
    print("B. 고점/과열 종목")
    for k, v in rb["breakdown"].items():
        print(f"   {SIGNAL_LABELS[k]:<18} {v}")
    print(f"   → 종합 {rb['composite']} (신호 {rb['n_signals_used']}개)\n")

    # 시나리오 C: 배당 함정 (수익률 높지만 배당성향 과다)
    c_div = score_dividend_yield(9.0, [3, 4, 5, 6, 7, 9], dps=9000, eps=9500, had_dividend_cut=False)
    print(f"C. 배당 함정 테스트: 수익률 9%인데 배당성향 {9000/9500:.0%} → 점수 = {c_div} (None이면 정상 배제)\n")

    # 시나리오 D: 신용잔고 (후보군 내 상대 순위)
    peers = [1.2, 3.4, 5.0, 0.8, 2.1, 4.4, 6.7]
    d_low = score_margin_balance(0.5, peers)   # 후보군 중 최저 → 고점
    d_high = score_margin_balance(6.7, peers)  # 후보군 중 최고 → 저점
    print(f"D. 신용잔고 테스트: 최저 비중 → {d_low}, 최고 비중 → {d_high}\n")

    # 시나리오 E: 변동성 수축 (밴드폭 자체 히스토리 대비 좁음/넓음)
    bw_hist = [4.2, 5.1, 3.8, 6.0, 4.5, 7.2, 2.1, 5.5, 3.0, 8.4]
    e_narrow = score_volatility_squeeze(bw_hist[:-1] + [1.0])   # 현재값이 역대 최저 → 고점
    e_wide = score_volatility_squeeze(bw_hist[:-1] + [9.0])     # 현재값이 역대 최고 → 저점
    print(f"E. 변동성 수축 테스트: 최근 밴드 역대 최저 → {e_narrow}, 최고 → {e_wide}\n")

    # 검증 단언
    assert ra["composite"] > 75, "바닥 종목은 고득점이어야 함"
    assert rb["composite"] < 30, "고점 종목은 저득점이어야 함"
    assert c_div is None, "배당 함정은 배제(None)되어야 함"
    assert ra["n_signals_used"] == 6 and rb["n_signals_used"] == 5, "무배당주는 신호 5개"
    assert d_low > d_high, "신용잔고 낮은 쪽이 고득점이어야 함"
    assert e_narrow > e_wide, "밴드 좁은 쪽이 고득점이어야 함"
    print("✅ 모든 로직 검증 통과 (바닥 고득점 / 고점 저득점 / 함정 배제 / 무배당 분모조정 / 신용잔고 순위 / 밴드 스퀴즈)")
