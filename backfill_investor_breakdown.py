"""
backfill_investor_breakdown.py — daily_investor_flow에 개인/기관/외국인 분해값
(individual_net_buy/inst_net_buy/foreign_net_buy)을 과거 구간에 채운다.

배경: accumulation 신호는 (기관합계+외국인) 20일 순매수만 쓰는데, "개인이 던지는
걸 기관·외국인이 받는" 손바뀜 신호로 재설계할지 진단하려면 개인 순매수가
따로 필요하다(2026-07 결정). 대상 날짜들은 이미 inst_foreign_net_buy로
수집이 끝난 과거 구간이라 backfill.py --tables daily_investor_flow는 이미
"수집완료"로 보고 건너뛴다(db.table_collected 기준) — 그래서 이 스크립트가
따로 존재한다. db.upsert_investor_flow_breakdown로 기존 inst_foreign_net_buy는
절대 안 건드리고 3개 컬럼만 채운다(순수 add, 삭제 없음 — 2026-07 결정).

[필수 검증] 새로 받은 (기관합계+외국인)이 그 날짜에 이미 저장된
inst_foreign_net_buy와 일치하는지 날짜마다 확인한다 — pykrx/KRX 쪽 집계
방식이 원 수집 시점 이후 바뀌었으면 이 비교가 어긋난다. 개별 종목 1~2개의
사소한 오차는 흔할 수 있어(반올림 등) 종목별 불일치 "비율"이 임계치를
넘을 때만 그 자리에서 즉시 중단한다 — 자동으로 넘어가면 잘못된 분해값이
조용히 깔릴 위험이 있어서다.

[참고 검증] 개인+기관합계+외국인+기타법인+기타외국인의 합은 이론상 0에
수렴한다(모든 매수엔 대응하는 매도가 있어 시장 전체 순매수는 0) — 종목별로
이 항등식이 크게 깨지면 경고 로그를 남기되(중단하지 않음, 원인 규명은 이
스크립트의 범위 밖), CI 로그만으로는 실행이 끝나면 사라져 사후 추적이 안
되므로 날짜별 잔차 통계를 investor_flow_residual_summary.jsonl(레포 루트,
RESIDUAL_SUMMARY_PATH)에 한 줄씩 append한다(2026-07 결정 — 기타법인/기타외국인
원본 자체는 여전히 DB에 안 남긴다, 요약 통계만).

사용법:
    python backfill_investor_breakdown.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
        [--max-runtime-min 50] [--mismatch-ratio-stop 0.005]
    --start 생략 시 2022-01-03(DB 커버리지 시작일), --end 생략 시 오늘.
"""
from __future__ import annotations
import json
from pathlib import Path
import argparse
import sys
import sqlite3
import time
import datetime as dt

from pykrx_import import import_pykrx_stock
try:
    stock = import_pykrx_stock()
except RuntimeError as e:
    # 2026-07: KRX 로그인 응답이 일시적으로 비정상(빈 응답 등)일 때 pykrx_import
    # 자체 재시도(3회)도 소진되는 경우가 실제로 있었다 — 이건 데이터 불일치처럼
    # "사람이 봐야 하는" 상황이 아니라 순전히 일시적 인프라 문제라, exit code를
    # 대조 불일치 중단(1)과 구분되는 3으로 줘서 워크플로우가 이 경우만 재시도하게
    # 한다(backfill_investor_breakdown.yml 참고).
    print(f"[backfill_investor_breakdown] pykrx 임포트 실패(일시적 KRX 접속 문제로 추정): {e}")
    sys.exit(3)
import db
import screener as scr
from date_utils import business_days

# 기관합계+외국인 재대조 시 종목별 허용오차 — 이 범위 밖이면 "그 종목 불일치"로 센다.
MISMATCH_TOL_ABS = 1_000.0   # 원 단위, 아주 작은 값 근처 반올림 오차 흡수
MISMATCH_TOL_PCT = 0.01      # 상대오차 1%

# 항등식(5주체 합≈0) 잔차 사후검증용 요약 파일 — data/*.db(LFS)와 별개, 일반
# git 텍스트 파일이라 diff로 "어느 날 잔차가 커졌나"를 바로 볼 수 있다.
RESIDUAL_SUMMARY_PATH = Path(__file__).parent / "investor_flow_residual_summary.jsonl"


