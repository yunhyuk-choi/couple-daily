"""2026-10 운영 사고 회귀 — DB 가 흔들리면 앱이 통째로 죽던 구조.

사고: 코드 변경 없이 DB 만 응답하지 않았는데 앱이 **무응답**이었다. ``create_app()`` 이
import 시점에 ``create_all`` → 마이그레이션 → 시드를 돌렸고, 라우트 등록이 그 **뒤**라
``/healthz`` 조차 없었다. 거기에 ``connect_timeout`` 이 없어 죽은 DB 앞에서 무한 대기였다.

지키려는 계약:
  * **DB 가 완전히 죽어 있어도 앱은 뜨고 ``/healthz`` 는 200 + ``status: ok`` 다.**
  * ``/readyz`` 는 DB 왕복·마이그레이션 상태를 말하고, 아니면 503 이다.
  * 마이그레이션 실패는 **삼키지 않고** 반환값·``/readyz`` 에 드러난다.
  * 스키마가 준비되기 전의 일반 요청은 매달리지 않고 503 이다(헬스 경로는 예외).
"""
import json
import os
import subprocess
import sys
import textwrap

import app as app_module
import models
from models import db

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# 1. 핵심 불변식 — 죽은 DB 에서 실제로 부팅한다(새 프로세스: import 시점이 곧 검증 대상)
# --------------------------------------------------------------------------- #
def test_app_boots_and_serves_healthz_with_dead_db():
    # 닫힌 포트 = 즉시 거절. 가짜 자격증명뿐이다(어디에도 접속하지 않는다).
    code = textwrap.dedent(
        """
        import json
        import app as m
        c = m.app.test_client()
        h = c.get("/healthz")
        r = c.get("/readyz")
        p = c.get("/")
        print(json.dumps({"h": [h.status_code, h.get_json()["status"]],
                          "r": [r.status_code, r.get_json()],
                          "root": p.status_code}))
        """
    )
    env = dict(os.environ, DATABASE_URL="postgresql://u:p@127.0.0.1:1/db",
               SECRET_KEY="t", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTEST_CURRENT_TEST", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["h"] == [200, "ok"]
    assert got["r"][0] == 503
    assert got["r"][1]["status"] == "unavailable"
    assert got["r"][1]["db"]["ok"] is False
    assert got["root"] == 503  # 일반 요청은 매달리지 않고 바로 503


# --------------------------------------------------------------------------- #
# 2. /readyz
# --------------------------------------------------------------------------- #
def test_readyz_ok_on_healthy_db(flask_app):
    resp = flask_app.test_client().get("/readyz")
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["status"] == "ok"
    assert body["db"]["ok"] is True
    assert body["migrations"]["status"] == "ok"
    assert body["migrations"]["failed_steps"] == 0


def test_readyz_503_when_migrations_failed(flask_app, monkeypatch):
    """마이그레이션 일부 실패는 앱을 살려 두되(서빙) /readyz 로는 **보인다**."""
    monkeypatch.setitem(app_module._db_boot, "status", "degraded")
    monkeypatch.setitem(app_module._db_boot, "failures", ["x", "y"])
    resp = flask_app.test_client().get("/readyz")
    assert resp.status_code == 503
    assert resp.get_json()["migrations"] == {
        "status": "degraded", "attempts": app_module._db_boot["attempts"],
        "failed_steps": 2, "error": app_module._db_boot["error"],
    }
    # 서빙은 한다 — degraded 는 영업 중지가 아니다.
    assert flask_app.test_client().get("/healthz").status_code == 200


def test_readyz_503_when_db_probe_fails(flask_app, monkeypatch):
    monkeypatch.setattr(app_module, "_probe_db",
                        lambda app: {"ok": False, "error": "Timeout"})
    resp = flask_app.test_client().get("/readyz")
    assert resp.status_code == 503
    assert resp.get_json()["db"] == {"ok": False, "error": "Timeout"}


def test_probe_db_does_not_hang_on_a_stuck_connection(flask_app, monkeypatch):
    """DB 왕복이 매달려도 /readyz 는 상한 안에 답한다(풀 고갈·무응답 소켓)."""
    import threading

    gate = threading.Event()

    class _Stuck:
        def connect(self):
            gate.wait(10)
            raise RuntimeError("never")

    monkeypatch.setattr(type(db), "engine", property(lambda self: _Stuck()))
    try:
        got = app_module._probe_db(flask_app, timeout=0.3)
    finally:
        gate.set()
    assert got == {"ok": False, "error": "Timeout"}


# --------------------------------------------------------------------------- #
# 3. 스키마 준비 전 게이트
# --------------------------------------------------------------------------- #
def test_requests_get_503_before_schema_ready_but_health_paths_do_not(flask_app, monkeypatch):
    monkeypatch.setitem(app_module._db_boot, "status", "failed")
    c = flask_app.test_client()
    page = c.get("/")
    assert page.status_code == 503 and page.headers["Retry-After"] == "10"
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 503  # 라우트는 있다 — 404 가 아니다
    assert c.get("/sw.js").status_code == 200


def test_requests_pass_when_degraded(flask_app, monkeypatch):
    monkeypatch.setitem(app_module._db_boot, "status", "degraded")
    assert flask_app.test_client().get("/").status_code != 503


# --------------------------------------------------------------------------- #
# 4. 마이그레이션 실패는 삼켜지지 않는다
# --------------------------------------------------------------------------- #
def test_migrations_report_a_failed_step(flask_app):
    """videos 에 중복 행이 있으면 유니크 인덱스가 실패한다 — 반환값에 **나온다**."""
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()
        with db.engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, "
                                 "display_name VARCHAR(60) NOT NULL)")
            conn.exec_driver_sql("CREATE TABLE videos (id INTEGER PRIMARY KEY, "
                                 "onedrive_item_id VARCHAR)")
            conn.exec_driver_sql("INSERT INTO videos (onedrive_item_id) VALUES ('a'), ('a')")
        failures = models.run_startup_migrations()
        assert any("uq_videos_onedrive_item" in f for f in failures), failures
        # 멱등 — 다시 돌려도 같은 결과(조용히 사라지지 않는다)
        assert models.run_startup_migrations() == failures
        # 뒷정리는 flask_app 픽스처의 drop_all/create_all 이 한다(같은 테이블 이름).


def test_migrations_return_empty_list_when_clean(flask_app):
    with flask_app.app_context():
        assert models.run_startup_migrations() == []


# --------------------------------------------------------------------------- #
# 5. 엔진 옵션
# --------------------------------------------------------------------------- #
def test_connect_timeout_only_for_networked_dbs():
    pg = app_module._engine_options("postgresql+psycopg2://u:p@h/db")
    assert pg["connect_args"] == {"connect_timeout": app_module._DB_CONNECT_TIMEOUT_SEC}
    assert pg["pool_pre_ping"] is True and pg["pool_recycle"] == 280  # 기존 값 보존
    assert "connect_args" not in app_module._engine_options("sqlite:///x.db")
