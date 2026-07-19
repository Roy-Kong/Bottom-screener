"""analyze_backtest.py — 여러 backtest_*.csv를 합쳐서 월별/전체 요약(5%/7%/10%
도달률, 평균 최대낙폭)을 계산한다. PowerShell Where-Object의 단일값 .Count 함정을
피하려고 Python csv/집계로 고정.

사용법:
    python analyze_backtest.py --out backtests/backtest_2023.csv \\
        backtests/backtest_2023_h1.csv backtests/backtest_2023_h2.csv
"""
from __future__ import annotations
import argparse
import csv
import sys
from pathlib import Path


def load_rows(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        with open(p, newline="", encoding="utf-8-sig") as f:
            rows.extend(csv.DictReader(f))
    return rows


def write_merged(rows: list[dict], out_path: str) -> None:
    if not rows:
        return
    all_keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                all_keys.append(k)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=all_keys)
        w.writeheader()
        w.writerows(rows)


def stats_for(rows: list[dict]) -> dict:
    n = len(rows)
    r5 = sum(1 for r in rows if r.get("reached_5pct") == "True")
    r7 = sum(1 for r in rows if r.get("reached_7pct") == "True")
    r10 = sum(1 for r in rows if r.get("reached_10pct") == "True")
    dds = [float(r["max_drawdown_pct"]) for r in rows if r.get("max_drawdown_pct")]
    avg_dd = sum(dds) / len(dds) if dds else None
    return {
        "n": n,
        "r5": r5, "p5": (100.0 * r5 / n) if n else 0.0,
        "r7": r7, "p7": (100.0 * r7 / n) if n else 0.0,
        "r10": r10, "p10": (100.0 * r10 / n) if n else 0.0,
        "avg_dd": avg_dd,
    }


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="입력 backtest_*.csv 파일들")
    ap.add_argument("--out", required=True, help="합쳐서 저장할 CSV 경로")
    args = ap.parse_args(argv)

    rows = load_rows(args.inputs)
    write_merged(rows, args.out)

    bought = [r for r in rows if r.get("excluded") == "False"]
    excluded = [r for r in rows if r.get("excluded") == "True"]

    print(f"입력 파일: {args.inputs}")
    print(f"합쳐서 저장: {args.out} (총 {len(rows)}행)")
    print(f"매수 성립: {len(bought)}건, 매수 제외: {len(excluded)}건")
    if excluded:
        print("  제외 사유 내역:")
        for r in excluded:
            print(f"    {r['screening_month']} {r['ticker']} {r['name']}: {r['exclude_reason']}")
    print()

    by_month: dict[str, list[dict]] = {}
    for r in bought:
        by_month.setdefault(r["screening_month"], []).append(r)

    print(f"{'월':8} {'n':>3}  {'5%달성':>14}  {'7%달성':>14}  {'10%달성':>14}  {'평균최대낙폭':>10}")
    for m in sorted(by_month):
        s = stats_for(by_month[m])
        dd_str = f"{s['avg_dd']:.2f}%" if s["avg_dd"] is not None else "n/a"
        print(f"{m:8} {s['n']:>3}  {s['r5']:>2}/{s['n']:<3}({s['p5']:5.1f}%)  "
              f"{s['r7']:>2}/{s['n']:<3}({s['p7']:5.1f}%)  "
              f"{s['r10']:>2}/{s['n']:<3}({s['p10']:5.1f}%)  {dd_str:>10}")

    print()
    s_all = stats_for(bought)
    dd_str = f"{s_all['avg_dd']:.2f}%" if s_all["avg_dd"] is not None else "n/a"
    print(f"{'전체':8} {s_all['n']:>3}  {s_all['r5']:>2}/{s_all['n']:<3}({s_all['p5']:5.1f}%)  "
          f"{s_all['r7']:>2}/{s_all['n']:<3}({s_all['p7']:5.1f}%)  "
          f"{s_all['r10']:>2}/{s_all['n']:<3}({s_all['p10']:5.1f}%)  {dd_str:>10}")


if __name__ == "__main__":
    main(sys.argv[1:])
