"""사진 중복 업로드(아이폰 요청 재전송·더블탭) — 같은 업로드 시도가 두 번 와도 결과는 하나.

멱등 키는 **요청이 아니라 파일 한 장의 업로드 시도** 단위다(AJAX 업로더는 파일마다 요청이
하나라 요청 단위 토큰은 정상 다중 업로드를 막는다). 서버는 (couple_id, upload_token) 유니크
인덱스로 막고, **OneDrive 업로드 전에** 검사해 쓰레기 파일·대역 낭비를 없앤다.
"""
import io
import threading

import pytest
from sqlalchemy import create_engine, event, inspect, text

import app as app_module
import onedrive
from models import Photo, Video, db

TOKEN = "a" * 32


class Fake:
    """OneDrive·캡션·미리보기 가짜 — 호출 수를 센다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.uploads = []
        self.deleted = []
        self.captions = []
        self.n = 0

    def upload(self, stream, size, filename):
        with self.lock:
            self.n += 1
            item = f"item-{self.n}"
            self.uploads.append((item, filename))
        return item, f"stored-{self.n}-{filename}"


@pytest.fixture()
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "upload_photo_stream", f.upload)
    monkeypatch.setattr(onedrive, "get_item_meta", lambda *a, **k: None)
    monkeypatch.setattr(onedrive, "delete_photo", lambda i: f.deleted.append(i))
    monkeypatch.setattr(app_module, "_spawn_warm_previews", lambda *a, **k: None)

    def cap(app, pid):
        with f.lock:
            f.captions.append(pid)

    monkeypatch.setattr(app_module, "_spawn_caption_if_idle", cap)
    return f


def _img(name="a.jpg", body=b"\xff\xd8\xff\xe0fakejpeg"):
    return (io.BytesIO(body), name)


def _ajax(client, files, token=None, field="photos", **extra):
    data = {field: files}
    if token is not None:
        data["upload_token"] = token
    data.update(extra)
    return client.post("/memories/upload?ajax=1", data=data,
                       content_type="multipart/form-data",
                       headers={"X-Requested-With": "fetch"})


# ------------------------------ 중복 흡수 ------------------------------ #
def test_same_token_twice_one_photo_one_caption_one_onedrive_upload(client, fake):
    r1 = _ajax(client, [_img()], TOKEN)
    r2 = _ajax(client, [_img()], TOKEN)
    for r in (r1, r2):
        j = r.get_json()
        assert r.status_code == 200 and j["ok"] is True and j["saved"] == 1
    assert Photo.query.count() == 1
    pid = Photo.query.one().id
    # 두 응답 모두 같은 사진을 가리킨다(후기 폼 업로더가 photo_id 로 스트립에 담는다).
    assert r1.get_json()["photo_id"] == r2.get_json()["photo_id"] == pid
    assert len(fake.uploads) == 1       # OneDrive 는 한 번만
    assert fake.captions == [pid]       # 캡션 claude 도 한 번만
    assert Photo.query.one().upload_token == TOKEN


def test_duplicate_skips_onedrive_even_if_it_would_fail(client, fake, monkeypatch):
    """검사가 업로드 **앞**에 있다는 증거 — 두 번째 도착에선 업로더가 아예 안 불린다."""
    _ajax(client, [_img()], TOKEN)

    def boom(*a, **k):
        raise AssertionError("중복인데 OneDrive 를 불렀다")

    monkeypatch.setattr(onedrive, "upload_photo_stream", boom)
    r = _ajax(client, [_img()], TOKEN)
    assert r.get_json()["ok"] is True and Photo.query.count() == 1


def test_non_ajax_duplicate_redirects_without_new_row(client, fake):
    d = {"photos": _img(), "upload_token": TOKEN}
    r1 = client.post("/memories/upload", data=d, content_type="multipart/form-data")
    d = {"photos": _img(), "upload_token": TOKEN}
    r2 = client.post("/memories/upload", data=d, content_type="multipart/form-data")
    assert r1.status_code == r2.status_code == 302
    assert Photo.query.count() == 1 and len(fake.uploads) == 1


def test_token_is_scoped_to_couple(client, fake, couple_user):
    from models import Couple
    other = Couple(invite_code="OTHERCODE")
    db.session.add(other)
    db.session.flush()
    db.session.add(Photo(couple_id=other.id, onedrive_item_id="x", filename="x",
                         uploaded_by=couple_user.id, upload_token=TOKEN,
                         caption_status="ready"))
    db.session.commit()
    r = _ajax(client, [_img()], TOKEN)
    assert r.get_json()["saved"] == 1
    assert Photo.query.filter_by(couple_id=couple_user.couple_id).count() == 1
    assert len(fake.uploads) == 1


# ------------------------------ 정상 경로 ------------------------------ #
def test_ajax_uploader_many_sequential_each_with_own_token(client, fake):
    """AJAX 업로더: 파일마다 요청 하나, 장마다 다른 토큰 — 전부 저장·캡션 각각."""
    ids = []
    for i in range(5):
        r = _ajax(client, [_img(f"p{i}.jpg")], f"{i:032x}")
        assert r.get_json()["saved"] == 1
        ids.append(r.get_json()["photo_id"])
    assert Photo.query.count() == 5 and len(set(ids)) == 5
    assert len(fake.uploads) == 5
    assert sorted(fake.captions) == sorted(ids)


def test_reupload_same_photo_with_new_token_is_saved_again(client, fake):
    """같은 사진을 일부러 다시 고르면(새 토큰) 막히지 않는다 — 내용 해시였다면 막혔을 것."""
    _ajax(client, [_img("same.jpg")], "1" * 32)
    _ajax(client, [_img("same.jpg")], "2" * 32)
    assert Photo.query.count() == 2 and len(fake.uploads) == 2


def test_bulk_photos_one_request_all_saved(client, fake):
    r = _ajax(client, [_img("a.jpg"), _img("b.jpg"), _img("c.jpg")], TOKEN)
    assert r.get_json()["saved"] == 3
    assert Photo.query.count() == 3 and len(fake.captions) == 3
    # 파일이 여럿이면 어느 토큰이 어느 파일인지 모르니 토큰을 쓰지 않는다.
    assert all(p.upload_token is None for p in Photo.query.all())
    assert len(fake.uploads) == 3


def test_legacy_single_photo_field_still_saves(client, fake):
    r = _ajax(client, [_img("old.jpg")], TOKEN, field="photo")
    assert r.get_json()["saved"] == 1 and Photo.query.count() == 1
    assert Photo.query.one().upload_token == TOKEN


def test_no_token_never_blocked(client, fake):
    """옛 폼·캐시된 화면 — 토큰이 없으면 예전처럼 그냥 저장(같은 파일 두 번이어도)."""
    _ajax(client, [_img()])
    _ajax(client, [_img()])
    assert Photo.query.count() == 2 and len(fake.uploads) == 2


@pytest.mark.parametrize("bad", ["zz", "A" * 32, "a" * 31, "a" * 33, " "])
def test_malformed_token_treated_as_absent(client, fake, bad):
    _ajax(client, [_img()], bad)
    _ajax(client, [_img()], bad)
    assert Photo.query.count() == 2
    assert all(p.upload_token is None for p in Photo.query.all())


def test_validation_failure_does_not_consume_token(client, fake):
    """이미지가 아니라 거절된 요청은 토큰을 쓰지 않는다 — 같은 토큰으로 정상 파일이 통과."""
    bad = _ajax(client, [(io.BytesIO(b"x"), "a.txt")], TOKEN)
    assert bad.get_json()["ok"] is False
    ok = _ajax(client, [_img()], TOKEN)
    assert ok.get_json()["ok"] is True and Photo.query.count() == 1


def test_onedrive_failure_does_not_consume_token(client, fake, monkeypatch):
    real = onedrive.upload_photo_stream

    def fail(*a, **k):
        raise onedrive.OneDriveError("down")

    monkeypatch.setattr(onedrive, "upload_photo_stream", fail)
    assert _ajax(client, [_img()], TOKEN).get_json()["ok"] is False
    monkeypatch.setattr(onedrive, "upload_photo_stream", real)
    assert _ajax(client, [_img()], TOKEN).get_json()["ok"] is True
    assert Photo.query.count() == 1


# ------------------------------ 동시 요청 ------------------------------ #
def test_concurrent_same_token_unique_index_catches_race(flask_app, couple_user,
                                                         fake):
    """사전 조회를 **둘 다 통과한 뒤에** INSERT 하도록 장벽을 세워 최악의 순간을 재현.

    사전 조회만 있었다면 사진이 2개가 된다. 진 쪽은 성공으로 응답하고, 방금 올린
    OneDrive 고아 파일을 지우며, 캡션 claude 를 돌리지 않는다."""
    barrier = threading.Barrier(2, timeout=10)
    hit = []

    def wait_both(mapper, conn, target):
        if target.upload_token == TOKEN:
            hit.append(1)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass

    event.listen(Photo, "before_insert", wait_both)
    results = []

    def worker():
        with app_module.app.app_context():
            c = app_module.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = couple_user.id
            r = _ajax(c, [_img()], TOKEN)
            results.append((r.status_code, r.get_json()))
            db.session.remove()

    try:
        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
    finally:
        event.remove(Photo, "before_insert", wait_both)
    assert len(hit) == 2, "두 요청이 모두 INSERT 직전까지 가야 경쟁이 재현된다"
    assert len(results) == 2
    assert all(code == 200 and j["ok"] and j["saved"] == 1 for code, j in results)
    assert Photo.query.count() == 1
    pid = Photo.query.one().id
    assert {j["photo_id"] for _, j in results} == {pid}
    assert fake.captions == [pid]                    # 진 쪽은 캡션을 안 돌린다
    assert len(fake.uploads) == 2                    # 경쟁 상태에선 둘 다 올렸지만
    assert len(fake.deleted) == 1                    # 진 쪽 고아 파일은 지웠다
    assert fake.deleted[0] != Photo.query.one().onedrive_item_id


# ------------------------------ 스키마·마이그레이션 ------------------------------ #
def test_unique_indexes_exist_on_model_schema(flask_app):
    for name, col in (("uq_photos_couple_upload_token", "upload_token"),
                      ("uq_videos_onedrive_item", "onedrive_item_id")):
        sql = db.session.execute(text(
            "select sql from sqlite_master where type='index' and name=:n"),
            {"n": name}).scalar()
        assert sql and "UNIQUE" in sql.upper() and col in sql


def test_startup_migration_adds_column_and_indexes_to_old_tables(tmp_path,
                                                                 monkeypatch):
    """운영 DB(옛 photos·videos: 컬럼·인덱스 없음)에 붙이고, 반복해도 멱등."""
    import models
    eng = create_engine(
        "sqlite:///" + str(tmp_path / "old.sqlite").replace("\\", "/"))
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, "
                       "email VARCHAR, password_hash VARCHAR)"))
        c.execute(text("CREATE TABLE photos (id INTEGER PRIMARY KEY, "
                       "couple_id INTEGER NOT NULL, tags TEXT, "
                       "caption_status VARCHAR, taken_at TIMESTAMP)"))
        c.execute(text("CREATE TABLE videos (id INTEGER PRIMARY KEY, "
                       "couple_id INTEGER NOT NULL, onedrive_item_id VARCHAR)"))

    class _DB:
        engine = eng

    monkeypatch.setattr(models, "db", _DB)
    models.run_startup_migrations()
    models.run_startup_migrations()
    insp = inspect(eng)
    assert "upload_token" in {c["name"] for c in insp.get_columns("photos")}
    assert any(i["name"] == "uq_photos_couple_upload_token" and i["unique"]
               for i in insp.get_indexes("photos"))
    assert any(i["name"] == "uq_videos_onedrive_item" and i["unique"]
               for i in insp.get_indexes("videos"))


def test_video_item_id_unique_index_blocks_duplicate_row(flask_app, couple_user):
    from sqlalchemy.exc import IntegrityError
    kw = dict(couple_id=couple_user.couple_id, onedrive_item_id="same",
              filename="a.mp4", uploaded_by=couple_user.id, size_bytes=1)
    db.session.add(Video(**kw))
    db.session.commit()
    db.session.add(Video(**kw))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


# ------------------------------ 클라이언트 ------------------------------ #
def test_clients_send_a_fresh_token_per_file(client):
    mem = client.get("/memories").get_data(as_text=True)
    assert "newUploadToken" in mem and 'append("upload_token"' in mem
    # 파일을 고를 때(change) 장마다 만든다 — 제출 때 만들면 재전송 구분이 안 된다.
    assert "tokens = imgs.map(function () { return window.newUploadToken(); })" in mem
    rev = client.get("/reviews/new").get_data(as_text=True)
    assert "newUploadToken" in rev and "append('upload_token'" in rev
    assert "files.map(function () { return window.newUploadToken(); })" in rev
