"""이미지 구조 계약 — **서버는 원본 픽셀을 디코드하지 않는다.**

이 파일이 지키는 것은 메모리 수치가 아니라 **구조**다. 숫자는 환경마다 흔들리지만
"원본을 열었는가"는 흔들리지 않는다. 그래서 '원본 바이트를 받았는가'와 'Pillow 로
열었는가'를 **동작으로** 못 박는다 — 누가 편의를 위해 되돌리면 여기서 깨진다.

배경(2026-10-07 OOM 이후 2차 설계):
  * 크롭 좌표는 0~1 **정규화**라 해상도와 무관하다 → '어디를 자를지'는 작은 렌디션만
    보고 정하고, '실제로 자르기'는 원본을 가진 **브라우저**가 한다.
  * 그래서 서버가 픽셀을 만지는 자리는 **하나**만 남았다: 네이버 스마트에디터에
    붙여넣는 HTML 의 `<img src>` 가 가리키는 std 바이트(브라우저가 낄 자리가 없다).
    그 하나도 입력이 **원본이 아니라 렌디션**이다.
  * 업로드는 `file.read()` 없이 스트림으로 흘린다.
"""
import io

import pytest

import ai
import app as app_module
import onedrive
from models import Photo, db

PIL = pytest.importorskip("PIL.Image", reason="Pillow 없으면 이미지 경로가 꺼진다")
from PIL import Image  # noqa: E402


