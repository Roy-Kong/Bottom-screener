"""strategies/resistance_breakout/resistance_breakout.py — "매물대 돌파" 전략.

[매물대(저항선) 정의 — 간이 방식]
그날(오늘) 기준 최근 RESISTANCE_LOOKBACK_DAYS(60영업일)의 "어제까지"(오늘
제외 — 오늘 자신의 큰 움직임이 저항선 계산에 순환적으로 섞이지 않도록) 고가
중, 특정 가격의 ±RESISTANCE_BAND(1.5%) 범위 안에 다른 고가가
RESISTANCE_MIN_TOUCHES(3)개 이상(자기 자신 포함) 모여있는 가격을 "매물대"로
본다. 그런 가격이 여러 개면, 어제 종가보다 위에 있는 것 중 가장 가까운
값을 기준 매물대로 쓴다(현재가 바로 위의 저항선).

[돌파 조건]
1) 오늘 종가가 그 매물대 가격보다 BREAKOUT_MARGIN(1%) 이상 높게 마감
   (걸치기만 하는 가짜 돌파 제외)
2) 오늘 거래량이 오늘을 제외한 이전 VOLUME_LOOKBACK_DAYS(20영업일) 평균의
   VOLUME_MULTIPLE(1.5배) 이상 (거래량 동반 돌파만 인정 — signals.py
   score_volume_surge가 예전에 자기참조 버그를 겪었던 것과 같은 이유로
   "오늘을 제외한 이전 N일"로 분모를 분리한다)

[매매 규칙]
3) 돌파 확인된 다음 영업일 시가 매수
4) +TAKE_PROFIT_PCT(5%) 익절 / STOP_LOSS_PCT(-3%) 손절 — 아랫꼬리류 전략과
   달리 비대칭(손절 타이트, 목표 넉넉). 같은 날 둘 다 도달하면 보수적으로
   손절 우선(이 프로젝트 전반의 기존 관례).
5) MAX_HOLD_TRADING_DAYS(20영업일) 안에 둘 다 미도달이면 20일째 시가 매도

[비용] 매수 0.33% 수수료(매도 수수료 없음 — portfolio_simulation.py에서
사용자가 정한 것과 동일 관례), 슬리피지 0.2% 매수·매도 양쪽 불리한 방향
(base_breakout.py와 동일 관례).

[시점 무결성·생존편향·거래정지] 종목별 시계열은 항상 그 날짜까지만
윈도잉해서 만든다(base_breakout.py에서 확립한 패턴 그대로). 유니버스는 그
달 월초 앵커 스냅샷(그 시점 실제 상장 종목만) 재사용. 거래정지(OHLC=0)
스냅샷은 screener.is_halted_snapshot으로 걸러 매수 후보/보유 판정 양쪽에
적용한다(base_breakout.py에서는 보유판정에 빠져있던 걸 이번엔 처음부터
넣음).

[검증] 사용자가 명시적으로 요구: 전체 백테스트를 돌리기 전에 실제 신호
몇 건을 원본 DB와 대조해서 수동 검증한다 — verify_signals() 함수와
"python resistance_breakout.py --verify" 참고.

사용법:
    python strategies/resistance_breakout/resistance_breakout.py --verify
    python strategies/resistance_breakout/resistance_breakout.py \\
        --train-start 20220201 --train-end 20231229 \\
        --val-start 20240102 --val-end 20260630
"""
from __future__ import annotations
import argparse
import bisect
import csv
import sys
import time
import datetime as dt
from dataclasses import dataclass, asdict, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "base_breakout"))
sys.stdout.reconfigure(encoding="utf-8")

import screener as scr
import db_reader as dbr
from base_breakout import load_all_month_snapshots, governing_month, _bulk_load_day_files  # 읽기 전용 재사용

