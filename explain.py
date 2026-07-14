"""
explain.py — 신호 breakdown + raw_values로 "왜 이 종목이 뽑혔는지" 설명하는
한국어 문장을 조립한다. AI 호출 없이 순수 문자열 템플릿 조합만 사용한다.

screener.py가 종목별로 breakdown(점수)과 raw(정규화 전 원본 수치)를 만든 뒤
explain_result()를 호출해 결과를 results.json에 그대로 저장한다 — 즉 문장은
매 실행마다 서버에서 한 번만 조립되고, 프론트는 그 텍스트를 그대로 표시한다.
"""
from __future__ import annotations

SIGNAL_ON_THRESHOLD = 50   # 이 값 이상인 신호만 문장으로 언급 (약한 신호는 생략)

BOTTOM_LABELS = {
    "volume_dryness": "거래량 고갈",
    "accumulation": "기관·외국인 매집",
    "short_covering": "공매도 감소",
    "pbr_low": "PBR 역사적 저점",
    "dividend_yield": "배당수익률",
    "relative_strength": "상대강도",
    "volatility_squeeze": "변동성 수축",
}

TURNAROUND_LABELS = {
    "volume_surge": "거래량 동반 상승",
    "ma_breakout": "이평선 돌파",
    "short_term_breakout": "단기 고점 돌파",
    "relative_strength_accel": "상대강도 가속",
    "accumulation_accel": "매집 가속",
}


# ---------- 신호별 문장 템플릿 ----------
def _sentence_volume_dryness(raw: dict) -> str | None:
    r = raw.get("ratio")
    if r is None:
        return None
    return f"6~25일 전 거래량이 과거 120일 평균의 {r * 100:.0f}%로 크게 줄었습니다."


def _sentence_accumulation(raw: dict) -> str | None:
    krw = raw.get("net_buy_krw")
    if krw is None:
        return None
    intensity = raw.get("intensity_pct")
    price_change = raw.get("price_change_pct")
    s = f"최근 20일간 기관·외국인이 {krw / 1e8:,.0f}억원"
    if intensity is not None:
        s += f"(유통시총의 {intensity:.1f}%)"
    s += "을 순매수했"
    if price_change is not None:
        s += f"고, 그동안 주가는 {price_change:+.1f}%로 조용했"
    return s + "습니다."


def _sentence_short_covering(raw: dict) -> str | None:
    pct = raw.get("pct_of_max")
    if pct is None:
        return None
    return f"공매도잔고비중이 3개월 최고치 대비 {pct:.0f}%까지 줄었습니다."


def _sentence_pbr_low(raw: dict) -> str | None:
    pbr, pct = raw.get("pbr"), raw.get("percentile")
    if pbr is None or pct is None:
        return None
    return f"PBR이 {pbr:.2f}배로 최근 5년 밴드 하위 {pct:.0f}% 구간입니다."


def _sentence_dividend_yield(raw: dict) -> str | None:
    div, pct = raw.get("div_pct"), raw.get("percentile")
    if div is None or pct is None:
        return None
    return f"배당수익률이 {div:.1f}%로 최근 5년 대비 상위 {100 - pct:.0f}% 구간입니다."


def _sentence_relative_strength(raw: dict) -> str | None:
    stock_ret, idx_ret, excess = raw.get("stock_ret_pct"), raw.get("index_ret_pct"), raw.get("excess_pct")
    if stock_ret is None or idx_ret is None:
        return None
    bench = "업종지수" if str(raw.get("benchmark", "")).startswith("sector:") else "코스피"
    return f"최근 60일 {bench}는 {idx_ret:+.1f}%인데 이 종목은 {stock_ret:+.1f}%로 {bench}보다 {excess:+.1f}%p 선방했습니다."


def _sentence_volatility_squeeze(raw: dict) -> str | None:
    pct = raw.get("percentile")
    if pct is None:
        return None
    return f"볼린저밴드폭이 최근 구간 중 하위 {pct:.0f}%로 가격이 좁게 눌려 있습니다."


