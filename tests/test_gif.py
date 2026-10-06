"""동영상 → GIF(노선 2 Step 5) — 경로 선택 · 용량 가드 · 진단 · 생성 경로 격리 테스트.

여기서 지키려는 계약:
  * **서버는 픽셀을 만들지 않는다.** 리포 어디에도 서버 ffmpeg 호출이 없고,
    Dockerfile·requirements 도 ffmpeg 를 설치하지 않는다 — 512MB·0.1 CPU 에서
    인코딩은 비현실적이라 **브라우저가 만든다**(gifmaker.py 머리말).
  * **영상 바이트는 서버 메모리에 통째로 올라오지 않는다.** 업로드는 청크 스트림,
    다운로드는 Range(206) 중계다.
  * **용량이 한도를 넘으면 막는다 — 몰래 깎지 않는다.** 클라이언트가 ``ok`` 를
    주장해도 서버가 다시 잰다(썸네일의 ``evaluate_render_check`` 와 같은 태도).
  * **조용한 실패를 만들지 않는다.** 실패에는 이름(DIAG_CODES)이 있고 저장된다.
  * **GIF 는 초안 생성 경로에 끼지 않는다.** 이미 느린(~170초) 생성에 아무것도
    얹지 않는다 — 사용자가 요청할 때만 도는 별개 경로다.
  * 네트워크·`claude` 를 타지 않는다.
"""
import hashlib
import io
import json
import os
import re

import pytest

import ai
import app as app_module
import gifmaker
import onedrive
from models import BlogReview, Couple, User, Video, db

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_READY_AI = {
    "title": "문래 저녁 고기집",
    "blocks": [{"type": "para", "text": "주말 저녁에 다녀왔어요."}],
    "hashtags": ["#문래갈매기"],
}


def _video(couple_user, name="IMG_0001.MOV", size=1234567):
    v = Video(
        couple_id=couple_user.couple_id,
        onedrive_item_id="item-" + name,
        filename=name,
        original_name=name,
        uploaded_by=couple_user.id,
        size_bytes=size,
        content_type="video/quicktime",
    )
    db.session.add(v)
    db.session.commit()
    return v


# --------------------------------------------------------------------------- #
# 1. 선행 판단이 코드에 박혀 있는가 — 서버 ffmpeg 는 **없다**
# --------------------------------------------------------------------------- #
def test_no_server_side_ffmpeg_anywhere():
    """서버에서 영상을 인코딩하는 길이 코드에도 이미지에도 없어야 한다.

    이 테스트가 깨진다는 건 누가 "서버에서 그냥 ffmpeg 쓰자"로 되돌렸다는 뜻이고,
    그건 Render 무료티어(512MB · 0.1 CPU · python:3.12-slim)에서 터지는 길이다.
    """
    for rel in ("gifmaker.py", "app.py", "onedrive.py"):
        src = io.open(os.path.join(_ROOT, rel), encoding="utf-8").read()
        # 주석/문서의 'ffmpeg' 언급은 허용하되, **실행**하는 코드가 없어야 한다.
        assert not re.search(r"subprocess\.[A-Za-z_]+\([^)]*ffmpeg", src), rel
        assert "ffmpeg -" not in src, rel
    dockerfile = io.open(os.path.join(_ROOT, "Dockerfile"), encoding="utf-8").read()
    assert "ffmpeg" not in dockerfile.lower()
    reqs = io.open(os.path.join(_ROOT, "requirements.txt"), encoding="utf-8").read()
    assert "ffmpeg" not in reqs.lower()


