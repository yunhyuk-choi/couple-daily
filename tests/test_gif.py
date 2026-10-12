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
import logging
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
# 5. 업로드 — **바이트가 서버를 지나지 않는다** (브라우저 → OneDrive 직접)
# --------------------------------------------------------------------------- #
# 예전엔 브라우저가 파일을 우리 Flask 로 POST 하고 워커가 그걸 다시 OneDrive 로
# 청크 PUT 했다. 그래서 요청 본문 상한(55MB)·영상 한도(50MB)가 걸렸고 업로드 내내
# 워커 하나가 묶였다. 지금은 서버가 **업로드 세션만 발급**하고, 브라우저가 Graph 의
# 사전 인증 URL 로 직접 PUT 한다(2026-10-12 CORS 실측에 근거 — gif.js 머리말).
_FAKE_SESSION_URL = "https://api.onedrive.com/rup/CAPABILITY-TOKEN-DO-NOT-LEAK"


def _arm_session(monkeypatch, name="20261012_abcd1234_IMG_9.MOV", seen=None):
    """세션 발급을 가짜로 끼우고, 바이트 경로가 안 돌았음을 못질한다."""
    def fake_session(filename):
        if seen is not None:
            seen["filename"] = filename
        return _FAKE_SESSION_URL, name

    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "create_upload_session", fake_session)
    monkeypatch.setattr(onedrive, "upload_stream",
                        lambda *a, **k: pytest.fail(
                            "영상 바이트가 서버를 지나면 안 된다"))
    return name


def test_no_route_receives_video_bytes_anymore():
    """바이트를 받던 자리는 **없어졌다** — 남아 있으면 옛 한도가 같이 돌아온다."""
    rules = {r.rule for r in app_module.app.url_map.iter_rules()}
    assert "/videos/upload" not in rules, "바이트를 받는 라우트가 되살아났다"
    assert "/videos/upload-session" in rules
    assert "/videos/complete" in rules
    src = io.open(os.path.join(_ROOT, "app.py"), encoding="utf-8").read()
    assert 'request.files.get("video")' not in src


