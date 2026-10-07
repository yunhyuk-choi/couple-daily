"""2026-10-07 OOM 사고 회귀 — 무엇이 512MB 를 넘기는지에 대한 계약.

사고: Render 가 "Web Service couple-daily exceeded its memory limit" 로 워커를
자동 재시작했다. 실측으로 범인을 가렸고(아래 숫자는 3.03MB · 3024×4032 아이폰
원본 기준 RSS 실측), 이 파일은 그 교정이 되돌아가지 않게 못을 박는다.

  * ``onedrive._bytes_cache``  : 개수 상한 64개 = **197MB 상주** → 바이트 예산 16MB
  * ``app._blog_proc_cache``   : 개수 상한 200개 = 최악 **656MB 상주**(hq 3.28MB/장)
                                 → 바이트 예산 48MB
  * Pillow 전체 사본 2개(exif_transpose + convert) : hq 봉우리 **143MB** → 50MB
  * ``onedrive._thumb_cache``  : 192개 가득 차도 0.83MB — 무죄(그대로 둔다)
  * ``/videos/<id>/stream``    : 50MB 를 흘려도 봉우리 +0.02MB — 무죄(제너레이터)

지키려는 계약:
  1. 캐시는 **개수가 아니라 바이트**로 묶인다(엔트리 크기가 5배 넘게 들쭉날쭉하다).
  2. 그러면서도 캐시는 여전히 **캐시로 동작한다**(히트가 난다 — 조용한 기능 약화 금지).
  3. 이미지 가공은 불필요한 전체 사본을 만들지 않는다 — 그러면서 **출력은 동일**하다.
  4. 영상 프록시는 응답을 버퍼링하지 않는다(게으른 제너레이터 + 반드시 close).
"""
import io
import os
import tempfile
import time

import pytest

import app as app_module
import bytecache
import onedrive
from models import Video, db

PIL = pytest.importorskip("PIL.Image", reason="Pillow 없으면 이미지 경로가 꺼진다")
from PIL import Image  # noqa: E402


