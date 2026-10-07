"""화면 미리보기 ≠ 발행본 — **둘을 가른다.**

사용자 지적(2026-10-07):
    "애초에 본문 미리보기에선 왜 서명 url 을 쓰는 거지? 저것도 미리보긴데?
     다른 사진들하고 똑같은 썸네일 쓰면 되는 거 아니야? 발행 버튼 눌렀을 때만
     실제 서명 url 로 배선하고?"

서명·만료가 필요한 이유는 **앱 밖(네이버)에서 열려야 하기 때문**이다. 로그인한
본인이 자기 화면에서 보는 그림에는 필요 없다. 그 결정 때문에 ``/blog-img`` 가
터지자 **본문 사진만** 전부 엑박이 됐다(사진 목록·크롭 UI 는 멀쩡했다).

여기서 못 박는 계약:

  1. 상세 화면의 본문 미리보기에는 ``/blog-img`` **src 가 하나도 없다** — 사진
     목록·크롭 UI 와 같은 미리보기 자산(고정 URL·서명 없음·만료 없음)을 쓴다.
  2. 그 자리의 **발행 URL 은 사라지지 않는다** — ``data-cd-pub`` 에 그대로 실려
     가고, 브라우저가 저장·복사·발행할 때 그 값으로 발행본을 되돌린다.
     (저장 형식 ``edited_text`` 는 1바이트도 안 바뀐다.)
  3. 발행 경로는 그대로 **서명·만료 + v=orig 원본 + 좌표**다(화질 후퇴 없음).
  4. 미리보기를 그리는 동안 서버는 **픽셀을 한 번도 디코드하지 않는다.**
"""
import io
import json
import re

import pytest

import app as app_module
import onedrive
import previews
from models import BlogReview, Photo, db

PIL = pytest.importorskip("PIL.Image", reason="Pillow 없으면 이미지 경로가 꺼진다")
from PIL import Image  # noqa: E402


def _jpeg(w, h):
    img = Image.new("RGB", (w, h), (90, 120, 140))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


class _Drive:
    """가짜 드라이브 — '원본을 받았나'를 센다."""

    def __init__(self):
        self.w, self.h = 3024, 4032
        self.original = _jpeg(600, 800)
        self.calls = []

    def get_item_meta(self, item_id, refresh=False):
        self.calls.append("meta")
        return {"id": item_id, "name": "p.jpg", "size": len(self.original),
                "ctype": "image/jpeg", "width": self.w, "height": self.h,
                "taken_at": None, "etag": "e1"}

    def get_rendition(self, item_id, long_edge, min_long_edge=None,
                      expect_aspect=None):
        self.calls.append(f"rendition:{long_edge}")
        edge = int(long_edge)
        scale = min(1.0, edge / max(self.w, self.h))
        w, h = max(1, round(self.w * scale)), max(1, round(self.h * scale))
        if min_long_edge and max(w, h) < int(min_long_edge):
            return None
        return _jpeg(w, h), "image/jpeg", (w, h)

    def get_photo_content(self, item_id):
        self.calls.append("original")
        return self.original, "image/jpeg"

    def get_photo_content_cached(self, item_id):
        return self.get_photo_content(item_id)

    def count(self, kind):
        return sum(1 for c in self.calls if c == kind)


@pytest.fixture()
def drive(monkeypatch, tmp_path):
    d = _Drive()
    for name in ("get_item_meta", "get_rendition", "get_photo_content",
                 "get_photo_content_cached"):
        monkeypatch.setattr(onedrive, name, getattr(d, name))
    monkeypatch.setattr(app_module._blog_proc_cache, "dir", str(tmp_path / "proc"))
    return d


