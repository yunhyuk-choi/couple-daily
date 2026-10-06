"""pytest 공통 픽스처.

`app.py`는 import 시점에 `create_app()`을 돌리고 `DATABASE_URL`이 없으면 instance/의
개발용 SQLite를 연다. 테스트가 개발 DB를 건드리면 안 되므로 **app을 import 하기 전에**
임시 파일 SQLite를 `DATABASE_URL`로 꽂는다. (`DATABASE_URL`이 있으면 앱이 prod로 보고
Secure 쿠키를 켜므로, import 후에 그 설정만 꺼서 테스트 클라이언트가 세션을 유지한다.)

claude CLI는 **절대 돌지 않는다** — 배경 생성 스폰은 픽스처에서 no-op으로 바꾸고,
프롬프트 조립 검증은 `ai._run_claude`를 가짜로 끼워 넣어서 한다.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".sqlite", prefix="couple-daily-test-")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")
os.environ.setdefault("SECRET_KEY", "test-secret")

import app as app_module  # noqa: E402  (DATABASE_URL을 먼저 꽂아야 한다)
from models import BlogReview, Couple, User, db  # noqa: E402

app_module.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)


@pytest.fixture()
def flask_app():
    """매 테스트마다 비운 스키마 위에서 도는 앱."""
    with app_module.app.app_context():
        db.drop_all()
        db.create_all()
        yield app_module.app
        db.session.remove()


@pytest.fixture()
def couple_user(flask_app):
    """승인된 커플 1쌍 중 한 명(라우트의 @active_couple_required를 통과)."""
    couple = Couple(invite_code="TESTCODE")
    db.session.add(couple)
    db.session.flush()
    user = User(
        email="a@example.com",
        password_hash="x",
        display_name="테스터",
        couple_id=couple.id,
        status="approved",
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def client(flask_app, couple_user, monkeypatch):
    """로그인된 테스트 클라이언트. 배경 AI 스폰은 no-op으로 막는다."""
    monkeypatch.setattr(app_module, "_spawn_generate_review", lambda *a, **k: None)
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = couple_user.id
    return c


@pytest.fixture()
def make_review(couple_user):
    """BlogReview 한 건을 만들어 커밋하는 헬퍼."""
    def _make(**kw):
        kw.setdefault("topic", "문래갈매기")
        kw.setdefault("prose", "고기가 맛있었어요.")
        kw.setdefault("overall_score", 8)
        review = BlogReview(
            couple_id=couple_user.couple_id, created_by=couple_user.id, **kw
        )
        db.session.add(review)
        db.session.commit()
        return review
    return _make
