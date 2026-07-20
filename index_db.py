"""index_db.py — 코스피/코스닥 지수 OHLCV 저장소, data/index_history.sqlite 단일 파일.

db.py(종목 원본, 하루 1파일+LFS)와 다른 구조를 쓰는 이유: 지수는 종목(하루
~2600개)과 달리 코스피+코스닥 2개뿐이라 전체 기간(2022~)을 다 쌓아도 수천 행,
수백 KB~수 MB 수준이다. db.py가 "하루 1파일"을 쓴 이유(LFS가 델타 압축을
안 해서 한 파일에 계속 누적하면 매 커밋마다 그때까지의 전체가 통째로 다시
쌓이는 문제)가 이 정도 규모에선 사실상 무시할 수준이라, 파일 하나에 계속
append하는 게 오히려 더 단순하다. 확장자를 .sqlite로 둔 이유: 저장소
.gitattributes의 "*.db filter=lfs" 패턴에 안 걸리게 해서(LFS 필요 없음) 그냥
일반 git으로 추적되게 하기 위함(snapshot_cache.py가 .json을 쓰는 것과 같은
이유)."""
from __future__ import annotations
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "index_history.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_index (
    date TEXT NOT NULL,
    index_code TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    PRIMARY KEY (date, index_code)
);
CREATE INDEX IF NOT EXISTS idx_daily_index_code_date ON daily_index(index_code, date);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    return conn


def upsert_index(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (date, index_code, open, high, low, close) 튜플 리스트."""
    conn.executemany(
        "INSERT INTO daily_index (date, index_code, open, high, low, close) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(date, index_code) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close",
        rows)
    conn.commit()


def existing_dates(index_code: str) -> list[str]:
    """이 지수코드에 대해 이미 저장된 날짜 목록(오름차순) — 백필 재개 판단용."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT date FROM daily_index WHERE index_code=? ORDER BY date", (index_code,)
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def load_close_series(index_code: str, fromdate: str | None = None,
                       todate: str | None = None) -> dict[str, float]:
    """{date: close} — 상대강도 신호가 쓰는 형태(screener.index_close_by_date와 동일 모양)."""
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    try:
        q = "SELECT date, close FROM daily_index WHERE index_code=?"
        params: list = [index_code]
        if fromdate:
            q += " AND date>=?"
            params.append(fromdate)
        if todate:
            q += " AND date<=?"
            params.append(todate)
        rows = conn.execute(q, params).fetchall()
        return {d: c for d, c in rows if c is not None}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def load_ohlc_series(index_code: str, fromdate: str | None = None,
                      todate: str | None = None) -> dict[str, tuple]:
    """{date: (open, high, low, close)} — 향후 지수 자체의 고가/저가가 필요한
       신호(예: 지수 레벨 거래정지 감지 등)가 생기면 바로 쓸 수 있게 미리 보관."""
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    try:
        q = "SELECT date, open, high, low, close FROM daily_index WHERE index_code=?"
        params: list = [index_code]
        if fromdate:
            q += " AND date>=?"
            params.append(fromdate)
        if todate:
            q += " AND date<=?"
            params.append(todate)
        rows = conn.execute(q, params).fetchall()
        return {d: (o, h, l, c) for d, o, h, l, c in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