def test_session_endpoint_issues_a_url_and_takes_no_bytes(client, flask_app,
                                                          monkeypatch):
    seen = {}
    _arm_session(monkeypatch, seen=seen)
    r = client.post("/videos/upload-session",
                    json={"name": "IMG_9.MOV", "size": 4 * 1024 ** 3,
                          "type": "video/quicktime"},
                    headers={"X-Requested-With": "fetch"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True
    assert j["upload_url"] == _FAKE_SESSION_URL
    assert seen["filename"] == "IMG_9.MOV"
    # Graph 업로드 세션 규약: 마지막을 뺀 모든 청크는 320KiB 의 배수.
    assert j["chunk_size"] == onedrive.UPLOAD_CHUNK_BYTES
    assert j["chunk_size"] % (320 * 1024) == 0
    # 아직 아무것도 올라가지 않았다 — 세션만 열렸다.
    assert Video.query.count() == 0


def test_a_four_gigabyte_video_is_not_refused_by_a_byte_cap(client, flask_app,
                                                            monkeypatch):
    """⛔ 50MB 한도는 **지워졌다.** 바이트가 서버를 안 지나므로 근거가 없다.

    (옛 코드에서 이 테스트는 ``too_large`` 400 으로 실패한다 — 그게 요점이다.)
    """
    _arm_session(monkeypatch)
    r = client.post("/videos/upload-session",
                    json={"name": "4K.MOV", "size": 4 * 1024 ** 3,
                          "type": "video/quicktime"})
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_no_video_byte_cap_survives_anywhere():
    """한도는 숫자만 바뀐 게 아니라 **없다** — 새 임의의 숫자를 발명하지 않았다."""
    assert not hasattr(gifmaker, "MAX_VIDEO_BYTES")
    assert "max_video_bytes" not in gifmaker.spec()
    # 413 문구도 영상 한도로 부풀리지 않는다 (사진 한도만 남는다).
    src = io.open(os.path.join(_ROOT, "app.py"), encoding="utf-8").read()
    assert "max(_MAX_PHOTO_BYTES" not in src


def test_session_endpoint_rejects_non_videos_and_empty_files(client, flask_app,
                                                             monkeypatch):
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "create_upload_session",
                        lambda *a: pytest.fail("세션이 열리면 안 된다"))
    r = client.post("/videos/upload-session",
                    json={"name": "a.jpg", "size": 10, "type": "image/jpeg"})
    assert r.status_code == 400 and r.get_json()["error"] == "bad_type"
    r = client.post("/videos/upload-session",
                    json={"name": "a.mp4", "size": 0, "type": "video/mp4"})
    assert r.status_code == 400 and r.get_json()["error"] == "empty"
    assert Video.query.count() == 0


def test_complete_asks_graph_instead_of_believing_the_client(client, flask_app,
                                                             couple_user,
                                                             monkeypatch):
    """완료 보고는 **주장**이다 — 서버가 OneDrive 에 직접 물어 확인하고 그 값을 쓴다."""
    name = _arm_session(monkeypatch)
    asked = {}

    def fake_find(n):
        asked["name"] = n
        return {"id": "item-real", "name": n, "size": 777,
                "ctype": "video/quicktime"}

    monkeypatch.setattr(onedrive, "find_in_folder", fake_find)
    client.post("/videos/upload-session",
                json={"name": "IMG_9.MOV", "size": 999, "type": "video/quicktime"})
    r = client.post("/videos/complete",
                    json={"name": name, "original_name": "IMG_9.MOV",
                          # 클라이언트가 보낸 거짓말들 — 하나도 쓰이면 안 된다.
                          "item_id": "item-FORGED", "size": 10 ** 9,
                          "content_type": "video/forged"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert asked["name"] == name
    row = Video.query.filter_by(couple_id=couple_user.couple_id).one()
    assert row.onedrive_item_id == "item-real"   # 클라이언트가 준 id 가 아니다
    assert row.size_bytes == 777                 # 클라이언트가 준 크기가 아니다
    assert row.content_type == "video/quicktime"
    assert row.original_name == "IMG_9.MOV"


def test_complete_refuses_a_name_this_browser_never_opened(client, flask_app,
                                                           monkeypatch):
    """남이 올린 영상을 자기 것으로 등록하는 길을 막는다."""
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    monkeypatch.setattr(onedrive, "find_in_folder",
                        lambda *a: pytest.fail("조회까지 가면 안 된다"))
    r = client.post("/videos/complete", json={"name": "20261012_dead_x.mov"})
    assert r.status_code == 403 and r.get_json()["error"] == "unknown_session"
    assert Video.query.count() == 0


def test_complete_refuses_an_item_onedrive_does_not_have(client, flask_app,
                                                         monkeypatch):
    """끝까지 가지 못한 업로드로 행을 만들지 않는다 (폴더 밖도 여기서 None 이다)."""
    name = _arm_session(monkeypatch)
    monkeypatch.setattr(onedrive, "find_in_folder", lambda n: None)
    client.post("/videos/upload-session",
                json={"name": "IMG_9.MOV", "size": 1, "type": "video/quicktime"})
    r = client.post("/videos/complete", json={"name": name})
    assert r.status_code == 409 and r.get_json()["error"] == "not_found"
    assert Video.query.count() == 0


def test_complete_refuses_an_item_another_row_already_claims(client, flask_app,
                                                             couple_user,
                                                             monkeypatch):
    """같은 drive-item 을 두 행이 가리키지 못하게 — 커플 간 유출 경로를 막는다."""
    name = _arm_session(monkeypatch)
    other = Couple(invite_code="OTHERONE")
    db.session.add(other)
    db.session.flush()
    db.session.add(Video(couple_id=other.id, onedrive_item_id="item-real",
                         filename="x.mov", original_name="x.mov",
                         uploaded_by=couple_user.id, size_bytes=1,
                         content_type="video/mp4"))
    db.session.commit()
    monkeypatch.setattr(onedrive, "find_in_folder",
                        lambda n: {"id": "item-real", "name": n, "size": 9,
                                   "ctype": "video/mp4"})
    client.post("/videos/upload-session",
                json={"name": "IMG_9.MOV", "size": 9, "type": "video/quicktime"})
    r = client.post("/videos/complete", json={"name": name})
    assert r.status_code == 409 and r.get_json()["error"] == "already_claimed"
    assert Video.query.filter_by(couple_id=couple_user.couple_id).count() == 0


def test_a_transient_verify_failure_stays_retryable(client, flask_app,
                                                    monkeypatch):
    """조회가 일시적으로 실패해도 그 영상을 **영원히 잃지 않는다**.

    바이트는 이미 OneDrive 에 있다 — 여기서 발급 기록까지 태워 버리면 다시
    등록할 길이 사라진다(확인과 소비를 합치면 생기는 사고).
    """
    name = _arm_session(monkeypatch)
    client.post("/videos/upload-session",
                json={"name": "IMG_9.MOV", "size": 5, "type": "video/quicktime"})

    def boom(n):
        raise onedrive.OneDriveError("graph hiccup")

    monkeypatch.setattr(onedrive, "find_in_folder", boom)
    r = client.post("/videos/complete", json={"name": name})
    assert r.status_code == 502 and r.get_json()["error"] == "verify_failed"

    # 같은 이름으로 **다시 시도하면 된다** — 403(모르는 세션)이 아니어야 한다.
    monkeypatch.setattr(onedrive, "find_in_folder",
                        lambda n: {"id": "item-real", "name": n, "size": 5,
                                   "ctype": "video/mp4"})
    r = client.post("/videos/complete", json={"name": name})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    # 그리고 성공한 뒤에는 그 기록이 소비돼 두 번 쓰이지 않는다.
    r = client.post("/videos/complete", json={"name": name})
    assert r.status_code == 403 and r.get_json()["error"] == "unknown_session"
    assert Video.query.count() == 1


def test_the_upload_url_is_a_credential_and_never_lands_in_the_db_or_the_log(
        client, flask_app, couple_user, monkeypatch, caplog):
    """uploadUrl 은 capability URL 이다 — 가진 사람이 그 자리에 쓸 수 있다.

    응답 본문으로 본인에게 건네는 것 말고는 **어디에도 남지 않아야** 한다.
    """
    name = _arm_session(monkeypatch)
    monkeypatch.setattr(onedrive, "find_in_folder",
                        lambda n: {"id": "item-real", "name": n, "size": 5,
                                   "ctype": "video/mp4"})
    with caplog.at_level(logging.DEBUG):
        client.post("/videos/upload-session",
                    json={"name": "IMG_9.MOV", "size": 5, "type": "video/quicktime"})
        client.post("/videos/complete", json={"name": name})
    secret = "CAPABILITY-TOKEN-DO-NOT-LEAK"
    assert secret not in caplog.text, "업로드 URL 이 로그에 남았다"
    row = Video.query.one()
    dumped = json.dumps({c.name: str(getattr(row, c.name))
                         for c in row.__table__.columns}, ensure_ascii=False)
    assert secret not in dumped, "업로드 URL 이 DB 에 남았다"


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
    _arm_session(monkeypatch, name="20261012_zzzz_a.mp4")
    monkeypatch.setattr(onedrive, "find_in_folder",
                        lambda n: {"id": "i", "name": n, "size": 99,
                                   "ctype": "video/mp4"})
    client.post("/videos/upload-session",
                json={"name": "a.mp4", "size": 99, "type": "video/mp4"})
    client.post("/videos/complete", json={"name": "20261012_zzzz_a.mp4"})
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


# --------------------------------------------------------------------------- #
# 10. ⛔ 침묵 금지 — "고르면 아무 안내 없이 조용함" 회귀 방어
#
# 실측된 증상(2026-10-12): 동영상 올리기 → 파일 선택 → **아무 일도 안 일어남.**
# 올라가는 중인지, 크기 때문인지, 어디서 터졌는지 사람이 전혀 알 수 없었다.
# 원인은 둘이 겹친 것이었다:
#   (1) 진행·경고·진단·상태 네 줄이 모두 ``.rv-gif-panel`` **안**에 있었는데,
#       그 패널은 영상이 하나도 없으면 ``hidden`` 이다 — 즉 **첫 업로드**의 모든
#       안내가 보이지 않는 곳에 쓰였다. 화면은 말하고 있었지만 아무도 못 봤다.
#   (2) 업로더가 ``fetch(...).then(r => r.json().catch(() => null))`` 라 HTTP
#       상태를 통째로 버렸고, 413 이 돌려준 HTML 에러 **페이지**는 JSON 파싱에
#       실패해 "영상을 올리지 못했어." 한 줄로 뭉개졌다(그마저 (1) 때문에 안 보임).
# 아래 테스트는 그 둘을 각각 못질한다.
# --------------------------------------------------------------------------- #
def _panel_markup(html):
    """렌더된 HTML 에서 ``.rv-gif-panel`` 요소 하나를 깊이 세기로 잘라 낸다."""
    start = html.index('<div class="rv-gif-panel"')
    depth, i = 0, start
    tag = re.compile(r"<(/?)div\b", re.I)
    while i < len(html):
        m = tag.search(html, i)
        if not m:
            break
        depth += 1 if not m.group(1) else -1
        i = m.end()
        if depth == 0:
            break
    return html[start:i]


def test_feedback_lines_live_outside_the_panel_that_hides_without_videos(
        client, flask_app, couple_user, make_review, monkeypatch):
    """영상이 0개여도 진행·경고·진단·상태는 **보이는 자리**에 있어야 한다.

    이게 깨지면 첫 업로드가 통째로 무음이 된다(실측된 증상 그 자체).
    """
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready")
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)

    panel = _panel_markup(html)
    # 영상이 없으므로 패널은 실제로 숨겨져 있다 — 전제를 먼저 못질한다.
    assert "hidden" in panel.split(">", 1)[0]
    for cls in ("rv-gif-status", "rv-gif-bar", "rv-gif-warn", "rv-gif-diag"):
        assert cls in html, f"{cls} 가 화면에서 사라졌다"
        assert cls not in panel, (
            f"{cls} 가 숨은 패널 안에 있다 — 첫 업로드가 조용히 죽는다")


def test_the_card_says_the_bytes_skip_our_server_and_that_no_cap_remains(
        client, flask_app, couple_user, make_review, monkeypatch):
    """"브라우저가 만드는데 왜 크기 제한?" 의 답이 **바뀌었다** — 제한이 없어졌다.

    바이트가 서버를 안 지나므로 옛 50MB 의 근거가 사라졌다. 화면이 없는 한도를
    계속 말하면 사람은 멀쩡한 파일을 자른다.
    """
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready")
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "rv-gif-why" in html
    assert "OneDrive" in html and "보관" in html
    # 바이트가 어디로 가는지 화면이 말한다.
    assert "서버를 거치지 않고" in html
    assert "크기 한도는 없어" in html
    # GIF 자체의 한도는 **남아 있고** 그건 따로 말한다 (둘을 섞지 않는다).
    assert str(gifmaker.MAX_GIF_BYTES // 1048576) in html
    # 없어진 한도를 화면이 되살려 말하지 않는다.
    assert "50MB 이하" not in html


def test_oversized_request_answers_the_uploader_in_json_not_an_html_page(
        client, flask_app, monkeypatch):
    """요청이 MAX_CONTENT_LENGTH 를 넘으면 업로더는 **읽을 수 있는 이유**를 받아야 한다.

    업로더가 ``X-Requested-With: fetch`` 를 보내므로 413 은 JSON 으로 와야 한다 —
    HTML 에러 페이지가 오면 클라이언트가 파싱에 실패해 이유가 사라진다.
    ⚠️ 이제 이 핸들러를 타는 건 **사진뿐이다** (영상 바이트는 서버로 오지 않는다).
    """
    monkeypatch.setattr(onedrive, "onedrive_enabled", lambda: True)
    cap = flask_app.config["MAX_CONTENT_LENGTH"]
    monkeypatch.setitem(flask_app.config, "MAX_CONTENT_LENGTH", 2048)
    r = client.post("/memories/upload",
                    data={"photos": (io.BytesIO(b"x" * 8192), "big.jpg")},
                    content_type="multipart/form-data",
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 413
    assert r.is_json, "413 이 HTML 페이지로 오면 업로더가 이유를 읽지 못한다"
    assert r.get_json()["error"] == "too_large"
    # 문구의 숫자는 **사진 한도**다 — 없어진 영상 한도로 부풀리지 않는다.
    assert "50MB" in r.get_json()["reason"]
    assert cap > 0  # 원래 설정이 테스트 사이에 새지 않게 읽어만 둔다


def test_uploader_reports_progress_and_never_swallows_the_http_status():
    """gif.js 업로더가 (1) 진행률을 말하고 (2) 상태별로 다른 문장을 낸다."""
    src = io.open(os.path.join(_ROOT, "static", "gif.js"), encoding="utf-8").read()
    # (1) fetch 로는 업로드 진행률을 알 수 없다 — XHR 의 upload.onprogress 여야 한다.
    #     바이트가 OneDrive 로 직접 가게 된 뒤에도 이 배선은 그대로여야 한다.
    assert "xhr.upload.onprogress" in src
    assert "영상 올리는 중…" in src
    # (2) 상태를 버리지 않는다 — 우리 서버(401/502)와 OneDrive(416/507/429…)가
    #     각각 다른 말을 한다.
    assert "function uploadFailureText" in src
    assert "function putFailureText" in src
    for needle in ("401", "502", "404", "409", "416", "423", "429", "507"):
        assert needle in src, f"HTTP {needle} 분기가 사라졌다"
    # (3) 서버가 JSON 으로 답하게 하는 신호를 반드시 보낸다.
    assert 'setRequestHeader("X-Requested-With", "fetch")' in src
    # (4) ⛔ 경과 시간 상한으로 업로드를 끊지 않는다 — 멈추는 길은 중단 버튼뿐이다.
    assert "xhr.timeout = 0" in src
    assert "function cancelUpload" in src
    # (5) 중단도 실패와 **다른 문장**이고, 중단하면 세션을 지운다.
    assert "올리기를 중단했어" in src
    assert "function dropSession" in src


def test_uploader_puts_straight_to_onedrive_under_the_measured_cors_rules():
    """직접 PUT 이 실측된 CORS 규칙을 그대로 지키는가 — 어기면 브라우저가 막는다.

    실측(2026-10-12): 두 업로드 호스트 모두 ``Access-Control-Allow-Credentials``
    를 주지 않고, ``my.microsoftpersonalcontent.com`` 의 허용 헤더는
    ``content-range`` **하나뿐**이다.
    """
    src = io.open(os.path.join(_ROOT, "static", "gif.js"), encoding="utf-8").read()
    start = src.index("function putChunk(")
    body = src[start:src.index("\n  // 세션 하나를 끝까지", start)]
    assert 'xhr.open("PUT", session.upload_url' in body
    assert "xhr.withCredentials = false" in body, "자격을 붙이면 ACAC 부재로 막힌다"
    # 붙이는 헤더는 Content-Range 하나뿐이어야 한다.
    assert body.count("setRequestHeader") == 1
    assert '"Content-Range"' in body
    assert "Authorization" not in body, "uploadUrl 은 이미 인증된 URL이다"
    # 서버로 되돌아가는 길이 없다 — 바이트는 OneDrive 로만 간다.
    assert "SESSION_URL" not in body and "COMPLETE_URL" not in body


def test_uploader_blocks_a_non_video_before_opening_a_session():
    """형식이 틀린 파일은 **세션을 열기도 전에** 막고 말한다.

    ⛔ 크기 프리체크는 **없다** — 서버에 한도가 없으므로 화면이 없는 한도를
    말하면 안 된다(있던 50MB 분기를 지웠다).
    """
    src = io.open(os.path.join(_ROOT, "static", "gif.js"), encoding="utf-8").read()
    assert "function uploadPrecheck" in src
    assert "동영상 파일만 올릴 수 있어" in src
    assert "SPEC.video_exts" in src
    # 없어진 한도를 클라이언트가 되살리지 않는다.
    assert "max_video_bytes" not in src
    # 프리체크 실패는 ``warn`` 으로 화면에 남는다(조용한 return 금지).
    assert "올리지 않았어" in src


def test_select_video_chain_cannot_end_in_an_unhandled_rejection():
    """영상 선택 체인에 종착 catch 가 있어야 한다 — 없으면 '불러오는 중…' 에 고착된다."""
    src = io.open(os.path.join(_ROOT, "static", "gif.js"), encoding="utf-8").read()
    start = src.index("function selectVideo(")
    body = src[start:src.index("\n  function watchDuration(", start)]
    assert ".catch(function (e) {" in body, "selectVideo 체인에 종착 catch 가 없다"
    assert "영상 정보를 읽지 못했어" in body
    # 단계가 번호로 말한다 — 어디서 멈췄는지가 곧 어디서 죽었나의 답이다.
    for step in ("(1/3)", "(2/3)", "(3/3)"):
        assert step in body


def test_no_silent_return_paths_left_in_the_buttons():
    """버튼을 눌렀는데 아무 말도 없는 길이 남아 있으면 안 된다."""
    src = io.open(os.path.join(_ROOT, "static", "gif.js"), encoding="utf-8").read()
    # 스펙을 못 읽어도 말하고 죽는다(예전엔 그냥 return 이었다).
    assert "GIF 설정(rv-gif-spec)을 읽지 못해서" in src
    # 만들기/내려받기/미리보기의 '조건 불충족' 이 각각 문장을 낸다.
    assert "먼저 영상을 골라줘" in src
    assert "아직 내려받을 GIF 가 없어" in src
    assert "이미 만드는 중이야" in src
    # 클릭 직후 어느 디코더로 가는지 즉시 밝힌다.
    assert "GIF 만들기 시작" in src
