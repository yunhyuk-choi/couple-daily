"""gifmaker.py — 동영상 → GIF (노선 2 Step 5). **서버는 픽셀을 만들지 않는다.**

썸네일(`thumbnail.py`)과 **같은 철학**이다. 거기서 서버가 Chromium 을 올리지 않고
사용자의 브라우저가 DOM+Canvas 로 그렸듯이, 여기서도 서버는 ffmpeg 를 올리지 않고
**사용자의 브라우저가 디코딩·프레임 추출·GIF 인코딩을 전부 한다**(`static/gif.js`).

### 왜 서버에서 안 하나 (선행 판단 — 다시 조사하지 않는다)

* 프로덕션은 Render 무료티어 **512MB · 0.1 CPU**, 베이스 이미지는 `python:3.12-slim`
  으로 **ffmpeg 가 없다.** 설치하면 이미지가 수백 MB 불고, 0.1 CPU 에서 3초짜리
  480p 인코딩도 요청 타임아웃을 넘긴다. 둘 다 비현실적이다.
* 브라우저에는 이미 하드웨어 디코더가 있다(OS 코덱을 빌려 쓴다). 공짜로 쓰는 게 맞다.

### 서버가 하는 일은 세 가지뿐

1. **스펙을 단일 원천으로 내려보낸다** — 프리셋(가로폭·fps)·한도·상한. 화면과
   클라이언트와 서버 판정이 *같은 dict* 를 본다(썸네일 `thumbnail.spec()` 과 같은 결).
2. **원본 바이트를 Range(206)로 흘린다** — `app.video_stream`. 브라우저 `<video>` 의
   seek 은 Range 없이는 동작하지 않는다.
3. **클라이언트가 보고한 결과를 다시 판정한다** — 용량 한도 초과 여부를 서버가
   재계산한다(`evaluate_result`). 클라이언트의 ``ok`` 주장은 읽지 않는다.

### 규율 — 넘치면 **몰래 화질을 떨어뜨리지 않는다**

썸네일이 "넘치면 폰트를 줄이지 않고 문구를 줄인다" 였던 것과 같다. GIF 가 한도를
넘으면 **자동으로 색/fps/크기를 깎지 않는다.** 내려받기를 막고 "길이를 줄여라"라고
말한다. 줄이는 것은 사람이고, 프리셋을 낮추는 것도 사람이 고른다. 그래서 이 모듈
어디에도 "한도에 맞춰 재인코딩" 경로가 없다.
"""
import re

# --------------------------------------------------------------------------- #
# 1. 프리셋 — 화면·클라이언트·서버가 공유하는 단일 원천
# --------------------------------------------------------------------------- #
# 가로폭(px). 높이는 원본 비율에서 계산한다(짝수로 맞춘다 — 디코더 호환).
WIDTH_PRESETS = (480, 360, 240)
# 초당 프레임. GIF 는 프레임마다 256색 팔레트를 새로 쓰므로 fps 가 곧 용량이다.
FPS_PRESETS = (12, 10, 8, 5)

DEFAULT_WIDTH = 360
DEFAULT_FPS = 10

# 길이 — 네이버 블로그에 붙일 '한 장면' 이다. 길게 만들 수단을 두지 않는다.
MIN_DURATION = 0.3
MAX_DURATION = 6.0
DEFAULT_DURATION = 2.0

# 용량 한도. 넘으면 **내려받기를 막는다**(화질을 깎지 않는다).
MAX_GIF_BYTES = 8 * 1024 * 1024

# 프레임 수 상한 — 브라우저가 멈춘 것처럼 보이는 걸 막는 안전장치.
# MAX_DURATION × 가장 높은 fps 와 같은 값이라 UI 에서 도달할 수 없고, 손으로 만든
# 요청만 걸린다.
MAX_FRAMES = int(MAX_DURATION * max(FPS_PRESETS))

# 업로드 받는 동영상 — 네 기기(아이폰·갤럭시·맥북·윈도우)가 실제로 만드는 컨테이너.
ALLOWED_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv")
# 요청 본문 상한(app.MAX_CONTENT_LENGTH=55MB)보다 작아야 멀티파트 여유가 남는다.
MAX_VIDEO_BYTES = 50 * 1024 * 1024

_EXT_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
}


def content_type_for(name):
    """파일명 → 동영상 MIME. 모르면 ``video/mp4``(가장 흔한 컨테이너)."""
    m = re.search(r"(\.[A-Za-z0-9]+)$", name or "")
    ext = (m.group(1).lower() if m else "")
    return _EXT_CONTENT_TYPES.get(ext, "video/mp4")


def is_allowed_video(name, ctype=""):
    """확장자 **또는** 선언된 content-type 이 동영상이면 받는다.

    사진 업로드(`app._ALLOWED_IMAGE_EXTS`)와 **같은 판정 방식**이다 — 아이폰이
    ``.MOV`` 를 대문자로 보내거나 안드로이드가 확장자 없이 보내는 경우를 둘 중
    하나로 건진다.
    """
    m = re.search(r"(\.[A-Za-z0-9]+)$", name or "")
    ext = (m.group(1).lower() if m else "")
    return ext in ALLOWED_VIDEO_EXTS or (ctype or "").lower().startswith("video/")


