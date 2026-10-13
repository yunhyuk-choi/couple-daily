"""📤 네이버로 보내기 — **클립보드에 사진 바이트를 담지 않는다.**

사용자 신고(2026-10-13):
    "어제 아이폰 자동화 테스트해보는데 클립보드 터지더라고? 네이버로 보내기 하니까
     계속 실패하고 그래서 안드로이드로 같은 게시물해봤는데 그것도 그래."

실측(사용자 사진 1장 3,078,870B · 3024×4032):

    크롭 없음(원본 통과) → dataUri 3.92MB → 8장이면 **31.3MB**
    4:3 크롭 q95       → dataUri 2.79MB → 8장이면 **22.4MB**
    URL+크롭 좌표만    → 8장 **8.0KB** · 30장 **13.8KB**

두 가지가 동시에 깨지고 있었다:

  (a) **안드로이드** — 클립보드는 바인더 IPC 를 타고 트랜잭션 상한이 ~1MB 라
      **한 장만으로도** 넘는다.
  (b) **iOS 사파리** — 바이트를 받아 캔버스로 굽는 **비동기** 체인이 끝난 뒤
      ``clipboard.writeText`` 를 부르면 transient activation 이 이미 풀려
      크기와 무관하게 NotAllowedError 로 거절된다.

그래서 못 박는 계약:

  1. 서버 페이로드(``_build_export_payload``)는 **URL + 크롭 좌표**만 낸다.
     ``dataUri`` 키는 어디에도 없다.
  2. 상세 화면의 ``buildExportPayload()`` 는 **동기**다 — 네트워크·캔버스·
     Promise 가 없어야 클릭 제스처 안에서 클립보드를 쓸 수 있다.
  3. 사진이 30장이어도 클립보드 페이로드는 **64KB 를 넘지 않는다.**
  4. 실패는 **조용하지 않다** — 사라지는 토스트가 아니라 남는 결과창에
     이유·크기·다음 행동을 쓴다.

⛔ 뒤집지 않는 결론: **발행된 글의 사진은 여전히 네이버 내부 이미지다.** 외부
   링크로 올린 글이 삭제된 적이 있어서 바이트를 올리게 바꾼 역사가 있는데, 그건
   *최종 글* 얘기지 *클립보드* 얘기가 아니었다. 바이트는 네이버 쪽 유저스크립트가
   이 URL 로 받아 네이버 라이브러리에 **올린다**(tools/naver-uploader.user.js).
"""
import json
import re

from models import Photo, db

HERE_TEMPLATE = "templates/review_detail.html"
USERSCRIPT = "tools/naver-uploader.user.js"

# 클립보드 예산. 안드로이드 바인더 상한(~1MB)에서 두 자릿수 배수의 여유를 둔 값이고,
# 실측상 30장이 13.8KB 라 어떤 후기도 근처에 가지 않는다. 이 숫자를 올려야 할 일이
# 생겼다면 그건 바이트가 다시 클립보드로 샜다는 뜻이다.
CLIPBOARD_BUDGET_BYTES = 64 * 1024


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _photos(couple_user, n):
    pids = []
    for i in range(n):
        p = Photo(couple_id=couple_user.couple_id, onedrive_item_id=f"item-{i}",
                  filename=f"p{i}.jpg", original_name=f"p{i}.jpg",
                  uploaded_by=couple_user.id, caption=f"사진 {i}",
                  caption_status="ready")
        db.session.add(p)
        db.session.flush()
        pids.append(p.id)
    return pids


def _review_with(make_review, couple_user, n_photos):
    pids = _photos(couple_user, n_photos)
    blocks = [{"type": "para", "text": "도입 단락이야. " * 6}]
    for i in range(n_photos):
        blocks.append({"type": "image", "photo_index": i,
                       "crop": [0.0, 0.212, 1.0, 0.563]})
        blocks.append({"type": "para", "text": "본문 단락이야. " * 8})
    ai_json = json.dumps({"title": "문래 저녁 고기집", "blocks": blocks,
                          "hashtags": ["#문래맛집"]}, ensure_ascii=False)
    return make_review(status="ready", ai_json=ai_json,
                       photo_ids=json.dumps(pids))


# --------------------------------------------------------------------------- #
# 1. 서버가 내는 재료 — URL + 좌표. 바이트 없음.
# --------------------------------------------------------------------------- #
def test_the_server_payload_carries_urls_and_crops_never_bytes(
        flask_app, couple_user, make_review):
    import app as app_module

    review = _review_with(make_review, couple_user, 3)
    with flask_app.test_request_context("/"):
        doc_data, images = app_module._build_export_payload(review)
    assert doc_data and images and len(images) == 3
    for im in images:
        assert set(im) == {"src", "url", "crop"}, (
            f"페이로드에 예상 밖 키가 있다: {sorted(im)}"
        )
        assert "dataUri" not in im
        assert im["url"].startswith("http") and "v=orig" in im["url"]
        assert im["crop"] == [0.0, 0.212, 1.0, 0.563]


