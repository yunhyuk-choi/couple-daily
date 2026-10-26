"""2026-10-26 운영 중단 회귀 — 빈 큐의 펌프가 2초마다 DB 를 때려 Neon 무료 컴퓨트 한도를 소진했다.

지키려는 계약:
  * **일이 없으면 펌프는 DB 쿼리를 내지 않는다**(Neon 은 쿼리 뒤 5분 깨어 있다).
  * 일이 들어오면 **즉시** 집어간다(이벤트) — 폴링 시절의 2초보다 느려지면 안 된다.
  * 폴백(주기 복구)은 남아 있다.
  * 화면의 '멎었다' 판정은 긴 유휴를 멎음으로 오판하지 않고, 진짜 멎음은 여전히 잡는다.
"""
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

import aijobs
import app as app_module
from models import AiJob, db


@pytest.fixture(autouse=True)
def _clean(flask_app):
    aijobs.stop_pump()
    AiJob.query.delete()
    db.session.commit()
    yield
    aijobs.stop_pump()
    AiJob.query.delete()
    db.session.commit()


class _Counter:
    def __init__(self, engine):
        self.n = 0
        self.engine = engine
        event.listen(engine, "before_cursor_execute", self._hit)

    def _hit(self, *a, **k):
        self.n += 1

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self._hit)


def _wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_idle_pump_issues_no_queries(flask_app):
    aijobs.start_pump(flask_app)
    assert _wait_for(lambda: aijobs._last_tick is not None)
    time.sleep(0.5)  # 부팅 복구·첫 claim 이 끝나 잠들 때까지
    c = _Counter(db.engine)
    try:
        time.sleep(3.0)  # 예전 POLL_SEC=2 였다면 여기서 쿼리가 나갔다
    finally:
        c.close()
    assert c.n == 0, f"빈 큐인데 DB 쿼리 {c.n}회"


def test_enqueue_wakes_the_pump_immediately(flask_app, monkeypatch):
    started = {}
    done = threading.Event()

    def handler(app, payload):
        started["t"] = time.monotonic()
        done.set()

    monkeypatch.setitem(aijobs._handlers, "review", handler)
    aijobs.start_pump(flask_app)
    assert _wait_for(lambda: aijobs._last_tick is not None)
    time.sleep(0.5)  # 잠든 뒤
    t0 = time.monotonic()
    assert aijobs.enqueue("review", "review:1", {"review_id": 1}) is True
    assert done.wait(2.0), "이벤트로 안 깼다 — 폴백(45분)까지 기다리게 된다"
    assert started["t"] - t0 < 1.0, f"집어가기까지 {started['t'] - t0:.3f}s"


def test_no_job_is_lost_to_the_clear_vs_enqueue_race(flask_app, monkeypatch):
    seen = []
    monkeypatch.setitem(aijobs._handlers, "review", lambda a, p: seen.append(p["i"]))
    aijobs.start_pump(flask_app)
    assert _wait_for(lambda: aijobs._last_tick is not None)
    for i in range(30):
        aijobs.enqueue("review", f"review:{i}", {"i": i})
        time.sleep(0.003 * (i % 3))  # 펌프가 비우고 잠드는 순간과 엇갈리게
    assert _wait_for(lambda: len(seen) == 30, timeout=10), f"처리 {len(seen)}/30"


def test_fallback_still_polls_and_recovers(flask_app, monkeypatch):
    """신호가 없어도 폴백 주기마다 복구가 돈다(다른 프로세스가 남긴 죽은 running 행)."""
    monkeypatch.setattr(aijobs, "FALLBACK_POLL_SEC", 0.2)
    calls = []
    real = aijobs.recover
    monkeypatch.setattr(aijobs, "recover", lambda: calls.append(1) or real())
    db.session.add(AiJob(kind="review", job_key="review:dead", status="running",
                         owner="other-proc", attempts=1,
                         updated_at=datetime.utcnow()))
    db.session.commit()
    monkeypatch.setitem(aijobs._handlers, "review", lambda a, p: None)
    aijobs.start_pump(flask_app)
    assert _wait_for(lambda: len(calls) >= 3, timeout=3), "폴백 복구가 안 돈다"
    assert _wait_for(lambda: AiJob.query.filter_by(job_key="review:dead").first() is None)


def test_stop_pump_returns_promptly_while_asleep(flask_app):
    aijobs.start_pump(flask_app)
    assert _wait_for(lambda: aijobs._last_tick is not None)
    time.sleep(0.3)
    t0 = time.monotonic()
    aijobs.stop_pump(timeout=5)
    assert time.monotonic() - t0 < 2.0


# --------------------------------------------------------------------------- #
# 화면 판정 — 긴 유휴 ≠ 멎음
# --------------------------------------------------------------------------- #
def _snap(age_sec):
    return {"created_at": datetime.utcnow() - timedelta(seconds=age_sec)}


def _pump(silent, key=None, alive=True):
    return {"alive": alive, "silent_sec": silent, "current_key": key}


def test_idle_pump_asleep_for_30min_is_not_stalled_when_it_just_woke():
    # 일이 5초 전에 들어왔고 펌프는 그 직후(4.9초 전) 틱을 찍었다 — 정상.
    assert app_module._pump_looks_stalled(_snap(5), _pump(4.9), datetime.utcnow()) is False


def test_idle_sleep_alone_is_not_stalled_signal():
    # 30분째 조용하지만 대기 중인 일이 막 들어왔다(아직 깨울 시간 이내).
    assert app_module._pump_looks_stalled(_snap(3), _pump(1800.0), datetime.utcnow()) is False


def test_wake_that_never_arrived_is_stalled():
    # 일이 60초 전 들어왔는데 마지막 틱은 30분 전 — 이벤트가 안 닿았다.
    assert app_module._pump_looks_stalled(_snap(60), _pump(1800.0), datetime.utcnow()) is True


def test_busy_pump_is_not_stalled_by_silence_signal(flask_app):
    # 한 잡을 20분째 쥔 펌프 — 침묵 신호로는 멎음이 아니다. DB 신호도 running 행이 있어 아니다.
    db.session.add(AiJob(kind="caption", job_key="caption:1", status="running"))
    db.session.commit()
    assert app_module._pump_looks_stalled(
        _snap(600), _pump(1200.0, key="caption:1"), datetime.utcnow()) is False


def test_dead_pump_thread_is_stalled():
    assert app_module._pump_looks_stalled(_snap(1), _pump(None, alive=False),
                                          datetime.utcnow()) is True


# --------------------------------------------------------------------------- #
# 부트스트랩 재시도 상한
# --------------------------------------------------------------------------- #
def test_bootstrap_backoff_grows_then_caps_at_minutes():
    got = [app_module._db_boot_delay(i) for i in range(10)]
    assert got[:7] == [5, 10, 20, 40, 80, 160, 300]
    assert set(got[6:]) == {300}
    assert app_module._DB_BOOT_RETRY_MAX_SEC >= 180  # 분 단위 상한(예전 60초)
