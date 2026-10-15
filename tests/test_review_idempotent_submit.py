"""후기 중복 제출(아이폰 Safari 요청 재전송·더블탭) — 요청이 두 번 와도 결과는 하나.

서버 멱등성(제출 토큰 + (couple_id, submit_token) 유니크 인덱스)이 진짜 방어이고,
클라이언트 가드는 사람의 더블탭용 UX 다. 둘 다 여기서 고정한다.
"""
import re
import threading

import pytest
from sqlalchemy import create_engine, event, inspect, text

import app as app_module
from models import BlogReview, Couple, db


def _form(token, **over):
    d = {"topic": "성수 카페", "prose": "맛있었다", "overall_score": "8",
         "photo_ids": "[]"}
    if token is not None:
        d["submit_token"] = token
    d.update(over)
    return d


def _token_of(html):
    m = re.search(r'name="submit_token" value="([0-9a-f]{32})"', html)
    assert m, "폼에 제출 토큰이 없다"
    return m.group(1)


@pytest.fixture()
def spawns(monkeypatch):
    """claude 스폰 호출을 센다(conftest 의 no-op 위에 덮어쓴다)."""
    calls = []
    lock = threading.Lock()

    def fake(app, review_id):
        with lock:
            calls.append(review_id)

    monkeypatch.setattr(app_module, "_spawn_generate_review", fake)
    return calls


def test_double_post_makes_one_review_and_one_spawn(client, spawns):
    token = _token_of(client.get("/reviews/new").get_data(as_text=True))
    r1 = client.post("/reviews/new", data=_form(token))
    r2 = client.post("/reviews/new", data=_form(token))
    assert r1.status_code == 302 and r2.status_code == 302
    assert BlogReview.query.count() == 1
    rid = BlogReview.query.one().id
    # 두 번째 응답도 에러가 아니라 먼저 만들어진 그 후기로 간다.
    assert r1.headers["Location"].endswith(f"/reviews/{rid}")
    assert r2.headers["Location"].endswith(f"/reviews/{rid}")
    assert spawns == [rid]  # claude 는 한 번만


def test_normal_path_unchanged_single_submit_spawns(client, spawns):
    token = _token_of(client.get("/reviews/new").get_data(as_text=True))
    resp = client.post("/reviews/new", data=_form(token))
    assert resp.status_code == 302
    review = BlogReview.query.one()
    assert review.status == "pending" and review.submit_token == token
    assert spawns == [review.id]


def test_each_form_get_issues_a_fresh_token_so_two_reviews_in_a_row_work(
        client, spawns):
    t1 = _token_of(client.get("/reviews/new").get_data(as_text=True))
    t2 = _token_of(client.get("/reviews/new").get_data(as_text=True))
    assert t1 != t2
    client.post("/reviews/new", data=_form(t1, topic="첫째"))
    client.post("/reviews/new", data=_form(t2, topic="둘째"))
    assert BlogReview.query.count() == 2
    assert len(spawns) == 2


def test_validation_error_keeps_token_usable(client, spawns):
    token = _token_of(client.get("/reviews/new").get_data(as_text=True))
    bad = client.post("/reviews/new", data=_form(token, topic=""))
    assert bad.status_code == 200
    assert _token_of(bad.get_data(as_text=True)) == token  # 안 쓰였으니 그대로
    assert BlogReview.query.count() == 0
    ok = client.post("/reviews/new", data=_form(token))
    assert ok.status_code == 302 and BlogReview.query.count() == 1


def test_missing_or_malformed_token_still_creates(client, spawns):
    # 옛 폼(캐시된 페이지)·이상한 값 — 막지 않는다(토큰 없이 만든다).
    assert client.post("/reviews/new", data=_form(None)).status_code == 302
    assert client.post("/reviews/new", data=_form("zz")).status_code == 302
    assert BlogReview.query.count() == 2
    assert all(r.submit_token is None for r in BlogReview.query.all())


def test_token_is_scoped_to_couple(client, spawns, flask_app):
    """남의 커플이 만든 토큰으로 내 제출이 남의 후기로 리다이렉트되면 안 된다."""
    other = Couple(invite_code="OTHERCODE")
    db.session.add(other)
    db.session.flush()
    token = "a" * 32
    db.session.add(BlogReview(couple_id=other.id, created_by=1, topic="남",
                              prose="x", overall_score=1, submit_token=token))
    db.session.commit()
    resp = client.post("/reviews/new", data=_form(token))
    assert resp.status_code == 302
    mine = BlogReview.query.filter(BlogReview.topic == "성수 카페").one()
    assert resp.headers["Location"].endswith(f"/reviews/{mine.id}")