def _sentence_volume_surge(raw: dict) -> str | None:
    r = raw.get("ratio")
    if r is None:
        return None
    return f"최근 5일 거래량이 20일 평균 대비 {r * 100:.0f}%로 늘었습니다."


def _sentence_ma_breakout(raw: dict) -> str | None:
    close_gap, ma_gap = raw.get("close_vs_ma60_pct"), raw.get("ma20_vs_ma60_pct")
    if close_gap is None or ma_gap is None:
        return None
    return (f"20일 이평선이 60일 이평선보다 {ma_gap:+.1f}% 위로 정배열이고, "
            f"종가는 60일 이평선을 {close_gap:+.1f}% 돌파했습니다.")


def _sentence_short_term_breakout(raw: dict) -> str | None:
    pct = raw.get("pct_of_high60")
    if pct is None:
        return None
    return f"현재가가 최근 60일 박스권 고점의 {pct:.0f}%까지 올라왔습니다."


def _sentence_relative_strength_accel(raw: dict) -> str | None:
    recent, prior, accel = raw.get("rs_recent10_pct"), raw.get("rs_prior10_pct"), raw.get("accel_pct")
    if recent is None or prior is None:
        return None
    return f"상대강도가 이전 10일 {prior:+.1f}%p에서 최근 10일 {recent:+.1f}%p로 {accel:+.1f}%p 가속됐습니다."


def _sentence_accumulation_accel(raw: dict) -> str | None:
    ratio = raw.get("ratio")
    if ratio is None:
        return None
    return f"최근 5일 일평균 순매수가 이전 15일 평균의 {ratio * 100:.0f}%로 매집이 강해졌습니다."


BOTTOM_SENTENCES = {
    "volume_dryness": _sentence_volume_dryness,
    "accumulation": _sentence_accumulation,
    "short_covering": _sentence_short_covering,
    "pbr_low": _sentence_pbr_low,
    "dividend_yield": _sentence_dividend_yield,
    "relative_strength": _sentence_relative_strength,
    "volatility_squeeze": _sentence_volatility_squeeze,
}

TURNAROUND_SENTENCES = {
    "volume_surge": _sentence_volume_surge,
    "ma_breakout": _sentence_ma_breakout,
    "short_term_breakout": _sentence_short_term_breakout,
    "relative_strength_accel": _sentence_relative_strength_accel,
    "accumulation_accel": _sentence_accumulation_accel,
}


def _on_signals(breakdown: dict | None) -> list[tuple[str, float]]:
    """score>=SIGNAL_ON_THRESHOLD인 신호만, 점수 내림차순으로 (key, score) 리스트."""
    if not breakdown:
        return []
    on = [(k, v) for k, v in breakdown.items() if v is not None and v >= SIGNAL_ON_THRESHOLD]
    on.sort(key=lambda kv: kv[1], reverse=True)
    return on


def _paragraph(breakdown: dict | None, raw: dict | None, sentences: dict) -> str:
    if not breakdown or not raw:
        return ""
    lines = []
    for k, _ in _on_signals(breakdown):
        fn = sentences.get(k)
        if fn is None:
            continue
        s = fn(raw.get(k, {}) or {})
        if s:
            lines.append(s)
    return " ".join(lines)


def explain_bottom(breakdown: dict | None, raw: dict | None) -> str:
    """바닥 신호 설명 문단 (점수 높은 순, 50점 미만은 생략)."""
    return _paragraph(breakdown, raw, BOTTOM_SENTENCES)


def explain_turnaround(breakdown: dict | None, raw: dict | None) -> str:
    """턴어라운드 신호 설명 문단 (점수 높은 순, 50점 미만은 생략)."""
    return _paragraph(breakdown, raw, TURNAROUND_SENTENCES)


