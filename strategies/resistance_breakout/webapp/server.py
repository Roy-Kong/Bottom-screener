"""strategies/resistance_breakout/webapp/server.py — 매물대 돌파 전략 로컬 웹앱.

바닥스크리너(screener.py, docs/results.json 등)와 완전히 별도인 독립 로컬
서버다. 파이썬 표준 라이브러리 http.server만 쓴다(새 의존성 없음 —
requirements.txt에 flask/fastapi가 없어서, 이 프로젝트 전체가 지금까지
pykrx+pandas만으로 돌아가던 관례를 그대로 따름).

- GET  /              → index.html (프론트엔드)
- GET  /api/coverage   → 로컬 DB가 커버하는 날짜 범위
- POST /api/simulate   → 요청 바디의 파라미터로 훈련+검증 구간 백테스트를
                          실제로 실행하고 결과(JSON)를 반환

OHLCV 사전로딩(preload)은 날짜범위·저항선 lookback이 같으면 서버 프로세스가
살아있는 동안 메모리에 캐싱해서 재사용한다 — 매 요청마다 몇 분씩 걸리는
OHLCV 재로딩을 피하기 위함(전략 파라미터는 그 이후 스캔 단계에서만 쓰이므로
캐시 무효화 조건이 아님).

사용법:
    python strategies/resistance_breakout/webapp/server.py [--port 8765]
    브라우저에서 http://localhost:8765 열기
"""
from __future__ import annotations
import argparse
import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEBAPP_DIR.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "resistance_breakout"))
sys.stdout.reconfigure(encoding="utf-8")

import resistance_breakout as rb
import db
import index_db


def kospi_curve_for(dates: list[str], start_capital: float) -> list[dict]:
    """전략 equity_curve와 같은 날짜축으로, 코스피를 start_capital 기준
       지수화한 비교선(같은 돈으로 코스피를 그냥 샀으면 얼마가 됐을지)."""
    if not dates:
        return []
    closes = index_db.load_close_series("1001", dates[0], dates[-1])
    base = None
    out = []
    for d in dates:
        c = closes.get(d)
        if c is None:
            continue
        if base is None:
            base = c
        out.append({"date": d, "value": round(start_capital * (c / base), 0)})
    return out

_cache_lock = threading.Lock()
_preload_cache: dict = {"key": None, "pre": None}


def get_preload(start: str, end: str, params: rb.Params) -> dict:
    key = (start, end, params.resistance_lookback_days)
    with _cache_lock:
        if _preload_cache["key"] == key:
            print("  [캐시 재사용] OHLCV 프리로드 스킵")
            return _preload_cache["pre"]
    pre = rb.preload(start, end, params)
    with _cache_lock:
        _preload_cache["key"] = key
        _preload_cache["pre"] = pre
    return pre


def params_from_request(body: dict) -> rb.Params:
    """UI는 %를 사람이 읽기 쉬운 정수/소수(예: 1.5, -3)로 보내고, Params는
       내부적으로 비율(0.015, -0.03)을 쓰므로 여기서 변환한다."""
    d = rb.DEFAULT_PARAMS
    return rb.Params(
        resistance_lookback_days=int(body.get("resistance_lookback_days", d.resistance_lookback_days)),
        resistance_band=float(body.get("resistance_band_pct", d.resistance_band * 100)) / 100,
        resistance_min_touches=int(body.get("resistance_min_touches", d.resistance_min_touches)),
        breakout_margin=float(body.get("breakout_margin_pct", d.breakout_margin * 100)) / 100,
        volume_lookback_days=int(body.get("volume_lookback_days", d.volume_lookback_days)),
        volume_multiple=float(body.get("volume_multiple", d.volume_multiple)),
        start_capital=float(body.get("start_capital", d.start_capital)),
        max_position_pct=float(body.get("max_position_pct", d.max_position_pct * 100)) / 100,
        max_new_buys_per_day=int(body.get("max_new_buys_per_day", d.max_new_buys_per_day)),
        buy_fee_pct=float(body.get("buy_fee_pct", d.buy_fee_pct * 100)) / 100,
        slippage_pct=float(body.get("slippage_pct", d.slippage_pct * 100)) / 100,
        take_profit_pct=float(body.get("take_profit_pct", d.take_profit_pct * 100)) / 100,
        stop_loss_pct=float(body.get("stop_loss_pct", d.stop_loss_pct * 100)) / 100,
        max_hold_trading_days=int(body.get("max_hold_trading_days", d.max_hold_trading_days)),
    )


def run_simulation(body: dict) -> dict:
    t0 = time.time()
    start = body.get("start", "20220201").replace("-", "")
    end = body.get("end", "20231229").replace("-", "")
    params = params_from_request(body)

    print(f"[시뮬레이션 요청] {start}~{end}, {params}")
    pre = get_preload(start, end, params)
    all_days = sorted(pre["matrix"].keys())
    period_days = [d for d in all_days if start <= d <= end]
    signals = rb.scan_signals(period_days, pre, params)

    trade_log, equity_curve = rb.simulate(signals, period_days, pre, params)
    stats = rb.compute_stats(trade_log, equity_curve, params)
    kospi = kospi_curve_for([d for d, _ in equity_curve], params.start_capital)
    kospi_final = kospi[-1]["value"] if kospi else None
    kospi_return_pct = round((kospi_final / params.start_capital - 1) * 100, 2) if kospi_final else None
    stats["kospi_return_pct"] = kospi_return_pct
    stats["excess_return_pct"] = (round(stats["total_return_pct"] - kospi_return_pct, 2)
                                  if kospi_return_pct is not None else None)

    result = {
        "params_used": {**body},
        "elapsed_sec": round(time.time() - t0, 1),
        "stats": stats,
        "trade_log": trade_log,
        "equity_curve": [{"date": d, "value": v} for d, v in equity_curve],
        "kospi_curve": kospi,
    }
    print(f"[시뮬레이션 완료] {result['elapsed_sec']}초")
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("  " + (fmt % args))

    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send_file(WEBAPP_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path == "/api/coverage":
            dates = db.existing_dates()
            self._send_json({"first": dates[0] if dates else None, "last": dates[-1] if dates else None,
                             "n_days": len(dates)})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/simulate":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            result = run_simulation(body)
            self._send_json(result)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[resistance_breakout webapp] http://localhost:{args.port} 에서 실행 중… (Ctrl+C로 종료)")
    server.serve_forever()


if __name__ == "__main__":
    main()
