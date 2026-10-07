"""'오래 걸리는 건지 죽은 건지' — 화면이 그걸 **구분해서 말하는가**.

2026-10-07 사고의 마지막 조각. 큐 행(``ai_jobs``)은 필요한 걸 이미 다 알고 있었다
(차례 · 시작 시각 · 시도 횟수 · 지금 몇 단계). 그런데 **그걸 사람에게 꺼내는 길이
없었다** — 화면은 "AI가 블로그 초안을 쓰고 있어요…" 한 줄만 4초마다 반복했다.

여기서 못 박는 계약:

  1. **일하는 쪽이 자기 진척을 적는다** — claude 콜 사이에. 바깥에서 시간을 보고
     올리는 가짜 진행률이 아니다(멈추면 멈춘 채로 보인다).
  2. 분모는 **정직하다** — 추정으로 시작하고, 실제 수가 밝혀지면 고친다. 분자가
     분모를 넘는 거짓말은 안 한다.
  3. 화면은 **다섯 상태**를 구분해 말한다: 대기(앞에 몇 개) / 진행(몇 분째·몇 단계) /
     오래 걸리지만 정상(예상 범위) / 비정상(상한·재시도 누적 → ↻) / **아무도
     안 집어감**(펌프 정지).
  4. 기다리는 동안 **페이지를 통째로 다시 받지 않는다** — 작은 JSON 하나만 오간다.
  5. 폴링 엔드포인트가 **셀프힐 자리**를 그대로 물려받는다(meta refresh 가 하던 일).
"""
import json
from datetime import datetime, timedelta

import pytest

import ai
import aijobs
import app as app_module
import keyword_research
from models import AiJob, db


@pytest.fixture(autouse=True)
def _clean_queue(flask_app, monkeypatch):
    """빈 줄에서 시작하고, **진짜 펌프는 돌지 않게** 한다.

    이 모듈은 큐 행을 손으로 세워 놓고 화면이 뭐라고 말하는지 본다 — 백그라운드
    펌프가 그 행을 집어가 버리면 보려던 상태 자체가 사라진다. (앞선 테스트가
    ``generate_review`` 를 끝까지 돌리면 프리렌더 enqueue 가 펌프를 띄운다.)
    """
    aijobs.stop_pump()
    monkeypatch.setattr(aijobs, "start_pump", lambda app: None)
    AiJob.query.delete()
    db.session.commit()
    yield
    AiJob.query.delete()
    db.session.commit()


@pytest.fixture()
def healthy_pump(monkeypatch):
    """펌프가 멀쩡히 돌고 있는 상태를 흉내 낸다(테스트는 진짜 펌프를 안 띄운다)."""
    monkeypatch.setattr(
        aijobs, "pump_status",
        lambda: {"alive": True, "silent_sec": 0.0, "current_key": None},
    )


class _FakeSession:
    """프로세스를 띄우지 않는 claude 세션 대역."""

    def __init__(self, allow_web=False, **kw):
        self.allow_web = allow_web

    def start(self):
        return self

    def healthy(self):
        return True

    def ask(self, prompt, timeout=None):
        return "{}"

    def close(self):
        pass


def _queue_row(review, **kw):
    """그 후기의 큐 행을 만들어 준다(원하는 모양으로)."""
    row = AiJob(kind="review", job_key=f"review:{review.id}",
                payload_json=json.dumps({"review_id": review.id}),
                status=kw.pop("status", "queued"))
    for k, v in kw.items():
        setattr(row, k, v)
    db.session.add(row)
    db.session.commit()
    return row


# --------------------------------------------------------------------------- #
# 1. 일하는 쪽이 자기 진척을 적는다
# --------------------------------------------------------------------------- #
def test_progress_is_written_on_the_job_row(flask_app, make_review):
    review = make_review(status="pending")
    key = f"review:{review.id}"
    _queue_row(review, status="running")

    assert aijobs.progress(key, "본문 쓰는 중", 4, 8) is True
    snap = aijobs.snapshot(key)
    assert (snap["step"], snap["step_index"], snap["step_total"]) == (
        "본문 쓰는 중", 4, 8
    )


def test_progress_on_a_job_that_is_gone_is_harmless(flask_app):
    """행이 이미 지워졌으면 조용히 False — 진척 보고가 본업을 깨뜨리지 않는다."""
    assert aijobs.progress("review:999", "본문 쓰는 중", 1, 3) is False
    assert aijobs.snapshot("review:999") is None


