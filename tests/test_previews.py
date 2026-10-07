"""미리보기 자산 계약 — **우리가 만들어 우리가 소유하고, 두 번째부터는 Graph 가 0이다.**

이 파일이 지키는 것은 숫자가 아니라 **구조**다(이미지 파이프라인 테스트와 같은 사상):

  1. 미리보기 바이트는 **DB 에 영구 보관**된다 — 프로세스 메모리도 임시 디스크도
     아니다. Render 는 재배포마다 둘 다 비우므로, 거기 두면 '배포할 때마다 전부
     다시 받기'가 돌아온다.
  2. 같은 화면을 두 번째로 열 때 **Graph 왕복이 0**이다.
  3. 미리보기 URL 에는 **서명도 만료도 없다**. 사진당 고정이고, 불변 자산처럼
     캐시되며(immutable + ETag), 그래서 재방문은 네트워크가 안 나간다.
     만료·서명은 네이버 발행용 **외부 노출** 이미지에만 남는다.
  4. 사람이 보는 화면에 **원본을 걸지 않는다**(썸네일 카드 배경·라이트박스).
     원본은 '내려받기'와 '발행용 캔버스 크롭'에만 쓰인다.
"""
import io

import pytest

import app as app_module
import onedrive
import previews
from models import Photo, PhotoPreview, db

PIL = pytest.importorskip("PIL.Image", reason="Pillow 없으면 이미지 경로가 꺼진다")
from PIL import Image  # noqa: E402


