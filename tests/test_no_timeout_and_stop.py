"""**느리다고 죽이지 않는다** — 경과 시간 상한 대신 '사람의 중단'과 '침묵 감지'.

왜(실측 포함)는 ``ai.py`` 머리말이 정본. 짧게: 본문 생성 한 콜의 실측이 **73.7초**
인데 상한이 120초였고, Render(0.1 CPU)는 그보다 느리다 — 즉 우리가 **일하고 있는**
claude 를 끊어 후기를 ``failed`` 로 만들고 있었다.

여기서 못 박는 계약:

  1. **오래 걸려도 안 끊는다** — 이벤트가 흐르는 한 상한 없이 기다린다.
  2. **침묵이면 끊는다** — 이벤트가 하나도 없는 침묵은 느린 게 아니라 고장이다.
     (1)과 (2)를 가르는 것은 **경과 시간이 아니라 침묵 시간**이다.
  3. **사람이 멈출 수 있다** — 중단은 claude 프로세스 · 큐 행 · 인프로세스 가드 ·
     후기 상태 **넷 다** 치운다. 하나라도 빠지면 '멈췄다'는 말이 거짓이 된다.
  4. 상한이 **남아 있는 자리**는 성질이 다르다(요청 경로의 오늘의 질문 = gunicorn
     응답 예산). 그 자리는 그대로 있어야 한다.
"""
import inspect
import subprocess
import threading
import time

import pytest

import ai
import aijobs
import app as app_module
from models import AiJob, db


class FakeProc:
    """프로세스를 안 띄우는 대역 — 죽였는지만 기록한다."""

    def __init__(self):
        self.killed = False
        self.returncode = None
        self.stdin = _NullStdin()

    def poll(self):
        return 0 if self.killed else None

    def terminate(self):
        self.killed = True
        self.returncode = -15

    def wait(self, timeout=None):
        # 아직 안 죽였으면 **안 죽는다** — stdin 을 닫아도 안 끝나는 '행' 상태를
        # 흉내 낸다(침묵 감지가 상대해야 하는 바로 그 프로세스다).
        if not self.killed:
            raise subprocess.TimeoutExpired("claude", timeout or 0)
        return self.returncode


class _NullStdin:
    def write(self, *_a):
        return None

    def flush(self):
        return None

    def close(self):
        return None


@pytest.fixture(autouse=True)
def _no_cancel_leaks():
    ai._scope_tls.key = None
    with ai._cancel_lock:
        ai._cancel_keys.clear()
        ai._cancel_procs.clear()
    yield
    ai._scope_tls.key = None
    with ai._cancel_lock:
        ai._cancel_keys.clear()
        ai._cancel_procs.clear()


def _session(idle_sec):
    """프로세스를 안 띄운 세션(읽기 루프는 테스트가 손으로 민다)."""
    sess = ai.ClaudeSession(idle_sec=idle_sec)
    sess.proc = FakeProc()
    sess.started_at = sess.last_event_at = time.time()
    return sess


# --------------------------------------------------------------------------- #
# 1. 느림 vs 침묵 — 이 둘을 가르는 것이 이번 변경의 전부다
# --------------------------------------------------------------------------- #
def test_a_slow_turn_is_not_killed_even_past_the_old_limit():
    """**경과 시간이 상한을 한참 넘겨도** 이벤트가 흐르면 끝까지 기다린다.

    침묵 상한(0.4초)보다 **7배 넘게 오래 걸리는** 턴이다. 예전처럼 경과 시간으로
    끊었다면 여기서 죽었을 것이고, 실제로 그게 73.7초짜리 본문 생성을 120초 상한
    아래에서 죽이던 구조였다.
    """
    sess = _session(idle_sec=0.4)

    def feed():
        end = time.time() + 3.0
        while time.time() < end:
            sess.last_event_at = time.time()   # 이벤트가 계속 흐른다
            time.sleep(0.1)
        with sess._cv:
            sess._results.append({"type": "result", "result": "늦었지만 나왔어"})
            sess._cv.notify_all()

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    started = time.time()
    out = sess.ask("아주 긴 본문을 써줘")
    t.join(timeout=5)
    assert out == "늦었지만 나왔어"
    assert time.time() - started > sess.idle_sec * 3, (
        "테스트가 침묵 상한보다 짧게 끝났다 — '오래 걸림'을 재현하지 못했다"
    )
    assert sess.proc is not None and not sess.proc.killed, (
        "느리다는 이유로 claude 를 죽였다"
    )