def fetch_investor_breakdown(date: str) -> dict[str, dict[str, float]]:
    """{investor: {ticker: net_buy_krw}} — 5개 투자자 타입(개인/기관합계/외국인/
       기타법인/기타외국인) × 2개 시장을 모아 합산한다. 기타법인·기타외국인은
       참고 검증(항등식)에만 쓰고 DB엔 안 남긴다."""
    investors = ("개인", "기관합계", "외국인", "기타법인", "기타외국인")
    out: dict[str, dict[str, float]] = {inv: {} for inv in investors}
    for mkt in scr.TARGET_MARKETS:
        for investor in investors:
            try:
                df = stock.get_market_net_purchases_of_equities_by_ticker(date, date, mkt, investor)
            except Exception as e:
                print(f"    [{date}] {mkt}/{investor} 조회 실패({type(e).__name__}: {e})")
                df = None
            if df is not None and not df.empty:
                col = "순매수거래대금" if "순매수거래대금" in df.columns else df.columns[-1]
                bucket = out[investor]
                for tkr, row in df.iterrows():
                    bucket[tkr] = bucket.get(tkr, 0.0) + float(row.get(col, 0) or 0)
            time.sleep(scr.REQUEST_PAUSE)
    return out


def _existing_inst_foreign(path, date: str) -> dict[str, float]:
    if not db.is_real_sqlite_file(path):
        return {}
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT ticker, inst_foreign_net_buy FROM daily_investor_flow WHERE date=?", (date,)
        ).fetchall()
    except sqlite3.DatabaseError:
        rows = []
    finally:
        conn.close()
    return {tkr: val for tkr, val in rows if val is not None}


def _already_has_breakdown(path, date: str) -> bool:
    """이 스크립트를 여러 번 나눠 돌릴 때 재개 판단용 — 이미 분해값이 채워진
       날짜(0건짜리 휴장일 포함)는 건너뛴다.

       2026-07 사고: 이 함수의 호출부(run())가 이미 db.table_collected로
       스텁/0바이트 파일을 걸러내지만, 이 함수 단독으로도 안전하도록 같은
       매직바이트 검사를 한 번 더 둔다(방어적 이중화) — 또한 원래 여기 있던
       except sqlite3.OperationalError는 "file is not a database"(포인터
       스텁) 예외의 실제 타입인 DatabaseError를 못 잡는 버그였다(OperationalError는
       DatabaseError의 하위클래스라 역방향은 안 잡힘) — DatabaseError로 수정."""
    if not db.is_real_sqlite_file(path):
        return False
    conn = sqlite3.connect(str(path))
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM daily_investor_flow WHERE date=?", (date,)).fetchone()[0]
        if total == 0:
            return True  # 그 날짜에 원래 아무 행도 없음(휴장 등) — 채울 게 없으니 완료로 간주
        filled = conn.execute(
            "SELECT COUNT(*) FROM daily_investor_flow WHERE date=? AND individual_net_buy IS NOT NULL",
            (date,)).fetchone()[0]
        return filled > 0
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def reconcile(date: str, old_inst_foreign: dict[str, float],
              inst_map: dict[str, float], foreign_map: dict[str, float],
              mismatch_ratio_stop: float) -> tuple[bool, str]:
    """새로 받은 (기관합계+외국인)과 기존 inst_foreign_net_buy를 종목별로 대조.
       반환: (계속 진행해도 되는가, 요약 메시지)."""
    if not old_inst_foreign:
        return True, "기존 inst_foreign_net_buy 없음(신규/휴장 추정) — 대조 생략"
    mismatches = []
    for tkr, old_val in old_inst_foreign.items():
        new_val = inst_map.get(tkr, 0.0) + foreign_map.get(tkr, 0.0)
        tol = max(MISMATCH_TOL_ABS, abs(old_val) * MISMATCH_TOL_PCT)
        if abs(new_val - old_val) > tol:
            mismatches.append((tkr, old_val, new_val))
    ratio = len(mismatches) / len(old_inst_foreign)
    msg = f"대조 {len(old_inst_foreign)}종목 중 불일치 {len(mismatches)}종목({ratio*100:.2f}%)"
    if mismatches[:5]:
        msg += " · 예시: " + ", ".join(f"{t}(구{o:.0f}/신{n:.0f})" for t, o, n in mismatches[:5])
    if ratio > mismatch_ratio_stop:
        return False, msg + f" — 임계치({mismatch_ratio_stop*100:.2f}%) 초과, 중단"
    return True, msg


def identity_check(date: str, breakdown: dict[str, dict[str, float]]) -> dict:
    """개인+기관합계+외국인+기타법인+기타외국인 합이 종목별로 0에 가까운지
       참고용으로만 확인(중단하지 않음, 경고 로그만) — 날짜별 잔차 통계를
       반환해서 run()이 RESIDUAL_SUMMARY_PATH에 사후검증용으로 남기게 한다."""
    all_tkrs = set()
    for m in breakdown.values():
        all_tkrs |= set(m)
    residuals: list[tuple[str, float]] = []
    for tkr in all_tkrs:
        total = sum(breakdown[inv].get(tkr, 0.0) for inv in breakdown)
        residuals.append((tkr, total))
    bad = []
    for tkr, total in residuals:
        scale = max(abs(breakdown[inv].get(tkr, 0.0)) for inv in breakdown) or 1.0
        if abs(total) > max(MISMATCH_TOL_ABS * 10, scale * 0.02):
            bad.append((tkr, total))
    if bad:
        print(f"    [참고검증] {date}: 항등식(5주체 합≈0)에서 벗어난 종목 {len(bad)}/{len(all_tkrs)}개"
              f"(경고만, 중단 안 함)")
    top_residuals = sorted(residuals, key=lambda x: -abs(x[1]))[:10]
    return {
        "date": date,
        "n_tickers": len(all_tkrs),
        "n_flagged": len(bad),
        "flagged_ratio": (len(bad) / len(all_tkrs)) if all_tkrs else 0.0,
        "total_abs_residual": sum(abs(r) for _, r in residuals),
        "top_residuals": [[t, r] for t, r in top_residuals],
    }


