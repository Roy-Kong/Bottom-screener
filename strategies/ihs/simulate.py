"""strategies/ihs/simulate.py — 역헤드앤숄더 신호만으로 수익률 시뮬레이션.

바닥/턴어라운드 시그널 체계와 완전히 독립(사용자 지시) — detect_ihs()가 낸
신호(status: forming/retest/breakout)만으로 가상매매를 돌린다.

[매매 규칙]
- 진입: 어떤 날 D에 어떤 종목의 새 패턴이 처음으로 params.statuses 중 하나(기본
  breakout)에 도달하고 score>=min_score면, D+1일 시가에 매수(다음 영업일 시가
  매수는 base_breakout.py 등 기존 전략들과 동일 컨벤션).
  같은 패턴(ticker, d_ls로 식별)은 한 번만 진입한다 — 여러 날 연속 breakout
  상태라고 매일 다시 사지 않는다.
- 청산: 보유 중 그날 저가<=stop이면 손절(stop가 체결), 고가>=target이면
  익절(target가 체결) — 둘 다 걸리면 보수적으로 손절 우선. max_hold_trading_days
  영업일 초과 시 시가 청산(만기청산).
- 슬롯: max_slots개, 슬롯당 자본 1/max_slots 균등배분(portfolio_simulation.py와
  동일 컨벤션). 빈 슬롯이 없으면 그날 신규 신호는 건너뛴다(다음날 재평가하지
  않음 — 그 시점 신호는 소멸).
- 비용: 매수 시 buy_fee_pct(기본 0.33%), 슬리피지 slippage_pct(기본 0.2%)는
  매수·매도 양쪽 다 불리한 방향(base_breakout.py와 동일 컨벤션).

[시점 무결성] 각 날짜 D의 신호 판단은 detect_ihs(df_전체이력, cfg, as_of=D)로
계산한다 — as_of 절단은 detect_ihs 내부에서 처리하므로(날짜 문자열 비교),
여기서는 종목별 전체 이력 DataFrame을 한 번만 만들어 재사용하고 매 호출마다
다시 만들지 않는다(성능 — base_breakout.py의 "전체 기간 한 번에 프리로드"
설계와 동일한 이유)."""
from __future__ import annotations
import sys
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import db_reader as dbr
import screener as scr

from ihs_screener import IHSConfig, detect_ihs


@dataclass
class SimParams:
    start: str
    end: str
    cfg: IHSConfig = field(default_factory=IHSConfig)
    statuses: tuple[str, ...] = ("breakout",)
    min_score: float = 50.0
    max_slots: int = 10
    max_hold_trading_days: int = 60
    buy_fee_pct: float = 0.0033
    slippage_pct: float = 0.002
    start_capital: float = 100_000_000.0
    lookback_trading_days: int = 320   # detect_ihs 패턴탐지에 필요한 최대 과거 구간
    apply_liquidity_filter: bool = True
    universe: list[str] | None = None  # None이면 기간 내 등장한 전종목
    top_n_by_liquidity: int | None = 800  # None=전종목. detect_ihs가 종목당 ~20ms라
        # 전종목(~2400개)×수백 거래일을 매일 스캔하면 시간이 너무 오래 걸려서(수십분+),
        # 시작일 기준 최근 20일 평균거래대금 상위 N종목으로만 제한하는 속도 옵션.
        # 매물대 돌파 웹앱의 top_n_market_cap과 같은 취지 — 전략의 성격이
        # 바뀐다는 점(소형주 배제)을 UI에서 경고할 것.


@dataclass
class Trade:
    ticker: str
    entry_status: str
    d_ls: str
    d_head: str
    d_rs: str
    d_breakout: str | None
    score: float
    signal_date: str
    buy_date: str
    buy_price: float
    sell_date: str | None = None
    sell_price: float | None = None
    sell_reason: str | None = None  # "target" | "stop" | "max_hold" | "open"(미청산)
    shares: float = 0.0
    pnl: float = 0.0
    return_pct: float = 0.0
    target: float = 0.0    # 진입 시점 detect_ihs가 계산한 목표가(청산 판정용, 로그 출력 제외)
    stop: float = 0.0      # 진입 시점 detect_ihs가 계산한 손절가(청산 판정용, 로그 출력 제외)