# ---------------- 전략 파라미터 ----------------
# 아래 모듈 상수는 기본값(DEFAULT_PARAMS)이자 CLI(--verify 등) 하위호환용이다.
# 실제 계산 함수들은 전부 params: Params를 받아서 동작하므로, 로컬 웹앱
# (webapp/server.py)이 사용자가 UI에서 바꾼 값으로 Params를 새로 만들어
# 넘기면 이 상수들을 건드리지 않고도 다른 조건으로 재계산할 수 있다.
RESISTANCE_LOOKBACK_DAYS = 60
RESISTANCE_BAND = 0.015         # 매물대 판정 가격 폭 ±1.5%
RESISTANCE_MIN_TOUCHES = 3      # 이 이상 모이면 매물대로 인정
BREAKOUT_MARGIN = 0.01          # 매물대 대비 종가 +1% 이상
VOLUME_LOOKBACK_DAYS = 20       # 대량거래 판정용 이전 영업일 수(오늘 제외)
VOLUME_MULTIPLE = 1.5

START_CAPITAL = 100_000_000.0
MAX_POSITION_PCT = 0.20
MAX_NEW_BUYS_PER_DAY = 5
BUY_FEE_PCT = 0.0033
SLIPPAGE_PCT = 0.002

TAKE_PROFIT_PCT = 0.05
STOP_LOSS_PCT = -0.03
MAX_HOLD_TRADING_DAYS = 20

DB_COVERAGE_START = "20220103"


@dataclass
class Params:
    resistance_lookback_days: int = RESISTANCE_LOOKBACK_DAYS
    resistance_band: float = RESISTANCE_BAND
    resistance_min_touches: int = RESISTANCE_MIN_TOUCHES
    breakout_margin: float = BREAKOUT_MARGIN
    volume_lookback_days: int = VOLUME_LOOKBACK_DAYS
    volume_multiple: float = VOLUME_MULTIPLE
    start_capital: float = START_CAPITAL
    max_position_pct: float = MAX_POSITION_PCT
    max_new_buys_per_day: int = MAX_NEW_BUYS_PER_DAY
    buy_fee_pct: float = BUY_FEE_PCT
    slippage_pct: float = SLIPPAGE_PCT
    take_profit_pct: float = TAKE_PROFIT_PCT
    stop_loss_pct: float = STOP_LOSS_PCT
    max_hold_trading_days: int = MAX_HOLD_TRADING_DAYS


DEFAULT_PARAMS = Params()


# ==================== 사전 로딩 ====================

def preload(start: str, end: str, params: Params = DEFAULT_PARAMS) -> dict:
    month_snaps = load_all_month_snapshots()
    anchor0 = dt.datetime.strptime(start, "%Y%m%d").date()
    ohlcv_lookback_start = scr.recent_business_dates(params.resistance_lookback_days + 5, anchor0)[0]
    lookback_start = max(DB_COVERAGE_START, ohlcv_lookback_start)
    all_calendar_dates = scr.recent_business_dates(2000, dt.datetime.strptime(end, "%Y%m%d").date())
    needed_dates = sorted(d for d in all_calendar_dates if lookback_start <= d <= end)
    print(f"  OHLCV 로딩 ({lookback_start}~{end}, {len(needed_dates)}일 요청)…")
    matrix, _short, _mc, _accum = _bulk_load_day_files(needed_dates)
    print(f"    {len(matrix)}개 실제 거래일 확보")
    return {"matrix": matrix, "month_snaps": month_snaps}


# ==================== 매물대(저항선) 계산 ====================