# --------------------------------------------------------------------------- #
# 2. 용량 추정 — **예상치일 뿐이고, 판정은 언제나 실측이다**
# --------------------------------------------------------------------------- #
# GIF 는 프레임마다 256색 + LZW 다. 실사 영상에서 픽셀당 압축 후 바이트는 대략
# 0.25~0.6B 사이로 흔들린다(배경이 단순할수록 작다). 아래 계수는 **보수적인 중앙값**
# 이고, 화면에는 "대략"이라고 적는다. 실제 용량은 인코딩이 끝나야 안다.
_BYTES_PER_PIXEL = 0.42
_GIF_HEADER_BYTES = 800


def estimate_bytes(width, height, fps, duration):
    """프리셋 조합의 **대략의** GIF 용량(바이트). 안내용이지 판정이 아니다."""
    frames = max(1, int(round(float(fps) * float(duration))))
    px = max(1, int(width)) * max(1, int(height))
    return int(_GIF_HEADER_BYTES + px * frames * _BYTES_PER_PIXEL)


def frame_count(fps, duration):
    """이 조합이 만들 프레임 수 — 화면이 "N장"으로 보여 주고, 상한도 이걸로 잰다."""
    return max(1, int(round(float(fps) * float(duration))))


def target_height(src_width, src_height, width):
    """원본 비율을 지킨 목표 높이(짝수). 원본 크기를 모르면 ``None``.

    ⚠️ 업스케일은 하지 않는다 — 원본이 목표 가로폭보다 작으면 원본 크기를 쓴다
    (늘려 봐야 흐려지기만 하고 용량만 는다).

    ⚠️ **가장 가까운 짝수로** 반올림한다 — 폴백 디코더가 쓰는 ffmpeg ``scale=W:-2``
    와 같은 규칙이다. 한쪽만 내림하면 두 디코딩 경로가 1px 다른 GIF 를 낸다.
    """
    try:
        sw, sh = int(src_width), int(src_height)
    except (TypeError, ValueError):
        return None
    if sw <= 0 or sh <= 0:
        return None
    w = min(int(width), sw)
    return max(2, 2 * int(round(sh * (w / sw) / 2)))


