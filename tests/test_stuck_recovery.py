"""2026-10-07 운영 사고 회귀 — '크래시로 죽은 pending 행'이 되살아나는가.

사고: Render 가 메모리 한도 초과로 워커를 자동 재시작했다. 생성 중이던 인프로세스
데몬 스레드가 같이 죽었고, DB 의 블로그 초안 행은 ``pending`` 그대로 **영원히** 남았다.
화면은 '작성중'만 말하며 4초마다 새로고침을 돌았고, 사용자가 빠져나갈 길이 없었다.

여기서 지키려는 계약:
  * 만드는 스레드가 사라진 'pending' 행은 **다음 조회에서 되살아난다**(재스폰).
  * 되살릴 때 **직전 산출물(ai_json·edited_text)을 절대 지우지 않는다.**
  * 정상 생성 중인 행을 stale 로 **오인하지 않는다** — 후기 상한은 월간 회고의
    5분이 아니라 후기용(_STUCK_REVIEW)이다.
  * 화면이 무슨 일이 났는지 말하고 ↻ 탈출구를 준다(무한 새로고침만 남지 않는다).
  * 같은 구멍(인프로세스 스레드 + pending 행)인 썸네일 카피·판결·데이트 추천도
    같은 방식으로 되살아난다.
"""
import json
from datetime import datetime, timedelta

import pytest

import app as app_module
from models import Case, DateRecommendation, db


# --------------------------------------------------------------------------- #
# 0. 공통 헬퍼
# --------------------------------------------------------------------------- #
def _age(row, minutes):
    """행을 'minutes 분 전에 pending 이 된 것'으로 만든다."""
    row.updated_at = datetime.utcnow() - timedelta(minutes=minutes)
    db.session.commit()


@pytest.fixture()
def spawned(monkeypatch):
    """스폰 호출을 기록만 하는 가짜 — 테스트에서 claude 는 절대 돌지 않는다."""
    calls = {"review": [], "thumb": [], "judge": [], "reco": []}
    monkeypatch.setattr(app_module, "_spawn_generate_review",
                        lambda app, rid: calls["review"].append(rid))
    monkeypatch.setattr(app_module, "_spawn_generate_thumbnail",
                        lambda app, rid: calls["thumb"].append(rid))
    monkeypatch.setattr(app_module, "_spawn_judge_if_idle",
                        lambda app, cid: calls["judge"].append(cid))
    monkeypatch.setattr(app_module, "_spawn_recommend_if_idle",
                        lambda app, cid: calls["reco"].append(cid))
    app_module._generating_reviews.clear()
    app_module._refailed_reviews.clear()   # 'failed 자동 1회 재시도' 가드
    app_module._thumbing_reviews.clear()
    app_module._judging.clear()
    app_module._recommending.clear()
    yield calls
    app_module._generating_reviews.clear()
    app_module._refailed_reviews.clear()   # 'failed 자동 1회 재시도' 가드
    app_module._thumbing_reviews.clear()
    app_module._judging.clear()
    app_module._recommending.clear()


# --------------------------------------------------------------------------- #
# 1. 상한 선택의 근거 — 5분은 후기에 쓸 수 없다
# --------------------------------------------------------------------------- #
def test_review_threshold_is_longer_than_the_monthly_one():
    """후기 생성은 claude 콜이 여러 번이라 월간 회고의 5분을 그대로 쓰면 안 된다.

    실측 정상치 ~170초 + 사진마다 비전 크롭(ai.CAPTION_TIMEOUT=180s)이 붙는다 —
    사진 5장이면 21분까지 '정상'이다. 상한이 그보다 짧으면 정상 생성을 stale 로
    오인한다.
    """
    import ai

    worst = (3 * ai.CLAUDE_TIMEOUT) + (5 * ai.CAPTION_TIMEOUT)  # 초
    assert app_module._STUCK_REVIEW > app_module._STUCK_GENERATING
    assert app_module._STUCK_REVIEW >= timedelta(seconds=worst), (
        "후기 상한이 '사진 5장짜리 최악의 정상 생성'보다 짧다 — 정상 생성을 "
        "stale 로 오인한다"
    )


def test_a_slow_but_live_generation_is_not_called_stale(make_review, spawned):
    """정상 생성(스레드 살아 있음)이 10분째여도 stale 이 아니다 — 5분 상한이었다면
    여기서 오인했을 것이다."""
    review = make_review(status="pending")
    app_module._generating_reviews.add(review.id)  # 스레드가 들고 있다
    _age(review, 10)
    assert app_module.review_pending_verdict(review) == "running"
    assert app_module.resume_review_if_orphaned(None, review) is False
    assert spawned["review"] == []