def _load_universe_series(params: SimParams) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """[start-lookback, end] 구간을 한 번에 벌크 로드해서 종목별 전체이력
       DataFrame을 한 번씩만 만든다(이후 detect_ihs가 as_of로 반복 절단)."""
    all_dates = db.existing_dates()
    trading_dates_in_range = [d for d in all_dates if params.start <= d <= params.end]
    if not trading_dates_in_range:
        return {}, []
    idx_start = all_dates.index(trading_dates_in_range[0])
    lookback_start_idx = max(0, idx_start - params.lookback_trading_days)
    window_dates = all_dates[lookback_start_idx: all_dates.index(trading_dates_in_range[-1]) + 1]

    print(f"[IHS sim] OHLCV 로딩 {window_dates[0]}~{window_dates[-1]} ({len(window_dates)}일)…", file=sys.stderr)
    matrix = dbr.load_ohlcv_matrix_from_db_full(window_dates)

    per_ticker: dict[str, list[tuple]] = {}
    for d in window_dates:
        for tkr, row in matrix.get(d, {}).items():
            per_ticker.setdefault(tkr, []).append((d,) + row)

    universe = params.universe
    if universe is None:
        universe = sorted(per_ticker.keys())

    if params.top_n_by_liquidity is not None:
        # start일 기준 가장 가까운 과거 20거래일 평균거래대금으로 랭킹.
        rank_dates = [d for d in window_dates if d <= trading_dates_in_range[0]][-20:]
        traded_value: dict[str, float] = {}
        n_days_seen: dict[str, int] = {}
        for d in rank_dates:
            for tkr, row in matrix.get(d, {}).items():
                close, vol = row[3], row[4]
                traded_value[tkr] = traded_value.get(tkr, 0.0) + close * vol
                n_days_seen[tkr] = n_days_seen.get(tkr, 0) + 1
        avg_value = {tkr: v / n_days_seen[tkr] for tkr, v in traded_value.items()}
        universe_set = set(universe)
        ranked = sorted((t for t in avg_value if t in universe_set), key=lambda t: avg_value[t], reverse=True)
        universe = ranked[: params.top_n_by_liquidity]
        print(f"[IHS sim] 유동성 상위 {len(universe)}종목으로 제한(top_n_by_liquidity={params.top_n_by_liquidity})", file=sys.stderr)

    dfs: dict[str, pd.DataFrame] = {}
    cfg = params.cfg
    for tkr in universe:
        rows = per_ticker.get(tkr)
        if not rows:
            continue
        rows = [r for r in rows if r[1] > 0 and r[2] > 0 and r[3] > 0 and r[4] > 0]
        if len(rows) < cfg.min_bars:
            continue
        rows.sort(key=lambda r: r[0])
        dfs[tkr] = pd.DataFrame({
            cfg.date_col: [r[0] for r in rows], cfg.open_col: [r[1] for r in rows],
            cfg.high_col: [r[2] for r in rows], cfg.low_col: [r[3] for r in rows],
            cfg.close_col: [r[4] for r in rows], cfg.volume_col: [r[5] for r in rows],
        })
    print(f"[IHS sim] 대상 종목 {len(dfs)}개", file=sys.stderr)
    return dfs, trading_dates_in_range


def _passes_liquidity(df_t: pd.DataFrame, as_of: str, cfg: IHSConfig) -> bool:
    sub = df_t[df_t[cfg.date_col] <= as_of]
    if len(sub) < cfg.liquidity_window:
        return False
    tail = sub.tail(cfg.liquidity_window)
    if tail[cfg.close_col].iloc[-1] < cfg.min_close:
        return False
    avg_val = (tail[cfg.close_col] * tail[cfg.volume_col]).mean()
    if avg_val < cfg.min_avg_trading_value:
        return False
    recent = sub.tail(5)
    if scr.is_trading_halted(
        recent[cfg.open_col].tolist(), recent[cfg.high_col].tolist(), recent[cfg.low_col].tolist(),
        recent[cfg.close_col].tolist(), recent[cfg.volume_col].tolist(),
    ):
        return False
    return True