def append_residual_summary(record: dict) -> None:
    with open(RESIDUAL_SUMMARY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(start_str: str, end_str: str, max_runtime_min: int, mismatch_ratio_stop: float) -> None:
    start = dt.datetime.strptime(start_str, "%Y-%m-%d").date()
    end = dt.datetime.strptime(end_str, "%Y-%m-%d").date()

    todo = []
    for d in business_days(start, end):
        ds = d.strftime("%Y%m%d")
        if not db.date_file_exists(ds):
            continue  # daily.yml/기존 백필이 아직 안 만든 날짜 — 이 스크립트 대상 아님
        if not db.table_collected(ds, "daily_investor_flow"):
            continue  # 애초에 기관+외국인 원본조차 없는 날짜(아직 미수집) — 대상 아님
        path = db.daily_db_path(ds)
        if _already_has_breakdown(path, ds):
            continue
        todo.append(ds)

    print(f"[개인순매수 재백필] 기간 {start_str}~{end_str}: 대상 {len(todo)}일")
    if not todo:
        print("[개인순매수 재백필] 이미 전부 채워짐. 완료.")
        return

    t0 = time.time()
    deadline = t0 + max_runtime_min * 60
    done = 0
    for ds in todo:
        if time.time() > deadline:
            print(f"[개인순매수 재백필] 시간 제한({max_runtime_min}분) 도달 — 중단. "
                  f"워크플로우를 다시 실행하면 여기부터 이어집니다.")
            break
        path = db.daily_db_path(ds)
        old_inst_foreign = _existing_inst_foreign(path, ds)
        breakdown = fetch_investor_breakdown(ds)
        inst_map, foreign_map, individual_map = breakdown["기관합계"], breakdown["외국인"], breakdown["개인"]

        ok, msg = reconcile(ds, old_inst_foreign, inst_map, foreign_map, mismatch_ratio_stop)
        print(f"  {ds}: {msg}")
        if not ok:
            print(f"[개인순매수 재백필] {ds}에서 중단 — pykrx 집계 방식이 원 수집 시점과 달라졌을 "
                  f"가능성. 사람 확인 필요, 재백필은 여기서 멈춥니다.")
            sys.exit(1)

        append_residual_summary(identity_check(ds, breakdown))

        all_tkrs = set(inst_map) | set(foreign_map) | set(individual_map)
        rows = [
            (ds, tkr, individual_map.get(tkr, 0.0), inst_map.get(tkr, 0.0), foreign_map.get(tkr, 0.0))
            for tkr in all_tkrs
        ]
        conn = db.get_connection(path)
        db.upsert_investor_flow_breakdown(conn, rows)
        conn.commit()
        conn.close()
        done += 1
        if done % 20 == 0:
            elapsed = (time.time() - t0) / 60
            print(f"  진행: {done}/{len(todo)}일 처리 ({ds}까지), 경과 {elapsed:.1f}분")

    remaining = len(todo) - done
    print(f"\n[개인순매수 재백필] 이번 실행 요약: {done}일 처리, 남은 미처리 {max(remaining, 0)}일")
    if remaining > 0:
        print("[개인순매수 재백필] 아직 안 끝났습니다 — 워크플로우를 다시 실행해 이어가세요.")
    else:
        print(f"[개인순매수 재백필] {start_str}~{end_str} 구간 완료.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="daily_investor_flow 개인/기관/외국인 분해값 과거 재백필")
    p.add_argument("--start", default="2022-01-03", help="시작일 YYYY-MM-DD (기본: DB 커버리지 시작일)")
    p.add_argument("--end", default=dt.date.today().strftime("%Y-%m-%d"), help="종료일 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--max-runtime-min", type=int, default=50, help="이번 실행 최대 시간(분)")
    p.add_argument("--mismatch-ratio-stop", type=float, default=0.005,
                    help="이 비율 넘게 종목이 불일치하면 즉시 중단(기본 0.5%%)")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    run(args.start, args.end, args.max_runtime_min, args.mismatch_ratio_stop)
