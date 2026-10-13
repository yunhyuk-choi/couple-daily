"""행사 채점 재시도 — **같은 실패를 영원히 반복하지 않는다** (그러나 포기하지도 않는다).

## 무엇이 문제였나 (상시 낭비)

``score_events_for_couple`` 의 선택 쿼리는 ``EventScore.status != 'ready'`` 였다 —
``'failed'`` 도 다시 고른다는 뜻이다. 그래서 **항상 실패하는 행사 몇 건**이 있으면

* ``/dates`` 를 **열 때마다** 채점 잡이 서고 claude 가 한 번 더 돌고,
* 야간 ``prewarm_scores`` 는 매일 밤 그 몇 건을 다시 집어 또 claude 를 불렀다.

결과가 바뀔 이유가 하나도 없는데(**프롬프트가 글자 하나 다르지 않다**) 매일 같은
호출을 버린 것이다.

## 무엇으로 영구·일시를 가르나

**claude 가 답을 했느냐**다. 이 판정은 호출 결과 자체에 들어 있다:

* ``ai.score_events`` 가 **``None``** → 콜 자체가 실패했다(프로세스·파싱·취소).
  그 배치의 어떤 행사에 대해서도 **아무것도 배우지 못했다** → 전부 ``failed``
  (일시) → 다음 패스가 그대로 재시도한다.
* 리스트를 돌려줬는데 **이 ref 만 빠졌다** → 모델이 답은 했고 이 행사를 고르지
  않았다. 같은 프롬프트를 또 보내면 같은 답이 온다 → ``unscorable``(이 입력에
  한해 영구) → **입력이 바뀌기 전까지** 다시 고르지 않는다.

입력 = (커플 취향 프로필, 행사 내용) 둘뿐이고 둘 다 바뀌면 프롬프트가 바뀐다 —
그때는 결과가 달라질 수 있으니 **다시 시도한다.** 시계로도, 건수로도 끊지 않는다.
"""
import datetime as dt

import pytest

import ai
import app as app_module
from models import Couple, EventItem, EventScore, User, db


@pytest.fixture()
def paired_couple(flask_app):
    """승인 멤버 2명짜리 커플 — ``prewarm_scores`` 가 '활성'으로 보는 조건."""
    couple = Couple(invite_code="PREWARMC")
    db.session.add(couple)
    db.session.flush()
    for i in range(2):
        db.session.add(
            User(
                email=f"p{i}@example.com",
                password_hash="x",
                display_name=f"P{i}",
                couple_id=couple.id,
                status="approved",
            )
        )
    db.session.commit()
    return couple


def _make_events(n, prefix="행사"):
    today = dt.date.today()
    rows = []
    for i in range(n):
        ev = EventItem(
            source="seoul",
            source_uid=f"{prefix}-{i}",
            title=f"{prefix} {i}",
            end_date=today + dt.timedelta(days=30),
        )
        db.session.add(ev)
        rows.append(ev)
    db.session.commit()
    return rows


class FakeClaude:
    """``ai.score_events`` 가짜. 콜마다 넘어온 ref 를 기록한다.

    ``poison`` 에 든 제목은 **답에서 빠진다**(모델이 답은 했지만 그 행사는 안 골랐다).
    ``dead=True`` 면 콜 자체가 실패한 것처럼 ``None`` 을 돌려준다.
    """

    def __init__(self, poison=(), dead=False):
        self.calls = []
        self.poison = set(poison)
        self.dead = dead

    @property
    def n(self):
        return len(self.calls)

    def __call__(self, profile_text, events):
        self.calls.append([e["ref"] for e in events])
        if self.dead:
            return None
        return [
            {"ref": e["ref"], "score": 70, "reason": "둘이 가기 좋아"}
            for e in events
            if e["title"] not in self.poison
        ]


def _install(monkeypatch, fake, profile="프로필 v1"):
    monkeypatch.setattr(ai, "score_events", fake)
    monkeypatch.setattr(app_module.ai, "score_events", fake)
    monkeypatch.setattr(
        app_module, "build_couple_taste_profile", lambda couple: profile
    )


def _statuses():
    return sorted(
        (s.event.title, s.status)
        for s in EventScore.query.all()
    )