@pytest.fixture()
def ready_review(couple_user, make_review):
    """사진 2장짜리 완성 초안 — 한 장은 크롭 있고 한 장은 없다(옛 후기 모사)."""
    pids = []
    for i in (1, 2):
        p = Photo(couple_id=couple_user.couple_id, onedrive_item_id=f"item-{i}",
                  filename=f"p{i}.jpg", original_name=f"p{i}.jpg",
                  uploaded_by=couple_user.id, caption=f"사진 {i}",
                  caption_status="ready")
        db.session.add(p)
        db.session.flush()
        pids.append(p.id)
    ai_json = json.dumps({
        "title": "문래 저녁 고기집",
        "blocks": [
            {"type": "para", "text": "도입 단락이야."},
            {"type": "image", "photo_index": 0, "crop": [0.0, 0.212, 1.0, 0.563]},
            {"type": "para", "text": "본문 단락이야."},
            {"type": "image", "photo_index": 1},
            {"type": "para", "text": "총평이야."},
        ],
        "hashtags": ["#문래맛집"],
    }, ensure_ascii=False)
    review = make_review(status="ready", ai_json=ai_json,
                         photo_ids=json.dumps(pids))
    return review


# --------------------------------------------------------------------------- #
# 1. 변환 자체 — 서명 URL 이 미리보기 자산으로, 발행 URL 은 data-cd-pub 으로
# --------------------------------------------------------------------------- #
def test_the_screen_html_swaps_the_signed_url_for_an_owned_asset(flask_app):
    pub = ("/blog-img/7?e=123&amp;t=abc&amp;c=0,0.212,1,0.563")
    html = (f'<p style="text-align:center;"><img src="{pub}" alt="사진 하나" '
            'style="max-width:100%;"></p>')
    with flask_app.test_request_context("/"):
        out = app_module._review_screen_html(html)
    assert "/blog-img/" not in re.sub(r'data-cd-pub="[^"]*"', "", out), (
        "화면본에 아직 서명 URL 이 src 로 남아 있다"
    )
    assert f"/memories/7/preview?r={previews.REV}" in out
    assert f'data-cd-pub="{pub}"' in out, "발행 URL 을 잃어버렸다 — 되돌릴 수 없다"
    assert 'data-cd-alt="사진 하나"' in out
    # 문단·스타일은 그대로 — <img> 하나만 바꾼다.
    assert '<p style="text-align:center;">' in out


def test_a_cropped_photo_is_css_cropped_not_server_cropped(flask_app):
    """크롭은 CSS 로 — 서버가 픽셀을 자르지 않는다. 좌표 수학을 못 박는다."""
    pub = "/blog-img/7?e=1&amp;t=a&amp;c=0.25,0.1,0.5,0.375"
    with flask_app.test_request_context("/"):
        out = app_module._review_screen_html(f'<img src="{pub}" alt="x">')
    # x=0.25 w=0.5 → left = -0.25/0.5 = -50% · width = 1/0.5 = 200%
    assert "left:-50.0000%" in out
    assert "width:200.0000%" in out
    # y=0.1 h=0.375 → top = -0.1/0.375 = -26.6667%
    assert "top:-26.6667%" in out
    assert "aspect-ratio:1.333333" in out
    assert "overflow:hidden" in out


def test_an_old_photo_without_a_crop_stays_a_plain_image(flask_app):
    with flask_app.test_request_context("/"):
        out = app_module._review_screen_html(
            '<img src="/blog-img/9?e=1&amp;t=a" alt="옛 사진">'
        )
    assert "<span" not in out and "aspect-ratio" not in out
    assert f"/memories/9/preview?r={previews.REV}" in out
    assert 'data-cd-pub="/blog-img/9?e=1&amp;t=a"' in out


def test_images_that_are_not_ours_are_left_alone(flask_app):
    html = '<img src="https://example.test/x.jpg" alt="남의 그림">'
    with flask_app.test_request_context("/"):
        assert app_module._review_screen_html(html) == html


def test_the_screen_html_never_raises(flask_app):
    assert app_module._review_screen_html("") == ""
    assert app_module._review_screen_html(None) is None


# --------------------------------------------------------------------------- #
# 2. 화면 — 본문 미리보기에 서명 URL 이 없고, 서버는 픽셀을 안 만진다
# --------------------------------------------------------------------------- #
def _body(client, review):
    return client.get(f"/reviews/{review.id}").get_data(as_text=True)


