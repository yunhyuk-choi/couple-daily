"""2026-10-07 OOM 회귀 — `claude -p` 는 **절대 둘이 동시에 뜨지 않는다**.

실측(2026-10-07): `claude -p` 한 개의 RSS 봉우리가 약 400MB다. 512MB 한 칸에
파이썬 워커(~92MB)까지 같이 사는 무료 티어에서 **둘이 겹치면 그 자리에서 죽는다.**
레포는 이미 "512MB: 동시에 claude 하나만"을 규율로 적어 뒀지만, 실제로는 그 문
(``_CAPTION_SEM``)을 **안 지나는 호출이 셋** 있었다:

  * 월간 회고(regenerate_monthly_report) — ``/answer`` 커밋마다 트리거
  * 판결(judge_case) — 사람이 아무 때나 누르는 버튼
  * 오늘의 질문(get_or_create_today_question) — **요청 경로**

신규 기능(키워드 조사→썸네일→GIF)이 들어오며 후기 초안이 세마포어를 쥐는 시간이
~20초에서 170초+ 로 늘자, 그 위에 셋 중 하나가 겹칠 확률이 같이 뛰었다.

여기서 지키려는 계약:
  1. 백그라운드 claude 호출은 **전부** 슬롯을 쥐고 돈다.
  2. 요청 경로(오늘의 질문)는 슬롯을 **짧게만** 기다린다 — gunicorn 타임아웃에
     걸릴 때까지 블로킹하지 않는다.
  3. 그렇다고 기능을 깎지 않는다 — 슬롯이 바빠 폴백으로 띄운 질문은 나중에
     **개인화 질문으로 올라간다**(아무도 답하지 않았을 때만).
"""
import threading
from datetime import date, timedelta

import pytest

import ai
import app as app_module
from models import Case, CaseStatement, Couple, DailyQuestion, User, db


@pytest.fixture(autouse=True)
def _free_slot():
    """매 테스트는 슬롯이 비어 있는 상태에서 시작하고, 끝나면 반드시 비운다."""
    while app_module._CAPTION_SEM.acquire(blocking=False):
        pass
    app_module._CAPTION_SEM.release()
    yield
    while app_module._CAPTION_SEM.acquire(blocking=False):
        pass
    app_module._CAPTION_SEM.release()


def _slot_was_held():
    """지금 claude 슬롯이 잡혀 있나 — 잡혀 있으면 True(= 직렬화되고 있다).

    공개 API 만 쓴다: 비차단으로 잡아 보고, 잡히면 아무도 안 쥐고 있었다는 뜻이다.
    """
    if app_module._CAPTION_SEM.acquire(blocking=False):
        app_module._CAPTION_SEM.release()
        return False
    return True


@pytest.fixture()
def partnered(couple_user):
    """승인 멤버 2명 — 월간 회고는 두 사람이 있어야 돈다."""
    mate = User(email="b@example.com", password_hash="x", display_name="짝",
                couple_id=couple_user.couple_id, status="approved")
    db.session.add(mate)
    db.session.commit()
    return couple_user, mate


# --------------------------------------------------------------------------- #
# 1. 백그라운드 claude 호출은 전부 슬롯을 쥐고 돈다
# --------------------------------------------------------------------------- #
def test_monthly_report_holds_the_claude_slot(flask_app, partnered, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ai, "generate_monthly_report",
        lambda *a, **k: seen.setdefault("held", _slot_was_held()) or None)
    u, _ = partnered
    app_module.regenerate_monthly_report(flask_app, u.couple_id, 2026, 10)
    assert seen.get("held") is True, "월간 회고가 슬롯 없이 claude 를 띄웠다"


def test_judge_holds_the_claude_slot(flask_app, couple_user, monkeypatch):
    case = Case(couple_id=couple_user.couple_id, created_by=couple_user.id,
                title="t", situation="s", status="judging")
    db.session.add(case)
    db.session.flush()
    db.session.add(CaseStatement(case_id=case.id, user_id=couple_user.id, text="진술"))
    db.session.commit()
    seen = {}
    monkeypatch.setattr(
        ai, "judge_fight",
        lambda *a, **k: seen.setdefault("held", _slot_was_held()) or None)
    app_module.judge_case(flask_app, case.id)
    assert seen.get("held") is True, "판결이 슬롯 없이 claude 를 띄웠다"