def test_a_silent_turn_is_cut_because_silence_is_not_slowness():
    """이벤트가 **하나도** 없으면 그건 느린 게 아니라 고장이다 — 그때만 끊는다."""
    sess = _session(idle_sec=0.5)
    proc = sess.proc
    with pytest.raises(ai.SessionError) as e:
        sess.ask("대답이 영영 안 오는 프롬프트")
    assert "silent" in str(e.value), f"침묵이라고 말하지 않았다: {e.value}"
    assert proc.killed, "침묵인데도 프로세스를 안 거뒀다 — 펌프가 영원히 막힌다"
    assert sess.proc is None and sess.dead


def test_health_no_longer_expires_a_long_lived_session():
    """세션 '수명' 상한은 없앴다 — 오래 산 세션도 이벤트가 최근이면 건강하다."""
    sess = _session(idle_sec=600)
    sess.started_at = time.time() - 10 * 3600   # 10시간째 살아 있다
    sess.last_event_at = time.time()
    assert sess.healthy(), "오래 살았다는 이유로 세션을 버렸다"
    assert not hasattr(ai, "SESSION_MAX_SEC"), (
        "경과 시간 기반 수명 상한이 되살아났다"
    )


# --------------------------------------------------------------------------- #
# 2. 기본값 — 백그라운드 콜에는 상한이 없고, 요청 경로에만 남는다
# --------------------------------------------------------------------------- #
def test_background_calls_have_no_elapsed_cap_by_default():
    for fn in (ai._run_claude, ai._run_claude_oneshot):
        assert inspect.signature(fn).parameters["timeout"].default is None, (
            f"{fn.__name__} 가 아직 기본 상한을 들고 있다"
        )
    assert inspect.signature(ai.ClaudeSession.ask).parameters["timeout"].default is None


def test_the_review_body_call_passes_no_timeout(monkeypatch):
    """본문 생성(write_review)은 상한 없이 부른다 — 이번 변경의 핵심 경로."""
    seen = {}

    def _fake(prompt, timeout=None, allow_web=False):
        seen["timeout"] = timeout
        return '{"title":"t","blocks":[]}'

    monkeypatch.setattr(ai, "_run_claude", _fake)
    ai.write_review("문래갈매기", "문래동", "맛있었어", 8, [])
    assert seen["timeout"] is None, "본문 생성에 아직 시간 상한이 걸려 있다"


def test_the_request_path_question_keeps_its_hard_budget(monkeypatch):
    """'오늘의 질문'만 성질이 다르다 — gunicorn 응답 예산이라 상한이 **남아야** 한다."""
    seen = {}

    def _fake(prompt, timeout=None, allow_web=False):
        seen["timeout"] = timeout
        return '{"question": "오늘 뭐가 제일 좋았어?"}'

    monkeypatch.setattr(ai, "_run_claude", _fake)
    ai.generate_daily_question([])
    assert seen["timeout"] == ai.CLAUDE_TIMEOUT, (
        "요청 경로 상한까지 걷어내면 느린 claude 가 gunicorn 워커를 통째로 죽인다"
    )


# --------------------------------------------------------------------------- #
# 3. 중단 — 플래그가 아니라 **실제로** 멈춘다
# --------------------------------------------------------------------------- #
def test_cancel_kills_the_live_claude_process():
    proc = FakeProc()
    with ai.cancel_scope("review:7"):
        ai._register_proc(proc)
        assert ai.request_cancel("review:7") == 1
        assert proc.killed, "중단을 눌렀는데 claude 가 계속 돈다"
        assert ai.is_cancelled()


def test_a_cancelled_call_does_not_quietly_fall_back_to_a_one_shot(monkeypatch):
    """중단은 '이 길이 막혔다'가 아니라 '그만하라'다 — 다른 길로 가면 안 된다."""
    monkeypatch.setattr(
        ai, "_run_claude_oneshot",
        lambda *a, **k: pytest.fail("중단했는데 claude 를 또 띄웠다"),
    )
    with ai.cancel_scope("review:7"):
        ai.request_cancel("review:7")
        with pytest.raises(ai.Cancelled):
            ai._run_claude("뭐든 써줘")


def test_leaving_the_scope_forgets_the_cancellation():
    """↻ 로 다시 돌린 생성이 지난 중단을 물려받으면 안 된다."""
    with ai.cancel_scope("review:7"):
        ai.request_cancel("review:7")
        assert ai.is_cancelled("review:7")
    assert not ai.is_cancelled("review:7")


