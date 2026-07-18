"""
db.py — SQLite 원본 시세 데이터 저장소, 하루 1파일(data/YYYYMMDD.db) 구조.

계산된 신호 점수는 절대 저장하지 않는다 — 신호 로직(가중치·신호 종류)이 계속
바뀌므로, 점수를 저장하면 로직이 바뀔 때마다 무효가 된다. 원본만 있으면 로직이
바뀌어도 재계산만 하면 된다.

하루 1파일인 이유: Git LFS는 버전 간 델타 압축을 안 한다 — 파일 내용이 조금만
바뀌어도 전체를 새 객체로 저장한다. 모든 날짜를 한 파일에 누적하면 매일 커밋마다
그 시점까지의 전체가 통째로 다시 쌓여서(등차수열로) 무료 저장·대역폭 한도를
순식간에 넘긴다. 하루치를 각자의 파일에 담아 한 번 쓰고 다시는 건드리지 않으면,
버전 중복이 전혀 없어 전체 저장량이 원본 데이터量 그대로에 수렴한다.

각 날짜 파일 안에서도 4개 테이블은 (date, ticker) 복합 기본키(=암묵적 유니크
인덱스) + 명시적 인덱스를 그대로 둔다(한 파일에 그 날짜 데이터만 있어 사실상
ticker만으로도 충분하지만, 스키마를 통일해 두면 여러 날짜 파일을 순회하는 쪽
코드가 단순해진다)."""
from __future__ import annotations
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

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
"""


def daily_db_path(date: str) -> Path:
    return DATA_DIR / f"{date}.db"


def date_file_exists(date: str) -> bool:
    """이 날짜가 이미 수집됐는지(파일 존재 여부만 확인 — LFS 실제 내용을 안 받아온
       상태(포인터 파일)라도 git 트리에 커밋만 돼 있으면 파일 자체는 존재하므로,
       daily.yml/backfill.py의 재개 판단에는 lfs pull 없이도 충분하다)."""
    return daily_db_path(date).exists()


def get_connection(db_path: Path | str) -> sqlite3.Connection:
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


def save_single_day(date: str, day_data: dict) -> Path:
    """하루치 데이터를 그 날짜 전용 파일(data/YYYYMMDD.db)에 저장한다. 이 파일은
       이후 절대 다시 열어서 쓰지 않는다(휴장일도 빈 스키마만 있는 파일을 만들어서
       "이미 확인함" 표시로 남긴다 — 매번 재조회하지 않도록)."""
    DATA_DIR.mkdir(exist_ok=True)
    path = daily_db_path(date)
    conn = get_connection(path)
    upsert_prices(conn, day_data["daily_prices"])
    upsert_fundamental(conn, day_data["daily_fundamental"])
    upsert_short(conn, day_data["daily_short"])
    upsert_investor_flow(conn, day_data["daily_investor_flow"])
    conn.commit()
    conn.close()
    return path


def existing_dates() -> list[str]:
    """data/ 아래 존재하는 모든 날짜 파일의 날짜 목록(오름차순)."""
    if not DATA_DIR.exists():
        return []
    return sorted(p.stem for p in DATA_DIR.glob("*.db"))