# --------------------------------------------------------------------------- #
# 픽스처 — 가짜 OneDrive. '무엇을 불렀는지'를 전부 센다.
# --------------------------------------------------------------------------- #
def _jpeg(w, h, quality=92):
    small = Image.effect_noise((max(2, w // 6), max(2, h // 6)), 48).convert("RGB")
    img = small.resize((w, h), Image.BICUBIC)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()


class FakeDrive:
    """원본 3024x4032 한 장을 가진 가짜 드라이브. 호출을 전부 기록한다."""

    def __init__(self, w=3024, h=4032):
        self.w, self.h = w, h
        self.original = _jpeg(w, h)
        self.calls = []

    # --- onedrive 인터페이스 ---
    def get_item_meta(self, item_id, refresh=False):
        self.calls.append(("meta", item_id))
        return {"id": item_id, "name": "p.jpg", "size": len(self.original),
                "ctype": "image/jpeg", "width": self.w, "height": self.h,
                "taken_at": None, "etag": "etag-1"}

    def get_rendition_meta(self, item_id, spec):
        self.calls.append(("rendition_meta", spec))
        edge = int(spec[1:].split("x")[0])
        scale = min(1.0, edge / max(self.w, self.h))
        return {"width": max(1, round(self.w * scale)),
                "height": max(1, round(self.h * scale)), "url": "https://cdn/x"}

    def get_rendition(self, item_id, long_edge, min_long_edge=None,
                      expect_aspect=None):
        self.calls.append(("rendition", int(long_edge)))
        scale = min(1.0, int(long_edge) / max(self.w, self.h))
        w, h = max(1, round(self.w * scale)), max(1, round(self.h * scale))
        if min_long_edge and max(w, h) < int(min_long_edge):
            return None
        return _jpeg(w, h, quality=90), "image/jpeg", (w, h)

    def get_photo_content(self, item_id):
        self.calls.append(("original", item_id))
        return self.original, "image/jpeg"

    def get_photo_content_cached(self, item_id):
        return self.get_photo_content(item_id)

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


@pytest.fixture()
def drive(monkeypatch, tmp_path):
    d = FakeDrive()
    for name in ("get_item_meta", "get_rendition_meta", "get_rendition",
                 "get_photo_content", "get_photo_content_cached"):
        monkeypatch.setattr(onedrive, name, getattr(d, name))
    # 가공완료 캐시는 테스트마다 빈 디렉토리로 격리(레포 워킹트리는 절대 안 건드린다).
    # ⚠️ 속성 이름은 ``dir`` 이다(``directory`` 는 생성자 인자일 뿐). 예전엔
    # ``directory`` 를 ``raising=False`` 로 덮어 **격리가 전혀 안 됐고**, 시스템
    # 임시폴더의 공용 캐시를 그대로 썼다(옛 실행의 히트가 섞인다).
    monkeypatch.setattr(app_module._blog_proc_cache, "dir",
                        str(tmp_path / "proc"))
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
# 1. 역산 — 결과 화질을 지키는 최소 렌디션
# --------------------------------------------------------------------------- #
def test_required_rendition_is_smaller_than_the_original():
    """세로 12MP + 4:3 크롭 + 1280 출력 → 원본(4032)이 아니라 ~1706px면 충분하다."""
    crop = app_module.compute_crop_rect(3024, 4032, None, 4 / 3)
    need = app_module._rendition_long_edge_for(3024, 4032, crop, 1280)
    assert need is not None
    assert need < 4032, "원본을 받아야 한다고 계산했다 — 역산이 망가졌다"
    assert 1600 <= need <= 1800, f"예상 밖 요구치: {need}"


def test_required_rendition_never_undershoots_a_tight_crop():
    """작은 영역을 타이트하게 집으면 요구치가 **커진다**(화질을 깎지 않는다)."""
    loose = app_module._rendition_long_edge_for(4000, 3000, [0, 0, 1.0, 1.0], 1280)
    tight = app_module._rendition_long_edge_for(4000, 3000, [0.4, 0.4, 0.1, 0.1], 1280)
    assert tight > loose
    # 10%만 쓰는 크롭이면 소스가 10배 커야 한다 → 원본보다 크므로 원본을 쓴다.
    assert tight >= 4000


def test_required_rendition_is_orientation_safe():
    """``image`` 패싯이 회전 전 값이어도 **모자라지 않게** 더 큰 쪽을 고른다."""
    a = app_module._rendition_long_edge_for(3024, 4032, [0, 0, 1.0, 0.5625], 1280)
    b = app_module._rendition_long_edge_for(4032, 3024, [0, 0, 1.0, 0.5625], 1280)
    assert a >= 1280 and b >= 1280


# --------------------------------------------------------------------------- #
# 2. std 서빙 — 입력이 **원본이 아니라 렌디션**이다 (출력은 그대로)
# --------------------------------------------------------------------------- #
def test_std_blog_img_never_downloads_the_original(drive, photo, flask_app):
    crop = app_module.compute_crop_rect(3024, 4032, None, 4 / 3)
    data, label, target = app_module._blog_std_source(photo, crop, 1280)
    assert data is not None
    assert target is not None, "원본 기준 출력 크기를 같이 돌려주지 않았다"
    assert drive.count("original") == 0, (
        "std 경로가 원본 바이트를 받았다 — 12MP 디코드가 되돌아왔다"
    )
    assert label.startswith("rendition"), label


def test_std_output_from_a_rendition_matches_the_original_path(drive, photo):
    """같은 1280 결과를 **원본에서** 만든 것과 **렌디션에서** 만든 것이 같은가.

    출력 해상도가 같고 픽셀이 (리샘플 오차 수준에서) 같아야 한다. 몰래 깎였으면
    여기서 드러난다.
    """
    crop = app_module.compute_crop_rect(3024, 4032, None, 4 / 3)
    from_orig = app_module._blog_img_process(
        drive.original, crop=crop, max_edge=1280, quality=92)
    src, _label, target = app_module._blog_std_source(photo, crop, 1280)
    from_rend = app_module._blog_img_process(
        src, crop=crop, max_edge=1280, quality=92, target_size=target)
    with Image.open(io.BytesIO(from_orig)) as a, Image.open(io.BytesIO(from_rend)) as b:
        assert a.size == b.size, f"출력 해상도가 달라졌다: {a.size} vs {b.size}"
        assert max(a.size) == 1280


def test_std_route_serves_jpeg_without_touching_the_original(drive, photo,
                                                             flask_app, client):
    with flask_app.test_request_context("/"):
        url = app_module.blog_img_url(photo, crop=[0.0, 0.2, 1.0, 0.6],
                                      external=False)
    r = client.get(url)
    assert r.status_code == 200
    assert r.mimetype == "image/jpeg"
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    assert drive.count("original") == 0


# --------------------------------------------------------------------------- #
# 3. 중계 등급(v=orig / v=rN) — 서버가 **아무것도 열지 않는다**
# --------------------------------------------------------------------------- #
class _LazyUpstream:
    def __init__(self, payload, chunk=64 * 1024):
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload)),
                        "Content-Type": "image/jpeg"}
        self.status_code = 200
        self.pulled = 0
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.payload), chunk_size):
            self.pulled += 1
            yield self.payload[i:i + chunk_size]

    def close(self):
        self.closed = True


