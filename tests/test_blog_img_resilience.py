"""이미지 서빙은 **어떤 경우에도 500 을 내지 않는다** — 2026-10-07 운영 사고 회귀.

무엇이 났나
-----------
운영에서 후기 본문 미리보기의 사진이 **전부 엑박**이었다. 같은 화면의 사진 목록·
크롭 UI 는 멀쩡했다. 실제 traceback::

    File "/app/app.py", in _blog_std_source
        got = onedrive.get_rendition(item_id, need, min_long_edge=need)
    File "/app/onedrive.py", in get_rendition_meta
        body = r.json() or {}
    requests.exceptions.JSONDecodeError: Expecting property name ...

Graph 가 커스텀 렌디션 스펙(``c1707x1707``)에 **JSON 이 아닌 것**을 돌려줬고,
``JSONDecodeError`` 는 ``OneDriveError`` 가 아니라서 호출부의
``except onedrive.OneDriveError`` 에 **안 걸렸다.** 설계돼 있던 원본 폴백에
영영 닿지 못한 채 500 이 됐다.

여기서 못 박는 계약 넷:

  1. ``onedrive`` 밖으로 나가는 실패는 **언제나 OneDriveError** 다 (raw requests
     예외가 새지 않는다).
  2. 렌디션이 **무엇으로 터지든** ``/blog-img`` 는 200 이다(원본 폴백).
  3. 폴백 사다리는 커스텀 → named → 원본 순으로 내려간다. 커스텀을 안 받는
     드라이브는 **한 번 알아내면 기억**해서 매번 같은 값을 치르지 않는다.
  4. 조용한 실패 금지 — 폴백할 때마다 로그에 남는다.
"""
import io
import logging

import pytest
import requests

import app as app_module
import onedrive
from models import Photo, db

PIL = pytest.importorskip("PIL.Image", reason="Pillow 없으면 이미지 경로가 꺼진다")
from PIL import Image  # noqa: E402


def _jpeg(w, h):
    img = Image.new("RGB", (w, h), (120, 90, 140))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


class _Resp:
    """requests.Response 흉내 — 본문이 JSON 이 아닐 수도 있다."""

    def __init__(self, status=200, body="", ctype="text/html"):
        self.status_code = status
        self.text = body
        self.headers = {"Content-Type": ctype}
        self.content = body.encode("utf-8", "replace")

    def json(self):
        raise requests.exceptions.JSONDecodeError(
            "Expecting property name enclosed in double quotes", self.text or "", 1
        )


@pytest.fixture()
def photo(couple_user):
    p = Photo(couple_id=couple_user.couple_id, onedrive_item_id="item-1",
              filename="p.jpg", original_name="p.jpg",
              uploaded_by=couple_user.id, caption_status="ready")
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture(autouse=True)
def _reset_custom_flag():
    onedrive._custom_renditions_ok = None
    yield
    onedrive._custom_renditions_ok = None


@pytest.fixture()
def proc_cache_dir(monkeypatch, tmp_path):
    """가공 캐시를 테스트마다 빈 디렉토리로 격리.

    ⚠️ 속성 이름은 ``dir`` 이다(``directory`` 는 생성자 인자일 뿐). ``directory`` 를
    ``raising=False`` 로 덮으면 **아무 일도 안 일어나고** 시스템 임시폴더의 공용
    캐시를 그대로 쓴다 — 실제로 이 테스트가 옛 실행의 캐시 히트를 받아 200 을
    내는 바람에 들켰다.
    """
    monkeypatch.setattr(app_module._blog_proc_cache, "dir",
                        str(tmp_path / "proc"))


# --------------------------------------------------------------------------- #
# 1. onedrive 밖으로 raw requests 예외가 새지 않는다
# --------------------------------------------------------------------------- #
def test_non_json_rendition_meta_becomes_a_onedrive_error(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Resp(200, "<html>oops</html>"))
    monkeypatch.setattr(onedrive, "_auth_headers", lambda: {})
    with pytest.raises(onedrive.OneDriveError) as e:
        onedrive.get_rendition_meta("item-1", "c1707x1707")
    assert "non-JSON" in str(e.value)
    assert "<html>" in str(e.value), "Graph 가 뭘 줬는지 로그에 안 남는다"


def test_non_json_item_meta_becomes_a_onedrive_error(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, ""))
    monkeypatch.setattr(onedrive, "_auth_headers", lambda: {})
    monkeypatch.setattr(onedrive, "_meta_cache", {})
    with pytest.raises(onedrive.OneDriveError):
        onedrive.get_item_meta("item-nojson")


def test_get_rendition_never_raises_whatever_goes_wrong(monkeypatch):
    """docstring 이 '예외 아님'이라고 약속한다 — 이제 정말 지킨다."""
    def _boom(*a, **k):
        raise RuntimeError("아무거나 터진다")

    monkeypatch.setattr(onedrive, "get_rendition_meta", _boom)
    monkeypatch.setattr(onedrive, "get_item_meta", lambda *a, **k: None)
    assert onedrive.get_rendition("item-1", 1707, min_long_edge=1707) is None