def simulate(params: SimParams, progress_every: int = 20) -> dict:
    dfs, sim_dates = _load_universe_series(params)
    if not sim_dates:
        return {"trades": pd.DataFrame(), "equity": pd.DataFrame(), "stats": {}, "params": params}

    cfg = params.cfg
    cash = params.start_capital
    open_positions: dict[str, Trade] = {}          # ticker -> Trade(미청산)
    hold_days: dict[str, int] = {}
    pending_buys: dict[str, list[dict]] = {}        # buy_date -> [signal dict, ...]
    seen_patterns: set[tuple[str, str]] = set()     # (ticker, d_ls) 이미 진입 이력
    trades: list[Trade] = []
    equity_curve: list[dict] = []

    date_idx = {d: i for i, d in enumerate(sim_dates)}

    for i, day in enumerate(sim_dates):
        if progress_every and i % progress_every == 0:
            print(f"[IHS sim] {day} ({i+1}/{len(sim_dates)}) 보유={len(open_positions)} 현금={cash:,.0f}", file=sys.stderr)

        # ---- 1) 보유 포지션 청산 체크(그날 저가/고가로 stop/target) ----
        for tkr in list(open_positions.keys()):
            df_t = dfs[tkr]
            row = df_t[df_t[cfg.date_col] == day]
            if row.empty:
                continue  # 그날 거래정지/데이터없음 — 보유 유지
            o, h, l, c = row[cfg.open_col].iloc[0], row[cfg.high_col].iloc[0], row[cfg.low_col].iloc[0], row[cfg.close_col].iloc[0]
            trade = open_positions[tkr]
            hold_days[tkr] += 1

            sell_price = None
            reason = None
            # 목표가/손절가는 진입 시점 값 그대로 유지(재계산하지 않음 — 진입 당시
            # 확정된 패턴 기하 기준. 스펙에 재계산 규정 없음). 단, 신호일과 매수일(다음
            # 영업일 시가) 사이에 주가가 크게 움직이면 target<=buy_price(이미 목표가를
            # 넘어서 진입) 또는 stop>=buy_price(이미 손절가 아래로 진입)인 채로 포지션이
            # 열릴 수 있다 — 이 경우 "target 도달"인데 매수가보다 싸게 팔려 손실로
            # 찍히는(혹은 그 반대) 라벨 오염이 생겨서, 경제적으로 말이 되는 방향으로만
            # 각 조건을 활성화한다(실제로 브라우저 스모크테스트에서 이 버그를 발견함).
            target = trade.target
            stop = trade.stop
            valid_stop = stop < trade.buy_price
            valid_target = target > trade.buy_price
            if valid_stop and l <= stop:
                sell_price, reason = stop, "stop"
            elif valid_target and h >= target:
                sell_price, reason = target, "target"
            elif hold_days[tkr] >= params.max_hold_trading_days:
                sell_price, reason = o, "max_hold"

            if sell_price is not None:
                sell_price *= (1 - params.slippage_pct)
                proceeds = trade.shares * sell_price
                cash += proceeds
                trade.sell_date = day
                trade.sell_price = round(sell_price, 2)
                trade.sell_reason = reason
                trade.pnl = round(proceeds - trade.shares * trade.buy_price, 2)
                trade.return_pct = round(sell_price / trade.buy_price - 1, 4)
                trades.append(trade)
                del open_positions[tkr]
                del hold_days[tkr]

        # ---- 2) 예약된 매수 체결(시가) ----
        for sig in pending_buys.pop(day, []):
            if len(open_positions) >= params.max_slots:
                continue  # 슬롯 다 참 — 이 신호는 소멸(다음날 재평가 안 함)
            df_t = dfs[sig["ticker"]]
            row = df_t[df_t[cfg.date_col] == day]
            if row.empty:
                continue
            buy_price = float(row[cfg.open_col].iloc[0]) * (1 + params.slippage_pct)
            slot_capital = params.start_capital / params.max_slots
            if cash < slot_capital:
                continue
            fee_adj_price = buy_price * (1 + params.buy_fee_pct)
            shares = slot_capital / fee_adj_price
            cash -= shares * fee_adj_price
            trade = Trade(
                ticker=sig["ticker"], entry_status=sig["status"], d_ls=sig["d_ls"], d_head=sig["d_head"],
                d_rs=sig["d_rs"], d_breakout=sig["d_breakout"], score=sig["score"], signal_date=sig["signal_date"],
                buy_date=day, buy_price=round(fee_adj_price, 2), shares=shares,
                target=sig["target"], stop=sig["stop"],
            )
            open_positions[sig["ticker"]] = trade
            hold_days[sig["ticker"]] = 0

        # ---- 3) 오늘자 신규 신호 스캔(빈 슬롯 있을 때만, 있는 만큼만 예약) ----
        free_slots = params.max_slots - len(open_positions) - sum(len(v) for v in pending_buys.values())
        if free_slots > 0:
            candidates: list[dict] = []
            for tkr, df_t in dfs.items():
                if tkr in open_positions:
                    continue
                if df_t[df_t[cfg.date_col] == day].empty:
                    continue  # 그날 거래 데이터 없는 종목은 신규 진입 후보에서 제외
                if params.apply_liquidity_filter and not _passes_liquidity(df_t, day, cfg):
                    continue
                try:
                    patterns = detect_ihs(df_t, cfg, ticker=tkr, as_of=day)
                except Exception as e:
                    print(f"  [IHS sim] {tkr} {day} 검출 실패: {e}", file=sys.stderr)
                    continue
                for p in patterns:
                    key = (tkr, p.d_ls)
                    if key in seen_patterns:
                        continue
                    if p.status not in params.statuses or p.score < params.min_score:
                        continue
                    seen_patterns.add(key)
                    candidates.append({
                        "ticker": tkr, "status": p.status, "score": p.score, "d_ls": p.d_ls,
                        "d_head": p.d_head, "d_rs": p.d_rs, "d_breakout": p.d_breakout,
                        "signal_date": day, "target": p.target, "stop": p.stop,
                    })
            candidates.sort(key=lambda c: c["score"], reverse=True)
            next_day = sim_dates[i + 1] if i + 1 < len(sim_dates) else None
            if next_day is not None:
                for c in candidates[:free_slots]:
                    pending_buys.setdefault(next_day, []).append(c)

        mtm = cash
        for tkr, trade in open_positions.items():
            row = dfs[tkr][dfs[tkr][cfg.date_col] == day]
            px = float(row[cfg.close_col].iloc[0]) if not row.empty else trade.buy_price
            mtm += trade.shares * px
        equity_curve.append({"date": day, "cash": cash, "equity": mtm, "n_positions": len(open_positions)})

    # 미청산 포지션은 마지막 종가로 평가만 하고 realized 통계에는 포함하지 않음
    open_summary = []
    for tkr, trade in open_positions.items():
        row = dfs[tkr][dfs[tkr][cfg.date_col] == sim_dates[-1]]
        px = float(row[cfg.close_col].iloc[0]) if not row.empty else trade.buy_price
        trade.sell_reason = "open"
        trade.sell_price = round(px, 2)
        trade.return_pct = round(px / trade.buy_price - 1, 4)
        open_summary.append(trade)

    trades_df = pd.DataFrame([t.__dict__ for t in trades]) if trades else pd.DataFrame()
    open_df = pd.DataFrame([t.__dict__ for t in open_summary]) if open_summary else pd.DataFrame()
    equity_df = pd.DataFrame(equity_curve)

    stats = _compute_stats(trades_df, equity_df, params)
    return {"trades": trades_df, "open_positions": open_df, "equity": equity_df, "stats": stats, "params": params}