def test_v_orig_streams_the_original_without_decoding(drive, photo, flask_app,
                                                      client, monkeypatch):
    up = _LazyUpstream(drive.original)
    monkeypatch.setattr(onedrive, "open_range",
                        lambda item_id, rh=None: (200, up.headers, up))
    opened = {"n": 0}
    real_open = Image.open

    def _spy(*a, **k):
        opened["n"] += 1
        return real_open(*a, **k)

    monkeypatch.setattr(app_module._PILImage, "open", _spy)

    with flask_app.test_request_context("/"):
        url = app_module.publish_source_url(photo, external=False)
    r = client.get(url)
    assert r.status_code == 200
    body = r.get_data()
    assert body == drive.original, "원본 바이트가 그대로 나가지 않았다"
    assert opened["n"] == 0, "서버가 Pillow 로 이미지를 열었다 — 중계 등급의 계약 위반"
    assert drive.count("original") == 0, "원본을 통째로 변수에 담았다(중계가 아니다)"
    assert up.pulled > 1, "한 덩어리로 읽었다 — 제너레이터 중계가 아니다"
    assert up.closed, "업스트림 소켓을 거두지 않았다"


def test_v_rendition_is_relayed_as_is(drive, photo, flask_app, client):
    with flask_app.test_request_context("/"):
        url = app_module.blog_img_url(photo, external=False, variant="r1280")
    r = client.get(url)
    assert r.status_code == 200
    with Image.open(io.BytesIO(r.get_data())) as im:
        assert max(im.size) <= 1280
    assert drive.count("original") == 0
    assert drive.count("rendition") == 1


# --------------------------------------------------------------------------- #
# 4. 크롭 판단 — 치수도 비전도 원본을 안 쓴다
# --------------------------------------------------------------------------- #
def test_a_published_std_token_cannot_be_upgraded_to_the_original(
        drive, photo, flask_app, client):
    """⛔ 네이버 글에 박힌 std URL 에 ``&v=orig`` 를 붙여 **원본**을 꺼낼 수 없다.

    예전엔 ``v`` 가 서명 대상이 아니어서 가능했다. std 는 1280px 로 **잘린** 그림인데
    원본은 자르지 않은 풀 해상도다 — 크롭으로 가린 바깥까지 나간다. '같은 사진 한
    장이라 노출 범위가 같다'는 옛 전제가 거기서 깨진다.
    """
    with flask_app.test_request_context("/"):
        std = app_module.blog_img_url(photo, external=False)
    assert client.get(std).status_code == 200
    assert client.get(std + "&v=orig").status_code == 404, (
        "std 토큰으로 원본을 꺼낼 수 있다"
    )
    assert client.get(std + "&v=r1280").status_code == 404


def test_the_publish_source_url_expires_quickly(photo, flask_app):
    """발행 소스는 '보내기'를 누른 순간 한 번 받고 끝난다 — 24시간 살 이유가 없다."""
    import time as _t
    with flask_app.test_request_context("/"):
        url = app_module.publish_source_url(photo, external=False)
    exp = int(url.split("e=")[1].split("&")[0])
    life = exp - int(_t.time())
    assert life <= app_module._BLOG_PUBLISH_TTL + 2
    assert life < app_module._BLOG_IMG_TTL, (
        "발행 소스 URL 이 본문 이미지만큼 오래 산다"
    )


def test_crop_dims_come_from_metadata_not_pixels(drive, photo):
    dims = app_module._crop_vision_dims(photo)
    assert dims is not None
    assert max(dims) == app_module._RENDITION_VISION_EDGE, dims
    # 비율이 원본과 같아야 한다 — compute_crop_rect 는 비율만 쓴다.
    assert abs((dims[0] / dims[1]) / (3024 / 4032) - 1) < 0.01
    assert drive.count("original") == 0, "크기 두 개를 알자고 원본을 받았다"