# --------------------------------------------------------------------------- #
# 4. 중단 버튼 — 화면·큐·프로세스·상태가 **같은 뜻**이 된다
# --------------------------------------------------------------------------- #
@pytest.fixture()
def _clean_queue(flask_app):
    AiJob.query.delete()
    db.session.commit()
    yield
    AiJob.query.delete()
    db.session.commit()


@pytest.fixture()
def spawned(monkeypatch):
    """스폰 호출을 기록만 하는 가짜 — 테스트에서 claude 는 절대 돌지 않는다."""
    calls = {"review": []}
    monkeypatch.setattr(app_module, "_spawn_generate_review",
                        lambda app, rid: calls["review"].append(rid))
    app_module._generating_reviews.clear()
    app_module._refailed_reviews.clear()
    yield calls
    app_module._generating_reviews.clear()
    app_module._refailed_reviews.clear()


def test_the_waiting_screen_always_offers_a_way_to_stop(client, make_review):
    review = make_review(status="pending")
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert f"/reviews/{review.id}/cancel" in html, "멈출 길이 화면에 없다"
    assert "그만할래" in html
    assert "내가 끊지는 않아" in html, "안 끊는다는 사실을 말해 주지 않는다"


def test_pressing_stop_cleans_up_process_queue_guard_and_status(
        client, make_review, _clean_queue, monkeypatch):
    """누르면 **넷 다** 치운다 — 하나라도 남으면 '멈췄다'가 거짓말이 된다."""
    monkeypatch.setitem(aijobs._handlers, "review", lambda a, p: None)
    review = make_review(status="pending")
    key = f"review:{review.id}"
    aijobs.enqueue("review", key, {"review_id": review.id})
    app_module._generating_reviews.add(review.id)
    proc = FakeProc()
    ai.enter_cancel_scope(key)       # 워커가 돌고 있는 상태를 흉내 낸다
    try:
        ai._register_proc(proc)
        resp = client.post(f"/reviews/{review.id}/cancel")
    finally:
        ai.exit_cancel_scope(key)
    assert resp.status_code == 302
    assert proc.killed, "① claude 프로세스가 계속 돈다"
    assert aijobs.pending(key) is False, "② 큐 행이 남았다 — 펌프가 다시 집는다"
    assert review.id not in app_module._generating_reviews, (
        "③ 인프로세스 가드가 남았다 — 나중에 ↻ 가 조용히 no-op 이 된다"
    )
    db.session.refresh(review)
    assert review.status == "cancelled", "④ 상태가 'pending' 그대로다"


def test_a_cancelled_draft_is_not_auto_resurrected(client, make_review, spawned):
    """'failed' 와 달리 **아무도 혼자 되살리지 않는다** — 사람이 멈춘 것이니까."""
    review = make_review(status="cancelled")
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert spawned["review"] == [], "사람이 멈춘 초안을 멋대로 다시 돌렸다"
    db.session.refresh(review)
    assert review.status == "cancelled"
    assert "멈췄어" in html
    assert f"/reviews/{review.id}/regenerate" in html, "다시 만들 길은 있어야 한다"


def test_stopping_keeps_a_draft_that_is_already_written(
        client, make_review, _clean_queue, monkeypatch):
    """크롭만 남은 단계에서 멈춰도 **이미 쓴 글은 안 버린다.**"""
    monkeypatch.setitem(aijobs._handlers, "review", lambda a, p: None)
    review = make_review(status="ready",
                         ai_json='{"title":"t","blocks":[]}')
    key = f"review:{review.id}"
    aijobs.enqueue("review", key, {"review_id": review.id})
    client.post(f"/reviews/{review.id}/cancel")
    db.session.refresh(review)
    assert review.status == "ready", "사람이 멈췄다고 다 쓴 초안을 버렸다"
    assert aijobs.pending(key) is False


# --------------------------------------------------------------------------- #
# 5. 경고는 경고일 뿐 — 25분이 지나도 아무것도 죽지 않는다
# --------------------------------------------------------------------------- #
def test_the_overdue_banner_warns_without_claiming_death(make_review, flask_app):
    from datetime import datetime, timedelta

    review = make_review(status="pending")
    app_module._generating_reviews.add(review.id)
    review.updated_at = datetime.utcnow() - timedelta(minutes=60)
    db.session.commit()
    try:
        view = app_module.review_progress_view(flask_app, review)
    finally:
        app_module._generating_reviews.discard(review.id)
    assert view["state"] == "overdue"
    assert "중단된 것 같아" not in view["headline"], (
        "아직 돌고 있을 수 있는데 죽었다고 단정했다"
    )
    assert view["can_stop"] is True, "경고만 하고 멈출 길을 안 줬다"