def test_browser_renderer_is_native_first_with_a_lazy_wasm_fallback():
    """gif.js 가 (1) 네이티브를 먼저 쓰고 (2) 실패 후에만 WASM 을 받아야 한다."""
    src = io.open(os.path.join(_ROOT, "static", "gif.js"), encoding="utf-8").read()
    # 폴백 로더는 **함수 안에서만** 호출된다(= lazy). 최상위에서 즉시 받지 않는다.
    assert "function loadDecoder()" in src
    assert src.count("loadDecoder()") >= 2       # 정의 + 폴백에서의 호출
    # 32MB 코어는 extractWasm 안에서만 불린다 — 네이티브로 끝나면 영영 안 받는다.
    assert "loadDecoder().then" in src.split("function extractWasm")[1]
    # 네이티브 경로가 먼저 시도된다는 분기가 있다.
    assert "if (nativeOK)" in src
    # 실패에 전부 이름이 있다 — 조용한 실패 금지.
    for code in gifmaker.DIAG_CODES:
        assert code in src, code
    # 폴백은 **정확 탐색**을 쓴다(-ss 가 -i 뒤). 앞에 두면 컨테이너 duration 이
    # 잘못 적힌 파일에서 프레임이 모자라게 나온다(실측: 12장 → 4장).
    wasm = src.split("function extractWasm")[1]
    assert wasm.index('"-i", name') < wasm.index('"-ss"')
    # 두 경로가 같은 해상도를 쓰도록 높이는 **가장 가까운 짝수**로 반올림한다.
    assert "2 * Math.round(srcH * (w / srcW) / 2)" in src


def test_vendored_gif_encoder_is_the_untouched_upstream_copy():
    """GIF 인코더는 **주 경로**라 CDN 이 아니라 레포에 둔다 — 사본이 온전한가.

    헤더에 적어 둔 sha256 과 본문이 일치하는지 본다. 누가 이 파일을 손으로 고치면
    여기서 깨진다(고칠 일이 있으면 원본 URL 에서 다시 받아 바꾼다).
    """
    p = os.path.join(_ROOT, "static", "vendor", "gifenc.esm.js")
    text = io.open(p, encoding="utf-8").read()
    m = re.search(r"sha256:\s*([0-9a-f]{64})", text)
    assert m, "헤더에 sha256 이 없다"
    body = text.split("*/\n", 1)[1]
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == m.group(1)


# --------------------------------------------------------------------------- #
# 2. 프리셋·추정 — 화면과 서버가 **같은 수치**를 본다
# --------------------------------------------------------------------------- #
def test_spec_exposes_the_presets_and_limits():
    s = gifmaker.spec()
    assert s["widths"] == list(gifmaker.WIDTH_PRESETS)
    assert s["fps"] == list(gifmaker.FPS_PRESETS)
    assert s["max_bytes"] == gifmaker.MAX_GIF_BYTES
    assert s["default_width"] in s["widths"]
    assert s["default_fps"] in s["fps"]
    # 프레임 상한은 UI 로 도달할 수 없어야 한다(손으로 만든 요청만 걸린다).
    assert s["max_frames"] >= gifmaker.MAX_DURATION * max(gifmaker.FPS_PRESETS)


def test_target_height_keeps_the_aspect_and_never_upscales():
    assert gifmaker.target_height(1920, 1080, 480) == 270
    # 세로 영상도 비율 그대로.
    assert gifmaker.target_height(1080, 1920, 360) == 640
    # 원본보다 크게 늘리지 않는다 — 흐려지기만 하고 용량만 는다.
    assert gifmaker.target_height(320, 240, 480) == 240
    # 홀수 높이는 **가장 가까운 짝수**로 — 폴백 디코더의 ffmpeg ``scale=W:-2`` 와
    # 같은 규칙이어야 두 경로가 1px 다른 GIF 를 내지 않는다(실측: 960×540 을 240 폭
    # 으로 줄이면 135 → ffmpeg 는 136 을 쓴다).
    assert gifmaker.target_height(960, 540, 240) == 136
    assert gifmaker.target_height(100, 61, 100) % 2 == 0
    assert gifmaker.target_height(0, 0, 480) is None
    assert gifmaker.target_height(None, "x", 480) is None


def test_estimate_grows_with_frames_and_pixels():
    small = gifmaker.estimate_bytes(240, 135, 5, 1.0)
    big = gifmaker.estimate_bytes(480, 270, 12, 3.0)
    assert big > small * 10
    assert gifmaker.frame_count(10, 2.0) == 20