def test_crop_vision_image_is_a_rendition(drive, photo):
    data, ext = app_module._crop_vision_image(photo)
    assert data and ext == ".jpg"
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) <= app_module._RENDITION_VISION_EDGE
    assert len(data) < len(drive.original) / 4, "렌디션이 원본만큼 크다"
    assert drive.count("original") == 0


def test_attach_section_crops_never_fetches_the_original(drive, photo,
                                                         flask_app, monkeypatch):
    seen = {}

    def _fake_suggest(image_bytes, hint, aspect, ext=".jpg", image_url=None):
        seen["bytes"] = len(image_bytes or b"")
        seen["url"] = image_url
        return {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4} and [0.1, 0.1, 0.4, 0.4]

    monkeypatch.setattr(ai, "suggest_crop", _fake_suggest)
    result = {"blocks": [{"type": "image", "photo_index": 0}]}
    app_module._attach_section_crops(result, [photo])
    assert result["blocks"][0].get("crop"), "크롭이 안 붙었다"
    assert drive.count("original") == 0, "비전 경로가 원본을 받았다"
    assert seen["bytes"] < len(drive.original) / 4, "claude 에게 원본 크기를 보여 줬다"


# --------------------------------------------------------------------------- #
# 5. claude 에 URL 을 줄 때 — **만료 있는 서명 URL** 을 재사용한다
# --------------------------------------------------------------------------- #
def test_crop_vision_url_is_signed_and_expiring(photo, flask_app, monkeypatch):
    monkeypatch.setattr(app_module, "_CROP_VISION_SOURCE", "url")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    url = app_module._crop_vision_url(photo)
    assert url and url.startswith("https://example.test/blog-img/")
    assert "t=" in url and "e=" in url, "서명·만료가 없다"
    assert f"v=r{app_module._RENDITION_VISION_EDGE}" in url, "원본을 넘기려 한다"
    # 만료는 claude 콜 하나가 끝날 만큼만 — 하루짜리 토큰을 뿌리지 않는다.
    exp = int(url.split("e=")[1].split("&")[0])
    import time as _t
    assert 0 < exp - int(_t.time()) <= 600


def test_crop_vision_url_is_off_without_a_public_base(photo, flask_app,
                                                      monkeypatch):
    monkeypatch.setattr(app_module, "_CROP_VISION_SOURCE", "url")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert app_module._crop_vision_url(photo) is None


def test_suggest_crop_with_a_url_does_not_write_a_temp_file(monkeypatch):
    """URL 경로는 바이트를 **한 번도 안 만진다** — 임시파일도 안 만든다."""
    made = {"n": 0}
    real = ai.tempfile.mkstemp
    monkeypatch.setattr(ai.tempfile, "mkstemp",
                        lambda *a, **k: (made.__setitem__("n", made["n"] + 1)
                                         or real(*a, **k)))
    monkeypatch.setattr(ai, "_run_claude",
                        lambda p, timeout=None, allow_web=False:
                        '{"x":0.1,"y":0.1,"w":0.5,"h":0.5}')
    box = ai.suggest_crop(None, "힌트", 4 / 3, image_url="https://x/y.jpg")
    assert box == [0.1, 0.1, 0.5, 0.5]
    assert made["n"] == 0, "URL 을 줬는데 임시파일을 만들었다"