def test_writing_progress_keeps_a_long_job_from_being_called_stale(
        flask_app, make_review):
    """진척이 곧 **생존 신호**다 — 21분짜리 작업이 stale 로 되돌려지면 안 된다."""
    review = make_review(status="pending")
    key = f"review:{review.id}"
    old = datetime.utcnow() - timedelta(seconds=aijobs.STALE_RUNNING_SEC + 60)
    _queue_row(review, status="running", owner=aijobs._OWNER,
               created_at=old, started_at=old, updated_at=old)

    assert aijobs.recover() == (1, 0), "진척을 안 적으면 stale 로 되돌려진다(기준선)"

    db.session.query(AiJob).filter_by(job_key=key).update(
        {"status": "running", "owner": aijobs._OWNER, "updated_at": old})
    db.session.commit()
    aijobs.progress(key, "사진 3/5 자르는 중", 7, 9)
    assert aijobs.recover() == (0, 0), (
        "진척을 적었는데도 '주인이 죽었다'고 되돌렸다 — 돌고 있는 작업을 죽인다"
    )


# --------------------------------------------------------------------------- #
# 2. 분모는 정직하다
# --------------------------------------------------------------------------- #
def test_the_denominator_is_fixed_when_the_real_count_shows_up(flask_app,
                                                               make_review):
    review = make_review(status="pending")
    _queue_row(review, status="running")
    prog = app_module._JobProgress(f"review:{review.id}", 4)
    prog.step("본문 쓰는 중")
    prog.retotal(1 + 7)           # 본문이 실제로 사진 7장을 썼다
    prog.step("사진 1/7 자르는 중")
    snap = aijobs.snapshot(f"review:{review.id}")
    assert (snap["step_index"], snap["step_total"]) == (2, 8)


def test_the_numerator_never_passes_the_denominator(flask_app, make_review):
    review = make_review(status="pending")
    _queue_row(review, status="running")
    prog = app_module._JobProgress(f"review:{review.id}", 1)
    prog.step("하나")
    prog.step("둘")               # 계획보다 길어졌다
    snap = aijobs.snapshot(f"review:{review.id}")
    assert snap["step_index"] == 2 and snap["step_total"] == 2, (
        "분자가 분모를 넘었다 — 사람이 보는 숫자가 거짓말을 한다"
    )


def test_a_note_does_not_advance_the_step(flask_app, make_review):
    """세마포어 대기처럼 '단계 밖 기다림'은 이름만 바꾼다(단계는 안 넘어간다)."""
    review = make_review(status="pending")
    _queue_row(review, status="running")
    prog = app_module._JobProgress(f"review:{review.id}", 3)
    prog.step("본문 쓰는 중")
    prog.note("claude 차례 기다리는 중")
    snap = aijobs.snapshot(f"review:{review.id}")
    assert snap["step"] == "claude 차례 기다리는 중"
    assert snap["step_index"] == 1


# --------------------------------------------------------------------------- #
# 3. 파이프라인이 claude 콜 **사이에** 적는다
# --------------------------------------------------------------------------- #
_CANDIDATE = [{"keyword": "압구정 고기집", "question": "?", "answerable": "!"}]


def test_keyword_research_reports_each_slow_leg(monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "build_review_input_block", lambda *a, **k: "input")
    monkeypatch.setattr(ai, "suggest_keyword_candidates",
                        lambda *a, **k: _CANDIDATE)
    monkeypatch.setattr(keyword_research, "collect_evidence",
                        lambda *a, **k: ({}, []))
    monkeypatch.setattr(ai, "select_keywords", lambda *a, **k: {"main": "x"})
    keyword_research.research(
        "주제", "위치", "산문", 8, {},
        client_id="id", client_secret="secret", on_step=seen.append,
    )
    assert seen == list(keyword_research.STEPS)


def test_a_failing_on_step_never_breaks_the_research(monkeypatch):
    monkeypatch.setattr(ai, "build_review_input_block", lambda *a, **k: "input")
    monkeypatch.setattr(ai, "suggest_keyword_candidates",
                        lambda *a, **k: _CANDIDATE)
    monkeypatch.setattr(keyword_research, "collect_evidence",
                        lambda *a, **k: ({}, []))
    monkeypatch.setattr(ai, "select_keywords", lambda *a, **k: {"main": "x"})

    def _boom(_label):
        raise RuntimeError("진척 보고가 터졌다")

    out = keyword_research.research(
        "주제", "위치", "산문", 8, {},
        client_id="id", client_secret="secret", on_step=_boom,
    )
    assert out["status"] == "ok", "진척 보고가 본업을 깨뜨렸다"


