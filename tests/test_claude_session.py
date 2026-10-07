"""지속 세션 계약 — **파이프라인당 프로세스 하나**, 깨지면 조용히 one-shot.

왜(실측 포함)는 ``ai.py`` 의 '지속 세션' 머리말이 정본. 여기서 못 박는 계약:

  1. 세션 argv 는 one-shot 과 **같은 ``--allowedTools``** 를 쓴다. Render 는 root 로
     도는데 Claude Code 가 root 에서 ``--dangerously-skip-permissions`` 를 거부해
     모든 콜이 깨지므로, 그 플래그가 다시 들어오면 여기서 막힌다.
  2. 세션 블록 안에서는 ``_run_claude`` 가 프로세스를 **새로 띄우지 않는다**.
  3. 세션이 깨지면 **그 호출부터 one-shot** 으로 되돌아간다 — 초안은 반드시 나온다.
  4. 후기 파이프라인은 세션을 **한 번** 열고 ``_CAPTION_SEM`` 을 **한 번만** 쥔다
     (같은 스레드가 Semaphore(1) 을 두 번 잡으면 영원히 멈춘다).
"""
import json

import pytest

import ai
import app as app_module
from models import db


@pytest.fixture(autouse=True)
def _no_session_leaks():
    ai._session_tls.session = None
    yield
    ai._session_tls.session = None


@pytest.fixture(autouse=True)
def _free_slot():
    while app_module._CAPTION_SEM.acquire(blocking=False):
        pass
    app_module._CAPTION_SEM.release()
    yield
    while app_module._CAPTION_SEM.acquire(blocking=False):
        pass
    app_module._CAPTION_SEM.release()


class FakeSession:
    """프로세스를 띄우지 않는 세션 대역."""

    def __init__(self, allow_web=False, fail_at=None, **kw):
        self.allow_web = allow_web
        self.asked = []
        self.closed = False
        self.fail_at = fail_at
        self.dead = False

    def start(self):
        return self

    def healthy(self):
        return not self.dead

    def ask(self, prompt, timeout=None):
        self.asked.append(prompt)
        if self.fail_at is not None and len(self.asked) >= self.fail_at:
            self.dead = True
            raise ai.SessionError("세션이 깨졌다")
        return json.dumps({"ok": len(self.asked)})

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------- #
# 1. argv — 권한 플래그 계약
# --------------------------------------------------------------------------- #
def test_the_session_uses_the_same_allowed_tools_as_a_one_shot():
    argv = ai.ClaudeSession().argv()
    assert argv[:3] == ["claude", "-p", "--allowedTools"]
    for tool in ("Read", "Glob", "Grep"):
        assert tool in argv, f"{tool} 권한이 빠졌다 — 비전 콜이 거부된다"


def test_the_session_opens_a_bidirectional_stream():
    argv = ai.ClaudeSession().argv()
    for flag in ("--input-format", "stream-json", "--output-format", "--verbose"):
        assert flag in argv, f"{flag} 가 없으면 턴을 이어 붙일 수 없다"


def test_the_session_never_uses_the_root_hostile_flag():
    argv = ai.ClaudeSession().argv()
    assert "--dangerously-skip-permissions" not in argv, (
        "Render 는 root 로 돈다 — 이 플래그가 들어오면 모든 claude 콜이 깨진다"
    )
    assert "--permission-mode" not in argv


def test_web_tools_are_opt_in():
    assert "WebFetch" not in ai.ClaudeSession().argv()
    assert "WebFetch" in ai.ClaudeSession(allow_web=True).argv()


def test_a_user_turn_is_one_stream_json_line():
    line = ai._encode_user_message("안녕")
    assert line.endswith("\n") and line.count("\n") == 1
    ev = json.loads(line)
    assert ev["type"] == "user"
    assert ev["message"]["content"][0]["text"] == "안녕"


def test_session_id_is_extracted_defensively():
    assert ai._extract_session_id({"session_id": "a"}) == "a"
    assert ai._extract_session_id({"result": {"sessionId": "b"}}) == "b"
    assert ai._extract_session_id({"type": "system"}) is None


def test_stream_events_tolerate_noise():
    assert ai._parse_stream_event("") is None
    assert ai._parse_stream_event("not json") is None
    assert ai._parse_stream_event('{"type":"result"}') == {"type": "result"}