def test_normalize_settings_forces_values_into_the_presets():
    """프리셋 밖의 값은 **거부가 아니라 기본값**으로 떨어진다 — 손으로 만든 요청이
    임의의 숫자로 서버 판정을 흔들 수 없게."""
    s = gifmaker.normalize_settings(
        {"video_id": "7", "start": "3.456", "duration": 99, "width": 4000, "fps": 60}
    )
    assert s["video_id"] == 7
    assert s["width"] == gifmaker.DEFAULT_WIDTH
    assert s["fps"] == gifmaker.DEFAULT_FPS
    assert s["duration"] == gifmaker.MAX_DURATION          # 길이 상한으로 잘린다
    assert s["start"] == 3.46
    # 쓰레기 입력도 죽지 않고 기본값으로.
    bad = gifmaker.normalize_settings({"start": "abc", "duration": None, "video_id": "x"})
    assert bad["video_id"] is None
    assert bad["duration"] == gifmaker.DEFAULT_DURATION
    assert bad["start"] == 0.0
    # 영상 길이를 주면 시작점이 그 안으로 갇힌다.
    assert gifmaker.normalize_settings({"start": 50}, max_start=4.0)["start"] == 4.0
    assert gifmaker.normalize_settings(None)["width"] == gifmaker.DEFAULT_WIDTH


# --------------------------------------------------------------------------- #
# 3. 용량 가드 — **클라이언트의 주장을 읽지 않는다**
# --------------------------------------------------------------------------- #
def test_result_under_the_limit_passes():
    r = gifmaker.evaluate_result(
        {"bytes": 1_200_000, "width": 360, "height": 202, "frames": 20,
         "fps": 10, "duration": 2.0, "path": "native"}
    )
    assert r["ok"] is True
    assert r["over_by"] == 0
    assert r["path"] == "native"


def test_result_over_the_limit_is_blocked_and_never_quietly_downgraded():
    """한도를 넘으면 ``ok=False`` 로 남는다. **여기서 화질을 낮춘 재인코딩을 지시하는
    길이 없다** — 줄이는 건 사람이다(썸네일이 폰트 대신 문구를 줄인 것과 같은 규율)."""
    size = gifmaker.MAX_GIF_BYTES + 2_000_000
    r = gifmaker.evaluate_result(
        {"bytes": size, "width": 480, "height": 270, "frames": 36,
         "fps": 12, "duration": 3.0, "path": "wasm", "ok": True}  # 클라가 ok 주장
    )
    assert r["ok"] is False                      # 주장은 읽지 않는다
    assert r["over_by"] == 2_000_000
    # 결과 dict 어디에도 "다시 이렇게 깎아라"가 없다.
    assert not any("quality" in k or "retry" in k for k in r)
    src = io.open(os.path.join(_ROOT, "gifmaker.py"), encoding="utf-8").read()
    assert "re-encode" not in src.lower()


def test_result_rejects_junk_and_absurd_frame_counts():
    assert gifmaker.evaluate_result(None) is None
    assert gifmaker.evaluate_result({}) is None
    assert gifmaker.evaluate_result({"bytes": 0, "width": 1, "height": 1,
                                     "frames": 1}) is None
    assert gifmaker.evaluate_result(
        {"bytes": 10, "width": 10, "height": 10,
         "frames": gifmaker.MAX_FRAMES + 1}) is None
    # 모르는 경로 이름은 저장하지 않는다(None 으로 남는다).
    r = gifmaker.evaluate_result({"bytes": 10, "width": 10, "height": 10,
                                  "frames": 1, "path": "magic"})
    assert r["path"] is None


# --------------------------------------------------------------------------- #
# 4. 진단 — 실패에 **이름이 있다**
# --------------------------------------------------------------------------- #
def test_diag_only_accepts_known_codes():
    assert gifmaker.evaluate_diag({"code": "native-ok"})["code"] == "native-ok"
    got = gifmaker.evaluate_diag(
        {"code": "decode-failed", "detail": "  이 기기에 \n코덱이 없어 ",
         "src_width": "1920", "src_height": 1080}
    )
    assert got["detail"] == "이 기기에 코덱이 없어"
    assert got["src_width"] == 1920
    # 모르는 코드를 저장하면 나중에 화면이 '아무 말도 못 하는' 상태가 된다.
    assert gifmaker.evaluate_diag({"code": "그냥 안 됨"}) is None
    assert gifmaker.evaluate_diag("nope") is None


