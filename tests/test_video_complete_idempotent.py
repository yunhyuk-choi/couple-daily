"""/videos/complete 중복 등록 — 순차든 동시든 영상 행은 하나, 진 쪽은 500 이 아니라 already_claimed.

사전 조회만으로는 동시 요청을 못 막는다(둘 다 '없다'를 본다). 유니크 인덱스
(uq_videos_onedrive_item)가 막고, 핸들러가 그 IntegrityError 를 순차 중복과 같은 응답으로 바꾼다.
"""
import threading

from sqlalchemy import event

import app as app_module
import onedrive
from models import Video, db

NAME = "20261015_dup_x.mov"
KEY = "pending_video_names"  # app.py 의 _PENDING_VIDEO_KEY


def _fake_find(monkeypatch):
    monkeypatch.setattr(
        onedrive, "find_in_folder",
        lambda n: {"id": "item-same", "name": n, "size": 10,
                   "ctype": "video/quicktime"})


def _arm(c):
    with c.session_transaction() as s:
        s[KEY] = [NAME]


def test_sequential_duplicate_still_already_claimed(client, monkeypatch):
    _fake_find(monkeypatch)
    _arm(client)
    r1 = client.post("/videos/complete", json={"name": NAME})
    assert r1.status_code == 200 and r1.get_json()["ok"] is True
    _arm(client)  # 다른 탭/재시도 — 발급 기록이 다시 있는 상태
    r2 = client.post("/videos/complete", json={"name": NAME})
    assert r2.status_code == 409 and r2.get_json()["error"] == "already_claimed"
    assert Video.query.count() == 1


def test_concurrent_complete_loser_gets_already_claimed_not_500(
        flask_app, couple_user, monkeypatch):
    """사전 조회를 **둘 다 통과한 뒤에** INSERT 하도록 장벽을 세운다(최악의 순간 재현)."""
    _fake_find(monkeypatch)
    uid = couple_user.id  # 워커 스레드에서 main 세션의 만료 속성을 읽지 않게 미리 쥔다
    barrier = threading.Barrier(2, timeout=20)
    hit = []

    def wait_both(mapper, conn, target):
        hit.append(1)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass

    event.listen(Video, "before_insert", wait_both)
    results = []
    errs = []

    def worker():
        try:
            with app_module.app.app_context():
                c = app_module.app.test_client()
                with c.session_transaction() as s:
                    s["user_id"] = uid
                    s[KEY] = [NAME]
                r = c.post("/videos/complete", json={"name": NAME})
                results.append((r.status_code, (r.get_json() or {}).get("error"),
                                (r.get_json() or {}).get("ok")))
                db.session.remove()
        except BaseException as e:  # 스레드 안 예외를 삼키지 않고 메인에서 assert 한다
            errs.append(repr(e)[:400])

    try:
        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
    finally:
        event.remove(Video, "before_insert", wait_both)
    assert not errs, f"워커 스레드 예외: {errs}"
    assert len(hit) == 2, f"두 요청이 모두 INSERT 직전까지 가야 경쟁이 재현된다: {results}"
    assert len(results) == 2
    assert sorted(results, key=lambda r: r[0]) == [
        (200, None, True), (409, "already_claimed", False)], results
    assert Video.query.count() == 1
