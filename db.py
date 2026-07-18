"""
db.py — SQLite 원본 시세 데이터 저장소.

계산된 신호 점수는 절대 저장하지 않는다 — 신호 로직(가중치·신호 종류)이 계속
바뀌므로, 점수를 저장하면 로직이 바뀔 때마다 무효가 된다. 원본만 있으면 로직이
바뀌어도 재계산만 하면 된다.

4개 테이블 모두 (date, ticker) 복합 기본키(=암묵적 유니크 인덱스) + 명시적
인덱스를 둔다. collection_log는 신호 데이터가 아니라 "이 날짜를 이미 시도했는지"
추적용 운영 메타데이터(백필 재개 판단용)라 위 원칙과 무관하다.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "market_data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_prices (
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close REAL,
    volume REAL,
    market_cap REAL,
    PRIMARY KEY (date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_daily_prices_date_ticker ON daily_prices(date, ticker);

CREATE TABLE IF NOT EXISTS daily_investor_flow (
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    inst_foreign_net_buy REAL,
    PRIMARY KEY (date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_daily_investor_flow_date_ticker ON daily_investor_flow(date, ticker);

CREATE TABLE IF NOT EXISTS daily_short (
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    short_ratio REAL,
    PRIMARY KEY (date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_daily_short_date_ticker ON daily_short(date, ticker);

CREATE TABLE IF NOT EXISTS daily_fundamental (
    date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    pbr REAL,
    div REAL,
    dps REAL,
    eps REAL,
    bps REAL,
    PRIMARY KEY (date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_daily_fundamental_date_ticker ON daily_fundamental(date, ticker);

CREATE TABLE IF NOT EXISTS collection_log (
    date TEXT PRIMARY KEY,
    prices_count INTEGER
);
"""


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    return conn


def upsert_prices(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_prices (date,ticker,close,volume,market_cap) VALUES (?,?,?,?,?)",
            rows)


def upsert_investor_flow(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_investor_flow (date,ticker,inst_foreign_net_buy) VALUES (?,?,?)",
            rows)


def upsert_short(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_short (date,ticker,short_ratio) VALUES (?,?,?)",
            rows)


def upsert_fundamental(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_fundamental (date,ticker,pbr,div,dps,eps,bps) VALUES (?,?,?,?,?,?,?)",
            rows)


def mark_collected(conn: sqlite3.Connection, date: str, count: int) -> None:
    conn.execute("INSERT OR REPLACE INTO collection_log (date, prices_count) VALUES (?, ?)", (date, count))


def date_already_collected(conn: sqlite3.Connection, date: str) -> bool:
    cur = conn.execute("SELECT 1 FROM collection_log WHERE date = ? LIMIT 1", (date,))
    return cur.fetchone() is not None


def save_day(conn: sqlite3.Connection, date: str, day_data: dict) -> None:
    """market_data_collector.collect_day()의 반환값을 4개 테이블에 upsert하고
       collection_log에 기록한다(휴장일처럼 빈 날도 기록 — 재시도 낭비 방지)."""
    upsert_prices(conn, day_data["daily_prices"])
    upsert_fundamental(conn, day_data["daily_fundamental"])
    upsert_short(conn, day_data["daily_short"])
    upsert_investor_flow(conn, day_data["daily_investor_flow"])
    mark_collected(conn, date, len(day_data["daily_prices"]))
    conn.commit()


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ["daily_prices", "daily_investor_flow", "daily_short", "daily_fundamental", "collection_log"]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
