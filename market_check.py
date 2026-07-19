"""market_check.py — 애드혹 데이터 점검용 고정 스크립트 (백테스트 결과 이상치 조사 등).
매번 python -c로 즉석 코드를 짜는 대신, 여기 정의된 좁은 범위의 조회만 수행한다
(임의 코드실행이 아니라 read-only 조회 몇 가지로 제한).

사용법:
    python market_check.py ohlcv TICKER FROM TO
    python market_check.py index CODE FROM TO
    python market_check.py dbcount DATE TABLE
"""
from __future__ import annotations
import sys
import sqlite3
from pathlib import Path


def cmd_ohlcv(ticker: str, fromdate: str, todate: str) -> None:
    from pykrx import stock
    df = stock.get_market_ohlcv_by_date(fromdate, todate, ticker)
    print(df.to_string())


def cmd_index(code: str, fromdate: str, todate: str) -> None:
    from pykrx import stock
    df = stock.get_index_ohlcv(fromdate, todate, code)
    print(df.to_string())


def cmd_dbcount(date: str, table: str) -> None:
    path = Path("data") / f"{date}.db"
    if not path.exists():
        print(f"NO_FILE: {path}")
        return
    conn = sqlite3.connect(str(path))
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{date} {table}: {n} rows")
    except sqlite3.OperationalError as e:
        print(f"ERROR: {e}")
    finally:
        conn.close()


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "ohlcv" and len(rest) == 3:
        cmd_ohlcv(*rest)
    elif cmd == "index" and len(rest) == 3:
        cmd_index(*rest)
    elif cmd == "dbcount" and len(rest) == 2:
        cmd_dbcount(*rest)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
