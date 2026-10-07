"""올린 사진을 다 쓰고(A) · 크롭을 묶어 묻고(B) · 멈춘 라벨을 설명하고(C) ·
끝나면 알린다(D).

여기서 못 박는 계약:

  A. 사람이 **고른** 사진이다 — 본문의 기본값은 **전부 배치**다. 프롬프트가 그렇게
     말하고 있는가(예전엔 "모든 사진을 다 넣을 필요는 없다"고 말했다). 다만 글을
     사진 수에 맞추라는 **기계적 수치 강제는 하지 않는다**(gf-blog 규율).
  B. 크롭은 **묶어서** 묻는다 — 사진 N장에 claude 턴 N번이 아니라 ``N/묶음`` 번.
     대신 세 가지를 구조로 막는다: 매핑 사고(키 검증) · 빠진 사진(그 장만 단건
     재질의) · 전부 아니면 전무(묶음 하나만 잃는다). 진척 보고는 **블록 수만큼**
     그대로 올라간다.
  C. 진척 쓰기가 실패하면 **흔적이 남는다**(카운터 + WARNING). 그리고 한 단계가
     길어서 라벨이 안 바뀌는 것은 'claude 가 방금까지 답하고 있었다'로 설명된다.
  D. 초안이 끝나면 **두 사람 모두에게** 알림이 간다 — 성공도 실패도. 사람이 멈춘
     것은 안 보낸다. 한 사이클에 한 번뿐이다.
"""
import logging
from datetime import datetime, timedelta

import pytest

import ai
import aijobs
import app as app_module
from models import AiJob, BlogReview, Notification, User, db


# --------------------------------------------------------------------------- #
# A. 올린 사진은 다 쓴다
# --------------------------------------------------------------------------- #
def _capture_prompt(monkeypatch):
    seen = {}

    def _run(prompt, timeout=None, allow_web=False):
        seen["prompt"] = prompt
        raise RuntimeError("프롬프트만 본다")

    monkeypatch.setattr(ai, "_run_claude", _run)
    return seen


def test_the_photo_rule_says_use_them_all_by_default(monkeypatch):
    """사진 규칙이 '다 쓰는 게 기본'이라고 말하는가 — 예전 문구가 남아 있지 않은가."""
    seen = _capture_prompt(monkeypatch)
    photos = [{"index": i, "caption": f"사진 {i}", "tags": []} for i in range(11)]
    assert ai.write_review("문래갈매기", "문래동", "맛있었어", 8, photos) is None
    p = seen["prompt"]
    assert "모든 사진을 다 넣을 필요는 없다" not in p, (
        "'다 안 써도 된다'가 그대로 남아 있다 — 사람이 고른 사진인데"
    )
    assert "전부 쓰는 것이 기본값이다" in p
    assert "0~10번이 빠짐없이 들어갔는지" in p, "다 썼는지 세어 보라고 안 한다"
    assert "11장" in p


def test_the_photo_rule_does_not_force_mechanical_counts(monkeypatch):
    """gf-blog 규율 — 글자 수·단락 수를 사진 수에 맞추라고 하면 안 된다."""
    seen = _capture_prompt(monkeypatch)
    photos = [{"index": i, "caption": f"사진 {i}", "tags": []} for i in range(5)]
    ai.write_review("문래갈매기", "문래동", "맛있었어", 8, photos)
    p = seen["prompt"]
    assert "글을 사진 수에 맞추지는 마라" in p
    assert "글자 수를 고정하지 마라" in p, "분량 고정 금지 규율이 사라졌다"
    # 빠질 수 있는 사진의 여지는 남아 있어야 한다(중복·흐림·무관).
    assert "중복" not in p or "겹쳐 올라왔거나" in p


def test_with_no_photos_the_rule_still_says_no_image_blocks(monkeypatch):
    seen = _capture_prompt(monkeypatch)
    ai.write_review("문래갈매기", "문래동", "맛있었어", 8, [])
    assert "image 블록은 넣지 마라" in seen["prompt"]