def test_nightly_calls_stop_when_the_model_keeps_skipping_the_same_events(
    flask_app, paired_couple, monkeypatch
):
    """⭐ 핵심 회귀 — 못 고르는 행사 3건이 있어도 **둘째 밤부터 claude 는 0 콜**이다.

    고치기 전에는 밤마다 1 콜씩(그리고 ``/dates`` 를 열 때마다 또 한 콜) 영원히
    버려졌다.
    """
    _make_events(7, "좋은행사")
    _make_events(3, "안되는행사")
    poison = [f"안되는행사 {i}" for i in range(3)]

    fake = FakeClaude(poison=poison)
    _install(monkeypatch, fake)

    app_module.prewarm_scores(flask_app)
    night1 = fake.n

    fake.calls.clear()
    app_module.prewarm_scores(flask_app)
    night2 = fake.n

    fake.calls.clear()
    app_module.prewarm_scores(flask_app)
    night3 = fake.n

    # 그리고 사람이 /dates 를 다섯 번 열어도(= 채점 워커 다섯 번) 한 번도 안 부른다.
    fake.calls.clear()
    for _ in range(5):
        app_module.score_events_for_couple(flask_app, paired_couple.id)
    views = fake.n

    # ASCII 로만 찍는다 — 윈도우 콘솔(cp949)에서 한글 print 가 터지면 **측정값이
    # 아니라 인코딩 에러**가 결과가 된다.
    print(f"\n[measured] unscorable=3 good=7 -> night1={night1} night2={night2} "
          f"night3={night3} page_views(5)={views} claude calls")

    assert night1 >= 1
    assert night2 == 0, "둘째 밤에 또 부르면 그게 그 낭비다"
    assert night3 == 0
    assert views == 0, "/dates 를 여는 것만으로 claude 가 돌면 안 된다"

    by_title = dict(_statuses())
    assert by_title["좋은행사 0"] == "ready"
    assert by_title["안되는행사 0"] == "unscorable"


def test_a_dead_call_is_transient_and_stays_retryable(
    flask_app, paired_couple, monkeypatch
):
    """콜 자체가 실패하면(답 없음) 그 배치는 **아무 판정도 받지 않는다** — 재시도된다."""
    _make_events(3, "행사")

    dead = FakeClaude(dead=True)
    _install(monkeypatch, dead)
    app_module.prewarm_scores(flask_app)
    assert dead.n >= 1
    assert {s for _, s in _statuses()} == {"failed"}

    # 다음 밤 claude 가 정상으로 돌아오면 전부 채점된다.
    alive = FakeClaude()
    _install(monkeypatch, alive)
    app_module.prewarm_scores(flask_app)
    assert alive.n >= 1
    assert {s for _, s in _statuses()} == {"ready"}


def test_unscorable_is_retried_when_the_couple_profile_changes(
    flask_app, paired_couple, monkeypatch
):
    """포기는 **이 입력에 한해서**다 — 프로필이 바뀌면 프롬프트가 바뀌니 다시 묻는다."""
    _make_events(2, "안되는행사")
    poison = [f"안되는행사 {i}" for i in range(2)]

    first = FakeClaude(poison=poison)
    _install(monkeypatch, first, profile="프로필 v1")
    app_module.prewarm_scores(flask_app)
    assert {s for _, s in _statuses()} == {"unscorable"}

    # 같은 프로필 → 다시 안 묻는다.
    again = FakeClaude(poison=poison)
    _install(monkeypatch, again, profile="프로필 v1")
    app_module.score_events_for_couple(flask_app, paired_couple.id)
    assert again.n == 0

    # 프로필이 자랐다(새 답변·캡션) → 다시 묻는다. 이번엔 모델이 고른다.
    grown = FakeClaude()
    _install(monkeypatch, grown, profile="프로필 v2 — 둘 다 전시를 좋아한다")
    app_module.score_events_for_couple(flask_app, paired_couple.id)
    assert grown.n == 1
    assert {s for _, s in _statuses()} == {"ready"}


def test_unscorable_is_retried_when_the_event_itself_changes(
    flask_app, paired_couple, monkeypatch
):
    """행사 내용이 갱신되면(제목·설명) 그것도 다른 프롬프트다 — 다시 묻는다."""
    rows = _make_events(1, "안되는행사")
    poison = ["안되는행사 0"]

    first = FakeClaude(poison=poison)
    _install(monkeypatch, first)
    app_module.prewarm_scores(flask_app)
    assert {s for _, s in _statuses()} == {"unscorable"}

    quiet = FakeClaude(poison=poison)
    _install(monkeypatch, quiet)
    app_module.score_events_for_couple(flask_app, paired_couple.id)
    assert quiet.n == 0

    # 야간 upsert 가 설명을 채웠다 — updated_at 이 밀린다.
    ev = db.session.get(EventItem, rows[0].id)
    ev.description = "사실은 아주 근사한 전시였다"
    db.session.commit()

    retried = FakeClaude()
    _install(monkeypatch, retried)
    app_module.score_events_for_couple(flask_app, paired_couple.id)
    assert retried.n == 1
    assert {s for _, s in _statuses()} == {"ready"}


def test_score_events_tells_a_dead_call_apart_from_an_empty_answer(monkeypatch):
    """``ai.score_events`` 는 '답을 못 받았다'(None)와 '답은 받았다'([])를 가른다."""
    monkeypatch.setattr(
        ai, "_run_claude", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("죽음"))
    )
    assert ai.score_events("프로필", [{"ref": 1, "title": "행사"}]) is None

    monkeypatch.setattr(ai, "_run_claude", lambda *a, **k: '{"scores": []}')
    assert ai.score_events("프로필", [{"ref": 1, "title": "행사"}]) == []