# --------------------------------------------------------------------------- #
# 2. 클립보드 예산 — 30장이어도 64KB 아래
# --------------------------------------------------------------------------- #
def test_the_clipboard_payload_stays_tiny_even_with_thirty_photos(
        client, couple_user, make_review):
    """브라우저가 조립할 페이로드를 그대로 재현해 **바이트 수로** 잰다.

    `#cd-export-images`(사진마다 src·url·crop) + 발행본 HTML + `#cd-doc-data` 가
    재료의 전부다 — ``buildExportPayload()`` 가 하는 일이 이 합치기뿐이다.
    """
    review = _review_with(make_review, couple_user, 30)
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)

    def _json_block(el_id):
        m = re.search(
            rf'<script type="application/json" id="{el_id}">(.*?)</script>',
            html, re.DOTALL,
        )
        assert m, f"{el_id} 블록이 화면에 없다"
        return json.loads(m.group(1))

    specs = _json_block("cd-export-images")
    doc = _json_block("cd-doc-data")
    assert len(specs) == 30
    body = html.split('id="review-copy"', 1)[1].split("</div>", 1)[0]

    payload = {
        "copy_html": body,
        "images": [{"src": s["src"], "url": s["url"], "crop": s["crop"]}
                   for s in specs],
        "doc": doc,
    }
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert size < CLIPBOARD_BUDGET_BYTES, (
        f"사진 30장 클립보드 페이로드가 {size:,}B 로 예산 "
        f"{CLIPBOARD_BUDGET_BYTES:,}B 를 넘었다 — 바이트가 다시 샜는지 보라"
    )
    assert "dataUri" not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# 3. 화면 — 동기 빌더 · 바이트 굽는 설비 없음 · 실패가 남는다
# --------------------------------------------------------------------------- #
def test_the_export_builder_is_synchronous_and_touches_no_bytes():
    src = _read(HERE_TEMPLATE)
    body = src.split("function buildExportPayload()", 1)
    assert len(body) == 2, "buildExportPayload 가 사라졌다"
    fn = body[1].split("\n  }", 1)[0]
    for banned in ("blobToDataUri", "cdImageCrop", "dataUri", "fetch(",
                   "Promise", "await"):
        assert banned not in fn, (
            f"buildExportPayload 가 다시 비동기·바이트 경로를 탄다: {banned!r} "
            "(iOS 는 제스처가 풀려 클립보드를 거절하고 안드로이드는 상한을 넘는다)"
        )
    assert "return { copy_html: copyHtml, images: images, doc: doc };" in fn


def test_the_page_no_longer_ships_the_browser_cropper():
    src = _read(HERE_TEMPLATE)
    assert "imagecrop.js" not in src, (
        "앱 페이지가 더 이상 픽셀을 안 만지는데 크롭 설비를 싣고 있다"
    )


def test_a_failed_copy_says_why_and_hands_the_payload_over():
    src = _read(HERE_TEMPLATE)
    assert 'id="export-result"' in src and 'id="export-payload"' in src, (
        "실패가 남는 자리가 없다 — 토스트는 1.8초 뒤 사라진다"
    )
    assert "clipFailed" in src and "클립보드 복사가 막혔어" in src
    assert "KB)" in src, "실패 문구가 페이로드 크기를 말하지 않는다"
    # window.prompt 폴백은 지웠다 — 모바일에서 긴 값을 못 담는다.
    assert "window.prompt(" not in src


def test_the_pending_registration_is_not_swallowed():
    """v2 자동 경로 등록 실패를 ``.catch(function () {})`` 로 삼키지 않는다."""
    src = _read(HERE_TEMPLATE)
    mark = src.split("fetch(markUrl", 1)
    assert len(mark) == 2
    chunk = mark[1][:900]
    assert "자동 전달 등록이 안 됐어" in chunk


# --------------------------------------------------------------------------- #
# 4. 받는 쪽 — URL 모양과 옛 dataUri 모양을 **둘 다** 받는다
# --------------------------------------------------------------------------- #
def test_the_userscript_resolves_both_payload_shapes():
    src = _read(USERSCRIPT)
    assert "function resolveImageBlobs(" in src
    fn = src.split("function resolveImageBlobs(", 1)[1].split("\n  }", 1)[0]
    assert "im.dataUri" in fn, "옛 페이로드(dataUri)를 더는 못 읽는다"
    assert "fetch(im.url" in fn, "URL 모양을 못 읽는다"
    assert "cropToBlob(blob, im.crop)" in fn, "받은 뒤 자르지 않는다"
    # 붙여넣기 3경로 + 자동 경로가 **전부** 같은 해석기를 쓴다(규율 단일 원천).
    for caller in ("function process(u)", "function processDataUri(u)",
                   "function processNative(u)", "function proceed(ctx)"):
        head = src.split(caller, 1)
        assert len(head) == 2, f"{caller} 가 없다"
        assert "resolveImageBlobs(" in head[1][:3000], (
            f"{caller} 가 공용 해석기를 쓰지 않는다 — URL 페이로드를 못 받는다"
        )
