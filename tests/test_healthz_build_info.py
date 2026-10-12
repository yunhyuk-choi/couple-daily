"""`/healthz` 가 **지금 떠 있는 코드가 어느 커밋인지** 말하는지 본다.

머지 후 배포 확인이 추측이 되지 않으려면 밖에서 커밋을 읽을 수 있어야 한다.
서버 내부 렌더만 바뀐 변경(본문 강조 같은)은 새 라우트도, 바뀐 정적 파일 바이트도
남기지 않아 간접 신호가 전부 침묵하기 때문이다.
"""
import json

import app as app_module


def _healthz(flask_app):
    # 인증 없는 엔드포인트 — 로그인된 client 픽스처가 필요 없다.
    resp = flask_app.test_client().get("/healthz")
    assert resp.status_code == 200
    return json.loads(resp.data)


def test_healthz_reports_render_commit(flask_app, monkeypatch):
    """Render 가 주는 RENDER_GIT_COMMIT 이 있으면 그 커밋을 그대로 돌려준다."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    body = _healthz(flask_app)
    assert body["commit"] == "0123456789abcdef0123456789abcdef01234567"


def test_healthz_is_honest_without_env(flask_app, monkeypatch):
    """변수가 없는 환경(로컬·테스트)에서는 깨지지도, 가짜 SHA를 지어내지도 않는다."""
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    body = _healthz(flask_app)
    assert body["commit"] is None


def test_healthz_keeps_status_ok_contract(flask_app, monkeypatch):
    """기존 계약 — 헬스체크가 보는 status 키는 그대로 있어야 한다."""
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    assert _healthz(flask_app)["status"] == "ok"


def test_healthz_reports_start_time(flask_app):
    """기동 시각(UTC ISO8601)도 함께 — 같은 커밋의 재배포를 구분하게."""
    started = _healthz(flask_app)["started_at"]
    assert started.endswith("Z") and started[4] == "-" and "T" in started
    # 프로세스당 한 번 박힌 값이라 요청마다 흔들리지 않는다.
    assert _healthz(flask_app)["started_at"] == started


def test_healthz_leaks_no_secrets(flask_app, monkeypatch):
    """환경변수를 통째로 덤프하지 않는다 — 커밋/기동시각 수준만."""
    monkeypatch.setenv("SECRET_KEY", "super-secret-value")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host/db")
    body = _healthz(flask_app)
    assert set(body) == {"status", "commit", "started_at"}
    assert "super-secret-value" not in json.dumps(body)


def test_build_info_strips_blank_env(monkeypatch):
    """빈 문자열·공백만 든 변수는 '없음'으로 본다(빈 커밋을 보고하지 않게)."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", "   ")
    assert app_module.build_info()["commit"] is None