# --------------------------------------------------------------------------- #
# 2. 블록 안에서는 프로세스를 새로 띄우지 않는다
# --------------------------------------------------------------------------- #
def test_calls_inside_a_session_reuse_one_process(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(ai, "ClaudeSession", lambda **kw: fake)
    spawned = []
    monkeypatch.setattr(ai, "_run_claude_oneshot",
                        lambda *a, **k: spawned.append(a) or "{}")
    with ai.claude_session():
        ai._run_claude("하나")
        ai._run_claude("둘")
        ai._run_claude("셋")
    assert fake.asked == ["하나", "둘", "셋"]
    assert spawned == [], "세션 안에서 프로세스를 새로 띄웠다 — 부팅이 또 든다"
    assert fake.closed, "블록을 나가며 세션을 안 거뒀다(400MB 가 남는다)"


def test_outside_a_session_every_call_is_a_one_shot(monkeypatch):
    spawned = []
    monkeypatch.setattr(ai, "_run_claude_oneshot",
                        lambda *a, **k: spawned.append(a[0]) or "{}")
    ai._run_claude("하나")
    ai._run_claude("둘")
    assert spawned == ["하나", "둘"]


def test_a_broken_session_falls_back_to_one_shot(monkeypatch):
    """세션이 어떤 이유로든 깨지면 **글은 그래도 나와야 한다.**"""
    fake = FakeSession(fail_at=2)
    monkeypatch.setattr(ai, "ClaudeSession", lambda **kw: fake)
    spawned = []
    monkeypatch.setattr(ai, "_run_claude_oneshot",
                        lambda *a, **k: spawned.append(a[0]) or "fallback")
    with ai.claude_session():
        assert ai._run_claude("하나") != "fallback"
        assert ai._run_claude("둘") == "fallback", "세션이 깨졌는데 폴백을 안 했다"
        assert ai._run_claude("셋") == "fallback", "그 뒤 호출도 폴백이어야 한다"
    assert spawned == ["둘", "셋"]


def test_a_session_that_cannot_start_is_not_fatal(monkeypatch):
    def boom(**kw):
        raise ai.SessionError("못 띄웠다")
    monkeypatch.setattr(ai, "ClaudeSession", boom)
    monkeypatch.setattr(ai, "_run_claude_oneshot", lambda *a, **k: "fallback")
    with ai.claude_session() as sess:
        assert sess is None
        assert ai._run_claude("하나") == "fallback"


def test_the_session_can_be_switched_off(monkeypatch):
    made = []
    monkeypatch.setattr(ai, "ClaudeSession",
                        lambda **kw: made.append(kw) or FakeSession())
    monkeypatch.setattr(ai, "_run_claude_oneshot", lambda *a, **k: "one-shot")
    with ai.claude_session(enabled=False):
        assert ai._run_claude("하나") == "one-shot"
    assert made == [], "CLAUDE_SESSION 을 꺼도 세션을 띄웠다"


def test_a_web_enabled_call_does_not_ride_a_text_only_session(monkeypatch):
    fake = FakeSession(allow_web=False)
    monkeypatch.setattr(ai, "ClaudeSession", lambda **kw: fake)
    monkeypatch.setattr(ai, "_run_claude_oneshot", lambda *a, **k: "one-shot")
    with ai.claude_session():
        assert ai._run_claude("웹", allow_web=True) == "one-shot"
    assert fake.asked == [], "웹 권한이 없는 세션에 웹 콜을 태웠다"


# --------------------------------------------------------------------------- #
# 3. 후기 파이프라인 — 세션 1개 · 세마포어 1회
# --------------------------------------------------------------------------- #
def test_the_review_pipeline_opens_exactly_one_session(
        flask_app, make_review, monkeypatch):
    opened = []

    def _fake_session(**kw):
        s = FakeSession(**{k: v for k, v in kw.items() if k == "allow_web"})
        opened.append(s)
        return s

    monkeypatch.setattr(ai, "ClaudeSession", _fake_session)
    monkeypatch.setattr(ai, "write_review",
                        lambda *a, **k: {"title": "t", "blocks": []})
    monkeypatch.setattr(app_module.keyword_research, "research",
                        lambda *a, **k: {"status": "skipped"})
    monkeypatch.setattr(app_module, "_attach_section_crops",
                        lambda *a, **k: None)
    review = make_review(status="pending")
    app_module.generate_review(flask_app, review.id)
    assert len(opened) == 1, f"파이프라인이 세션을 {len(opened)}개 열었다"
    assert opened[0].closed, "파이프라인이 끝났는데 세션이 살아 있다"


def test_the_pipeline_holds_the_claude_slot_exactly_once(
        flask_app, make_review, monkeypatch):
    """세션이 사는 동안 다른 claude 가 끼어들면 512MB 가 터진다 — 문은 하나다.

    그리고 같은 스레드가 ``Semaphore(1)`` 을 두 번 잡으면 **영원히 멈춘다**. 크롭
    단계가 바깥에서 이미 쥔 문을 또 잡으려 하지 않는지 여기서 본다.
    """
    held = []
    monkeypatch.setattr(ai, "ClaudeSession", lambda **kw: FakeSession())
    monkeypatch.setattr(ai, "write_review",
                        lambda *a, **k: {"title": "t", "blocks": []})
    monkeypatch.setattr(app_module.keyword_research, "research",
                        lambda *a, **k: {"status": "skipped"})

    def _crops(result, photos, acquire_sem=True, on_step=None):
        held.append(acquire_sem)
        # 문이 잡혀 있어야 한다(= 바깥에서 쥐고 들어왔다).
        got = app_module._CAPTION_SEM.acquire(blocking=False)
        if got:
            app_module._CAPTION_SEM.release()
        held.append(("slot_free", got))

    monkeypatch.setattr(app_module, "_attach_section_crops", _crops)
    review = make_review(status="pending")
    app_module.generate_review(flask_app, review.id)
    assert held[0] is False, "크롭 단계가 세마포어를 또 잡으려 한다 — 데드락이다"
    assert held[1] == ("slot_free", False), "파이프라인이 문을 안 쥐고 돌았다"
    db.session.expire_all()   # 워커는 자기 세션에서 커밋했다 — 여기서 다시 읽는다
    assert db.session.get(type(review), review.id).status == "ready"