# --------------------------------------------------------------------------- #
# 6. 업로드 — file.read() 가 없다
# --------------------------------------------------------------------------- #
def test_upload_route_streams_and_never_reads_the_whole_file(
        drive, flask_app, client, monkeypatch):
    payload = _jpeg(1200, 900)
    seen = {"read_all": False, "streamed": False, "size": None}

    def _fake_upload_stream(stream, size, filename):
        seen["streamed"] = True
        seen["size"] = size
        n = 0
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            n += len(chunk)
        assert n == size
        return "item-new", "stored.jpg"

    monkeypatch.setattr(onedrive, "upload_photo_stream", _fake_upload_stream)
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "upload_photo", lambda *a, **k: (
        seen.__setitem__("read_all", True) or ("x", "y")))
    monkeypatch.setattr(app_module, "caption_photo", lambda *a, **k: None)

    r = client.post(
        "/memories/upload?ajax=1",
        data={"photos": (io.BytesIO(payload), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["ok"] is True
    assert seen["streamed"], "스트리밍 업로드를 쓰지 않았다"
    assert seen["size"] == len(payload)
    assert not seen["read_all"], "바이트를 통째로 읽는 옛 경로가 되살아났다"


def test_upload_falls_back_to_graph_for_the_taken_date(
        drive, flask_app, client, monkeypatch):
    """헤더 조각에서 EXIF 를 못 찾으면 Graph 가 읽어 둔 촬영일로 메운다."""
    from datetime import datetime

    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "upload_photo_stream",
                        lambda s, size, fn: ("item-new", "stored.jpg"))
    monkeypatch.setattr(onedrive, "get_item_meta",
                        lambda item_id, refresh=False: {
                            "taken_at": datetime(2026, 5, 4, 12, 0, 0)})
    monkeypatch.setattr(app_module, "caption_photo", lambda *a, **k: None)
    r = client.post(
        "/memories/upload?ajax=1",
        data={"photos": (io.BytesIO(_jpeg(80, 60)), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    assert r.get_json()["taken_at"].startswith("2026-05-04")


# --------------------------------------------------------------------------- #
# 7. 렌디션 검증 — 비율이 다르면 **쓰지 않는다**
# --------------------------------------------------------------------------- #
def test_std_source_can_be_switched_back_to_byte_identical(drive, photo,
                                                           monkeypatch):
    """화질이 **바이트까지** 같아야 하는 사람은 한 줄로 되돌릴 수 있다.

    기본(rendition)은 출력 해상도는 같지만 픽셀이 완전히 같지는 않다
    (사진형 PSNR 45.6 dB · 노이즈 31.0 dB — 둘 다 실측). 그 교환을 받아들이지
    않겠다면 ``BLOG_STD_SOURCE=original`` 이다.
    """
    monkeypatch.setattr(app_module, "_BLOG_STD_SOURCE", "original")
    crop = app_module.compute_crop_rect(3024, 4032, None, 4 / 3)
    data, label, target = app_module._blog_std_source(photo, crop, 1280)
    assert label == "original" and target is None
    assert data == drive.original, "원본 바이트를 그대로 쓰지 않았다"


def test_rendition_with_a_wrong_aspect_is_rejected(monkeypatch):
    """Graph 가 (문서와 달리) 잘라서 주면 조용히 쓰지 않고 None 을 돌려준다."""
    monkeypatch.setattr(onedrive, "get_rendition_meta",
                        lambda item_id, spec: {"width": 800, "height": 800,
                                               "url": "https://cdn/x"})
    monkeypatch.setattr(onedrive, "get_item_meta",
                        lambda item_id, refresh=False: {"width": 3024,
                                                        "height": 4032})
    assert onedrive.get_rendition("i", 800) is None


def test_rendition_too_small_is_rejected(monkeypatch):
    monkeypatch.setattr(onedrive, "get_rendition_meta",
                        lambda item_id, spec: {"width": 600, "height": 800,
                                               "url": "https://cdn/x"})
    monkeypatch.setattr(onedrive, "get_item_meta",
                        lambda item_id, refresh=False: {"width": 3024,
                                                        "height": 4032})
    assert onedrive.get_rendition("i", 1600, min_long_edge=1600) is None


# --------------------------------------------------------------------------- #
# 8. 발행 재료 — 서버는 **좌표만** 준다
# --------------------------------------------------------------------------- #
def test_export_payload_hands_the_browser_the_uncropped_original_plus_coords(
        drive, photo, flask_app, make_review, monkeypatch):
    review = make_review(photo_ids="[%d]" % photo.id, status="ready",
                         ai_json='{"title":"t","blocks":[{"type":"image",'
                                 '"photo_index":0,"crop":[0.1,0.2,0.5,0.4]}]}')
    with flask_app.test_request_context("/"):
        doc, images = app_module._build_export_payload(review)
    assert doc and images and len(images) == 1
    im = images[0]
    assert "v=orig" in im["url"], "발행용으로 원본이 아닌 가공본을 넘기고 있다"
    assert "c=" not in im["url"], "서버가 자른 URL 을 넘기고 있다"
    assert im["crop"] == [0.1, 0.2, 0.5, 0.4], "크롭 좌표를 안 넘겼다"