# --------------------------------------------------------------------------- #
# B. 크롭은 묶어서 묻는다
# --------------------------------------------------------------------------- #
def test_crop_batches_chunks_in_order():
    assert ai.crop_batches(list(range(11)), 4) == [
        [0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10],
    ]
    assert ai.crop_batches([1, 2], 1) == [[1], [2]], "1 이면 단건과 같아야 한다"
    assert ai.crop_batches([], 4) == []


def test_parse_crops_batch_refuses_a_key_we_did_not_ask_for():
    """⛔ 매핑 사고 — 모르는 키의 좌표는 **버린다**. 안 버리면 엉뚱한 데를 자른다."""
    data = {"crops": [
        {"key": "p00", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3},
        {"key": "p99", "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.3},   # 요청 안 한 키
        {"key": "p01", "x": 0.2, "y": 0.2, "w": 0.4, "h": 0.4},
    ]}
    got = ai.parse_crops_batch(data, ["p00", "p01"])
    assert set(got) == {"p00", "p01"}
    assert got["p00"] == [0.1, 0.1, 0.3, 0.3]


def test_parse_crops_batch_keeps_the_first_of_a_duplicated_key():
    data = {"crops": [
        {"key": "p00", "x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3},
        {"key": "p00", "x": 0.9, "y": 0.9, "w": 0.3, "h": 0.3},
    ]}
    assert ai.parse_crops_batch(data, ["p00"])["p00"] == [0.1, 0.1, 0.3, 0.3]


def test_parse_crops_batch_drops_an_unusable_box():
    data = {"crops": [{"key": "p00", "x": "?", "y": 0, "w": 1, "h": 1}]}
    assert ai.parse_crops_batch(data, ["p00"]) == {}


def test_parse_crops_batch_also_reads_a_keyed_object():
    data = {"p00": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}, "p9": {}}
    assert list(ai.parse_crops_batch(data, ["p00"])) == ["p00"]


class _P:
    def __init__(self, pid):
        self.id = pid


@pytest.fixture()
def crop_bed(monkeypatch):
    """크롭 경로를 claude 없이 돌릴 수 있게 깐다(픽셀·Graph 왕복 0)."""
    monkeypatch.setattr(app_module, "_crop_vision_dims", lambda p: (1200, 900))
    monkeypatch.setattr(app_module, "_crop_vision_url", lambda p: "http://x/i.jpg")
    monkeypatch.setattr(ai, "CROP_BATCH_SIZE", 4)
    return None


def _result_with(n):
    blocks = []
    for i in range(n):
        blocks.append({"type": "para", "text": f"단락 {i}"})
        blocks.append({"type": "image", "photo_index": i})
    return {"blocks": blocks}


def test_eleven_photos_take_three_turns_not_eleven(flask_app, crop_bed,
                                                   monkeypatch):
    """사진 11장 → claude 턴 11번이 아니라 3번(묶음 4)."""
    calls = []

    def _batch(jobs, aspect):
        calls.append([j["key"] for j in jobs])
        return {j["key"]: [0.1, 0.1, 0.5, 0.4] for j in jobs}

    monkeypatch.setattr(ai, "suggest_crops_batch", _batch)
    monkeypatch.setattr(ai, "suggest_crop", _never_called)
    result = _result_with(11)
    steps = []
    app_module._attach_section_crops(
        result, [_P(i) for i in range(11)], acquire_sem=False,
        on_step=lambda d, t: steps.append((d, t)),
    )
    assert len(calls) == 3, f"턴이 3번이 아니다: {calls}"
    assert calls == [["p00", "p01", "p02", "p03"],
                     ["p04", "p05", "p06", "p07"],
                     ["p08", "p09", "p10"]]
    # 진척은 **블록 수만큼** 순서대로 — 사람에겐 여전히 '사진 k/11'이 보인다.
    assert steps == [(i, 11) for i in range(1, 12)]
    crops = [b["crop"] for b in result["blocks"] if b["type"] == "image"]
    assert len(crops) == 11 and all(crops)


def _never_called(*a, **k):
    raise AssertionError("단건 크롭이 불렸다 — 묶음이 다 받았는데")


def test_a_photo_the_batch_skipped_is_re_asked_alone(flask_app, crop_bed,
                                                     monkeypatch):
    """묶음이 한 장을 빠뜨리면 **그 장만** 단건으로 다시 묻는다."""
    singles = []

    monkeypatch.setattr(
        ai, "suggest_crops_batch",
        lambda jobs, aspect: {j["key"]: [0.1, 0.1, 0.5, 0.4]
                              for j in jobs if j["key"] != "p02"},
    )

    def _single(data, hint, aspect, ext=".jpg", image_url=None):
        singles.append(hint)
        return [0.2, 0.2, 0.5, 0.4]

    monkeypatch.setattr(ai, "suggest_crop", _single)
    result = _result_with(4)
    app_module._attach_section_crops(result, [_P(i) for i in range(4)],
                                     acquire_sem=False)
    assert len(singles) == 1, "빠진 한 장만 다시 물어야 한다"
    crops = [b["crop"] for b in result["blocks"] if b["type"] == "image"]
    assert all(crops), "빠진 사진이 크롭 없이 남았다"


def test_a_broken_batch_only_loses_that_batch(flask_app, crop_bed, monkeypatch):
    """묶음 하나가 통째로 터져도 **그 묶음만** 손해다(나머지는 그대로 간다)."""
    seen = []

    def _batch(jobs, aspect):
        seen.append(jobs[0]["key"])
        if jobs[0]["key"] == "p00":
            raise RuntimeError("첫 묶음이 터졌다")
        return {j["key"]: [0.1, 0.1, 0.5, 0.4] for j in jobs}

    monkeypatch.setattr(ai, "suggest_crops_batch", _batch)
    monkeypatch.setattr(ai, "suggest_crop", lambda *a, **k: None)
    result = _result_with(8)
    steps = []
    app_module._attach_section_crops(
        result, [_P(i) for i in range(8)], acquire_sem=False,
        on_step=lambda d, t: steps.append(d),
    )
    assert seen == ["p00", "p04"], "첫 묶음이 터지자 둘째 묶음을 안 돌렸다"
    assert steps == list(range(1, 9)), "진척이 중간에 끊겼다"
    crops = [b.get("crop") for b in result["blocks"] if b["type"] == "image"]
    # 터진 묶음도 중앙 크롭으로는 채워진다 — 서빙이 항상 랜드스케이프가 되게.
    assert all(crops), "터진 묶음이 크롭 없이 남았다"


def test_a_single_image_block_asks_once_not_twice(flask_app, crop_bed,
                                                  monkeypatch):
    """한 장짜리는 묶을 게 없다 — 단건 한 번이고 재질의도 없다."""
    n = {"c": 0}

    def _single(*a, **k):
        n["c"] += 1
        return None       # 비전이 못 정했다 → 중앙 크롭

    monkeypatch.setattr(ai, "suggest_crop", _single)
    monkeypatch.setattr(ai, "suggest_crops_batch", _never_called)
    result = _result_with(1)
    app_module._attach_section_crops(result, [_P(0)], acquire_sem=False)
    assert n["c"] == 1, "같은 질문을 두 번 했다"


def test_cancelling_stops_the_remaining_batches(flask_app, crop_bed, monkeypatch):
    """사람이 멈추면 남은 묶음으로 넘어가지 않는다."""
    seen = []

    def _batch(jobs, aspect):
        seen.append(jobs[0]["key"])
        raise ai.Cancelled("사람이 멈췄다")

    monkeypatch.setattr(ai, "suggest_crops_batch", _batch)
    with pytest.raises(ai.Cancelled):
        app_module._attach_section_crops(_result_with(8), [_P(i) for i in range(8)],
                                         acquire_sem=False)
    assert seen == ["p00"], "중단했는데 다음 묶음을 돌렸다"


def test_suggest_crops_batch_requires_image_material():
    assert ai.suggest_crops_batch([{"key": "p00", "hint": "x"}], 4 / 3) == {}
    assert ai.suggest_crops_batch([], 4 / 3) == {}


# --------------------------------------------------------------------------- #
# C. 멈춘 라벨 — 실패는 흔적을 남기고, 긴 단계는 설명된다
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_progress_counters():
    aijobs.progress_failures = 0
    aijobs.progress_last_failure = None
    aijobs.progress_last_failure_at = None
    ai._last_activity_at = None
    yield
    aijobs.progress_failures = 0
    aijobs.progress_last_failure = None
    aijobs.progress_last_failure_at = None
    ai._last_activity_at = None


def test_a_progress_write_with_no_row_leaves_a_trace(flask_app, caplog):
    """⛔ 예전엔 완전한 무음이었다 — 영원히 모르면 고칠 수도 없다."""
    with caplog.at_level(logging.WARNING, logger="aijobs"):
        assert aijobs.progress("review:없는키", "본문 쓰는 중", 4, 15) is False
    assert aijobs.progress_failures == 1
    assert aijobs.progress_failure_age() is not None
    assert any("진척 기록 실패" in r.getMessage() for r in caplog.records), caplog.text


def test_a_progress_write_that_raises_never_breaks_the_job(flask_app, caplog,
                                                           monkeypatch):
    """진척 보고가 본업을 깨뜨리면 본말전도 — 삼키되 센다."""
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("DB 가 끊겼다")

    monkeypatch.setattr(aijobs.db, "session", _BoomSession())
    with caplog.at_level(logging.WARNING, logger="aijobs"):
        assert aijobs.progress("review:1", "본문 쓰는 중") is False
    assert aijobs.progress_failures == 1
    assert "DB 가 끊겼다" in (aijobs.progress_last_failure or "")


class _BoomSession:
    def query(self, *a, **k):
        raise RuntimeError("DB 가 끊겼다")

    def rollback(self):
        pass


def test_claude_activity_is_recorded_and_ages():
    assert ai.last_activity_age() is None
    ai._mark_activity()
    age = ai.last_activity_age()
    assert age is not None and age < 5


def test_a_long_step_is_explained_by_claude_still_talking(flask_app, make_review,
                                                          healthy_pump_c):
    """라벨이 안 바뀌는 게 '죽은 것'이 아님을 화면이 말해 준다."""
    review = make_review(status="pending")
    _queue(review, step="본문 쓰는 중", step_index=4, step_total=15,
           started_at=datetime.utcnow() - timedelta(minutes=8))
    ai._mark_activity()
    v = app_module.review_progress_view(flask_app, review)
    assert "4/15단계" in v["headline"]
    assert "claude 는 방금까지 답하고 있었어" in v["hint"]
    # 기존 안내를 **덮지 않는다** — 예상 범위·'안 끊는다'는 그대로여야 한다.
    assert "내가 끊지는 않아" in v["hint"]


def test_a_failing_progress_write_is_surfaced_on_screen(flask_app, make_review,
                                                        healthy_pump_c):
    review = make_review(status="pending")
    _queue(review, step="본문 쓰는 중", step_index=4, step_total=15,
           started_at=datetime.utcnow() - timedelta(minutes=2))
    aijobs.progress("review:없는키", "본문 쓰는 중", 4, 15)   # 실패 1건
    v = app_module.review_progress_view(flask_app, review)
    assert "단계 표시를 1번 못 적었어" in v["hint"]


@pytest.fixture()
def healthy_pump_c(monkeypatch):
    monkeypatch.setattr(
        aijobs, "pump_status",
        lambda: {"alive": True, "silent_sec": 0.0, "current_key": None},
    )


def _queue(review, **kw):
    kw.setdefault("started_at", datetime.utcnow())
    row = AiJob(kind="review", job_key=f"review:{review.id}", status="running",
                owner="t", attempts=1, created_at=datetime.utcnow(), **kw)
    db.session.add(row)
    db.session.commit()
    return row


# --------------------------------------------------------------------------- #
# D. 끝나면 알린다 — 두 사람 모두에게, 한 번만
# --------------------------------------------------------------------------- #
@pytest.fixture()
def pair(couple_user):
    """커플 두 사람 — 게시판이 공용이라 알림도 둘 다 받는다."""
    mate = User(email="b@example.com", password_hash="x", display_name="반쪽",
                couple_id=couple_user.couple_id, status="approved")
    db.session.add(mate)
    db.session.commit()
    return couple_user, mate


@pytest.fixture()
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "send_push",
                        lambda user, title, body, url: sent.append(
                            (user.id, title, body, url)))
    return sent


