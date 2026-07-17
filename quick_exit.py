"""
quick_exit.py — "단기 익절 전략" 검증. backtest.py의 상위 10개(2022-10-31 기준)를
재사용해서, 각 종목이 기준일 이후 최대 max_days일 동안 +5%/+10%에 며칠 만에
도달했는지(그날 고가 기준 — 익절은 종가가 아니라 장중 터치로 판단하는 게 현실적)
확인한다. 이미 backtest.py가 전종목을 스캔해서 상위 10개를 뽑아 놓았으므로,
여기서는 그 결과를 상수로 재사용하고 개별 종목 시세만 다시 조회한다(전종목
재스캔은 낭비).

사용법: python quick_exit.py [YYYYMMDD] [max_days]
  기본값: 20221031, 90
"""
from __future__ import annotations
import sys
import datetime as dt

from pykrx import stock
import screener as scr

# backtest.py run_backtest("20221031", 10) 결과 (2026-07-18 실행) — 재사용
TOP10 = [
    # (ticker, name, anchor_close, score)
    ("047040", "대우건설", 4205, 76.0),
    ("195940", "HK이노엔", 36350, 75.6),
    ("120110", "코오롱인더", 43500, 71.4),
    ("005690", "파미셀", 10200, 70.6),
    ("028050", "삼성E&A", 23850, 69.9),
    ("402340", "SK스퀘어", 36950, 69.7),
    ("011200", "HMM", 19100, 69.1),
    ("091990", "셀트리온헬스케어", 69500, 69.0),
    ("067630", "HLB생명과학", 12150, 68.7),
    ("004370", "농심", 302000, 68.3),
]


def quick_exit_analysis(ticker: str, anchor_close: float, asof: str,
                        targets: tuple[float, ...] = (5.0, 10.0),
                        max_days: int = 90) -> dict | None:
    """anchor_close 대비 +targets%에 며칠 만에 도달했는지(그날 고가 기준).
       목표별로 도달 못 했으면 그 목표는 None. 관찰기간 내 최고/최저 수익률
       (고가/저가 기준)과 기간 마지막 거래일 종가 기준 손익도 같이 반환."""
    start = dt.datetime.strptime(asof, "%Y%m%d").date()
    end = start + dt.timedelta(days=max_days)
    try:
        df = stock.get_market_ohlcv_by_date(scr.yyyymmdd(start), scr.yyyymmdd(end), ticker)
    except Exception:
        df = None
    if df is None or df.empty:
        return None

    reach_days: dict[float, int | None] = {t: None for t in targets}
    max_ret = min_ret = final_ret = None
    for idx_date, row in df.iterrows():
        try:
            d = idx_date.date()
        except AttributeError:
            d = dt.datetime.strptime(str(idx_date), "%Y%m%d").date()
        if d <= start:
            continue
        elapsed = (d - start).days
        high = float(row.get("고가", 0) or 0)
        low = float(row.get("저가", 0) or 0)
        close = float(row.get("종가", 0) or 0)
        if high <= 0 or low <= 0 or close <= 0:
            continue
        high_ret = (high / anchor_close - 1) * 100
        low_ret = (low / anchor_close - 1) * 100
        close_ret = (close / anchor_close - 1) * 100

        max_ret = high_ret if max_ret is None else max(max_ret, high_ret)
        min_ret = low_ret if min_ret is None else min(min_ret, low_ret)
        final_ret = close_ret   # 마지막까지 갱신되면 관찰기간 종료 시점 종가 기준 손익

        for t in targets:
            if reach_days[t] is None and high_ret >= t:
                reach_days[t] = elapsed

    return {"reach_days": reach_days, "max_ret": max_ret, "min_ret": min_ret, "final_ret": final_ret}


def run(anchor_str: str = "20221031", max_days: int = 90):
    targets = (5.0, 10.0)
    print(f"[단기익절] 기준일 {anchor_str}, 관찰기간 최대 {max_days}일(달력일), 목표 {targets}")
    print("(도달 판정은 그날 '고가' 기준 — 종가가 목표를 못 넘어도 장중 터치했으면 도달로 침)\n")

    rows = []
    for ticker, name, anchor_close, score in TOP10:
        r = quick_exit_analysis(ticker, anchor_close, anchor_str, targets, max_days)
        if r is None:
            print(f"  {name}({ticker}): 데이터 없음")
            continue
        rows.append((ticker, name, score, r))
        rd = r["reach_days"]
        r5 = f"{rd[5.0]}일" if rd[5.0] is not None else "미도달"
        r10 = f"{rd[10.0]}일" if rd[10.0] is not None else "미도달"
        print(f"  {name}({ticker}) 점수={score}: +5%={r5} +10%={r10} "
              f"관찰기간최고={r['max_ret']:+.1f}% 최저={r['min_ret']:+.1f}% "
              f"{max_days}일차종가손익={r['final_ret']:+.1f}%")

    print(f"\n[단기익절] 목표별 요약:")
    for t in targets:
        reached = [r for (_, _, _, r) in rows if r["reach_days"][t] is not None]
        missed = [r for (_, _, _, r) in rows if r["reach_days"][t] is None]
        n = len(rows)
        win_rate = len(reached) / n * 100 if n else 0
        avg_days = (sum(r["reach_days"][t] for r in reached) / len(reached)) if reached else None
        avg_days_str = f"{avg_days:.1f}일" if avg_days is not None else "N/A(도달 종목 없음)"
        print(f"\n  === +{t:.0f}% 목표 ===")
        print(f"  승률: {len(reached)}/{n} ({win_rate:.0f}%), 평균 도달 소요일수: {avg_days_str}")

        miss_final = [r["final_ret"] for r in missed if r["final_ret"] is not None]
        winners_but_missed = [v for v in miss_final if v >= 0]
        losers = [v for v in miss_final if v < 0]
        wb_str = f"{len(winners_but_missed)}개(평균 {sum(winners_but_missed)/len(winners_but_missed):+.1f}%)" \
            if winners_but_missed else "0개"
        ls_str = f"{len(losers)}개(평균 {sum(losers)/len(losers):+.1f}%)" if losers else "0개"
        print(f"  미도달 {len(missed)}개의 {max_days}일차 손익: 플러스 {wb_str}, 마이너스 {ls_str}")

        loss_rate = len(missed) / n if n else 0
        avg_loss = abs(sum(losers) / len(losers)) if losers else 0.0
        ev = (win_rate / 100) * t - loss_rate * avg_loss
        print(f"  기댓값 = 승률{win_rate:.0f}%×목표{t:.0f}% - 미도달률{loss_rate*100:.0f}%×미도달손실평균{avg_loss:.1f}% "
              f"= {ev:+.2f}%")

    print(f"\n[단기익절] 주의: 위 기댓값은 요청받은 단순 공식(승률×목표% - 미도달률×미도달 중 "
          f"손실평균)을 그대로 계산한 것이다. 미도달했지만 그 시점엔 플러스였던 종목은 이 공식에서 "
          f"이익에도 손실에도 안 잡혀 0으로 취급된다 — 그만큼 실제보다 기댓값이 보수적으로(낮게) "
          f"나올 수 있다. 또한 이건 상위 10개 중 하나뿐인 관찰(2022-10-31)이라 이것만으로 "
          f"'+5%가 낫다/+10%가 낫다'를 일반화하기엔 표본이 작다.")


if __name__ == "__main__":
    anchor_arg = sys.argv[1] if len(sys.argv) > 1 else "20221031"
    days_arg = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    run(anchor_arg, days_arg)