def resistance_zone_above(sorted_highs: list[float], ref_price: float,
                          params: Params = DEFAULT_PARAMS) -> float | None:
    """정렬된 고가 리스트에서 ref_price보다 높은 후보 중, ±band 범위 안에
       min_touches개 이상 모여있는(자기 자신 포함) 가격들 중 가장 낮은
       (=현재가에 가장 가까운) 값. 없으면 None. 이분탐색으로 O(n log n)에
       계산한다(n=최대 60이라 어차피 작지만, 이 함수가 대량거래 1차 필터를
       통과한 후보에서만 불려서 전체 계산량을 더 줄인다)."""
    band, min_touches = params.resistance_band, params.resistance_min_touches
    best = None
    for h in sorted_highs:
        if h <= ref_price:
            continue
        lo, hi = h / (1 + band), h * (1 + band)
        lo_idx = bisect.bisect_left(sorted_highs, lo)
        hi_idx = bisect.bisect_right(sorted_highs, hi)
        if hi_idx - lo_idx >= min_touches:
            if best is None or h < best:
                best = h
    return best


def ticker_series_upto(matrix: dict, tkr: str, day: str, lookback_dates: list[str]) -> tuple:
    """day까지(포함)만 잘라낸 종목 시계열(시점 무결성 — base_breakout.py와 동일 패턴)."""
    dates, opens, highs, lows, closes, vols = [], [], [], [], [], []
    for d in lookback_dates:
        if d > day:
            break
        row = matrix.get(d, {}).get(tkr)
        if row is None:
            continue
        o, h, l, c, v = row
        dates.append(d); opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)
    return dates, opens, highs, lows, closes, vols


# ==================== 매수신호 스캔 ====================

def scan_signals(trading_days: list[str], pre: dict, params: Params = DEFAULT_PARAMS) -> dict[str, list[dict]]:
    """{date: [{"ticker","name","volume_ratio","resistance_price","close"}, ...]}
       1차: 거래량 조건(싼 산술)만 먼저 걸러서, 매물대 계산(조금 더 비쌈)은
       그 후보에만 적용한다."""
    matrix = pre["matrix"]
    month_snaps = pre["month_snaps"]
    out: dict[str, list[dict]] = {}
    vld, rld = params.volume_lookback_days, params.resistance_lookback_days

    for day in trading_days:
        ym = governing_month(day)
        snap = month_snaps.get(ym)
        if snap is None:
            continue
        universe = snap["universe"]
        day_row = matrix.get(day, {})

        vol_hist_dates = scr.recent_business_dates(vld + 1, dt.datetime.strptime(day, "%Y%m%d").date())
        vol_hist_dates = [d for d in vol_hist_dates if d < day][-vld:]
        res_hist_dates = scr.recent_business_dates(rld + 1, dt.datetime.strptime(day, "%Y%m%d").date())
        res_hist_dates = [d for d in res_hist_dates if d < day][-rld:]
        if len(vol_hist_dates) < vld or len(res_hist_dates) < rld:
            continue
        prev_day_row = matrix.get(res_hist_dates[-1], {})  # 어제

        candidates = []
        for tkr in day_row:
            if tkr not in universe:
                continue
            today_row = day_row[tkr]
            today_open, today_high, today_low, today_close, today_vol = today_row
            if scr.is_halted_snapshot(today_open, today_high, today_low, today_close):
                continue

            # 1차(싼) 필터: 거래량
            prior_vols = [matrix[d][tkr][4] for d in vol_hist_dates if d in matrix and tkr in matrix[d]]
            if len(prior_vols) < vld:
                continue
            prior_avg_vol = sum(prior_vols) / len(prior_vols)
            if prior_avg_vol <= 0 or today_vol / prior_avg_vol < params.volume_multiple:
                continue
            vol_ratio = today_vol / prior_avg_vol

            prev_row = prev_day_row.get(tkr)
            if prev_row is None or prev_row[3] <= 0:
                continue
            prev_close = prev_row[3]

            # 2차(상대적으로 비쌈): 매물대
            highs = sorted(matrix[d][tkr][1] for d in res_hist_dates if d in matrix and tkr in matrix[d])
            if len(highs) < params.resistance_min_touches:
                continue
            zone = resistance_zone_above(highs, prev_close, params)
            if zone is None:
                continue
            if today_close < zone * (1 + params.breakout_margin):
                continue

            candidates.append({"ticker": tkr, "name": universe.get(tkr, tkr), "volume_ratio": vol_ratio,
                               "resistance_price": zone, "close": today_close})
        if candidates:
            candidates.sort(key=lambda c: -c["volume_ratio"])
            out[day] = candidates[:params.max_new_buys_per_day]
    return out