def test_daily_question_holds_the_claude_slot(flask_app, couple_user, monkeypatch):
    seen = {}

    def _gen(recent_pairs, past_questions=None):
        seen["held"] = _slot_was_held()
        return "슬롯 안에서 만든 질문", "ai"

    monkeypatch.setattr(ai, "generate_daily_question", _gen)
    with flask_app.test_request_context():
        q = app_module.get_or_create_today_question(couple_user.couple)
    assert seen.get("held") is True, "오늘의 질문이 슬롯 없이 claude 를 띄웠다"
    assert q.text == "슬롯 안에서 만든 질문" and q.source == "ai"


def test_the_slot_is_released_even_when_claude_raises(flask_app, partnered,
                                                      monkeypatch):
    """claude 가 터져도 슬롯을 돌려줘야 한다 — 안 그러면 앱 전체가 멈춘다."""
    def _boom(*a, **k):
        raise RuntimeError("claude CLI timed out")

    monkeypatch.setattr(ai, "generate_monthly_report", _boom)
    u, _ = partnered
    app_module.regenerate_monthly_report(flask_app, u.couple_id, 2026, 10)
    assert app_module._CAPTION_SEM.acquire(blocking=False), "슬롯이 샜다"
    app_module._CAPTION_SEM.release()


# --------------------------------------------------------------------------- #
# 2. 요청 경로는 슬롯을 기다리다 타임아웃에 걸리지 않는다
# --------------------------------------------------------------------------- #
def test_daily_question_does_not_block_when_the_slot_is_busy(
        flask_app, couple_user, monkeypatch):
    """슬롯이 바쁘면 claude 를 **아예 돌리지 않고** 즉시 폴백으로 응답한다."""
    called = {"n": 0}

    def _gen(*a, **k):
        called["n"] += 1
        return "느린 개인화 질문", "ai"

    monkeypatch.setattr(ai, "generate_daily_question", _gen)
    monkeypatch.setattr(app_module, "_QUESTION_SLOT_WAIT", 0.2)
    spawned = []
    monkeypatch.setattr(app_module, "_spawn_question_upgrade",
                        lambda app, qid: spawned.append(qid))

    app_module._CAPTION_SEM.acquire()  # 후기 초안이 돌고 있는 상황
    try:
        with flask_app.test_request_context():
            q = app_module.get_or_create_today_question(couple_user.couple)
    finally:
        app_module._CAPTION_SEM.release()

    assert called["n"] == 0, "슬롯이 바쁜데 두 번째 claude 를 띄웠다"
    assert q is not None and q.text in ai.FALLBACK_QUESTIONS
    assert q.source == "fallback"
    assert spawned == [q.id], "나중에 개인화 질문으로 올려줄 예약이 없다"


def test_today_page_renders_while_the_slot_is_busy(client, couple_user, monkeypatch):
    """실제 라우트로도 확인 — 슬롯이 바빠도 /today 는 질문을 들고 바로 뜬다."""
    monkeypatch.setattr(ai, "generate_daily_question",
                        lambda *a, **k: pytest.fail("두 번째 claude 가 떴다"))
    monkeypatch.setattr(app_module, "_QUESTION_SLOT_WAIT", 0.2)
    monkeypatch.setattr(app_module, "_spawn_question_upgrade", lambda app, qid: None)
    app_module._CAPTION_SEM.acquire()
    try:
        resp = client.get("/today")
    finally:
        app_module._CAPTION_SEM.release()
    assert resp.status_code == 200
    q = DailyQuestion.query.filter_by(couple_id=couple_user.couple_id).first()
    assert q is not None and q.source == "fallback"
    assert q.text in resp.get_data(as_text=True)


def test_question_slot_wait_fits_inside_the_gunicorn_timeout():
    """대기 + claude 최대 = 135s 로 gunicorn --timeout 180 안에 들어와야 한다."""
    assert app_module._QUESTION_SLOT_WAIT + ai.CLAUDE_TIMEOUT < 180


# --------------------------------------------------------------------------- #
# 3. 기능을 깎지 않는다 — 폴백 질문은 나중에 개인화 질문으로 올라간다
# --------------------------------------------------------------------------- #
def _fallback_question(couple_user, text=None):
    q = DailyQuestion(couple_id=couple_user.couple_id, q_date=date.today(),
                      text=text or ai.FALLBACK_QUESTIONS[0], source="fallback")
    db.session.add(q)
    db.session.commit()
    return q


def test_fallback_question_is_upgraded_later(flask_app, couple_user, monkeypatch):
    q = _fallback_question(couple_user)
    seen = {}

    def _gen(recent_pairs, past_questions=None):
        seen["held"] = _slot_was_held()
        return "나중에 올라온 개인화 질문", "ai"

    monkeypatch.setattr(ai, "generate_daily_question", _gen)
    app_module.upgrade_daily_question(flask_app, q.id)
    assert seen.get("held") is True, "업그레이드도 슬롯을 쥐고 돌아야 한다"
    # 워커는 자기 app_context(= 별도 세션)에서 커밋한다 — 바깥 세션을 만료시키고 다시 읽는다.
    db.session.expire_all()
    fresh = db.session.get(DailyQuestion, q.id)
    assert fresh.text == "나중에 올라온 개인화 질문"
    assert fresh.source == "ai"