def test_apply_state_drops_a_stale_result_when_the_settings_change():
    s1 = gifmaker.normalize_settings({"video_id": 1, "duration": 2, "width": 360, "fps": 10})
    st = gifmaker.apply_state({}, settings=s1,
                              result={"ok": True, "bytes": 10})
    assert st["result"]["bytes"] == 10
    s2 = gifmaker.normalize_settings({"video_id": 1, "duration": 3, "width": 360, "fps": 10})
    st = gifmaker.apply_state(st, settings=s2)
    assert "result" not in st        # 옛 용량으로 "통과"라고 말하지 않는다
    # 같은 설정을 다시 보내면 결과는 살아 있다.
    st = gifmaker.apply_state(st, result={"ok": True, "bytes": 11})
    st = gifmaker.apply_state(st, settings=s2)
    assert st["result"]["bytes"] == 11


def test_allowed_video_types():
    assert gifmaker.is_allowed_video("IMG_1234.MOV")
    assert gifmaker.is_allowed_video("clip.mp4")
    assert gifmaker.is_allowed_video("noext", "video/mp4")   # 확장자 없는 안드로이드
    assert not gifmaker.is_allowed_video("photo.jpg", "image/jpeg")
    assert gifmaker.content_type_for("x.MOV") == "video/quicktime"
    assert gifmaker.content_type_for("weird") == "video/mp4"