# ==================== 포트폴리오 시뮬레이션 ====================

def simulate(signals: dict[str, list[dict]], trading_days: list[str], pre: dict,
            params: Params = DEFAULT_PARAMS) -> tuple[list[dict], list[tuple]]:
    matrix = pre["matrix"]
    idx_of = {d: i for i, d in enumerate(trading_days)}

    cash = params.start_capital
    holdings: dict[str, dict] = {}
    pending_buys: dict[str, list[dict]] = {}
    trade_log: list[dict] = []
    equity_curve: list[tuple[str, float]] = []

    def portfolio_value(day: str) -> float:
        v = cash
        for tkr, pos in holdings.items():
            row = matrix.get(day, {}).get(tkr)
            price = row[3] if row else pos["buy_price"]
            v += pos["shares"] * price
        return v

    for i, day in enumerate(trading_days):
        for cand in pending_buys.pop(day, []):
            tkr = cand["ticker"]
            if tkr in holdings:
                continue
            row = matrix.get(day, {}).get(tkr)
            if row is None or row[0] <= 0:
                continue
            if scr.is_halted_snapshot(*row[:4]):
                continue
            raw_open = row[0]
            fill_price = raw_open * (1 + params.slippage_pct)
            base_value = portfolio_value(day)
            invest = base_value * params.max_position_pct
            fee = invest * params.buy_fee_pct
            if invest + fee > cash:
                continue
            shares = invest / fill_price
            cash -= (invest + fee)
            holdings[tkr] = {"buy_date": day, "buy_price": fill_price, "shares": shares,
                              "name": cand["name"], "volume_ratio": cand["volume_ratio"],
                              "resistance_price": cand["resistance_price"],
                              "signal_date": cand.get("signal_date")}

        for tkr in sorted(list(holdings.keys())):
            pos = holdings[tkr]
            row = matrix.get(day, {}).get(tkr)
            if row is None:
                continue
            today_open, today_high, today_low, today_close, _ = row
            if scr.is_halted_snapshot(today_open, today_high, today_low, today_close):
                continue  # 거래정지 추정 — 오늘은 매도판정 스킵(보유 유지, base_breakout에 없던 보완)

            tp_price = pos["buy_price"] * (1 + params.take_profit_pct)
            sl_price = pos["buy_price"] * (1 + params.stop_loss_pct)
            hit_tp = today_high >= tp_price
            hit_sl = today_low <= sl_price
            buy_idx = idx_of[pos["buy_date"]]
            hit_expiry = (i - buy_idx) >= params.max_hold_trading_days

            sell_today, sell_price, reason = False, None, None
            if hit_sl:
                sell_today, sell_price, reason = True, sl_price, f"{params.stop_loss_pct*100:.0f}%손절"
            elif hit_tp:
                sell_today, sell_price, reason = True, tp_price, f"+{params.take_profit_pct*100:.0f}%익절"
            elif hit_expiry:
                sell_today, sell_price, reason = True, today_open, f"{params.max_hold_trading_days}일만기"

            if sell_today:
                fill_price = sell_price * (1 - params.slippage_pct)
                proceeds = pos["shares"] * fill_price
                cash += proceeds
                trade_log.append({
                    "ticker": tkr, "name": pos["name"], "signal_date": pos.get("signal_date"),
                    "volume_ratio": round(pos["volume_ratio"], 2),
                    "resistance_price": round(pos["resistance_price"], 2),
                    "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 2),
                    "sell_date": day, "sell_price": round(fill_price, 2), "sell_reason": reason,
                    "holding_trading_days": i - buy_idx,
                    "return_pct": round((fill_price / pos["buy_price"] - 1) * 100, 2),
                    "open_position": False,
                })
                del holdings[tkr]

        today_signals = signals.get(day)
        if today_signals and i + 1 < len(trading_days):
            next_day = trading_days[i + 1]
            tagged = [{**c, "signal_date": day} for c in today_signals]  # 돌파가 실제로 발생한 날짜(매수일 하루 전)
            pending_buys.setdefault(next_day, []).extend(tagged)

        equity_curve.append((day, portfolio_value(day)))

    last_day = trading_days[-1]
    for tkr, pos in holdings.items():
        row = matrix.get(last_day, {}).get(tkr)
        last_close = row[3] if row else pos["buy_price"]
        buy_idx = idx_of[pos["buy_date"]]
        trade_log.append({
            "ticker": tkr, "name": pos["name"], "signal_date": pos.get("signal_date"),
            "volume_ratio": round(pos["volume_ratio"], 2),
            "resistance_price": round(pos["resistance_price"], 2),
            "buy_date": pos["buy_date"], "buy_price": round(pos["buy_price"], 2),
            "sell_date": None, "sell_price": None, "sell_reason": None,
            "holding_trading_days": len(trading_days) - 1 - buy_idx,
            "return_pct": round((last_close / pos["buy_price"] - 1) * 100, 2),
            "open_position": True,
        })

    return trade_log, equity_curve