def test_the_detail_page_preview_uses_no_signed_urls(client, drive,
                                                     ready_review):
    html = _body(client, ready_review)
    box = html.split('id="review-copy"', 1)[1].split("</div>", 1)[0]
    # src 로 쓰인 /blog-img 가 없어야 한다(data-cd-pub 에는 있어야 한다).
    srcs = re.findall(r'<img[^>]*\bsrc="([^"]+)"', box)
    assert srcs, "미리보기에 이미지가 하나도 없다"
    assert not any("/blog-img/" in s for s in srcs), (
        f"미리보기가 아직 서명 URL 을 쓴다: {srcs}"
    )
    assert all("/memories/" in s and "/preview" in s for s in srcs), srcs
    assert "data-cd-pub=" in box, "발행 URL 을 들고 가지 않는다 — 복사가 깨진다"


def test_rendering_the_preview_decodes_no_pixels_on_the_server(
        client, drive, ready_review, monkeypatch):
    """미리보기를 그리는 동안 서버는 Pillow 를 한 번도 열지 않는다."""
    decoded = []
    real = app_module._blog_img_process
    monkeypatch.setattr(
        app_module, "_blog_img_process",
        lambda *a, **k: (decoded.append(1), real(*a, **k))[1],
    )
    _body(client, ready_review)
    assert decoded == [], "상세 화면 렌더가 서버에서 픽셀을 디코드했다"
    assert drive.count("original") == 0, "상세 화면 렌더가 원본을 받았다"


def test_the_preload_hint_points_at_the_asset_not_the_signed_url(
        client, drive, ready_review):
    html = _body(client, ready_review)
    m = re.search(r'<link rel="preload" as="image"[^>]*href="([^"]+)"', html)
    assert m, "첫 사진 preload 힌트가 사라졌다"
    assert "/blog-img/" not in m.group(1), m.group(1)


# --------------------------------------------------------------------------- #
# 3. 발행 경로는 그대로 — 서명·만료 + v=orig 원본 + 좌표
# --------------------------------------------------------------------------- #
def test_the_publish_payload_still_uses_signed_originals(flask_app,
                                                         ready_review):
    with flask_app.test_request_context("/"):
        doc, images = app_module._build_export_payload(ready_review)
    assert doc and images and len(images) == 2
    for im in images:
        assert "/blog-img/" in im["url"] and "v=orig" in im["url"], (
            "발행 소스가 원본 서명 URL 이 아니다 — 화질이 떨어진다"
        )
        assert "t=" in im["url"] and "e=" in im["url"], "서명·만료가 사라졌다"
    assert images[0]["crop"] == [0.0, 0.212, 1.0, 0.563], "크롭 좌표를 잃었다"
    assert images[1]["crop"] is None


def test_saved_text_keeps_the_publish_format(client, drive, ready_review):
    """사람이 저장한 본문(발행본)은 다음 렌더에서 **다시 화면본으로** 바뀐다.

    저장 형식이 안 바뀌었다는 뜻이다 — 북마클릿 매칭·토큰 재서명이 그대로 산다.
    """
    saved = ('<p>내가 고친 문장</p>'
             '<p><img src="/blog-img/{pid}?e=1&amp;t=zz&amp;c=0,0.2,1,0.5" '
             'alt="사진 1" style="max-width:100%;"></p>')
    pid = json.loads(ready_review.photo_ids)[0]
    ready_review.edited_text = saved.format(pid=pid)
    db.session.commit()
    box = _body(client, ready_review).split('id="review-copy"', 1)[1]
    assert "내가 고친 문장" in box, "편집본이 날아갔다"
    assert f"/memories/{pid}/preview" in box, "저장본이 화면본으로 안 바뀌었다"
    assert "data-cd-pub=" in box
    # 저장된 원문은 손대지 않는다.
    row = db.session.get(BlogReview, ready_review.id)
    assert "/blog-img/" in row.edited_text
