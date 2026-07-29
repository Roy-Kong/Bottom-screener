"""count_lfs_stubs.py — 콤마구분 파일목록 중 아직 실제 sqlite 콘텐츠가 아닌
(포인터 스텁/미pull) 파일 개수를 출력한다. db.is_real_sqlite_file 재사용(pykrx
의존성 없음, 빠름) — backfill_investor_breakdown.yml이 git lfs pull 후 검증용으로
쓴다(2026-07: git lfs pull --include=이 매번 요청한 파일을 전부 못 받아오는
경우가 실제로 있어서, "받았다고 믿기"가 아니라 직접 확인 후 안 된 것만
재시도하려고 만들었다).

사용법: python count_lfs_stubs.py "data/a.db,data/b.db,..."
출력: 스텁(미확보) 개수 한 줄, 그 다음 줄에 스텁 파일들의 콤마구분 목록
      (재시도용 --include에 그대로 재사용 가능).
"""
from __future__ import annotations
import sys
import db

if __name__ == "__main__":
    paths = [p for p in sys.argv[1].split(",") if p]
    stubs = [p for p in paths if not db.is_real_sqlite_file(p)]
    print(len(stubs))
    print(",".join(stubs))