def test_a_finished_draft_notifies_both_of_them_once(flask_app, pair,
                                                     make_review, pushes):
    me, mate = pair
    review = make_review(status="ready", ai_json='{"title":"t","blocks":[]}')
    assert app_module._notify_review_done(review) is True
    rows = Notification.query.all()
    assert {r.user_id for r in rows} == {me.id, mate.id}
    assert all(r.link == f"/reviews/{review.id}" for r in rows)
    assert all("문래갈매기" in r.message for r in rows)
    assert all(r.type == "review_ai" for r in rows)
    # 푸시도 같은 자리에서 같이 나간다 — 새 발송 경로를 만들지 않았다.
    assert {u for u, _t, _b, _u in pushes} == {me.id, mate.id}
    assert all(u == f"/reviews/{review.id}" for _i, _t, _b, u in pushes)


def test_a_failed_draft_also_notifies(flask_app, pair, make_review, pushes):
    """기다리다 아무 소식 없는 게 제일 나쁘다 — 실패도 알린다."""
    review = make_review(status="failed")
    assert app_module._notify_review_done(review) is True
    rows = Notification.query.all()
    assert len(rows) == 2
    assert all("못 만들었어" in r.message for r in rows)


def test_a_cancelled_draft_does_not_notify(flask_app, pair, make_review, pushes):
    """자기가 ✋ 를 눌러 놓고 알림을 받을 이유는 없다."""
    review = make_review(status="cancelled")
    assert app_module._notify_review_done(review) is False
    assert Notification.query.count() == 0 and pushes == []