def test_concurrent_posts_unique_index_catches_race(flask_app, couple_user,
                                                    spawns):
    """'조회해 보고 없으면 만든다'의 경쟁을 **유니크 인덱스**가 막는지.

    두 요청이 사전 조회를 **둘 다 통과한 뒤에** INSERT 하도록 before_insert 에서
    장벽을 세운다(둘 다 '없다'를 본 최악의 순간을 결정적으로 재현). 사전 조회만
    있었다면 여기서 후기가 2개가 된다."""
    token = "b" * 32
    barrier = threading.Barrier(2, timeout=10)
    hit = []

    def wait_both(mapper, conn, target):
        if target.submit_token == token:
            hit.append(1)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass

    event.listen(BlogReview, "before_insert", wait_both)
    results = []

    def worker():
        with app_module.app.app_context():
            c = app_module.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = couple_user.id
            r = c.post("/reviews/new", data=_form(token))
            results.append((r.status_code, r.headers.get("Location")))
            db.session.remove()

    try:
        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
    finally:
        event.remove(BlogReview, "before_insert", wait_both)
    assert len(hit) == 2, "두 요청이 모두 INSERT 직전까지 가야 경쟁이 재현된다"
    assert len(results) == 2, results
    assert all(code == 302 for code, _ in results), results
    assert BlogReview.query.count() == 1
    rid = BlogReview.query.one().id
    assert all(loc.endswith(f"/reviews/{rid}") for _, loc in results)
    assert spawns == [rid]  # 진 쪽은 claude 를 안 돌린다


def test_unique_index_exists_on_model_schema(flask_app):
    # sqlite_master 를 직접 본다 — Inspector 는 앞선 스레드 테스트 뒤에 캐시가 비어
    # 돌아오는 일이 있어(테스트 순서 의존) 신뢰하지 않는다.
    sql = db.session.execute(text(
        "select sql from sqlite_master where type='index' "
        "and name='uq_blog_reviews_couple_token'")).scalar()
    assert sql and "UNIQUE" in sql.upper() and "submit_token" in sql


def test_startup_migration_adds_column_and_index_to_old_table(tmp_path,
                                                              monkeypatch):
    """운영 DB(옛 blog_reviews: 컬럼·인덱스 없음)에 마이그레이션이 붙이고, 반복해도 멱등."""
    import models
    eng = create_engine(
        "sqlite:///" + str(tmp_path / "old.sqlite").replace("\\", "/"))
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, "
                       "email VARCHAR, password_hash VARCHAR)"))
        c.execute(text("CREATE TABLE blog_reviews (id INTEGER PRIMARY KEY, "
                       "couple_id INTEGER NOT NULL, topic VARCHAR(200))"))

    class _DB:  # run_startup_migrations 는 db.engine 만 쓴다
        engine = eng

    monkeypatch.setattr(models, "db", _DB)
    models.run_startup_migrations()
    models.run_startup_migrations()  # 멱등
    insp = inspect(eng)
    assert "submit_token" in {c["name"] for c in insp.get_columns("blog_reviews")}
    assert any(i["name"] == "uq_blog_reviews_couple_token" and i["unique"]
               for i in insp.get_indexes("blog_reviews"))


# ----------------------------- 클라이언트 가드 ----------------------------- #
def test_form_has_token_and_guard_hooks(client):
    html = client.get("/reviews/new").get_data(as_text=True)
    assert 'id="review-form"' in html and 'id="review-submit"' in html
    assert "등록 중…" in html
    assert "pageshow" in html          # iOS bfcache 복구
    assert "setTimeout(function () { setBusy(true); }, 0)" in html  # 제출 후에 잠근다


def test_edit_form_has_no_token(client, make_review):
    r = make_review()
    html = client.get(f"/reviews/{r.id}/edit").get_data(as_text=True)
    assert 'name="submit_token"' not in html
    assert "수정 중…" in html


# ------------------ _spawn_generate_review 자체의 중복 방어 ------------------ #
def test_spawn_generate_review_twice_same_id_queues_one_job(flask_app, make_review):
    """같은 review_id 로 두 번 불려도 claude 일거리는 하나다(토큰 누수의 직접 원인 점검).

    인프로세스 가드(_generating_reviews) + 큐의 job_key 유니크 두 겹이다."""
    import aijobs
    from models import AiJob
    r = make_review()
    app_module._generating_reviews.discard(r.id)
    try:
        app_module._spawn_generate_review(flask_app, r.id)
        app_module._spawn_generate_review(flask_app, r.id)
        rows = AiJob.query.filter_by(job_key=f"review:{r.id}").all()
        assert len(rows) == 1
        # 가드를 비워도(예: 재시작) 큐 유니크가 두 번째를 흡수한다.
        app_module._generating_reviews.discard(r.id)
        app_module._spawn_generate_review(flask_app, r.id)
        assert AiJob.query.filter_by(job_key=f"review:{r.id}").count() == 1
        assert aijobs.depth() >= 1
    finally:
        app_module._generating_reviews.discard(r.id)
