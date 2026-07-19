"""pykrx_import.py — `from pykrx import stock`를 재시도 가능하게 감싼다.

pykrx는 `import pykrx`(정확히는 pykrx.website.comm.webio) 최상단에서 즉시
KRX 로그인을 시도한다(`_session = build_krx_session()`, 모듈 레벨 부작용).
그 로그인 응답이 가끔 JSON이 아니면(KRX 쪽 일시적 이상 응답, pykrx 자체가
resp.json()에 try/except를 안 둠) requests.exceptions.JSONDecodeError로
**임포트 자체가 실패**하며 스크립트가 그 자리에서 죽는다 — 이 시점엔 아직
우리 코드가 하나도 안 돌아서, 각 스크립트가 나중에 두는 "작업 시작 전
KRX 웜업 재시도"로는 이 케이스를 못 잡는다(그건 임포트가 이미 성공한
다음에나 실행되는 코드라서). import 문 자체를 재시도해야 한다 — 실패한
모듈은 sys.modules에 안 남으므로 재시도 시 최상단 코드가 다시 실행된다.

한 프로세스에서 최초 1번만 실제로 네트워크를 타고(성공하면 sys.modules에
캐싱), 그 뒤로 이 프로젝트의 다른 모듈들(screener.py 등)이 각자
`from pykrx import stock`을 해도 캐시를 재사용할 뿐이라 안전하다 — 그래서
이 재시도 로직은 프로세스당 진입점 스크립트에서 딱 한 번만 있으면 된다.
"""
from __future__ import annotations
import time


def import_pykrx_stock(max_attempts: int = 3, retry_delay_sec: int = 5):
    for attempt in range(1, max_attempts + 1):
        try:
            from pykrx import stock
            return stock
        except Exception as e:
            print(f"[pykrx] import 시도 {attempt}/{max_attempts} 실패: {e}")
            if attempt < max_attempts:
                time.sleep(retry_delay_sec)
    raise RuntimeError("pykrx import 재시도 소진 — KRX 로그인 응답이 계속 비정상")
