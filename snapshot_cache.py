"""snapshot_cache.py — 앵커(스크리닝 기준일)별 "가중치를 바꿔도 안 변하는" 값 캐시.

db.py의 4개 테이블(daily_prices 등, 하루 1파일)과는 목적이 다르다 — 그건 종목별
원본 신호 입력값이고, 여기는 db.py가 의도적으로 안 담기로 한(db_reader.py 상단
주석 참고) 5가지: 종목 유니버스, 업종 매핑, 코스피/코스닥 지수, 업종지수,
매수 후 30일 시세. db_reader.py의 "캐싱 이득이 적다"는 판단은 KRX 로그인이
되는 GitHub Actions를 전제로 한 것이라 로컬(로그인 없이 라이브 호출이 거의
다 실패)에는 해당하지 않는다 — 그래서 별도로 캐싱한다.

앵커 데이터는 과거 고정값(그 날짜의 코스피 종가는 영원히 안 바뀜)이라
db.py와 같은 "한 번 쓰고 다시 안 건드림" 원칙을 그대로 따른다 — 앵커당 1파일,
매수후추적은 (종목,매수일)당 1파일. 전체 예상 용량이 수십 MB 수준(2022~2025
전체 기준 약 14MB 추정)이라 db.py와 달리 LFS가 필요 없다 — 그냥 git으로 추적.

디렉터리 구조 (data/의 원본 4테이블과 헷갈리지 않도록 명확히 분리):
    cache/anchor_snapshots/{anchor_date}.json   — universe/sector/index (앵커당 1개)
    cache/post_buy_tracking/{buy_date}_{ticker}.json — 매수후 30일 OHLCV (종목당 1개)
"""
from __future__ import annotations
import json
from pathlib import Path

ANCHOR_DIR = Path(__file__).parent / "cache" / "anchor_snapshots"
TRACKING_DIR = Path(__file__).parent / "cache" / "post_buy_tracking"


def anchor_snapshot_path(anchor_date: str) -> Path:
    return ANCHOR_DIR / f"{anchor_date}.json"


def save_anchor_snapshot(anchor_date: str, universe: dict, ticker_market: dict,
                          sector_map: dict, sector_names: dict,
                          market_idx_by_date: dict, sector_idx_by_date: dict) -> None:
    ANCHOR_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "anchor_date": anchor_date,
        "universe": universe,
        "ticker_market": ticker_market,
        "sector_map": sector_map,
        "sector_names": sector_names,
        "market_idx_by_date": market_idx_by_date,
        # 업종코드는 dict 키라 JSON은 문자열로 바꾸는데, 원래도 pykrx 코드가
        # 문자열이라 왕복에 손실 없음.
        "sector_idx_by_date": sector_idx_by_date,
    }
    path = anchor_snapshot_path(anchor_date)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_anchor_snapshot(anchor_date: str) -> dict | None:
    path = anchor_snapshot_path(anchor_date)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def tracking_path(buy_date: str, ticker: str) -> Path:
    return TRACKING_DIR / f"{buy_date}_{ticker}.json"


def save_tracking(buy_date: str, ticker: str, records: list[dict]) -> None:
    TRACKING_DIR.mkdir(parents=True, exist_ok=True)
    tracking_path(buy_date, ticker).write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")


def load_tracking(buy_date: str, ticker: str) -> list[dict] | None:
    path = tracking_path(buy_date, ticker)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