def test_the_same_cycle_never_notifies_twice(flask_app, pair, make_review,
                                             pushes):
    """재시도·셀프힐로 완료 전이를 여러 번 밟아도 한 사이클엔 한 번뿐."""
    review = make_review(status="ready")
    assert app_module._notify_review_done(review) is True
    assert app_module._notify_review_done(review) is False
    assert app_module._notify_review_done(review) is False
    assert Notification.query.count() == 2, "같은 사이클인데 또 울렸다"
    assert len(pushes) == 2


def test_a_new_cycle_arms_the_notification_again(flask_app, pair, make_review,
                                                 pushes, monkeypatch):
    """↻ 로 다시 돌리면 그건 새 사이클이다 — 그땐 다시 알린다."""
    monkeypatch.setattr(app_module, "_enqueue_ai", lambda *a, **k: True)
    review = make_review(status="ready")
    app_module._notify_review_done(review)
    app_module._spawn_generate_review(flask_app, review.id)   # 새 사이클
    app_module._generating_reviews.discard(review.id)
    row = db.session.get(BlogReview, review.id)
    assert row.ai_notified_at is None, "새 사이클인데 지난 표시가 남았다"
    row.status = "ready"
    db.session.commit()
    assert app_module._notify_review_done(row) is True
    assert Notification.query.count() == 4


def test_a_notification_failure_never_breaks_generation(flask_app, pair,
                                                        make_review, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("알림이 터졌다")

    monkeypatch.setattr(app_module, "_safe_notify", _boom)
    review = make_review(status="ready")
    assert app_module._notify_review_done(review) is False   # 삼킨다, raise 안 함