# --------------------------------------------------------------------------- #
# 2. 사고 그 자체 — 크래시로 죽은 pending 이 되살아난다
# --------------------------------------------------------------------------- #
def test_crashed_pending_review_is_detected_as_orphaned(make_review, spawned):
    """프로세스 재시작 = 인프로세스 가드가 비어 있다 → 'orphaned'."""
    review = make_review(status="pending")
    _age(review, 60 * 20)  # 어제부터 멈춰 있던 행
    assert app_module.review_pending_verdict(review) == "orphaned"


def test_crashed_pending_review_is_resumed(flask_app, make_review, spawned):
    review = make_review(status="pending")
    _age(review, 180)
    with flask_app.test_request_context():
        assert app_module.resume_review_if_orphaned(flask_app, review) is True
    assert spawned["review"] == [review.id], "되살리면서 생성을 다시 스폰해야 한다"
    # updated_at 이 '지금'으로 갱신돼 다음 조회가 또 되살리지 않는다.
    assert (datetime.utcnow() - review.updated_at) < timedelta(seconds=30)


def test_resuming_never_destroys_the_previous_draft(flask_app, make_review, spawned):
    """⚠️ 자동 복구는 사람이 누른 /regenerate 가 아니다 — 직전 초안과 사람이 편집한
    복사본을 그대로 둔다(재생성이 빈손이어도 직전 산출물을 안 지우는 규율)."""
    review = make_review(
        status="pending",
        ai_json=json.dumps({"title": "직전 초안", "blocks": []}, ensure_ascii=False),
        edited_text="<p>사람이 손본 복사본</p>",
        research_json=json.dumps({"status": "ok"}, ensure_ascii=False),
    )
    _age(review, 180)
    with flask_app.test_request_context():
        assert app_module.resume_review_if_orphaned(flask_app, review) is True
    assert review.ai["title"] == "직전 초안"
    assert review.edited_text == "<p>사람이 손본 복사본</p>"
    assert review.research["status"] == "ok"
    assert review.status == "pending"


def test_just_spawned_pending_is_not_resumed(make_review, spawned):
    """스폰 직후의 찰나(행 커밋 ↔ 가드 등록)는 유예한다 — 중복 생성 금지."""
    review = make_review(status="pending")  # updated_at = 방금
    assert app_module.review_pending_verdict(review) == "running"
    assert app_module.resume_review_if_orphaned(None, review) is False
    assert spawned["review"] == []


def test_hung_generation_is_overdue_but_not_respawned(make_review, spawned):
    """스레드는 살아 있는데 상한을 넘겼다 → 자동 재스폰은 안 하고(중복 claude 금지)
    화면이 사람에게 탈출구를 준다."""
    review = make_review(status="pending")
    app_module._generating_reviews.add(review.id)
    _age(review, 60)  # _STUCK_REVIEW(25분) 초과
    assert app_module.review_pending_verdict(review) == "overdue"
    assert app_module.resume_review_if_orphaned(None, review) is False
    assert spawned["review"] == []


def test_non_pending_review_has_no_verdict(make_review):
    assert app_module.review_pending_verdict(make_review(status="ready")) is None
    assert app_module.review_pending_verdict(make_review(status="failed")) is None
    assert app_module.review_pending_verdict(None) is None


# --------------------------------------------------------------------------- #
# 3. 화면 — 무한 새로고침만 남지 않는다
# --------------------------------------------------------------------------- #
def test_detail_page_resumes_and_says_what_happened(client, make_review, spawned):
    """사용자가 멈춘 초안 화면을 열면: 되살아나고 + 무슨 일인지 말하고 + ↻ 버튼."""
    review = make_review(status="pending")
    _age(review, 180)
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert spawned["review"] == [review.id]
    assert "중단된 것 같아" in html
    assert "다시 생성" in html
    assert f"/reviews/{review.id}/regenerate" in html


# --------------------------------------------------------------------------- #
# 'failed' 로 끝난 초안도 한 번은 되살아난다 — 같은 사고가 pending/failed 로
# 갈리는 **비대칭**을 없앤다.
#
# 2026-10-07 에 후기 두 건이 같은 원인(메모리 초과)으로 멈췄는데 결과가 달랐다.
# 리눅스 OOM 킬러는 cgroup 에서 가장 큰 프로세스를 고르는데, 이 앱에서 그건 보통
# `claude`(Node, 실측 ~400MB)지 gunicorn 워커(~95MB)가 아니다:
#   * 워커째 죽으면 → 행이 pending 으로 남고 고아 복구가 되살린다(사용자가 본 쪽).
#   * claude 자식만 죽으면 → `_run_claude` 가 "exited -9" 로 raise → write_review 가
#     None → prior 없음 → **failed 로 종결**. 자동 복구 경로가 없었다(남은 쪽).
# --------------------------------------------------------------------------- #
def test_a_failed_draft_is_retried_once_when_the_page_is_opened(
        client, make_review, spawned):
    review = make_review(status="failed")
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert spawned["review"] == [review.id], "실패한 초안을 다시 돌리지 않았다"
    db.session.refresh(review)
    assert review.status == "pending"
    assert "다시 시작했어" in html