# --------------------------------------------------------------------------- #
# 3. 설정 정규화 — 화면에서 온 값을 **프리셋 안으로 가둔다**
# --------------------------------------------------------------------------- #
def _clamp_float(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def normalize_settings(raw, max_start=None):
    """``{video_id, start, duration, width, fps}`` 를 믿을 수 있는 값으로.

    프리셋 밖의 가로폭·fps 는 **거부하지 않고 기본값으로 떨어뜨린다** — 사람이
    고를 수 있는 선택지는 화면의 라디오뿐이고, 손으로 만든 요청이 임의의 숫자로
    서버 판정을 흔들게 두지 않는다.

    ``max_start`` (영상 길이)를 주면 시작점을 그 안으로 가둔다.
    """
    raw = raw if isinstance(raw, dict) else {}
    try:
        width = int(raw.get("width"))
    except (TypeError, ValueError):
        width = DEFAULT_WIDTH
    if width not in WIDTH_PRESETS:
        width = DEFAULT_WIDTH
    try:
        fps = int(raw.get("fps"))
    except (TypeError, ValueError):
        fps = DEFAULT_FPS
    if fps not in FPS_PRESETS:
        fps = DEFAULT_FPS
    duration = _clamp_float(
        raw.get("duration"), MIN_DURATION, MAX_DURATION, DEFAULT_DURATION
    )
    hi = MAX_DURATION * 60  # 시작점의 느슨한 상한(영상 길이를 모를 때)
    if max_start is not None:
        try:
            hi = max(0.0, float(max_start))
        except (TypeError, ValueError):
            pass
    start = _clamp_float(raw.get("start"), 0.0, hi, 0.0)
    try:
        video_id = int(raw.get("video_id"))
    except (TypeError, ValueError):
        video_id = None
    return {
        "video_id": video_id,
        "start": round(start, 2),
        "duration": round(duration, 2),
        "width": width,
        "fps": fps,
    }


# --------------------------------------------------------------------------- #
# 4. 결과 판정 — 클라이언트의 ``ok`` 를 읽지 않고 **서버가 다시 잰다**
# --------------------------------------------------------------------------- #
# 디코딩 경로. 브라우저가 OS 코덱으로 바로 읽었으면 'native', OS 에 코덱이 없어
# 브라우저 안 WASM 디코더로 떨어졌으면 'wasm'. 어느 쪽이든 GIF 인코더는 **같다**.
DECODE_PATHS = ("native", "wasm")


def evaluate_result(raw):
    """브라우저가 보고한 인코딩 결과를 서버가 **다시 판정**한다. 못 믿으면 ``None``.

    받는 것은 측정값뿐이다 — 만들어진 바이트 수·해상도·프레임 수·fps·길이·경로.
    ``ok`` 는 여기서 계산한다(``bytes <= MAX_GIF_BYTES``). 썸네일의
    ``evaluate_render_check`` 와 같은 태도이고, 믿는 것은 '측정값'이지 '주장'이 아니다.

    ⚠️ 한도를 넘었을 때 **여기서 화질을 낮춘 재인코딩을 지시하지 않는다.** ``ok=False``
    로 남기고 화면이 내려받기를 막는다 — 길이를 줄이는 건 사람이다.
    """
    if not isinstance(raw, dict):
        return None
    try:
        size = int(raw.get("bytes"))
        width = int(raw.get("width"))
        height = int(raw.get("height"))
        frames = int(raw.get("frames"))
    except (TypeError, ValueError):
        return None
    if size <= 0 or width <= 0 or height <= 0 or frames <= 0:
        return None
    if frames > MAX_FRAMES:
        return None
    fps = _clamp_float(raw.get("fps"), 1, max(FPS_PRESETS), DEFAULT_FPS)
    duration = _clamp_float(raw.get("duration"), 0.0, MAX_DURATION, 0.0)
    path = raw.get("path") if raw.get("path") in DECODE_PATHS else None
    return {
        "ok": size <= MAX_GIF_BYTES,
        "bytes": size,
        "limit": MAX_GIF_BYTES,
        "over_by": max(0, size - MAX_GIF_BYTES),
        "width": width,
        "height": height,
        "frames": frames,
        "fps": round(fps, 2),
        "duration": round(duration, 2),
        "path": path,
    }


# 진단 — 이 기기가 이 영상을 **왜** 못 읽는지 화면이 말할 수 있게 하는 재료.
# 조용히 빈 프레임을 내는 것이 이 기능의 최악의 실패 모드라, 실패는 반드시 이름이 있다.
DIAG_CODES = (
    "native-ok",        # OS 코덱으로 바로 디코딩됨
    "wasm-ok",          # OS 코덱이 없어 브라우저 내 디코더로 디코딩됨
    "decode-failed",    # 둘 다 실패 — 이 컨테이너/코덱은 이 기기에서 못 읽는다
    "tainted",          # 캔버스가 오염돼 픽셀을 읽을 수 없다(CORS)
    "blank-frames",     # 디코더가 에러 없이 빈(단색) 프레임만 냈다 — 조용한 실패
    "loader-failed",    # 폴백 디코더(WASM)를 받아오지 못했다
    "no-video",         # 고를 영상이 없다
)


def evaluate_diag(raw):
    """브라우저 진단 보고를 저장 가능한 모양으로(순수/오프라인). 못 믿으면 ``None``.

    코드는 ``DIAG_CODES`` 안의 값만 받는다 — 화면 문구가 코드에 매여 있으므로
    모르는 코드를 저장하면 나중에 '아무 말도 못 하는' 상태가 된다.
    """
    if not isinstance(raw, dict):
        return None
    code = raw.get("code")
    if code not in DIAG_CODES:
        return None
    out = {"code": code}
    for k in ("codec", "container", "detail"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = re.sub(r"\s+", " ", v).strip()[:200]
    for k in ("src_width", "src_height"):
        try:
            out[k] = int(raw.get(k))
        except (TypeError, ValueError):
            pass
    return out


# --------------------------------------------------------------------------- #
# 5. 상태 — blog_reviews.gif_json 에 담기는 dict
# --------------------------------------------------------------------------- #
def apply_state(state, settings=None, result=None, diag=None):
    """사람이 고른 설정 + 브라우저가 보고한 결과/진단을 상태 dict 에 반영(순수 함수).

    설정이 바뀌면 직전 결과는 **더 이상 이 설정의 것이 아니므로 버린다**(썸네일에서
    문구가 바뀌면 ``render_check`` 를 비운 것과 같은 결). 그래야 화면이 옛 용량을
    보고 "통과"라고 말하는 일이 없다.
    """
    state = dict(state) if isinstance(state, dict) else {}
    if settings is not None:
        prev = state.get("settings")
        state["settings"] = settings
        if prev != settings:
            state.pop("result", None)
    if result is not None:
        state["result"] = result
    if diag is not None:
        state["diag"] = diag
    return state


def spec():
    """프리셋·한도 묶음 — 화면과 `static/gif.js` 가 **같은 이 dict** 를 본다.

    썸네일의 ``thumbnail.spec()`` 과 같은 자리다. 다만 여기엔 외부 파일이 없어
    실패할 일이 없으므로 ``None`` 을 돌려주지 않는다.
    """
    return {
        "widths": list(WIDTH_PRESETS),
        "fps": list(FPS_PRESETS),
        "default_width": DEFAULT_WIDTH,
        "default_fps": DEFAULT_FPS,
        "min_duration": MIN_DURATION,
        "max_duration": MAX_DURATION,
        "default_duration": DEFAULT_DURATION,
        "max_bytes": MAX_GIF_BYTES,
        "max_frames": MAX_FRAMES,
        "bytes_per_pixel": _BYTES_PER_PIXEL,
        "max_video_bytes": MAX_VIDEO_BYTES,
        "video_exts": list(ALLOWED_VIDEO_EXTS),
    }