# --------------------------------------------------------------------------- #
# 2. 폴백 사다리 — 커스텀 → named → 원본
# --------------------------------------------------------------------------- #
def test_the_ladder_goes_custom_then_named(monkeypatch):
    seen = []

    def _meta(item_id, spec):
        seen.append(spec)
        if spec.startswith("c"):
            raise onedrive.OneDriveError("이 드라이브는 커스텀을 안 받는다")
        return {"width": 800, "height": 600, "url": "https://cdn/x"}

    monkeypatch.setattr(onedrive, "get_rendition_meta", _meta)
    monkeypatch.setattr(onedrive, "get_item_meta",
                        lambda *a, **k: {"width": 4000, "height": 3000})
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: type("R", (), {
            "status_code": 200, "content": b"jpeg",
            "headers": {"Content-Type": "image/jpeg"}})(),
    )
    got = onedrive.get_rendition("item-1", 1707)
    assert got and got[2] == (800, 600)
    assert seen[0].startswith("c"), "커스텀을 먼저 보지 않았다"
    assert "large" in seen, "named 사다리로 안 내려갔다"


def test_a_drive_that_refuses_custom_specs_is_remembered(monkeypatch):
    """매 요청마다 똑같이 실패하고 비용만 치르지 않는다."""
    seen = []

    def _meta(item_id, spec):
        seen.append(spec)
        if spec.startswith("c"):
            raise onedrive.OneDriveError("거부")
        return None

    monkeypatch.setattr(onedrive, "get_rendition_meta", _meta)
    monkeypatch.setattr(onedrive, "get_item_meta", lambda *a, **k: None)
    onedrive.get_rendition("item-1", 1707)
    assert onedrive.custom_renditions_supported() is False
    seen.clear()
    onedrive.get_rendition("item-1", 1600)
    assert not any(s.startswith("c") for s in seen), (
        "안 받는 걸 알면서 또 커스텀을 물었다"
    )


def test_a_rendition_too_small_stops_the_ladder(monkeypatch):
    """요구 해상도에 못 미치면 named(더 작다)를 더 보지 않고 원본으로 넘긴다."""
    seen = []

    def _meta(item_id, spec):
        seen.append(spec)
        return {"width": 400, "height": 300, "url": "https://cdn/x"}

    monkeypatch.setattr(onedrive, "get_rendition_meta", _meta)
    monkeypatch.setattr(onedrive, "get_item_meta", lambda *a, **k: None)
    assert onedrive.get_rendition("item-1", 1707, min_long_edge=1707) is None
    assert len(seen) == 1, f"쓸데없이 Graph 를 더 왕복했다: {seen}"


# --------------------------------------------------------------------------- #
# 3. /blog-img 는 500 을 내지 않는다 — 원본 폴백으로 **200**
# --------------------------------------------------------------------------- #
def _std_url(flask_app, photo, crop):
    with flask_app.test_request_context("/"):
        return app_module.blog_img_url(photo, crop=crop, external=False)


@pytest.mark.parametrize("boom", [
    requests.exceptions.JSONDecodeError("bad", "<html>", 1),   # 실제 운영 사고
    RuntimeError("Graph 가 이상한 걸 줬다"),
    ValueError("잘린 JSON"),
])
def test_blog_img_falls_back_to_the_original_when_renditions_explode(
        boom, flask_app, client, photo, proc_cache_dir, monkeypatch, caplog):
    original = _jpeg(1200, 900)
    monkeypatch.setattr(onedrive, "get_item_meta",
                        lambda *a, **k: {"width": 1200, "height": 900,
                                         "ctype": "image/jpeg", "etag": "e"})
    monkeypatch.setattr(onedrive, "get_photo_content_cached",
                        lambda item_id: (original, "image/jpeg"))

    def _boom(*a, **k):
        raise boom

    monkeypatch.setattr(onedrive, "get_rendition_meta", _boom)
    url = _std_url(flask_app, photo, [0.0, 0.212, 1.0, 0.563])
    with caplog.at_level(logging.WARNING):
        resp = client.get(url)
    assert resp.status_code == 200, (
        f"이미지 서빙이 {resp.status_code} 를 냈다 — 폴백에 못 닿았다"
    )
    assert resp.mimetype.startswith("image/")
    assert len(resp.data) > 0


def test_blog_img_logs_instead_of_failing_silently(
        flask_app, client, photo, proc_cache_dir, monkeypatch, caplog):
    """조용한 실패 금지 — 폴백했으면 그 사실이 로그에 남아야 한다."""
    monkeypatch.setattr(onedrive, "get_item_meta",
                        lambda *a, **k: {"width": 4000, "height": 3000})
    monkeypatch.setattr(onedrive, "get_photo_content_cached",
                        lambda item_id: (_jpeg(1200, 900), "image/jpeg"))
    monkeypatch.setattr(onedrive, "get_rendition_meta",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.exceptions.JSONDecodeError("bad", "x", 1)))
    url = _std_url(flask_app, photo, [0.0, 0.2, 1.0, 0.5])
    with caplog.at_level(logging.WARNING):
        assert client.get(url).status_code == 200
    assert any("렌디션" in r.getMessage() for r in caplog.records), caplog.text


def test_blog_img_returns_404_not_500_when_everything_fails(
        flask_app, client, photo, proc_cache_dir, monkeypatch):
    """원본조차 못 받으면 **404**다 — 에러 페이지(500)가 아니다."""
    monkeypatch.setattr(onedrive, "get_item_meta", lambda *a, **k: None)

    def _dead(*a, **k):
        raise RuntimeError("드라이브가 통째로 죽었다")

    monkeypatch.setattr(onedrive, "get_photo_content_cached", _dead)
    url = _std_url(flask_app, photo, None)
    assert client.get(url).status_code == 404