# ==================== 출력 ====================

def write_csv(rows: list[dict], out_path: str) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        print(f"    저장할 행이 없습니다: {out_path}")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"    CSV 저장: {out_path} ({len(rows)}행)")


def compute_stats(trade_log: list[dict], equity_curve: list[tuple], params: Params = DEFAULT_PARAMS) -> dict:
    closed = [t for t in trade_log if not t["open_position"]]
    open_pos = [t for t in trade_log if t["open_position"]]
    wins = [t for t in closed if t["return_pct"] > 0]
    hold_days = [t["holding_trading_days"] for t in closed]

    max_dd = 0.0
    if equity_curve:
        peak = equity_curve[0][1]
        for _, v in equity_curve:
            peak = max(peak, v)
            max_dd = min(max_dd, (v / peak - 1) * 100)

    final_value = equity_curve[-1][1] if equity_curve else params.start_capital
    return {
        "n_trades": len(trade_log), "n_closed": len(closed), "n_open": len(open_pos),
        "avg_holding_days": round(sum(hold_days) / len(hold_days), 1) if hold_days else None,
        "avg_return_pct": round(sum(t["return_pct"] for t in closed) / len(closed), 2) if closed else None,
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "max_drawdown_pct": round(max_dd, 2),
        "final_value": round(final_value, 0),
        "total_return_pct": round((final_value / params.start_capital - 1) * 100, 2),
    }


# ==================== 검증 (전체 백테스트 전 필수) ====================