def test_crop_step_counts_the_blocks_that_actually_get_a_vision_call(
        flask_app, monkeypatch):
    """'사진 2/3' 의 분모는 사진 개수가 아니라 **본문이 실제로 쓴 image 블록 수**다."""
    seen = []

    class _P:
        def __init__(self, pid):
            self.id = pid

    photos = [_P(1), _P(2), _P(3), _P(4)]          # 올린 사진은 4장
    result = {"blocks": [
        {"type": "para", "text": "ㅎ"},
        {"type": "image", "photo_index": 0},
        {"type": "image", "photo_index": 2},        # 본문이 쓴 건 2장뿐
        {"type": "image", "photo_index": 99},       # 범위 밖 — 콜이 안 붙는다
    ]}
    monkeypatch.setattr(app_module, "_crop_vision_dims", lambda p: (100, 80))
    monkeypatch.setattr(app_module, "_crop_vision_url", lambda p: "http://x/i.jpg")
    monkeypatch.setattr(ai, "suggest_crop", lambda *a, **k: None)
    app_module._attach_section_crops(
        result, photos, acquire_sem=False,
        on_step=lambda done, total: seen.append((done, total)),
    )
    assert seen == [(1, 2), (2, 2)]


def test_the_pipeline_writes_progress_between_claude_calls(
        flask_app, make_review, monkeypatch):
    """본문을 쓰기 **전에** '본문 쓰는 중'이 행에 적혀 있어야 한다."""
    review = make_review(status="pending")
    key = f"review:{review.id}"
    _queue_row(review, status="running")
    seen = {}

    monkeypatch.setattr(ai, "ClaudeSession", lambda **kw: _FakeSession(**kw))
    monkeypatch.setattr(app_module.keyword_research, "research",
                        lambda *a, **k: {"status": "skipped"})
    monkeypatch.setattr(app_module, "_attach_section_crops", lambda *a, **k: None)

    def _write(*a, **k):
        seen["at_write"] = aijobs.snapshot(key)
        return {"title": "t", "blocks": []}

    monkeypatch.setattr(ai, "write_review", _write)
    app_module.generate_review(flask_app, review.id)

    snap = seen["at_write"]
    assert snap["step"] == "본문 쓰는 중"
    assert (snap["step_index"], snap["step_total"]) == (1, 1), (
        "키가 없어 조사가 꺼졌는데도 조사 단계가 분모에 남아 있다"
    )


# --------------------------------------------------------------------------- #
# 4. 화면이 다섯 상태를 **구분해서** 말한다
# --------------------------------------------------------------------------- #
def _view(flask_app, review, **kw):
    return app_module.review_progress_view(flask_app, review, **kw)


def test_1_waiting_in_line_says_how_many_are_ahead(flask_app, make_review,
                                                   healthy_pump):
    review = make_review(status="pending")
    db.session.add(AiJob(kind="caption", job_key="caption:1", status="queued"))
    db.session.add(AiJob(kind="caption", job_key="caption:2", status="running"))
    db.session.commit()
    _queue_row(review, status="queued")

    v = _view(flask_app, review)
    assert v["state"] == "queued" and v["ahead"] == 2
    assert "앞에 2개가 먼저야" in v["headline"]
    assert v["retry"] is False


def test_1b_front_of_the_line_says_so(flask_app, make_review, healthy_pump):
    review = make_review(status="pending")
    _queue_row(review, status="queued")
    v = _view(flask_app, review)
    assert v["state"] == "queued" and v["ahead"] == 0
    assert "줄 맨 앞이야" in v["headline"]


def test_2_running_says_how_long_and_which_step(flask_app, make_review,
                                                healthy_pump):
    review = make_review(status="pending")
    started = datetime.utcnow() - timedelta(minutes=2)
    _queue_row(review, status="running", started_at=started,
               step="본문 쓰는 중", step_index=4, step_total=8)
    v = _view(flask_app, review)
    assert v["state"] == "running"
    assert "2분째" in v["headline"] and "4/8단계" in v["headline"]
    assert "본문 쓰는 중" in v["headline"]
    assert v["retry"] is False


