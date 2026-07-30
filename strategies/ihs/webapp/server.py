"""strategies/ihs/webapp/server.py — 역헤드앤숄더 전용 독립 로컬 웹앱.

바닥스크리너(screener.py/results.json/docs/) 및 그 어떤 기존 UI와도 완전히
분리된 서버다(사용자 지시: "바닥시그널과 섞지말고 역헤드앤숄더만 가지고
스크리닝"). 표준 라이브러리 http.server만 쓴다(requirements.txt에 Flask/FastAPI가
없다는 기존 프로젝트 제약 그대로 따름 — resistance_breakout 웹앱과 동일한
설계 원칙, 지금은 삭제됐지만 이 서버 구조는 그때와 같은 패턴이다).

scan_universe()/simulate() 둘 다 수십 초~수 분 걸릴 수 있어(전종목 스캔 시
detect_ihs 1콜당 ~20ms), 동기 HTTP 요청 하나로 처리하면 브라우저가 타임아웃
날 수 있다 — 그래서 "작업 시작(POST) → 백그라운드 스레드 실행 → 폴링(GET)"
패턴을 쓴다.

사용법:
    python strategies/ihs/webapp/server.py [--port 8792]
"""
from __future__ import annotations
import argparse
import json
import sys
import threading
import traceback
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
IHS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(IHS_DIR))
sys.stdout.reconfigure(encoding="utf-8")

from ihs_screener import IHSConfig, scan_universe
from simulate import SimParams, simulate

INDEX_HTML = (Path(__file__).resolve().parent / "index.html").read_text(encoding="utf-8")

# ---------------- 작업(Job) 저장소 ----------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _run_job(job_id: str, fn, *args, **kwargs) -> None:
    try:
        result = fn(*args, **kwargs)
        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "result": result}
    except Exception as e:
        traceback.print_exc()
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "error": str(e)}


def _start_job(fn, *args, **kwargs) -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "running"}
    t = threading.Thread(target=_run_job, args=(job_id, fn) + args, kwargs=kwargs, daemon=True)
    t.start()
    return job_id


# ---------------- 작업 함수 ----------------

def _scan_job(as_of: str | None, min_score: float, statuses: tuple[str, ...]) -> dict:
    df = scan_universe(as_of=as_of, cfg=IHSConfig(), statuses=statuses, min_score=min_score)
    return {"patterns": [] if df.empty else df.to_dict(orient="records"), "n": len(df)}


def _simulate_job(params: SimParams) -> dict:
    res = simulate(params)
    trades = res["trades"]
    open_pos = res["open_positions"]
    equity = res["equity"]
    return {
        "stats": _clean_json(res["stats"]),
        "trades": [] if trades.empty else trades.to_dict(orient="records"),
        "open_positions": [] if open_pos.empty else open_pos.to_dict(orient="records"),
        "equity": [] if equity.empty else equity.to_dict(orient="records"),
    }


def _clean_json(obj):
    """numpy 스칼라(np.float64 등)를 순수 파이썬으로 재귀 변환 — json.dumps가
       numpy 타입을 직렬화 못 해서 필요."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_json(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if v != v else v  # NaN -> null
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, float) and obj != obj:
        return None
    return obj


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[webapp] {self.address_string()} {fmt % args}", file=sys.stderr)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(_clean_json(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/job":
            job_id = q.get("id", [None])[0]
            with _jobs_lock:
                job = _jobs.get(job_id)
            if job is None:
                self._send_json({"status": "not_found"}, status=404)
            else:
                self._send_json(job)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/scan/start":
            as_of = body.get("as_of") or None
            min_score = float(body.get("min_score", 50.0))
            statuses = tuple(body.get("statuses") or ["forming", "retest", "breakout"])
            job_id = _start_job(_scan_job, as_of, min_score, statuses)
            self._send_json({"job_id": job_id})
            return

        if path == "/api/simulate/start":
            cfg = IHSConfig()
            for k in ("order", "min_pattern_days", "max_pattern_days", "breakout_max_age"):
                if k in body:
                    setattr(cfg, k, int(body[k]))
            for k in ("head_prominence", "shoulder_sym_tol", "neckline_slope_tol", "min_depth",
                      "max_depth", "prior_decline", "retest_tol", "breakout_buffer"):
                if k in body:
                    setattr(cfg, k, float(body[k]))
            params = SimParams(
                start=body["start"], end=body["end"], cfg=cfg,
                statuses=tuple(body.get("statuses") or ["breakout"]),
                min_score=float(body.get("min_score", 50.0)),
                max_slots=int(body.get("max_slots", 10)),
                max_hold_trading_days=int(body.get("max_hold_trading_days", 60)),
                top_n_by_liquidity=(int(body["top_n_by_liquidity"]) if body.get("top_n_by_liquidity") else None),
                apply_liquidity_filter=bool(body.get("apply_liquidity_filter", True)),
            )
            job_id = _start_job(_simulate_job, params)
            self._send_json({"job_id": job_id})
            return

        self.send_response(404)
        self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8792)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"IHS 웹앱: http://127.0.0.1:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