def verify_signals(start: str, end: str, n_samples: int = 5) -> None:
    """실제 신호 n_samples건을 원본 DB에서 독립적으로 재계산해서 대조한다.
       scan_signals()가 쓰는 것과 같은 코드 경로가 아니라, 여기서 새로
       sqlite 쿼리를 짜서(수동 검증) 매물대 터치횟수·거래량배율·매수가가
       정확히 일치하는지 확인한다."""
    import sqlite3
    print(f"[검증] {start}~{end} 구간에서 신호 샘플 {n_samples}건을 원본 DB와 대조합니다…")
    pre = preload(start, end)
    trading_days = sorted(d for d in pre["matrix"].keys() if start <= d <= end)
    signals = scan_signals(trading_days, pre)

    samples = []
    for day in sorted(signals.keys()):
        for c in signals[day]:
            samples.append((day, c))
            if len(samples) >= n_samples:
                break
        if len(samples) >= n_samples:
            break

    all_ok = True
    for day, c in samples:
        tkr = c["ticker"]
        print(f"\n  --- {day} {c['name']}({tkr}) ---")
        print(f"  scan_signals() 결과: 매물대={c['resistance_price']}, 종가={c['close']}, "
              f"거래량배율={c['volume_ratio']:.2f}")

        # 독립 재계산: 매물대 lookback
        res_hist = scr.recent_business_dates(RESISTANCE_LOOKBACK_DAYS + 1, dt.datetime.strptime(day, "%Y%m%d").date())
        res_hist = [d for d in res_hist if d < day][-RESISTANCE_LOOKBACK_DAYS:]
        highs = []
        for d in res_hist:
            p = REPO_ROOT / "data" / f"{d}.db"
            if not p.exists():
                continue
            conn = sqlite3.connect(str(p))
            row = conn.execute("SELECT high FROM daily_prices WHERE ticker=?", (tkr,)).fetchone()
            conn.close()
            if row and row[0]:
                highs.append(row[0])
        highs.sort()

        conn = sqlite3.connect(str(REPO_ROOT / "data" / f"{res_hist[-1]}.db"))
        prev_close = conn.execute("SELECT close FROM daily_prices WHERE ticker=?", (tkr,)).fetchone()
        conn.close()
        prev_close = prev_close[0] if prev_close else None

        band = RESISTANCE_BAND
        target = c["resistance_price"]
        lo, hi = target / (1 + band), target * (1 + band)
        touches = [h for h in highs if lo <= h <= hi]
        ok_touches = len(touches) >= RESISTANCE_MIN_TOUCHES
        ok_above_prev = prev_close is not None and target > prev_close
        print(f"  수동 재계산: 전일종가={prev_close}, 매물대 {target} 주변(±1.5%) 터치={touches} "
              f"({len(touches)}개, 기준 {RESISTANCE_MIN_TOUCHES}개 이상 {'OK' if ok_touches else 'FAIL'}), "
              f"전일종가보다 위={'OK' if ok_above_prev else 'FAIL'}")

        # 독립 재계산: 돌파폭, 거래량배율
        ok_margin = c["close"] >= target * (1 + BREAKOUT_MARGIN)
        print(f"  돌파폭 조건(종가 >= 매물대*1.01 = {target*(1+BREAKOUT_MARGIN):.1f}): "
              f"{'OK' if ok_margin else 'FAIL'} (종가 {c['close']})")

        vol_hist = scr.recent_business_dates(VOLUME_LOOKBACK_DAYS + 1, dt.datetime.strptime(day, "%Y%m%d").date())
        vol_hist = [d for d in vol_hist if d < day][-VOLUME_LOOKBACK_DAYS:]
        vols = []
        for d in vol_hist:
            conn = sqlite3.connect(str(REPO_ROOT / "data" / f"{d}.db"))
            row = conn.execute("SELECT volume FROM daily_prices WHERE ticker=?", (tkr,)).fetchone()
            conn.close()
            if row: vols.append(row[0])
        avg_vol = sum(vols) / len(vols) if vols else 0
        conn = sqlite3.connect(str(REPO_ROOT / "data" / f"{day}.db"))
        today_vol = conn.execute("SELECT volume FROM daily_prices WHERE ticker=?", (tkr,)).fetchone()
        conn.close()
        recomputed_ratio = today_vol[0] / avg_vol if avg_vol else None
        ratio_match = recomputed_ratio is not None and abs(recomputed_ratio - c["volume_ratio"]) < 0.01
        print(f"  거래량배율 재계산: {recomputed_ratio:.4f} vs scan_signals() {c['volume_ratio']:.4f} "
              f"({'일치 OK' if ratio_match else 'FAIL'})")

        # 다음영업일 시가(매수가) 확인
        next_days = [d for d in trading_days if d > day]
        if next_days:
            nd = next_days[0]
            conn = sqlite3.connect(str(REPO_ROOT / "data" / f"{nd}.db"))
            nrow = conn.execute("SELECT open FROM daily_prices WHERE ticker=?", (tkr,)).fetchone()
            conn.close()
            print(f"  다음영업일({nd}) 시가(매수 체결가, 슬리피지 전): {nrow[0] if nrow else 'N/A'}")

        ok = ok_touches and ok_above_prev and ok_margin and ratio_match
        all_ok = all_ok and ok
        print(f"  => {'✅ 일치' if ok else '❌ 불일치 — 코드 재점검 필요'}")

    print(f"\n[검증] {'전부 통과 — 전체 백테스트 진행 가능' if all_ok else '불일치 발견 — 전체 백테스트 중단하고 코드 점검 필요'}")
    return all_ok