def _jpeg(w, h, quality=85):
    small = Image.effect_noise((max(2, w // 6), max(2, h // 6)), 48).convert("RGB")
    out = io.BytesIO()
    small.resize((w, h), Image.BICUBIC).save(out, format="JPEG", quality=quality)
    return out.getvalue()


class FakeDrive:
    """원본 3024x4032 한 장. **Graph 를 몇 번 불렀는지** 전부 센다."""

    def __init__(self, w=3024, h=4032, rendition_ok=True):
        self.w, self.h = w, h
        self.rendition_ok = rendition_ok
        self.calls = []

    def get_rendition(self, item_id, long_edge, min_long_edge=None,
                      expect_aspect=None):
        self.calls.append(("rendition", int(long_edge)))
        if not self.rendition_ok:
            return None
        scale = min(1.0, int(long_edge) / max(self.w, self.h))
        w, h = max(1, round(self.w * scale)), max(1, round(self.h * scale))
        return _jpeg(w, h), "image/jpeg", (w, h)

    def get_thumbnail(self, item_id, size="medium"):
        self.calls.append(("thumbnail", size))
        px = {"small": 96, "medium": 176, "large": 800}.get(size, 176)
        scale = min(1.0, px / max(self.w, self.h))
        return _jpeg(max(1, round(self.w * scale)), max(1, round(self.h * scale))), \
            "image/jpeg"

    def get_photo_content(self, item_id):
        self.calls.append(("original", item_id))
        return _jpeg(self.w, self.h), "image/jpeg"

    def get_photo_content_cached(self, item_id):
        return self.get_photo_content(item_id)

    def count(self, kind=None):
        if kind is None:
            return len(self.calls)
        return sum(1 for c in self.calls if c[0] == kind)


@pytest.fixture()
def drive(monkeypatch):
    d = FakeDrive()
    for name in ("get_rendition", "get_thumbnail", "get_photo_content",
                 "get_photo_content_cached"):
        monkeypatch.setattr(onedrive, name, getattr(d, name))
    previews._locks.clear()
    return d


@pytest.fixture()
def photo(couple_user):
    p = Photo(couple_id=couple_user.couple_id, onedrive_item_id="item-1",
              filename="p.jpg", original_name="p.jpg",
              uploaded_by=couple_user.id, caption_status="ready")
    db.session.add(p)
    db.session.commit()
    return p


# --------------------------------------------------------------------------- #
# 1. 만들기는 한 번, 보관은 DB
# --------------------------------------------------------------------------- #
def test_preview_is_generated_once_and_stored_in_the_database(drive, photo):
    got = previews.ensure(photo, "grid")
    assert got is not None
    data, ctype, etag, w, h = got
    assert data and ctype.startswith("image/") and etag
    row = PhotoPreview.query.filter_by(photo_id=photo.id, tier="grid").one()
    assert row.data == data, "자산이 DB 에 그대로 보관되지 않았다"
    assert row.byte_len == len(data)
    assert row.source.startswith("rendition:"), "렌디션이 아니라 다른 걸로 만들었다"
    assert (w, h) == (row.width, row.height)


def test_the_second_view_does_not_touch_graph_at_all(drive, photo):
    previews.ensure(photo, "grid")
    before = drive.count()
    assert before > 0, "첫 생성이 Graph 를 아예 안 불렀다면 이 테스트가 무의미하다"
    for _ in range(5):
        previews.ensure(photo, "grid")
    assert drive.count() == before, (
        "두 번째부터는 **Graph 왕복이 0**이어야 한다 — DB 읽기만 남는다"
    )


def test_the_asset_survives_a_restart_because_it_lives_in_the_database(
        drive, photo, monkeypatch):
    """재시작·재배포 모사 — 프로세스 메모리·락을 전부 비워도 Graph 는 0이다.

    Render 재배포는 프로세스 메모리와 임시 디스크를 **둘 다** 비우고 DB 만 남긴다.
    그래서 여기서도 인프로세스 상태를 통째로 비우고 다시 묻는다.
    """
    previews.ensure(photo, "grid")
    calls_before = drive.count()
    previews._locks.clear()           # 워커-내 상태를 전부 버린다(= 새 프로세스)
    onedrive._thumb_cache.clear()
    got = previews.ensure(photo, "grid")
    assert got is not None and got[0]
    assert drive.count() == calls_before, (
        "재시작 뒤에 Graph 를 다시 불렀다 — 자산이 영구 보관되지 않았다는 뜻이다"
    )


def test_a_tier_is_generated_lazily_and_separately(drive, photo):
    previews.ensure(photo, "grid")
    assert drive.count("rendition") == 1
    previews.ensure(photo, "view")
    assert drive.count("rendition") == 2, "view 티어는 따로 한 번 만든다"
    edges = sorted(c[1] for c in drive.calls if c[0] == "rendition")
    assert edges == sorted([previews.GRID_EDGE, previews.VIEW_EDGE])


def test_generation_never_opens_the_original(drive, photo):
    previews.ensure(photo, "grid")
    previews.ensure(photo, "view")
    assert drive.count("original") == 0, (
        "미리보기를 만들려고 원본을 받았다 — 서버는 원본 픽셀을 만지지 않는다"
    )


def test_a_drive_without_custom_renditions_falls_back_to_named_thumbnails(
        monkeypatch, photo):
    d = FakeDrive(rendition_ok=False)
    for name in ("get_rendition", "get_thumbnail", "get_photo_content"):
        monkeypatch.setattr(onedrive, name, getattr(d, name))
    previews._locks.clear()
    got = previews.ensure(photo, "grid")
    assert got is not None and got[0], "폴백이 없으면 그리드가 통째로 빈다"
    row = PhotoPreview.query.filter_by(photo_id=photo.id, tier="grid").one()
    assert row.source.startswith("thumbnail:")
    assert d.count("original") == 0


def test_a_broken_drive_degrades_to_none_instead_of_raising(monkeypatch, photo):
    def boom(*a, **k):
        raise onedrive.OneDriveError("graph is down")
    monkeypatch.setattr(onedrive, "get_rendition", boom)
    monkeypatch.setattr(onedrive, "get_thumbnail", boom)
    previews._locks.clear()
    assert previews.ensure(photo, "grid") is None


def test_deleting_a_photo_takes_its_previews_with_it(drive, photo):
    previews.ensure(photo, "grid")
    previews.ensure(photo, "view")
    assert PhotoPreview.query.filter_by(photo_id=photo.id).count() == 2
    previews.forget(photo.id)
    assert PhotoPreview.query.filter_by(photo_id=photo.id).count() == 0


# --------------------------------------------------------------------------- #
# 2. 내주기 — 고정 URL · 불변 캐시 · 304
# --------------------------------------------------------------------------- #
def test_thumb_route_is_cached_like_an_immutable_asset(drive, photo, client):
    r = client.get(f"/memories/{photo.id}/thumb")
    assert r.status_code == 200 and r.data
    cc = r.headers["Cache-Control"]
    assert "immutable" in cc and "max-age=31536000" in cc, (
        "재방문 네트워크 0 은 불변 캐시에서 나온다 — " + cc
    )
    assert r.headers.get("ETag"), "ETag 가 없으면 재검증이 304 로 끝나지 못한다"


def test_an_unchanged_asset_answers_304(drive, photo, client):
    r1 = client.get(f"/memories/{photo.id}/thumb")
    etag = r1.headers["ETag"]
    r2 = client.get(f"/memories/{photo.id}/thumb",
                    headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.data == b"", "304 인데 바이트를 보냈다"


def test_serving_the_same_url_twice_costs_one_graph_trip_in_total(
        drive, photo, client):
    client.get(f"/memories/{photo.id}/thumb")
    n = drive.count()
    client.get(f"/memories/{photo.id}/thumb")
    client.get(f"/memories/{photo.id}/thumb")
    assert drive.count() == n, "같은 썸네일을 다시 내주며 Graph 를 또 불렀다"


def test_preview_route_serves_the_view_tier(drive, photo, client):
    r = client.get(f"/memories/{photo.id}/preview")
    assert r.status_code == 200
    row = PhotoPreview.query.filter_by(photo_id=photo.id, tier="view").one()
    assert r.data == row.data


def test_preview_urls_carry_no_signature_and_no_expiry(flask_app, photo):
    with flask_app.test_request_context("/"):
        t = app_module._thumb_url(photo.id)
        v = app_module._preview_url(photo.id)
    for url in (t, v):
        assert "e=" not in url and "t=" not in url, (
            "사용자가 보는 미리보기에 만료·서명이 다시 붙었다 — " + url
        )
        assert "/blog-img/" not in url
    assert f"r={previews.REV}" in t and f"r={previews.REV}" in v, (
        "캐시 버전이 없으면 티어 크기를 바꿔도 캐시된 브라우저에 닿지 못한다"
    )


def test_another_couples_photo_is_not_served(drive, photo, client, couple_user):
    from models import Couple, User
    other = Couple(invite_code="OTHER")
    db.session.add(other)
    db.session.flush()
    stranger = User(email="z@example.com", password_hash="x", display_name="남",
                    couple_id=other.id, status="approved")
    db.session.add(stranger)
    db.session.flush()
    theirs = Photo(couple_id=other.id, onedrive_item_id="item-x", filename="x.jpg",
                   uploaded_by=stranger.id, caption_status="ready")
    db.session.add(theirs)
    db.session.commit()
    assert client.get(f"/memories/{theirs.id}/thumb").status_code == 404
    assert client.get(f"/memories/{theirs.id}/preview").status_code == 404


# --------------------------------------------------------------------------- #
# 3. 사람이 보는 화면에 원본을 걸지 않는다
# --------------------------------------------------------------------------- #
def test_the_thumbnail_card_previews_with_our_asset_and_rasters_from_the_original(
        drive, photo, client, make_review):
    """썸네일 카드는 **두 URL**을 받는다 — 화면용(자산)과 캔버스용(원본).

    예전엔 하나뿐이었고 그게 **원본**이라, 썸네일 카드가 있는 후기는 상세 화면을
    열 때마다 아무도 '내려받기'를 누르지 않아도 원본 한 장을 통째로 받았다.
    """
    import json as _json

    import thumbnail as thumb_mod
    state = thumb_mod.normalize_copy({
        "candidates": [{"main_line1": "진한", "main_line2": "하루",
                        "sub": "성수동", "badge": "후기"}],
        "picked": 0,
    })
    state["status"] = "ready"
    review = make_review(
        photo_ids="[%d]" % photo.id, status="ready",
        ai_json='{"title":"t","blocks":[]}',
        thumbnail_json=_json.dumps(state, ensure_ascii=False),
    )
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert f'data-preview="/memories/{photo.id}/preview' in html, (
        "썸네일 카드의 화면 미리보기가 아직 원본을 띄운다"
    )
    assert "v=orig" in html, "캔버스 래스터용 원본 URL 이 사라졌다 — 화질이 깎인다"


def test_the_crop_ui_preview_is_our_asset_not_a_signed_rendition(
        drive, photo, flask_app, make_review, client):
    review = make_review(
        photo_ids="[%d]" % photo.id, status="ready",
        ai_json='{"title":"t","blocks":[{"type":"image","photo_index":0,'
                '"crop":[0.1,0.2,0.5,0.4]}]}',
    )
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert f"/memories/{photo.id}/preview" in html, (
        "크롭 UI 가 아직 만료되는 서명 렌디션을 쓰고 있다"
    )
    assert "v=r1280" not in html, "서명 렌디션 미리보기가 남아 있다"


def test_the_grid_markup_opens_the_preview_not_the_original(
        drive, photo, client, monkeypatch):
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "reconnect_needed", lambda: False)
    monkeypatch.setattr(app_module, "_spawn_caption_if_idle", lambda *a, **k: None)
    html = client.get("/memories").get_data(as_text=True)
    assert f'data-full="/memories/{photo.id}/preview' in html, (
        "라이트박스가 아직 원본을 띄운다"
    )
    assert f'data-download="/memories/{photo.id}/image?download=1"' in html, (
        "내려받기는 **원본**이어야 한다 — 그건 미리보기가 아니다"
    )


# --------------------------------------------------------------------------- #
# 4. 발행용 서명 URL — 만료는 남되, 렌더마다 바뀌지는 않는다
# --------------------------------------------------------------------------- #
def test_the_published_image_url_is_stable_within_its_ttl_bucket(flask_app):
    """만료가 '지금+TTL' 이면 **렌더마다 URL 이 달라져** 브라우저 캐시가 매번 깨진다.

    그래서 만료를 TTL 경계에 올려 버킷 안에서는 같은 URL 이 나오게 했다. 유효
    기간은 TTL~2×TTL 이라 **옛 동작보다 짧아지지 않는다**(네이버가 가져갈 시간).
    """
    ttl = app_module._BLOG_IMG_TTL
    base = 1_700_000_000 // ttl * ttl        # 버킷 경계
    with flask_app.app_context():
        e1 = app_module._blog_img_exp(base + 1)
        e2 = app_module._blog_img_exp(base + ttl - 1)
        e3 = app_module._blog_img_exp(base + ttl + 1)
    assert e1 == e2, "같은 버킷 안에서 만료가 달라졌다 — 캐시 키가 매번 깨진다"
    assert e3 > e1, "버킷이 넘어가면 만료도 넘어가야 한다"
    assert e1 - (base + ttl - 1) >= ttl, "유효 기간이 옛 TTL 보다 짧아졌다"


def test_the_published_image_url_still_expires(flask_app, photo, client):
    """미리보기에서 만료를 걷어냈다고 **발행용**까지 걷으면 안 된다."""
    with flask_app.test_request_context("/"):
        url = app_module.blog_img_url(photo)
    assert "e=" in url and "t=" in url, "외부 노출 이미지는 만료·서명이 맞다"


# --------------------------------------------------------------------------- #
# 5. 구조 — 이 모듈은 픽셀을 만지지 않는다
# --------------------------------------------------------------------------- #
def test_previews_module_never_decodes_pixels():
    """구조 계약 — 이 모듈은 Pillow 도 원본 바이트도 건드리지 않는다.

    머리말(문서)은 그 사실을 **설명**하느라 단어를 쓰므로, 코드만 본다.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(previews))
    tree.body = [n for n in tree.body
                 if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                         and isinstance(n.value.value, str))]
    code = ast.unparse(tree)
    assert "PIL" not in code and "Image" not in code, (
        "미리보기 생성이 Pillow 를 쓰기 시작했다 — 크기 조정은 Graph 가 한다"
    )
    assert "get_photo_content" not in code, (
        "미리보기 생성이 원본 바이트를 받기 시작했다"
    )