def explain_summary(status: str, breakdown: dict | None, turnaround_breakdown: dict | None) -> str | None:
    """confirmed_turnaround 종목만 "바닥+턴어라운드 동시 확인" 한 줄 요약. 아니면 None."""
    if status != "confirmed_turnaround":
        return None
    bottom_names = [BOTTOM_LABELS[k] for k, _ in _on_signals(breakdown)[:2] if k in BOTTOM_LABELS]
    turn_names = [TURNAROUND_LABELS[k] for k, _ in _on_signals(turnaround_breakdown)[:2] if k in TURNAROUND_LABELS]
    b = ", ".join(bottom_names) if bottom_names else "바닥 신호"
    t = ", ".join(turn_names) if turn_names else "턴어라운드 신호"
    return f"바닥 신호({b})와 턴어라운드 신호({t})가 동시에 확인됐습니다."


def explain_result(result: dict) -> dict:
    """screener.py의 종목별 결과 dict 하나를 받아
       {"summary": str|None, "bottom": str, "turnaround": str} 반환."""
    return {
        "summary": explain_summary(result.get("status"), result.get("breakdown"), result.get("turnaround_breakdown")),
        "bottom": explain_bottom(result.get("breakdown"), result.get("raw")),
        "turnaround": explain_turnaround(result.get("turnaround_breakdown"), result.get("turnaround_raw")),
    }


# ---------- 스모크 테스트 (네트워크 없이 로직 검증) ----------
if __name__ == "__main__":
    print("=== explain.py 스모크 테스트 ===\n")

    sample = {
        "status": "confirmed_turnaround",
        "breakdown": {
            "volume_dryness": 88.0, "accumulation": 76.0, "short_covering": 62.0,
            "pbr_low": 92.0, "dividend_yield": None, "relative_strength": 71.0,
            "volatility_squeeze": 40.0,
        },
        "raw": {
            "volume_dryness": {"ratio": 0.35},
            "accumulation": {"net_buy_krw": 2.5e11, "intensity_pct": 2.1, "price_change_pct": 2.8},
            "short_covering": {"current_ratio_pct": 0.4, "max_ratio_3m_pct": 1.0, "pct_of_max": 40.0},
            "pbr_low": {"pbr": 0.82, "percentile": 8.0},
            "dividend_yield": {"div_pct": 0.0, "percentile": None},
            "relative_strength": {"stock_ret_pct": -5.0, "index_ret_pct": -20.0, "excess_pct": 15.0},
            "volatility_squeeze": {"bandwidth_pct": 4.5, "percentile": 45.0},
        },
        "turnaround_score": 74.0,
        "turnaround_breakdown": {
            "volume_surge": 82.0, "ma_breakout": 90.0, "short_term_breakout": 40.0,
            "relative_strength_accel": 71.0, "accumulation_accel": 30.0,
        },
        "turnaround_raw": {
            "volume_surge": {"ratio": 2.2},
            "ma_breakout": {"close": 105, "ma20": 103, "ma60": 100, "close_vs_ma60_pct": 5.0, "ma20_vs_ma60_pct": 3.0},
            "short_term_breakout": {"close": 84, "high60": 100, "pct_of_high60": 84.0},
            "relative_strength_accel": {"rs_recent10_pct": 7.0, "rs_prior10_pct": -2.0, "accel_pct": 9.0},
            "accumulation_accel": {"recent5_avg_krw": 1.0e8, "prior15_avg_krw": 5.0e8, "ratio": 0.2},
        },
    }

    out = explain_result(sample)
    print("요약:", out["summary"])
    print("바닥 신호 설명:", out["bottom"])
    print("턴어라운드 신호 설명:", out["turnaround"])

    assert out["summary"] is not None, "confirmed_turnaround는 요약이 있어야 함"
    assert "PBR" in out["bottom"], "PBR 문장이 포함돼야 함(92점, 임계값 이상)"
    assert "배당" not in out["bottom"], "None 점수 신호는 언급하면 안 됨"
    assert "변동성" not in out["bottom"], "40점(임계값 미만)은 생략돼야 함"
    assert "이평선" in out["turnaround"], "90점 이평선 돌파는 포함돼야 함"
    assert "단기 고점" not in out["turnaround"], "40점(임계값 미만)은 생략돼야 함"
    print("\n✅ 모든 로직 검증 통과 (요약 생성 / 임계값 필터 / None 배제)")