@pytest.fixture()
def photo_bytes():
    """사진처럼 '잘 안 눌리는' 3024×4032 JPEG (단색/그라디언트는 비현실적으로 작다)."""
    w, h = 3024, 4032
    small = Image.effect_noise((w // 6, h // 6), 48).convert("RGB")
    grad = Image.linear_gradient("L").resize((w // 6, h // 6))
    small = Image.merge("RGB", (small.split()[0], grad, small.split()[2]))
    img = small.resize((w, h), Image.BICUBIC)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


@pytest.fixture(autouse=True)
def _clean_caches(tmp_path, monkeypatch):
    # 바이트 캐시 둘(가공완료·OneDrive 원본)은 **디스크**에 산다 — 테스트마다 빈
    # 임시 디렉토리로 격리한다(레포 워킹트리에는 어떤 경우에도 쓰지 않는다).
    monkeypatch.setattr(app_module._blog_proc_cache, "dir", str(tmp_path / "proc"))
    monkeypatch.setattr(onedrive._bytes_cache, "dir", str(tmp_path / "orig"))
    onedrive._bytes_ctype.clear()
    onedrive._thumb_cache.clear()
    yield
    onedrive._bytes_ctype.clear()
    onedrive._thumb_cache.clear()


def _held(cache, idx=0):
    return sum(len(v[idx]) for v in cache.values())


def _disk_held(cache=None):
    return (cache or app_module._blog_proc_cache).stats()[1]


def _disk_entries(cache=None):
    return (cache or app_module._blog_proc_cache).stats()[0]


# --------------------------------------------------------------------------- #
# 1. 가공완료 캐시 — RAM 이 아니라 디스크에 산다 (용량은 오히려 늘었다)
# --------------------------------------------------------------------------- #
def test_proc_cache_keeps_nothing_in_memory():
    """가공 바이트가 프로세스 메모리에 남으면 안 된다 — 656MB 상주의 원인이었다."""
    assert isinstance(app_module._blog_proc_cache, bytecache.DiskByteCache), (
        "가공완료 바이트가 다시 RAM(dict)으로 돌아갔다"
    )
    data = b"z" * (1024 * 1024)
    app_module._blog_proc_cache_put((1, "", "hq"), data)
    assert _disk_held() >= len(data), "디스크에 안 쓰였다"


def test_proc_cache_round_trips():
    data = b"\xff\xd8" + b"z" * 4096
    app_module._blog_proc_cache_put((1, "", "std"), data)
    assert app_module._blog_proc_cache_get((1, "", "std")) == data
    assert app_module._blog_proc_cache_get((2, "", "std")) is None
    assert app_module._blog_proc_cache_get((1, "0,0,1,1", "std")) is None  # 키 분리


def test_proc_cache_is_bounded_by_bytes_on_disk(monkeypatch):
    """예산을 넘기면 **오래된 것부터** 지운다(개수가 아니라 바이트 기준)."""
    monkeypatch.setattr(app_module._blog_proc_cache, "budget", 8 * 1024 * 1024)
    big = b"x" * (1024 * 1024)
    for i in range(40):
        app_module._blog_proc_cache_put((i, "", "hq"), big)
    assert _disk_held() <= app_module._blog_proc_cache.budget, "디스크 예산을 넘었다"
    assert _disk_entries() < 40, "축출이 일어나지 않았다"
    assert app_module._blog_proc_cache_get((39, "", "hq")) == big  # 최근 것은 산다


def test_proc_cache_holds_far_more_than_the_old_entry_cap():
    """용량을 깎은 게 아니라 자리를 옮긴 것 — 옛 상한(200개)보다 많이 담긴다."""
    std_entry = 600 * 1024  # 실측 std(1280·q92) 엔트리 크기
    fits = app_module._BLOG_PROC_DISK_BUDGET // std_entry
    assert fits > 200, (
        f"std 미리보기를 {fits}장밖에 못 담는다 — 옛 개수 상한(200)보다 적다"
    )


def test_proc_cache_entries_expire():
    data = b"old" * 100
    app_module._blog_proc_cache_put((1, "", "std"), data)
    path = app_module._blog_proc_cache.path((1, "", "std"))
    old = time.time() - app_module._BLOG_PROC_CACHE_TTL - 60
    os.utime(path, (old, old))
    assert app_module._blog_proc_cache_get((1, "", "std")) is None


def test_proc_cache_survives_an_unwritable_dir(monkeypatch):
    """디스크를 못 쓰는 환경이면 조용히 '항상 미스' — 절대 요청을 깨뜨리지 않는다."""
    monkeypatch.setattr(app_module._blog_proc_cache, "dir", "\0bad\0dir")
    app_module._blog_proc_cache_put((1, "", "std"), b"data")  # 예외 없이 통과해야
    assert app_module._blog_proc_cache_get((1, "", "std")) is None


def test_proc_cache_default_dir_is_outside_the_repo():
    """레포 워킹트리에 캐시 파일을 흘리지 않는다."""
    default = os.environ.get("BLOG_PROC_CACHE_DIR") or os.path.join(
        tempfile.gettempdir(), "couple-daily-blog-proc")
    repo = os.path.dirname(os.path.abspath(app_module.__file__))
    for d in (default, bytecache.DiskByteCache("x", 1, 1).dir):
        assert not os.path.abspath(d).startswith(os.path.abspath(repo) + os.sep)


# --------------------------------------------------------------------------- #
# 2. OneDrive 원본 바이트 캐시 — 같은 계약
# --------------------------------------------------------------------------- #
def test_bytes_cache_lives_on_disk_and_is_bounded(monkeypatch, photo_bytes):
    """원본 캐시도 RAM 이 아니라 디스크다 — 옛 '64개' 상한은 197MB 상주였다."""
    monkeypatch.setattr(onedrive._bytes_cache, "budget", 12 * 1024 * 1024)
    monkeypatch.setattr(onedrive, "get_photo_content",
                        lambda item_id: (bytes(photo_bytes), "image/jpeg"))
    for i in range(40):
        onedrive.get_photo_content_cached(f"item-{i}")
    n, held = onedrive._bytes_cache.stats()
    assert held <= onedrive._bytes_cache.budget, (
        f"원본 바이트 캐시가 예산을 넘었다: {held} > {onedrive._bytes_cache.budget}"
    )
    assert 0 < n < 40, "축출이 일어나지 않았다"


def test_bytes_cache_holds_as_many_originals_as_the_old_cap_did():
    """용량을 깎지 않았다 — 옛 개수 상한(64개)과 같은 급을 디스크에 담는다."""
    original = 3.03 * 1024 * 1024  # 실측 아이폰 12MP 원본
    fits = onedrive._BYTES_CACHE_BUDGET / original
    assert fits >= 30, f"원본을 {fits:.0f}장밖에 못 담는다"


def test_bytes_cache_still_absorbs_a_refetch_storm(monkeypatch, photo_bytes):
    """예산을 넣었다고 '캐시가 아니게' 되면 안 된다 — 같은 사진 재요청은 히트."""
    calls = {"n": 0}

    def _get(item_id):
        calls["n"] += 1
        return bytes(photo_bytes), "image/jpeg"

    monkeypatch.setattr(onedrive, "get_photo_content", _get)
    for _ in range(5):
        onedrive.get_photo_content_cached("same-item")
    assert calls["n"] == 1, "같은 아이템을 5번 요청했는데 업스트림을 여러 번 쳤다"


def test_thumb_cache_count_cap_is_left_alone():
    """썸네일은 엔트리가 균일하게 작아(실측 4.4KB) 개수 상한이 곧 바이트 상한이다."""
    assert onedrive._THUMB_CACHE_MAX == 192
    # 192개가 가득 차도 1MB 미만이라는 산수가 유지되는지(엔트리 10KB 가정도 2MB).
    assert onedrive._THUMB_CACHE_MAX * 10 * 1024 < 4 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 3. 이미지 가공 — 전체 사본을 만들지 않으면서 출력은 그대로
# --------------------------------------------------------------------------- #
def test_as_rgb_does_not_copy_an_rgb_image():
    img = Image.new("RGB", (8, 8))
    assert app_module._as_rgb(img) is img, "이미 RGB 인데 전체 사본을 만들었다"


def test_as_rgb_converts_a_non_rgb_image():
    img = Image.new("RGBA", (8, 8))
    out = app_module._as_rgb(img)
    assert out.mode == "RGB"


def test_exif_upright_does_not_copy_when_there_is_no_orientation():
    img = Image.new("RGB", (8, 8))
    assert app_module._exif_upright(img) is img


def test_blog_img_process_output_is_unchanged(photo_bytes):
    """사본을 없앴다고 출력이 달라지면 안 된다(실측: 픽스 전후 바이트 동일)."""
    std = app_module._blog_img_process(photo_bytes, max_edge=1280, quality=92)
    hq = app_module._blog_img_process(photo_bytes, max_edge=None, quality=95)
    assert std and hq
    with Image.open(io.BytesIO(std)) as im:
        assert max(im.size) == 1280 and im.mode == "RGB"
    with Image.open(io.BytesIO(hq)) as im:
        assert im.size == (3024, 4032)      # hq 는 원본 해상도 그대로
    assert len(hq) > len(std)


def test_blog_img_process_still_crops(photo_bytes):
    out = app_module._blog_img_process(
        photo_bytes, crop=[0.0, 0.25, 1.0, 0.5], max_edge=1280, quality=92)
    with Image.open(io.BytesIO(out)) as im:
        w, h = im.size
    assert w > h, "가로로 자른 크롭이 적용되지 않았다"


def test_image_display_size_is_exif_aware(photo_bytes):
    assert app_module._image_display_size(photo_bytes) == (3024, 4032)


# --------------------------------------------------------------------------- #
# 4. 영상 프록시 — 지상검증(보고가 아니라 동작으로)
# --------------------------------------------------------------------------- #
class _LazyUpstream:
    """iter_content 로만 바이트를 내는 가짜 업스트림 — 몇 청크를 뽑았는지 센다."""

    def __init__(self, total, chunk):
        self.headers = {"Content-Length": str(total)}
        self.status_code = 200
        self.total, self.chunk = total, chunk
        self.pulled = 0
        self.closed = False

    def iter_content(self, chunk_size=1):
        sent = 0
        while sent < self.total:
            n = min(chunk_size, self.total - sent)
            sent += n
            self.pulled += 1
            yield b"\0" * n

    def close(self):
        self.closed = True


def test_video_stream_is_lazy_and_closes_upstream(flask_app, couple_user,
                                                  monkeypatch):
    """50MB 영상을 중계해도 응답 본문을 통째로 만들지 않는다(제너레이터).

    보고로는 '256KB 청크라 평평하다'였지만 지상검증이 없었다. 여기서 **동작으로**
    확인한다: 몇 청크만 소비하면 업스트림도 딱 그만큼만 당겨지고, 응답을 닫으면
    업스트림 소켓이 반드시 거둬진다.
    """
    video = Video(couple_id=couple_user.couple_id, onedrive_item_id="vid-1",
                  filename="v.mp4", original_name="v.mp4",
                  uploaded_by=couple_user.id, size_bytes=50 * 1024 * 1024,
                  content_type="video/mp4")
    db.session.add(video)
    db.session.commit()

    up = _LazyUpstream(50 * 1024 * 1024, 256 * 1024)
    monkeypatch.setattr(onedrive, "open_range",
                        lambda item_id, rh=None: (200, up.headers, up))

    with flask_app.test_request_context(f"/videos/{video.id}/stream"):
        from flask import session
        session["user_id"] = couple_user.id
        resp = flask_app.view_functions["video_stream"](video.id)
        assert resp.is_streamed, "응답이 버퍼링됐다 — 50MB 가 메모리에 올라온다"
        it = resp.response.__iter__()
        first = next(it)
        assert len(first) == 256 * 1024
        assert up.pulled == 1, "한 청크만 소비했는데 업스트림을 더 당겼다"
        next(it)
        assert up.pulled == 2
        it.close()  # 끊긴 연결
    assert up.closed, "업스트림 소켓을 거두지 않았다"


def test_video_stream_passes_range_through(flask_app, couple_user, monkeypatch):
    """Range 를 서버가 직접 자르지 않고 그대로 중계한다(바이트를 메모리에 안 모은다)."""
    video = Video(couple_id=couple_user.couple_id, onedrive_item_id="vid-2",
                  filename="v.mp4", original_name="v.mp4",
                  uploaded_by=couple_user.id, size_bytes=1024,
                  content_type="video/mp4")
    db.session.add(video)
    db.session.commit()
    seen = {}

    def _open(item_id, range_header=None):
        seen["range"] = range_header
        up = _LazyUpstream(10, 10)
        return 206, {"Content-Range": "bytes 0-9/1024"}, up

    monkeypatch.setattr(onedrive, "open_range", _open)
    with flask_app.test_request_context(
            f"/videos/{video.id}/stream", headers={"Range": "bytes=0-9"}):
        from flask import session
        session["user_id"] = couple_user.id
        resp = flask_app.view_functions["video_stream"](video.id)
        assert seen["range"] == "bytes=0-9"
        assert resp.status_code == 206
        assert resp.headers["Content-Range"] == "bytes 0-9/1024"
        assert resp.headers["Accept-Ranges"] == "bytes"