def test_2b_running_without_a_step_says_it_does_not_know(flask_app, make_review,
                                                         healthy_pump):
    """모르면 모른다고 쓴다 — 가짜 단계를 지어내지 않는다."""
    review = make_review(status="pending")
    _queue_row(review, status="running",
               started_at=datetime.utcnow() - timedelta(minutes=1))
    v = _view(flask_app, review)
    assert "안 적혔어" in v["headline"]


def test_3_a_long_but_normal_run_says_the_expected_range(flask_app, make_review,
                                                         healthy_pump):
    review = make_review(status="pending")
    _queue_row(review, status="running",
               started_at=datetime.utcnow() - timedelta(minutes=9),
               step="사진 2/5 자르는 중", step_index=6, step_total=9)
    v = _view(flask_app, review)
    assert v["state"] == "running_long"
    assert "아직 정상 범위야" in v["headline"] and "9분째" in v["headline"]
    assert "추정이야" in v["hint"], "예상 범위를 '추정'이라고 밝히지 않았다"
    assert "25분을 넘기면" in v["hint"]
    assert v["retry"] is False, "정상 범위인데 탈출구부터 들이민다"


def test_4_overdue_says_it_is_abnormal_and_offers_the_escape_hatch(
        flask_app, make_review, healthy_pump):
    review = make_review(status="pending")
    _queue_row(review, status="running",
               started_at=datetime.utcnow() - timedelta(minutes=40))
    v = _view(flask_app, review)
    assert v["state"] == "overdue"
    assert "상한을 넘겼어" in v["headline"]
    assert v["retry"] is True


def test_4b_piled_up_attempts_are_said_out_loud(flask_app, make_review,
                                                healthy_pump):
    review = make_review(status="pending")
    _queue_row(review, status="running", attempts=3,
               started_at=datetime.utcnow() - timedelta(minutes=1))
    v = _view(flask_app, review)
    assert "3번째 시도" in v["hint"]
    assert v["retry"] is True


def test_5_a_queue_nobody_pumps_is_its_own_state(flask_app, make_review,
                                                 monkeypatch):
    """줄에는 섰는데 아무도 집어가지 않는다 — '느린 것'과 **다른 상태**다."""
    woke = []
    monkeypatch.setattr(aijobs, "start_pump", lambda app: woke.append(app) or object())
    monkeypatch.setattr(
        aijobs, "pump_status",
        lambda: {"alive": False, "silent_sec": None, "current_key": None},
    )
    review = make_review(status="pending")
    _queue_row(review, status="queued",
               created_at=datetime.utcnow() - timedelta(minutes=4))
    v = _view(flask_app, review)
    assert v["state"] == "stalled"
    assert "아무도 집어가질 않아" in v["headline"]
    assert v["retry"] is True
    assert woke, "멈춘 걸 알아놓고 깨우지도 않았다"


def test_5b_a_pump_busy_with_another_job_is_not_stalled(flask_app, make_review,
                                                        monkeypatch):
    """한 잡을 21분째 쥐고 있는 펌프는 '멈춘' 게 아니다 — 그냥 앞 차례가 길다."""
    monkeypatch.setattr(aijobs, "start_pump", lambda app: None)
    monkeypatch.setattr(
        aijobs, "pump_status",
        lambda: {"alive": True, "silent_sec": 1200.0, "current_key": "caption:1"},
    )
    review = make_review(status="pending")
    db.session.add(AiJob(kind="caption", job_key="caption:1", status="running"))
    db.session.commit()
    _queue_row(review, status="queued",
               created_at=datetime.utcnow() - timedelta(minutes=10))
    v = _view(flask_app, review)
    assert v["state"] == "queued" and v["ahead"] == 1


# --------------------------------------------------------------------------- #
# 5. 폴링 — 페이지가 아니라 문장 한 줄만 다시 받는다
# --------------------------------------------------------------------------- #
def test_the_pending_page_no_longer_refreshes_itself(client, make_review):
    review = make_review(status="pending")
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "<noscript><meta http-equiv=\"refresh\"" in html, (
        "JS 없는 브라우저의 폴백까지 걷어냈다"
    )
    # noscript 밖에 남은 meta refresh 가 없어야 한다 — 그게 '페이지 통째로'였다.
    assert html.count('http-equiv="refresh"') == 1
    assert f"/reviews/{review.id}/progress" in html