def _compute_stats(trades_df: pd.DataFrame, equity_df: pd.DataFrame, params: SimParams) -> dict:
    if trades_df.empty:
        return {"n_trades": 0}
    stats: dict = {"n_trades": len(trades_df)}
    stats["win_rate"] = round((trades_df["return_pct"] > 0).mean(), 4)
    stats["avg_return_pct"] = round(trades_df["return_pct"].mean(), 4)
    stats["median_return_pct"] = round(trades_df["return_pct"].median(), 4)
    stats["total_pnl"] = round(trades_df["pnl"].sum(), 0)
    if not equity_df.empty:
        stats["final_equity"] = round(equity_df["equity"].iloc[-1], 0)
        stats["total_return_pct"] = round(equity_df["equity"].iloc[-1] / params.start_capital - 1, 4)
        running_max = equity_df["equity"].cummax()
        drawdown = equity_df["equity"] / running_max - 1
        stats["max_drawdown_pct"] = round(drawdown.min(), 4)
    stats["by_sell_reason"] = {
        reason: {
            "n": int(len(g)), "win_rate": round((g["return_pct"] > 0).mean(), 4),
            "avg_return_pct": round(g["return_pct"].mean(), 4),
        }
        for reason, g in trades_df.groupby("sell_reason")
    }
    stats["by_entry_status"] = {
        status: {
            "n": int(len(g)), "win_rate": round((g["return_pct"] > 0).mean(), 4),
            "avg_return_pct": round(g["return_pct"].mean(), 4),
            "total_pnl": round(g["pnl"].sum(), 0),
        }
        for status, g in trades_df.groupby("entry_status")
    }
    return stats
