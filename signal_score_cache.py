"""signal_score_cache.py — 하루×종목 단위 "가중치 적용 전" 신호 raw 점수 캐시.

portfolio_simulation.py의 score_day()가 매일 전종목(~2500개)을 스캔하며 DB
읽기+지표 계산을 하는 게 제일 비싼 부분이다(6개월 전체 실행에 37분). 그런데
이 계산 결과 중 실제로 가중치·매매조건에 따라 달라지는 부분은 마지막
"raw 점수들을 어떻게 조합해서 컷라인을 넘기는지"뿐이다 — OHLCV/펀더멘털/
공매도/매집 데이터를 읽어서 신호별 0~100점(또는 None)을 내는 계산 자체는
signals.BOTTOM_WEIGHTS 값이 몇이든, SCORE_THRESHOLD가 65점이든 70점이든
바뀌지 않는다.

그래서 신호별 raw 점수(가중치 적용 전)를 종목별로 여기 캐싱해두면, 나중에
가중치나 매매조건(±%, 슬롯 수, 쿨다운, 컷라인)만 바꿔서 재실험할 때는 이미
계산된 raw 점수를 다른 가중치로 재조합만 하면 돼서(순수 파이썬, DB 접근 0회)
몇 분이 아니라 몇 초 만에 끝난다.

db.py(종목 원본, 하루 1파일+LFS)와 같은 이유로 하루 1파일 + LFS를 쓴다 —
하루치가 전종목(~2500개)×12개 신호값이라 데이터량이 db.py 수준으로 크고
(수백KB/일 × 향후 여러 해로 확장되면 수백MB), 한 번 계산되면 그 날짜의 raw
점수는 영원히 안 바뀌므로(과거 시세가 안 바뀌듯) "한 번 쓰고 다시 안 건드림"
원칙이 그대로 적용된다."""
from __future__ import annotations
import json
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache" / "daily_signal_scores"

# v2(2026-07): score_accumulation/score_volume_dryness가 절대문턱(_lin) 방식에서
# 자기 히스토리 percentile 방식으로 바뀌어, 그 이전에 저장된 raw 점수(버전 표시가
# 아예 없는 v1, bare list)는 stale하다 — "한 번 계산되면 안 바뀐다"는 이 캐시의
# 원래 전제가 신호 공식 자체가 바뀌는 경우엔 성립하지 않으므로, 스키마 버전을 찍어
# v1 파일은 조용히 무시(캐시미스로 처리해 재계산)한다. v1 파일 자체는 지우지 않음
# (필요하면 나중에 별도로 정리 — 지금은 정합성만 확보).
CACHE_SCHEMA_VERSION = 2


def _path(date: str) -> Path:
    return CACHE_DIR / f"{date}.json"


def save_day_scores(date: str, entries: list[dict]) -> None:
    """entries: [{"ticker","name","bottom_scores":{...},"turnaround_scores":{...},
       "pbr_caution_sector":bool}, ...] — 그날 기본 게이트(생존게이트·거래대금·
       시총·PBR>0)를 통과한 전 종목(최종 65점 미달이어도 포함)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": CACHE_SCHEMA_VERSION, "entries": entries}
    _path(date).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_day_scores(date: str) -> list[dict] | None:
    path = _path(date)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list) or raw.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None  # 구버전(v1) 또는 스키마 불일치 — stale, 캐시미스로 재계산
    return raw["entries"]