def test_a_finished_review_page_polls_nothing_at_all(client, make_review):
    """다 된 화면은 **아무것도 더 묻지 않는다** — '재진입 네트워크 0'을 안 깬다."""
    review = make_review(
        status="ready",
        ai_json=json.dumps(
            {"title": "t", "blocks": [{"type": "para", "text": "본문"}]},
            ensure_ascii=False),
    )
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "rv-progress" not in html
    assert "http-equiv=\"refresh\"" not in html
    assert f"/reviews/{review.id}/progress" not in html


def test_the_progress_json_is_tiny_next_to_the_page(client, make_review):
    review = make_review(status="pending")
    page = client.get(f"/reviews/{review.id}").get_data()
    js = client.get(f"/reviews/{review.id}/progress")
    assert js.status_code == 200 and js.is_json
    assert len(js.get_data()) * 10 < len(page), (
        "폴링 한 번이 페이지의 1/10 보다 무겁다 — 바꾼 보람이 없다"
    )


def test_the_progress_json_tells_the_browser_when_to_stop(client, make_review):
    ready = make_review(
        status="ready",
        ai_json=json.dumps({"title": "t", "blocks": []}, ensure_ascii=False),
    )
    body = client.get(f"/reviews/{ready.id}/progress").get_json()
    assert body["done"] is True


def test_a_ready_draft_whose_crops_still_run_is_not_called_done(client,
                                                                make_review):
    """파이프라인은 **본문을 크롭 전에** 커밋한다 — 'ready' 가 곧 '끝'은 아니다.

    사진 크롭이 가장 긴 구간인데, 거기서 조용히 손 떼면 사람은 그림이 왜 바뀌는지
    모른다. 큐 행이 사라져야 진짜 끝이다.
    """
    review = make_review(
        status="ready",
        ai_json=json.dumps({"title": "t", "blocks": []}, ensure_ascii=False),
    )
    _queue_row(review, status="running", step="사진 2/5 자르는 중",
               step_index=6, step_total=9)
    body = client.get(f"/reviews/{review.id}/progress").get_json()
    assert body["done"] is False and body["state"] == "finishing"
    assert "사진 2/5 자르는 중" in body["headline"]
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "사진 2/5 자르는 중" in html


def test_the_progress_json_keeps_polling_while_the_thumbnail_is_pending(
        client, make_review):
    review = make_review(
        status="ready",
        ai_json=json.dumps({"title": "t", "blocks": []}, ensure_ascii=False),
        thumbnail_json=json.dumps({"status": "pending"}, ensure_ascii=False),
    )
    body = client.get(f"/reviews/{review.id}/progress").get_json()
    assert body["done"] is False and body["thumb_pending"] is True


def test_another_couples_progress_is_404(client, couple_user, flask_app):
    from models import BlogReview, Couple
    other = Couple(invite_code="OTHER")
    db.session.add(other)
    db.session.flush()
    theirs = BlogReview(couple_id=other.id, created_by=couple_user.id,
                        topic="남의 후기",
                        prose="x", overall_score=5, status="pending")
    db.session.add(theirs)
    db.session.commit()
    assert client.get(f"/reviews/{theirs.id}/progress").status_code == 404


# --------------------------------------------------------------------------- #
# 6. 폴링이 셀프힐 자리를 물려받는다 (meta refresh 가 하던 일)
# --------------------------------------------------------------------------- #
def test_polling_resumes_an_orphaned_draft(client, make_review, monkeypatch):
    """새로고침을 걷었으니, 복구는 **폴링이** 돌려야 한다.

    안 그러면 '사람이 화면을 다시 열어야 되살아나는' 2026-10-07 로 되돌아간다.
    """
    spawned = []
    monkeypatch.setattr(app_module, "_spawn_generate_review",
                        lambda app, rid: spawned.append(rid))
    app_module._generating_reviews.clear()
    review = make_review(status="pending")
    review.updated_at = datetime.utcnow() - timedelta(minutes=10)
    db.session.commit()

    body = client.get(f"/reviews/{review.id}/progress").get_json()
    assert spawned == [review.id], "폴링이 고아 초안을 되살리지 않았다"
    assert body["state"] == "resumed" and body["retry"] is True
    assert "다시 시작했어" in body["headline"]
