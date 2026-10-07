"""AI 작업 큐 — **들어온 순서대로, 한 번에 하나씩, 끝나면 지운다.**

왜 이게 있나 (2026-10-07 사고의 남은 절반)
-------------------------------------------
이 앱의 느린 AI 작업(캡션·판결·월간 회고·후기 초안·썸네일 카피·행사 채점·데이트
추천)은 전부 **데몬 스레드 + 세마포어**로 돌았다. 그 구조는 두 가지를 못 한다:

1. **재시작을 못 넘긴다.** 데몬 스레드는 프로세스가 죽으면 같이 죽는데 DB 행은
   ``pending`` 그대로 남는다 — 아무도 그 화면을 다시 열지 않으면 **영원히 '작성중'**
   이다. 지금까지는 '그 화면을 열면 되살린다'(``resume_*_if_orphaned``)로 메웠는데,
   그건 **사람이 보러 와야** 돌아가는 복구다.
2. **줄이 안 보인다.** 세마포어는 '동시에 하나'만 보장할 뿐, 누가 먼저 왔는지도
   지금 몇 개가 밀려 있는지도 아무 데도 적혀 있지 않다. 둘이 동시에 뭔가를 시키면
   순서는 OS 스케줄러가 정한다.

그래서 **일거리를 DB 행으로 적는다.** 행이 있으면 아직 안 끝난 것이고, 끝나면
**지운다**(완료 기록은 각 기능의 자기 테이블에 이미 남는다 — 여기는 '할 일' 목록이지
역사가 아니다). 프로세스가 죽어도 행은 남아 있으므로, 다음 부팅의 펌프가 **아무도
안 보고 있어도** 이어서 한다.

구조
----
::

    요청/이벤트 ──enqueue(kind, key, payload)──▶ [ai_jobs 테이블: 도착순]
                                                        │
                                       펌프 스레드 1개 ──┘  (한 번에 하나)
                                       claim → 핸들러 실행 → **DELETE**

* **펌프는 프로세스당 하나**다. 그리고 이 앱은 **gunicorn 워커 1개**가 전제다
  (Dockerfile ``--workers ${WEB_CONCURRENCY:-1}``). 그 위에서 '동시에 claude 하나'가
  구조적으로 보장된다. claim 은 그래도 원자적으로 한다(워커를 늘려도 한 행을 둘이
  집지는 않게).
* 펌프 자신은 ``_CAPTION_SEM`` 을 쥐지 않는다 — **핸들러(기존 워커)가 각자 쥔다**
  (쥐던 그대로다. 펌프가 또 쥐면 같은 스레드가 Semaphore(1) 을 두 번 잡아 영원히
  멈춘다). 큐 밖에 남아 있는 claude 경로(요청 경로의 오늘의 질문, 크론 선채점)와
  **같은 문**을 쓰는 것은 그 핸들러들이다.
* 핸들러는 **절대 raise 하지 않는다**(기존 워커들이 이미 그 규율이다). 그래도 펌프가
  한 겹 더 감싸서, 어떤 잡이 터져도 다음 잡이 돈다.

⛔ **이미지 바이트를 여기 넣지 않는다.** 큐 행에는 **참조만**(사진 id·후기 id) 담는다.
업로드 경로는 이미 스트리밍이라(werkzeug 스풀 → 3.2MiB 청크 PUT, 41.5MB 사진 실측
피크 6.5MB) '사진을 메모리에 들고 차례를 기다리는' 구간이 **없다** — 그러니 수십 MB
blob 을 DB 에 적재하는 것은 없던 비용을 새로 만드는 일이다. 측정이 바뀌면 그때
바꾼다(payload_json 은 작은 JSON 전용이고, 큰 값은 들어오지 못하게 막는다).
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

from models import AiJob, db

log = logging.getLogger(__name__)

# 핸들러 등록소 — kind -> callable(app, payload: dict). app.py 가 import 시점에
# 채운다(여기서 app 을 import 하면 순환이라 등록 방식으로 뒤집었다).
_handlers: dict = {}

# payload 는 '참조'만 담는 작은 JSON 이다. 이 상한은 규율을 **코드로** 고정한다 —
# 누가 이미지 바이트를 base64 로 끼워 넣으려 하면 여기서 막힌다.
MAX_PAYLOAD_BYTES = int(os.environ.get("AI_JOB_MAX_PAYLOAD", "4096"))

# 펌프가 큐를 확인하는 주기(빈 큐일 때). 짧게 잡아도 비용은 인덱스 조회 한 번이다.
POLL_SEC = float(os.environ.get("AI_JOB_POLL_SEC", "2"))

# 'running' 인데 이 시간 넘게 갱신이 없으면 주인이 죽은 것으로 보고 되돌린다.
# 상한은 가장 긴 작업(후기 초안: 사진 5장 최악 21분)보다 넉넉해야 한다.
STALE_RUNNING_SEC = int(os.environ.get("AI_JOB_STALE_SEC", "2700"))

# 같은 잡이 계속 터지면 큐를 막는다 — 이 횟수를 넘으면 버린다(기능은 degrade 하고,
# 사람이 화면에서 다시 요청하면 새 행으로 들어온다).
MAX_ATTEMPTS = int(os.environ.get("AI_JOB_MAX_ATTEMPTS", "3"))

_OWNER = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

_pump_lock = threading.Lock()
_pump_thread = None
_pump_stop = threading.Event()
# 테스트·진단용 카운터(처리한 잡 수). 운영 로직은 이 값을 보지 않는다.
processed = 0


def register(kind, handler):
    """``kind`` 를 처리할 함수를 등록한다 — ``handler(app, payload: dict)``."""
    _handlers[kind] = handler


def handler_for(kind):
    return _handlers.get(kind)


def _dumps(payload):
    s = json.dumps(payload or {}, ensure_ascii=False)
    if len(s.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            "ai job payload 가 너무 크다 — 큐에는 **참조만** 담는다 "
            f"({len(s)}B > {MAX_PAYLOAD_BYTES}B)"
        )
    return s


def enqueue(kind, key, payload=None, couple_id=None):
    """일거리 하나를 줄 끝에 세운다.

    ``key`` 는 '무엇에 대한 일인가'를 식별하는 문자열이다(예: ``"review:12"``,
    ``"monthly:3:2026:10"``). 같은 대상에 대한 중복 요청(더블탭·여러 화면에서 동시
    방문·재시작 후 복구)은 여기서 **한 줄로 합쳐진다.**

    반환값은 "지금 이 일거리가 줄에 있는가"다 — **새로 넣었든 이미 있었든 True**.
    호출부가 알고 싶은 건 '내가 넣었나'가 아니라 '이 일이 돌긴 하나'이기 때문이다.
    False 는 진짜 실패(모르는 kind · payload 거부 · DB 실패)뿐이고, 그때만 호출부가
    인프로세스 가드를 되돌린다. 절대 raise 하지 않는다.
    """
    if kind not in _handlers:
        log.error("ai job: 모르는 kind=%s — 큐에 넣지 않는다", kind)
        return False
    try:
        data = _dumps(payload)
    except ValueError:
        log.exception("ai job: payload 거부 (kind=%s key=%s)", kind, key)
        return False
    try:
        if AiJob.query.filter_by(job_key=key).first() is not None:
            return True  # 이미 줄에 있다 — 그 일은 곧 돈다
        db.session.add(AiJob(
            kind=kind, job_key=key, payload_json=data, couple_id=couple_id,
            status="queued",
        ))
        db.session.commit()
        return True
    except Exception:  # noqa: BLE001 — 유니크 충돌(동시 enqueue) 포함
        db.session.rollback()
        log.debug("ai job: enqueue 경합 (kind=%s key=%s)", kind, key, exc_info=True)
        # 경합이면 남이 넣어 둔 것이다 — 있으면 True.
        try:
            return AiJob.query.filter_by(job_key=key).first() is not None
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return False


def pending(key):
    """그 일거리가 아직 줄에 있나(queued 또는 running)."""
    try:
        return AiJob.query.filter_by(job_key=key).first() is not None
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return False


def depth():
    """줄 길이(진단·테스트용)."""
    try:
        return AiJob.query.count()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return 0


def claim_next():
    """가장 먼저 들어온 ``queued`` 한 건을 **원자적으로** 집는다(없으면 None).

    id 오름차순 = 도착순이다. UPDATE 에 ``status='queued'`` 를 다시 걸어, 같은 행을
    둘이 집는 일이 없게 한다(워커를 늘려도 안전).
    """
    try:
        row = (
            AiJob.query.filter_by(status="queued").order_by(AiJob.id.asc()).first()
        )
        if row is None:
            return None
        now = _utcnow()
        updated = (
            db.session.query(AiJob)
            .filter(AiJob.id == row.id, AiJob.status == "queued")
            .update(
                {"status": "running", "owner": _OWNER, "attempts": AiJob.attempts + 1,
                 "started_at": now, "updated_at": now},
                synchronize_session=False,
            )
        )
        db.session.commit()
        if not updated:
            return None  # 남이 먼저 집었다
        return db.session.get(AiJob, row.id)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("ai job: claim 실패")
        return None


def finish(job_id):
    """끝난 일거리를 **지운다**(완료 기록은 각 기능의 자기 테이블에 남는다)."""
    try:
        db.session.query(AiJob).filter(AiJob.id == job_id).delete(
            synchronize_session=False
        )
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("ai job: 삭제 실패 (id=%s)", job_id)


def recover():
    """부팅·주기 점검: 주인이 죽은 ``running`` 을 줄로 되돌린다.

    데몬 스레드가 프로세스와 함께 죽으면 행은 ``running`` 으로 남는다. 다음 부팅의
    펌프가 그걸 보고 **아무도 화면을 열지 않아도** 이어서 한다 — 이게 이 테이블이
    존재하는 첫 번째 이유다. 시도 횟수가 상한을 넘은 행은 버린다(큐를 막지 않게).
    """
    try:
        now = _utcnow()
        cutoff = now.timestamp() - STALE_RUNNING_SEC
        rows = AiJob.query.filter_by(status="running").all()
        revived = dropped = 0
        for row in rows:
            ts = (row.updated_at or row.created_at or now).timestamp()
            # 다른 프로세스가 남긴 행은 '죽은 것'으로 본다(이 앱은 워커 1개 전제).
            if row.owner == _OWNER and ts > cutoff:
                continue
            if (row.attempts or 0) >= MAX_ATTEMPTS:
                db.session.delete(row)
                dropped += 1
                continue
            row.status = "queued"
            row.owner = None
            row.updated_at = now
            revived += 1
        if revived or dropped:
            db.session.commit()
            log.info("ai job: 재시작 복구 — 되돌림 %s · 버림 %s", revived, dropped)
        else:
            db.session.rollback()
        return revived, dropped
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("ai job: 복구 실패")
        return 0, 0


def run_one(app):
    """줄에서 한 건 집어 처리한다. 처리했으면 True, 줄이 비었으면 False.

    테스트는 펌프 스레드 없이 이 함수만 불러 큐 동작을 검증한다.
    """
    global processed
    job = claim_next()
    if job is None:
        return False
    kind, job_id, key = job.kind, job.id, job.job_key
    try:
        payload = json.loads(job.payload_json or "{}")
    except (TypeError, ValueError):
        payload = {}
    handler = _handlers.get(kind)
    if handler is None:
        log.error("ai job: 핸들러 없음 kind=%s — 버린다 (key=%s)", kind, key)
        finish(job_id)
        return True
    try:
        handler(app, payload)
    except Exception:  # noqa: BLE001 — 한 잡의 실패가 큐를 멈추지 않게
        log.exception("ai job: 핸들러 실패 (kind=%s key=%s)", kind, key)
    finally:
        finish(job_id)
        processed += 1
    return True


def _pump_loop(app):
    with app.app_context():
        try:
            recover()
        except Exception:  # noqa: BLE001
            log.exception("ai job: 부팅 복구 실패")
        last_recover = time.time()
        while not _pump_stop.is_set():
            try:
                did = run_one(app)
            except Exception:  # noqa: BLE001 — 펌프는 절대 죽지 않는다
                log.exception("ai job: 펌프 루프 예외")
                did = False
            if not did:
                _pump_stop.wait(POLL_SEC)
                if time.time() - last_recover > STALE_RUNNING_SEC:
                    try:
                        recover()
                    except Exception:  # noqa: BLE001
                        log.exception("ai job: 주기 복구 실패")
                    last_recover = time.time()
        db.session.remove()


def start_pump(app):
    """펌프 스레드를 띄운다(이미 돌고 있으면 no-op). 절대 raise 하지 않는다."""
    global _pump_thread
    with _pump_lock:
        if _pump_thread is not None and _pump_thread.is_alive():
            return _pump_thread
        _pump_stop.clear()
        try:
            _pump_thread = threading.Thread(
                target=_pump_loop, args=(app,), daemon=True,
                name="ai-job-pump",
            )
            _pump_thread.start()
        except Exception:  # noqa: BLE001
            log.exception("ai job: 펌프 스폰 실패 — 큐가 쌓이기만 한다")
            _pump_thread = None
        return _pump_thread


def stop_pump(timeout=5):
    """테스트·종료용. 펌프를 세운다."""
    global _pump_thread
    _pump_stop.set()
    th = _pump_thread
    if th is not None:
        th.join(timeout=timeout)
    _pump_thread = None


def _utcnow():
    # 이 레포의 다른 테이블과 **같은 기준**을 쓴다(naive UTC) — 섞으면 비교가 깨진다.
    return datetime.utcnow()