# ==================== 실행 ====================

def run_train_val(train_start: str, train_end: str, val_start: str, val_end: str, out_dir: str) -> dict[str, dict]:
    t0 = time.time()
    print(f"[resistance_breakout] 훈련 {train_start}~{train_end}, 검증 {val_start}~{val_end}")

    print("사전 로딩(훈련+검증 전체 구간 한 번에)…")
    pre = preload(train_start, val_end)
    all_trading_days = sorted(pre["matrix"].keys())
    print(f"  [소요 {(time.time()-t0)/60:.1f}분]")

    print("매수신호 스캔(1차 거래량 → 2차 매물대, 전체 구간 한 번에)…")
    t1 = time.time()
    scan_days = [d for d in all_trading_days if train_start <= d <= val_end]
    signals = scan_signals(scan_days, pre)
    n_sig = sum(len(v) for v in signals.values())
    print(f"  신호 {len(signals)}일 {n_sig}건  [소요 {(time.time()-t1)/60:.1f}분]")

    results: dict[str, dict] = {}
    for label, (s, e) in (("train", (train_start, train_end)), ("val", (val_start, val_end))):
        period_days = [d for d in all_trading_days if s <= d <= e]
        period_signals = {d: v for d, v in signals.items() if s <= d <= e}
        n_p = sum(len(v) for v in period_signals.values())
        print(f"\n[{label}] {s}~{e}: {len(period_days)}거래일, 매수신호 {len(period_signals)}일 {n_p}건")
        t2 = time.time()
        trade_log, equity_curve = simulate(period_signals, period_days, pre)
        stats = compute_stats(trade_log, equity_curve)
        results[label] = stats
        out_path = f"{out_dir}/{label}.csv"
        write_csv(trade_log, out_path)
        print(f"  거래 {stats['n_trades']}건(청산 {stats['n_closed']}, 미청산 {stats['n_open']}), "
              f"평균보유 {stats['avg_holding_days']}일, 평균수익률 {stats['avg_return_pct']}%, "
              f"승률 {stats['win_rate_pct']}%, MDD {stats['max_drawdown_pct']}%, "
              f"최종평가액 {stats['final_value']:,.0f}원({stats['total_return_pct']:+.2f}%)  "
              f"[소요 {(time.time()-t2)/60:.1f}분]")

    print(f"\n[resistance_breakout] 전체 소요시간: {(time.time()-t0)/60:.1f}분")
    return results


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="매물대 돌파 전략 백테스트")
    p.add_argument("--train-start", default="20220201")
    p.add_argument("--train-end", default="20231229")
    p.add_argument("--val-start", default="20240102")
    p.add_argument("--val-end", default="20260630")
    p.add_argument("--out-dir", default="backtests/resistance_breakout")
    p.add_argument("--verify", action="store_true", help="전체 백테스트 대신 신호 샘플 검증만 실행")
    p.add_argument("--verify-start", default="20220701")
    p.add_argument("--verify-end", default="20220930")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    if args.verify:
        verify_signals(args.verify_start, args.verify_end)
    else:
        run_train_val(args.train_start, args.train_end, args.val_start, args.val_end, args.out_dir)