def test_a_failed_draft_is_not_retried_in_a_loop(client, make_review, spawned):
    """한 프로세스에서 **한 번만** — 영영 안 되는 초안으로 claude 를 돌리지 않는다."""
    review = make_review(status="failed")
    client.get(f"/reviews/{review.id}")
    review.status = "failed"            # 재시도도 실패했다고 치자
    db.session.commit()
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert spawned["review"] == [review.id], "같은 행을 두 번 돌렸다"
    db.session.refresh(review)
    assert review.status == "failed"
    assert "다시 생성" in html, "자동 재시도를 멈췄으면 사람 탈출구가 있어야 한다"


def test_retrying_a_failed_draft_keeps_everything(client, make_review, spawned):
    """자동 재시도는 ``resume_review_if_orphaned`` 와 같은 규율 — 아무것도 안 지운다."""
    review = make_review(status="failed", edited_text="<p>사람이 고친 본문</p>",
                         research_json='{"status":"ok"}')
    client.get(f"/reviews/{review.id}")
    db.session.refresh(review)
    assert review.edited_text == "<p>사람이 고친 본문</p>"
    assert review.research_json == '{"status":"ok"}'


def test_the_draft_is_committed_before_the_long_crop_pass(
        client, make_review, monkeypatch, flask_app):
    """본문이 나오면 **크롭 전에** 커밋한다 — 크롭 중 죽어도 120초가 안 날아간다.

    크롭 단계는 사진마다 claude 비전 콜이라 이 작업에서 가장 긴 구간이다.
    """
    import ai
    review = make_review(status="pending")
    seen = {}

    monkeypatch.setattr(ai, "write_review",
                        lambda *a, **k: {"title": "t", "blocks": []})
    monkeypatch.setattr(app_module.keyword_research, "research",
                        lambda *a, **k: {"status": "skipped"})

    def _crops(result, photos, **kw):   # kw: acquire_sem (파이프라인이 넘긴다)
        # 크롭 단계에 들어온 '그 순간' 행이 어떤 상태인지 본다.
        row = db.session.get(type(review), review.id)
        seen["status_during_crops"] = row.status
        seen["ai_during_crops"] = bool(row.ai_json)

    monkeypatch.setattr(app_module, "_attach_section_crops", _crops)
    app_module.generate_review(flask_app, review.id)
    assert seen.get("status_during_crops") == "ready", (
        "크롭이 도는 동안 본문이 아직 커밋되지 않았다"
    )
    assert seen.get("ai_during_crops") is True


def test_detail_page_of_a_live_generation_still_just_waits(client, make_review,
                                                           spawned):
    review = make_review(status="pending")
    app_module._generating_reviews.add(review.id)
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    # 말투가 반말체로 통일됐다(앱 전체와 같은 말투) — 뜻은 그대로다.
    assert "블로그 초안을 쓰고 있어" in html
    assert "중단된 것 같아" not in html
    assert spawned["review"] == []


def test_detail_page_of_a_hung_generation_offers_the_escape_hatch(
        client, make_review, spawned):
    review = make_review(status="pending")
    app_module._generating_reviews.add(review.id)
    _age(review, 60)
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "중단된 것 같아" in html
    assert f"/reviews/{review.id}/regenerate" in html
    assert spawned["review"] == [], "hung 생성에 두 번째 claude 를 붙이지 않는다"


# --------------------------------------------------------------------------- #
# 4. 같은 구멍 — 썸네일 카피
# --------------------------------------------------------------------------- #
def test_crashed_thumbnail_copy_is_resumed(flask_app, make_review, spawned):
    review = make_review(
        status="ready",
        ai_json=json.dumps({"title": "t", "blocks": []}, ensure_ascii=False),
        thumbnail_json=json.dumps({"status": "pending"}, ensure_ascii=False),
    )
    _age(review, 30)
    assert app_module.thumbnail_pending_verdict(review) == "orphaned"
    with flask_app.test_request_context():
        assert app_module.resume_thumbnail_if_orphaned(flask_app, review) is True
    assert spawned["thumb"] == [review.id]
    # 사람이 고른 내용은 그대로 — 상태만 pending 으로 남아 있다.
    assert review.thumbnail["status"] == "pending"


