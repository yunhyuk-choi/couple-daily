"""AI 작업 큐 계약 — **들어온 순서대로, 한 번에 하나씩, 끝나면 지운다.**

왜 이 테이블이 있는지(2026-10-07 '작성중' 영구 고착)는 ``aijobs.py`` 머리말이 정본.
여기서 못 박는 계약:

  1. 일거리는 **DB 행**이다 — 프로세스가 죽어도 남고, 다음 부팅이 이어서 한다.
  2. 같은 대상에 대한 중복 요청은 **한 줄로 합쳐진다**(더블탭·여러 화면 동시 방문).
  3. 처리 순서는 **도착순**이다.
  4. 끝난 일거리는 **지운다** — 이 테이블은 '할 일'이지 역사가 아니다.
  5. 한 잡이 터져도 큐는 멈추지 않는다.
  6. 큐에는 **참조만** 담는다 — 이미지 바이트를 넣으려 하면 거부된다.
"""
import json

import pytest

import aijobs
import app as app_module
from models import AiJob, db


@pytest.fixture(autouse=True)
def _clean_queue(flask_app):
    AiJob.query.delete()
    db.session.commit()
    aijobs.processed = 0
    yield
    AiJob.query.delete()
    db.session.commit()


@pytest.fixture()
def spy(monkeypatch):
    """핸들러 3종을 기록만 하는 가짜로 바꾼다(claude 는 절대 안 돈다)."""
    seen = []
    for kind in ("review", "caption", "thumbnail"):
        monkeypatch.setitem(
            aijobs._handlers, kind,
            (lambda k: (lambda a, p: seen.append((k, p))))(kind),
        )
    return seen


def test_enqueue_writes_a_row_that_outlives_the_process(spy, flask_app):
    assert aijobs.enqueue("review", "review:7", {"review_id": 7}) is True
    row = AiJob.query.filter_by(job_key="review:7").one()
    assert row.status == "queued" and row.kind == "review"
    assert json.loads(row.payload_json) == {"review_id": 7}


def test_the_same_target_never_queues_twice(spy, flask_app):
    aijobs.enqueue("review", "review:7", {"review_id": 7})
    aijobs.enqueue("review", "review:7", {"review_id": 7})
    aijobs.enqueue("review", "review:7", {"review_id": 7})
    assert aijobs.depth() == 1, "같은 대상이 줄에 여러 번 섰다"


def test_jobs_run_in_arrival_order(spy, flask_app):
    aijobs.enqueue("caption", "caption:1", {"photo_id": 1})
    aijobs.enqueue("review", "review:2", {"review_id": 2})
    aijobs.enqueue("thumbnail", "thumbnail:3", {"review_id": 3})
    while aijobs.run_one(flask_app):
        pass
    assert [k for k, _ in spy] == ["caption", "review", "thumbnail"]


def test_a_finished_job_is_deleted(spy, flask_app):
    aijobs.enqueue("review", "review:7", {"review_id": 7})
    assert aijobs.run_one(flask_app) is True
    assert aijobs.depth() == 0, "끝난 일거리가 테이블에 남았다 — 여기는 역사가 아니다"


def test_an_empty_queue_does_nothing(flask_app):
    assert aijobs.run_one(flask_app) is False


def test_a_failing_handler_does_not_block_the_queue(monkeypatch, flask_app):
    seen = []
    monkeypatch.setitem(aijobs._handlers, "review",
                        lambda a, p: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setitem(aijobs._handlers, "caption",
                        lambda a, p: seen.append(p))
    aijobs.enqueue("review", "review:1", {"review_id": 1})
    aijobs.enqueue("caption", "caption:2", {"photo_id": 2})
    while aijobs.run_one(flask_app):
        pass
    assert aijobs.depth() == 0, "터진 잡이 줄에 남아 뒤를 막는다"
    assert seen == [{"photo_id": 2}], "앞 잡이 터지자 뒤 잡이 안 돌았다"


def test_the_queue_refuses_image_bytes(spy, flask_app):
    """⛔ 큐에는 **참조만** 담는다 — 수십 MB blob 을 DB 에 적재하지 않는다."""
    big = {"photo": "x" * (aijobs.MAX_PAYLOAD_BYTES + 10)}
    assert aijobs.enqueue("review", "review:9", big) is False
    assert aijobs.depth() == 0


def test_an_unknown_kind_is_refused(flask_app):
    assert aijobs.enqueue("nope", "nope:1", {}) is False
    assert aijobs.depth() == 0


# --------------------------------------------------------------------------- #
# 재시작 복구 — 이 테이블이 존재하는 첫 번째 이유
# --------------------------------------------------------------------------- #
def test_a_job_left_running_by_a_dead_process_goes_back_in_line(spy, flask_app):
    aijobs.enqueue("review", "review:7", {"review_id": 7})
    job = aijobs.claim_next()
    assert job is not None and job.status == "running"
    # 다른 프로세스가 남긴 행으로 만든다(= 그 프로세스는 죽었다).
    job.owner = "some-dead-process"
    db.session.commit()

    revived, dropped = aijobs.recover()
    assert (revived, dropped) == (1, 0)
    assert AiJob.query.filter_by(job_key="review:7").one().status == "queued"
    assert aijobs.run_one(flask_app) is True
    assert spy and spy[0][1] == {"review_id": 7}


def test_a_job_that_keeps_crashing_is_eventually_dropped(spy, flask_app):
    aijobs.enqueue("review", "review:7", {"review_id": 7})
    row = AiJob.query.one()
    row.status = "running"
    row.owner = "dead"
    row.attempts = aijobs.MAX_ATTEMPTS
    db.session.commit()
    revived, dropped = aijobs.recover()
    assert (revived, dropped) == (0, 1), "영원히 터지는 잡이 큐를 막는다"


# --------------------------------------------------------------------------- #
# 앱 배선 — 스폰 지점이 스레드가 아니라 큐를 쓴다
# --------------------------------------------------------------------------- #
def test_review_generation_is_queued_not_threaded(
        flask_app, make_review, monkeypatch):
    started = []
    monkeypatch.setattr(app_module.threading, "Thread",
                        lambda *a, **k: started.append(k) or (_ for _ in ()).throw(
                            AssertionError("스레드를 띄웠다 — 큐를 써야 한다")))
    monkeypatch.setattr(aijobs, "start_pump", lambda app: None)
    review = make_review(status="pending")
    app_module._spawn_generate_review(flask_app, review.id)
    assert AiJob.query.filter_by(job_key=f"review:{review.id}").count() == 1


def test_a_queued_job_is_not_mistaken_for_an_orphan(
        flask_app, make_review, monkeypatch):
    """인프로세스 가드는 재시작에 비지만 큐 행은 남는다 — 그걸 '사라졌다'로 읽으면
    멀쩡히 줄 선 일을 다시 되살린다고 떠든다."""
    from datetime import datetime, timedelta
    review = make_review(status="pending")
    review.updated_at = datetime.utcnow() - timedelta(minutes=10)
    db.session.commit()
    monkeypatch.setattr(aijobs, "start_pump", lambda app: None)
    aijobs.enqueue("review", f"review:{review.id}", {"review_id": review.id})
    with app_module._generating_reviews_lock:          # 가드는 비어 있다(= 재시작)
        app_module._generating_reviews.discard(review.id)
    assert app_module.review_pending_verdict(review) == "running"

    AiJob.query.delete()
    db.session.commit()
    assert app_module.review_pending_verdict(review) == "orphaned"