def test_an_answered_question_is_never_swapped(flask_app, couple_user, monkeypatch):
    """누가 이미 답했으면 그가 본 질문이 손에서 바뀌면 안 된다."""
    from models import Answer

    q = _fallback_question(couple_user)
    db.session.add(Answer(question_id=q.id, user_id=couple_user.id, text="내 답"))
    db.session.commit()
    monkeypatch.setattr(ai, "generate_daily_question",
                        lambda *a, **k: ("바뀌면 안 되는 질문", "ai"))
    app_module.upgrade_daily_question(flask_app, q.id)
    assert q.text == ai.FALLBACK_QUESTIONS[0]
    assert q.source == "fallback"


def test_upgrade_leaves_an_ai_question_alone(flask_app, couple_user, monkeypatch):
    q = DailyQuestion(couple_id=couple_user.couple_id, q_date=date.today(),
                      text="이미 개인화된 질문", source="ai")
    db.session.add(q)
    db.session.commit()
    called = {"n": 0}
    monkeypatch.setattr(
        ai, "generate_daily_question",
        lambda *a, **k: (called.__setitem__("n", called["n"] + 1), ("x", "ai"))[1])
    app_module.upgrade_daily_question(flask_app, q.id)
    assert called["n"] == 0
    assert q.text == "이미 개인화된 질문"


def test_upgrade_keeps_the_fallback_when_claude_fails_again(
        flask_app, couple_user, monkeypatch):
    q = _fallback_question(couple_user)
    monkeypatch.setattr(ai, "generate_daily_question",
                        lambda *a, **k: ai.fallback_daily_question([]))
    app_module.upgrade_daily_question(flask_app, q.id)
    assert q.source == "fallback"
    assert app_module._CAPTION_SEM.acquire(blocking=False), "슬롯이 샜다"
    app_module._CAPTION_SEM.release()


def test_upgrade_never_raises_and_frees_its_guard(flask_app, couple_user,
                                                  monkeypatch):
    q = _fallback_question(couple_user)

    def _boom(*a, **k):
        raise RuntimeError("claude exploded")

    monkeypatch.setattr(ai, "generate_daily_question", _boom)
    app_module._question_upgrades.add(q.id)
    app_module.upgrade_daily_question(flask_app, q.id)  # raise 하면 테스트 실패
    assert q.id not in app_module._question_upgrades
    assert app_module._CAPTION_SEM.acquire(blocking=False), "슬롯이 샜다"
    app_module._CAPTION_SEM.release()


# --------------------------------------------------------------------------- #
# 4. 동시성 그 자체 — 두 워커가 겹쳐도 claude 는 한 번에 하나
# --------------------------------------------------------------------------- #
def test_two_workers_never_overlap_inside_claude(flask_app, partnered,
                                                 couple_user, monkeypatch):
    """월간 회고와 판결을 동시에 돌려도 claude 구간은 겹치지 않는다."""
    inside = []
    peak = {"n": 0, "max": 0}
    lock = threading.Lock()

    def _enter():
        with lock:
            peak["n"] += 1
            peak["max"] = max(peak["max"], peak["n"])
        inside.append(1)
        import time as _t
        _t.sleep(0.15)  # claude 가 도는 동안
        with lock:
            peak["n"] -= 1

    monkeypatch.setattr(ai, "generate_monthly_report",
                        lambda *a, **k: (_enter(), None)[1])
    monkeypatch.setattr(ai, "judge_fight", lambda *a, **k: (_enter(), None)[1])

    case = Case(couple_id=couple_user.couple_id, created_by=couple_user.id,
                title="t", situation="s", status="judging")
    db.session.add(case)
    db.session.flush()
    db.session.add(CaseStatement(case_id=case.id, user_id=couple_user.id, text="진술"))
    db.session.commit()
    cid, couple_id = case.id, couple_user.couple_id

    ts = [
        threading.Thread(target=app_module.regenerate_monthly_report,
                         args=(flask_app, couple_id, 2026, 10)),
        threading.Thread(target=app_module.judge_case, args=(flask_app, cid)),
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)

    assert len(inside) == 2, "두 워커가 다 돌지 않았다"
    assert peak["max"] == 1, (
        f"claude 가 동시에 {peak['max']}개 떴다 — 512MB 티어에서 OOM 이다"
    )