def test_detail_page_resumes_a_stuck_thumbnail_copy(client, make_review, spawned):
    review = make_review(
        status="ready",
        ai_json=json.dumps(
            {"title": "제목", "blocks": [{"type": "para", "text": "본문"}]},
            ensure_ascii=False),
        thumbnail_json=json.dumps({"status": "pending"}, ensure_ascii=False),
    )
    _age(review, 30)
    resp = client.get(f"/reviews/{review.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert spawned["thumb"] == [review.id]
    assert "중단된 것 같아" in html
    assert "문구 다시 만들기" in html


def test_live_thumbnail_copy_is_left_alone(make_review, spawned):
    review = make_review(
        status="ready",
        ai_json=json.dumps({"title": "t", "blocks": []}, ensure_ascii=False),
        thumbnail_json=json.dumps({"status": "pending"}, ensure_ascii=False),
    )
    app_module._thumbing_reviews.add(review.id)
    assert app_module.thumbnail_pending_verdict(review) == "running"
    assert app_module.resume_thumbnail_if_orphaned(None, review) is False
    assert spawned["thumb"] == []


# --------------------------------------------------------------------------- #
# 5. 같은 구멍 — 판결(Case)
# --------------------------------------------------------------------------- #
def test_crashed_judging_case_is_resumed(flask_app, couple_user, spawned):
    case = Case(couple_id=couple_user.couple_id, created_by=couple_user.id,
                title="누가 설거지", situation="…", status="judging")
    db.session.add(case)
    db.session.commit()
    _age(case, 30)
    assert app_module.case_judging_verdict(case) == "orphaned"
    with flask_app.test_request_context():
        assert app_module.resume_judging_if_orphaned(flask_app, case) is True
    assert spawned["judge"] == [case.id]
    assert case.status == "judging"


def test_judging_case_page_unlocks_the_button_when_stuck(client, couple_user,
                                                         spawned):
    """'판결 중…' + disabled 로 영구 고착되면 안 된다 — 되살리고 버튼도 풀어준다."""
    from models import CaseStatement

    case = Case(couple_id=couple_user.couple_id, created_by=couple_user.id,
                title="누가 설거지", situation="…", status="judging")
    db.session.add(case)
    db.session.flush()
    db.session.add(CaseStatement(case_id=case.id, user_id=couple_user.id,
                                 text="내 진술"))
    db.session.commit()
    _age(case, 30)
    html = client.get(f"/cases/{case.id}").get_data(as_text=True)
    assert spawned["judge"] == [case.id]
    assert "다시 시작했어" in html
    assert "판결 다시 맡기기" in html


def test_live_judging_case_is_left_alone(couple_user, spawned):
    case = Case(couple_id=couple_user.couple_id, created_by=couple_user.id,
                title="x", situation="…", status="judging")
    db.session.add(case)
    db.session.commit()
    app_module._judging.add(case.id)
    assert app_module.case_judging_verdict(case) == "running"
    assert app_module.resume_judging_if_orphaned(None, case) is False
    assert spawned["judge"] == []


# --------------------------------------------------------------------------- #
# 6. 같은 구멍 — 데이트 추천
# --------------------------------------------------------------------------- #
def test_crashed_recommendation_is_resumed(flask_app, couple_user, spawned):
    rec = DateRecommendation(couple_id=couple_user.couple_id, status="pending",
                             message="직전 추천")
    db.session.add(rec)
    db.session.commit()
    _age(rec, 30)
    with flask_app.test_request_context():
        assert app_module.resume_recommendation_if_orphaned(
            flask_app, couple_user.couple_id) is True
    assert spawned["reco"] == [couple_user.couple_id]
    assert rec.message == "직전 추천", "직전 추천 내용을 지우지 않는다"


def test_live_recommendation_is_left_alone(flask_app, couple_user, spawned):
    rec = DateRecommendation(couple_id=couple_user.couple_id, status="pending")
    db.session.add(rec)
    db.session.commit()
    app_module._recommending.add(couple_user.couple_id)
    _age(rec, 30)
    with flask_app.test_request_context():
        assert app_module.resume_recommendation_if_orphaned(
            flask_app, couple_user.couple_id) is False
    assert spawned["reco"] == []


def test_ready_recommendation_is_not_touched(flask_app, couple_user, spawned):
    rec = DateRecommendation(couple_id=couple_user.couple_id, status="ready",
                             message="좋은 추천")
    db.session.add(rec)
    db.session.commit()
    _age(rec, 600)
    with flask_app.test_request_context():
        assert app_module.resume_recommendation_if_orphaned(
            flask_app, couple_user.couple_id) is False
    assert spawned["reco"] == []