# --------------------------------------------------------------------------- #
# 5. 업로드 — **바이트를 통째로 읽지 않는다**
# --------------------------------------------------------------------------- #
def test_upload_streams_instead_of_reading_the_whole_file(client, flask_app,
                                                          couple_user, monkeypatch):
    seen = {}

    def fake_upload_stream(stream, size, filename):
        # 스트림 객체를 받았는가(= bytes 가 아니라) — 그게 이 경로의 요점이다.
        assert hasattr(stream, "read") and not isinstance(stream, (bytes, bytearray))
        seen["size"] = size
        seen["read"] = len(stream.read())
        return "item-x", "stored.mov"

    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "upload_stream", fake_upload_stream)
    data = {"video": (io.BytesIO(b"\x00" * 5000), "IMG_9.MOV")}
    r = client.post("/videos/upload", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert seen["size"] == 5000 and seen["read"] == 5000
    row = Video.query.filter_by(couple_id=couple_user.couple_id).one()
    assert row.onedrive_item_id == "item-x"
    assert row.size_bytes == 5000
    assert row.original_name == "IMG_9.MOV"


def test_upload_rejects_non_videos_and_empty_files(client, flask_app, monkeypatch):
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "upload_stream",
                        lambda *a: pytest.fail("업로드가 호출되면 안 된다"))
    r = client.post("/videos/upload",
                    data={"video": (io.BytesIO(b"x" * 10), "a.jpg")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"] == "bad_type"
    r = client.post("/videos/upload",
                    data={"video": (io.BytesIO(b""), "a.mp4")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"] == "empty"
    assert Video.query.count() == 0


def test_upload_refuses_a_video_bigger_than_the_cap(client, flask_app, monkeypatch):
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(gifmaker, "MAX_VIDEO_BYTES", 100)
    monkeypatch.setattr(onedrive, "upload_stream",
                        lambda *a: pytest.fail("업로드가 호출되면 안 된다"))
    r = client.post("/videos/upload",
                    data={"video": (io.BytesIO(b"x" * 500), "a.mp4")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"] == "too_large"


# --------------------------------------------------------------------------- #
# 6. 스트리밍 — Range(206) 중계. **전체를 메모리에 올리지 않는다**
# --------------------------------------------------------------------------- #
class _FakeUpstream:
    """``requests`` 의 stream=True 응답 흉내. 닫혔는지까지 본다."""

    def __init__(self, payload, chunks=3):
        self.payload = payload
        self.chunks = chunks
        self.closed = False
        self.iterated = 0

    def iter_content(self, chunk_size=None):
        step = max(1, len(self.payload) // self.chunks)
        for i in range(0, len(self.payload), step):
            self.iterated += 1
            yield self.payload[i:i + step]

    def close(self):
        self.closed = True


def test_stream_relays_the_range_header_and_the_206(client, flask_app,
                                                    couple_user, monkeypatch):
    video = _video(couple_user)
    seen = {}
    up = _FakeUpstream(b"BCDEF")

    def fake_open_range(item_id, range_header=None):
        seen["item"] = item_id
        seen["range"] = range_header
        return 206, {"Content-Range": "bytes 1-5/100", "Content-Length": "5"}, up

    monkeypatch.setattr(onedrive, "open_range", fake_open_range)
    r = client.get(f"/videos/{video.id}/stream", headers={"Range": "bytes=1-5"})
    body = r.get_data()
    assert r.status_code == 206
    # Range 는 **그대로** OneDrive 로 건너간다 — 서버가 직접 자르지 않는다.
    assert seen["range"] == "bytes=1-5"
    assert seen["item"] == video.onedrive_item_id
    assert r.headers["Content-Range"] == "bytes 1-5/100"
    assert r.headers["Accept-Ranges"] == "bytes"
    assert body == b"BCDEF"
    assert up.iterated > 1      # 청크로 흘렀다(한 덩어리로 읽지 않았다)
    assert up.closed            # 소켓을 거뒀다


def test_stream_without_a_range_is_a_plain_200(client, flask_app, couple_user,
                                               monkeypatch):
    video = _video(couple_user)
    up = _FakeUpstream(b"abcdef")
    monkeypatch.setattr(onedrive, "open_range",
                        lambda i, h=None: (200, {"Content-Length": "6"}, up))
    r = client.get(f"/videos/{video.id}/stream")
    assert r.status_code == 200
    assert r.get_data() == b"abcdef"
    assert r.headers["Accept-Ranges"] == "bytes"


def test_stream_is_couple_scoped(client, flask_app, couple_user, monkeypatch):
    other = Couple(invite_code="OTHER")
    db.session.add(other)
    db.session.flush()
    stranger = User(email="b@example.com", password_hash="x", display_name="남",
                    couple_id=other.id, status="approved")
    db.session.add(stranger)
    db.session.flush()
    v = Video(couple_id=other.id, onedrive_item_id="x", filename="x.mp4",
              uploaded_by=stranger.id, size_bytes=1)
    db.session.add(v)
    db.session.commit()
    monkeypatch.setattr(onedrive, "open_range",
                        lambda *a, **k: pytest.fail("남의 영상을 열면 안 된다"))
    assert client.get(f"/videos/{v.id}/stream").status_code == 404
    assert client.get(f"/videos/{v.id}/source").status_code == 404
    assert client.post(f"/videos/{v.id}/delete").status_code == 404


def test_stream_404s_when_the_item_is_gone(client, flask_app, couple_user,
                                           monkeypatch):
    video = _video(couple_user)
    monkeypatch.setattr(onedrive, "open_range", lambda *a, **k: (404, {}, None))
    assert client.get(f"/videos/{video.id}/stream").status_code == 404


# --------------------------------------------------------------------------- #
# 7. 바이트 경로 (a) vs (b) — **주장이 아니라 실측으로** 고른다
# --------------------------------------------------------------------------- #
def test_source_picks_direct_only_when_cors_is_actually_allowed(
        client, flask_app, couple_user, monkeypatch):
    video = _video(couple_user)
    calls = []
    monkeypatch.setattr(
        onedrive, "probe_direct_cors",
        lambda item, origin: calls.append(origin) or
        {"ok": True, "acao": "*", "range": True, "status": 206},
    )
    monkeypatch.setattr(onedrive, "get_download_url",
                        lambda item: "https://cdn.example/x?token=zzz")
    j = client.get(f"/videos/{video.id}/source").get_json()
    assert j["mode"] == "direct"
    assert j["url"].startswith("https://cdn.example/")
    assert len(calls) == 1
    # 두 번째 호출은 행에 캐시된 실측을 쓴다 — 영상당 한 번만 잰다.
    client.get(f"/videos/{video.id}/source")
    assert len(calls) == 1
    db.session.expire_all()
    assert db.session.get(Video, video.id).cors_probe_data["acao"] == "*"


def test_source_falls_back_to_the_proxy_and_never_leaks_the_signed_url(
        client, flask_app, couple_user, monkeypatch):
    """ACAO 가 없으면 캔버스가 오염돼 프레임 추출이 *아예* 막힌다 → (b) 프록시.

    그리고 **쓰지도 못할 다운로드 URL 을 DOM 으로 흘리지 않는다** — 그 URL 은
    쿼리에 토큰이 박힌 자격증명이다.
    """
    video = _video(couple_user)
    monkeypatch.setattr(onedrive, "probe_direct_cors",
                        lambda item, origin: {"ok": False, "acao": None,
                                              "range": True, "status": 206})
    monkeypatch.setattr(onedrive, "get_download_url",
                        lambda item: pytest.fail("폴백에서 URL 을 캐면 안 된다"))
    j = client.get(f"/videos/{video.id}/source").get_json()
    assert j["mode"] == "proxy"
    assert j["url"] == f"/videos/{video.id}/stream"
    assert "token" not in json.dumps(j)
    # 실측 결과는 남는다 — 왜 (b)로 떨어졌는지가 데이터로 남아야 한다.
    db.session.expire_all()
    assert db.session.get(Video, video.id).cors_probe_data["ok"] is False


def test_source_falls_back_when_the_probe_itself_fails(client, flask_app,
                                                       couple_user, monkeypatch):
    video = _video(couple_user)
    monkeypatch.setattr(onedrive, "probe_direct_cors", lambda item, origin: None)
    j = client.get(f"/videos/{video.id}/source").get_json()
    assert j["mode"] == "proxy"


def test_source_needs_range_support_too(client, flask_app, couple_user, monkeypatch):
    """CORS 가 되더라도 206 이 안 되면 seek 이 안 된다 → 직접 경로를 쓰지 않는다."""
    video = _video(couple_user)
    monkeypatch.setattr(onedrive, "probe_direct_cors",
                        lambda item, origin: {"ok": True, "acao": "*",
                                              "range": False, "status": 200})
    assert client.get(f"/videos/{video.id}/source").get_json()["mode"] == "proxy"


# --------------------------------------------------------------------------- #
# 8. 상태 저장 — 서버가 다시 잰다
# --------------------------------------------------------------------------- #
def test_state_saves_settings_result_and_diag(client, flask_app, couple_user,
                                              make_review):
    video = _video(couple_user)
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready")
    r = client.post(
        f"/reviews/{review.id}/gif/state",
        json={
            "settings": {"video_id": video.id, "start": 1.5, "duration": 2,
                         "width": 360, "fps": 10},
            "result": {"bytes": 900_000, "width": 360, "height": 202,
                       "frames": 20, "fps": 10, "duration": 2, "path": "native"},
            "diag": {"code": "native-ok"},
        },
    )
    assert r.status_code == 200 and r.get_json()["size_ok"] is True
    db.session.expire_all()
    state = db.session.get(BlogReview, review.id).gif
    assert state["settings"]["video_id"] == video.id
    assert state["result"]["ok"] is True
    assert state["result"]["made_at"]
    assert state["diag"]["code"] == "native-ok"


def test_state_blocks_an_oversized_gif_even_if_the_client_says_it_is_fine(
        client, flask_app, couple_user, make_review):
    video = _video(couple_user)
    review = make_review(status="ready")
    big = gifmaker.MAX_GIF_BYTES + 1
    r = client.post(
        f"/reviews/{review.id}/gif/state",
        json={"settings": {"video_id": video.id, "duration": 3, "width": 480,
                           "fps": 12},
              "result": {"bytes": big, "width": 480, "height": 270, "frames": 36,
                         "fps": 12, "duration": 3, "path": "native", "ok": True}},
    )
    j = r.get_json()
    assert j["size_ok"] is False and j["over_by"] == 1
    db.session.expire_all()
    assert db.session.get(BlogReview, review.id).gif["result"]["ok"] is False


def test_state_refuses_someone_elses_video(client, flask_app, couple_user,
                                           make_review):
    other = Couple(invite_code="OTHER2")
    db.session.add(other)
    db.session.flush()
    stranger = User(email="c@example.com", password_hash="x", display_name="남2",
                    couple_id=other.id, status="approved")
    db.session.add(stranger)
    db.session.flush()
    v = Video(couple_id=other.id, onedrive_item_id="x", filename="x.mp4",
              uploaded_by=stranger.id, size_bytes=1)
    db.session.add(v)
    db.session.commit()
    review = make_review(status="ready")
    r = client.post(f"/reviews/{review.id}/gif/state",
                    json={"settings": {"video_id": v.id}})
    assert r.status_code == 400 and r.get_json()["error"] == "bad_video"


def test_state_refuses_unknown_diag_codes_and_junk_results(client, flask_app,
                                                           make_review):
    review = make_review(status="ready")
    assert client.post(f"/reviews/{review.id}/gif/state",
                       json={"diag": {"code": "뭔가 이상함"}}).status_code == 400
    assert client.post(f"/reviews/{review.id}/gif/state",
                       json={"result": {"bytes": -1}}).status_code == 400


def test_state_is_couple_scoped(client, flask_app, make_review):
    other = Couple(invite_code="OTHER3")
    db.session.add(other)
    db.session.flush()
    stranger = User(email="d@example.com", password_hash="x", display_name="남3",
                    couple_id=other.id, status="approved")
    db.session.add(stranger)
    db.session.flush()
    foreign = BlogReview(couple_id=other.id, created_by=stranger.id, topic="남의 후기",
                         prose="x", overall_score=5)
    db.session.add(foreign)
    db.session.commit()
    assert client.post(f"/reviews/{foreign.id}/gif/state", json={}).status_code == 404


# --------------------------------------------------------------------------- #
# 9. 생성 경로 격리 — GIF 가 초안 생성 시간(~170초)을 **건드리지 않는다**
# --------------------------------------------------------------------------- #
def test_draft_generation_never_touches_the_gif_path(flask_app, make_review,
                                                     monkeypatch):
    """초안 생성에 claude 호출도, OneDrive 영상 호출도 **하나도 더 얹지 않는다.**"""
    monkeypatch.setattr(ai, "write_review", lambda *a, **k: dict(_READY_AI))
    monkeypatch.setattr(app_module.keyword_research, "research",
                        lambda *a, **k: {"status": "skipped"})
    for name in ("upload_stream", "open_range", "probe_direct_cors"):
        monkeypatch.setattr(onedrive, name,
                            lambda *a, **k: pytest.fail(f"{name} 가 초안 경로에서 돌면 안 된다"))
    review = make_review(status="pending")
    rid = review.id
    app_module.generate_review(flask_app, rid)
    db.session.expire_all()
    row = db.session.get(BlogReview, rid)
    assert row.status == "ready"
    assert row.gif_json is None          # 초안은 GIF 상태를 만들지 않는다


def test_the_gif_path_never_runs_claude(client, flask_app, couple_user,
                                        make_review, monkeypatch):
    """GIF 는 AI 기능이 아니다 — 이 경로 어디에도 `claude` 가 없다."""
    monkeypatch.setattr(
        ai, "_run_claude",
        lambda *a, **k: pytest.fail("claude must never run in the gif path"),
    )
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "upload_stream", lambda *a: ("i", "n.mov"))
    client.post("/videos/upload",
                data={"video": (io.BytesIO(b"x" * 99), "a.mp4")},
                content_type="multipart/form-data")
    review = make_review(status="ready")
    r = client.post(f"/reviews/{review.id}/gif/state",
                    json={"diag": {"code": "native-ok"}})
    assert r.status_code == 200


def test_review_detail_renders_the_gif_card_without_touching_onedrive(
        client, flask_app, couple_user, make_review, monkeypatch):
    """상세 페이지 렌더는 **DB 읽기뿐**이다 — 영상 목록 때문에 네트워크를 기다리지 않는다."""
    _video(couple_user, name="clip.mp4")
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    for name in ("open_range", "probe_direct_cors", "get_download_url",
                 "get_photo_content_cached"):
        monkeypatch.setattr(onedrive, name,
                            lambda *a, **k: pytest.fail(f"{name} 가 렌더에서 돌면 안 된다"))
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready")
    r = client.get(f"/reviews/{review.id}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="rv-gif"' in html
    assert "clip.mp4" in html
    assert "/static/gif.js" in html
