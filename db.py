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
    open REAL,
    high REAL,
    low REAL,
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

CREATE TABLE IF NOT EXISTS collected_marker (
    date TEXT NOT NULL,
    table_name TEXT NOT NULL,
    PRIMARY KEY (date, table_name)
);
"""

# 테이블 선택형 백필(backfill.py --tables)이 다루는 4개 표준 원본 테이블.
ALL_TABLES = ["daily_prices", "daily_investor_flow", "daily_short", "daily_fundamental"]


def daily_db_path(date: str) -> Path:
    return DATA_DIR / f"{date}.db"


def date_file_exists(date: str) -> bool:
    """이 날짜가 이미 수집됐는지(파일 존재 여부만 확인 — LFS 실제 내용을 안 받아온
       상태(포인터 파일)라도 git 트리에 커밋만 돼 있으면 파일 자체는 존재하므로,
       daily.yml/backfill.py의 재개 판단에는 lfs pull 없이도 충분하다)."""
    return daily_db_path(date).exists()


def table_collected(date: str, table: str) -> bool:
    """이 (date, table) 조합이 이미 수집 시도됐는지(휴장일이라 0건이어도 '시도함'
       으로 간주 — 재시도 방지). collected_marker가 있는 파일(테이블 선택형
       백필로 만들어진 파일)은 그 마커로 정확히 판단한다. collected_marker가
       이 날짜에 대해 전혀 없는 파일(2022~ 전체 4테이블 백필처럼, 이 기능이
       생기기 전에 항상 4개를 한 번에 통째로 수집하던 구버전 결과물)은 존재
       자체를 '표준 4개 테이블 전부 수집 완료'로 간주한다 — 안 그러면 기존
       완료 구간을 이 프레임으로 다시 돌릴 때 전부 재수집하게 된다."""
    path = daily_db_path(date)
    if not path.exists():
        return False
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS collected_marker "
            "(date TEXT NOT NULL, table_name TEXT NOT NULL, PRIMARY KEY (date, table_name))")
        marker_count = conn.execute(
            "SELECT COUNT(*) FROM collected_marker WHERE date=?", (date,)).fetchone()[0]
        if marker_count == 0:
            return table in ALL_TABLES
        row = conn.execute(
            "SELECT 1 FROM collected_marker WHERE date=? AND table_name=? LIMIT 1",
            (date, table)).fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        # 커밋은 됐지만 아직 실제 LFS 내용을 받아오지 않은 포인터 스텁 상태(git lfs
        # pull 없이 checkout만 한 경우). 내용을 열어볼 수 없으니 "미수집"으로 보고
        # 다시 수집한다 — INSERT OR REPLACE라 데이터가 덮어써질 뿐 손상되진 않는다.
        # backfill.yml은 Run backfill 전에 대상 구간 파일을 미리 git lfs pull 하므로
        # 정상 실행에서는 이 경로를 안 타는 게 정상이다.
        return False
    finally:
        conn.close()


def _ensure_ohlc_columns(conn: sqlite3.Connection) -> None:
    """2026-07 이전에 만들어진 파일은 daily_prices에 open/high/low 컬럼이 없다
       (close/volume만 저장하던 구버전 스키마) — CREATE TABLE IF NOT EXISTS는
       기존 테이블에 컬럼을 추가해주지 않으므로, 파일을 열 때마다 컬럼 존재를
       확인해서 없으면 ALTER TABLE로 채워 넣는다(기존 close/volume/market_cap
       값은 그대로 유지, 새 컬럼만 NULL로 추가됨)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_prices)").fetchall()}
    for col in ("open", "high", "low"):
        if col not in cols:
            conn.execute(f"ALTER TABLE daily_prices ADD COLUMN {col} REAL")


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    _ensure_ohlc_columns(conn)
    return conn


def upsert_prices(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (date,ticker,open,high,low,close,volume,market_cap) 8-tuple.
       새로 수집하는 날(백필의 신규 구간, daily.yml)에 쓴다 — 그 날짜의 행을
       통째로 새로 쓰는 것이므로 OHLCV 6개 값을 한 번에 다 받는다."""
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_prices "
            "(date,ticker,open,high,low,close,volume,market_cap) VALUES (?,?,?,?,?,?,?,?)",
            rows)


def upsert_ohlc_only(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (open,high,low,date,ticker) 5-tuple(UPDATE 파라미터 순서). 이미
       close/volume/market_cap이 있는 기존 행에 open/high/low만 채워 넣는다
       (backfill_ohlc.py 전용) — 기존 값은 절대 안 건드리고 이 3개 컬럼만 갱신."""
    if rows:
        conn.executemany(
            "UPDATE daily_prices SET open=?, high=?, low=? WHERE date=? AND ticker=?",
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


TABLE_UPSERT = {
    "daily_prices": upsert_prices,
    "daily_investor_flow": upsert_investor_flow,
    "daily_short": upsert_short,
    "daily_fundamental": upsert_fundamental,
}


def save_single_day(date: str, day_data: dict, tables: list[str] | None = None) -> Path:
    """하루치 데이터를 그 날짜 전용 파일(data/YYYYMMDD.db)에 저장한다. 이 파일은
       이후 절대 다시 열어서 쓰지 않는다(휴장일도 빈 스키마만 있는 파일을 만들어서
       "이미 확인함" 표시로 남긴다 — 매번 재조회하지 않도록). tables를 생략하면
       day_data에 들어있는 테이블만 저장한다(기본: 전체 4개, update_db_daily.py처럼
       항상 4개를 같이 수집하는 호출부는 그대로 동작). 저장한 각 테이블마다
       collected_marker에 '수집 시도함' 표시를 남겨 이후 --tables 백필의 재개
       판단에 쓴다."""
    if tables is None:
        tables = list(day_data.keys())
    DATA_DIR.mkdir(exist_ok=True)
    path = daily_db_path(date)
    conn = get_connection(path)
    for table in tables:
        TABLE_UPSERT[table](conn, day_data.get(table, []))
    conn.executemany(
        "INSERT OR REPLACE INTO collected_marker (date, table_name) VALUES (?,?)",
        [(date, t) for t in tables])
    conn.commit()
    conn.close()
    return path


def existing_dates() -> list[str]:
    """data/ 아래 존재하는 모든 날짜 파일의 날짜 목록(오름차순)."""
    if not DATA_DIR.exists():
        return []
    return sorted(p.stem for p in DATA_DIR.glob("*.db"))
