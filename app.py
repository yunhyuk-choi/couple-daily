"""couple-daily — a tiny daily-question app for exactly two people.

Identity constraint (do not violate): the AI mechanism is the `claude` CLI run
as a backend subprocess (see ai.py), NOT the Anthropic API/SDK. This file wires
Flask + SQLAlchemy, auth, couple linking, the daily question, monthly insight,
settings and PWA plumbing on top of that.

Run locally:   python app.py            (SQLite fallback, debug reloader)
Run in prod:   gunicorn 'app:create_app()'   (Postgres via DATABASE_URL)
"""
import calendar
import functools
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import string
import sys
import tempfile
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from urllib.parse import urlencode

import requests

try:  # web push is optional — the app runs fine without VAPID keys configured
    import pywebpush
except ImportError:  # pragma: no cover
    pywebpush = None
from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    send_from_directory,
    url_for,
)
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from markupsafe import escape
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import aijobs
import bytecache
import events
import exifutil
import gifmaker
import ics
import insights
import keyword_research
import naver_api
import onedrive
import previews
import thumbnail
from models import (
    Answer,
    Bet,
    BetCheckin,
    BlogReview,
    Case,
    CaseStatement,
    Comment,
    Couple,
    DailyQuestion,
    DateRecommendation,
    EventItem,
    EventPick,
    EventScore,
    MonthlyReport,
    Notification,
    Photo,
    PushSubscription,
    Schedule,
    Setting,
    User,
    Video,
    db,
    notify,
    run_startup_migrations,
)

log = logging.getLogger(__name__)

DEFAULT_APP_NAME = os.environ.get("APP_NAME", "우리의 하루")

# HEIC/HEIF는 iOS Safari만 <img>로 렌더한다 — Android Chrome·데스크톱은 못 본다.
# 풀이미지 프록시에서 HEIC 원본을 JPEG로 변환해 서빙하면 갤럭시·PC에서도 보인다.
# Pillow(+pillow-heif)를 한 번만·방어적으로 로드한다. 없으면 _HEIC_OK=False로
# 두고 변환을 건너뛴다(원본 그대로 서빙 — 앱은 계속 동작).
try:  # pragma: no cover - 환경에 따라 분기
    from io import BytesIO as _BytesIO
    from PIL import Image as _PILImage  # type: ignore
    from PIL import ImageOps as _PILImageOps  # type: ignore

    try:
        import pillow_heif as _pillow_heif  # type: ignore

        _pillow_heif.register_heif_opener()  # HEIC/HEIF를 PIL.Image.open이 열게 등록(1회)
    except Exception:  # noqa: BLE001 - HEIC 미지원이어도 다른 포맷 경로는 산다
        log.info("app: pillow-heif 미탑재 — HEIC→JPEG 변환 비활성")
    _HEIC_OK = True
except Exception:  # noqa: BLE001 - Pillow 자체가 없으면 변환 전체 비활성
    _PILImage = None  # type: ignore
    _PILImageOps = None  # type: ignore
    _HEIC_OK = False
    log.info("app: Pillow 미탑재 — HEIC→JPEG 변환 비활성(원본 서빙)")


# --------------------------------------------------------------------------- #
# ⛔ 이미지 디코드 경로의 메모리 규율 (2026-10-07 OOM 사고에서 실측으로 승격)
# --------------------------------------------------------------------------- #
# 3024×4032(12.2MP) 한 장의 RGB 픽셀 버퍼는 그 자체로 ~36MB다. Pillow 로 '무심코'
# 쓰면 그 36MB 짜리 사본이 **여러 개** 동시에 살아 있게 된다:
#   * ``ImageOps.exif_transpose(img)`` 는 방향 태그가 없어도 **전체 사본**을 만든다.
#   * ``img.convert("RGB")`` 는 이미 RGB 여도 **또 전체 사본**을 만든다.
# 실측(3.03MB JPEG · 3024×4032, RSS 봉우리):
#   원본 해상도 저장(hq)  : 143MB → **50MB**  (-65%)
#   1280 다운스케일(std) : 112MB → **66MB**  (-41%)
#   두 경로 모두 출력 픽셀은 **완전히 동일**(채널별 평균차 0.0, 최대차 0).
# 512MB 티어에서 claude -p 한 개가 ~265MB·파이썬 워커가 ~100MB 를 쓰므로, 이 사본
# 하나가 그대로 OOM 과 생존의 차이다. 아래 두 헬퍼를 **모든 디코드 경로가** 쓴다.
def _exif_upright(img):
    """EXIF 방향을 적용한 이미지 — 가능하면 **제자리에서**(전체 사본 금지).

    ``in_place=True`` 는 Pillow 9.5+ 이고 이 앱은 ``Pillow>=10.2`` 를 쓴다. 혹시
    더 옛 Pillow 면 옛 경로(사본)로 폴백한다 — 느려지지 않고 메모리만 예전과 같다.
    """
    try:
        return _PILImageOps.exif_transpose(img, in_place=True) or img
    except TypeError:  # pragma: no cover - Pillow < 9.5
        return _PILImageOps.exif_transpose(img)


def _as_rgb(img):
    """JPEG 로 저장할 수 있는 RGB 이미지 — 이미 RGB 면 **사본을 만들지 않는다.**"""
    return img if img.mode == "RGB" else img.convert("RGB")


# EXIF 방향 태그(274). 값 5~8 은 90°/270° 회전이라 표시 기준 가로·세로가 바뀐다.
_EXIF_ORIENTATION = 0x0112


def _heic_to_jpeg(data):
    """HEIC/HEIF 바이트를 JPEG 바이트로 변환. 실패하면 None(호출부는 원본 폴백).

    절대 예외를 던지지 않는다 — Pillow/pillow-heif 부재·디코드 실패 모두 None.
    메모리 규율은 위 머리말 참고(실측 봉우리 ~99MB → 사본 제거분만큼 내려간다).
    """
    if not _HEIC_OK or not data:
        return None
    try:
        with _PILImage.open(_BytesIO(data)) as img:
            rgb = _as_rgb(img)
            out = _BytesIO()
            rgb.save(out, format="JPEG", quality=85)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - 손상·비이미지·미지원 → 원본 폴백
        log.warning("HEIC→JPEG 변환 실패 — 원본 바이트로 폴백", exc_info=True)
        return None


# 블로그 서빙 이미지 긴 변 상한(px) — 앱 미리보기는 가볍게. 네이버 export만 hq(원본)로.
_BLOG_IMG_MAX_EDGE = 1280
# 블로그 후기 이미지 크롭 목표 가로세로비(가로/세로). 4:3. 3/2·16/9로 자유 교체.
_BLOG_CROP_ASPECT = 4 / 3

# 가공완료(crop+downscale+JPEG) 블로그 이미지 바이트 캐시. (photo,crop,mode)당
# 결과가 불변이라 무효화 불필요. 히트 시 OneDrive 호출·_blog_img_process를 모두
# 건너뛰어 재방문/재export가 즉시다.
#
# ⚠️ **이 캐시는 RAM 이 아니라 디스크에 산다.** 예전엔 개수 상한 200개짜리 모듈
# dict 였는데, 개수 상한은 메모리를 전혀 묶지 못했다 — 실측 엔트리 크기가
# std(1280·q92) 0.59MB / hq(원본 해상도·q95) **3.28MB** 라 200개면 최악 **656MB**
# 상주다(512MB 티어를 캐시 하나가 혼자 넘긴다 — 2026-10-07 OOM 의 지분).
#
# 그렇다고 개수를 줄이면 '다시 방문하면 즉시' 라는 기능이 깎인다. 그래서 **용량을
# 줄이는 대신 자리를 옮겼다**: 바이트는 컨테이너의 임시 디스크(Render 는 ephemeral
# **disk**, tmpfs 가 아니다 — 캡션 임시 이미지도 이미 파일로 쓴다)에 두고, 메모리엔
# 아무것도 남기지 않는다. 결과:
#   * 상주 RAM            656MB(최악) → **0**
#   * 담을 수 있는 장수   200개 → std 기준 ~217장 / hq 기준 ~39장 (128MB 예산)
#   * 히트 비용           OneDrive 왕복 + Pillow 재인코딩(0.1 CPU 에서 수 초)
#                         → 로컬 파일 read 수 ms. 기능은 오히려 더 세진다.
# 디스크가 안 되는 환경(읽기전용·권한 없음)에서는 조용히 '항상 미스'로 동작한다 —
# 느려질 뿐 절대 실패하지 않는다.
_BLOG_PROC_CACHE_TTL = 6 * 3600  # seconds (~6h)
_BLOG_PROC_DISK_BUDGET = 128 * 1024 * 1024
_blog_proc_cache = bytecache.DiskByteCache(
    "blog-proc", ttl=_BLOG_PROC_CACHE_TTL, budget=_BLOG_PROC_DISK_BUDGET,
    directory=os.environ.get("BLOG_PROC_CACHE_DIR"),
)


def _blog_proc_cache_get(key):
    """가공완료 캐시 조회 — 유효(미만료) 히트면 바이트, 아니면 None."""
    return _blog_proc_cache.get(key)


def _blog_proc_cache_put(key, data):
    """가공완료 캐시 저장 — 디스크에 원자적으로 쓰고 예산 안으로 축출."""
    _blog_proc_cache.put(key, data)


# 네이버 자동 export(v2) PENDING 슬롯 — 사용자 export_key → {rid, exp}. 앱에서 📤를
# 누르면(mark) 슬롯을 세우고, 유저스크립트가 공개 pending API로 꺼내 간다(서브 시 삭제).
# DB 컬럼 대신 모듈 dict(캐시 패턴). TTL 15분, 상한으로 메모리 폭주 방지.
_NAVER_EXPORT_TTL = 900  # seconds
_NAVER_EXPORT_MAX = 500
_naver_export_pending = {}  # export_key -> (rid, expiry)


def _naver_export_put(key, rid):
    """pending 슬롯 세팅 — 삽입 전 만료·최고령 축출로 상한 유지."""
    now = time.time()
    if len(_naver_export_pending) >= _NAVER_EXPORT_MAX:
        for k in [k for k, v in _naver_export_pending.items() if v[1] <= now]:
            _naver_export_pending.pop(k, None)
        if len(_naver_export_pending) >= _NAVER_EXPORT_MAX:
            oldest = min(_naver_export_pending, key=lambda k: _naver_export_pending[k][1])
            _naver_export_pending.pop(oldest, None)
    _naver_export_pending[key] = (rid, now + _NAVER_EXPORT_TTL)


def _naver_export_take(key):
    """pending 슬롯 꺼내며 삭제(clear-on-serve, 재실행 방지). 없거나 만료면 None."""
    if not key:
        return None
    hit = _naver_export_pending.pop(key, None)
    if not hit:
        return None
    rid, exp = hit
    if time.time() >= exp:
        return None
    return rid


def _image_display_size(data):
    """이미지 바이트의 '표시 기준'(EXIF 방향 반영) (w, h)를 돌려준다. 실패 시 None.

    compute_crop_rect가 쓸 실제 픽셀 크기 — claude가 본 것과 같은 방향으로 맞춘다.

    ⚡ **픽셀을 디코드하지 않는다.** ``Image.open`` 은 지연 로딩이라 헤더만 읽고도
    ``size`` 를 알고, 방향은 EXIF 태그 274 한 개면 된다. 예전엔 크기 두 개를 알려고
    ``exif_transpose`` 로 **이미지 전체를 디코드+복사**했다 — 12MP 한 장에 실측
    **90.9MB** 봉우리다. 이 함수는 후기 초안 생성 중 사진마다 불리므로(그 순간
    `claude -p` 가 같이 떠 있다) 그 봉우리가 그대로 OOM 거리였다. 실측 **0.0MB**.
    EXIF 방향 8종 + 태그 없음 + PNG 에서 옛 구현과 **같은 값**임을 확인했다.
    """
    if not _HEIC_OK or not _PILImage or not data:
        return None
    try:
        with _PILImage.open(_BytesIO(data)) as im:
            w, h = im.size  # 지연 로딩 — 아직 픽셀을 읽지 않았다
            try:
                orientation = im.getexif().get(_EXIF_ORIENTATION)
            except Exception:  # noqa: BLE001 - EXIF 없음·깨짐 → 방향 보정 없음
                orientation = None
            # 5~8 은 90°/270° 회전(+전치)이라 표시 기준 축이 바뀐다.
            if orientation in (5, 6, 7, 8):
                w, h = h, w
            return (int(w), int(h))
    except Exception:  # noqa: BLE001 - 손상·비이미지·미지원
        return None


def compute_crop_rect(img_w, img_h, focus_box, target_aspect):
    """이미지 안에 들어가는 '가장 큰' target_aspect(가로/세로) 직사각형의 정규화
    크롭 [x, y, w, h]를 돌려준다(x·w는 폭 기준, y·h는 높이 기준 0..1).

    - 반환 직사각형의 '픽셀' 비율은 정확히 target_aspect(=가로/세로).
    - focus_box(정규화 [x,y,w,h] 또는 None)가 있으면 크롭이 focus 박스를 최대한
      '통째로 담도록'(CONTAIN) 축마다 독립 배치한다 — 중심만 맞추던 옛 방식은
      크거나 가장자리에 붙은 대상을 잘랐다. 각 축에서 크롭 길이 L이 focus 구간을
      담을 만큼 크면 focus를 크롭 안에 완전히 넣고(가능하면 가운데), 못 담으면
      focus 중심에 맞춘다. 어느 경우든 이미지 경계로 클램프. None이면 정중앙 크롭
      (세로 원본이면 위아래를 대칭으로 잘라 가운데 가로 스트립).
    - 업스케일·비율 왜곡 없음. 항상 경계 안(0<=x, 0<=y, x+w<=1, y+h<=1).
    """
    W = max(1.0, float(img_w or 0) or 1.0)
    H = max(1.0, float(img_h or 0) or 1.0)
    a = float(target_aspect) if target_aspect else (4 / 3)
    if a <= 0:
        a = 4 / 3

    # 이미지 안에 들어가는 가장 큰 a-비율 픽셀 직사각형.
    if W / H >= a:          # 이미지가 목표보다 넓음 → 높이에 걸림
        crop_h = H
        crop_w = a * H
    else:                   # 이미지가 목표보다 높음(세로) → 폭에 걸림
        crop_w = W
        crop_h = W / a

    def _place(L, DIM, f0, f1):
        """축 하나: 길이 L 크롭을 focus 구간 [f0,f1]을 최대한 담도록 배치(픽셀).

        L이 구간을 담을 만큼 크면 focus를 크롭 안에 완전히 넣되 가능하면 가운데
        (start=focus_center-L/2)로 두고 [f1-L, f0]로 클램프해 '완전 포함'을 보장한다.
        못 담으면 focus 중심에 맞춘다. 마지막에 이미지 경계 [0, DIM-L]로 클램프.
        """
        fc = (f0 + f1) / 2.0
        if L >= (f1 - f0):
            start = fc - L / 2.0
            # focus 전체를 담도록: start ∈ [f1 - L, f0].
            lo, hi = f1 - L, f0
            if lo > hi:  # 수치 안전(사실상 L>=폭이면 lo<=hi)
                lo, hi = hi, lo
            start = max(lo, min(hi, start))
        else:
            start = fc - L / 2.0  # 담을 수 없음 → 중심 맞춤
        # 이미지 경계로 클램프(항상 in-bounds).
        return max(0.0, min(DIM - L, start))

    if focus_box:
        try:
            fx, fy, fw, fh = (float(v) for v in focus_box)
            fx0, fx1 = fx * W, (fx + fw) * W
            fy0, fy1 = fy * H, (fy + fh) * H
            x0 = _place(crop_w, W, fx0, fx1)
            y0 = _place(crop_h, H, fy0, fy1)
        except (TypeError, ValueError):
            x0 = max(0.0, min(W - crop_w, W / 2.0 - crop_w / 2.0))
            y0 = max(0.0, min(H - crop_h, H / 2.0 - crop_h / 2.0))
    else:
        # focus 없음 → 정중앙 크롭.
        x0 = max(0.0, min(W - crop_w, W / 2.0 - crop_w / 2.0))
        y0 = max(0.0, min(H - crop_h, H / 2.0 - crop_h / 2.0))

    return [x0 / W, y0 / H, crop_w / W, crop_h / H]


def _round_aspect(number, key):
    """Pillow ``Image.thumbnail`` 내부의 반올림 — **같은 크기**를 내기 위한 사본."""
    return max(min(math.floor(number), math.ceil(number), key=key), 1)


def _thumbnail_size(w, h, max_edge):
    """``img.thumbnail((max_edge, max_edge))`` 가 만들 크기를 **미리** 계산한다.

    Pillow 의 계산을 그대로 옮긴 것이다. 왜 필요한가: 같은 결과를 원본에서 만들 때와
    렌디션에서 만들 때, 크롭 박스의 픽셀 반올림이 달라 **세로가 1px 어긋났다**
    (실측: 1280x960 vs 1280x961). 1px 이라도 '출력이 달라졌다'는 사실이 중요하므로,
    렌디션 경로는 **원본 기준으로 계산한 크기**로 정확히 리사이즈한다.
    """
    x = y = int(max_edge)
    if w <= 0 or h <= 0:
        return (max(1, x), max(1, y))
    aspect = w / h
    if x / y >= aspect:
        x = _round_aspect(y * aspect, key=lambda n: abs(aspect - n / y))
    else:
        y = _round_aspect(
            x / aspect, key=lambda n: 0 if n == 0 else abs(aspect - x / n)
        )
    return (x, y)


def _crop_box_px(img_w, img_h, crop):
    """정규화 crop → 픽셀 박스 (left, top, right, bottom). ``_blog_img_process`` 와
    **같은 반올림**을 쓴다(그래야 원본 경로와 결과 크기가 같다)."""
    W, H = int(img_w), int(img_h)
    x, y, w, h = (float(v) for v in crop)
    left = int(max(0, min(W - 1, round(x * W))))
    top = int(max(0, min(H - 1, round(y * H))))
    right = int(max(left + 1, min(W, round((x + w) * W))))
    bottom = int(max(top + 1, min(H, round((y + h) * H))))
    return left, top, right, bottom


def _blog_img_process(data, crop=None, max_edge=_BLOG_IMG_MAX_EDGE, quality=92,
                      target_size=None):
    """블로그 서빙용으로 원본 바이트를 (선택적 크롭 →) 다운스케일한 JPEG로.

    EXIF 방향 보정 → crop(정규화 [x,y,w,h], 있으면) → 긴 변 ≤ max_edge(업스케일
    안 함, 비율 유지) → RGB → JPEG(quality, optimize). ``max_edge`` 가 falsy(None/0)
    면 다운스케일 자체를 건너뛴다 — 네이버 export용 hq(원본 해상도, EXIF·crop만).
    HEIC도 같은 open으로 처리(pillow_heif 등록됨). 어떤 이유로든 실패하면 None
    (호출부가 원본 폴백해 절대 500 안 나게). 저장 원본은 손대지 않는다(메모리 사본만).

    메모리: 전체 사본을 만들지 않는 ``_exif_upright``/``_as_rgb`` 를 쓴다(위 머리말의
    실측 — hq 봉우리 143MB → 50MB, std 112MB → 66MB, 출력 픽셀은 완전히 동일).
    """
    if not _HEIC_OK or not _PILImage or not data:
        return None
    try:
        resample = getattr(getattr(_PILImage, "Resampling", _PILImage), "LANCZOS")
        with _PILImage.open(_BytesIO(data)) as im:
            img = _exif_upright(im)
            if crop:
                try:
                    x, y, w, h = (float(v) for v in crop)
                except (TypeError, ValueError):
                    x = None
                if x is not None and w > 0 and h > 0:
                    W, H = img.size
                    left = int(max(0, min(W - 1, round(x * W))))
                    top = int(max(0, min(H - 1, round(y * H))))
                    right = int(max(left + 1, min(W, round((x + w) * W))))
                    bottom = int(max(top + 1, min(H, round((y + h) * H))))
                    img = img.crop((left, top, right, bottom))
            if target_size:
                # 소스가 렌디션일 때 — '원본에서 만들었다면 나왔을 크기'로 정확히.
                # (업스케일은 하지 않는다: 요구 해상도를 못 채우는 렌디션은
                #  _blog_std_source 가 애초에 거절하고 원본으로 폴백한다.)
                tw, th = int(target_size[0]), int(target_size[1])
                if (tw, th) != img.size and tw <= img.size[0] and th <= img.size[1]:
                    img = img.resize((tw, th), resample)
                elif (tw, th) != img.size:
                    img.thumbnail((max_edge, max_edge), resample)
            elif max_edge:
                img.thumbnail((max_edge, max_edge), resample)  # 다운스케일만(업스케일 X)
            rgb = _as_rgb(img)
            out = _BytesIO()
            rgb.save(out, format="JPEG", quality=quality, optimize=True)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - 손상·비이미지·미지원 → 원본 폴백
        log.warning("blog-img 크롭/리사이즈 실패 — 원본 바이트로 폴백", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# ⛔ 이미지 소스 등급 — **원본을 아무 데나 부르지 않는다**
# --------------------------------------------------------------------------- #
# 사진 한 장에는 '하나의 URL'이 아니라 **용도별 등급**이 있다. 전부 원본으로
# 통일하면 코드는 간단해지지만, 0.1 CPU · 512MB 에서 그 간단함의 값을 사용자가
# 체감 속도로 치른다. 등급은 셋이다:
#
#   | 용도                               | 소스                      | 서버 비용 |
#   |------------------------------------|---------------------------|-----------|
#   | 갤러리 그리드 (작은 그림)           | Graph 썸네일 medium (4.4KB)| 0 (중계)  |
#   | 상세 화면·크롭 UI·비전 판단 (미리보기) | Graph 렌디션 c1280/c1600   | 0 (중계)  |
#   | 발행용 크롭 산출 (실제 결과물)       | **그때만** 원본           | 0 (스트리밍)|
#
# Graph 렌디션(`c{W}x{H}`)은 **Microsoft 가 서버에서** 비율을 지켜 줄여 준다 —
# 우리 CPU·메모리는 0이고, HEIC 도 JPEG 로 받아 브라우저 호환 문제까지 같이 풀린다.
# 상한은 문서에 없어 `onedrive.get_rendition` 이 **돌아온 크기를 보고** 판정한다
# (`tools/probe_graph_renditions.py` 로 운영에서 한 번 재면 실제 한도를 알 수 있다).
_RENDITION_PREVIEW_EDGE = 1280   # 상세 화면·크롭 UI 미리보기
_RENDITION_VISION_EDGE = 1600    # claude 비전의 크롭 '판단'용 (판단은 정규화 좌표라
                                 # 해상도 독립 — 12MP 를 보여 줄 이유가 없다)

# --------------------------------------------------------------------------- #
# 미리보기 자산 — **우리가 소유한다.** (previews.py 머리말이 근거의 단일 원천)
# --------------------------------------------------------------------------- #
# 사람이 보는 작은 그림(그리드 썸네일·크롭 UI·라이트박스)은 사진당 한 번 만들어
# DB 에 영구 보관하고, **사진당 고정 URL**로 내준다. 그래서:
#
#   * Graph 왕복 = 사진·티어당 **평생 1회** (예전엔 화면을 열 때마다 2회)
#   * 서명·만료 **없음** — 로그인한 본인에게 자기 사진을 주는 데 만료는 무의미하고,
#     토큰이 매 렌더 달라지면 **브라우저 캐시 키가 매번 깨진다**(= 캐시가 안 먹는다).
#     서명·만료는 네이버 발행용 **외부 노출** 이미지(/blog-img)에만 남는다.
#   * 재방문 = 네트워크 **0** (불변 자산 캐시 + ETag/304)
#
# `immutable` 이 정직한 이유: 사진은 업로드 후 바뀌지 않고, 자산은 (사진, 티어)의
# 결정론적 산물이다. 티어 크기를 바꿔 **이미 캐시된 브라우저**까지 새로 받게 하려면
# `PREVIEW_REV` 를 올린다 — URL 이 바뀌므로 캐시가 비껴간다.
_PREVIEW_CACHE_CONTROL = "private, max-age=31536000, immutable"


def _thumb_url(photo_id):
    """갤러리 그리드용 작은 미리보기의 고정 URL(캐시 버전 포함)."""
    return url_for("memory_thumb", photo_id=photo_id, r=previews.REV)


def _preview_url(photo_id):
    """크게 보는 미리보기(크롭 UI·라이트박스)의 고정 URL(캐시 버전 포함)."""
    return url_for("memory_preview", photo_id=photo_id, r=previews.REV)

# --------------------------------------------------------------------------- #
# std(미리보기·클립보드) 가공의 **입력**을 무엇으로 할 것인가 — 측정해서 고를 일
# --------------------------------------------------------------------------- #
# 'rendition'(기본) : 필요한 만큼만 받은 렌디션에서 자른다.
#     메모리  실측 RSS 봉우리 **90.7MB → 10.2MB** (3.03MB·3024x4032 기준)
#     화질    원본에서 자른 것과 **완전히 같지는 않다**(리샘플이 두 번 + 중간 JPEG):
#               사진형 이미지   PSNR 45.6 dB · 채널 평균차 0.76/255  (육안 동일)
#               순수 노이즈      PSNR 31.0 dB · 채널 평균차 6.1/255  (최악 케이스)
#             출력 **해상도는 완전히 동일**하다(_thumbnail_size 로 맞춘다).
# 'original'        : 예전과 똑같이 원본에서 자른다. 출력은 **바이트까지 동일**하고
#     메모리는 90.7MB 봉우리로 돌아간다.
#
# 기본을 rendition 으로 둔 근거: 이 경로가 내는 것은 **최종 발행본이 아니다.**
# 실제로 네이버에 올라가는 바이트는 브라우저가 **원본에서** 잘라 업로드하고(그쪽은
# 화질이 오히려 좋아졌다 — 서버 재인코딩 한 세대가 사라졌다), 이 std URL 은
# (a) 상세 화면 미리보기와 (b) 유저스크립트 없이 붙여넣었을 때의 **24시간 만료**
# 임시 외부 이미지에 쓰인다. 1280px 미리보기에서 0.76/255 는 보이지 않는다.
# 그래도 '바뀌었다'는 사실은 사실이므로, 한 줄로 되돌릴 수 있게 열어 둔다.
_BLOG_STD_SOURCE = (os.environ.get("BLOG_STD_SOURCE") or "rendition").strip().lower()


def _rendition_long_edge_for(img_w, img_h, crop, max_edge):
    """긴 변 ``max_edge`` 짜리 결과를 **원본에서 만든 것과 같은 픽셀 수**로 내려면
    소스 이미지의 긴 변이 최소 얼마여야 하는가. 모르면 None.

    크롭은 정규화 [x,y,w,h]이므로 잘라낸 영역의 픽셀 긴 변은 ``max(w·W, h·H)``다.
    그 값이 ``max_edge`` 이상이면 결과는 거기서 축소돼 나오므로, 소스는
    ``max(W,H) × max_edge / max(w·W, h·H)`` 만 있으면 **화질 손실이 0**이다.

    ⚠️ ``image`` 패싯의 W·H 는 EXIF 회전 전 값일 수 있다. 어느 쪽이 맞는지 모르는
    채로 작은 쪽을 고르면 해상도가 모자라므로, **두 방향 모두 계산해 더 큰 요구치**를
    택한다(모자라느니 조금 크게 받는다 — 바이트 몇십 KB 차이다).
    """
    try:
        W, H = float(img_w or 0), float(img_h or 0)
        E = float(max_edge or 0)
    except (TypeError, ValueError):
        return None
    if W <= 0 or H <= 0 or E <= 0:
        return None
    cw, ch = (float(crop[2]), float(crop[3])) if crop else (1.0, 1.0)
    if cw <= 0 or ch <= 0:
        cw = ch = 1.0
    # 방향 두 가지: (W,H) 와 (H,W). 각각의 '잘라낸 영역 긴 변'.
    longs = [max(cw * W, ch * H), max(cw * H, ch * W)]
    crop_long = min(l for l in longs if l > 0)
    if crop_long <= E:
        return int(max(W, H))        # 원본에서도 확대하지 않는다 → 원본이 곧 소스
    return int(math.ceil(max(W, H) * E / crop_long))


def _blog_std_source(photo, crop, max_edge):
    """std(미리보기·클립보드) 가공의 **입력 바이트** — 가능하면 Graph 렌디션.

    ``(data, source_label, target_size)``. 렌디션을 쓰면 12MP 를 디코드할 일이
    사라진다. 렌디션이 없거나 요구 해상도에 못 미치면 **원본으로 폴백**한다 —
    화질을 몰래 깎지 않는다.

    ``target_size`` 는 **원본에서 만들었다면 나왔을 출력 크기**다(렌디션일 때만).
    크롭 박스의 픽셀 반올림이 소스 해상도에 따라 달라져 결과가 1px 어긋나는 것을
    막는다 — 실측으로 잡은 차이다(1280x960 vs 1280x961).

    ⛔ **렌디션 경로의 어떤 실패도 여기서 끝난다.** 2026-10-07 운영 500 의 교훈:
    ``onedrive.get_rendition`` 안에서 ``requests`` 의 ``JSONDecodeError`` 가 새어
    나왔고, 이 자리의 ``except onedrive.OneDriveError`` 가 그걸 못 잡아 **설계돼
    있던 원본 폴백에 영영 닿지 못한 채** 500 이 됐다. 폴백이 정상 경로라면, 그
    폴백으로 가는 길은 **예외 종류에 의존하면 안 된다.**
    """
    item_id = photo.onedrive_item_id
    try:
        meta = None
        if _BLOG_STD_SOURCE == "rendition":
            try:
                meta = onedrive.get_item_meta(item_id)
            except Exception:  # noqa: BLE001 — 메타를 못 얻으면 원본 경로로
                log.warning("blog-img: 아이템 메타 조회 실패 — 원본 경로로 간다",
                            exc_info=True)
        if meta and meta.get("width") and meta.get("height"):
            need = _rendition_long_edge_for(meta["width"], meta["height"], crop,
                                            max_edge)
            full = max(meta["width"], meta["height"])
            if need and need < full:
                got = onedrive.get_rendition(item_id, need, min_long_edge=need)
                if got:
                    rw, rh = got[2]
                    mw, mh = meta["width"], meta["height"]
                    # ``image`` 패싯은 EXIF 회전 전일 수 있다. 렌디션은 이미 똑바로
                    # 서 있으므로, 둘의 방향이 다르면 원본 표시 크기는 뒤집힌 쪽이다.
                    if (rw > rh) != (mw > mh) and rw != rh and mw != mh:
                        mw, mh = mh, mw
                    if crop:
                        left, top, right, bottom = _crop_box_px(mw, mh, crop)
                        cw, ch = right - left, bottom - top
                    else:
                        cw, ch = mw, mh
                    target = _thumbnail_size(cw, ch, max_edge)
                    return got[0], f"rendition c{need} ({rw}x{rh})", target
    except Exception:  # noqa: BLE001 — 렌디션은 **최적화**지 전제가 아니다
        log.warning("blog-img: 렌디션 경로 실패 — 원본으로 폴백 (photo=%s)",
                    getattr(photo, "id", "?"), exc_info=True)
    data, _ctype = onedrive.get_photo_content_cached(item_id)
    return data, "original", None


# --------------------------------------------------------------------------- #
# 왜 OneDrive URL 로 **302 하지 않는가** (재서 내린 결론 — 바꾸기 전에 읽을 것)
# --------------------------------------------------------------------------- #
# "서버가 바이트를 들지 말고 브라우저를 OneDrive 로 리다이렉트하면 되잖아"는 맞는
# 직관이고, 메모리 목표도 달성한다. 그런데 **클라이언트 캐시 목표를 정면으로
# 깨뜨린다** — 그게 이 변경의 또 다른 절반이라 결론이 뒤집혔다:
#
#  1. Graph 의 pre-auth 다운로드 URL 은 **발급할 때마다 다른 URL** 이다(쿼리에
#     1회용 토큰이 박힌다). 브라우저 캐시의 키는 '최종 URL' 이므로, 302 로 보내면
#     같은 사진을 다시 봐도 **매번 캐시 미스**다. 지금처럼 `/memories/<id>/image`
#     라는 **고정 URL** 을 주고 `Cache-Control`+`ETag` 를 붙이면 재방문이 304 다.
#     (302 자체를 캐시시키면 만료된 CDN URL 을 계속 쓰게 돼 깨진 이미지가 된다.)
#  2. 302 는 Graph 왕복을 **늘린다**. 리다이렉트하려면 먼저 `/items/<id>` 를 쳐서
#     URL 을 받아야 하는데, 그 뒤 브라우저가 CDN 을 또 친다. 중계는 한 번이다.
#  3. 그 URL 은 **자격증명이다** — 쿼리의 토큰만으로 그 파일이 열린다. 우리 코드는
#     이미 영상 경로에서 같은 판단을 내려 뒀다(`onedrive.probe_direct_cors` 머리말:
#     "반환값에 URL을 절대 담지 않는다"). 브라우저 히스토리·확장·공유로 새어 나갈
#     수 있는 값을 굳이 내보낼 이유가, 위 1·2 때문에 **아예 없다.**
#
# 그래서 택한 길: **고정 URL + 제너레이터 중계 + 강한 클라이언트 캐시**. 메모리는
# 302 와 똑같이 평평하고(한 번에 64KB), 캐시는 오히려 살아난다.
# (영상은 다르다 — `<video>` 의 Range seek 때문에 `video_source` 가 CORS 를 실측해
#  가능하면 직접 URL 을 쓴다. 거기선 캐시가 아니라 대역폭이 관심사다.)
# --------------------------------------------------------------------------- #

# 사진 중계 청크. 영상(_VIDEO_CHUNK=256KB)과 같은 사상 — 한 번에 이만큼만 메모리에
# 있다. 사진은 영상보다 작으므로 64KB면 충분하고 첫 바이트가 더 빨리 나간다.
_IMAGE_STREAM_CHUNK = 64 * 1024


def _photo_etag(photo, download=False):
    """사진 응답의 ETag — 재방문을 **304 로** 끝내기 위한 값.

    이 앱에서 Photo 행의 바이트는 **절대 바뀌지 않는다**(수정 업로드가 없다 — 새
    업로드는 새 행이다). 그래서 OneDrive item id 로 충분하고, 표시본/다운로드본이
    포맷이 다르므로 그 구분만 섞는다.
    """
    item_id = getattr(photo, "onedrive_item_id", None)
    if not item_id:
        return None
    mode = "dl" if download else "disp"
    h = hashlib.sha256(f"{item_id}:{mode}".encode("utf-8")).hexdigest()[:24]
    return f'"{h}"'

_EXT_CTYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif",
}
# 브라우저가 <img>·Canvas 로 바로 그릴 수 있는 포맷. HEIC/HEIF 는 여기 없다 —
# 사파리 말고는 못 연다.
_WEB_DRAWABLE = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _ctype_for_name(name):
    return _EXT_CTYPES.get(os.path.splitext(name or "")[1].lower(), "image/jpeg")


def _stream_original(photo, want_web_format=False, disposition=None):
    """원본 바이트를 **열지 않고 흘려보낸다** — 응답은 게으른 제너레이터다.

    ``/videos/<id>/stream`` 이 쓰는 것과 같은 설비(``onedrive.open_range``)다.
    한 번에 메모리에 있는 것은 ``_IMAGE_STREAM_CHUNK`` 뿐이라, 50MB 사진을 내보내도
    워커 메모리는 평평하다. 예전엔 바이트를 통째로 변수에 담아 응답을 만들었다.

    ``want_web_format=True`` 면 브라우저가 그릴 수 있는 포맷을 보장한다:
    HEIC/HEIF 원본은 **같은 해상도의 Graph 렌디션(JPEG)** 으로 바꿔 내고(우리 CPU 0),
    Graph 가 그 크기를 못 만들면 그때만 Pillow 로 변환한다(기존 동작 — 화질 후퇴 없음).
    """
    name = photo.original_name or photo.filename or ""
    ctype = _ctype_for_name(name)

    if want_web_format and ctype not in _WEB_DRAWABLE:
        data = _web_format_bytes(photo)
        if data is not None:
            return Response(data, mimetype="image/jpeg")
        # 변환이 전부 실패하면 원본을 그대로 흘린다(사파리는 연다, 다른 곳은 못 연다 —
        # 예전과 같은 최후 폴백이다).

    try:
        status, headers, upstream = onedrive.open_range(photo.onedrive_item_id)
    except onedrive.OneDriveError:
        log.exception("이미지 스트림을 열지 못했다 (photo=%s)", photo.id)
        abort(502)
    if upstream is None:
        abort(404)  # OneDrive 에서 사라짐

    def _pump():
        try:
            for chunk in upstream.iter_content(chunk_size=_IMAGE_STREAM_CHUNK):
                if chunk:
                    yield chunk
        finally:
            upstream.close()  # 끊긴 연결에서도 소켓을 반드시 거둔다

    up_ctype = headers.get("Content-Type")
    if up_ctype and up_ctype.startswith("image/"):
        ctype = up_ctype
    resp = Response(_pump(), status=status, mimetype=ctype)
    if headers.get("Content-Length"):
        resp.headers["Content-Length"] = headers["Content-Length"]
    if disposition:
        resp.headers["Content-Disposition"] = disposition
    return resp


def _web_format_bytes(photo):
    """HEIC/HEIF 원본을 브라우저가 여는 JPEG 로 — **해상도를 깎지 않고**.

    1순위는 Graph 렌디션(원본과 같은 긴 변으로 요청 — Microsoft 가 서버에서 만든다).
    Graph 가 그만한 크기를 못 내면(문서화되지 않은 상한) 그때만 Pillow 로 변환한다.
    둘 다 안 되면 None(호출부가 원본을 그대로 흘린다). 절대 raise 하지 않는다.
    """
    item_id = photo.onedrive_item_id
    try:
        meta = onedrive.get_item_meta(item_id)
    except onedrive.OneDriveError:
        meta = None
    full = None
    if meta and meta.get("width") and meta.get("height"):
        full = max(meta["width"], meta["height"])
    if full:
        try:
            got = onedrive.get_rendition(item_id, full, min_long_edge=full)
        except onedrive.OneDriveError:
            got = None
        if got:
            return got[0]
        log.info(
            "HEIC: Graph 가 원본 해상도(%spx) 렌디션을 못 냈다 — Pillow 로 변환한다 "
            "(photo=%s)", full, photo.id,
        )
    try:
        data, _ctype = onedrive.get_photo_content_cached(item_id)
    except onedrive.OneDriveError:
        return None
    if not data:
        return None
    return _heic_to_jpeg(data)


# ---- Kakao OAuth 2.0 config (read from env; never hardcode secrets) ----
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
KAKAO_CLIENT_SECRET = os.environ.get("KAKAO_CLIENT_SECRET")  # optional
KAKAO_AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_USERINFO_URL = "https://kapi.kakao.com/v2/user/me"


def kakao_enabled() -> bool:
    """Kakao login is only offered when a REST API key is configured."""
    return bool(KAKAO_REST_API_KEY)


# ---- Web Push (VAPID) config (read from env; NEVER hardcode/commit keys) ----
# One VAPID keypair per deployment. The public key is the browser's
# applicationServerKey; the private key signs pushes. Set once on Render and
# keep stable — rotating them invalidates every existing subscription.
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT")  # e.g. mailto:you@example.com

# Shared secret guarding the daily-reminder cron endpoint.
CRON_SECRET = os.environ.get("CRON_SECRET")

# '데이트 뉴스' 새로고침이 동시에 두 번 돌지 않게 하는 최소 가드(비차단).
_events_refresh_lock = threading.Lock()

# 팝업 수집(claude 웹검색)이 동시에 두 번 돌지 않게 하는 최소 가드(비차단).
_popups_refresh_lock = threading.Lock()


def push_enabled() -> bool:
    """Web push is only wired up when a full VAPID keypair + subject exist and
    the pywebpush library is importable."""
    return bool(
        pywebpush and VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_SUBJECT
    )


def send_push(user, title, body, url):
    """Best-effort Web Push fan-out to every device ``user`` has subscribed.

    Layered on top of the in-app Notification (already persisted by the caller).
    Never raises to the caller — a push-service hiccup must not break the main
    action or the in-app row. Dead subscriptions (404/410) are pruned; other
    errors are logged. No-op when push is disabled or there's no recipient.
    """
    if user is None or not push_enabled():
        return
    subs = PushSubscription.query.filter_by(user_id=user.id).all()
    if not subs:
        return
    payload = json.dumps({"title": title, "body": body, "url": url})
    for sub in subs:
        try:
            pywebpush.webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
        except pywebpush.WebPushException as e:  # noqa: BLE001
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                # Subscription is gone (unsubscribed / expired) — prune it.
                try:
                    db.session.delete(sub)
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception("failed pruning dead push subscription %s", sub.id)
            else:
                log.warning("web push failed (status=%s): %s", status, e)
        except Exception:  # noqa: BLE001 — push must never break the caller
            log.exception("unexpected web push error for user %s", user.id)


_INVITE_ALPHABET = string.ascii_uppercase + string.digits  # unambiguous enough

# ---- minimal in-memory login rate limiting (per IP) ----
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_RL_WINDOW = 300  # seconds
_RL_MAX = 8       # attempts per window


def _normalize_db_url(url: str) -> str:
    # Heroku/Fly sometimes hand out postgres://; SQLAlchemy 2.x wants postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # ⚠️ 드라이버를 명시한다. SQLAlchemy 2.1부터 `postgresql://`의 기본 드라이버가
    # psycopg2 -> psycopg(v3)로 바뀌었는데, 우리 requirements는 psycopg2-binary다.
    # 명시하지 않으면 부팅이 `ModuleNotFoundError: No module named 'psycopg'`로 죽는다
    # (2026-10-06 실제 사고 — 코드 변경 없이 재기동만으로 터졌다).
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def create_app():
    app = Flask(__name__)

    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        db_url = _normalize_db_url(db_url)
    else:
        # Local dev fallback: SQLite file in instance/ so it runs without Postgres.
        os.makedirs(app.instance_path, exist_ok=True)
        db_url = "sqlite:///" + os.path.join(app.instance_path, "couple_daily.db")

    is_prod = os.environ.get("FLASK_ENV") == "production" or bool(os.environ.get("DATABASE_URL"))

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-insecure-change-me"),
        SQLALCHEMY_DATABASE_URI=db_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=is_prod,  # HTTPS-only cookies in prod
        PERMANENT_SESSION_LIFETIME=timedelta(days=90),  # "로그인 유지" lifetime
        # 요청 하나가 큰 사진 1장(최대 50MB)은 넉넉히 통과하되, 그보다 큰(적대적/
        # 실수) 요청은 여기서 막아 작은 Render 워커를 OOM/타임아웃에서 보호한다.
        # 업로드는 사진 단위로 쪼개 보내므로(memories.html) 이 상한이면 충분하다.
        MAX_CONTENT_LENGTH=55 * 1024 * 1024,  # 55 MB (사진 1장 + 멀티파트 여유)
        SQLALCHEMY_ENGINE_OPTIONS={
            "pool_pre_ping": True,   # 유휴 후 죽은 커넥션 자동 감지·재연결 (콜드스타트 500 원인 제거)
            "pool_recycle": 280,     # Neon/프록시가 끊기 전에 선제 재활용 (초)
        },
    )

    db.init_app(app)
    with app.app_context():
        db.create_all()
        # Bring pre-existing tables (live Postgres) up to date for Kakao login.
        run_startup_migrations()
        # Seed the configurable app name once.
        if Setting.get("app_name") is None:
            Setting.set("app_name", DEFAULT_APP_NAME)
            db.session.commit()

    _register_routes(app)
    return app


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def _safe_notify(recipient, type, message, link):
    """Create + commit a Notification without ever breaking the caller's action.

    The main action (answer/comment/approval) is already committed by the time
    this runs; a notification failure here is logged and swallowed so it can
    never undo or block that action. Skips silently when there's no recipient.

    This is the single fan-out point the future web-push branch will extend:
    persist the in-app row here, then also enqueue a push to ``recipient``.
    """
    if recipient is None:
        return
    try:
        notify(recipient, type, message, link)
        db.session.commit()
    except Exception:  # noqa: BLE001 — a notification must never break the action
        db.session.rollback()
        log.exception("notify failed (type=%s recipient=%s)", type, getattr(recipient, "id", None))
        return
    # In-app row is persisted; ALSO fan out a Web Push to the same recipient.
    # send_push never raises and is a no-op when push is disabled, so this can
    # neither undo the in-app row nor break the main action.
    send_push(recipient, Setting.get("app_name", DEFAULT_APP_NAME), message, link)


def _gen_invite_code():
    for _ in range(20):
        code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(8))
        if not Couple.query.filter_by(invite_code=code).first():
            return code
    raise RuntimeError("could not generate a unique invite code")


def _resolve_couple_for_join(invite_code: str):
    """Shared couple-attach logic for both email signup and Kakao connect.

    ``invite_code`` must already be normalized (stripped/upper-cased); "" means
    "create a new space". Returns ``(couple, is_admin, status, error)`` — on any
    validation failure ``couple`` is None and ``error`` is a Korean message.

    Mirrors the original signup rules exactly so both paths stay in sync:
      * no code  → create a new Couple, become admin, status 'approved'
      * has code → join an existing (non-full) Couple, status 'pending'
    """
    if invite_code:
        couple = Couple.query.filter_by(invite_code=invite_code).first()
        if not couple:
            return None, None, None, "초대 코드가 올바르지 않아."
        if couple.is_full:
            return None, None, None, "이 커플 공간은 이미 두 명이 꽉 찼어."
        return couple, False, "pending", None
    # First user → creates the couple space, becomes admin.
    couple = Couple(invite_code=_gen_invite_code())
    db.session.add(couple)
    db.session.flush()  # get couple.id
    return couple, True, "approved", None


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)

    return wrapped


def active_couple_required(view):
    """Only approved + linked users may use the core features."""
    @functools.wraps(view)
    def wrapped(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if not u.is_active_couple:
            return redirect(url_for("index"))
        return view(*a, **kw)

    return wrapped


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < _RL_WINDOW]
    _LOGIN_ATTEMPTS[ip] = hits
    return len(hits) >= _RL_MAX


def _record_attempt(ip: str):
    _LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())


# --------------------------------------------------------------------------- #
# 내기(Bet) — 롤링 주 헬퍼 (순수/테스트 가능; HTTP·요청 컨텍스트 불필요)
# --------------------------------------------------------------------------- #
def bet_week_window(bet, on=None):
    """습관 내기의 '현재 롤링 주' 창을 계산한다.

    주(week)는 달력 주(월~일)가 아니라 ``bet.start_date``를 기점으로 7일씩 굴러가는
    윈도우다. ``week_index = (on - start_date).days // 7`` 이고
    ``week_start = start_date + 7*week_index``, ``week_end = week_start + 6일``.

    반환: ``(week_start, week_end, week_index)``.
    ``on < start_date`` (아직 시작 전)이면 첫 주(week 0) 창을 돌려주되 ``week_index``
    를 음수로 반환해 '시작 안 함'을 신호한다(호출부가 부드럽게 처리)."""
    if on is None:
        on = date.today()
    start = bet.start_date
    delta_days = (on - start).days
    if delta_days < 0:
        # 시작 전 — 첫 주 창을 주되 음수 인덱스로 not-started 신호
        return start, start + timedelta(days=6), delta_days // 7
    k = delta_days // 7
    week_start = start + timedelta(days=7 * k)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end, k


def bet_participants(bet, couple):
    """이 내기의 참여자 목록(안정적 id 순).

    ``target_user_id``가 NULL이면 승인된 커플 구성원 두 명 모두(같이), 아니면 그
    한 명만(아직 커플에 남아 있을 때). 커플에서 빠진 대상이면 빈 목록."""
    members = sorted(couple.approved_members, key=lambda u: u.id)
    if bet.target_user_id is None:
        return members
    return [u for u in members if u.id == bet.target_user_id]


def bet_progress(bet, couple, on=None):
    """현재 롤링 주 기준 참여자별 진행 상황.

    반환 dict: ``week_start``·``week_end``·``week_index``·``started`` +
    ``participants``: 각 ``{user_id, name, count, target, done_today, met}``.
    ``count`` = 그 사용자의 [week_start, week_end] 내 BetCheckin 수,
    ``met`` = count ≥ count_target, ``done_today`` = ``on`` 날짜 체크인 존재."""
    if on is None:
        on = date.today()
    week_start, week_end, week_index = bet_week_window(bet, on)
    started = week_index >= 0
    target = bet.count_target or 0
    # 이 내기의 이번 주 체크인만 한 번에 조회(bet 자체가 커플 스코프라 커플 안전).
    rows = (
        BetCheckin.query.filter(
            BetCheckin.bet_id == bet.id,
            BetCheckin.date >= week_start,
            BetCheckin.date <= week_end,
        ).all()
    )
    by_user = {}
    for r in rows:
        by_user.setdefault(r.user_id, set()).add(r.date)
    participants = []
    for u in bet_participants(bet, couple):
        dates = by_user.get(u.id, set())
        count = len(dates)
        participants.append(
            {
                "user_id": u.id,
                "name": u.display_name,
                "count": count,
                "target": target,
                "done_today": on in dates,
                "met": started and target > 0 and count >= target,
            }
        )
    return {
        "week_start": week_start,
        "week_end": week_end,
        "week_index": week_index,
        "started": started,
        "participants": participants,
    }


def bet_week_days(week_start):
    """롤링 주 창의 7개 날짜(week_start..week_start+6)."""
    return [week_start + timedelta(days=i) for i in range(7)]


def bet_week_checkins(bet, week_start, week_end):
    """[week_start, week_end] 창의 (user_id, date) 체크인 집합 맵을 돌려준다.
    상세 화면의 요일별 체크 표시에 쓴다."""
    rows = (
        BetCheckin.query.filter(
            BetCheckin.bet_id == bet.id,
            BetCheckin.date >= week_start,
            BetCheckin.date <= week_end,
        ).all()
    )
    by_user = {}
    for r in rows:
        by_user.setdefault(r.user_id, set()).add(r.date)
    return by_user


def bet_past_weeks(bet, couple, on=None, cap=8):
    """완료된 지난 주들의 참여자별 지킴/못지킴 요약(최근 ~cap개, 최신 먼저).

    현재 주(week_index) 직전부터 과거로 최대 cap개 창을 훑는다. 작업량을 묶기 위해
    전체 범위 체크인을 한 번만 조회한 뒤 파이썬에서 창별로 센다."""
    if on is None:
        on = date.today()
    _, _, cur_index = bet_week_window(bet, on)
    if cur_index <= 0:
        return []  # 아직 완료된 지난 주가 없음
    parts = bet_participants(bet, couple)
    target = bet.count_target or 0
    start = bet.start_date
    first = max(0, cur_index - cap)
    range_start = start + timedelta(days=7 * first)
    range_end = start + timedelta(days=7 * cur_index - 1)  # 현재 주 직전까지
    rows = (
        BetCheckin.query.filter(
            BetCheckin.bet_id == bet.id,
            BetCheckin.date >= range_start,
            BetCheckin.date <= range_end,
        ).all()
    )
    weeks = []
    for k in range(cur_index - 1, first - 1, -1):  # 직전 주 → 과거 순
        ws = start + timedelta(days=7 * k)
        we = ws + timedelta(days=6)
        per = []
        for u in parts:
            cnt = sum(1 for r in rows if r.user_id == u.id and ws <= r.date <= we)
            per.append(
                {
                    "user_id": u.id,
                    "name": u.display_name,
                    "count": cnt,
                    "target": target,
                    "met": target > 0 and cnt >= target,
                }
            )
        weeks.append(
            {
                "week_index": k,
                "week_start": ws,
                "week_end": we,
                "participants": per,
            }
        )
    return weeks


def bet_end_info(bet, on=None):
    """내기의 '종료일(마감)' 표시 정보(순수). 목록·상세·체크인 판정 공용.

    반환 dict:
      * ``has_end``   — end_date가 설정돼 있나.
      * ``end_date``  — 종료일(또는 None).
      * ``closed``    — 마감됐나. status가 'ended'거나 (종료일이 오늘보다 과거).
      * ``days_left`` — 오늘 기준 남은 일수(종료일 - 오늘). 종료일 없으면 None.
                        0이면 오늘이 마감일(아직 체크인 가능), 음수면 지남."""
    if on is None:
        on = date.today()
    end = bet.end_date
    if end is None:
        return {
            "has_end": False,
            "end_date": None,
            "closed": bet.status == "ended",
            "days_left": None,
        }
    return {
        "has_end": True,
        "end_date": end,
        "closed": bet.status == "ended" or end < on,
        "days_left": (end - on).days,
    }


# --------------------------------------------------------------------------- #
# 내기(Bet) — 예측 내기(2b) 헬퍼 (순수/테스트 가능; HTTP·요청 컨텍스트 불필요)
# --------------------------------------------------------------------------- #
def prediction_members(couple):
    """예측 내기의 '안정적 a/b → 구성원' 매핑을 돌려준다.

    a = 승인 구성원 중 id가 작은 사람(첫째), b = 둘째. 모든 곳에서 *같은* id 오름차순
    정렬을 써서 'a'/'b'가 항상 같은 사람을 가리키게 한다(별도 컬럼 없이 렌더 시 파생).
    승인 구성원이 2명 미만이면 부족분은 None."""
    members = sorted(couple.approved_members, key=lambda u: u.id) if couple else []
    a = members[0] if len(members) >= 1 else None
    b = members[1] if len(members) >= 2 else None
    return a, b


def prediction_result(bet, couple):
    """예측 내기 렌더용 결과 dict(순수). habit 내기면 None.

    반환: ``a_name``·``b_name``(안정적 a/b 순서의 표시 이름), ``guess_a``·``guess_b``
    (각자 예측 날짜), ``actual``(실제 날짜 또는 None), ``winner``('a'|'b'|'tie'|None),
    ``winner_name``(승자 이름, 무승부/미해결이면 None), ``da``·``db``(실제 날짜와의
    |차이| 일수 — 실제 날짜가 없으면 None). 목록·상세·결과 flash에서 공용으로 쓴다."""
    if bet.type != "prediction":
        return None
    a, b = prediction_members(couple)
    a_name = a.display_name if a else "A"
    b_name = b.display_name if b else "B"
    da = dist_b = None
    if bet.actual_date is not None:
        if bet.guess_a_date is not None:
            da = abs((bet.guess_a_date - bet.actual_date).days)
        if bet.guess_b_date is not None:
            dist_b = abs((bet.guess_b_date - bet.actual_date).days)
    winner_name = None
    if bet.winner == "a":
        winner_name = a_name
    elif bet.winner == "b":
        winner_name = b_name
    return {
        "a_name": a_name,
        "b_name": b_name,
        "guess_a": bet.guess_a_date,
        "guess_b": bet.guess_b_date,
        "actual": bet.actual_date,
        "winner": bet.winner,  # 'a' | 'b' | 'tie' | None
        "winner_name": winner_name,  # 무승부/미해결이면 None
        "da": da,
        "db": dist_b,
    }


def get_or_create_today_question(couple: Couple) -> DailyQuestion:
    today = date.today()
    q = DailyQuestion.query.filter_by(couple_id=couple.id, q_date=today).first()
    if q:
        return q

    # Personalize from recent history (most recent first, excluding today).
    recent = (
        DailyQuestion.query.filter(
            DailyQuestion.couple_id == couple.id, DailyQuestion.q_date < today
        )
        .order_by(DailyQuestion.q_date.desc())
        .limit(8)
        .all()
    )
    recent_pairs = [
        {"question": r.text, "answers": [a.text for a in r.answers.all()]}
        for r in recent
    ]
    # 의미 중복 회피용 — 개인화(8개)보다 넓게 지난 질문 '텍스트'만 최대 40개 모은다.
    past_questions = [
        text_ for (text_,) in (
            DailyQuestion.query.with_entities(DailyQuestion.text)
            .filter(
                DailyQuestion.couple_id == couple.id,
                DailyQuestion.q_date < today,
            )
            .order_by(DailyQuestion.q_date.desc())
            .limit(40)
            .all()
        )
    ]
    # ⚠️ 이 앱에서 claude 가 **요청 경로에서** 도는 유일한 자리다(하루 한 번, 그날
    # 첫 방문). 여기에 세마포어를 무한 대기로 걸면 후기 초안 생성(세마포어를 수 분간
    # 쥔다) 뒤에 줄을 서다가 gunicorn `--timeout 180` 에 걸려 502 가 난다. 반대로
    # 세마포어 없이 돌리면 claude 가 둘이 뜬다 — 실측 한 개 ≈ 400MB 라 512MB
    # 티어는 그 자리에서 죽는다(2026-10-07 OOM 의 유력 경로).
    # 그래서 **짧게만 기다린다**:
    #   * 슬롯을 잡으면 → 지금까지와 똑같이 개인화 질문을 생성한다.
    #   * 못 잡으면 → claude 를 돌리지 않고 폴백 질문으로 **즉시** 행을 만들고,
    #     슬롯이 비는 대로 백그라운드가 같은 행을 개인화 질문으로 **올려준다**
    #     (아래 upgrade_daily_question). 기능을 깎지 않으려는 장치다 — 폴백 질문이
    #     하루 종일 남지 않는다.
    upgrade_later = False
    if _CAPTION_SEM.acquire(timeout=_QUESTION_SLOT_WAIT):
        try:
            text, source = ai.generate_daily_question(
                recent_pairs, past_questions=past_questions
            )
        finally:
            _CAPTION_SEM.release()
    else:
        log.info("오늘의 질문: claude 슬롯이 바빠 폴백으로 띄우고 나중에 올린다")
        text, source = ai.fallback_daily_question(recent_pairs)
        upgrade_later = True

    q = DailyQuestion(couple_id=couple.id, q_date=today, text=text, source=source)
    db.session.add(q)
    try:
        db.session.commit()
    except IntegrityError:
        # Partner generated it concurrently — take theirs.
        db.session.rollback()
        q = DailyQuestion.query.filter_by(couple_id=couple.id, q_date=today).first()
        upgrade_later = False
    if upgrade_later and q is not None:
        _spawn_question_upgrade(current_app._get_current_object(), q.id)
    return q


def build_comment_thread(q: DailyQuestion):
    """Return ``[(top_comment, [replies…]), …]`` for a question, oldest-first.

    Comments form an adjacency list (``parent_id``). For a calm, readable UI we
    render exactly two visual levels: each top-level comment, then a *flat* list
    of all its descendants (any depth) at a single reply indent. Returns the
    grouped thread plus the total comment count.
    """
    comments = q.ordered_comments()  # already oldest-first
    by_id = {c.id: c for c in comments}
    tops = []
    replies_of = {}
    for c in comments:
        # Walk up to the top-level ancestor (guards against orphaned parents).
        root = c
        while root.parent_id and root.parent_id in by_id:
            root = by_id[root.parent_id]
        if root.id == c.id:
            tops.append(c)
        else:
            replies_of.setdefault(root.id, []).append(c)
    thread = [(t, replies_of.get(t.id, [])) for t in tops]
    return thread, len(comments)


# --------------------------------------------------------------------------- #
# Monthly report — cached AI qualitative insight, (re)built in the background
# --------------------------------------------------------------------------- #
# The /insight page must render instantly from the cached MonthlyReport row and
# NEVER run the slow `claude -p` subprocess on the request path. Generation is
# offloaded to a daemon thread that owns its own app context + DB session.
#
# Concurrency guards (two layers):
#   * DB-level: the report row's status='generating' + updated_at. A fresh
#     'generating' means a build is in flight; a 'generating' older than
#     _STUCK_GENERATING is treated as stale (a crashed/killed thread) and is
#     retryable so a month can never get stuck showing the placeholder forever.
#   * In-process: a set of (couple_id, year, month) keys currently generating,
#     protected by a lock. This is the fast, exact guard for the single-worker
#     Render free tier (one gunicorn worker) so rapid events don't double-spawn.
_generating_lock = threading.Lock()
_generating: set[tuple[int, int, int]] = set()
_STUCK_GENERATING = timedelta(minutes=5)


# --------------------------------------------------------------------------- #
# 'pending 행 + 인프로세스 데몬 스레드' 공통 복구 규율 (실측된 사고에서 승격)
# --------------------------------------------------------------------------- #
# 이 앱의 모든 AI 생성은 "DB 행을 pending/generating 으로 세우고 데몬 스레드를 띄운다"
# 는 한 패턴을 쓴다. 그 패턴에는 구멍이 하나 있다: **프로세스가 죽으면 스레드도 같이
# 죽는데 행은 pending 그대로 영원히 남는다.** 2026-10-07 운영 사고가 정확히 이것이다 —
# Render 가 메모리 한도 초과로 워커를 자동 재시작했고, 생성 중이던 블로그 초안 행이
# '작성중'에 영구히 박혔다(사용자는 화면에서 빠져나갈 길조차 없었다).
#
# 월간 회고는 이미 이 규율(_STUCK_GENERATING)을 갖고 있었지만 그 한 곳에만 걸려
# 있었다. 아래 두 함수가 그 규율을 **패턴 전체의 단일 원천**으로 올린다. 판정은 두 겹:
#   * 인프로세스 가드: 이 워커가 그 작업의 키를 들고 있나. 재시작 뒤엔 이 집합이
#     비어 있으므로 '죽은 스레드'를 **즉시**(상한을 기다리지 않고) 알아본다.
#     — 이 앱은 단일 gunicorn 워커가 전제다(Dockerfile `--workers ${WEB_CONCURRENCY:-1}`,
#       claude 직렬화용 _CAPTION_SEM 도 프로세스 단위다). 그 전제 위에서 이 가드는
#       '아무도 안 만들고 있다'와 정확히 같은 뜻이다.
#   * 행 신선도: 가드가 비어 있어도 스폰 직후의 찰나(_BG_SPAWN_GRACE)는 유예한다.
#     가드가 차 있어도 상한을 넘겼으면(hang·무한 대기) 사람에게 탈출구를 준다.
_BG_SPAWN_GRACE = timedelta(seconds=60)


def _enqueue_ai(app_obj, kind, key, payload, couple_id=None):
    """느린 claude 작업 하나를 **DB 큐**에 세우고 펌프가 돌고 있는지 확인한다.

    예전엔 여기서 데몬 스레드를 띄웠다. 스레드는 프로세스가 죽으면 같이 죽고,
    DB 행은 ``pending`` 으로 남아 **아무도 그 화면을 열지 않으면 영원히 '작성중'**
    이었다(2026-10-07 실사고). 줄을 DB 에 적으면 다음 부팅의 펌프가 **아무도 안 보고
    있어도** 이어서 한다. 같은 대상에 대한 중복 요청은 큐의 유니크 키가 흡수한다.

    True = 줄에 있다(새로 넣었거나 이미 있었다). False = 못 넣었다 — 호출부가
    인프로세스 가드를 되돌려, 다음 방문의 ``resume_*`` 가 다시 시도하게 한다.
    """
    try:
        ok = aijobs.enqueue(kind, key, payload, couple_id=couple_id)
    except Exception:  # noqa: BLE001 — 큐가 삐끗해도 요청을 깨뜨리지 않는다
        log.exception("ai job enqueue 실패 (kind=%s key=%s)", kind, key)
        return False
    if ok:
        aijobs.start_pump(app_obj)
    return ok


def _bg_job_verdict(lock, keys, key, updated_at, stuck_after, job_key=None):
    """백그라운드 생성 한 건의 상태를 판정한다 — 'running' / 'orphaned' / 'overdue'.

    * ``running``  — 정상 생성 중(줄에 있거나 가드 보유, 상한 이내). 기다리면 된다.
    * ``orphaned`` — 만드는 쪽이 사라졌다(프로세스 재시작). **되살려야 한다.**
    * ``overdue``  — 등록은 돼 있는데 상한을 넘겼다(hang·세마포어 장기 대기).
      자동으로 할 수 있는 일은 없으니 화면이 사람에게 탈출구를 줘야 한다.

    ``job_key`` 를 주면 **DB 작업 큐**를 먼저 본다. 인프로세스 가드는 재시작에
    비워지지만 큐 행은 남으므로, 큐에 있으면 그 일은 '사라진' 게 아니라 '아직
    차례'다 — 그걸 orphaned 로 읽으면 멀쩡히 줄 선 일을 다시 되살린다고 떠든다.
    """
    now = datetime.utcnow()
    age = now - (updated_at or now)
    with lock:
        alive = key in keys
    if not alive and job_key:
        # 큐에 적혀 있으면 살아 있는 것이다(펌프가 순서대로 집는다).
        try:
            alive = aijobs.pending(job_key)
        except Exception:  # noqa: BLE001 — 판정이 페이지를 깨뜨리지 않게
            log.debug("ai job pending 조회 실패 (%s)", job_key, exc_info=True)
    if not alive:
        return "orphaned" if age >= _BG_SPAWN_GRACE else "running"
    return "overdue" if age >= stuck_after else "running"


def regenerate_monthly_report(app, couple_id, year, month):
    """Background worker: (re)build the cached qualitative report for a month.

    Runs in a daemon thread. Pushes a fresh app context (which yields a NEW
    thread-local scoped DB session — request sessions are never shared across
    threads) and calls the slow `claude -p` pass, then upserts the row:
      * success  → content fields + status='ready' + generated_at.
      * failure/None → status='failed', UNLESS the row already had a good
        report (generated_at set), in which case the prior 'ready' content is
        kept so the user still sees the last good insight.
    Never raises out of the thread; always frees the in-process guard key.
    """
    key = (couple_id, year, month)
    try:
        with app.app_context():
            try:
                couple = db.session.get(Couple, couple_id)
                members = couple.approved_members if couple else []
                if len(members) < 2:
                    return  # solo space — nothing two-person to summarize
                user_a, user_b = members[0], members[1]

                # 크로스도메인 컨텍스트(Q&A + 사진 + 판결 + 다녀온 데이트)를 모아
                # 하나의 따뜻한 '우리의 N월' 회고를 생성한다. 전부 저렴한 DB 쿼리로
                # 모으고, 느린 claude 호출은 이 백그라운드 스레드에서만 돈다.
                context = insights.collect_month_context(
                    couple_id, year, month, user_a, user_b
                )
                month_label = f"{year}년 {month}월"
                # 캡션·채점·추천·후기와 '같은' _CAPTION_SEM으로 직렬화한다.
                # ⚠️ 이게 빠져 있었다 — 월간 회고는 /answer 커밋마다 트리거되므로,
                # 후기 초안(170초+ 동안 세마포어를 쥔다)이 도는 중에 답변 하나만
                # 들어와도 claude 가 둘이 됐다. 실측 `claude -p` 한 개 ≈ 400MB →
                # 둘이면 512MB 티어가 즉사한다(2026-10-07 OOM 의 유력 경로).
                _CAPTION_SEM.acquire()
                try:
                    result = ai.generate_monthly_report(
                        month_label,
                        user_a.display_name,
                        user_b.display_name,
                        context,
                    )
                except Exception:  # noqa: BLE001 — claude must never crash the thread
                    log.exception(
                        "claude monthly report raised (couple=%s %s-%s)",
                        couple_id, year, month,
                    )
                    result = None
                finally:
                    _CAPTION_SEM.release()

                report = MonthlyReport.query.filter_by(
                    couple_id=couple_id, year=year, month=month
                ).first()
                if report is None:
                    report = MonthlyReport(
                        couple_id=couple_id, year=year, month=month
                    )
                    db.session.add(report)

                now = datetime.utcnow()
                if result:
                    # 새 구조화 리포트 전문을 저장 + 레거시 필드도 계속 채워
                    # 하위 호환(과거 캐시/폴백)을 유지한다.
                    report.report_json = json.dumps(result, ensure_ascii=False)
                    report.summary = result.get("summary") or ""
                    report.themes = json.dumps(
                        result.get("themes") or [], ensure_ascii=False
                    )
                    report.tone = result.get("tone") or ""
                    report.fun = result.get("fun") or ""
                    report.status = "ready"
                    report.generated_at = now
                elif report.generated_at is not None:
                    # Generation failed but we have prior good content — keep it
                    # visible rather than degrading to a placeholder.
                    report.status = "ready"
                else:
                    report.status = "failed"
                report.updated_at = now

                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception(
                        "commit failed for monthly report (couple=%s %s-%s)",
                        couple_id, year, month,
                    )
            except Exception:  # noqa: BLE001 — belt & suspenders; never escape
                db.session.rollback()
                log.exception(
                    "regenerate_monthly_report failed (couple=%s %s-%s)",
                    couple_id, year, month,
                )
    finally:
        with _generating_lock:
            _generating.discard(key)


def _kick_monthly_report(couple_id, year, month):
    """Atomically claim a month for generation and spawn the background thread.

    Must be called inside an app/request context. No-ops (does NOT spawn) when a
    build is already in flight (in-process key held, or a fresh DB 'generating').
    Marks the row status='generating' (creating it if missing) and commits BEFORE
    spawning, so a concurrent request sees the claim. Content columns are left
    untouched so a prior good report survives the regen window.
    """
    key = (couple_id, year, month)
    now = datetime.utcnow()

    report = MonthlyReport.query.filter_by(
        couple_id=couple_id, year=year, month=month
    ).first()
    # A fresh in-flight DB claim → don't pile on. A stale one (crashed thread) is
    # retryable so the month can't be wedged in 'generating' forever.
    if (
        report is not None
        and report.status == "generating"
        and report.updated_at
        and (now - report.updated_at) < _STUCK_GENERATING
    ):
        return

    with _generating_lock:
        if key in _generating:
            return
        _generating.add(key)

    spawned = False
    try:
        if report is None:
            report = MonthlyReport(
                couple_id=couple_id, year=year, month=month, status="generating"
            )
            db.session.add(report)
        else:
            report.status = "generating"
        report.updated_at = now
        try:
            db.session.commit()
        except IntegrityError:
            # A concurrent request created the row first — take theirs.
            db.session.rollback()
            report = MonthlyReport.query.filter_by(
                couple_id=couple_id, year=year, month=month
            ).first()
            if report is None:
                raise
            report.status = "generating"
            report.updated_at = now
            db.session.commit()

        spawned = _enqueue_ai(
            current_app._get_current_object(), "monthly",
            f"monthly:{couple_id}:{year}:{month}",
            {"couple_id": couple_id, "year": year, "month": month},
            couple_id=couple_id,
        )
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception(
            "failed to kick monthly report (couple=%s %s-%s)",
            couple_id, year, month,
        )
    finally:
        if not spawned:
            with _generating_lock:
                _generating.discard(key)


# --------------------------------------------------------------------------- #
# Photo captioning (claude vision) — background, never on the request path
# --------------------------------------------------------------------------- #
# Same discipline as the monthly report: a slow `claude` subprocess (here a
# vision pass) must run off the request thread with its own app context + fresh
# DB session, catch everything, and never escape the thread. Captioning is
# per-photo (each photo is unique), so a simple in-process guard set keyed by
# photo_id is enough to stop a double-spawn for the same photo.
_captioning_lock = threading.Lock()
_captioning: set[int] = set()

# claude 동시성 상한 — 기본 1(직렬화). 512MB 단일 워커는 `claude`(Node) 프로세스를
# 한 번에 **하나밖에** 못 버틴다. 실측(2026-10-07): `claude -p` 한 개의 RSS 봉우리가
# 약 400MB다(앱과 같은 argv·stdin 프롬프트, 넉넉한 머신 기준 상한값 — 컨테이너에선
# V8 힙이 더 작게 잡혀 그보다 낮지만, **둘이 동시에 뜨면 512MB 를 넘는다**는 결론은
# 같다). 그래서 이 세마포어는 '캡션용'이 아니라 **이 앱의 모든 claude 호출**이 지나야
# 하는 단 하나의 문이다.
#
# ⚠️ 2026-10-07 OOM 직전까지 이 문을 **안 지나는 claude 호출이 셋** 있었다 —
# 월간 회고(/answer 마다 트리거)·판결(버튼)·오늘의 질문(요청 경로). 신규 기능(키워드
# 조사→썸네일→GIF)이 들어오면서 후기 초안이 세마포어를 쥐는 시간이 ~20초에서
# **170초+** 로 늘자, 그 위에 셋 중 하나가 겹칠 확률이 같이 뛰었다. 이제 전부 이 문을
# 지난다(아래 _QUESTION_SLOT_WAIT 참고 — 요청 경로만 특별 취급).
_CAPTION_SEM = threading.Semaphore(int(os.environ.get("CAPTION_MAX_CONCURRENCY", "1")))

# 요청 경로(오늘의 질문)가 claude 슬롯을 기다릴 수 있는 최대 시간(초).
# gunicorn `--timeout 180` 안에 반드시 들어와야 한다: 대기 15s + claude 최대 120s
# (ai.CLAUDE_TIMEOUT) = 135s < 180s. 못 잡으면 폴백 질문으로 즉시 응답하고
# 백그라운드가 개인화 질문으로 올려준다(upgrade_daily_question).
_QUESTION_SLOT_WAIT = 15

_question_upgrade_lock = threading.Lock()
_question_upgrades: set[int] = set()


def upgrade_daily_question(app, question_id):
    """폴백으로 즉시 띄운 '오늘의 질문'을 개인화 질문으로 **올려준다**(백그라운드).

    요청 경로가 claude 슬롯을 못 잡았을 때만 돈다. 슬롯이 비기를 기다렸다가 평소와
    같은 생성을 돌리고, 같은 행의 ``text``/``source`` 를 갈아끼운다. 기능을 깎지
    않으려는 장치다 — 바쁜 순간에 걸렸다고 폴백 질문이 하루 종일 남지 않는다.

    ⚠️ **아직 아무도 답하지 않았을 때만** 바꾼다. 한 사람이라도 답했으면 그가 본
    질문이 손에서 바뀌면 안 되므로 폴백 질문을 그대로 둔다.
    절대 raise 하지 않고 finally 에서 가드를 푼다.
    """
    try:
        with app.app_context():
            try:
                q = db.session.get(DailyQuestion, question_id)
                if q is None or q.source != "fallback" or q.answers.count():
                    return
                couple = db.session.get(Couple, q.couple_id)
                if couple is None:
                    return
                recent = (
                    DailyQuestion.query.filter(
                        DailyQuestion.couple_id == couple.id,
                        DailyQuestion.q_date < q.q_date,
                    )
                    .order_by(DailyQuestion.q_date.desc())
                    .limit(8)
                    .all()
                )
                recent_pairs = [
                    {"question": r.text, "answers": [a.text for a in r.answers.all()]}
                    for r in recent
                ]
                past_questions = [
                    t for (t,) in (
                        DailyQuestion.query.with_entities(DailyQuestion.text)
                        .filter(
                            DailyQuestion.couple_id == couple.id,
                            DailyQuestion.q_date < q.q_date,
                        )
                        .order_by(DailyQuestion.q_date.desc())
                        .limit(40)
                        .all()
                    )
                ]
                _CAPTION_SEM.acquire()
                try:
                    text, source = ai.generate_daily_question(
                        recent_pairs, past_questions=past_questions
                    )
                except Exception:  # noqa: BLE001
                    log.exception("오늘의 질문 업그레이드 생성 실패 (q=%s)", question_id)
                    return
                finally:
                    _CAPTION_SEM.release()
                if source != "ai" or not text:
                    return  # 또 폴백이면 굳이 갈아끼우지 않는다
                # 기다리는 사이 누가 답했을 수 있다 — 다시 확인하고 그때만 바꾼다.
                q = db.session.get(DailyQuestion, question_id)
                if q is None or q.source != "fallback" or q.answers.count():
                    return
                q.text = text
                q.source = source
                db.session.commit()
                log.info("오늘의 질문을 개인화 질문으로 올렸다 (q=%s)", question_id)
            except Exception:  # noqa: BLE001 — 절대 스레드 밖으로 내보내지 않는다
                db.session.rollback()
                log.exception("upgrade_daily_question failed (q=%s)", question_id)
    finally:
        with _question_upgrade_lock:
            _question_upgrades.discard(question_id)


def _spawn_question_upgrade(app, question_id):
    """위 업그레이드 스레드를 스폰(이미 돌고 있으면 no-op). 절대 raise 안 한다."""
    with _question_upgrade_lock:
        if question_id in _question_upgrades:
            return
        _question_upgrades.add(question_id)
    if not _enqueue_ai(app, "question_upgrade", f"question:{question_id}",
                       {"question_id": question_id}):
        with _question_upgrade_lock:
            _question_upgrades.discard(question_id)

# Image extensions claude's Read tool recognizes; used to give the temp file the
# right suffix so vision actually ingests it. Falls back to .jpg.
_CAPTION_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}


def _caption_tmp_dir():
    """A scratch dir for vision temp images, INSIDE the process working dir.

    Load-bearing: `claude` sandboxes its file reads to its working directory
    (the subprocess cwd == the app's cwd). A temp image written to the SYSTEM
    temp dir (%TEMP% / /tmp) is outside that sandbox and claude refuses to read
    it — verified. Writing under cwd keeps the image inside the sandbox so vision
    actually ingests it. The dir is git-ignored and files are deleted after use.
    """
    d = os.path.join(os.getcwd(), ".caption-tmp")
    os.makedirs(d, exist_ok=True)
    return d


def caption_photo(app, photo_id):
    """Background worker: caption ONE uploaded photo with `claude` vision.

    Pushes a fresh app context (→ a new thread-local DB session), loads the
    Photo, downloads its bytes from OneDrive to a temp file with the correct
    extension, runs the slow vision pass, and stores the result:
      * success → ``caption`` + ``tags`` (JSON) + ``caption_status='ready'``.
      * failure/timeout/parse-error → ``caption_status='failed'`` (caption null).
    ALWAYS deletes the temp file (finally) and ALWAYS frees the guard key. Never
    raises out of the thread.
    """
    with _captioning_lock:
        if photo_id in _captioning:
            return  # already being captioned — don't pile on
        _captioning.add(photo_id)

    try:
        with app.app_context():
            tmp_path = None
            try:
                photo = db.session.get(Photo, photo_id)
                if photo is None:
                    return  # deleted before we got to it

                item_id = photo.onedrive_item_id
                name_for_ext = photo.filename or photo.original_name or ""

                # 메모리 무거운 구간(사진 바이트 다운로드 + 임시파일 기록 + claude
                # vision)을 세마포어로 직렬화한다: 단일 워커 512MB 티어는 한 번에 사진
                # 하나의 바이트만 메모리에 올리고 claude(Node) 프로세스도 하나만 돌릴 수
                # 있다(동시 실행 시 OOM → 캡션 실패). 다른 캡션 스레드는 여기서 줄 서서
                # 하나씩 순차 처리된다.
                _CAPTION_SEM.acquire()
                try:
                    try:
                        data, _ctype = onedrive.get_photo_content(item_id)
                    except onedrive.OneDriveError:
                        log.exception(
                            "caption_photo: OneDrive fetch failed (photo=%s)", photo_id
                        )
                        data = None

                    if not data:
                        photo.caption_status = "failed"
                        db.session.commit()
                        return

                    ext = os.path.splitext(name_for_ext)[1].lower()
                    if ext not in _CAPTION_IMAGE_EXTS:
                        ext = ".jpg"
                    fd, tmp_path = tempfile.mkstemp(
                        prefix="cd_caption_", suffix=ext, dir=_caption_tmp_dir()
                    )
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)

                    result = ai.caption_image(tmp_path)
                finally:
                    _CAPTION_SEM.release()

                # Re-load in case the row changed; commit the outcome.
                photo = db.session.get(Photo, photo_id)
                if photo is None:
                    return
                if result and result.get("caption"):
                    photo.caption = (result["caption"] or "")[:1000]
                    photo.tags = json.dumps(
                        result.get("tags") or [], ensure_ascii=False
                    )
                    photo.caption_status = "ready"
                else:
                    photo.caption_status = "failed"
                db.session.commit()
            except Exception:  # noqa: BLE001 — never let the thread crash
                db.session.rollback()
                log.exception("caption_photo failed (photo=%s)", photo_id)
                # Best-effort: don't leave the row stuck on 'pending' forever.
                try:
                    photo = db.session.get(Photo, photo_id)
                    if photo is not None and photo.caption_status == "pending":
                        photo.caption_status = "failed"
                        db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        log.exception(
                            "caption_photo: temp cleanup failed (%s)", tmp_path
                        )
    finally:
        with _captioning_lock:
            _captioning.discard(photo_id)


def _spawn_caption_if_idle(app, photo_id):
    """Spawn a background caption thread for ONE photo unless it's already
    queued/running (checked against the same ``_captioning`` guard that
    ``caption_photo`` uses, so we never pile a second thread on the same photo).
    Best-effort — never raises. Safe now that captioning is serialized via the
    semaphore, so re-spawning many pending photos just queues them one at a time.
    """
    with _captioning_lock:
        if photo_id in _captioning:
            return
    _enqueue_ai(app, "caption", f"caption:{photo_id}", {"photo_id": photo_id})


def _warm_previews(app, photo_ids):
    """업로드 직후 미리보기 자산(grid)을 만들어 둔다 — 배경, best-effort.

    claude 와 **무관한** 값싼 HTTP 한 번이라 ``_CAPTION_SEM`` 뒤에 줄 서지 않는다
    (거기 세우면 캡션 뒤에서 몇 분을 기다린다). 실패해도 아무 일도 일어나지 않는다 —
    갤러리가 그 사진을 처음 열 때 그때 만든다.
    """
    try:
        with app.app_context():
            for pid in photo_ids or []:
                try:
                    photo = db.session.get(Photo, pid)
                    if photo is not None:
                        previews.ensure(photo, "grid")
                except Exception:  # noqa: BLE001 — 사진당 실패 격리
                    log.exception("preview warm 실패 (photo=%s)", pid)
            db.session.remove()
    except Exception:  # noqa: BLE001 — 절대 스레드 밖으로 나가지 않는다
        log.exception("preview warm 스레드가 예외로 끝났다")


def _spawn_warm_previews(app, photo_ids):
    """``_warm_previews`` 를 배경 스레드로. 절대 raise 하지 않는다."""
    ids = [int(i) for i in (photo_ids or [])]
    if not ids:
        return
    try:
        threading.Thread(
            target=_warm_previews, args=(app, ids), daemon=True
        ).start()
    except Exception:  # noqa: BLE001 — 미리보기 예열은 best-effort
        log.exception("failed to spawn preview warm thread")


# --------------------------------------------------------------------------- #
# 커플 싸움 AI 판사 — background verdict, never on the request path
# --------------------------------------------------------------------------- #
# Same discipline as the monthly report / photo captioner: the slow `claude -p`
# judge pass runs off the request thread with its own app context + fresh DB
# session, catches everything, and never escapes the thread. Per-case in-process
# guard set keyed by case_id stops a double-spawn for the same case.
_judging_lock = threading.Lock()
_judging: set[int] = set()


def judge_case(app, case_id):
    """Background worker: run the AI judge for ONE case, mirroring
    ``regenerate_monthly_report``.

    Pushes a fresh app context (→ a NEW thread-local DB session — request
    sessions are never shared across threads), loads the Case + its statements +
    the two partners' display names, runs the slow judge pass, and upserts the
    outcome:
      * success  → ``verdict_json`` + status='decided' + decided_at.
      * failure/None → status='failed', UNLESS the case already had a good
        verdict (decided_at set), in which case the prior verdict is kept
        ('decided') so the couple still sees the last good judgment.
    Never raises out of the thread; always frees the in-process guard key.
    """
    try:
        with app.app_context():
            try:
                case = db.session.get(Case, case_id)
                if case is None:
                    return  # deleted before we got to it

                couple = db.session.get(Couple, case.couple_id)
                members = couple.approved_members if couple else []
                # Two people (any order) — used only for display names in the
                # prompt. A statement author who left may not be a current member,
                # so resolve names per-statement below, falling back to members.
                name_a = members[0].display_name if len(members) >= 1 else "한 사람"
                name_b = members[1].display_name if len(members) >= 2 else "상대"

                statements = []
                for st in case.statements:
                    author = db.session.get(User, st.user_id)
                    nm = author.display_name if author else "익명"
                    statements.append({"name": nm, "text": st.text})

                # 캡션·채점·추천·후기와 '같은' _CAPTION_SEM으로 직렬화한다.
                # ⚠️ 이게 빠져 있었다 — 판결은 사람이 아무 때나 누르는 버튼이라
                # 후기 초안 생성(170초+ 세마포어 보유) 위에 그대로 겹쳤다.
                # 실측 `claude -p` 한 개 ≈ 400MB → 둘이면 512MB 티어가 즉사한다.
                _CAPTION_SEM.acquire()
                try:
                    result = ai.judge_fight(
                        name_a, name_b, case.situation, statements
                    )
                except Exception:  # noqa: BLE001 — claude must never crash the thread
                    log.exception("claude fight judgment raised (case=%s)", case_id)
                    result = None
                finally:
                    _CAPTION_SEM.release()

                # Re-load in case the row changed while claude ran.
                case = db.session.get(Case, case_id)
                if case is None:
                    return
                now = datetime.utcnow()
                if result:
                    case.verdict_json = json.dumps(result, ensure_ascii=False)
                    case.status = "decided"
                    case.decided_at = now
                elif case.decided_at is not None:
                    # Judgment failed but we have a prior good verdict — keep it
                    # visible rather than degrading to a failed state.
                    case.status = "decided"
                else:
                    case.status = "failed"
                case.updated_at = now

                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception("commit failed for case verdict (case=%s)", case_id)
            except Exception:  # noqa: BLE001 — belt & suspenders; never escape
                db.session.rollback()
                log.exception("judge_case failed (case=%s)", case_id)
    finally:
        with _judging_lock:
            _judging.discard(case_id)


def _spawn_judge_if_idle(app_obj, case_id):
    """판결 백그라운드 스레드를 스폰(이미 진행 중이면 no-op). 절대 raise 안 한다.

    다른 생성기들의 ``_spawn_*_if_idle`` 과 같은 패턴 — 빠른 더블탭이 같은 사건에
    스레드 두 개를 띄우지 못하게 ``_judging`` 가드로 떨군다.
    """
    spawn = False
    with _judging_lock:
        if case_id not in _judging:
            _judging.add(case_id)
            spawn = True
    if not spawn:
        return
    if not _enqueue_ai(app_obj, "judge", f"judge:{case_id}", {"case_id": case_id}):
        with _judging_lock:
            _judging.discard(case_id)


def case_judging_verdict(case):
    """'judging' 사건의 상태 — 'running'/'orphaned'/'overdue' 또는 None.

    후기 초안과 **같은 구멍**: 재시작하면 사건 상세가 '판결 중…'에 영구히 박히고,
    그 화면의 '판결 맡기기' 버튼은 judging 동안 **disabled** 라 탈출구조차 없다.
    상한은 ``_STUCK_GENERATING``(5분) — 판결은 claude 콜 한 번이다.
    """
    if case is None or case.status != "judging":
        return None
    return _bg_job_verdict(
        _judging_lock, _judging, case.id, case.updated_at, _STUCK_GENERATING,
        job_key=f"judge:{case.id}",
    )


def resume_judging_if_orphaned(app_obj, case):
    """고아가 된 'judging' 사건의 판결을 되살린다(되살렸으면 True).

    ⚠️ ``verdict_json`` 을 건드리지 않는다 — 직전 판결이 있으면 그대로 남는다.
    """
    if case_judging_verdict(case) != "orphaned":
        return False
    case.updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("판결 재개 커밋 실패 (case=%s)", case.id)
        return False
    log.warning("판결 스레드가 사라져 재개한다 (case=%s)", case.id)
    _spawn_judge_if_idle(app_obj, case.id)
    return True


# --------------------------------------------------------------------------- #
# 데이트 뉴스 커플 맞춤 추천 점수 (P2) — 취향 프로필(비-AI) + 배치 채점(백그라운드)
# --------------------------------------------------------------------------- #
# 채점은 커플 데이터에서 만든 '취향 프로필'을 근거로 한다. 프로필 조립은 순전히
# 이 커플 자신의 데이터를 뽑아 이어붙이는 값싼 비-AI 작업이고(여기 claude 없음),
# 느린 claude 배치 채점은 아래 백그라운드 워커에서만 돈다.
_PROFILE_MAXLEN = 1400  # 프로필 총 길이 상한(프롬프트 폭주 방지)


def build_couple_taste_profile(couple) -> str:
    """이 커플 자신의 데이터로 짧은 한국어 취향 프로필 텍스트를 만든다(비-AI).

    claude를 부르지 않는다 — 최근 오늘의질문 답변·추억 캡션/태그·(옵션)지난 판사
    사건 상황을 사실 그대로 이어붙여 요약한다. 총 길이를 몇백 자로 제한한다.
    데이터가 사실상 없으면 ""를 반환한다(그럼 채점은 중립 프로필로도 동작한다).
    호출부(백그라운드 워커)가 app_context 안에서 부른다.
    """
    if couple is None:
        return ""
    parts = []

    try:
        # 1) 최근 오늘의질문 답변(양쪽) — 표현된 관심사·기분.
        recent_qs = (
            DailyQuestion.query.filter_by(couple_id=couple.id)
            .order_by(DailyQuestion.q_date.desc())
            .limit(10)
            .all()
        )
        ans_lines = []
        for q in recent_qs:
            for a in q.answers.all():
                t = (a.text or "").strip()
                if t:
                    ans_lines.append(t[:120])
        if ans_lines:
            parts.append("[최근 오늘의질문 답변]\n" + "\n".join(f"- {t}" for t in ans_lines[:16]))
    except Exception:  # noqa: BLE001 — 프로필은 best-effort, 절대 터지지 않게
        db.session.rollback()
        log.debug("taste profile: 답변 수집 실패", exc_info=True)

    try:
        # 2) 최근 추억 사진 캡션 + 태그(ready) — 뭘 찍고 좋아하는지.
        photos = (
            Photo.query.filter_by(couple_id=couple.id, caption_status="ready")
            .order_by(Photo.created_at.desc(), Photo.id.desc())
            .limit(20)
            .all()
        )
        cap_lines = []
        tag_bag = []
        for p in photos:
            c = (p.caption or "").strip()
            if c:
                cap_lines.append(c[:120])
            for tg in p.tags_list:
                tg = str(tg).strip()
                if tg and tg not in tag_bag:
                    tag_bag.append(tg)
        if cap_lines:
            parts.append("[추억 사진 속 모습]\n" + "\n".join(f"- {c}" for c in cap_lines[:12]))
        if tag_bag:
            parts.append("[자주 담는 것들] " + ", ".join(tag_bag[:20]))
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.debug("taste profile: 사진 수집 실패", exc_info=True)

    try:
        # 3) (옵션) 지난 판사 사건 상황 — 민감할 수 있는 주제(피하기용 참고).
        cases = (
            Case.query.filter_by(couple_id=couple.id)
            .order_by(Case.created_at.desc())
            .limit(5)
            .all()
        )
        sit_lines = []
        for c in cases:
            s = (c.title or c.situation or "").strip()
            if s:
                sit_lines.append(s[:80])
        if sit_lines:
            parts.append(
                "[가끔 부딪히는 지점(자극 피하기 참고)]\n"
                + "\n".join(f"- {s}" for s in sit_lines[:5])
            )
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.debug("taste profile: 사건 수집 실패", exc_info=True)

    profile = "\n\n".join(parts).strip()
    if len(profile) > _PROFILE_MAXLEN:
        profile = profile[:_PROFILE_MAXLEN].rstrip()
    return profile


# 백그라운드 채점 가드 — 같은 커플에 두 패스가 겹치지 않게 하는 in-process 집합
# (캡션의 _captioning과 같은 패턴). claude 호출은 아래에서 캡션과 '같은'
# _CAPTION_SEM으로 직렬화해 512MB 티어에서 동시에 claude가 둘 이상 뜨지 않게 한다.
_scoring_lock = threading.Lock()
_scoring: set[int] = set()
_SCORE_BATCH_DEFAULT = 24  # 한 패스당 채점 상한(배치 1콜 크기)


def score_events_for_couple(app, couple_id, limit=_SCORE_BATCH_DEFAULT):
    """백그라운드 워커: 이 커플에 아직 점수 없는 진행 중 행사들을 배치로 채점한다.

    caption_photo와 같은 규율: 자기 app_context(→ 새 스레드로컬 세션)를 열고,
    캡션과 공유하는 _CAPTION_SEM으로 claude 배치 콜을 직렬화하고, 모든 예외를
    삼키며 스레드 밖으로 절대 raise 안 하고, finally에서 가드 키를 해제한다.

    선택: 진행 중(end_date NULL 또는 >= 오늘) 행사 중 이 커플에 'ready' 점수가
    없는(행 없거나 status!='ready') 것들을 마감 임박 순으로 최대 ``limit``개.
    없으면 그냥 반환. 선택된 것들에 'pending' 행을 만들어(UI가 "분석 중" 표시)
    두고, 프로필을 한 번 만들고, 배치 페이로드를 꾸려 ai.score_events를 '한 번'
    호출한다. 돌아온 ref → score/reason/status='ready'; 안 돌아온 ref →
    status='failed'(이 패스에서 무한 재시도 방지; 이후 패스가 재시도 가능).

    반환값: 이 패스에서 새로 'ready'가 된 행사 수(int). 채점할 게 없거나
    이미 이 커플 채점 중이거나 에러면 0. 선채점 루프(prewarm_scores)가 이 값이
    0이 될 때까지(또는 하드 캡까지) 반복 호출한다.
    """
    with _scoring_lock:
        if couple_id in _scoring:
            return 0  # 이미 이 커플 채점 중 — 겹치지 않게
        _scoring.add(couple_id)

    try:
        with app.app_context():
            try:
                couple = db.session.get(Couple, couple_id)
                if couple is None:
                    return 0
                today = date.today()

                # 진행 중 행사 중 이 커플에 'ready' 점수가 없는 것 — 마감 임박 순.
                # LEFT JOIN으로 EventScore가 없거나 ready가 아닌 행만 고른다.
                rows = (
                    db.session.query(EventItem, EventScore)
                    .outerjoin(
                        EventScore,
                        db.and_(
                            EventScore.event_id == EventItem.id,
                            EventScore.couple_id == couple_id,
                        ),
                    )
                    .filter(
                        db.or_(
                            EventItem.end_date.is_(None),
                            EventItem.end_date >= today,
                        ),
                        db.or_(
                            EventScore.id.is_(None),
                            EventScore.status != "ready",
                        ),
                    )
                    .order_by(
                        (EventItem.end_date.is_(None)),
                        EventItem.end_date.asc(),
                        EventItem.start_date.asc(),
                    )
                    .limit(int(limit) if limit else _SCORE_BATCH_DEFAULT)
                    .all()
                )
                if not rows:
                    return 0

                # 선택된 행에 EventScore 행을 'pending'으로 보장(UI "분석 중").
                selected = []  # (event, score_row)
                for ev, sc_row in rows:
                    if sc_row is None:
                        sc_row = EventScore(
                            couple_id=couple_id, event_id=ev.id, status="pending"
                        )
                        db.session.add(sc_row)
                    else:
                        sc_row.status = "pending"
                    selected.append((ev, sc_row))
                try:
                    db.session.commit()
                except IntegrityError:
                    # 동시 패스가 행을 먼저 만들었을 수 있음 — 롤백하고 이번 패스는
                    # 양보(다음 on-view 트리거가 재시도).
                    db.session.rollback()
                    return 0

                # 프로필 1회 + 배치 페이로드.
                profile_text = build_couple_taste_profile(couple)
                batch = [
                    {
                        "ref": ev.id,
                        "title": ev.title,
                        "category": ev.category,
                        "place": ev.place,
                        "description": ev.description,
                    }
                    for ev, _ in selected
                ]

                # 캡션과 '같은' 세마포어로 claude 배치 콜을 직렬화(512MB: 동시에
                # claude 하나만). claude가 터져도 실패로만 취급.
                _CAPTION_SEM.acquire()
                try:
                    try:
                        results = ai.score_events(profile_text, batch)
                    except Exception:  # noqa: BLE001 — claude가 스레드 죽이지 못하게
                        log.exception(
                            "claude event scoring raised (couple=%s)", couple_id
                        )
                        results = []
                finally:
                    _CAPTION_SEM.release()

                by_ref = {}
                for r in results or []:
                    ref = r.get("ref")
                    if ref is not None:
                        by_ref[ref] = r

                # 결과 반영: 받은 ref → ready, 안 받은 ref → failed.
                # scored = 이 패스에서 새로 ready가 된 수(선채점 루프 종료 신호).
                now = datetime.utcnow()
                scored = 0
                for ev, sc_row in selected:
                    r = by_ref.get(ev.id)
                    if r is not None:
                        sc_row.score = r.get("score")
                        sc_row.reason = (r.get("reason") or "")[:1000]
                        sc_row.status = "ready"
                        scored += 1
                    else:
                        sc_row.status = "failed"
                    sc_row.updated_at = now
                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception(
                        "commit failed for event scores (couple=%s)", couple_id
                    )
                    return 0  # 반영 실패 — 진전 없음(루프 종료)
                return scored
            except Exception:  # noqa: BLE001 — belt & suspenders; 절대 탈출 금지
                db.session.rollback()
                log.exception("score_events_for_couple failed (couple=%s)", couple_id)
                return 0
    finally:
        with _scoring_lock:
            _scoring.discard(couple_id)


def _spawn_scoring_if_idle(app, couple_id):
    """이 커플 채점 백그라운드 스레드를 스폰(이미 큐/진행 중이면 no-op).

    dates() 뷰가 반복 조회돼도 스레드가 쌓이지 않도록 score_events_for_couple과
    같은 _scoring 가드로 중복을 떨군다. best-effort — 절대 raise 안 함.
    """
    with _scoring_lock:
        if couple_id in _scoring:
            return
    _enqueue_ai(app, "score", f"score:{couple_id}", {"couple_id": couple_id},
                couple_id=couple_id)


# --------------------------------------------------------------------------- #
# 데이트 뉴스 '추천받기' (P4) — 온디맨드 비동기 AI 맞춤 데이트 추천(팝업 포함)
# --------------------------------------------------------------------------- #
# 채점 워커와 같은 규율: 자기 app_context(→ 새 스레드로컬 세션)를 열고, 캡션·채점과
# 공유하는 _CAPTION_SEM으로 claude 콜을 직렬화하고(512MB: 동시에 claude 하나만),
# 모든 예외를 삼키며 스레드 밖으로 절대 raise 안 하고, finally에서 가드를 해제한다.
# 커플당 in-process 가드 집합으로 중복 실행을 막는다.
_recommending_lock = threading.Lock()
_recommending: set[int] = set()
_RECO_CANDIDATES = 20  # claude에 넘길 현재 피드 후보 상한


def recommend_dates_for_couple(app, couple_id):
    """백그라운드 워커: 이 커플의 현재 피드에서 맞춤 데이트 하나를 추천한다.

    DateRecommendation 행을 'pending'으로 보장 → 취향 프로필 1회 조립 → 현재
    진행 중(만료 안 된) 행사 최대 20개를 후보로(EventScore 높은 순 우선, 팝업 포함)
    모아 _CAPTION_SEM 안에서 ai.recommend_dates를 '한 번' 호출한다. 성공하면
    message + picks_json + status='ready', 실패하면 status='failed'(단, 직전
    ready 추천이 있으면 그걸 그대로 유지). 커밋은 rollback 가드. 절대 raise 안 하고
    finally에서 가드 키를 해제한다.
    """
    with _recommending_lock:
        if couple_id in _recommending:
            return  # 이미 이 커플 추천 생성 중 — 겹치지 않게
        _recommending.add(couple_id)

    try:
        with app.app_context():
            try:
                couple = db.session.get(Couple, couple_id)
                if couple is None:
                    return

                # 최신 추천 행을 'pending'으로 보장. 직전 ready 여부/내용을 먼저
                # 보관해 두어(실패 시 유지용) 덮어쓰기 전에 캡처한다.
                rec = DateRecommendation.query.filter_by(
                    couple_id=couple_id
                ).first()
                prior_ready = (
                    rec is not None and rec.status == "ready" and bool(rec.message)
                )
                prior_message = rec.message if rec is not None else None
                prior_picks = rec.picks_json if rec is not None else None
                if rec is None:
                    rec = DateRecommendation(couple_id=couple_id, status="pending")
                    db.session.add(rec)
                else:
                    rec.status = "pending"
                    rec.updated_at = datetime.utcnow()
                try:
                    db.session.commit()
                except IntegrityError:
                    # 동시 요청이 행을 먼저 만들었을 수 있음 — 재조회.
                    db.session.rollback()
                    rec = DateRecommendation.query.filter_by(
                        couple_id=couple_id
                    ).first()
                    if rec is None:
                        return

                # 취향 프로필(비-AI) 1회.
                profile_text = build_couple_taste_profile(couple)

                # 현재(만료 안 된) 행사 + 이 커플 점수 LEFT JOIN → EventScore가
                # ready·높은 점수 먼저 오도록 정렬해 상위 후보를 고른다(팝업 포함).
                today = date.today()
                rows = (
                    db.session.query(EventItem, EventScore)
                    .outerjoin(
                        EventScore,
                        db.and_(
                            EventScore.event_id == EventItem.id,
                            EventScore.couple_id == couple_id,
                        ),
                    )
                    .filter(
                        db.or_(
                            EventItem.end_date.is_(None),
                            EventItem.end_date >= today,
                        )
                    )
                    .all()
                )

                def _cand_key(row):
                    _ev, sc = row
                    # ready 점수가 높은 것 우선(-score), 나머지는 뒤로(-(-1)=1).
                    s = (
                        sc.score
                        if (sc is not None and sc.status == "ready" and sc.score is not None)
                        else -1
                    )
                    return -s
                rows.sort(key=_cand_key)

                candidates = []
                for ev, sc in rows[:_RECO_CANDIDATES]:
                    candidates.append(
                        {
                            "ref": ev.id,
                            "title": ev.title,
                            "category": ev.category,
                            "place": ev.place,
                            "description": ev.description,
                            "score": sc.score if sc is not None else None,
                        }
                    )

                if not candidates:
                    # 추천할 후보가 없음 — 직전 ready가 있으면 유지, 없으면 실패.
                    rec = DateRecommendation.query.filter_by(
                        couple_id=couple_id
                    ).first()
                    if rec is not None:
                        rec.status = "ready" if prior_ready else "failed"
                        rec.updated_at = datetime.utcnow()
                        try:
                            db.session.commit()
                        except Exception:  # noqa: BLE001
                            db.session.rollback()
                    return

                # 캡션·채점과 '같은' 세마포어로 claude 콜을 직렬화(512MB: 동시에
                # claude 하나만). claude가 터져도 실패로만 취급.
                _CAPTION_SEM.acquire()
                try:
                    try:
                        result = ai.recommend_dates(profile_text, candidates)
                    except Exception:  # noqa: BLE001 — claude가 스레드 죽이지 못하게
                        log.exception(
                            "claude date recommend raised (couple=%s)", couple_id
                        )
                        result = None
                finally:
                    _CAPTION_SEM.release()

                # 결과 반영(행이 바뀌었을 수 있어 재조회).
                rec = DateRecommendation.query.filter_by(
                    couple_id=couple_id
                ).first()
                if rec is None:
                    return
                now = datetime.utcnow()
                if result and result.get("message"):
                    rec.message = result["message"]
                    rec.picks_json = json.dumps(
                        result.get("picks") or [], ensure_ascii=False
                    )
                    rec.status = "ready"
                elif prior_ready:
                    # 실패했지만 직전 ready 추천이 있으니 그걸 그대로 유지.
                    rec.message = prior_message
                    rec.picks_json = prior_picks
                    rec.status = "ready"
                else:
                    rec.status = "failed"
                rec.updated_at = now
                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception(
                        "commit failed for date recommendation (couple=%s)", couple_id
                    )
            except Exception:  # noqa: BLE001 — belt & suspenders; 절대 탈출 금지
                db.session.rollback()
                log.exception(
                    "recommend_dates_for_couple failed (couple=%s)", couple_id
                )
    finally:
        with _recommending_lock:
            _recommending.discard(couple_id)


def _spawn_recommend_if_idle(app, couple_id):
    """이 커플 추천 생성 백그라운드 스레드를 스폰(이미 진행 중이면 no-op).

    _spawn_scoring_if_idle과 같은 패턴 — _recommending 가드로 중복을 떨군다.
    best-effort, 절대 raise 안 함."""
    with _recommending_lock:
        if couple_id in _recommending:
            return
    _enqueue_ai(app, "recommend", f"recommend:{couple_id}", {"couple_id": couple_id},
                couple_id=couple_id)


def resume_recommendation_if_orphaned(app_obj, couple_id):
    """고아가 된 'pending' 데이트 추천을 되살린다(되살렸으면 True).

    후기 초안과 **같은 구멍** — 재시작으로 추천 스레드가 죽으면 FAB 패널이
    '뽑는 중'으로 영원히 폴링한다(사용자가 다시 누르기 전엔 아무 일도 안 일어난다).
    상한은 월간 회고와 같은 ``_STUCK_GENERATING``(5분): 추천은 claude 콜 한 번이다.

    ⚠️ 직전 ready 추천 내용(message·picks_json)은 건드리지 않는다.
    """
    rec = DateRecommendation.query.filter_by(couple_id=couple_id).first()
    if rec is None or rec.status != "pending":
        return False
    verdict = _bg_job_verdict(
        _recommending_lock, _recommending, couple_id,
        rec.updated_at, _STUCK_GENERATING,
    )
    if verdict != "orphaned":
        return False
    rec.updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("데이트 추천 재개 커밋 실패 (couple=%s)", couple_id)
        return False
    log.warning("데이트 추천 스레드가 사라져 재개한다 (couple=%s)", couple_id)
    _spawn_recommend_if_idle(app_obj, couple_id)
    return True


# ---------------------------------------------------------------------------
# 데이트 후기 → 네이버 블로그 초안 백그라운드 생성 (P2)
# 캡션·채점·추천과 공유하는 _CAPTION_SEM으로 claude를 직렬화하고(512MB: 동시에
# claude 하나만), review_id 단위 in-process 가드로 중복 실행을 막는다. 절대 스레드
# 밖으로 raise하지 않고 finally에서 가드를 해제한다.
# ---------------------------------------------------------------------------
_generating_reviews_lock = threading.Lock()
_generating_reviews: set[int] = set()

# 후기 초안이 '오래 걸린다'고 **말해 줄** 기준 — ⛔ 끊는 상한이 아니다.
#
# 이 25분이 지나도 **아무것도 죽이지 않는다.** 생성은 계속 돌고, 화면이 한 줄 더
# 말할 뿐이다("오래 걸리고 있어 — 계속 기다릴래, 아니면 ✋ 그만할래?"). 실제로
# 끊는 것은 둘뿐이다: **사람의 중단 버튼**과 **침묵 감지**(ai.SESSION_IDLE_SEC).
#
# 근거(추정): 후기 생성은 claude 를 여러 번 부르고 전부 _CAPTION_SEM 뒤에 줄을 선다 —
#     키워드 후보 + 선정   ≈ 2 × ai.CLAUDE_TIMEOUT(120s) = 240s
#     본문 생성(write_review) ≈ 1콜 (로컬 실측 73.7s, Render 0.1 CPU 는 더 느리다)
#     사진 블록마다 비전 크롭  ≈ N × ai.CAPTION_EST_SEC(180s)
# 사진 5장이면 20분대까지도 '느리지만 정상'이다. 월간 회고의 _STUCK_GENERATING(5분)을
# 그대로 쓰면 정상 생성을 멈춘 것으로 오인한다. (재시작으로 스레드가 죽은 경우는 이
# 기준을 **기다리지 않는다** — 인프로세스 가드가 즉시 'orphaned' 로 판정한다.)
_STUCK_REVIEW = timedelta(minutes=25)


def review_pending_verdict(review):
    """'pending' 후기 초안의 생성 상태 — 'running'/'orphaned'/'overdue' 또는 None.

    pending 이 아니면 None. 판정 규율은 ``_bg_job_verdict`` 가 단일 원천이다.
    """
    if review is None or review.status != "pending":
        return None
    return _bg_job_verdict(
        _generating_reviews_lock, _generating_reviews, review.id,
        review.updated_at, _STUCK_REVIEW, job_key=f"review:{review.id}",
    )


def resume_review_if_orphaned(app_obj, review):
    """고아가 된 'pending' 후기 초안 생성을 되살린다(되살렸으면 True).

    프로세스가 재시작돼 데몬 스레드가 같이 죽은 행을 다시 스폰한다. ``/memories``
    가 멈춘 캡션을, ``/dates`` 가 미채점 행사를 되살리는 것과 같은 셀프힐이다.

    ⚠️ ``ai_json``·``edited_text``·``research_json`` 을 **절대 건드리지 않는다** —
    되돌리는 것은 '생성이 돌고 있다'는 표시(updated_at)뿐이다. 사람이 요청한
    ``/regenerate`` 와 달리 이건 자동 복구라, 직전 좋은 초안과 사람이 편집한
    복사본은 그대로 남는다(재생성이 빈손이어도 직전 산출물을 안 지우는 규율).
    """
    if review_pending_verdict(review) != "orphaned":
        return False
    review.updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001 — 복구가 요청을 깨뜨리면 안 된다
        db.session.rollback()
        log.exception("후기 초안 재개 커밋 실패 (review=%s)", review.id)
        return False
    log.warning(
        "후기 초안 생성 스레드가 사라져 재개한다 (review=%s) — 프로세스 재시작 추정",
        review.id,
    )
    _spawn_generate_review(app_obj, review.id)
    return True


# 'failed' 로 끝난 초안을 **이 프로세스에서 한 번** 자동 재시도했는지 기록한다.
# 프로세스가 재시작되면 비워진다 — 그게 맞다(재시작 자체가 '환경이 달라졌다'는
# 신호다). 무한 루프는 이 집합이 막는다.
_refailed_reviews_lock = threading.Lock()
_refailed_reviews: set[int] = set()


def resume_review_if_failed(app_obj, review):
    """'failed' 로 끝난 초안을 **한 번** 자동으로 다시 돌린다(돌렸으면 True).

    왜 이게 필요한가 — 2026-10-07 사고에서 후기 **두 건이 같은 원인(메모리 초과)**
    으로 멈췄는데 **결과가 서로 달랐다.** 리눅스 OOM 킬러는 cgroup 에서 가장 큰
    프로세스를 고르는데, 이 앱에서 그건 보통 ``claude``(Node, 실측 ~400MB)지
    gunicorn 워커(~95MB)가 아니다. 그래서:

      * 워커째 죽은 쪽 → 행은 ``pending`` 으로 남고, 인프로세스 가드가 비어 있어
        ``resume_review_if_orphaned`` 가 **자동 복구**했다(사용자가 본 '복구 메시지').
      * ``claude`` 자식만 죽은 쪽 → 워커는 살아서 ``_run_claude`` 가 "exited -9" 로
        RuntimeError 를 던졌고, ``write_review`` 가 None 을 돌려줘 ``failed`` 로
        **종결**됐다. 자동 복구 경로가 없어 사람이 ↻ 를 누를 때까지 그대로 남는다.

    원인이 하나인데 복구가 한쪽만 되는 **비대칭**이 문제다. 사용자 입장에서 둘은
    구분할 수 없는 같은 사고다. 그래서 'failed' 도 고아 복구와 **같은 자리에서 같은
    모양으로** 한 번 되살린다. 'failed' 가 사라지는 게 아니라, 한 번 더 해 보고도
    실패하면 그때 ``failed`` 로 남아 기존 탈출구(↻ 다시 생성 버튼)를 보여 준다.

    ⚠️ ``ai_json``·``edited_text``·``research_json`` 을 **건드리지 않는다**
    (``resume_review_if_orphaned`` 와 같은 규율). 직전 초안이 있는 행은 애초에
    ``failed`` 가 되지 않으므로(위 generate_review 의 prior_ok) 잃을 산출물도 없다.
    """
    if review is None or review.status != "failed":
        return False
    with _refailed_reviews_lock:
        if review.id in _refailed_reviews:
            return False
        _refailed_reviews.add(review.id)
    review.status = "pending"
    review.updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001 — 복구가 요청을 깨뜨리면 안 된다
        db.session.rollback()
        log.exception("실패한 후기 초안 자동 재시도 커밋 실패 (review=%s)", review.id)
        return False
    log.warning(
        "실패로 끝난 후기 초안을 자동으로 한 번 더 돌린다 (review=%s) — "
        "메모리 압박에 claude 자식이 죽은 경우가 이렇게 보인다", review.id,
    )
    _spawn_generate_review(app_obj, review.id)
    return True


# --------------------------------------------------------------------------- #
# '작성중' 한 줄을 사람이 구분할 수 있는 **다섯 상태**로 펼친다
# --------------------------------------------------------------------------- #
# 2026-10-07 사고의 남은 절반: 큐 행(``ai_jobs``)은 필요한 걸 이미 다 갖고 있었는데
# (status·owner·attempts·created_at·started_at) **그걸 화면에 꺼내는 길이 없었다.**
# 사용자가 본 것은 "AI가 블로그 초안을 쓰고 있어요…" 하나뿐이었고, 그래서
# "오래 걸리는 건지 죽은 건지" 알 수 없었다.
#
# 여기서 구분해 말하는 다섯:
#   1. queued   — 아직 내 차례가 아니다 (앞에 몇 개)
#   2. running  — 지금 돌고 있다 (몇 분째 · 몇/몇 단계 · 무슨 단계)
#   3. running(오래) — 오래 걸리지만 정상 범위다 (예상 범위를 같이 말한다)
#   4. overdue / attempts — 비정상이다 (탈출구 ↻)
#   5. stalled  — 줄에는 섰는데 **아무도 집어가지 않는다** (펌프가 안 돈다)
#
# ⛔ 가짜 진행률을 만들지 않는다. 숫자는 전부 **일하는 쪽이 적은 것**(step/step_index)
#    이거나 **DB 가 아는 사실**(ahead·attempts·started_at)이고, 추정은 추정이라고 쓴다.

# 큐에 서 있는데 아무도 'running' 이 아닌 채 이만큼 지나면 '집어갈 사람이 없다'로 본다.
# 펌프는 빈 큐에서도 ``aijobs.POLL_SEC``(2초)마다 한 바퀴 돈다 — 90초면 넉넉하다.
_QUEUE_STALL = timedelta(seconds=90)


def _review_eta(n_photos):
    """이 후기 한 건의 (claude 콜 수, '느려도 정상'인 최대 분).

    근거는 콜 하나의 **추정 소요**뿐이다 — 조사 2 + 본문 1 = 3 × 120초,
    사진은 ``ai.CROP_BATCH_SIZE`` 장씩 **묶어** 비전 콜 하나 × ``ai.CAPTION_EST_SEC``.
    (예전엔 사진 한 장당 한 콜이었다 — 11장이면 11콜. 실측 147초 → 묶으니 3콜 31초.)
    **추정이지 약속이 아니고, 이 시간이 지나도 아무것도 끊지 않는다**(끊는 건 사람의
    중단 버튼과 침묵 감지뿐).
    """
    n = max(0, int(n_photos))
    size = max(1, int(getattr(ai, "CROP_BATCH_SIZE", 4)))
    crop_calls = -(-n // size)      # 올림 나눗셈
    calls = 3 + crop_calls
    worst_sec = 3 * ai.CLAUDE_TIMEOUT + crop_calls * ai.CAPTION_EST_SEC
    return calls, max(1, int(round(worst_sec / 60.0)))


def _pump_looks_stalled(snap, pump, now):
    """'줄에 있는데 아무도 집어가지 않는다'인가 — 두 신호를 **같이** 본다.

    * 프로세스 안쪽 사실: 펌프 스레드가 죽었거나, 아무 잡도 안 쥔 채 오래 조용하다.
      (잡을 쥐고 있으면 조용한 게 정상이다 — 한 잡이 21분까지 간다.)
    * DB 쪽 사실: 오래 queued 인데 **running 인 행이 하나도 없다**. 펌프가 이
      프로세스 밖에 있는 배치에서도 성립하는 신호다.
    """
    if not pump["alive"]:
        return True
    silent = pump["silent_sec"]
    if (pump["current_key"] is None and silent is not None
            and silent > aijobs.PUMP_SILENT_SEC):
        return True
    waited = now - (snap["created_at"] or now)
    return waited >= _QUEUE_STALL and not aijobs.anyone_running()


def _claude_liveness_hint():
    """"지금 claude 가 살아 있나"를 사람 문장으로 (할 말 없으면 "").

    근거는 ``ai.last_activity_age()`` — claude 가 **실제로 이벤트를 준** 뒤 흐른
    초다. 한 단계가 길어서 라벨이 안 바뀌는 것과, 정말 죽어서 안 바뀌는 것을
    구분해 주는 유일한 신호다(시간으로 혼자 올라가는 가짜 진행률이 아니다).
    진척 쓰기가 **최근에** 실패했으면 그것도 같이 말한다 — 조용히 삼키면 사람은
    "일은 도는데 숫자만 안 바뀐다"를 영원히 모른다.

    기존 안내를 **덮지 않고 덧붙인다** — 예상 범위·'안 끊는다' 같은 말은 그대로
    남아야 한다.
    """
    bits = []
    try:
        age = ai.last_activity_age()
    except Exception:  # noqa: BLE001 — 진단이 페이지를 깨뜨리지 않게
        age = None
    if age is not None and age < _CLAUDE_LIVE_SEC:
        bits.append(
            "claude 는 방금까지 답하고 있었어 — 한 단계가 길어서 숫자가 안 바뀌는 "
            "것뿐이야." if age < 20
            else f"claude 가 마지막으로 말한 게 {int(age)}초 전이야 — 아직 도는 중이야."
        )
    try:
        fail_age = aijobs.progress_failure_age()
    except Exception:  # noqa: BLE001
        fail_age = None
    if fail_age is not None and fail_age < _PROGRESS_FAIL_FRESH_SEC:
        bits.append(
            f"⚠️ 단계 표시를 {aijobs.progress_failures}번 못 적었어 — 일은 돌고 "
            "있는데 숫자만 안 바뀌는 걸 수 있어."
        )
    return " ".join(bits)


# claude 가 이 시간 안에 이벤트를 줬으면 '살아 있다'고 말한다. 길게 잡을 이유가
# 없다 — 오래된 신호는 '지금 살아 있다'의 근거가 못 된다.
_CLAUDE_LIVE_SEC = 180
# 진척 쓰기 실패를 '최근 일'로 칠 시간. 지난 실패가 영원히 경고로 남으면 그건
# 노이즈다(한 번 삐끗하고 회복되는 게 보통이다).
_PROGRESS_FAIL_FRESH_SEC = 600


def review_progress_view(app_obj, review, resumed=False):
    """후기 초안 생성의 지금 상태를 **사람의 문장**으로 만든다 (단일 원천).

    템플릿(첫 렌더)과 폴링 JSON 이 **같은 이 함수**를 쓴다 — 두 곳에서 따로 쓰면
    말이 갈라진다. 절대 raise 하지 않는다(상태 표시가 페이지를 깨뜨리면 본말전도).
    """
    thumb = review.thumbnail or {}
    thumb_pending = (thumb.get("status") == "pending")
    out = {
        "status": review.status,
        "done": False,          # 아래에서 확정한다
        "state": review.status,
        "headline": "",
        "hint": "",
        "retry": False,
        # 사람이 멈출 수 있는 상태인가 — '✋ 그만할래' 버튼의 노출 조건.
        # 시간 상한을 걷어낸 자리에 들어온 **유일한 명시적 탈출구**라, 오래 걸리는
        # 동안 **항상** 보여야 한다(오래 걸릴 때만 뜨면 그땐 이미 늦다).
        "can_stop": False,
        "ahead": None,
        "elapsed_min": None,
        "step": None,
        "step_index": None,
        "step_total": None,
        "attempts": None,
    }
    snap = aijobs.snapshot(f"review:{review.id}")
    if snap:
        out["attempts"] = snap["attempts"]
        out["step"] = snap["step"]
        out["step_index"] = snap["step_index"]
        out["step_total"] = snap["step_total"]
    # done = 더 기다릴 게 없다 → 화면을 한 번 새로 그리면 끝.
    # ⚠️ 'ready' 라고 끝난 게 아니다 — 파이프라인은 **본문을 크롭 전에 먼저 커밋**
    #    한다(거기서 죽어도 쓸 수 있는 초안이 남게). 그래서 초안이 떠도 사진 크롭은
    #    아직 돌고 있을 수 있고, 그게 가장 긴 구간이다. 큐 행이 사라져야 진짜 끝이다.
    out["done"] = (
        review.status != "pending" and not thumb_pending and snap is None
    )

    if review.status != "pending":
        if snap is not None:
            # 초안은 떴고 사진 크롭만 남았다 — 그림이 한 번 더 바뀐다고 미리 말한다.
            # 여기도 아직 claude 가 돌고 있으니 멈출 수 있어야 한다.
            out["state"] = "finishing"
            out["can_stop"] = True
            label = snap["step"] or "마무리하는 중"
            out["headline"] = f"✨ {label} — 다 되면 사진이 한 번 더 바뀔 거야."
        elif review.status == "cancelled":
            out["state"] = "cancelled"
            out["headline"] = "✋ 여기서 멈췄어 — 다시 만들고 싶으면 눌러줘."
            out["retry"] = True
        return out

    now = datetime.utcnow()
    limit_min = int(_STUCK_REVIEW.total_seconds() // 60)
    try:
        n_photos = len(review.photos_ordered)
    except Exception:  # noqa: BLE001
        n_photos = 0
    calls, worst_min = _review_eta(n_photos)
    how_many = (
        f"사진 {n_photos}장이면 claude 를 {calls}번 불러"
        if n_photos else f"사진이 없으니 claude 를 {calls}번 불러"
    )
    base_hint = (
        f"{how_many} — 보통 3분쯤, 느릴 땐 {worst_min}분까지도 정상이야(추정이야). "
        "오래 걸려도 내가 끊지는 않아 — 그만하려면 ✋ 를 눌러줘."
    )
    out["hint"] = base_hint
    # 'pending' 인 동안은 언제든 멈출 수 있다 — 줄에 서 있든(큐 행만 치운다),
    # claude 가 돌고 있든(프로세스를 죽인다) 같은 버튼 하나다.
    out["can_stop"] = True

    def _mins(since):
        return max(0, int((now - (since or now)).total_seconds() // 60))

    if resumed:
        out["state"] = "resumed"
        out["headline"] = "↻ 생성이 중단된 것 같아 방금 다시 시작했어 — 조금만 기다려줘!"
        out["retry"] = True
    elif snap is None:
        # 큐에 행이 없다 — 막 세우는 중이거나(인프로세스 가드만 쥔 찰나), 되살리지
        # 못한 경우다. 진척을 적을 행이 없으니 **단계는 모른다고 말한다.**
        verdict = review_pending_verdict(review)
        m = _mins(review.updated_at)
        out["elapsed_min"] = m
        if verdict == "overdue":
            # ⚠️ '죽었다'고 단정하지 않는다 — 시간 상한을 걷어낸 뒤로 이건 **아직
            # 돌고 있는데 오래 걸리는 것**일 수 있다(그게 보통이다). 사실만 말하고
            # 선택지를 준다.
            out["state"] = "overdue"
            out["headline"] = (
                f"⏳ {m}분째야 — 예상({limit_min}분)보다 오래 걸려. 아직 돌고 "
                "있을 수도 있어서 내가 끊지는 않았어. 더 기다리거나 ✋ 로 멈춰도 돼."
            )
            out["retry"] = True
        elif verdict == "orphaned":
            # resume 이 실패했을 때만 여기 온다(보통은 위 resumed 가 잡는다).
            out["state"] = "orphaned"
            out["headline"] = "⏳ 만들던 게 사라진 것 같아 — 다시 시도해줄래?"
            out["retry"] = True
        elif m == 0:
            out["state"] = "starting"
            out["headline"] = "✨ AI가 블로그 초안을 쓰고 있어 — 방금 시작했어."
        else:
            out["state"] = "running"
            out["headline"] = (
                f"✨ AI가 블로그 초안을 쓰고 있어 — {m}분째. "
                "(어느 단계인지는 아직 안 적혔어)"
            )
    elif snap["status"] == "running":
        m = _mins(snap["started_at"] or review.updated_at)
        out["elapsed_min"] = m
        i, t, label = snap["step_index"], snap["step_total"], snap["step"]
        if i and t:
            where = f"{i}/{t}단계" + (f" ({label})" if label else "")
        elif label:
            where = label
        else:
            where = "아직 어느 단계인지 안 적혔어"
        when = "방금 시작했어" if m == 0 else f"{m}분째"
        if m >= limit_min:
            # ⚠️ 경고지 판결이 아니다 — 큐 행은 지금도 'running' 이고, 단계가
            # 바뀌고 있으면 그건 **살아 있다는 증거**다. 끊지 않고 알려만 준다.
            out["state"] = "overdue"
            out["headline"] = (
                f"⏳ {m}분째야 — 예상({limit_min}분)보다 오래 걸려 · {where}. "
                "끊지는 않았어. 더 기다리거나 ✋ 로 멈춰도 돼."
            )
            out["retry"] = True
        elif m >= 5:
            # 3번 상태: 오래 걸리지만 정상이다. 불안하지 않게 범위를 같이 말한다.
            out["state"] = "running_long"
            out["headline"] = (
                f"✨ 오래 걸리고 있지만 아직 정상 범위야 — {when} · {where}"
            )
        else:
            out["state"] = "running"
            out["headline"] = f"✨ 지금 쓰고 있어 — {when} · {where}"
        # ⭐ **한 단계가 긴 것**과 **죽은 것**을 구분해 준다.
        # 단계 라벨은 claude 콜 '사이'에만 바뀐다. 본문 한 콜이 수 분이면 그 동안
        # 라벨은 당연히 그대로인데, 화면만 보면 멈춘 것과 똑같이 보인다 — 실제로
        # '4/15 본문 쓰는 중' 에서 10분 멈춘 것처럼 보였다는 신고가 있었다.
        # 그 동안에도 우리는 claude 의 stream 이벤트를 받고 있다. 그 사실을 그대로
        # 말해 준다(시간으로 혼자 올라가는 가짜 진행률이 아니다 — 실제 이벤트다).
        live = _claude_liveness_hint()
        if live:
            out["hint"] = (out["hint"] + " " + live).strip()
    else:  # queued
        m = _mins(snap["created_at"])
        out["elapsed_min"] = m
        out["ahead"] = snap["ahead"]
        pump = aijobs.pump_status()
        if _pump_looks_stalled(snap, pump, now):
            out["state"] = "stalled"
            # 안 돌고 있을 때**만** 깨우고, 깨웠을 때**만** 깨웠다고 말한다 —
            # 이미 살아 있는데 "방금 깨웠어"라고 하면 그건 거짓말이다.
            woke = False
            if not pump["alive"]:
                try:
                    woke = aijobs.start_pump(app_obj) is not None
                except Exception:  # noqa: BLE001 — 깨우기가 페이지를 깨뜨리지 않게
                    log.exception("큐 펌프 재기동 실패")
            out["headline"] = (
                f"🚧 줄에는 서 있는데 {m}분째 아무도 집어가질 않아 — "
                "일꾼(작업 큐)이 멈춘 것 같아."
                + (" 방금 다시 깨웠어." if woke else "")
            )
            out["hint"] = (
                "조금 기다려도 안 바뀌면 ↻ 로 다시 넣어줘. "
                "그래도 그대로면 서버가 뜨지 않은 거야."
            )
            out["retry"] = True
        elif snap["ahead"]:
            out["state"] = "queued"
            out["headline"] = (
                f"⏳ 아직 내 차례가 아니야 — 앞에 {snap['ahead']}개가 먼저야."
            )
            out["hint"] = "앞 작업이 끝나면 바로 시작해. " + base_hint
        else:
            out["state"] = "queued"
            out["headline"] = "⏳ 줄 맨 앞이야 — 곧 시작해."

    # 재시도가 쌓였으면 그대로 말한다 — '정상인데 느린 것'과 섞이면 안 된다.
    if (out["attempts"] or 0) >= 2 and out["state"] not in ("overdue", "stalled"):
        out["hint"] = (
            f"이 작업은 벌써 {out['attempts']}번째 시도야 — 계속 실패하는 중일 수 "
            "있어. 안 끝나면 ↻ 로 다시 돌려줘. " + out["hint"]
        )
        out["retry"] = True
    return out


def _crop_hint_for_image(blocks, idx):
    """image 블록(blocks[idx]) 주변에서 크롭 힌트(그 사진이 말하는 대상)를 뽑는다.

    가장 가까운 앞쪽 heading + 가장 가까운 para(앞쪽 우선, 없으면 뒤쪽)를 합쳐준다.
    """
    heading = ""
    para = ""
    for j in range(idx - 1, -1, -1):
        b = blocks[j]
        if not isinstance(b, dict):
            continue
        if b.get("type") == "heading" and not heading:
            heading = (b.get("text") or "").strip()
        if b.get("type") == "para" and not para:
            para = (b.get("text") or "").strip()
        if heading and para:
            break
    if not para:
        for j in range(idx + 1, len(blocks)):
            b = blocks[j]
            if isinstance(b, dict) and b.get("type") == "para":
                para = (b.get("text") or "").strip()
                break
    return (heading + " — " + para).strip(" —").strip()


# claude 에게 그림을 보여 주는 길. 'file'(기본) = 렌디션을 임시파일로 떨궈 Read,
# 'url' = 만료 서명 URL 을 주고 WebFetch 로 가져가게 한다.
#
# ⚠️ 실측으로 기본값을 골랐다(2026-10-07, `claude -p --allowedTools WebFetch Read`):
#   * URL 경로는 **된다** — WebFetch 가 JPEG 를 자기 쪽에 내려받고 Read 가 픽셀을
#     본다. 320x240/1600x1200/4000x3000 전부 정확히 묘사했다.
#   * 다만 **사진 한 장에 30~33초**가 더 든다(WebFetch 왕복). 사진 5장이면 +2분 반.
#   * 10MB JPEG 한 건은 WebFetch 가 텍스트로 변환해 **픽셀을 못 봤다** — 큰 이미지엔
#     못 믿는다(작은 렌디션만 안전).
#   * 아낄 수 있는 메모리는 렌디션 한 장치(수백 KB)뿐이다 — 큰 건 이미 원본 디코드를
#     없애서 가져왔다.
# 즉 **속도를 크게 내주고 메모리를 조금 얻는** 교환이라 기본은 'file' 이다. URL 경로는
# 그대로 살아 있고(만료 5분 서명 URL — 네이버 발행이 쓰는 그 설비), 환경변수로 켠다.
_CROP_VISION_SOURCE = (os.environ.get("CROP_VISION_SOURCE") or "file").strip().lower()
# claude 콜 하나가 끝나기에 충분한 최소 만료(비전 콜 추정 180s + 여유).
_CROP_VISION_URL_TTL = 300


def _public_base_url():
    """이 앱의 **외부에서 접근 가능한** 베이스 URL(끝 슬래시 없음) 또는 None.

    백그라운드 스레드에는 요청 컨텍스트가 없어 ``url_for(_external=True)`` 를 쓸 수
    없다. Render 는 ``RENDER_EXTERNAL_URL`` 을 자동으로 넣어 준다.
    """
    base = (os.environ.get("PUBLIC_BASE_URL")
            or os.environ.get("RENDER_EXTERNAL_URL") or "").strip()
    return base.rstrip("/") or None


def _crop_vision_dims(photo):
    """크롭 계산에 쓸 **표시 기준** (w, h) — 픽셀을 한 바이트도 받지 않는다.

    1순위는 렌디션 메타다: Graph 렌디션은 이미 EXIF 로 똑바로 세워져 오므로 그
    가로세로가 곧 '표시 기준'이고, claude 가 볼 그림과도 **같은 방향**이다.
    (``image`` 패싯은 회전 전 값일 수 있어 2순위.) 둘 다 없으면 None.
    """
    item_id = photo.onedrive_item_id
    try:
        rm = onedrive.get_rendition_meta(
            item_id, onedrive.rendition_name(_RENDITION_VISION_EDGE)
        )
    except onedrive.OneDriveError:
        rm = None
    if rm:
        return (rm["width"], rm["height"])
    try:
        meta = onedrive.get_item_meta(item_id)
    except onedrive.OneDriveError:
        meta = None
    if meta and meta.get("width") and meta.get("height"):
        return (meta["width"], meta["height"])
    return None


def _crop_vision_image(photo):
    """claude 에게 보여 줄 ``(bytes, ext)`` — 렌디션(수백 KB) 우선, 없으면 원본."""
    item_id = photo.onedrive_item_id
    try:
        got = onedrive.get_rendition(item_id, _RENDITION_VISION_EDGE)
    except onedrive.OneDriveError:
        got = None
    if got:
        return got[0], ".jpg"
    try:
        data, _ctype = onedrive.get_photo_content(item_id)
    except onedrive.OneDriveError:
        log.exception("crop: OneDrive fetch 실패 (photo=%s)", photo.id)
        return None, ".jpg"
    name = photo.original_name or photo.filename or ""
    return data, os.path.splitext(name)[1].lower()


def _crop_vision_url(photo):
    """claude 가 WebFetch 로 가져갈 **만료 있는 서명 URL**(렌디션) 또는 None.

    새 메커니즘을 만들지 않고 네이버 발행이 쓰는 ``_blog_img_sig`` 를 그대로 쓴다 —
    같은 HMAC·같은 만료 규약이고, 가리키는 것은 원본이 아니라 **작은 렌디션**이다.
    만료는 claude 콜 하나가 끝날 만큼만(5분).
    """
    if _CROP_VISION_SOURCE != "url":
        return None
    base = _public_base_url()
    if not base:
        return None  # 외부에서 못 닿는 환경(로컬 개발 등) → 파일 경로로 간다
    exp = int(time.time()) + _CROP_VISION_URL_TTL
    variant = f"r{_RENDITION_VISION_EDGE}"
    sig = _blog_img_sig(photo.id, exp, None, variant)
    return f"{base}/blog-img/{photo.id}?e={exp}&t={sig}&v={variant}"


def _attach_section_crops(result, photos_ordered, acquire_sem=True, on_step=None):
    """result['blocks']의 각 image 블록에 정규화 크롭 [x,y,w,h]를 채운다(내용 인지).

    ⛔ **원본 바이트를 받지 않는다.** 크롭 박스는 0~1 정규화 좌표라 **해상도와
    무관**하고, 비율만 알면 ``compute_crop_rect`` 가 돈다. 그래서 사진당:

      * 크기  — Graph 렌디션이 알려 주는 가로세로(이미 EXIF 로 똑바로 서 있다).
        예전엔 3MB 원본을 받아 Pillow 로 열었다(실측 봉우리 ~90MB).
      * 비전  — 같은 렌디션(수백 KB)을 claude 에게 보여 준다. '어디를 남길지'를
        정하는 데 12MP 는 필요 없다 — 디스크·전송·토큰이 전부 싸진다. 실제 크롭은
        나중에 **원본 픽셀에서** 브라우저가 한다(화질 손실 0).

    렌디션을 못 받으면 그때만 원본으로 폴백한다(동작은 예전과 동일).
    suggest_crop이 None이어도 focus=None(중앙 크롭)으로 크롭을 저장해 서빙이 항상
    랜드스케이프가 되게 한다. 사진당 실패는 그 사진만 크롭 없이 넘어간다(전체 실패로
    번지지 않게). 저장 원본은 안 건드린다.

    ``acquire_sem`` — 비전 콜마다 ``_CAPTION_SEM`` 을 쥘 것인가. 후기 파이프라인은
    **이미 바깥에서 쥔 채** 부르므로 False 다(같은 스레드가 Semaphore(1) 을 두 번
    잡으면 영원히 멈춘다). 단독 호출(재크롭 등)에서는 True 가 맞다.

    ``on_step(done, total)`` — 선택. 1-based ``done``, 실제 image 블록 수 ``total``.
    **정확히 블록 수만큼** 불리고 순서대로 올라간다. 이 구간이 파이프라인에서 가장
    길다(**상한은 없다**) — 여기서 아무 소식이 없으면 사람은 죽은 줄 안다. 콜백이
    터져도 크롭은 그대로 간다.

    ⚡ **묶음(batch)으로 돈다.** 예전엔 사진 한 장당 claude 턴 하나였다(11장이면 11턴,
    파이프라인에서 압도적으로 긴 구간). 지금은 ``ai.CROP_BATCH_SIZE`` 장씩 묶어 한 턴에
    보여 주고 **사진별 좌표를 한꺼번에** 받는다. 묶는 크기를 작게 두는 이유는 주의
    희석이다 — 한 턴에 너무 많이 주면 뒤쪽 사진을 대충 본다.

    세 가지 위험을 구조로 막는다:

      1. **매핑 사고** — 사진마다 키(``p00``…)를 주고 그 키가 **파일 경로에 그대로**
         박힌다. 돌아온 키가 요청한 키가 아니면 버린다(``ai.parse_crops_batch``).
      2. **빠진 사진** — 묶음 응답에 없는 키는 그 장만 **단건으로 다시 묻고**, 그래도
         없으면 ``focus=None`` 중앙 크롭으로 떨어진다. 그 사진만 손해다.
      3. **전부 아니면 전무** — 묶음 하나가 통째로 터져도 그 묶음만 잃는다(다음
         묶음은 그대로 돈다).

    재료 수집(렌디션 크기·바이트)은 **묶음 단위로** 한다 — 11장 치를 미리 다 받아
    오면 그동안 화면이 조용해진다.
    """
    if not result or not isinstance(result, dict):
        return
    photos_ordered = photos_ordered or []
    n = len(photos_ordered)
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        return
    # 실제로 비전 콜이 붙는 블록 수 — '사진 3/5' 의 분모다. 사진 개수가 아니라
    # **본문이 실제로 쓴 image 블록 수**여야 숫자가 정직하다.
    targets = [
        (i, b, photos_ordered[b["photo_index"]])
        for i, b in enumerate(blocks)
        if isinstance(b, dict) and b.get("type") == "image"
        and isinstance(b.get("photo_index"), int)
        and 0 <= b["photo_index"] < n
    ]
    total_imgs = len(targets)
    if not total_imgs:
        return

    def _report(done):
        if on_step is None:
            return
        try:
            on_step(done, total_imgs)
        except Exception:  # noqa: BLE001 — 진척 보고가 크롭을 깨뜨리지 않게
            log.debug("crop on_step 실패", exc_info=True)

    done_imgs = 0
    for chunk in ai.crop_batches(targets):
        # 긴 콜 **앞에서** 라벨을 움직인다 — 묶음의 첫 사진 번호로. 묶음이 끝나면
        # 나머지 번호를 마저 올린다(콜백 횟수 = 블록 수, 분자 ≤ 분모).
        _report(done_imgs + 1)
        try:
            jobs, rects = [], {}
            for pos, (i, blk, p) in enumerate(chunk):
                key = f"p{done_imgs + pos:02d}"
                try:
                    dims = _crop_vision_dims(p)   # 픽셀 0바이트
                    if not dims:
                        continue  # 크기를 못 알아냄 → 크롭 없이(서빙은 다운스케일만)
                    vision_url = _crop_vision_url(p)
                    if vision_url:
                        data, ext = None, ".jpg"   # 바이트를 아예 안 만진다
                    else:
                        data, ext = _crop_vision_image(p)
                        if not data:
                            continue  # 보여 줄 그림이 없다 → 이 사진만 건너뛴다
                except Exception:  # noqa: BLE001 — 사진당 실패 격리
                    log.exception("crop 재료 수집 실패 (photo=%s)", p.id)
                    continue
                rects[key] = (blk, dims)
                jobs.append({
                    "key": key,
                    "hint": _crop_hint_for_image(blocks, i),
                    "image_bytes": data,
                    "ext": ext,
                    "image_url": vision_url,
                })
            if jobs:
                boxes = _suggest_crops(jobs, acquire_sem)
                for job in jobs:
                    key = job["key"]
                    blk, (img_w, img_h) = rects[key]
                    rect = compute_crop_rect(
                        img_w, img_h, boxes.get(key), _BLOG_CROP_ASPECT
                    )
                    blk["crop"] = [round(v, 4) for v in rect]
        except ai.Cancelled:
            raise  # 사람이 멈췄다 — 남은 묶음으로 넘어가지 않는다
        except Exception:  # noqa: BLE001 — 묶음 실패 격리(초안은 계속)
            log.exception("image crop 묶음 실패 (사진 %s장)", len(chunk))
        for k in range(1, len(chunk)):
            _report(done_imgs + 1 + k)
        done_imgs += len(chunk)


def _suggest_crops(jobs, acquire_sem):
    """묶음 비전 콜 한 번 + **빠진 키만** 단건 재질의 → ``{key: box|None}``.

    묶음이 통째로 실패해도 ``{}`` 가 오고, 그 다음 단건 재질의가 각 사진을 따로
    구한다 — 어느 단계가 깨지든 **그 사진만** 중앙 크롭(``None``)으로 떨어진다.
    """
    def _sem(fn):
        if acquire_sem:
            _CAPTION_SEM.acquire()
        try:
            return fn()
        finally:
            if acquire_sem:
                _CAPTION_SEM.release()

    def _single(j):
        return _sem(lambda: ai.suggest_crop(
            j["image_bytes"], j["hint"], _BLOG_CROP_ASPECT,
            ext=j["ext"], image_url=j["image_url"],
        ))

    if len(jobs) == 1:
        # 한 장짜리 묶음은 묶을 게 없다 — 바로 단건이고, 재질의도 없다(같은 질문을
        # 두 번 하지 않는다). None 이면 그대로 중앙 크롭이다.
        try:
            return {jobs[0]["key"]: _single(jobs[0])}
        except ai.Cancelled:
            raise
        except Exception:  # noqa: BLE001
            log.exception("suggest_crop raised (%s)", jobs[0]["key"])
            return {jobs[0]["key"]: None}

    try:
        boxes = _sem(lambda: ai.suggest_crops_batch(jobs, _BLOG_CROP_ASPECT))
    except ai.Cancelled:
        raise  # 사람이 멈췄다 — 중앙 크롭 폴백으로 '성공'처럼 넘기지 않는다
    except Exception:  # noqa: BLE001 — 비전 실패는 아래 단건/중앙 크롭으로
        log.exception("suggest_crops_batch raised (%s장)", len(jobs))
        boxes = {}
    boxes = dict(boxes or {})
    for job in jobs:
        if boxes.get(job["key"]):
            continue
        # 묶음이 이 사진을 빠뜨렸거나 키가 안 맞았다 — 그 장만 단건으로 다시 묻는다.
        log.info("crop: 묶음에서 빠진 사진을 단건으로 다시 묻는다 (%s)", job["key"])
        try:
            boxes[job["key"]] = _single(job)
        except ai.Cancelled:
            raise
        except Exception:  # noqa: BLE001 — 중앙 크롭으로 떨어진다
            log.exception("suggest_crop raised (%s)", job["key"])
            boxes[job["key"]] = None
    return boxes


def _naver_credentials_for(review):
    """이 후기의 키워드 조사에 쓸 ``((client_id, secret), 키 주인 이름)``.

    작성자 키 우선 → 없으면 같은 커플 구성원 중 키가 있는 사람. 아무도 없으면
    ``((None, None), "")``이고 조사는 꺼진다(생성은 그대로 진행). 키 **값**은
    돌려주기만 하고 로그에 남기지 않는다.
    """
    owner = db.session.get(User, review.created_by) if review.created_by else None
    if owner is not None and owner.has_naver_api_keys:
        return owner.naver_api_credentials, (owner.display_name or "")
    mate = (
        User.query.filter(
            User.couple_id == review.couple_id,
            User.id != (owner.id if owner else -1),
            User.naver_api_key_id.isnot(None),
        ).first()
        if review.couple_id
        else None
    )
    if mate is not None and mate.has_naver_api_keys:
        return mate.naver_api_credentials, (mate.display_name or "")
    return (None, None), ""


class _JobProgress:
    """한 작업이 **자기 진척을 적는** 작은 리포터 (큐 행의 step/step_index/step_total).

    왜 작업이 직접 적나 — 진행 단계는 **일하는 쪽만 안다.** 바깥에서 시간을 보고
    추측하면 그건 가짜 진행률이고, 멈춘 작업도 계속 올라간다. 그래서 claude 콜과
    콜 사이에 이 리포터를 한 번씩 부른다. 안 부르면 숫자가 안 바뀌고, **안 바뀌는
    것 자체가 정보**다(그 단계에서 오래 걸리고 있다는 뜻).

    ``total`` 은 **계획**이라 추정이 섞인다(본문이 사진을 몇 장이나 쓸지는 쓰기
    전엔 모른다). 실제 수가 밝혀지면 ``retotal`` 로 분모를 고친다 — 분자가 분모를
    넘는 거짓말은 하지 않는다.
    """

    def __init__(self, job_key, total):
        self.job_key = job_key
        self.total = max(1, int(total or 1))
        self.index = 0

    def retotal(self, total):
        """실제 단계 수가 밝혀졌다 — 분모를 고친다(이미 지난 단계보다는 크게)."""
        try:
            self.total = max(self.index, int(total))
        except (TypeError, ValueError):
            pass

    def step(self, label):
        """다음 단계에 **들어가기 직전** 호출. 절대 raise 하지 않는다."""
        self.index += 1
        if self.index > self.total:
            self.total = self.index   # 계획보다 길어졌으면 분모를 늘린다
        aijobs.progress(self.job_key, label, self.index, self.total)

    def note(self, label):
        """단계는 안 넘기고 **지금 뭘 하는지만** 바꾼다(예: 세마포어 대기).

        단계 밖의 대기도 사람에겐 '멈춘 것'으로 보이므로 이름을 붙여 준다.
        """
        aijobs.progress(self.job_key, label)


def generate_review(app, review_id):
    """백그라운드 워커: 한 BlogReview의 네이버 블로그 초안을 생성한다.

    judge_case와 같은 결 — 새 app_context(→ 새 thread-local DB 세션)를 열고
    BlogReview + 순서대로의 사진 캡션/태그를 모아 _CAPTION_SEM 안에서
    **키워드 조사(선택) → ``ai.write_review``** 순으로 호출한다. 성공하면 ai_json + status='ready',
    실패하면 status='failed'(단, 직전에 쓸 만한 ai_json이 있으면 그걸 유지해
    'ready'로 둔다). 커밋은 rollback 가드. 절대 raise 안 하고 finally에서 가드 해제.

    ⛔ **시간으로 끊지 않는다.** 예전엔 claude 콜마다 120초 상한이 걸려 있었는데
    본문 생성 한 콜의 실측이 73.7초고 Render 는 그보다 느리다 — 일하는 claude 를
    우리가 끊고 있었다(그래서 ``failed``). 지금은 ① 사람이 '✋ 그만할래'로 멈추고
    (``ai.request_cancel`` → ``ai.Cancelled``) ② 침묵은 ``ai`` 가 자동으로 잡는다.
    """
    cancel_key = f"review:{review_id}"
    try:
        with app.app_context():
            try:
                # 이 스레드에서 뜨는 claude 를 전부 이 키에 묶는다 — '✋ 그만할래'가
                # 죽일 수 있는 것은 등록된 프로세스뿐이다.
                ai.enter_cancel_scope(cancel_key)
                review = db.session.get(BlogReview, review_id)
                if review is None:
                    return  # 생성 전에 삭제됐을 수 있음

                # 직전 성공 초안이 있으면 실패 시 유지하려고 미리 캡처.
                prior_ok = review.ai is not None

                # 순서대로 사진 재료(index/caption/tags)를 만든다. 준비된 캡션이
                # 없는 사진은 caption ""(모델이 지어내지 않게). 크롭 계산에도 쓰려고
                # 순서 리스트를 한 번만 잡아둔다.
                photos_ordered_list = review.photos_ordered
                photos_arg = []
                for i, p in enumerate(photos_ordered_list):
                    cap = (p.caption or "").strip() if p.caption_status == "ready" else ""
                    photos_arg.append({"index": i, "caption": cap, "tags": p.tags_list})

                topic = review.topic
                location = review.location
                prose = review.prose
                overall = review.overall_score
                # v2 '재료' 칸(선택 입력). 빈 칸은 dict에 안 들어가고 프롬프트에서도
                # 통째로 빠진다 — 옛 후기(컬럼 NULL)는 {}라 v1과 같은 입력이 된다.
                details = review.details
                # 키워드 조사(Step 3)에 쓸 자격증명 — **후기를 쓴 사람의 키**를 먼저
                # 보고, 없으면 같은 커플의 파트너 키로 폴백한다(둘이 같은 블로그
                # 워크플로우를 돌리는 커플 스코프 앱이고, 누구 키를 썼는지는 조사
                # 메모에 남는다). 아무도 안 넣었으면 조사는 그냥 꺼진다.
                creds, key_owner = _naver_credentials_for(review)

                # ---- 진척 계획 — 화면이 "4/8단계"를 말할 수 있게 ----------
                # 이 파이프라인의 단계는 **claude 를 부르는 횟수**가 아니라 사람이
                # 기다리는 구간이다: 조사 3(키가 있을 때만) + 본문 1 + 사진당 1.
                # 사진 수는 **계획 시점의 추정**이다(본문이 실제로 몇 장을 쓸지는
                # 쓰기 전엔 모른다) — 실제 수가 나오면 분모를 고친다.
                research_on = bool(
                    (creds[0] or "").strip() and (creds[1] or "").strip()
                )
                prog = _JobProgress(
                    f"review:{review_id}",
                    (len(keyword_research.STEPS) if research_on else 0)
                    + 1 + len(photos_ordered_list),
                )

                # ⛔ **세마포어 하나 · claude 프로세스 하나로 파이프라인 전체를 덮는다.**
                #
                # 세마포어: 캡션·채점·추천과 '같은' 문이다(512MB 한 칸에 claude 가
                # 둘 뜨면 죽는다). 예전엔 (1)(2) 와 크롭 N 개가 **각자** 이 문을
                # 여닫았는데, 이제는 파이프라인이 통째로 한 번만 쥔다 — 그 안에서
                # claude 프로세스가 **계속 살아 있기** 때문이다.
                #
                # 세션: ``ai.claude_session()`` 블록 안의 모든 ``_run_claude`` 호출이
                # **한 프로세스**를 공유한다(부팅 8번 → 1번). 실측 71.5s vs 91.9s,
                # 봉우리 RSS 는 사실상 동일(580 vs 578MB) — 근거는 ai.py 머리말.
                # 세션을 못 열거나 중간에 깨지면 호출마다 조용히 one-shot 으로
                # 되돌아간다(초안은 반드시 나온다).
                #
                # ⚠️ 블록 안에서는 ``_CAPTION_SEM`` 을 **다시 잡지 않는다** — 같은
                # 스레드가 Semaphore(1) 을 두 번 잡으면 영원히 멈춘다. 그래서
                # ``_attach_section_crops(acquire_sem=False)`` 다.
                #
                # 이 문 앞에서 기다리는 시간도 사람에겐 '멈춘 것'으로 보인다 —
                # 큐 밖 claude 경로(오늘의 질문·크론 선채점)가 쥐고 있으면 여기서
                # 한참 선다. 그래서 들어가기 전에 이름을 붙여 둔다.
                prog.note("claude 차례 기다리는 중")
                _CAPTION_SEM.acquire()
                try:
                    with ai.claude_session():
                        # (1) 키워드 조사 → (2) 본문 생성. 조사는 **절대 생성을 막지
                        # 않는다** — 키가 없으면 skipped, 실패하면 failed를 메모로
                        # 남기고 지금까지와 똑같이 쓴다.
                        try:
                            research = keyword_research.research(
                                topic, location, prose, overall, details,
                                client_id=creds[0], client_secret=creds[1],
                                key_owner=key_owner, on_step=prog.step,
                            )
                        except ai.Cancelled:
                            raise  # 사람이 멈췄다 — best-effort 로 삼키지 않는다
                        except Exception:  # noqa: BLE001 — 조사는 best-effort
                            log.exception(
                                "keyword research raised (review=%s)", review_id
                            )
                            research = {"status": "failed", "reason": "exception"}
                        prog.step("본문 쓰는 중")
                        try:
                            result = ai.write_review(
                                topic, location, prose, overall, photos_arg,
                                details=details, research=research,
                            )
                        except ai.Cancelled:
                            raise  # 사람이 멈췄다 — '실패'로 접지 않는다
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "claude write_review raised (review=%s)", review_id
                            )
                            result = None

                        # ⛔ **본문을 먼저 확정해 둔다 — 크롭 전에.**
                        # 크롭 단계는 사진마다 claude 비전 콜이라 가장 긴 구간이고,
                        # 2026-10-07 처럼 그 사이에 프로세스가 죽으면 **이미 끝난
                        # 본문 생성이 통째로 버려졌다**(행은 pending 으로 남아 처음부터
                        # 다시 돌았다). 먼저 커밋해 두면 거기서 죽어도 사람은 쓸 수
                        # 있는 초안을 보고, 크롭만 다시 붙이면 된다.
                        if result:
                            _save_review_result(
                                review_id, result, research, status="ready"
                            )

                        # 각 섹션 사진에 '내용 인지' 크롭을 계산해 result에 심는다
                        # (서빙 시 적용). 크롭 실패는 초안 저장을 막지 않는다.
                        if result:
                            crop_base = prog.index   # 여기까지 소화한 단계 수

                            def _crop_step(done, total, _base=crop_base):
                                # 본문이 실제로 쓴 사진 수가 여기서 밝혀진다 —
                                # 추정이었던 분모를 그때 고친다.
                                prog.retotal(_base + total)
                                prog.step(f"사진 {done}/{total} 자르는 중")

                            try:
                                _attach_section_crops(
                                    result, photos_ordered_list, acquire_sem=False,
                                    on_step=_crop_step,
                                )
                            except ai.Cancelled:
                                raise  # 사람이 멈췄다 — 남은 사진을 더 돌리지 않는다
                            except Exception:  # noqa: BLE001 — belt & suspenders
                                log.exception(
                                    "attach section crops failed (review=%s)",
                                    review_id,
                                )
                finally:
                    _CAPTION_SEM.release()

                # 행이 바뀌었을 수 있어 재조회.
                review = db.session.get(BlogReview, review_id)
                if review is None:
                    return
                now = datetime.utcnow()
                # 조사 메모는 생성 성패와 무관하게 남긴다 — 사람이 '왜 조사가 안
                # 붙었는지'(키 미설정·조사 실패)를 화면에서 봐야 한다.
                try:
                    review.research_json = json.dumps(research, ensure_ascii=False)
                except (TypeError, ValueError):
                    review.research_json = None
                if result:
                    review.ai_json = json.dumps(result, ensure_ascii=False)
                    review.status = "ready"
                elif prior_ok:
                    # 실패했지만 직전 초안이 있으니 그대로 살려 둔다.
                    review.status = "ready"
                else:
                    review.status = "failed"
                review.updated_at = now
                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception(
                        "commit failed for blog review (review=%s)", review_id
                    )
                # 끝났다는 사실을 **폰까지** 보낸다 — 성공이든 실패든.
                # 후기는 몇 분이 걸리고 비동기라, 사람은 그 화면을 보고 있지 않다.
                # (사람이 직접 멈춘 cancelled 는 위 except 로 빠지므로 여기 안 온다.)
                _notify_review_done(review)

                # 초안이 섰으면 **상세 화면 그림을 미리 굽는다**(프리렌더).
                # 사람이 열기 전에 끝내 두는 게 요점이라 줄 맨 뒤에 세운다 —
                # claude 를 안 쓰는 잡이라 다음 claude 잡을 늦추지도 않는다.
                if result or prior_ok:
                    _enqueue_ai(app, "prerender", f"prerender:{review_id}",
                                {"review_id": review_id})
            except ai.Cancelled:
                # 사람이 멈췄다 — **실패가 아니다.** 상태는 중단 라우트가 이미
                # 'cancelled' 로 적어 뒀고(본문이 이미 커밋돼 있었으면 'ready' 로
                # 그대로 둔다), 여기서는 아무것도 덮어쓰지 않는다. 남은 단계(크롭·
                # 프리렌더)도 돌리지 않는다.
                db.session.rollback()
                log.info("후기 초안 생성을 사람이 중단했다 (review=%s)", review_id)
            except Exception:  # noqa: BLE001 — belt & suspenders; 절대 탈출 금지
                db.session.rollback()
                log.exception("generate_review failed (review=%s)", review_id)
            finally:
                ai.exit_cancel_scope(cancel_key)
    finally:
        with _generating_reviews_lock:
            _generating_reviews.discard(review_id)


def prerender_review_images(app_obj, review_id):
    """후기 상세 화면이 **열리기 전에** 그 화면의 그림을 전부 구워 둔다 (프리렌더).

    왜: 초안이 끝나면 곧 사람이 상세 화면을 연다. 그때 서버가 비로소 Graph 렌디션을
    왕복하고 Pillow 로 자르기 시작하면 그 전부가 **사람이 기다리는 시간**이 된다
    (실측: 사진 5장이면 Graph 왕복 10회 · 브라우저 기준 첫 진입 1341ms). 그 일은
    지금 해 두면 된다 — claude 는 이미 끝났고 펌프는 어차피 다음 잡을 집기 전이다.

    굽는 것 셋:
      * grid 티어 미리보기 자산 (사진 목록 썸네일)
      * view 티어 미리보기 자산 (크롭 UI·라이트박스)
      * 본문 std 바이트 (블록의 크롭 좌표 그대로 — 캐시 키가 크롭을 포함하므로
        나중에 크롭을 바꾸면 그 블록만 다시 굽힌다)

    claude 를 전혀 쓰지 않는다. 전부 best-effort — 실패하면 예전처럼 '열 때' 만들어질
    뿐이다. 절대 raise 하지 않는다.
    """
    try:
        with app_obj.app_context():
            review = db.session.get(BlogReview, review_id)
            if review is None:
                return
            photos = review.photos_ordered
            for p in photos:
                for tier in ("grid", "view"):
                    try:
                        previews.ensure(p, tier)
                    except Exception:  # noqa: BLE001 — 사진·티어당 격리
                        log.exception("prerender: 미리보기 자산 실패 (photo=%s %s)",
                                      p.id, tier)
            data = review.ai or {}
            for blk in (data.get("blocks") or []):
                if not isinstance(blk, dict) or blk.get("type") != "image":
                    continue
                pi = blk.get("photo_index")
                if not (isinstance(pi, int) and 0 <= pi < len(photos)):
                    continue
                try:
                    blog_std_bytes(photos[pi].id, _crop_str(blk.get("crop")))
                except Exception:  # noqa: BLE001 — 블록당 격리
                    log.exception("prerender: std 굽기 실패 (photo_index=%s)", pi)
            db.session.remove()
    except Exception:  # noqa: BLE001 — 절대 스레드 밖으로 나가지 않는다
        log.exception("prerender_review_images failed (review=%s)", review_id)


def _save_review_result(review_id, result, research, status):
    """후기 행에 초안(+조사 메모)을 쓰고 커밋한다. 절대 raise 하지 않는다.

    ``generate_review`` 가 **두 번** 부른다 — 본문이 나온 직후(크롭 전)와 크롭을
    붙인 뒤. 두 번째가 첫 번째를 같은 모양으로 덮으므로 멱등하다.
    """
    review = db.session.get(BlogReview, review_id)
    if review is None:
        return
    try:
        review.research_json = json.dumps(research, ensure_ascii=False)
    except (TypeError, ValueError):
        review.research_json = None
    if result is not None:
        review.ai_json = json.dumps(result, ensure_ascii=False)
    review.status = status
    review.updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("commit failed for blog review (review=%s)", review_id)


def _notify_review_done(review):
    """초안 생성이 **끝났다**는 사실을 커플 두 사람에게 알린다(인앱 + 웹푸시).

    왜 — 후기 한 건은 몇 분이 걸리고 백그라운드로 돈다. 사람은 그 화면을 켜 놓고
    기다리지 않고 **딴 일을 하러 간다.** 끝난 걸 모르면 끝난 게 아니다.

    규율 넷:
      * **성공도 실패도** 보낸다 — 기다리다 아무 소식 없는 게 제일 나쁘다.
        사람이 직접 멈춘 ``cancelled`` 는 안 보낸다(자기가 누른 걸 안다).
      * **두 사람 모두** — 이 게시판은 커플 공용이다(사용자 지시).
      * **한 사이클에 한 번** — ``ai_notified_at`` 이 그 표시다. 새 사이클을 세울 때
        (``_spawn_generate_review``) 지워지고 여기서 한 번 채워진다. 재시도·셀프힐·
        ↻ 로 완료 전이를 여러 번 밟아도 같은 사이클이면 한 번뿐이고, 프로세스가
        재시작돼도 DB 에 남아 있어 두 번 울리지 않는다.
      * **절대 raise 하지 않는다** — 알림이 생성을 깨뜨리면 본말전도다
        (``_safe_notify`` 가 이미 그 성질이고, 여기도 같은 규율이다).

    발송 자체는 기존 ``_safe_notify`` 하나로 끝난다 — 인앱 행 + 웹푸시가 거기서
    같이 나간다. 새 발송 경로를 만들지 않는다. 푸시가 꺼져 있으면(VAPID 키 없음)
    ``send_push`` 가 조용히 no-op 이고 **인앱 알림은 그대로 간다.**
    """
    try:
        if review is None or review.status not in ("ready", "failed"):
            return False
        if review.ai_notified_at is not None:
            return False  # 이번 사이클엔 이미 보냈다
        topic = (review.topic or "").strip() or "후기"
        if len(topic) > 60:
            topic = topic[:59] + "…"
        if review.status == "ready":
            msg = f"📝 블로그 초안 다 썼어 — 「{topic}」 보러 올래?"
        else:
            msg = f"😵 「{topic}」 블로그 초안을 못 만들었어 — ↻ 로 다시 돌려줄래?"
        link = f"/reviews/{review.id}"
        # 먼저 '보냈다'를 **커밋**한다 — 알림 발송 도중 죽어도 두 번 울리지 않는다.
        review.ai_notified_at = datetime.utcnow()
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001 — 표시를 못 적으면 보내지 않는다(중복 방지 우선)
            db.session.rollback()
            log.exception("후기 알림 표시 커밋 실패 (review=%s)", review.id)
            return False
        people = User.query.filter_by(couple_id=review.couple_id).all()
        for u in people:
            _safe_notify(u, "review_ai", msg, link)
        log.info("후기 초안 완료 알림 — review=%s status=%s 수신 %s명",
                 review.id, review.status, len(people))
        return True
    except Exception:  # noqa: BLE001 — 알림이 생성을 깨뜨리지 않게
        log.exception("후기 완료 알림 실패 (review=%s)",
                      getattr(review, "id", "?"))
        return False


def _spawn_generate_review(app, review_id):
    """이 후기 초안 생성 백그라운드 스레드를 스폰(이미 진행 중이면 no-op).

    judge_case 스폰과 같은 패턴 — review_id 가드로 중복(더블탭)을 떨군다. 절대
    raise 안 하고, 스폰 실패 시 가드 키를 되돌린다.

    ⚠️ 지난 '중단' 표시를 먼저 지운다 — 안 지우면 사람이 ↻ 로 다시 돌린 생성이 옛
    중단 요청을 물려받아 첫 claude 콜에서 그대로 접힌다."""
    ai.clear_cancel(f"review:{review_id}")
    spawn = False
    with _generating_reviews_lock:
        if review_id not in _generating_reviews:
            _generating_reviews.add(review_id)
            spawn = True
    if not spawn:
        return
    # **새 사이클이다** — 지난 완료 알림 표시를 지운다. 이게 "한 사이클에 한 번"의
    # 리셋 지점이고, 여기 말고 다른 데서 지우지 않는다(스폰 경로가 하나뿐이라
    # 최초 생성·셀프힐·↻ 가 전부 여기로 모인다).
    try:
        row = db.session.get(BlogReview, review_id)
        if row is not None and row.ai_notified_at is not None:
            row.ai_notified_at = None
            db.session.commit()
    except Exception:  # noqa: BLE001 — 표시 지우기가 생성을 막지 않게
        db.session.rollback()
        log.exception("후기 알림 표시 초기화 실패 (review=%s)", review_id)
    if not _enqueue_ai(app, "review", f"review:{review_id}",
                       {"review_id": review_id}):
        with _generating_reviews_lock:
            _generating_reviews.discard(review_id)


# ---------------------------------------------------------------------------
# 썸네일 카피 백그라운드 생성 (Step 4)
#
# 초안 생성(generate_review)과 **같은 패턴**이되 **같은 경로가 아니다** — 썸네일은
# 글이 완성되고 제목이 확정된 뒤에 뽑는 것이라, 사용자가 상세 화면에서 요청할 때만
# 돈다. 그래서 초안 생성 시간(이미 Render 0.1 CPU에서 ~170초)에 **아무것도 더하지
# 않는다.** claude 직렬화는 같은 _CAPTION_SEM을 쓴다(동시에 claude 하나만).
# ---------------------------------------------------------------------------
_thumbing_reviews_lock = threading.Lock()
_thumbing_reviews: set[int] = set()


def generate_thumbnail_copy(app, review_id):
    """백그라운드 워커: 한 BlogReview의 썸네일 카피 후보 3개 + 선정을 만든다.

    완성된 본문·확정된 제목·키워드 조사를 재료로 ``ai.suggest_thumbnail_copy``를
    한 번 부른다. 성공하면 ``thumbnail_json``에 ``status='ready'``로, 실패하면
    ``status='failed'``로 남긴다(직전 성공분이 있으면 그걸 지킨다 — 초안 생성과 같은
    결). 사람이 이미 고르거나 줄여 둔 문구가 있으면 **재생성이 그걸 덮어쓴다**(재생성은
    명시적 요청이다). 절대 raise 안 하고 finally에서 가드를 해제한다.
    """
    try:
        with app.app_context():
            try:
                review = db.session.get(BlogReview, review_id)
                if review is None or not review.ai:
                    return
                prior = review.thumbnail
                prior_ok = bool(prior and prior.get("candidates"))
                data = review.ai
                input_block = ai.build_review_input_block(
                    review.topic, review.location, review.prose,
                    review.overall_score, review.details,
                )
                body_text = _review_copy_text(review)
                budget = (thumbnail.spec() or {}).get("budget")
                _CAPTION_SEM.acquire()
                try:
                    result = ai.suggest_thumbnail_copy(
                        input_block, (data.get("title") or "").strip(), body_text,
                        research=review.research, budget=budget,
                    )
                except Exception:  # noqa: BLE001 — 썸네일은 best-effort
                    log.exception(
                        "claude thumbnail copy raised (review=%s)", review_id
                    )
                    result = None
                finally:
                    _CAPTION_SEM.release()

                review = db.session.get(BlogReview, review_id)  # 행이 바뀌었을 수 있다
                if review is None:
                    return
                if result:
                    state = dict(result)
                    state["status"] = "ready"
                    # 쓸 사진은 기본 0번(첫 사진) — 사람이 화면에서 바꾼다. 재생성
                    # 전에 고른 사진이 있으면 그 선택은 지킨다(문구만 새로 뽑은 것이다).
                    state["photo_index"] = (prior or {}).get("photo_index", 0)
                elif prior_ok:
                    state = dict(prior)
                    state["status"] = "ready"
                    state["last_error"] = "재생성에 실패해서 직전 문구를 그대로 뒀어."
                else:
                    state = {"status": "failed"}
                try:
                    review.thumbnail_json = json.dumps(state, ensure_ascii=False)
                except (TypeError, ValueError):
                    review.thumbnail_json = None
                review.updated_at = datetime.utcnow()
                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception(
                        "commit failed for thumbnail copy (review=%s)", review_id
                    )
            except Exception:  # noqa: BLE001 — 절대 탈출 금지
                db.session.rollback()
                log.exception(
                    "generate_thumbnail_copy failed (review=%s)", review_id
                )
    finally:
        with _thumbing_reviews_lock:
            _thumbing_reviews.discard(review_id)


def _spawn_generate_thumbnail(app, review_id):
    """썸네일 카피 생성 스레드를 스폰(이미 진행 중이면 no-op). 절대 raise 안 한다."""
    spawn = False
    with _thumbing_reviews_lock:
        if review_id not in _thumbing_reviews:
            _thumbing_reviews.add(review_id)
            spawn = True
    if not spawn:
        return
    if not _enqueue_ai(app, "thumbnail", f"thumbnail:{review_id}",
                       {"review_id": review_id}):
        with _thumbing_reviews_lock:
            _thumbing_reviews.discard(review_id)


def thumbnail_pending_verdict(review):
    """'pending' 썸네일 카피 생성의 상태 — 'running'/'orphaned'/'overdue' 또는 None.

    초안 생성과 **같은 구멍**이 여기에도 있다(인프로세스 스레드 + pending JSON 상태):
    재시작하면 상세 화면이 '만드는 중'에 영구히 박힌다. 상한은 월간 회고와 같은
    ``_STUCK_GENERATING``(5분) — 썸네일 카피는 claude 콜이 **한 번**이라
    ai.CLAUDE_TIMEOUT(120s) 이 상한이고 5분이면 넉넉하다.
    """
    if review is None:
        return None
    state = review.thumbnail or {}
    if state.get("status") != "pending":
        return None
    return _bg_job_verdict(
        _thumbing_reviews_lock, _thumbing_reviews, review.id,
        review.updated_at, _STUCK_GENERATING,
        job_key=f"thumbnail:{review.id}",
    )


def resume_thumbnail_if_orphaned(app_obj, review):
    """고아가 된 'pending' 썸네일 카피 생성을 되살린다(되살렸으면 True).

    ⚠️ ``thumbnail_json`` 의 기존 내용(사람이 고른 후보·줄인 문구·사진 선택)을
    건드리지 않는다 — 상태는 이미 'pending' 이고 되돌리는 건 updated_at 뿐이다.
    """
    if thumbnail_pending_verdict(review) != "orphaned":
        return False
    review.updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("썸네일 카피 재개 커밋 실패 (review=%s)", review.id)
        return False
    log.warning(
        "썸네일 카피 생성 스레드가 사라져 재개한다 (review=%s)", review.id
    )
    _spawn_generate_thumbnail(app_obj, review.id)
    return True


def _star_bar(score):
    """0~10 점수를 별 5개 문자열로(꽉 찬 별=2점, 홀수는 반쪽 ⯪). 복사텍스트용."""
    s = max(0, min(10, int(score or 0)))
    full = s // 2
    half = 1 if s % 2 else 0
    empty = 5 - full - half
    return "★" * full + "⯪" * half + "☆" * empty


# --- 감성 후기 v2 인라인 강조 토큰 ------------------------------------------
# AI는 색을 직접 내지 않고 아래 가벼운 토큰으로 '단어만' 감싼다. 빌더가 고정
# 팔레트의 인라인 <span>으로 바꿔 일관성을 강제한다(AI-slop·색남발 방지).
#   **굵게**     → <b>            (반드시 * 보다 먼저 파싱)
#   *핑크*       → 감정·상호·핵심어 (#e64980)
#   `파랑`       → 가격·주차·시간 등 팩트 (#1c7ed6)
#   ==형광펜==   → 배경 하이라이트 (#ffec99)
# 항상 escape(구조/스크립트 주입 차단) '먼저' 하고, 그 위에 토큰만 span으로
# 치환한다. HTML escape는 * ` = 를 건드리지 않으므로 토큰은 그대로 남아 매칭된다.
# 매칭 안 되는 기호는 이스케이프된 원문 그대로 남는다.
_EMPH_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_EMPH_PINK_RE = re.compile(r"\*(.+?)\*", re.DOTALL)
_EMPH_BLUE_RE = re.compile(r"`(.+?)`", re.DOTALL)
_EMPH_MARK_RE = re.compile(r"==(.+?)==", re.DOTALL)


def _emph_html(v):
    """한 줄 텍스트를 HTML-escape 후 강조 토큰을 인라인 span으로 치환한다."""
    s = str(escape(str(v or "")))
    s = _EMPH_BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", s)
    s = _EMPH_PINK_RE.sub(
        lambda m: f'<span style="color:#e64980;font-weight:700;">{m.group(1)}</span>', s
    )
    s = _EMPH_BLUE_RE.sub(
        lambda m: f'<span style="color:#1c7ed6;font-weight:700;">{m.group(1)}</span>', s
    )
    s = _EMPH_MARK_RE.sub(
        lambda m: f'<span style="background-color:#ffec99;font-weight:600;">{m.group(1)}</span>',
        s,
    )
    return s


def _strip_emph(v):
    """강조 토큰을 벗겨 순수 텍스트로(text/plain 폴백용)."""
    s = str(v or "")
    s = _EMPH_BOLD_RE.sub(lambda m: m.group(1), s)
    s = _EMPH_PINK_RE.sub(lambda m: m.group(1), s)
    s = _EMPH_BLUE_RE.sub(lambda m: m.group(1), s)
    s = _EMPH_MARK_RE.sub(lambda m: m.group(1), s)
    return s


def _ensure_blocks(ai_data):
    """어떤 스키마의 review.ai든 새 자유형 blocks dict로 정규화해 돌려준다.

    새 스키마(``blocks`` 있음)는 그대로 통과. 옛 스키마(summary/sections/
    info_block/ratings/faq)는 blocks로 변환한다: summary→para, 각 section→
    [heading?, para?, image?], info_block→info, ratings→ratings, faq→faq.
    (마무리 quote는 빌더가 quote 블록이 없을 때 알아서 붙이므로 여기선 안 만든다.)
    이후 빌더·크롭은 오직 ``blocks``만 다룬다. ai_data가 없으면 None. 원본을
    변형하지 않게 새 dict를 만든다.
    """
    if not ai_data or not isinstance(ai_data, dict):
        return None
    title = (ai_data.get("title") or "").strip()
    hashtags = [str(t).strip() for t in (ai_data.get("hashtags") or []) if str(t).strip()]

    raw_blocks = ai_data.get("blocks")
    if isinstance(raw_blocks, list):
        return {"title": title, "blocks": list(raw_blocks), "hashtags": hashtags}

    # --- 옛 스키마 → blocks 변환 ---
    blocks = []
    summary = (ai_data.get("summary") or "").strip()
    if summary:
        blocks.append({"type": "para", "text": summary})
    for sec in ai_data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        heading = (sec.get("heading") or "").strip()
        text = (sec.get("text") or "").strip()
        if heading:
            blocks.append({"type": "heading", "text": heading})
        if text:
            blocks.append({"type": "para", "text": text})
        pi = sec.get("photo_index")
        if isinstance(pi, int):
            blk = {"type": "image", "photo_index": pi}
            crop = sec.get("crop")
            if crop:
                blk["crop"] = crop
            blocks.append(blk)
    info_items = [it for it in (ai_data.get("info_block") or []) if isinstance(it, dict)]
    if info_items:
        blocks.append({"type": "info", "items": info_items})
    rating_items = [it for it in (ai_data.get("ratings") or []) if isinstance(it, dict)]
    if rating_items:
        blocks.append({"type": "ratings", "items": rating_items})
    faq_items = [it for it in (ai_data.get("faq") or []) if isinstance(it, dict)]
    if faq_items:
        blocks.append({"type": "faq", "items": faq_items})
    return {"title": title, "blocks": blocks, "hashtags": hashtags}


def _review_copy_text(review):
    """review.ai(JSON) → 네이버에 그대로 붙여넣는 '플레인 텍스트' 블록.

    자유형 blocks를 순서대로 평문화한다. 이미지는 네이버에 붙여넣을 수 없으니
    사진 자리에는 ``[사진 N] — 캡션`` 마커를 둔다(사진 순서대로 1부터 번호).
    강조 토큰은 제거(v2는 애초에 강조를 쓰지 않지만, 옛 후기에는 남아 있다).

    v2 스키마는 para/heading/image/quote + 시스템이 붙이는 ratings 하나뿐이다.
    ``info``(요약표)·``faq``(고정 Q&A) 분기는 **옛 후기 호환용으로만** 남겨 둔다 —
    새 초안에는 더 이상 나오지 않는다. 초안이 없으면 "".
    """
    data = _ensure_blocks(review.ai)
    if not data:
        return ""

    captions = []
    for p in review.photos_ordered:
        cap = (p.caption or "").strip() if p.caption_status == "ready" else ""
        captions.append(cap)

    lines = []
    title = (data.get("title") or "").strip()
    if title:
        lines.append(title)
        lines.append("")

    for blk in data.get("blocks") or []:
        if not isinstance(blk, dict):
            continue
        t = blk.get("type")
        if t == "para":
            text = _strip_emph((blk.get("text") or "").strip())
            if text:
                lines.append("")
                lines.append(text)
        elif t == "heading":
            heading = (blk.get("text") or "").strip()
            if heading:
                lines.append("")
                lines.append(heading)
        elif t == "quote":
            text = _strip_emph((blk.get("text") or "").strip())
            if text:
                lines.append("")
                lines.append(text)
        elif t == "image":
            pi = blk.get("photo_index")
            if isinstance(pi, int) and 0 <= pi < len(captions):
                cap = captions[pi]
                marker = f"[사진 {pi + 1}]"
                lines.append(f"{marker} — {cap}" if cap else marker)
        elif t == "info":
            items = [it for it in (blk.get("items") or []) if isinstance(it, dict)]
            rows = []
            for it in items:
                label = (it.get("label") or "").strip()
                value = (it.get("value") or "").strip()
                if label and value:
                    rows.append(f"· {label}: {value}")
            if rows:
                lines.append("")
                lines.append("▶ 핵심 정보")
                lines.extend(rows)
        elif t == "ratings":
            # v2: 표도, '⭐ 별점' 머리글도 쓰지 않는다 — 사용자가 매긴 총점 한 줄.
            # ('전체 만족도'는 v2가 붙이는 총점 항목이라 '총점'으로 적는다.)
            rows = []
            for it in (blk.get("items") or []):
                if not isinstance(it, dict):
                    continue
                aspect = (it.get("aspect") or "").strip()
                if not aspect:
                    continue
                rs = max(0, min(10, int(it.get("score") or 0)))
                if aspect == "전체 만족도":
                    aspect = "총점"
                rows.append(f"{aspect} {_star_bar(rs)} ({rs}/10)")
            if rows:
                lines.append("")
                lines.extend(rows)
        elif t == "faq":
            rows = []
            for it in (blk.get("items") or []):
                if not isinstance(it, dict):
                    continue
                q = (it.get("q") or "").strip()
                a = (it.get("a") or "").strip()
                if not q or not a:
                    continue
                rows.append(f"Q. {q}")
                rows.append(f"A. {a}")
            if rows:
                lines.append("")
                lines.append("❓ 자주 묻는 질문")
                lines.extend(rows)

    hashtags = [t for t in (data.get("hashtags") or []) if str(t).strip()]
    if hashtags:
        lines.append("")
        lines.append(" ".join(str(t).strip() for t in hashtags))

    return "\n".join(lines).strip() + "\n"


# 공개 서명 이미지 URL의 유효기간(초). 네이버는 붙여넣기/발행 시 이미지를 자기
# 서버로 재호스팅하므로 짧은 창이면 충분하다 — 영원히 공개로 두지 않는다.
_BLOG_IMG_TTL = int(os.environ.get("BLOG_IMG_TTL_HOURS", "24")) * 3600

# **발행 소스(v=orig) 전용** 수명. 이 URL 은 '네이버로 보내기'를 누른 **그 순간**
# 브라우저가 한 번 받아 Canvas 로 자르는 데만 쓰인다 — 본문 std URL(네이버가 가져가
# 재호스팅하는 것)과 달리 24시간 살아 있을 이유가 없다. 버킷에 올리지도 않는다:
# 캐시 이득이 없고(한 번 받고 끝) 짧고 매번 다른 편이 맞다.
#
# 1시간인 이유(더 짧게 안 하는 이유): 이 URL 은 **페이지를 렌더할 때** 만들어져
# `cd-export-images` 에 실린다. 사람이 상세 화면을 열어 초안을 읽고·고치고 📤 를
# 누르기까지의 간격이 그만큼 벌어질 수 있다. 만료되면 조용히 깨지지 않고
# '사진 담기 실패' 토스트가 뜨며, **새로고침**하면 새 URL 로 다시 된다.
_BLOG_PUBLISH_TTL = int(os.environ.get("BLOG_PUBLISH_TTL_SEC", "3600"))


def _crop_str(crop):
    """정규화 크롭 [x,y,w,h]를 URL·서명용 컴팩트 'x,y,w,h' 문자열로(3자리 반올림).

    쓸 수 없으면 None. url_for로 인코딩하지 않고 이 문자열을 URL에 그대로 붙이므로
    (쉼표는 쿼리에서 유효), 라우트가 되읽는 값·서명 대상·refresh 재서명이 모두
    '같은' 문자열이 되어 서명이 어긋나지 않는다.
    """
    if not crop:
        return None
    try:
        x, y, w, h = (float(v) for v in crop)
    except (TypeError, ValueError):
        return None

    def _f(v):
        return f"{round(v, 3):g}"

    return f"{_f(x)},{_f(y)},{_f(w)},{_f(h)}"


# 확장자 → Content-Type. 라우트 클로저 밖(std 바이트 생성·예열기)에서도 쓰므로
# 모듈 레벨이다(클로저 안에 두면 예열기가 못 본다 — 실제로 깨졌다).
_EXT_CONTENT_TYPES_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif",
}


def content_type_for(name):
    ext = os.path.splitext(name or "")[1].lower()
    return _EXT_CONTENT_TYPES_BY_EXT.get(ext, "image/jpeg")


def blog_std_bytes(photo_id, crop_str):
    """발행·미리보기 본문용 std 바이트 ``(data, ctype)`` 또는 None.

    이 앱에서 **서버가 픽셀을 만지는 유일한 자리**다. 왜 남았나: 이 바이트는 네이버
    스마트에디터에 붙여넣는 HTML 의 ``<img src>`` 가 가리키는데, 그 경로에는 브라우저가
    끼어들 자리가 없다(네이버가 URL 을 그대로 가져간다). 대신 **입력을 원본이 아니라
    렌디션으로** 바꿔 12MP 디코드를 없앴다 — 출력 픽셀은 같다.

    라우트와 **예열기**가 같은 함수를 쓴다. 예열이 요점이다: 초안이 완성되는 순간
    (사람이 화면을 열기 전에) 미리 구워 두면, 상세 화면 첫 진입에서 Graph 왕복도
    Pillow 재인코딩도 **이미 끝나 있다** — 첫 진입이 '리페인트 시간'에 가까워진다.
    """
    proc_key = (photo_id, crop_str or "", "std")
    cached = _blog_proc_cache_get(proc_key)
    if cached is not None:
        return cached, "image/jpeg"
    photo = db.session.get(Photo, photo_id)
    if photo is None:
        return None
    # 크롭 파싱 — 4개 float, 아니면 크롭 없음.
    crop = None
    if crop_str:
        parts = crop_str.split(",")
        if len(parts) == 4:
            try:
                crop = [float(v) for v in parts]
            except ValueError:
                crop = None
    try:
        data, src_label, target = _blog_std_source(photo, crop, _BLOG_IMG_MAX_EDGE)
    except Exception:  # noqa: BLE001 — **이미지 서빙은 500 을 내지 않는다**
        # 예전엔 ``except onedrive.OneDriveError`` 였다. 그 좁은 그물 사이로
        # ``requests.exceptions.JSONDecodeError`` 가 빠져나가 운영에서 500 이 났다
        # (본문 이미지 전부 엑박). 조용히 삼키지는 않는다 — 스택을 남긴다.
        log.exception("blog-img: 이미지 바이트 조회 실패 photo=%s", photo.id)
        return None
    if data is None:
        return None  # OneDrive 에서 사라짐
    ctype = content_type_for(photo.filename or photo.original_name)
    processed = _blog_img_process(
        data, crop=crop, max_edge=_BLOG_IMG_MAX_EDGE, quality=92,
        target_size=target,
    )
    if processed is not None:
        log.debug("blog-img std photo=%s source=%s", photo.id, src_label)
        data, ctype = processed, "image/jpeg"
        _blog_proc_cache_put(proc_key, processed)  # 가공 성공분만 캐시
    return data, ctype


def _blog_img_sig(photo_id, exp, crop_str=None, variant=None):
    """공개 서명 이미지 URL용 HMAC 토큰(앱 SECRET_KEY 서명, 무상태·DB 컬럼 없음).

    id와 만료시각(exp, unix초)을 함께 서명해 토큰이 특정 시점 이후 무효가 되게
    한다. 크롭이 있으면(``crop_str``) 크롭까지 서명에 포함해 크롭 변조를 막는다.
    하위호환: 크롭이 없으면 '옛' 문자열 ``blogimg:{id}:{exp}`` 를 그대로 서명하므로
    이미 발급된 (크롭 없는) URL도 계속 검증된다. 토큰이 곧 인가다 — 유효한 서명 +
    미만료면 그 사진 하나를 공개로 노출한다.

    ⛔ **등급(``variant``)도 서명한다.** 예전엔 ``v`` 가 서명 대상이 아니어서, 네이버
    글에 박힌 std URL(1280px·크롭된 그림)에 ``&v=orig`` 만 붙이면 **자르지 않은 풀
    해상도 원본**이 나왔다 — 크롭으로 가린 바깥 영역까지. '같은 사진 한 장'이라는
    옛 주석의 전제가 거기서 깨진다. 이제 등급이 다르면 토큰도 다르다. ``v`` 가 없는
    (std) URL 의 서명 문자열은 **그대로**라 이미 발행된 글의 이미지는 계속 열린다.
    """
    secret = current_app.config["SECRET_KEY"]
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if crop_str:
        raw = f"blogimg:{photo_id}:{exp}:{crop_str}"
    else:
        raw = f"blogimg:{photo_id}:{exp}"
    if variant:
        raw += f"|v={variant}"
    return hmac.new(secret, raw.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _blog_img_exp(now=None):
    """서명 URL 의 만료 시각 — **TTL 버킷에 고정**한다(렌더마다 달라지지 않게).

    왜: 예전엔 ``now + TTL`` 이라 **같은 사진의 URL 이 렌더할 때마다 달랐다.**
    브라우저 캐시 키는 쿼리를 포함한 URL 전체라, 상세 화면을 다시 열 때마다 이미
    받아 둔 바이트를 **버리고 전부 다시 받았다**(그리고 서버는 그만큼 Graph 렌디션을
    다시 왕복했다). 토큰이 '지금'에 매달릴 이유는 없다 — 필요한 건 '언젠가 만료된다'
    뿐이다. 그래서 만료를 TTL 경계에 올려 **같은 버킷 안에서는 URL 이 바이트까지
    동일**하게 만든다.

    유효 기간은 버킷 위치에 따라 TTL ~ 2×TTL 이다 — **옛 동작(정확히 TTL)보다 짧아지는
    일은 없다.** 네이버에 붙여넣은 임시 외부 이미지의 수명도 줄지 않는다.
    """
    now = int(now if now is not None else time.time())
    return ((now // _BLOG_IMG_TTL) + 2) * _BLOG_IMG_TTL


def blog_img_url(photo, crop=None, external=True, variant=None, ttl=None):
    """공개(인증 불필요) 서명 이미지의 절대 https URL — 네이버/독자가 로그인 없이
    가져간다. ``_external=True``라 렌더 호스트 기준 절대 URL이 나온다.

    ``crop``(정규화 [x,y,w,h])이 주어지면 ``&c=x,y,w,h``를 붙이고 그 값까지 서명한다
    (서버가 서빙 시 그 영역으로 크롭).

    ``variant`` 는 등급(``orig`` / ``r<N>``)이고 **서명에 포함된다** — std URL 에
    ``&v=orig`` 를 붙여 원본으로 승급시키는 길을 막는다(``_blog_img_sig`` 머리말).

    만료: 기본은 ``_blog_img_exp`` 의 **TTL 버킷**(같은 버킷 안에서 URL 이 안 바뀌어
    브라우저 캐시가 산다). ``ttl`` 을 주면 그 대신 '지금 + ttl' 로 **짧고 매번 다른**
    만료를 쓴다 — 발행 소스(``v=orig``)처럼 한 번 받고 끝나는 URL 용이다.
    """
    exp = (int(time.time()) + int(ttl)) if ttl else _blog_img_exp()
    crop_str = _crop_str(crop)
    sig = _blog_img_sig(photo.id, exp, crop_str, variant)
    url = url_for("blog_img", photo_id=photo.id, e=exp, t=sig, _external=external)
    if crop_str:
        # url_for로 넘기지 않고 직접 붙인다(쉼표 인코딩 방지 → 서명 문자열과 일치).
        url += f"&c={crop_str}"
    if variant:
        url += f"&v={variant}"
    return url


def publish_source_url(photo, external=True):
    """'네이버로 보내기'가 쓰는 **자르지 않은 원본** URL — 짧은 만료 · 등급 서명.

    발행의 계약(사용자 요구 5번)은 이것이다: 브라우저가 **원본**을 이 URL 로 받아,
    블록에 저장된 **정규화 크롭 좌표**로 Canvas 에서 자른 뒤 네이버에 올린다. 서버는
    픽셀을 만지지 않고, 결과 화질은 원본 픽셀 그대로다(다운스케일본에서 자르는 것보다
    낫다 — static/imagecrop.js 머리말).
    """
    return blog_img_url(photo, external=external, variant="orig",
                        ttl=_BLOG_PUBLISH_TTL)


# /blog-img/<id> 를 (기존 ?e=..&t=.. 유무와 무관하게) 잡아내는 패턴 — 스킴/호스트
# 접두와 경로는 보존하고 e·t(·c) 쿼리만 새로 교체/추가하기 위한 것. HTML 속성
# 안에서는 ``&``가 ``&amp;``로 이스케이프되므로 두 형태 모두 매칭한다(안 그러면
# 이미 토큰이 박힌 src를 못 잡고 뒤에 두 번째 쿼리를 붙여 URL을 망가뜨린다).
# 그룹2 = 선택적 크롭 문자열(x,y,w,h) — refresh가 보존·재서명한다.
# 그룹3 = 선택적 등급(v=orig / v=r1280) — 이제 서명 대상이라 재서명에 함께 넣는다.
_BLOG_IMG_RE = re.compile(
    r"/blog-img/(\d+)(?:\?e=\d+&(?:amp;)?t=[0-9a-f]+"
    r"(?:&(?:amp;)?c=([0-9.,]+))?(?:&(?:amp;)?v=([0-9a-z]+))?)?"
)


def _refresh_blog_img_tokens(html):
    """HTML 안의 모든 ``/blog-img/<id>`` 를 '지금' 기준 신선한 ``?e=&t=(&c=)`` 로 재작성.

    저장된 edited_text가 옛(이미 만료된) 토큰을 얼려버렸어도, 뷰 시점에 항상
    유효한 토큰이 들어가게 한다. 스킴/호스트 접두와 ``/blog-img/<id>`` 경로,
    그리고 ``c=`` 크롭 파라미터는 그대로 보존하고 e·t만 교체하되, 새 토큰은 크롭까지
    포함해 재서명한다(크롭 URL도 계속 유효). blog-img가 아닌 URL·주변 속성은 건드리지
    않는다. ``blog_img_url``이 애초에 신선한 토큰을 내므로 재생성 경로에는 무해·멱등.

    HTML 속성 컨텍스트라 ``&``는 ``&amp;``로 낸다(빌더의 이스케이프와 일치 →
    재작성 결과를 다시 돌려도 멱등).
    """
    if not html:
        return html

    def _sub(m):
        pid = int(m.group(1))
        crop_str = m.group(2)   # 없으면 None
        variant = m.group(3)    # 없으면 None (저장된 본문 HTML 은 std 라 보통 없다)
        exp = _blog_img_exp()   # TTL 버킷 — 같은 버킷 안에서 URL 이 안 바뀐다
        sig = _blog_img_sig(pid, exp, crop_str, variant)
        out = f"/blog-img/{pid}?e={exp}&amp;t={sig}"
        if crop_str:
            out += f"&amp;c={crop_str}"
        if variant:
            out += f"&amp;v={variant}"
        return out

    return _BLOG_IMG_RE.sub(_sub, html)


_FIRST_IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"']", re.I)


def _first_img_src(html):
    """HTML 안 **첫 번째** ``<img src>`` 문자열(없으면 None).

    상세 화면이 ``<link rel=preload as=image>`` 로 **첫 사진 한 장만** 먼저 받기
    시작하는 데 쓴다. 미리 받을 URL 은 실제 ``src`` 와 **바이트까지 같아야** 한다 —
    한 글자라도 다르면 브라우저가 같은 그림을 두 번 받는다. 그래서 추정하지 않고
    렌더될 HTML 에서 그대로 꺼낸다(``&amp;`` 는 브라우저가 같은 URL 로 해석한다).
    """
    if not html:
        return None
    m = _FIRST_IMG_SRC_RE.search(html)
    if not m:
        return None
    return m.group(1).replace("&amp;", "&")


def _gen_export_key():
    """유저스크립트 인증용 URL-safe 토큰(~24자)."""
    return secrets.token_urlsafe(18)


def _ensure_export_key(u):
    """사용자에게 export_key가 없으면 생성·저장(유니크 충돌 시 재시도). 값을 반환."""
    if getattr(u, "export_key", None):
        return u.export_key
    for _ in range(5):
        cand = _gen_export_key()
        u.export_key = cand
        try:
            db.session.commit()
            return cand
        except IntegrityError:
            db.session.rollback()
            u = db.session.get(User, u.id)
            if getattr(u, "export_key", None):
                return u.export_key
    return getattr(u, "export_key", None)


def _build_export_payload(review):
    """준비된 후기 → (doc_data, images). cd-doc-data(온페이지)와 공개 pending API 공용.

    ⛔ ``images[i].url`` 은 이제 **크롭되지 않은 원본**(``v=orig``)이고, 잘라낼
    자리는 ``images[i].crop``(정규화 [x,y,w,h])으로 **좌표만** 함께 내려간다.
    유저스크립트가 받은 뒤 **Canvas 로 자른다** — 서버는 픽셀을 만지지 않고, 결과
    화질은 *원본 픽셀 그대로*다(서버가 1280 다운스케일본에서 자르던 것보다 낫다).
    ``images[i].src``↔블록 ``__src``는 iK(사진 id+크롭)로 매칭한다(쿼리/등급 무시).
    준비 안 됐으면 (None, None).
    """
    if not (review.status == "ready" and review.ai):
        return None, None
    data = _ensure_blocks(review.ai)
    blocks = (data or {}).get("blocks") or []
    photos_ordered = review.photos_ordered
    images = []
    for blk in blocks:
        if not isinstance(blk, dict) or blk.get("type") != "image":
            continue
        pi = blk.get("photo_index")
        if not (isinstance(pi, int) and 0 <= pi < len(photos_ordered)):
            continue
        p = photos_ordered[pi]
        crop = blk.get("crop")
        key_src = blog_img_url(p, crop=crop)      # 매칭 키(크롭까지 서명된 std URL)
        blk["__src"] = key_src
        images.append({
            "src": key_src,
            "url": publish_source_url(p),  # 자르지 않은 원본(짧은 만료·등급 서명)
            "crop": [round(float(v), 6) for v in crop] if crop else None,
        })
    created = review.created_at
    visit_ymd = f"{created.year}년 {created.month}월" if created else ""
    visit_foot = created.strftime("%Y. %m") if created else ""
    doc_data = {
        "title": (data or {}).get("title") or "",
        "visitDate": visit_ymd,
        "visitFoot": visit_foot,
        "overallScore": review.overall_score,
        "hashtags": (data or {}).get("hashtags") or [],
        "blocks": blocks,
    }
    return doc_data, images


def _review_copy_html(review):
    """review.ai(JSON) → 네이버 스마트에디터 ONE에 그대로 붙여넣는 HTML 본문.

    text/html 클립보드로 복사하면 제목/굵게/표/구분선/이미지 서식이 유지된다
    (사용자가 실브라우저에서 검증한 매핑). 래퍼 <div>/<style>/class 없이 인라인
    ``style``만 쓴 '플랫' 본문 — <html>/<head>/<body> 없음. 텍스트 내용은 전부
    이스케이프(구조 태그만 직접 조립). 이미지는 공개 서명 URL(blog_img_url·절대)로
    — 인증 프록시(/memories/<id>/image) 금지. 초안이 없으면 "".
    """
    data = _ensure_blocks(review.ai)
    if not data:
        return ""

    def esc(v):
        return str(escape(str(v or "").strip()))

    # --- 감성 후기 v2 팔레트/스타일(빌더가 고정 — AI는 색을 못 낸다) ---------
    HR = '<hr style="border:0;border-top:1px solid #e5e5e5;margin:26px 0;">'
    CENTER = 'style="text-align:center;margin:0 0 6px;"'          # 본문 한 줄
    H3 = ('style="text-align:center;font-size:17px;font-weight:800;'
          'color:#222;margin:6px 0 14px;"')
    # 소제목 장식 이모지 — 빌더가 heading 순서로 순환(AI가 못 고른다).
    MOTIFS = ["☕️", "🍰", "📷", "🌿", "🍽️", "🤍"]

    photos = review.photos_ordered
    score = max(0, min(10, int(review.overall_score or 0)))

    # 실제 방문 날짜(AI가 아닌 리뷰 날짜에서 계산 — footer와 같은 원천).
    created = review.created_at
    visit_ymd = f"{created.year}년 {created.month}월" if created else ""
    visit_foot = created.strftime("%Y. %m") if created else ""

    out = []

    def add_hr():
        """직전이 이미 HR이면 중복 방지, 아니면 구분선 추가."""
        if out and out[-1] == HR:
            return
        out.append(HR)

    def center_lines(raw):
        for ln in str(raw or "").replace("\r\n", "\n").split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            out.append(f'<p {CENTER}>{_emph_html(ln)}</p>')

    # 1) 제목(가운데). 줄바꿈은 <br>로.
    title = (data.get("title") or "").strip()
    if title:
        title_html = "<br>".join(
            _emph_html(t.strip())
            for t in title.replace("\r\n", "\n").split("\n") if t.strip()
        )
        out.append(
            '<h2 style="text-align:center;font-size:20px;font-weight:800;'
            f'line-height:1.5;color:#222;margin:6px 0 22px;">{title_html}</h2>'
        )

    # 2) 시스템 헤더 — 실제 방문 날짜 + 핑크 내돈내산 라인(항상, 위치 고정).
    if visit_ymd:
        out.append(
            # 볼드 금지(v2) — 시스템 헤더도 평문으로.
            f'<p style="text-align:center;margin:2px 0;line-height:2;">'
            f'방문 날짜 : {esc(visit_ymd)}</p>'
        )
    out.append(
        '<p style="text-align:center;color:#e64980;font-weight:700;'
        'letter-spacing:1px;margin:12px 0 2px;">✱ 내돈내산 데이트 후기 ✱</p>'
    )
    out.append(HR)

    def render_info(items):
        rows = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            label = (it.get("label") or "").strip()
            value = (it.get("value") or "").strip()
            if not label or not value:
                continue
            if "방문" in label and ("날짜" in label or "일" in label):
                continue  # 방문 날짜는 시스템 헤더가 담당 → AI값 무시(중복·지어냄 방지)
            rows.append(
                # 볼드 금지(v2). info 블록은 이제 생성되지 않고 옛 후기에만 남아 있다.
                f'<p style="text-align:center;margin:2px 0;line-height:2;">'
                f'{esc(label)} : {esc(value)}</p>'
            )
        if rows:
            add_hr()
            out.extend(rows)

    def render_ratings(items):
        """별점을 **표가 아니라 평문 줄**로 낸다 (v2).

        gf-blog 규율이 요약표·장단점표를 금지하고 네이버 발행 본문에서 표를 쓰지
        말라고 한다. 별점 자체는 사용자가 직접 매긴 값이라 남기되, 표 대신 한 줄로
        적는다. 옛 후기의 항목별 별점(AI가 만들던 값)도 같은 평문 줄로 그린다.
        """
        rows = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            aspect = (it.get("aspect") or "").strip()
            if not aspect:
                continue
            rs = max(0, min(10, int(it.get("score") or 0)))
            if aspect == "전체 만족도":
                continue  # 바로 아래 '총점' 줄과 같은 값이라 중복
            rows.append(
                f'<p style="text-align:center;margin:2px 0;line-height:2;">'
                f'{esc(aspect)} {_star_bar(rs)} ({rs}/10)</p>'
            )
        rows.append(
            f'<p style="text-align:center;margin:2px 0;line-height:2;">'
            f'총점 {_star_bar(score)} ({score}/10)</p>'
        )
        add_hr()
        out.extend(rows)

    def render_faq(items):
        faq_out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            q = (it.get("q") or "").strip()
            a = (it.get("a") or "").strip()
            if not q or not a:
                continue
            faq_out.append(
                f'<p style="text-align:center;margin:2px 0;">Q. {esc(q)}</p>'
            )
            faq_out.append(
                f'<p style="text-align:center;margin:2px 0 12px;">A. {esc(a)}</p>'
            )
        if faq_out:
            add_hr()
            out.append(f'<h3 {H3}>자주 묻는 질문</h3>')
            out.extend(faq_out)

    def render_quote(text):
        add_hr()
        body = "<br>".join(
            _emph_html(ln.strip())
            for ln in str(text or "").replace("\r\n", "\n").split("\n") if ln.strip()
        )
        foot = f"{esc(visit_foot)} 방문 · 내돈내산" if visit_foot else "내돈내산"
        out.append(
            '<blockquote style="border:0;text-align:center;font-size:16px;'
            f'color:#444;margin:8px 0;line-height:1.9;">{body}'
            '<span style="display:block;color:#aaa;font-size:13px;'
            f'margin-top:8px;">{foot}</span></blockquote>'
        )

    # 3) 블록을 순서대로 렌더.
    motif_i = 0
    has_quote = False
    for blk in data.get("blocks") or []:
        if not isinstance(blk, dict):
            continue
        t = blk.get("type")
        if t == "heading":
            heading = (blk.get("text") or "").strip()
            if not heading:
                continue
            add_hr()
            motif = MOTIFS[motif_i % len(MOTIFS)]
            motif_i += 1
            out.append(
                '<p style="text-align:center;color:#c4c4c4;letter-spacing:3px;'
                f'margin:22px 0 8px;">· · · {motif} · · ·</p>'
            )
            out.append(f'<h3 {H3}>{esc(heading)}</h3>')
        elif t == "para":
            center_lines(blk.get("text"))
        elif t == "image":
            pi = blk.get("photo_index")
            if isinstance(pi, int) and 0 <= pi < len(photos):
                p = photos[pi]
                cap = (p.caption or "").strip() if p.caption_status == "ready" else ""
                alt = esc(cap or "후기 사진")
                # 저장된 내용 인지/수동 크롭(있으면)을 서명 URL에 실어 서버가 그 영역을
                # 잘라 서빙. 없으면(옛 후기) 크롭 없이 다운스케일만.
                src = esc(blog_img_url(p, crop=blk.get("crop")))
                out.append(
                    f'<p style="text-align:center;margin:14px 0 4px;">'
                    f'<img src="{src}" alt="{alt}" '
                    'style="max-width:100%;height:auto;border-radius:10px;"></p>'
                )
        elif t == "info":
            render_info(blk.get("items"))
        elif t == "ratings":
            render_ratings(blk.get("items"))
        elif t == "faq":
            render_faq(blk.get("items"))
        elif t == "quote":
            render_quote(blk.get("text"))
            has_quote = True

    # 4) quote 블록이 없으면 마무리 한 줄을 붙여 방문월 footer를 보장.
    #    v2: "한 번 가보시길 추천드려요" 류 확언을 뺐다 — 코호트 23편에 그런 확언이
    #    한 건도 없고, 끝은 대부분 "변동될 수 있으니 방문 전 확인" 안내로 맺는다.
    if not has_quote:
        topic = (review.topic or "").strip()
        tail = "운영시간과 메뉴는 변동될 수 있으니 방문 전 최신 정보를 확인해 주세요."
        if topic:
            closing = f"{esc(topic)} 다녀온 후기였어요.<br>{tail}"
        else:
            closing = f"다녀온 후기였어요.<br>{tail}"
        add_hr()
        foot = f"{esc(visit_foot)} 방문 · 내돈내산" if visit_foot else "내돈내산"
        out.append(
            '<blockquote style="border:0;text-align:center;font-size:16px;'
            f'color:#444;margin:8px 0;line-height:1.9;">{closing}'
            '<span style="display:block;color:#aaa;font-size:13px;'
            f'margin-top:8px;">{foot}</span></blockquote>'
        )

    # 5) 해시태그(가운데, 핑크).
    hashtags = [str(t).strip() for t in (data.get("hashtags") or []) if str(t).strip()]
    if hashtags:
        joined = " ".join(esc(t) for t in hashtags)
        add_hr()
        out.append(
            '<p style="text-align:center;color:#e0559b;font-weight:600;'
            f'margin:6px 0;">{joined}</p>'
        )

    return "\n".join(out)


# 선채점 커플당 하드 캡 — 워커 배치(≈24행)를 최대 이만큼 반복(≈480행)해
# 부분 실패로 인한 폭주(무한 재선택)를 막는다.
_PREWARM_MAX_BATCHES = 20


def prewarm_scores(app):
    """야간 갱신 직후 선채점(先採点) — 활성 커플마다 진행 중 행사 중 'ready'
    점수가 없는 것을 미리 채점해, 누가 탭을 열기 전에 점수가 준비되게 한다.

    활성 커플 = 승인 멤버(status='approved')가 2명 이상인 커플. 커플당
    score_events_for_couple을 '무점수가 사라질 때까지'(반환 0) 또는 하드 캡
    (_PREWARM_MAX_BATCHES)까지 순차 반복한다. 워커가 자기 app_context를 열고
    캡션과 공유하는 _CAPTION_SEM으로 claude를 직렬화하므로 동시에 claude는
    항상 하나뿐이다. _scoring 가드는 호출 사이에 풀리므로 순차 루프는 데드락
    없다. 절대 스레드 밖으로 raise하지 않는다(모두 감싸고 로그).

    ⚠️ 중복 안전(dedupe 유지): 행사 원본의 (source, source_uid) upsert와
    uq_event_source_uid 제약은 전혀 손대지 않는다 — 신규 행사 삽입이 기존 행을
    복제하지 않는다. 선채점은 워커의 아웃터조인(EventScore 없음 OR status!=
    'ready') 대상만 골라 EventScore를 만들고, uq_eventscore_couple_event가
    커플·행사당 한 행을 보장하므로 점수 행도 중복되지 않는다.
    """
    try:
        active_ids = []
        with app.app_context():
            try:
                for c in Couple.query.all():
                    try:
                        if len(c.approved_members) >= 2:
                            active_ids.append(c.id)
                    except Exception:  # noqa: BLE001 — 한 커플 실패가 전체 막지 않게
                        continue
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("prewarm: 활성 커플 조회 실패")
                return

        # 커플별로 순차 선채점(워커가 각자 자기 app_context를 연다).
        for couple_id in active_ids:
            try:
                batches = 0
                while batches < _PREWARM_MAX_BATCHES:
                    n = score_events_for_couple(app, couple_id)
                    batches += 1
                    if not n:  # 남은 무점수 없음(또는 진전 없음) → 이 커플 종료
                        break
            except Exception:  # noqa: BLE001 — best-effort, 절대 탈출 금지
                log.exception("prewarm: 커플 %s 선채점 실패", couple_id)
    except Exception:  # noqa: BLE001 — belt & suspenders
        log.exception("prewarm_scores failed")


def refresh_popups_worker(app):
    """백그라운드 워커: claude 웹검색으로 팝업을 모아 EventItem(source='popup')로
    upsert하고 만료 팝업을 지운다. cron_refresh_popups가 데몬 스레드로 띄운다.

    캡션·채점과 공유하는 _CAPTION_SEM으로 claude 콜을 직렬화한다(512MB 티어:
    동시에 claude는 하나만). 서울 upsert와 '똑같이' (source, source_uid)로
    upsert하고 per-row try/except+rollback로 한 행 실패가 전체를 막지 않게 한다.
    절대 스레드 밖으로 raise하지 않으며, finally에서 세마포어와 락을 반드시 푼다.

    ⚠️ 중복 안전(dedupe 유지): (source, source_uid) upsert + 기존
    uq_event_source_uid 제약 덕에 팝업끼리도, 서울 행사와도(source가 달라)
    복제되지 않는다."""
    try:
        with app.app_context():
            # claude 웹검색 콜을 캡션/채점과 같은 세마포어로 직렬화.
            _CAPTION_SEM.acquire()
            try:
                try:
                    items = events.fetch_popups()
                except Exception:  # noqa: BLE001 — claude가 스레드 죽이지 못하게
                    log.exception("popup fetch raised")
                    items = []
            finally:
                _CAPTION_SEM.release()

            fetched = len(items)
            upserted = 0
            for it in items:
                try:
                    # 서울 upsert와 동일: (source, source_uid)로 조회→갱신, 없으면 삽입.
                    row = EventItem.query.filter_by(
                        source=it["source"], source_uid=it["source_uid"]
                    ).first()
                    if row is None:
                        row = EventItem(source=it["source"], source_uid=it["source_uid"])
                        db.session.add(row)
                    row.title = it["title"]
                    row.category = it.get("category")
                    row.description = it.get("description")
                    row.place = it.get("place")
                    row.district = it.get("district")
                    row.image_url = it.get("image_url")
                    row.link = it.get("link")
                    row.fee = it.get("fee")
                    row.is_free = it.get("is_free")
                    row.start_date = it.get("start_date")
                    row.end_date = it.get("end_date")
                    db.session.commit()
                    upserted += 1
                except Exception:  # noqa: BLE001 — 한 행 실패가 전체를 막지 않게
                    db.session.rollback()
                    log.exception("popup upsert failed (uid=%s)", it.get("source_uid"))

            # 만료 팝업 삭제(source='popup' AND end_date 있고 오늘보다 과거).
            expired_deleted = 0
            try:
                today = date.today()
                expired_deleted = (
                    EventItem.query.filter(
                        EventItem.source == "popup",
                        EventItem.end_date.isnot(None),
                        EventItem.end_date < today,
                    ).delete(synchronize_session=False)
                )
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("popup expiry delete failed")

            # 무기한(end_date NULL) 팝업 안전 만료: 종료일을 못 뽑은 팝업은
            # 21일 넘게 묵으면 지운다(무기한 잔류 방지). dedupe upsert는 그대로.
            stale_deleted = 0
            try:
                cutoff = datetime.utcnow() - timedelta(days=21)
                stale_deleted = (
                    EventItem.query.filter(
                        EventItem.source == "popup",
                        EventItem.end_date.is_(None),
                        EventItem.created_at < cutoff,
                    ).delete(synchronize_session=False)
                )
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("popup stale(undated) expiry delete failed")

            log.info(
                "popup refresh done: fetched=%s upserted=%s expired=%s stale=%s",
                fetched, upserted, expired_deleted, stale_deleted,
            )
    except Exception:  # noqa: BLE001 — 스레드 밖으로 절대 raise 금지
        log.exception("refresh_popups_worker failed")
    finally:
        # 스폰 측이 잡아둔 모듈 락을 워커 종료 시 반드시 해제(중복 실행 재허용).
        try:
            _popups_refresh_lock.release()
        except RuntimeError:
            pass  # 이미 풀렸으면 무시


# 데이트 뉴스 목록 무한스크롤 배치 크기(초기 렌더·/dates/more 공통).
_DATES_BATCH = 18

# 찜 상태 값(EventPick.status). interested만 확정/공유목록에 든다.
_PICK_STATUSES = ("interested", "visited", "dismissed")


def _event_pick(u, event_id):
    """현재 사용자의 이 행사 EventPick(없으면 None)."""
    if u is None:
        return None
    return EventPick.query.filter_by(event_id=event_id, user_id=u.id).first()


def _pick_states(couple_id, user_id, event_ids):
    """행사 여러 건의 이 커플 찜 상태를 한 번의 쿼리로 모아
    event_id -> {my_status, partner_status, confirmed, interested_count}로 준다.

    N+1을 피하려 (couple, 대상 event들)의 EventPick을 한 번에 읽고 파이썬에서
    묶는다. confirmed = interested한 서로 다른 사용자가 2명(둘 다 찜)."""
    states = {}
    ids = list(event_ids)
    if not ids:
        return states
    picks = EventPick.query.filter(
        EventPick.couple_id == couple_id,
        EventPick.event_id.in_(ids),
    ).all()
    by_event = {}
    for p in picks:
        by_event.setdefault(p.event_id, []).append(p)
    for eid in ids:
        my = None
        partner_status = None
        interested_users = set()
        for p in by_event.get(eid, []):
            if p.user_id == user_id:
                my = p.status
            else:
                partner_status = p.status
            if p.status == "interested":
                interested_users.add(p.user_id)
        states[eid] = {
            "my_status": my,
            "partner_status": partner_status,
            "confirmed": len(interested_users) >= 2,
            "interested_count": len(interested_users),
        }
    return states


def _upsert_pick(u, event_id, status):
    """현재 사용자의 이 행사 찜 상태를 status로 upsert(유니크 충돌은 재조회 갱신)."""
    pick = EventPick.query.filter_by(event_id=event_id, user_id=u.id).first()
    if pick is None:
        pick = EventPick(
            couple_id=u.couple_id, event_id=event_id, user_id=u.id, status=status
        )
        db.session.add(pick)
    else:
        pick.couple_id = u.couple_id
        pick.status = status
        pick.updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except IntegrityError:
        # 동시 삽입이 내 행을 먼저 만든 경우 — 재조회해 상태만 갱신.
        db.session.rollback()
        pick = EventPick.query.filter_by(event_id=event_id, user_id=u.id).first()
        if pick is not None:
            pick.status = status
            pick.updated_at = datetime.utcnow()
            db.session.commit()
    return pick


def _pick_msg(prefix, title, suffix=""):
    """알림 메시지 조립 — 제목이 길면 잘라 Notification.message(255) 상한을 넘기지 않게."""
    t = (title or "")[:60]
    return f"{prefix}{t}{suffix}"


def _ordered_date_items(couple_id, user_id, cat=None, sort="score", direction="desc"):
    """진행 중(만료 안 된) 행사를 이 커플 EventScore와 LEFT JOIN해
    {event, score, reason, score_status, bucket} 리스트로 만들고, 카테고리
    버킷(cat)으로 거르고 정렬해 반환한다.

    cat: None/"전체"이면 전체(“기타” 버킷은 여기서만 노출). 그 외는
         events.event_category_bucket 결과가 cat과 같은 것만("팝업"은 아직
         소스가 없어 빈 목록).
    sort/direction(모르는 값은 기본값으로 정규화):
      * sort="score"(추천순) — ready 먼저(점수순, direction=="desc"면 높은 순·
        "asc"면 낮은 순, 동점이면 마감 임박·시작 순) → pending → 나머지.
      * sort="date"(최신순) — 필터된 전부를 start_date(폴백 created_at)로만 정렬,
        direction=="desc"면 최신 먼저·"asc"면 오래된 먼저. NULL은 항상 맨 뒤.
    ~300행 기준 저렴하다. dates()·dates_more()가 이 단일 원천을 공유해 일관된다."""
    # sort/direction 정규화 — 알 수 없는 값은 안전한 기본으로.
    if sort not in ("score", "date"):
        sort = "score"
    if direction not in ("desc", "asc"):
        direction = "desc"
    want_bucket = cat if (cat and cat != "전체") else None

    today = date.today()
    rows = (
        db.session.query(EventItem, EventScore)
        .outerjoin(
            EventScore,
            db.and_(
                EventScore.event_id == EventItem.id,
                EventScore.couple_id == couple_id,
            ),
        )
        .filter(
            db.or_(EventItem.end_date.is_(None), EventItem.end_date >= today)
        )
        .all()
    )

    items = []
    for ev, sc in rows:
        bucket = events.event_category_bucket(ev.category)
        if want_bucket is not None and bucket != want_bucket:
            continue
        status = sc.status if sc is not None else None
        items.append(
            {
                "event": ev,
                "score": sc.score if sc is not None else None,
                "reason": sc.reason if sc is not None else None,
                "score_status": status,
                "bucket": bucket,
            }
        )

    # 사용자별 찜 상태 부착 + 피드 정리: 현재 사용자가 방문함/관심없음 한 행사는
    # 그 사용자 피드에서 제외한다(커플 스코프, 한 번의 쿼리로 배치 조회).
    states = _pick_states(couple_id, user_id, [it["event"].id for it in items])
    kept = []
    for it in items:
        st = states.get(it["event"].id, {})
        if st.get("my_status") in ("visited", "dismissed"):
            continue
        it["my_status"] = st.get("my_status")
        it["confirmed"] = st.get("confirmed", False)
        it["interested_count"] = st.get("interested_count", 0)
        kept.append(it)
    items = kept

    # 마감 없는(end_date NULL) 건 날짜 정렬에서 맨 뒤로 가도록 큰 값을 준다.
    _far = date.max

    if sort == "date":
        # 최신순: 점수 그룹 무시, start_date(폴백 created_at)로만 정렬. NULL은
        # 방향과 무관하게 항상 맨 뒤(플래그 0=있음/1=없음이 1차 키).
        asc = (direction == "asc")

        def _dkey(it):
            ev = it["event"]
            d = ev.start_date
            if d is None:
                ca = getattr(ev, "created_at", None)
                d = ca.date() if ca is not None else None
            if d is None:
                return (1, 0)  # 날짜 없음 → 맨 뒤
            ordv = d.toordinal()
            return (0, ordv if asc else -ordv)

        items.sort(key=_dkey)
        return items

    def _rank(it):
        st = it["score_status"]
        ev = it["event"]
        end = ev.end_date or _far
        if st == "ready":
            # ready 내부: direction=="desc"면 점수 높은 순(-score), "asc"면 낮은 순.
            s = it["score"] or 0
            skey = -s if direction == "desc" else s
            return (0, skey, end, ev.start_date or _far)
        if st == "pending":
            return (1, 0, end, ev.start_date or _far)
        return (2, 0, end, ev.start_date or _far)

    items.sort(key=_rank)
    return items


def _date_recommendation_payload(couple_id):
    """이 커플 최신 '추천받기' 결과를 렌더/JSON 공용 dict로 만든다(순수 DB 읽기).

    반환: ``{status, message, picks:[{event_id, title, why, image_url, category,
    source}]}``. picks는 status=='ready'일 때만 채우고, 각 픽의 행사를 조회해
    존재하지 않거나 만료된(end_date < 오늘) 픽은 떨군다. 행이 없으면 status
    'none'. dates()·date_recommend_status()가 이 단일 원천을 공유한다."""
    rec = DateRecommendation.query.filter_by(couple_id=couple_id).first()
    if rec is None:
        return {"status": "none", "message": None, "picks": []}
    picks = []
    if rec.status == "ready":
        today = date.today()
        for pk in rec.picks:
            if not isinstance(pk, dict):
                continue
            ref = pk.get("ref")
            if ref is None:
                continue
            ev = db.session.get(EventItem, ref)
            if ev is None:
                continue
            # 더 이상 존재하지 않거나 만료된 행사는 픽에서 제외.
            if ev.end_date is not None and ev.end_date < today:
                continue
            picks.append(
                {
                    "event_id": ev.id,
                    "title": ev.title,
                    "why": (pk.get("why") or ""),
                    "image_url": ev.image_url,
                    "category": ev.category,
                    "source": ev.source,
                }
            )
    return {"status": rec.status, "message": rec.message or "", "picks": picks}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _register_routes(app: Flask):
    @app.context_processor
    def inject_globals():
        # DB 장애 내성: 유휴 후 죽은 커넥션 등으로 DB가 잠깐 실패해도 여기서
        # 터지면 *모든* 페이지(에러 페이지 포함)가 함께 무너진다. DB를 만지는
        # 부분은 감싸서 실패 시 안전한 기본값으로 폴백한다(정상 경로는 그대로).
        me = None
        notif_unread = 0
        app_name = DEFAULT_APP_NAME
        try:
            me = current_user()
            # Cheap unread COUNT for the topbar bell dot — only for a logged-in user
            # who's in a couple (the only place the bell is shown).
            if me is not None and me.couple_id:
                notif_unread = (
                    Notification.query.filter_by(user_id=me.id, is_read=False).count()
                )
            app_name = Setting.get("app_name", DEFAULT_APP_NAME)
        except Exception:
            # 오염된 트랜잭션을 비우고 안전한 기본값으로 계속 렌더한다.
            db.session.rollback()
            log.debug("inject_globals: DB 조회 실패 — 안전 기본값으로 폴백", exc_info=True)
            me = None
            notif_unread = 0
            app_name = DEFAULT_APP_NAME
        return {
            "app_name": app_name,
            "me": me,
            "kakao_enabled": kakao_enabled(),
            "push_enabled": push_enabled(),
            "onedrive_enabled": onedrive.onedrive_enabled(),
            "notif_unread": notif_unread,
            # 미리보기 자산 URL — **사진당 고정**이고 서명·만료가 없다. 템플릿이
            # url_for 를 직접 부르지 않고 이걸 쓰는 이유는 캐시 버전(PREVIEW_REV)을
            # 한 곳에서만 붙이기 위해서다(티어 크기를 바꿨을 때의 유일한 손잡이).
            "thumb_url": _thumb_url,
            "preview_url": _preview_url,
            "preview_rev": previews.REV,
        }

    @app.template_filter("timeago")
    def _timeago(dt):
        """Short Korean relative time. Comment timestamps are UTC (utcnow)."""
        if not dt:
            return ""
        secs = (datetime.utcnow() - dt).total_seconds()
        if secs < 60:
            return "방금"
        mins = int(secs // 60)
        if mins < 60:
            return f"{mins}분 전"
        hours = int(mins // 60)
        if hours < 24:
            return f"{hours}시간 전"
        days = int(hours // 24)
        if days < 7:
            return f"{days}일 전"
        return dt.strftime("%Y.%m.%d")

    # ---- landing: 캘린더 홈 (질문 대시보드는 /today 로 이동) ----
    @app.route("/")
    def index():
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if not u.couple_id:
            # Shouldn't normally happen (signup always attaches a couple), but guard.
            return render_template("no_couple.html")
        if u.status == "pending":
            return render_template("pending.html")

        couple = u.couple
        # Admin waiting for a partner to accept.
        pending_partner = couple.members.filter_by(status="pending").first()
        if u.is_admin and pending_partner:
            return render_template(
                "approve.html", pending=pending_partner, invite_code=couple.invite_code
            )
        # Approved but partner hasn't joined yet.
        if len(couple.approved_members) < 2:
            return render_template("waiting_partner.html", invite_code=couple.invite_code)

        # Full, active couple → 캘린더 홈(이번 달 달력 + 날짜별 사진).
        return _render_calendar_home(couple)

    def _render_calendar_home(couple):
        """이번 달(또는 ?month=YYYY-MM) 달력을 렌더한다. 각 날짜에 그 날 올린
        사진(커버 썸네일·개수)을 얹고, 아래 날짜 패널은 클라이언트가 채운다."""
        today = date.today()
        raw = (request.args.get("month") or "").strip()
        year, month = today.year, today.month
        if raw:
            try:
                year, month = (int(x) for x in raw.split("-", 1))
                date(year, month, 1)  # 유효성 검증(엉뚱한 값이면 이번 달로 폴백)
            except (ValueError, TypeError):
                year, month = today.year, today.month

        month_start = date(year, month, 1)
        days_in_month = calendar.monthrange(year, month)[1]
        # 다음 달 1일(반열림 상한). created_at 은 datetime 이라 경계로 쓴다.
        next_start = date(year, month, days_in_month) + timedelta(days=1)

        # 이번 달 커플 사진을 조회해 '날짜 → 사진들'로 묶는다(커플 스코프).
        # 유효 날짜 = COALESCE(taken_at, created_at): EXIF 촬영일이 있으면 그 날,
        # 없으면 업로드시각으로 폴백한다. 나중에 올린 사진도 '찍은 날'에 매핑된다.
        eff_date = func.coalesce(Photo.taken_at, Photo.created_at)
        photos = (
            Photo.query.filter(
                Photo.couple_id == couple.id,
                eff_date >= datetime(year, month, 1),
                eff_date
                < datetime(next_start.year, next_start.month, next_start.day),
            )
            .order_by(eff_date.asc(), Photo.id.asc())
            .all()
        )
        by_day = {}
        for p in photos:
            eff = p.taken_at or p.created_at  # 유효 날짜(촬영일 우선, 없으면 업로드일)
            by_day.setdefault(eff.day, []).append(p)

        # 셀/패널이 함께 쓰는 JSON: "day" -> {count, cover, ids, schedules}
        PANEL_CAP = 60  # 하루 패널 썸네일 상한(과다 방지)
        days_data = {}
        for d, plist in by_day.items():
            ids = [p.id for p in plist]
            days_data[str(d)] = {
                "count": len(ids),
                "cover": ids[-1],  # 그 날 가장 최근 사진을 커버로
                "ids": ids[:PANEL_CAP],
                "schedules": [],
            }

        # 이번 달 일정(Schedule)을 조회해 같은 days_data에 병합한다(커플 스코프).
        # 사진만 있는 날·일정만 있는 날·둘 다 있는 날 모두 한 엔트리에 담긴다.
        # 여러 날 일정(end_date)은 이 달과 겹치는 모든 날에 제목을 얹는다 —
        # 이 달 전에 시작했거나 다음 달까지 이어지는 일정도 겹치는 구간만 표시한다.
        month_end = date(year, month, days_in_month)
        schedules = (
            Schedule.query.filter(
                Schedule.couple_id == couple.id,
                Schedule.date < next_start,
                db.or_(
                    Schedule.end_date >= month_start,
                    db.and_(
                        Schedule.end_date.is_(None),
                        Schedule.date >= month_start,
                    ),
                ),
            )
            .order_by(Schedule.date.asc(), Schedule.id.asc())
            .all()
        )
        for s in schedules:
            multi = s.end_date is not None and s.end_date != s.date
            # 이 달과 겹치는 구간 [span_start, span_end]의 모든 날에 표시한다.
            span_start = max(s.date, month_start)
            span_end = min(s.end_date or s.date, month_end)
            cur = span_start
            while cur <= span_end:
                key = str(cur.day)
                entry = days_data.get(key)
                if entry is None:
                    # 사진 없는 '일정만' 있는 날 — 사진 필드는 비워 둔다.
                    entry = {"count": 0, "cover": None, "ids": [], "schedules": []}
                    days_data[key] = entry
                entry["schedules"].append(
                    {
                        "id": s.id,
                        "title": s.title,
                        "time": s.time_label,  # '오전 9:00' 등, 종일이면 ''
                        "event_id": s.event_id,
                        "multi": multi,
                    }
                )
                cur += timedelta(days=1)

        # 이번 달 '내기 체크인'을 조회해 같은 days_data에 병합한다(커플 스코프).
        # 이 커플의 내기(Bet)에 속한 체크인만 세되, 날마다 '누가'(사람별)·'어떤 내기'
        # 를 구분해 담는다. 아바타와 같은 팔레트를 user.id % 6 로 인덱싱해 사람별
        # 색을 정한다(앱 전체 일관). 사진·일정이 없고 체크인만 있는 날도 엔트리를
        # 만들어 셀이 눌리고 마커가 보이게 한다.
        BET_PALETTE = ["#ff8fb1", "#ffb37a", "#8fd6b4", "#8fb6ff", "#c79bff", "#ff9e9e"]
        bet_rows = (
            db.session.query(
                BetCheckin.date,
                BetCheckin.user_id,
                User.display_name,
                Bet.id,
                Bet.title,
            )
            .join(Bet, BetCheckin.bet_id == Bet.id)
            .join(User, BetCheckin.user_id == User.id)
            .filter(
                Bet.couple_id == couple.id,
                BetCheckin.date >= month_start,
                BetCheckin.date < next_start,
            )
            .order_by(BetCheckin.date.asc(), BetCheckin.user_id.asc(), Bet.id.asc())
            .all()
        )
        # 날짜별로 (사람별 요약 people_map)·(체크인 상세 목록)을 모은다.
        bet_by_day = {}
        for cd, uid, uname, bid, btitle in bet_rows:
            day_info = bet_by_day.setdefault(cd.day, {"people": {}, "checkins": []})
            person = day_info["people"].get(uid)
            if person is None:
                person = {
                    "user_id": uid,
                    "name": uname,
                    "color": BET_PALETTE[uid % len(BET_PALETTE)],
                    "count": 0,
                }
                day_info["people"][uid] = person
            person["count"] += 1
            day_info["checkins"].append(
                {
                    "user_id": uid,
                    "name": uname,
                    "color": BET_PALETTE[uid % len(BET_PALETTE)],
                    "bet_id": bid,
                    "bet_title": btitle,
                }
            )
        for cd_day, day_info in bet_by_day.items():
            key = str(cd_day)
            entry = days_data.get(key)
            if entry is None:
                entry = {"count": 0, "cover": None, "ids": [], "schedules": []}
                days_data[key] = entry
            # 셀용: 사람별 색점(user id 순으로 안정 정렬).
            entry["bet_people"] = [
                day_info["people"][uid] for uid in sorted(day_info["people"])
            ]
            # 패널용: 각 체크인(사람+내기) 상세 목록.
            entry["bet_checkins"] = day_info["checkins"]

        # 일요일 시작 그리드 — 앞쪽 빈칸 = (첫날 요일 +1) % 7 (월=0..일=6).
        lead = (month_start.weekday() + 1) % 7
        cells = [None] * lead + list(range(1, days_in_month + 1))
        while len(cells) % 7:
            cells.append(None)
        weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]

        prev_last = month_start - timedelta(days=1)
        prev_month = f"{prev_last.year:04d}-{prev_last.month:02d}"
        next_month = f"{next_start.year:04d}-{next_start.month:02d}"

        today_day = today.day if (today.year == year and today.month == month) else None
        selected_day = today_day or 1  # 기본 선택일: 오늘(이번 달)이면 오늘, 아니면 1일

        return render_template(
            "calendar.html",
            year=year,
            month=month,
            month_label=f"{year}년 {month}월",
            weeks=weeks,
            weekday_headers=["일", "월", "화", "수", "목", "금", "토"],
            days_data=days_data,
            today_day=today_day,
            selected_day=selected_day,
            prev_month=prev_month,
            next_month=next_month,
        )

    # ---- 캘린더 일정(Schedule) CRUD ----
    def _get_schedule_or_404(u, sid):
        """일정을 로드하되, 요청자 커플의 것이 아니면 404 — 커플 간 접근 차단."""
        s = db.session.get(Schedule, sid)
        if s is None or s.couple_id != u.couple_id:
            abort(404)
        return s

    def _parse_date(raw):
        """'YYYY-MM-DD' → date. 형식이 틀리면 None(폴백 판단은 호출부가)."""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            y, m, d = (int(x) for x in raw.split("-", 2))
            return date(y, m, d)
        except (ValueError, TypeError):
            return None

    def _parse_time(raw):
        """'HH:MM' → datetime.time. 비었거나 형식이 틀리면 None(종일로 취급 — 비차단)."""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            hh, mm = (int(x) for x in raw.split(":", 1))
            return dtime(hh, mm)
        except (ValueError, TypeError):
            return None

    @app.route("/calendar/schedule/new", methods=["GET", "POST"])
    @active_couple_required
    def schedule_new():
        """일정 추가. GET은 폼(날짜·제목·설명; ?date=·?event_id= 프리필), POST는
        검증 후 생성하고 그 달 캘린더로 리다이렉트한다. ?event_id=가 이 커플 피드의
        유효한 행사면 event_id를 걸고 제목/설명을 프리필한다(무효면 무시)."""
        u = current_user()

        # 확정→'날짜 정하기' 연동: event_id가 이 커플 피드의 행사면 프리필/링크.
        def _load_event(raw):
            try:
                eid = int(raw)
            except (TypeError, ValueError):
                return None
            return db.session.get(EventItem, eid)

        if request.method == "POST":
            d = _parse_date(request.form.get("date"))
            end_raw = (request.form.get("end_date") or "").strip()
            end = _parse_date(end_raw)
            time_raw = (request.form.get("start_time") or "").strip()
            start_time = _parse_time(time_raw)  # 형식 틀리면 None(종일) — 비차단
            title = (request.form.get("title") or "").strip()[:200]
            description = (request.form.get("description") or "").strip() or None
            event = _load_event(request.form.get("event_id"))

            def _rerender():
                # 입력값을 살려 폼을 다시 보여준다.
                return render_template(
                    "schedule_form.html",
                    mode="new",
                    schedule=None,
                    form_date=(request.form.get("date") or ""),
                    form_end=end_raw,
                    form_time=time_raw,
                    form_title=title,
                    form_description=description or "",
                    event=event,
                )

            if not title or d is None:
                flash("날짜와 제목을 모두 입력해줘.", "error")
                return _rerender()
            if end_raw and (end is None or end < d):
                flash("종료일은 시작일과 같거나 이후 날짜여야 해.", "error")
                return _rerender()
            sched = Schedule(
                couple_id=u.couple_id,
                date=d,
                end_date=(end if end_raw else None),
                start_time=start_time,
                title=title,
                description=description,
                created_by=u.id,
                event_id=event.id if event is not None else None,
            )
            db.session.add(sched)
            db.session.commit()
            flash("일정을 추가했어. 📌", "ok")
            return redirect(url_for("index", month=d.strftime("%Y-%m")))

        # GET — ?date= / ?event_id= 프리필.
        prefill_date = _parse_date(request.args.get("date")) or date.today()
        event = _load_event(request.args.get("event_id"))
        form_title = event.title if event is not None else ""
        # 설명 프리필: 장소·링크 힌트를 부드럽게 채운다(있을 때만).
        form_description = ""
        if event is not None:
            bits = []
            if event.place:
                bits.append(event.place)
            if event.link:
                bits.append(event.link)
            form_description = "\n".join(bits)
        return render_template(
            "schedule_form.html",
            mode="new",
            schedule=None,
            form_date=prefill_date.strftime("%Y-%m-%d"),
            form_end="",
            form_time="",
            form_title=form_title,
            form_description=form_description,
            event=event,
        )

    @app.route("/calendar/schedule/<int:sid>")
    @active_couple_required
    def schedule_detail(sid):
        """일정 상세 — 날짜·제목·설명, event_id가 있으면 데이트 뉴스로 링크.
        이 커플의 일정이 아니면 404."""
        u = current_user()
        sched = _get_schedule_or_404(u, sid)
        return render_template("schedule_detail.html", schedule=sched, event=sched.event)

    @app.route("/calendar/schedule/<int:sid>/edit", methods=["GET", "POST"])
    @active_couple_required
    def schedule_edit(sid):
        """일정 수정(날짜·제목·설명). 커플 구성원 누구나 수정 가능.
        POST 후 상세로 리다이렉트."""
        u = current_user()
        sched = _get_schedule_or_404(u, sid)
        if request.method == "POST":
            d = _parse_date(request.form.get("date"))
            end_raw = (request.form.get("end_date") or "").strip()
            end = _parse_date(end_raw)
            time_raw = (request.form.get("start_time") or "").strip()
            start_time = _parse_time(time_raw)  # 비었거나 틀리면 None(종일) — 비차단
            title = (request.form.get("title") or "").strip()[:200]
            description = (request.form.get("description") or "").strip() or None

            def _rerender():
                return render_template(
                    "schedule_form.html",
                    mode="edit",
                    schedule=sched,
                    form_date=(request.form.get("date") or ""),
                    form_end=end_raw,
                    form_time=time_raw,
                    form_title=title,
                    form_description=description or "",
                    event=sched.event,
                )

            if not title or d is None:
                flash("날짜와 제목을 모두 입력해줘.", "error")
                return _rerender()
            if end_raw and (end is None or end < d):
                flash("종료일은 시작일과 같거나 이후 날짜여야 해.", "error")
                return _rerender()
            sched.date = d
            sched.end_date = end if end_raw else None
            sched.start_time = start_time  # 비우면 종일로 되돌린다(클리어)
            sched.title = title
            sched.description = description
            sched.updated_at = datetime.utcnow()
            db.session.commit()
            flash("일정을 수정했어.", "ok")
            return redirect(url_for("schedule_detail", sid=sched.id))
        return render_template(
            "schedule_form.html",
            mode="edit",
            schedule=sched,
            form_date=sched.date.strftime("%Y-%m-%d"),
            form_end=(sched.end_date.strftime("%Y-%m-%d") if sched.end_date else ""),
            form_time=(sched.start_time.strftime("%H:%M") if sched.start_time else ""),
            form_title=sched.title,
            form_description=sched.description or "",
            event=sched.event,
        )

    @app.route("/calendar/schedule/<int:sid>/delete", methods=["POST"])
    @active_couple_required
    def schedule_delete(sid):
        """일정 삭제 후 그 달 캘린더로 리다이렉트."""
        u = current_user()
        sched = _get_schedule_or_404(u, sid)
        month = sched.date.strftime("%Y-%m")
        db.session.delete(sched)
        db.session.commit()
        flash("일정을 삭제했어.", "ok")
        return redirect(url_for("index", month=month))

    @app.route("/calendar/schedule/<int:sid>/ics")
    @active_couple_required
    def schedule_ics(sid):
        """이 일정의 .ics(iCalendar)를 내려준다 — 탭하면 기기 캘린더에 '추가'된다
        (iOS Safari→캘린더 이벤트 추가, 안드→기본/구글 캘린더). 이 커플의 일정이
        아니면 404. 시간이 없으면 종일(배타적 DTEND), 있으면 그 시각 시작+1시간.
        연결된 데이트 행사가 있으면 장소·링크를 LOCATION·URL로 싣는다."""
        u = current_user()
        sched = _get_schedule_or_404(u, sid)
        all_day = sched.start_time is None
        if all_day:
            dt_start = sched.date
            # 배타적 DTEND: 여러 날이면 end_date+1, 하루면 date+1.
            dt_end = (sched.end_date or sched.date) + timedelta(days=1)
        else:
            dt_start = datetime.combine(sched.date, sched.start_time)
            dt_end = dt_start + timedelta(hours=1)  # 기본 1시간
        location = url = None
        if sched.event is not None:
            location = sched.event.place or None
            url = sched.event.link or None
        body = ics.build_ics(
            uid=f"schedule-{sched.id}@ourday",
            summary=sched.title,
            dt_start=dt_start,
            dt_end=dt_end,
            all_day=all_day,
            description=sched.description or None,
            location=location,
            url=url,
        )
        resp = app.response_class(body)
        resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
        # inline = iOS가 캘린더 앱으로 바로 넘김(다운로드 X)
        resp.headers["Content-Disposition"] = (
            f'inline; filename="ourday-schedule-{sched.id}.ics"'
        )
        return resp

    # ---- 캘린더 '내기'(Bet) — 습관 내기(2a) ----
    def _get_bet_or_404(u, bid):
        """내기를 로드하되, 요청자 커플의 것이 아니면 404 — 커플 간 접근 차단."""
        b = db.session.get(Bet, bid)
        if b is None or b.couple_id != u.couple_id:
            abort(404)
        return b

    def _resolve_target(u, raw):
        """대상 select 값 → target_user_id 매핑.
        ""(같이) → None · 커플 구성원 id → 그 id · 그 외 → (None, False) 무효."""
        raw = (raw or "").strip()
        if not raw:
            return None, True  # 같이(둘 다)
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            return None, False
        member_ids = {m.id for m in u.couple.approved_members}
        if tid in member_ids:
            return tid, True
        return None, False

    @app.route("/calendar/bets")
    @active_couple_required
    def calendar_bets():
        """이 커플의 내기 목록(진행 중 먼저, 종료 나중). 습관 내기는 이번 주 진행·
        오늘 달성 토글·벌칙을 함께 보여준다."""
        u = current_user()
        couple = u.couple
        bets = (
            Bet.query.filter(Bet.couple_id == couple.id)
            .order_by(Bet.status.asc(), Bet.created_at.desc())
            .all()
        )
        # 'active' < 'ended' 알파벳 순이라 status asc면 active가 먼저 온다.
        today = date.today()
        rows = []
        for b in bets:
            if b.type == "prediction":
                # 예측 내기 — 습관 헬퍼(주 진행) 대신 판정 결과를 붙인다.
                rows.append(
                    {
                        "bet": b,
                        "type": "prediction",
                        "prediction": prediction_result(b, couple),
                        "end": bet_end_info(b, today),
                    }
                )
                continue
            prog = bet_progress(b, couple, today)
            # 지금 사용자가 이 내기 참여자인지 + 오늘 이미 달성했는지
            mine = next(
                (p for p in prog["participants"] if p["user_id"] == u.id), None
            )
            rows.append(
                {
                    "bet": b,
                    "type": "habit",
                    "progress": prog,
                    "is_participant": mine is not None,
                    "done_today": bool(mine and mine["done_today"]),
                    "target_name": (b.target_user.display_name
                                    if b.target_user_id else None),
                    "end": bet_end_info(b, today),
                }
            )
        return render_template("bets.html", rows=rows, me=u)

    def _bet_form_ctx(u, mode, bet, **overrides):
        """bet_form.html 렌더용 공용 컨텍스트(습관+예측 필드 전부 프리필 가능).
        overrides로 폼 값(form_*)을 덮어쓴다."""
        member_a, member_b = prediction_members(u.couple)
        ctx = dict(
            mode=mode,
            bet=bet,
            me=u,
            partner=u.partner,
            member_a=member_a,
            member_b=member_b,
            form_type="habit",
            form_title="",
            form_count="",
            form_target="",
            form_start=date.today().strftime("%Y-%m-%d"),
            form_end="",
            form_penalty="",
            form_description="",
            form_guess_a="",
            form_guess_b="",
        )
        ctx.update(overrides)
        return ctx

    @app.route("/calendar/bets/new", methods=["GET", "POST"])
    @active_couple_required
    def bet_new():
        """내기 추가 — 타입 선택(습관/예측). 습관은 활동·주 N회·대상·시작일, 예측은
        각자 날짜(안정적 a/b)로 갈린다. 벌칙·설명은 공통. 서버가 type을 읽어 분기."""
        u = current_user()
        if request.method == "POST":
            bet_type = (request.form.get("type") or "habit").strip()
            penalty = (request.form.get("penalty") or "").strip() or None
            description = (request.form.get("description") or "").strip() or None
            # 종료일(마감일) — 두 종류 공통. 비면 NULL(무기한).
            end_raw = (request.form.get("end_date") or "").strip()
            end = _parse_date(end_raw)

            if bet_type == "prediction":
                # 예측 제목은 ptitle로 받는다(JS 없이도 정확히 서버가 읽게 필드 분리).
                title = (request.form.get("ptitle") or "").strip()[:200]
                member_a, member_b = prediction_members(u.couple)
                a_name = member_a.display_name if member_a else "A"
                b_name = member_b.display_name if member_b else "B"
                guess_a = _parse_date(request.form.get("guess_a_date"))
                guess_b = _parse_date(request.form.get("guess_b_date"))
                errs = []
                if not title:
                    errs.append("예측 내용")
                if guess_a is None:
                    errs.append(f"{a_name} 날짜")
                if guess_b is None:
                    errs.append(f"{b_name} 날짜")
                if end_raw and end is None:
                    errs.append("종료일(올바른 날짜)")
                if errs:
                    flash("입력을 확인해줘: " + ", ".join(errs) + ".", "error")
                    return render_template("bet_form.html", **_bet_form_ctx(
                        u, "new", None,
                        form_type="prediction",
                        form_title=title,
                        form_end=end_raw,
                        form_penalty=penalty or "",
                        form_description=description or "",
                        form_guess_a=(request.form.get("guess_a_date") or ""),
                        form_guess_b=(request.form.get("guess_b_date") or ""),
                    ))
                bet = Bet(
                    couple_id=u.couple_id,
                    type="prediction",
                    title=title,
                    description=description,
                    start_date=date.today(),  # 예측은 롤링 주 미사용 — 생성 마커
                    end_date=(end if end_raw else None),
                    guess_a_date=guess_a,
                    guess_b_date=guess_b,
                    penalty=penalty,
                    status="active",
                    created_by=u.id,
                )
                db.session.add(bet)
                db.session.commit()
                flash("예측 내기를 만들었어. 누가 더 가까울까? 🔮", "ok")
                return redirect(url_for("bet_detail", bid=bet.id))

            # ---- 습관 내기(기존 경로 — 동작 불변) ----
            title = (request.form.get("title") or "").strip()[:200]
            start = _parse_date(request.form.get("start_date")) or date.today()
            target_id, target_ok = _resolve_target(u, request.form.get("target"))
            try:
                count_target = int((request.form.get("count_target") or "").strip())
            except (TypeError, ValueError):
                count_target = 0
            errs = []
            if not title:
                errs.append("활동")
            if count_target < 1:
                errs.append("주 N회(1 이상)")
            if not target_ok:
                errs.append("대상")
            if end_raw and (end is None or end < start):
                errs.append("종료일(시작일 이후)")
            if errs:
                flash("입력을 확인해줘: " + ", ".join(errs) + ".", "error")
                return render_template("bet_form.html", **_bet_form_ctx(
                    u, "new", None,
                    form_type="habit",
                    form_title=title,
                    form_count=(request.form.get("count_target") or ""),
                    form_target=(request.form.get("target") or ""),
                    form_start=(request.form.get("start_date")
                                or date.today().strftime("%Y-%m-%d")),
                    form_end=end_raw,
                    form_penalty=penalty or "",
                    form_description=description or "",
                ))
            bet = Bet(
                couple_id=u.couple_id,
                type="habit",
                title=title,
                description=description,
                target_user_id=target_id,
                start_date=start,
                end_date=(end if end_raw else None),
                count_target=count_target,
                penalty=penalty,
                status="active",
                created_by=u.id,
            )
            db.session.add(bet)
            db.session.commit()
            flash("내기를 만들었어. 화이팅! 🔥", "ok")
            return redirect(url_for("bet_detail", bid=bet.id))

        return render_template("bet_form.html", **_bet_form_ctx(u, "new", None))

    @app.route("/calendar/bets/<int:bid>/checkin", methods=["POST"])
    @active_couple_required
    def bet_checkin(bid):
        """오늘의 '달성' 토글(행동하는 사용자 기준). 참여자가 아니면 부드러운 안내.
        (내기, 나, 오늘) 행이 없으면 생성, 있으면 삭제(오클릭 되돌리기).
        온 곳(목록/상세)으로 돌아간다."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        couple = u.couple
        today = date.today()

        # 되돌아갈 곳: next 파라미터 > referrer > 목록
        nxt = (request.form.get("next") or request.referrer
               or url_for("calendar_bets"))

        participant_ids = {p.id for p in bet_participants(bet, couple)}
        if u.id not in participant_ids:
            flash("이 내기의 대상이 아니야 — 응원만 해줘! 😊", "error")
            return redirect(nxt)
        if bet.status != "active":
            flash("종료된 내기야.", "error")
            return redirect(nxt)
        if bet.end_date is not None and bet.end_date < today:
            flash("이 내기는 마감됐어 — 종료일이 지났어.", "error")
            return redirect(nxt)

        existing = BetCheckin.query.filter_by(
            bet_id=bet.id, user_id=u.id, date=today
        ).first()
        if existing is None:
            db.session.add(
                BetCheckin(bet_id=bet.id, user_id=u.id, date=today)
            )
            try:
                db.session.commit()
            except IntegrityError:
                # 동시 중복(유니크 충돌) — 이미 있는 것으로 간주.
                db.session.rollback()
            flash("오늘 달성 체크! 🔥", "ok")
            # 파트너에게 가벼운 알림(선택·비차단).
            _safe_notify(
                u.partner,
                "answer",
                f"{u.display_name}님이 '{bet.title}' 내기를 오늘 달성했어! 🔥",
                url_for("bet_detail", bid=bet.id),
            )
        else:
            db.session.delete(existing)
            db.session.commit()
            flash("오늘 달성을 취소했어.", "ok")
        return redirect(nxt)

    @app.route("/calendar/bets/<int:bid>")
    @active_couple_required
    def bet_detail(bid):
        """내기 상세 — 타입별. 습관은 이번 주 요일 체크·지난 주 요약, 예측은 각자
        예측·실제 날짜·판정(더 가까운 사람)·벌칙. 커플 것이 아니면 404."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        couple = u.couple
        today = date.today()

        if bet.type == "prediction":
            return render_template(
                "bet_detail.html",
                bet=bet,
                me=u,
                pred=prediction_result(bet, couple),
                end=bet_end_info(bet, today),
            )

        prog = bet_progress(bet, couple, today)
        week_days = bet_week_days(prog["week_start"])
        by_user = bet_week_checkins(bet, prog["week_start"], prog["week_end"])
        # 참여자별 이번 주 요일 셀(체크 여부) 구성
        week_rows = []
        for p in prog["participants"]:
            checks = by_user.get(p["user_id"], set())
            cells = [{"date": d, "checked": d in checks, "is_today": d == today}
                     for d in week_days]
            week_rows.append({"p": p, "cells": cells})
        past = bet_past_weeks(bet, couple, today, cap=8)
        mine = next(
            (p for p in prog["participants"] if p["user_id"] == u.id), None
        )
        return render_template(
            "bet_detail.html",
            bet=bet,
            me=u,
            progress=prog,
            week_days=week_days,
            week_rows=week_rows,
            past=past,
            is_participant=mine is not None,
            done_today=bool(mine and mine["done_today"]),
            target_name=(bet.target_user.display_name if bet.target_user_id else None),
            end=bet_end_info(bet, today),
        )

    @app.route("/calendar/bets/<int:bid>/edit", methods=["GET", "POST"])
    @active_couple_required
    def bet_edit(bid):
        """내기 수정 — 타입별 분기. 습관은 활동·주 N회·대상·시작일, 예측은 예측
        내용·각자 날짜. 벌칙·설명은 공통. 내기의 type은 바꾸지 않는다."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        if request.method == "POST":
            penalty = (request.form.get("penalty") or "").strip() or None
            description = (request.form.get("description") or "").strip() or None
            # 종료일(마감일) — 두 종류 공통. 비면 NULL(무기한).
            end_raw = (request.form.get("end_date") or "").strip()
            end = _parse_date(end_raw)

            if bet.type == "prediction":
                title = (request.form.get("ptitle") or "").strip()[:200]
                member_a, member_b = prediction_members(u.couple)
                a_name = member_a.display_name if member_a else "A"
                b_name = member_b.display_name if member_b else "B"
                guess_a = _parse_date(request.form.get("guess_a_date"))
                guess_b = _parse_date(request.form.get("guess_b_date"))
                errs = []
                if not title:
                    errs.append("예측 내용")
                if guess_a is None:
                    errs.append(f"{a_name} 날짜")
                if guess_b is None:
                    errs.append(f"{b_name} 날짜")
                if end_raw and end is None:
                    errs.append("종료일(올바른 날짜)")
                if errs:
                    flash("입력을 확인해줘: " + ", ".join(errs) + ".", "error")
                    return render_template("bet_form.html", **_bet_form_ctx(
                        u, "edit", bet,
                        form_type="prediction",
                        form_title=title,
                        form_end=end_raw,
                        form_penalty=penalty or "",
                        form_description=description or "",
                        form_guess_a=(request.form.get("guess_a_date") or ""),
                        form_guess_b=(request.form.get("guess_b_date") or ""),
                    ))
                bet.title = title
                bet.guess_a_date = guess_a
                bet.guess_b_date = guess_b
                bet.end_date = end if end_raw else None
                bet.penalty = penalty
                bet.description = description
                bet.updated_at = datetime.utcnow()
                db.session.commit()
                flash("내기를 수정했어.", "ok")
                return redirect(url_for("bet_detail", bid=bet.id))

            # ---- 습관 내기(기존 경로 — 동작 불변) ----
            title = (request.form.get("title") or "").strip()[:200]
            start = _parse_date(request.form.get("start_date")) or bet.start_date
            target_id, target_ok = _resolve_target(u, request.form.get("target"))
            try:
                count_target = int((request.form.get("count_target") or "").strip())
            except (TypeError, ValueError):
                count_target = 0
            errs = []
            if not title:
                errs.append("활동")
            if count_target < 1:
                errs.append("주 N회(1 이상)")
            if not target_ok:
                errs.append("대상")
            if end_raw and (end is None or end < start):
                errs.append("종료일(시작일 이후)")
            if errs:
                flash("입력을 확인해줘: " + ", ".join(errs) + ".", "error")
                return render_template("bet_form.html", **_bet_form_ctx(
                    u, "edit", bet,
                    form_type="habit",
                    form_title=title,
                    form_count=(request.form.get("count_target") or ""),
                    form_target=(request.form.get("target") or ""),
                    form_start=(request.form.get("start_date")
                                or bet.start_date.strftime("%Y-%m-%d")),
                    form_end=end_raw,
                    form_penalty=penalty or "",
                    form_description=description or "",
                ))
            bet.title = title
            bet.count_target = count_target
            bet.target_user_id = target_id
            bet.start_date = start
            bet.end_date = end if end_raw else None
            bet.penalty = penalty
            bet.description = description
            bet.updated_at = datetime.utcnow()
            db.session.commit()
            flash("내기를 수정했어.", "ok")
            return redirect(url_for("bet_detail", bid=bet.id))

        # GET — 현재 값으로 프리필(타입별).
        _form_end = bet.end_date.strftime("%Y-%m-%d") if bet.end_date else ""
        if bet.type == "prediction":
            return render_template("bet_form.html", **_bet_form_ctx(
                u, "edit", bet,
                form_type="prediction",
                form_title=bet.title,
                form_end=_form_end,
                form_penalty=bet.penalty or "",
                form_description=bet.description or "",
                form_guess_a=(bet.guess_a_date.strftime("%Y-%m-%d")
                              if bet.guess_a_date else ""),
                form_guess_b=(bet.guess_b_date.strftime("%Y-%m-%d")
                              if bet.guess_b_date else ""),
            ))
        return render_template("bet_form.html", **_bet_form_ctx(
            u, "edit", bet,
            form_type="habit",
            form_title=bet.title,
            form_count=(bet.count_target if bet.count_target else ""),
            form_target=(str(bet.target_user_id) if bet.target_user_id else ""),
            form_start=bet.start_date.strftime("%Y-%m-%d"),
            form_end=_form_end,
            form_penalty=bet.penalty or "",
            form_description=bet.description or "",
        ))

    @app.route("/calendar/bets/<int:bid>/resolve", methods=["POST"])
    @active_couple_required
    def bet_resolve(bid):
        """예측 내기 결과 입력 — 실제 날짜를 받아 더 가까운 예측을 승자로 판정한다.
        da=|guess_a-actual|, db=|guess_b-actual| → da<db면 a, db<da면 b, 같으면 무승부.
        status를 ended로. 예측 내기만·커플 스코프."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        couple = u.couple
        if bet.type != "prediction":
            flash("예측 내기만 결과를 입력할 수 있어.", "error")
            return redirect(url_for("bet_detail", bid=bet.id))
        if bet.guess_a_date is None or bet.guess_b_date is None:
            flash("두 사람의 예측 날짜가 있어야 결과를 낼 수 있어.", "error")
            return redirect(url_for("bet_detail", bid=bet.id))
        actual = _parse_date(request.form.get("actual_date"))
        if actual is None:
            flash("실제 날짜를 올바르게 입력해줘.", "error")
            return redirect(url_for("bet_detail", bid=bet.id))
        da = abs((bet.guess_a_date - actual).days)
        dist_b = abs((bet.guess_b_date - actual).days)
        bet.actual_date = actual
        bet.winner = "a" if da < dist_b else "b" if dist_b < da else "tie"
        bet.status = "ended"
        bet.updated_at = datetime.utcnow()
        db.session.commit()
        res = prediction_result(bet, couple)
        if bet.winner == "tie":
            flash("무승부! 둘 다 똑같이 가까웠어. 🤝", "ok")
        else:
            flash(f"{res['winner_name']}님이 더 가까웠어! 🎉", "ok")
        return redirect(url_for("bet_detail", bid=bet.id))

    @app.route("/calendar/bets/<int:bid>/end", methods=["POST"])
    @active_couple_required
    def bet_end(bid):
        """내기 종료(status→ended). 삭제 없이 기록은 남긴다."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        bet.status = "ended"
        bet.updated_at = datetime.utcnow()
        db.session.commit()
        flash("내기를 종료했어.", "ok")
        return redirect(url_for("bet_detail", bid=bet.id))

    @app.route("/calendar/bets/<int:bid>/delete", methods=["POST"])
    @active_couple_required
    def bet_delete(bid):
        """내기 삭제(체크인도 함께 정리). 목록으로 돌아간다."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        db.session.delete(bet)  # cascade로 BetCheckin도 삭제
        db.session.commit()
        flash("내기를 삭제했어.", "ok")
        return redirect(url_for("calendar_bets"))

    # ---- 오늘의 질문 대시보드 (예전 '/' 본문 — 온보딩 게이트 이후 부분) ----
    @app.route("/today")
    @active_couple_required
    def today():
        u = current_user()
        q = get_or_create_today_question(u.couple)
        partner = u.partner
        my_ans = q.answer_by(u.id)
        partner_ans = q.answer_by(partner.id) if partner else None
        revealed = q.both_answered
        thread, comment_count = build_comment_thread(q) if revealed else ([], 0)
        return render_template(
            "dashboard.html",
            question=q,
            my_ans=my_ans,
            partner_ans=partner_ans,
            partner=partner,
            revealed=revealed,
            today=q.q_date,
            thread=thread,
            comment_count=comment_count,
        )

    # ---- auth ----
    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if current_user():
            return redirect(url_for("index"))
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            display_name = (request.form.get("display_name") or "").strip()
            invite_code = (request.form.get("invite_code") or "").strip().upper()

            if not email or not password or not display_name:
                flash("이메일, 비밀번호, 이름을 모두 입력해줘.", "error")
                return render_template("signup.html")
            if len(password) < 8:
                flash("비밀번호는 8자 이상으로 해줘.", "error")
                return render_template("signup.html")
            if User.query.filter_by(email=email).first():
                flash("이미 가입된 이메일이야.", "error")
                return render_template("signup.html")

            couple, is_admin, status, err = _resolve_couple_for_join(invite_code)
            if err:
                flash(err, "error")
                return render_template("signup.html")
            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                display_name=display_name,
                couple_id=couple.id,
                is_admin=is_admin,
                status=status,
            )
            db.session.add(user)
            db.session.commit()
            session.clear()
            session["user_id"] = user.id
            session.permanent = True  # 새 커플은 로그인 상태 유지
            return redirect(url_for("index"))
        return render_template("signup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user():
            return redirect(url_for("index"))
        if request.method == "POST":
            ip = request.remote_addr or "unknown"
            if _rate_limited(ip):
                flash("로그인 시도가 너무 많아. 잠시 후 다시 시도해줘.", "error")
                return render_template("login.html"), 429
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            user = User.query.filter_by(email=email).first()
            if not user or not check_password_hash(user.password_hash, password):
                _record_attempt(ip)
                flash("이메일 또는 비밀번호가 맞지 않아.", "error")
                return render_template("login.html")
            session.clear()
            session["user_id"] = user.id
            # "로그인 유지" 체크 시 90일 지속 쿠키, 아니면 브라우저 세션 쿠키.
            session.permanent = bool(request.form.get("remember"))
            nxt = request.args.get("next")
            return redirect(nxt or url_for("index"))
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- Kakao social login ----
    def _kakao_redirect_uri() -> str:
        # Must exactly match the redirect URI registered in the Kakao console.
        # Force https: on Render we sit behind a TLS proxy, so _external alone
        # can yield http.
        return url_for("kakao_callback", _external=True, _scheme="https")

    def _kakao_start_authorize():
        # Build a fresh CSRF state, stash it, and hand off to Kakao's authorize
        # endpoint. Shared by both the login flow and the link-existing flow so
        # they stay in lockstep on state handling and redirect_uri.
        state = secrets.token_urlsafe(24)
        session["kakao_oauth_state"] = state
        params = {
            "client_id": KAKAO_REST_API_KEY,
            "redirect_uri": _kakao_redirect_uri(),
            "response_type": "code",
            "state": state,
        }
        return redirect(KAKAO_AUTHORIZE_URL + "?" + urlencode(params))

    @app.route("/auth/kakao/login")
    def kakao_login():
        if current_user():
            return redirect(url_for("index"))
        if not kakao_enabled():
            flash("카카오 로그인이 설정되지 않았어.", "error")
            return redirect(url_for("login"))
        # Plain login: make sure we're not carrying a stale link intent.
        session.pop("kakao_link_mode", None)
        return _kakao_start_authorize()

    @app.route("/auth/kakao/link")
    @login_required
    def kakao_link():
        """Link Kakao to the *currently logged-in* account (no new account)."""
        if not kakao_enabled():
            flash("카카오 로그인이 설정되지 않았어.", "error")
            return redirect(url_for("settings"))
        if current_user().kakao_id:
            flash("이미 카카오가 연결된 계정이야.", "error")
            return redirect(url_for("settings"))
        session["kakao_link_mode"] = True
        return _kakao_start_authorize()

    @app.route("/auth/kakao/callback")
    def kakao_callback():
        if not kakao_enabled():
            abort(404)
        # Verify state (CSRF protection). Pop so it can't be replayed.
        expected = session.pop("kakao_oauth_state", None)
        got = request.args.get("state")
        if not expected or not got or not secrets.compare_digest(expected, got):
            flash("카카오 로그인 검증에 실패했어. 다시 시도해줘.", "error")
            return redirect(url_for("login"))
        if request.args.get("error"):
            flash("카카오 로그인이 취소됐어.", "error")
            return redirect(url_for("login"))
        code = request.args.get("code")
        if not code:
            flash("카카오 인증 코드를 받지 못했어.", "error")
            return redirect(url_for("login"))

        # Exchange authorization code for an access token.
        token_data = {
            "grant_type": "authorization_code",
            "client_id": KAKAO_REST_API_KEY,
            "redirect_uri": _kakao_redirect_uri(),
            "code": code,
        }
        if KAKAO_CLIENT_SECRET:
            token_data["client_secret"] = KAKAO_CLIENT_SECRET
        try:
            tr = requests.post(KAKAO_TOKEN_URL, data=token_data, timeout=10)
            tr.raise_for_status()
            access_token = tr.json().get("access_token")
        except requests.RequestException:
            log.exception("kakao token exchange failed")  # never log token body
            access_token = None
        if not access_token:
            flash("카카오 인증에 실패했어. 다시 시도해줘.", "error")
            return redirect(url_for("login"))

        # Fetch the Kakao profile.
        try:
            ur = requests.get(
                KAKAO_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            ur.raise_for_status()
            profile = ur.json()
        except requests.RequestException:
            log.exception("kakao userinfo fetch failed")
            flash("카카오 프로필을 가져오지 못했어. 다시 시도해줘.", "error")
            return redirect(url_for("login"))

        kakao_id = profile.get("id")
        if kakao_id is None:
            flash("카카오 사용자 정보를 확인하지 못했어.", "error")
            return redirect(url_for("login"))
        kakao_id = str(kakao_id)  # stable identity
        account = profile.get("kakao_account") or {}
        kprofile = account.get("profile") or {}
        props = profile.get("properties") or {}
        nickname = (
            kprofile.get("nickname") or props.get("nickname") or "카카오 사용자"
        )[:60]
        # Optional profile photo. Newer consent exposes it under
        # kakao_account.profile.profile_image_url; older/simple scope puts it in
        # properties.profile_image. Absent is fine — never crash on it.
        profile_image_url = (
            kprofile.get("profile_image_url") or props.get("profile_image") or None
        )
        if profile_image_url:
            profile_image_url = str(profile_image_url)[:512]

        # ---- link-to-existing-account flow ------------------------------- #
        # A logged-in email user chose "connect Kakao" in settings. Attach this
        # Kakao identity to their EXISTING account instead of creating a new one.
        # Pop unconditionally so a stale flag can never leak into a later login.
        if session.pop("kakao_link_mode", False):
            me = current_user()
            if me is not None:
                if me.kakao_id:
                    flash("이미 카카오가 연결된 계정이야.", "error")
                    return redirect(url_for("settings"))
                other = User.query.filter_by(kakao_id=kakao_id).first()
                if other is not None and other.id != me.id:
                    # Never steal/merge — this Kakao already belongs elsewhere.
                    flash("이 카카오 계정은 이미 다른 계정에 연결돼 있어.", "error")
                    return redirect(url_for("settings"))
                me.kakao_id = kakao_id
                if profile_image_url:
                    me.profile_image_url = profile_image_url
                db.session.commit()
                flash(
                    "카카오 계정이 연결됐어! 이제 카카오로도 로그인할 수 있어.", "ok"
                )
                return redirect(url_for("settings"))
            # Link mode but somehow not logged in → fall through to normal login.

        # Existing Kakao user → just log in. Keep their photo current.
        user = User.query.filter_by(kakao_id=kakao_id).first()
        if user:
            if profile_image_url and user.profile_image_url != profile_image_url:
                user.profile_image_url = profile_image_url
                db.session.commit()
            session.clear()
            session["user_id"] = user.id
            session.permanent = True
            return redirect(url_for("index"))

        # New Kakao user → needs to choose/join a couple space next.
        session["pending_kakao_id"] = kakao_id
        session["pending_kakao_nickname"] = nickname
        session["pending_kakao_profile_image"] = profile_image_url
        return redirect(url_for("kakao_connect"))

    @app.route("/auth/kakao/connect", methods=["GET", "POST"])
    def kakao_connect():
        if current_user():
            return redirect(url_for("index"))
        kakao_id = session.get("pending_kakao_id")
        nickname = session.get("pending_kakao_nickname")
        profile_image_url = session.get("pending_kakao_profile_image")
        if not kakao_id:
            # No pending Kakao identity — start over.
            return redirect(url_for("login"))

        if request.method == "POST":
            # Guard against a duplicate that appeared between callback and submit.
            if User.query.filter_by(kakao_id=kakao_id).first():
                session.pop("pending_kakao_id", None)
                session.pop("pending_kakao_nickname", None)
                session.pop("pending_kakao_profile_image", None)
                flash("이미 연결된 카카오 계정이야. 다시 로그인해줘.", "error")
                return redirect(url_for("login"))

            display_name = (request.form.get("display_name") or nickname or "").strip()
            display_name = display_name[:60]
            invite_code = (request.form.get("invite_code") or "").strip().upper()
            if not display_name:
                flash("이름을 입력해줘.", "error")
                return render_template(
                    "kakao_connect.html", nickname=nickname, invite_code=invite_code
                )

            couple, is_admin, status, err = _resolve_couple_for_join(invite_code)
            if err:
                flash(err, "error")
                return render_template(
                    "kakao_connect.html", nickname=nickname, invite_code=invite_code
                )
            user = User(
                kakao_id=kakao_id,
                profile_image_url=profile_image_url,
                display_name=display_name,
                couple_id=couple.id,
                is_admin=is_admin,
                status=status,
            )
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("연결 중 문제가 생겼어. 다시 시도해줘.", "error")
                return redirect(url_for("login"))
            session.pop("pending_kakao_profile_image", None)

            session.clear()
            session["user_id"] = user.id
            session.permanent = True  # 새 커플은 로그인 상태 유지
            return redirect(url_for("index"))

        return render_template(
            "kakao_connect.html", nickname=nickname, invite_code=""
        )

    @app.route("/auth/kakao/unlink", methods=["POST"])
    @login_required
    def kakao_unlink():
        """Detach Kakao from the current account. Refuse if it would lock the
        user out (a Kakao-only account has no email/password to fall back on)."""
        u = current_user()
        if not u.kakao_id:
            return redirect(url_for("settings"))
        if not u.email:
            flash("카카오만 연결된 계정은 연결을 해제할 수 없어.", "error")
            return redirect(url_for("settings"))
        u.kakao_id = None
        db.session.commit()
        flash("카카오 연결을 해제했어.", "ok")
        return redirect(url_for("settings"))

    # ---- couple approval ----
    @app.route("/approve/<int:user_id>", methods=["POST"])
    @login_required
    def approve(user_id):
        u = current_user()
        if not u.is_admin:
            abort(403)
        partner = User.query.get_or_404(user_id)
        if partner.couple_id != u.couple_id or partner.status != "pending":
            abort(400)
        partner.status = "approved"
        db.session.commit()
        # Notify the just-approved partner (never the admin/actor).
        _safe_notify(
            partner,
            "approval",
            "커플 연결이 승인됐어! 🎉",
            url_for("index"),
        )
        flash(f"{partner.display_name}님과 연결됐어! 이제 함께 시작해봐.", "ok")
        return redirect(url_for("index"))

    # ---- daily answer ----
    @app.route("/answer", methods=["POST"])
    @active_couple_required
    def answer():
        u = current_user()
        text = (request.form.get("answer") or "").strip()
        if not text:
            flash("답변을 입력해줘.", "error")
            return redirect(url_for("today"))
        q = get_or_create_today_question(u.couple)
        existing = q.answer_by(u.id)
        is_new = existing is None  # only a first answer notifies (edits stay quiet)
        if existing:
            existing.text = text  # allow editing until reveal is fine; keep simple
        else:
            db.session.add(Answer(question_id=q.id, user_id=u.id, text=text))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            is_new = False
        if is_new:
            # Notify the partner (the recipient) — never the answering user.
            _safe_notify(
                u.partner,
                "answer",
                f"{u.display_name}님이 오늘 답을 남겼어 💌",
                url_for("today"),
            )
        # Freshen this month's cached insight in the BACKGROUND (never blocks the
        # response, never runs claude here). Only meaningful once there's a
        # partner to have a two-person conversation. Guarded against duplicates.
        if u.partner is not None:
            today = date.today()
            _kick_monthly_report(u.couple_id, today.year, today.month)
        return redirect(url_for("today"))

    # ---- comments on a day's revealed answers ----
    @app.route("/question/<int:qid>/comment", methods=["POST"])
    @active_couple_required
    def add_comment(qid):
        u = current_user()
        q = db.session.get(DailyQuestion, qid)
        # Must be this couple's question, and only after both answered (reveal).
        if q is None or q.couple_id != u.couple_id:
            abort(404)
        if not q.both_answered:
            abort(403)

        text = (request.form.get("text") or "").strip()[:1000]
        parent_id_raw = request.form.get("parent_id")
        parent = None
        if parent_id_raw:
            try:
                parent = db.session.get(Comment, int(parent_id_raw))
            except (TypeError, ValueError):
                parent = None
            # A reply's parent must be an existing comment on the SAME question.
            if parent is None or parent.question_id != q.id:
                abort(400)

        if not text:
            flash("댓글을 입력해줘.", "error")
            return _redirect_after_comment(q)

        db.session.add(
            Comment(
                question_id=q.id,
                author_id=u.id,
                parent_id=parent.id if parent else None,
                text=text,
            )
        )
        db.session.commit()
        # Notify the OTHER partner (the one who didn't write this comment).
        # Today's comments live on the dashboard; a past day's live on its
        # detail page (/question/<qid>) — mirror that split in the deep link.
        if q.q_date == date.today():
            link = url_for("today") + f"#c-q{q.id}"
        else:
            link = url_for("question_detail", qid=q.id) + f"#c-q{q.id}"
        _safe_notify(
            u.partner,
            "comment",
            f"{u.display_name}님이 댓글을 남겼어",
            link,
        )
        return _redirect_after_comment(q)

    def _redirect_after_comment(q):
        # Today is answered/commented on the dashboard; a past day returns to
        # its own detail page so the reader stays on the day they're reading.
        if q.q_date == date.today():
            return redirect(url_for("today") + f"#c-q{q.id}")
        return redirect(url_for("question_detail", qid=q.id) + f"#c-q{q.id}")

    # ---- single day detail ----
    @app.route("/question/<int:qid>")
    @active_couple_required
    def question_detail(qid):
        """A single day's question + both answers + comment thread.

        Same reveal rule as the dashboard: both answers (and the comment box)
        appear only once both partners have answered. Pure DB reads — never
        touches claude. 404 for a missing question or one that isn't this
        couple's, so a day can't be read across couples."""
        u = current_user()
        q = db.session.get(DailyQuestion, qid)
        if q is None or q.couple_id != u.couple_id:
            abort(404)
        partner = u.partner
        my_ans = q.answer_by(u.id)
        partner_ans = q.answer_by(partner.id) if partner else None
        revealed = q.both_answered
        thread, comment_count = build_comment_thread(q) if revealed else ([], 0)
        return render_template(
            "question_detail.html",
            question=q,
            my_ans=my_ans,
            partner_ans=partner_ans,
            partner=partner,
            revealed=revealed,
            thread=thread,
            comment_count=comment_count,
        )

    # ---- history ----
    @app.route("/history")
    @active_couple_required
    def history():
        """A clean, clickable list of past days. Each row links to the day's
        detail page (/question/<qid>); the full answers + comment thread live
        there, not inline here. Metadata (reveal state + comment count) is kept
        cheap — a COUNT, never the built thread."""
        u = current_user()
        partner = u.partner
        qs = (
            DailyQuestion.query.filter_by(couple_id=u.couple_id)
            .order_by(DailyQuestion.q_date.desc())
            .all()
        )
        items = []
        for q in qs:
            revealed = q.both_answered
            items.append(
                {
                    "id": q.id,
                    "date": q.q_date,
                    "question": q.text,
                    "revealed": revealed,
                    "answered_mine": q.answer_by(u.id) is not None,
                    "comment_count": q.comments.count() if revealed else 0,
                }
            )
        return render_template("history.html", items=items, partner=partner)

    # ---- 추억 (memories): photos stored in OneDrive ----
    _ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
    _MAX_PHOTO_BYTES = 50 * 1024 * 1024  # 50 MB (대용량 HEIC/JPEG 대응, OneDrive 단순 업로드 250MB 한참 아래)
    # EXIF 촬영일시를 찾기 위해 읽는 **머리 조각**. JPEG 의 APP1(Exif) 세그먼트는
    # SOI 바로 뒤라 수 KB 안이고, HEIC 의 meta 박스도 보통 파일 앞머리에 있다.
    # 256KB 면 넉넉하고(실패해도 Graph 메타가 받친다) 메모리는 사진 크기와 무관하다.
    _EXIF_HEAD_BYTES = 256 * 1024
    _MAX_UPLOAD_BATCH = 20               # 요청당 사진 수 안전 상한 (JS는 이제 1장씩 보냄)
    _EXT_CONTENT_TYPES = _EXT_CONTENT_TYPES_BY_EXT
    _content_type_for = content_type_for

    def _attachment_disposition(name):
        """Build a Content-Disposition: attachment header that survives non-ASCII
        (Korean) filenames. Provides an ASCII fallback plus RFC 5987 ``filename*``
        so browsers download with the user's original name."""
        from urllib.parse import quote

        safe = name or "photo.jpg"
        # ASCII fallback: strip quotes/backslashes and drop non-latin1 chars.
        ascii_name = safe.replace('"', "").replace("\\", "")
        ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii") or "photo.jpg"
        return (
            f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(safe)}"
        )

    @app.route("/memories")
    @active_couple_required
    def memories():
        """Gallery of the couple's photos (newest first) + an upload form.

        When OneDrive isn't configured we do NOT crash — we render a friendly
        "connect OneDrive" note and hide the upload form. Each thumbnail's
        ``<img src>`` points at our own ``/memories/<id>/image`` proxy route (see
        ``memory_image``), NOT at a raw OneDrive URL — personal-OneDrive download
        URLs expire almost immediately and would 401 in the browser. So this
        render does NO network I/O; it's a pure DB read.
        """
        u = current_user()
        # 데이트 '다녀왔어'에서 넘어오면 caption 프리필 힌트(행사 제목)를 받는다.
        compose_hint = (request.args.get("compose") or "").strip()[:1000]
        if not onedrive.onedrive_enabled():
            return render_template(
                "memories.html", enabled=False, reconnect=False, photos=[],
                compose_hint=compose_hint,
            )

        rows = (
            Photo.query.filter_by(couple_id=u.couple_id)
            .order_by(Photo.created_at.desc(), Photo.id.desc())
            .all()
        )
        photos = [
            {
                "id": p.id,
                "caption": p.caption,
                "tags": p.tags_list,
                "caption_status": p.caption_status,
                "original_name": p.original_name,
                "uploaded_by": p.uploaded_by,
                "created_at": p.created_at,
            }
            for p in rows
        ]
        # If anything is still being captioned, the template gently auto-reloads
        # so finished captions appear without a manual refresh.
        any_pending = any(p["caption_status"] == "pending" for p in photos)
        any_failed = any(p["caption_status"] == "failed" for p in photos)

        # Self-heal: re-spawn a caption thread for every photo stuck on 'pending'
        # (its original thread may have died/OOM'd/never ran). The _captioning
        # guard drops duplicates, and the semaphore serializes the work, so this
        # is safe even with several stuck photos.
        app_obj = current_app._get_current_object()
        for p in photos:
            if p["caption_status"] == "pending":
                _spawn_caption_if_idle(app_obj, p["id"])

        return render_template(
            "memories.html",
            enabled=True,
            reconnect=onedrive.reconnect_needed(),
            photos=photos,
            any_pending=any_pending,
            any_failed=any_failed,
            compose_hint=compose_hint,
        )

    @app.route("/memories/search")
    @active_couple_required
    def memories_search():
        """자연어 추억 검색 — 캡션·태그·원본파일명에 대한 즉시 키워드 매칭.

        캡션/태그는 이미 배경 캡셔너가 만든 자연어 AI 설명이라, 그 위를 검색하면
        요청 경로에서 claude를 호출하지 않고도(앱 절대 제약: 동기 AI 금지) 자연어
        검색 경험을 준다 — 순수 DB 읽기다. 커플 범위 밖 사진은 절대 조회하지 않는다.

        매칭: 질의를 공백 토큰화(소문자)하고, 각 토큰이 caption·tags·original_name
        중 하나의 부분문자열이면 해당 토큰이 매칭된 것으로 본다. 결과는 (1) 서로 다른
        매칭 토큰 수 내림차순, (2) 동률이면 created_at 내림차순으로 정렬한다.
        """
        u = current_user()
        q = (request.args.get("q") or "").strip()
        if not onedrive.onedrive_enabled():
            return render_template(
                "memories_search.html", enabled=False, q=q, photos=[]
            )

        tokens = [t for t in q.lower().split() if t]
        photos = []
        if tokens:
            rows = (
                Photo.query.filter_by(couple_id=u.couple_id)
                .order_by(Photo.created_at.desc(), Photo.id.desc())
                .all()
            )
            scored = []
            for p in rows:
                # 검색 대상 텍스트를 소문자로 모아 토큰별 부분문자열 매칭.
                hay = [(p.caption or "").lower(), (p.original_name or "").lower()]
                hay.extend(t.lower() for t in p.tags_list)
                matched = 0
                for tok in tokens:
                    if any(tok in h for h in hay):
                        matched += 1
                if matched:
                    scored.append((matched, p))
            # 매칭 토큰 수 내림차순, 동률은 created_at 내림차순(rows가 이미 최신순이라
            # 안정 정렬로 recency 타이브레이크가 유지된다).
            scored.sort(key=lambda sp: sp[0], reverse=True)
            photos = [
                {
                    "id": p.id,
                    "caption": p.caption,
                    "tags": p.tags_list,
                    "caption_status": p.caption_status,
                    "original_name": p.original_name,
                    "uploaded_by": p.uploaded_by,
                    "created_at": p.created_at,
                }
                for _, p in scored
            ]
        return render_template(
            "memories_search.html",
            enabled=True,
            reconnect=onedrive.reconnect_needed(),
            q=q,
            photos=photos,
        )

    @app.route("/memories/<int:photo_id>/image")
    @active_couple_required
    def memory_image(photo_id):
        """원본 사진 중계 — **바이트를 변수에 담지 않고 흘려보낸다.**

        브라우저는 OneDrive URL 을 절대 보지 않는다. 매번 신선한 링크로
        (``open_range`` → ``/content`` 302) 열어 **제너레이터로 중계**하므로,
        50MB 사진을 내보내도 워커 메모리는 64KB 대에서 평평하다. 예전엔 바이트를
        통째로 읽어 응답을 만들고 그걸 또 16MB 캐시에 담았다.

        캐시는 **브라우저가** 한다 — 이 URL 은 사진당 고정이고 ``Cache-Control:
        private, max-age=3600`` + ``ETag`` 가 붙으므로, 재방문은 304 로 끝난다
        (서버는 아무것도 들고 있지 않다). ⛔ **OneDrive URL 로 302 하지 않는 이유**는
        파일 머리말 §'왜 302 를 안 쓰는가' 참고 — 그 URL 은 발급마다 달라져서
        브라우저 캐시 키가 매번 깨지고(= 캐시가 아예 안 먹는다), 자격증명이 박혀 있다.

        HEIC/HEIF 는 사파리 말고는 ``<img>`` 로 못 여므로 표시 경로에서만 JPEG 로
        바꾼다 — 1순위는 Graph 렌디션(우리 CPU 0), 안 되면 Pillow(기존 동작).
        다운로드(``?download=1``)는 **원본 파일명·원본 바이트 그대로**다.
        404 unless the photo belongs to the requester's couple.
        """
        u = current_user()
        photo = db.session.get(Photo, photo_id)
        if photo is None or photo.couple_id != u.couple_id:
            abort(404)
        download = bool(request.args.get("download"))
        disposition = None
        if download:
            disposition = _attachment_disposition(
                photo.original_name or photo.filename or f"photo-{photo.id}.jpg"
            )
        # 조건부 요청 — 바뀌지 않았으면 바이트를 한 톨도 보내지 않는다(304).
        etag = _photo_etag(photo, download)
        if etag and request.headers.get("If-None-Match") == etag:
            resp = app.response_class(status=304)
            resp.headers["ETag"] = etag
            resp.headers["Cache-Control"] = "private, max-age=3600"
            return resp
        resp = _stream_original(
            photo, want_web_format=not download, disposition=disposition
        )
        resp.headers["Cache-Control"] = "private, max-age=3600"
        if etag:
            resp.headers["ETag"] = etag
        return resp

    @app.route("/blog-img/<int:photo_id>")
    def blog_img(photo_id):
        """공개(인증 불필요) 서명 이미지 — 네이버/독자가 로그인 없이 사진을 가져간다.

        ``@active_couple_required`` 없음(공개). ``?e=<exp>&t=<sig>``의 만료시각과
        HMAC 토큰을 ``hmac.compare_digest``로 대조하고(불일치/누락/만료 → 404),
        토큰이 곧 인가라 어느 커플의 Photo든 로드한다. 선택적 ``&c=x,y,w,h``(정규화
        크롭)도 서명에 포함되므로 변조 시 404다.

        ⛔ **등급(``&v=``)으로 소스를 가른다 — 원본을 아무 데나 내보내지 않는다.**

        | ``v``        | 무엇                         | 서버가 픽셀을 만지나 |
        |--------------|------------------------------|----------------------|
        | (없음)       | 미리보기·클립보드용 std:      | **예 — 여기 한 곳뿐** |
        |              | (크롭→) 긴 변 ≤1280 · q92     | 입력은 **렌디션**     |
        | ``orig``     | 원본 바이트 **그대로 중계**    | 아니오 (스트리밍)     |
        | ``r<N>``     | Graph 렌디션 **그대로 중계**   | 아니오 (중계)         |

        ``v=orig`` 는 **브라우저가 Canvas 로 발행용 크롭을 굽기 위한** 소스다 —
        크롭을 서버가 하지 않으므로 결과 화질은 *원본 픽셀 그대로*이고(다운스케일본에서
        자르는 것보다 낫다), 0.1 CPU 워커는 바이트를 흘려보내기만 한다. HEIC 원본은
        브라우저가 못 열므로 같은 해상도의 Graph 렌디션(JPEG)으로 대체하고, 그것도
        안 되면 그때만 Pillow 로 변환한다(기존 동작 — 화질 후퇴 없음).

        ``v`` 는 서명 대상이 아니다(``t``/``e``/``c`` 만 대조) — 어느 등급이든 **같은
        사진 한 장**을 공개하므로 노출 범위가 달라지지 않는다. 예전 ``&hq=1`` 이
        하던 일(원본 해상도 서버 크롭)은 ``v=orig`` + 브라우저 크롭으로 대체됐다.

        보안: 이 서명 URL은 (만료 전까지) 링크를 가진 누구에게나 그 사진 하나를
        공개로 노출한다 — 이 사진들은 공개 블로그에 게시되는 것이므로 허용된다.
        추측 불가한 유효 HMAC 토큰 + 미만료여야만 도달 가능하다. 만료 시에도 410이
        아닌 404를 써 존재 여부를 흘리지 않는다.
        """
        got = request.args.get("t") or ""
        try:
            exp = int(request.args.get("e") or "")
        except (ValueError, TypeError):
            abort(404)
        # 선택적 크롭 — 서명에 포함된다. URL에 붙인 원문 그대로 되읽어 서명 대조.
        crop_str = request.args.get("c") or None
        variant = (request.args.get("v") or "").strip().lower()
        # 등급까지 서명 대조한다 — std 토큰으로 원본을 꺼내지 못하게.
        if not got or not hmac.compare_digest(
            got, _blog_img_sig(photo_id, exp, crop_str, variant or None)
        ):
            abort(404)
        if exp <= int(time.time()):  # 만료 → 404(존재 누설 방지로 410 대신)
            abort(404)

        def _public(resp):
            # PUBLIC 캐시 — 공개로 가져가라고 만든 URL이므로 private가 아니라 public.
            # 네이버 유저스크립트가 cross-origin 으로 받아 Canvas 에 그리므로 ACAO 도.
            resp.headers["Cache-Control"] = "public, max-age=86400"
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        # ---- 중계 등급(v=orig / v=rN) — 서버는 바이트를 **열지 않는다** --------
        if variant == "orig" or variant.startswith("r"):
            photo = db.session.get(Photo, photo_id)
            if photo is None:
                abort(404)
            if variant != "orig":
                try:
                    edge = int(variant[1:] or 0)
                except ValueError:
                    edge = 0
                if edge <= 0:
                    abort(404)
                try:
                    got_r = onedrive.get_rendition(photo.onedrive_item_id, edge)
                except onedrive.OneDriveError:
                    got_r = None
                if got_r is None:  # 렌디션을 못 만들면 원본 중계로 조용히 내려간다
                    return _public(_stream_original(photo))
                return _public(app.response_class(got_r[0], mimetype=got_r[1]))
            return _public(_stream_original(photo, want_web_format=True))

        # ---- std 등급 — 이 앱에서 서버가 픽셀을 만지는 **유일한** 자리 ---------
        # 왜 여기만 남았나: 이 바이트는 네이버 스마트에디터에 **붙여넣는 HTML 의
        # `<img src>`** 가 가리킨다. 그 경로에는 브라우저가 끼어들 자리가 없어서
        # (네이버가 URL 을 그대로 가져간다) 잘린 그림이 URL 끝에 있어야 한다.
        # 대신 **입력을 렌디션으로 바꿔** 12MP 디코드를 없앴다 — 출력 픽셀은 동일.
        # 마지막 그물 — 이 라우트는 **어떤 경우에도 500 을 내지 않는다.**
        # 못 만들면 404(존재 누설 없는 실패)지, 에러 페이지가 아니다. 원인은
        # 로그에 스택으로 남는다(조용한 실패 금지).
        try:
            got_std = blog_std_bytes(photo_id, crop_str)
        except Exception:  # noqa: BLE001
            log.exception("blog-img: std 서빙 실패 photo=%s crop=%s",
                          photo_id, crop_str)
            got_std = None
        if got_std is None:
            abort(404)
        data, ctype = got_std
        return _public(app.response_class(data, mimetype=ctype))

    def _serve_preview(photo_id, tier):
        """**우리가 소유한** 미리보기 자산을 내준다 — 사진당 고정 URL.

        ⛔ 여기엔 서명도 만료도 없다. 로그인한 커플 구성원만 닿고(``@active_couple_
        required``), 그 사람은 어차피 그 사진의 주인이다. 예전 경로는 화면을 열
        때마다 Graph 를 두 번 왕복해 썸네일을 '다시 만들었다' — 그게 미리보기 딜레이의
        정체였다. 지금은 **DB 읽기 한 번**이다(자산 생성은 사진당 평생 한 번).

        캐시: 자산은 사진이 사라지기 전까지 **절대 바뀌지 않으므로** 불변 자산처럼
        다룬다(``immutable`` + 1년). 그래서 재방문은 네트워크 요청이 **아예 안 나간다.**
        혹시 나가더라도 ``ETag`` 가 있어 **304** 로 끝난다. 티어 크기를 바꿔 이미
        캐시된 브라우저까지 새 자산을 줘야 하면 ``PREVIEW_REV`` 를 올린다(URL 이 바뀐다).

        자산을 못 만들면(OneDrive 미연결·Graph 실패) 404 로 죽지 않고 **원본 중계로
        폴백**한다 — 화면이 비는 것보다 느린 게 낫다.
        """
        u = current_user()
        photo = db.session.get(Photo, photo_id)
        if photo is None or photo.couple_id != u.couple_id:
            abort(404)
        got = previews.ensure(photo, tier)
        if got is None:
            return redirect(url_for("memory_image", photo_id=photo.id))
        data, ctype, etag, _w, _h = got
        if not ctype or not ctype.startswith("image/"):
            ctype = "image/jpeg"
        if etag and request.headers.get("If-None-Match") == etag:
            resp = app.response_class(status=304)
        else:
            resp = app.response_class(data, mimetype=ctype)
        resp.headers["Cache-Control"] = _PREVIEW_CACHE_CONTROL
        if etag:
            resp.headers["ETag"] = etag
        return resp

    @app.route("/memories/<int:photo_id>/thumb")
    @active_couple_required
    def memory_thumb(photo_id):
        """갤러리 그리드 썸네일 — 우리가 소유한 'grid' 티어 자산(DB 읽기 한 번).

        예전엔 요청마다 ``onedrive.get_thumbnail`` 로 Graph 를 두 번 왕복했다(메타 +
        CDN). 그 결과는 프로세스 메모리 캐시에만 살아서 재시작·재배포마다 전부
        다시 받았다. 지금은 사진당 **한 번** 만들어 DB 에 보관한 것을 내준다.
        """
        return _serve_preview(photo_id, "grid")

    @app.route("/memories/<int:photo_id>/preview")
    @active_couple_required
    def memory_preview(photo_id):
        """크게 보는 미리보기 — 우리가 소유한 'view' 티어 자산(긴 변 1280).

        크롭 UI·썸네일 배경 미리보기·라이트박스가 쓴다. 예전엔 같은 그림을 **만료
        5분~24시간짜리 서명 URL**(``/blog-img?e=&t=&v=r1280``)로 내줬다 — 렌더마다
        토큰이 달라져 브라우저 캐시 키가 매번 깨졌고(= 캐시가 아예 안 먹었다), 매
        요청이 Graph 렌디션 왕복이었다. 로그인한 본인에게 자기 사진을 보여 주는 데
        만료는 필요 없다. 서명·만료는 **네이버 발행용 외부 노출 이미지**에만 남는다.
        """
        return _serve_preview(photo_id, "view")

    def _wants_json():
        """AJAX 업로더 여부 판정: JS는 사진 1장씩 fetch로 보내며 아래 신호 중
        하나를 준다(헤더 X-Requested-With: fetch · ?ajax=1 · Accept: json).
        일반 페이지 전송(폴백)에는 해당하지 않으므로 기존 flash+redirect를 탄다."""
        if request.args.get("ajax") == "1":
            return True
        if request.headers.get("X-Requested-With", "").lower() == "fetch":
            return True
        accept = request.headers.get("Accept", "")
        return "application/json" in accept and "text/html" not in accept

    # ---- 동영상 (노선 2 Step 5) — 보관은 OneDrive, 변환은 브라우저 -----------
    # 사진 경로와 **일부러 갈라 둔다**: 영상은 자동 캡션(vision)·EXIF·블로그 서명
    # URL·썸네일 프록시 중 어느 것도 타지 않는다. 0.1 CPU 워커에 50MB 영상을
    # 먹이는 길을 아예 만들지 않는 게 요점이다(models.Video 머리말).
    #
    # ⛔ 서버는 **영상 바이트를 변수에 담지 않는다.** 업로드는 청크 PUT
    #    (onedrive.upload_stream), 다운로드는 Range 스트리밍(아래 video_stream)이다.
    def _video_or_404(video_id):
        u = current_user()
        video = db.session.get(Video, video_id)
        if video is None or video.couple_id != u.couple_id:
            abort(404)
        return video

    @app.route("/videos/upload", methods=["POST"])
    @active_couple_required
    def video_upload():
        """동영상 1개를 OneDrive로 **스트리밍 업로드**하고 행을 남긴다 (AJAX 전용).

        사진 업로더와 달리 ``file.read()`` 를 하지 않는다 — werkzeug 가 큰 업로드를
        디스크로 스풀해 두므로 그 스트림을 3.2MiB 청크로 그대로 OneDrive 업로드
        세션에 흘린다. 워커 메모리는 영상 크기와 무관하게 평평하다.
        """
        u = current_user()
        if not onedrive.onedrive_enabled():
            return jsonify(ok=False, error="onedrive_disabled",
                           reason="OneDrive 연결이 필요해."), 400
        file = request.files.get("video")
        if not file or not file.filename:
            return jsonify(ok=False, error="empty", reason="영상을 선택해줘."), 400
        if not gifmaker.is_allowed_video(file.filename, file.mimetype or ""):
            return jsonify(ok=False, error="bad_type",
                           reason="동영상 파일만 올릴 수 있어."), 400

        # 총 바이트를 먼저 잰다 — Content-Range 의 분모라 **정확해야** 한다.
        stream = file.stream
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        if size <= 0:
            return jsonify(ok=False, error="empty", reason="빈 파일이야."), 400
        if size > gifmaker.MAX_VIDEO_BYTES:
            mb = gifmaker.MAX_VIDEO_BYTES // (1024 * 1024)
            return jsonify(ok=False, error="too_large",
                           reason=f"영상 1개는 {mb}MB 이하만 올릴 수 있어."), 400

        try:
            item_id, stored_name = onedrive.upload_stream(stream, size, file.filename)
        except onedrive.OneDriveError:
            log.exception("OneDrive video upload failed for couple %s", u.couple_id)
            return jsonify(ok=False, error="upload_failed",
                           reason="OneDrive 업로드에 실패했어."), 502

        video = Video(
            couple_id=u.couple_id,
            onedrive_item_id=item_id,
            filename=stored_name[:255],
            original_name=file.filename[:255],
            uploaded_by=u.id,
            size_bytes=size,
            content_type=(file.mimetype
                          or gifmaker.content_type_for(file.filename))[:100],
        )
        db.session.add(video)
        db.session.commit()
        return jsonify(ok=True, video={
            "id": video.id,
            "name": video.original_name or video.filename,
            "size": video.size_bytes,
        })

    @app.route("/videos/<int:video_id>/delete", methods=["POST"])
    @active_couple_required
    def video_delete(video_id):
        video = _video_or_404(video_id)
        try:
            onedrive.delete_photo(video.onedrive_item_id)  # 같은 drive-item 삭제 API
        except onedrive.OneDriveError:
            log.exception("OneDrive video delete failed for video %s", video.id)
        db.session.delete(video)
        db.session.commit()
        return jsonify(ok=True)

    # 스트리밍 청크. 작게 잡아야 ``0.1 CPU · 512MB`` 에서 메모리가 평평하다.
    _VIDEO_CHUNK = 256 * 1024

    @app.route("/videos/<int:video_id>/stream")
    @active_couple_required
    def video_stream(video_id):
        """영상 바이트를 **Range(206) 지원**으로 흘린다 — 경로 (b).

        ``<video>`` 의 seek 은 Range 없이는 동작하지 않는다(서버가 200 으로 전체만
        주면 브라우저는 구간 지정을 포기하거나 전체를 받는다). 이 앱의 이미지
        프록시에는 Range 처리가 **없었고**(206 응답 0건), 영상은 그걸 그대로 쓸 수
        없어서 여기서 새로 연다.

        Range 헤더는 **그대로 OneDrive 로 전달**하고 돌아온 206 을 그대로 중계한다 —
        서버가 구간을 직접 잘라내지 않으므로 파싱 버그로 엉뚱한 바이트를 줄 일이 없고,
        전체를 받아 쪼갤 필요도 없다. 응답은 제너레이터라 **한 번에 256KB만** 메모리에
        있다.

        이 라우트는 same-origin 이다 — 그래서 ``<video>`` 를 Canvas 에 그려도
        tainted 가 되지 않고 ``getImageData`` 가 된다(그게 GIF 의 전제다).
        """
        video = _video_or_404(video_id)
        range_header = request.headers.get("Range")
        try:
            status, headers, upstream = onedrive.open_range(
                video.onedrive_item_id, range_header
            )
        except onedrive.OneDriveError:
            log.exception("could not open video stream for video %s", video.id)
            abort(502)
        if upstream is None:
            abort(404)  # OneDrive 에서 사라짐

        def _pump():
            try:
                for chunk in upstream.iter_content(chunk_size=_VIDEO_CHUNK):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()   # 끊긴 연결에서도 소켓을 반드시 거둔다

        ctype = (video.content_type
                 or gifmaker.content_type_for(video.original_name or video.filename))
        resp = app.response_class(_pump(), status=status, mimetype=ctype)
        # Range 중계에 필요한 헤더만 통과시킨다(그 외 OneDrive 헤더는 흘리지 않는다).
        for h in ("Content-Range", "Content-Length"):
            if headers.get(h):
                resp.headers[h] = headers[h]
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    @app.route("/videos/<int:video_id>/source")
    @active_couple_required
    def video_source(video_id):
        """브라우저가 **어디서** 영상 바이트를 받을지 — (a) 직접 / (b) 프록시.

        후보 (a)는 OneDrive 다운로드 URL을 브라우저에 넘겨 Microsoft CDN 에서 바로
        받게 하는 것이다(서버 대역폭 0). 되기만 하면 (a)가 낫다. **단 Canvas 로
        픽셀을 읽으려면 CDN 이 ``Access-Control-Allow-Origin`` 을 줘야 한다** —
        안 주면 캔버스가 오염돼 프레임 추출이 *아예* 막힌다.

        그래서 여기서 **주장하지 않고 잰다**: ``onedrive.probe_direct_cors`` 가 실제로
        ``Origin`` 을 붙여 1바이트를 받아 보고, ACAO 가 있으면 (a), 없으면 (b)를
        고른다. 결과는 행에 캐시해 영상당 한 번만 잰다.

        ⚠️ ACAO 가 없으면 **다운로드 URL을 클라이언트에 내보내지 않는다** — 그 URL은
        쿼리에 토큰이 박힌 자격증명이고, 쓰지도 못할 URL을 DOM 에 흘릴 이유가 없다.
        """
        video = _video_or_404(video_id)
        probe = video.cors_probe_data
        if probe is None:
            probe = onedrive.probe_direct_cors(
                video.onedrive_item_id, request.host_url.rstrip("/")
            ) or {"ok": False, "acao": None, "range": False, "status": 0,
                  "error": "probe-failed"}
            video.cors_probe = json.dumps(probe, ensure_ascii=False)
            db.session.commit()
        if probe.get("ok") and probe.get("range"):
            try:
                url = onedrive.get_download_url(video.onedrive_item_id)
            except onedrive.OneDriveError:
                url = None
            if url:
                return jsonify(ok=True, mode="direct", url=url, probe=probe,
                               size=video.size_bytes)
        return jsonify(
            ok=True, mode="proxy", probe=probe, size=video.size_bytes,
            url=url_for("video_stream", video_id=video.id),
        )

    @app.route("/memories/upload", methods=["POST"])
    @active_couple_required
    def memories_upload():
        u = current_user()
        ajax = _wants_json()
        if not onedrive.onedrive_enabled():
            if ajax:
                return jsonify(ok=False, error="onedrive_disabled",
                               reason="OneDrive 연결이 필요해."), 400
            flash("OneDrive 연결이 필요해.", "error")
            return redirect(url_for("memories"))

        # Bulk-capable: the form posts name="photos" (multiple). Fall back to the
        # legacy single name="photo" so an old cached form still works. The AJAX
        # uploader sends exactly one file per request but reuses the same field.
        files = request.files.getlist("photos") or request.files.getlist("photo")
        files = [f for f in files if f and f.filename]
        if not files:
            if ajax:
                return jsonify(ok=False, error="empty",
                               reason="사진을 선택해줘."), 400
            flash("사진을 선택해줘.", "error")
            return redirect(url_for("memories"))
        if len(files) > _MAX_UPLOAD_BATCH:
            if ajax:
                return jsonify(ok=False, error="too_many",
                               reason=f"한 번에 최대 {_MAX_UPLOAD_BATCH}장까지 올릴 수 있어."), 400
            flash(f"한 번에 최대 {_MAX_UPLOAD_BATCH}장까지 올릴 수 있어.", "error")
            return redirect(url_for("memories"))

        # A manually typed caption only makes sense for a single photo; when a
        # batch is uploaded we ignore it and auto-caption each one instead.
        manual_caption = (request.form.get("caption") or "").strip()[:1000] or None
        if len(files) > 1:
            manual_caption = None

        saved_ids = []          # rows that need background captioning
        saved_takens = []       # saved_ids와 짝: 각 사진의 taken_at(EXIF, 없으면 None)
        warm_ids = []           # 미리보기 자산을 미리 만들어 둘 사진(캡션 유무 무관)
        saved = 0
        skipped = 0             # invalid / empty / too-big files
        failed = 0              # OneDrive upload errors
        reason = None           # 마지막 스킵/실패 사유 (ajax 리포트용)
        last_name = files[-1].filename if files else None
        for file in files:
            # Validate it's an image by extension AND declared content-type.
            ext = os.path.splitext(file.filename)[1].lower()
            ctype = (file.mimetype or "").lower()
            if ext not in _ALLOWED_IMAGE_EXTS and not ctype.startswith("image/"):
                skipped += 1
                reason = "이미지 파일만 올릴 수 있어."
                continue
            # ⛔ **바이트를 통째로 읽지 않는다.** 예전엔 `file.read()` 로 최대 50MB 를
            #    파이썬 힙에 올렸다(한 번에 여러 장이면 그만큼 곱절). werkzeug 가 큰
            #    업로드를 **디스크로 스풀**해 두므로, 그 스트림을 그대로 OneDrive 로
            #    흘리면 워커 메모리는 사진 크기와 무관하게 평평하다 — 영상 업로드가
            #    이미 쓰는 길이다(onedrive.upload_stream).
            stream = file.stream
            try:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(0)
            except (OSError, ValueError):
                size = -1
            if size == 0:
                skipped += 1
                reason = "빈 파일이야."
                continue
            if size > _MAX_PHOTO_BYTES:
                skipped += 1
                reason = "사진 1장은 50MB 이하만 올릴 수 있어."
                continue
            # EXIF 촬영일시(DateTimeOriginal; HEIC 포함)는 **앞부분 몇십 KB** 안에
            # 있다(JPEG 는 SOI 바로 뒤 APP1, HEIC 는 meta 박스). 헤더 조각만 읽어
            # 뽑고, 거기서 못 찾으면 **업로드 뒤 Graph 가 서버에서 읽어 둔 값**
            # (photo.takenDateTime)으로 메운다 — 바이트를 다시 읽지 않는다.
            # 어떤 실패도 업로드를 깨면 안 되므로 방어적으로 감싼다.
            taken = None
            try:
                head = stream.read(_EXIF_HEAD_BYTES)
                stream.seek(0)
                taken = exifutil.extract_taken_at(head)
            except Exception:  # noqa: BLE001 - EXIF 추출은 best-effort
                taken = None
                try:
                    stream.seek(0)
                except (OSError, ValueError):
                    pass
            try:
                item_id, stored_name = onedrive.upload_photo_stream(
                    stream, size, file.filename
                )
            except onedrive.OneDriveError:
                log.exception("OneDrive upload failed for couple %s", u.couple_id)
                failed += 1
                reason = "OneDrive 업로드에 실패했어."
                continue
            if taken is None:
                # 헤더 조각에서 못 찾았다 — Graph 가 이미 EXIF 를 읽어 뒀는지 본다
                # (메타데이터 1KB, 픽셀 0바이트). 예전 구현은 여기서 포기했는데,
                # 이쪽이 오히려 **더 많이** 채운다.
                try:
                    meta = onedrive.get_item_meta(item_id)
                    taken = (meta or {}).get("taken_at")
                except Exception:  # noqa: BLE001 - best-effort
                    taken = None

            # A manual caption is treated as final ('ready'); otherwise the row
            # starts 'pending' and a background thread captions it via claude.
            caption = manual_caption
            photo = Photo(
                couple_id=u.couple_id,
                onedrive_item_id=item_id,
                filename=stored_name[:255],           # real name stored in OneDrive
                original_name=file.filename[:255],    # the user's original filename
                uploaded_by=u.id,
                caption=caption,
                caption_status="ready" if caption else "pending",
                taken_at=taken,  # EXIF 촬영일(없으면 None → 캘린더는 created_at 폴백)
            )
            db.session.add(photo)
            # Commit per photo so one later failure can't lose earlier successes.
            db.session.commit()
            saved += 1
            # 미리보기 자산을 **지금** 만들어 둔다 — 갤러리를 처음 열 때 Graph 를
            # 기다리지 않게. 업로드 응답은 이걸 기다리지 않는다(배경 스레드).
            warm_ids.append(photo.id)
            if caption is None:
                saved_ids.append(photo.id)
                saved_takens.append(taken)

        # 미리보기 자산을 **업로드 직후** 만들어 둔다(배경). 갤러리를 처음 열 때
        # Graph 를 기다리는 일이 아예 없어진다 — claude 와 무관한 값싼 HTTP 한 번이라
        # _CAPTION_SEM 뒤에 줄 서지 않는다.
        _spawn_warm_previews(current_app._get_current_object(), warm_ids)

        # Fire-and-forget auto-captioning for each new photo. The HTTP response
        # returns after the OneDrive PUTs + DB inserts — it NEVER waits on the
        # slow vision pass (CLAUDE.md: no synchronous AI on the request path).
        for pid in saved_ids:
            _spawn_caption_if_idle(current_app._get_current_object(), pid)

        # AJAX 업로더(사진 1장씩)에는 JSON으로 결과만 돌려준다 — redirect 없음.
        # 클라이언트가 진행률/실패 파일명을 직접 표시하고, 전부 끝난 뒤 페이지를
        # 새로고침해 새 그리드+flash를 보여준다.
        if ajax:
            # 후기 작성 폼의 갤러리 업로더(1장/요청)를 위해 방금 만든 Photo의 id와
            # 촬영일(taken_at)을 추가로 돌려준다 — 클라이언트가 촬영일 순으로 정렬해
            # 선택 스트립에 담는다. 기존 필드는 그대로라 추억 페이지 업로더는 무영향.
            new_photo_id = saved_ids[0] if saved_ids else None
            new_taken = saved_takens[0] if saved_takens else None
            return jsonify(
                ok=(saved > 0),
                saved=saved,
                skipped=skipped,
                failed=failed,
                filename=last_name,
                reason=reason,
                photo_id=new_photo_id,
                taken_at=(new_taken.isoformat() if new_taken else None),
            )

        # One honest flash summarizing the batch outcome.
        if saved and not (skipped or failed):
            flash(
                f"추억 {saved}장을 저장했어! 💖" if saved > 1 else "추억을 저장했어! 💖",
                "ok",
            )
        elif saved:
            flash(f"{saved}장 저장했어. {skipped + failed}장은 올리지 못했어.", "ok")
        elif failed and onedrive.reconnect_needed():
            flash("OneDrive 재연결이 필요해. 잠시 후 다시 시도해줘.", "error")
        elif failed:
            flash("사진 업로드에 실패했어. 잠시 후 다시 시도해줘.", "error")
        else:
            flash("이미지 파일만 50MB 이하로 올릴 수 있어.", "error")
        return redirect(url_for("memories"))

    @app.route("/memories/recaption", methods=["POST"])
    @active_couple_required
    def memories_recaption():
        """Retry captioning for THIS couple's photos that failed: reset their
        'failed' status back to 'pending', then (guarded) spawn a background
        caption thread for every 'pending' photo. Safe now that captioning is
        serialized — the photos queue up and caption one at a time."""
        u = current_user()
        # 이 커플의 실패한 사진만 다시 'pending'으로 되돌린다(커플 범위 밖은 절대 건드리지 않음).
        failed = Photo.query.filter_by(
            couple_id=u.couple_id, caption_status="failed"
        ).all()
        for p in failed:
            p.caption_status = "pending"
        db.session.commit()

        # 이 커플의 모든 'pending' 사진에 대해 배경 캡셔너를 (중복 방지 가드로) 띄운다.
        pending = Photo.query.filter_by(
            couple_id=u.couple_id, caption_status="pending"
        ).all()
        app_obj = current_app._get_current_object()
        for p in pending:
            _spawn_caption_if_idle(app_obj, p.id)

        flash("안 된 사진들을 다시 분석할게. 잠시 뒤 새로고침해줘.", "ok")
        return redirect(url_for("memories"))

    @app.route("/memories/<int:photo_id>/delete", methods=["POST"])
    @active_couple_required
    def memories_delete(photo_id):
        u = current_user()
        photo = db.session.get(Photo, photo_id)
        # Only this couple's members may delete this couple's photo.
        if photo is None or photo.couple_id != u.couple_id:
            abort(404)
        try:
            onedrive.delete_photo(photo.onedrive_item_id)
        except onedrive.OneDriveError:
            # Remote delete failed — keep the row so we can retry; don't orphan.
            log.exception("OneDrive delete failed for photo %s", photo.id)
            flash("사진 삭제에 실패했어. 잠시 후 다시 시도해줘.", "error")
            return redirect(url_for("memories"))
        # 원본이 사라지면 그 사진의 미리보기 자산도 같이 거둔다(FK 가 막기 전에).
        previews.forget(photo.id)
        db.session.delete(photo)
        db.session.commit()
        flash("사진을 삭제했어.", "ok")
        return redirect(url_for("memories"))

    # ---- 판사 (AI judge for couple fights): 사건 + 진술 + 백그라운드 판결 ----
    def _get_case_or_404(u, case_id):
        """Load a case, 404 unless it belongs to the requester's couple, so a
        case can never be read/acted on across couples."""
        case = db.session.get(Case, case_id)
        if case is None or case.couple_id != u.couple_id:
            abort(404)
        return case

    @app.route("/cases")
    @active_couple_required
    def cases():
        """List the couple's 사건 (newest first). Pure DB read — never claude."""
        u = current_user()
        rows = (
            Case.query.filter_by(couple_id=u.couple_id)
            .order_by(Case.created_at.desc(), Case.id.desc())
            .all()
        )
        items = []
        for c in rows:
            v = c.verdict
            items.append(
                {
                    "id": c.id,
                    "title": c.title,
                    "situation": c.situation,
                    "status": c.status,
                    "created_at": c.created_at,
                    "summary": (v.get("summary") if v else "") or "",
                    "fault": (v.get("fault") if v else None),
                }
            )
        return render_template("cases.html", items=items)

    @app.route("/cases/new", methods=["GET", "POST"])
    @active_couple_required
    def case_new():
        u = current_user()
        if request.method == "POST":
            situation = (request.form.get("situation") or "").strip()
            title = (request.form.get("title") or "").strip()[:120] or None
            statement = (request.form.get("statement") or "").strip()
            if not situation:
                flash("무슨 일이 있었는지 상황을 먼저 적어줘.", "error")
                return render_template("case_new.html")
            case = Case(
                couple_id=u.couple_id,
                created_by=u.id,
                title=title,
                situation=situation,
                status="open",
            )
            db.session.add(case)
            db.session.flush()  # get case.id for the optional statement
            if statement:
                db.session.add(
                    CaseStatement(case_id=case.id, user_id=u.id, text=statement)
                )
            db.session.commit()
            return redirect(url_for("case_detail", case_id=case.id))
        return render_template("case_new.html")

    @app.route("/cases/<int:case_id>")
    @active_couple_required
    def case_detail(case_id):
        """A single 사건: situation + both partners' statements + the verdict.

        Pure DB read — the slow claude judge NEVER runs here; it runs only in the
        background ``judge_case`` worker. 404 for a missing case or one that isn't
        this couple's, so a case can't be read across couples."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        partner = u.partner
        my_st = CaseStatement.query.filter_by(
            case_id=case.id, user_id=u.id
        ).first()
        partner_st = (
            CaseStatement.query.filter_by(case_id=case.id, user_id=partner.id).first()
            if partner is not None
            else None
        )
        # Render exactly the two couple members' sides (me first). A missing side
        # shows a gentle "아직 진술을 남기지 않았어" placeholder in the template.
        sides = [{"user": u, "statement": my_st, "is_me": True}]
        if partner is not None:
            sides.append({"user": partner, "statement": partner_st, "is_me": False})
        # 셀프힐: 'judging'인데 판결 스레드가 사라졌으면(프로세스 재시작) 되살린다.
        judging_resumed = resume_judging_if_orphaned(
            current_app._get_current_object(), case
        )
        return render_template(
            "case_detail.html",
            case=case,
            verdict=case.verdict,
            sides=sides,
            my_statement=my_st,
            has_statement=len(case.statements) >= 1,
            resolved=(case.status == "resolved"),
            judging_state=case_judging_verdict(case),
            judging_resumed=judging_resumed,
        )

    @app.route("/cases/<int:case_id>/statement", methods=["POST"])
    @active_couple_required
    def case_statement(case_id):
        """Upsert MY 진술 for this case (each partner has at most one).

        If a verdict already exists we do NOT auto-clear it — the user re-judges
        manually with the '다시 판결 맡기기' button."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        if case.status == "resolved":
            flash("이미 종결된 사건이라 진술을 바꿀 수 없어.", "error")
            return redirect(url_for("case_detail", case_id=case.id))
        text = (request.form.get("text") or "").strip()
        if not text:
            flash("진술 내용을 입력해줘.", "error")
            return redirect(url_for("case_detail", case_id=case.id))
        st = CaseStatement.query.filter_by(case_id=case.id, user_id=u.id).first()
        if st:
            st.text = text
            st.updated_at = datetime.utcnow()
        else:
            db.session.add(
                CaseStatement(case_id=case.id, user_id=u.id, text=text)
            )
        try:
            db.session.commit()
        except IntegrityError:
            # A concurrent submit created my statement first — take theirs, update.
            db.session.rollback()
            st = CaseStatement.query.filter_by(
                case_id=case.id, user_id=u.id
            ).first()
            if st:
                st.text = text
                st.updated_at = datetime.utcnow()
                db.session.commit()
        return redirect(url_for("case_detail", case_id=case.id))

    @app.route("/cases/<int:case_id>/judge", methods=["POST"])
    @active_couple_required
    def case_judge(case_id):
        """Kick off the BACKGROUND AI judgment for this case.

        Requires ≥1 statement. Marks the case 'judging', commits so the detail
        page shows the '판결 중' state (+ auto-refresh), then spawns the daemon
        thread. The slow claude pass runs ONLY in ``judge_case`` — never here."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        if case.status == "resolved":
            flash("이미 화해로 종결된 사건이야. 다시 열면 판결을 새로 맡길 수 있어.", "ok")
            return redirect(url_for("case_detail", case_id=case.id))
        if len(case.statements) < 1:
            flash("먼저 진술을 남겨줘.", "error")
            return redirect(url_for("case_detail", case_id=case.id))

        case.status = "judging"
        case.updated_at = datetime.utcnow()
        db.session.commit()

        # In-process guard so a rapid double-tap can't spawn two judge threads
        # for the same case (single-worker Render free tier). The thread frees
        # the key in its finally.
        _spawn_judge_if_idle(current_app._get_current_object(), case.id)
        return redirect(url_for("case_detail", case_id=case.id))

    @app.route("/cases/<int:case_id>/delete", methods=["POST"])
    @active_couple_required
    def case_delete(case_id):
        """Delete a case (cascade removes its statements). Any couple member may
        delete their couple's case."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        db.session.delete(case)
        db.session.commit()
        flash("사건을 삭제했어.", "ok")
        return redirect(url_for("cases"))

    @app.route("/cases/<int:case_id>/resolve", methods=["POST"])
    @active_couple_required
    def case_resolve(case_id):
        """화해 완료 — 판결이 난('decided') 사건을 'resolved'로 종결한다.

        새 DB 컬럼/마이그레이션 없이 기존 ``status`` 문자열만 바꾼다. 종결되면
        진술 수정·재판결이 잠긴다(case_statement/case_judge 가드)."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        if case.status != "decided":
            flash("판결이 난 사건만 화해 완료로 종결할 수 있어.", "error")
            return redirect(url_for("case_detail", case_id=case.id))
        case.status = "resolved"
        case.updated_at = datetime.utcnow()
        db.session.commit()
        flash("화해 완료! 🎉 이 사건은 종결됐어.", "ok")
        return redirect(url_for("case_detail", case_id=case.id))

    @app.route("/cases/<int:case_id>/reopen", methods=["POST"])
    @active_couple_required
    def case_reopen(case_id):
        """실수로 종결한 경우를 위한 안전장치 — 'resolved'를 'decided'로 되돌린다."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        if case.status == "resolved":
            case.status = "decided"
            case.updated_at = datetime.utcnow()
            db.session.commit()
        return redirect(url_for("case_detail", case_id=case.id))

    # ---- monthly insight ----
    @app.route("/insight")
    @active_couple_required
    def insight():
        u = current_user()
        partner = u.partner
        today = date.today()
        try:
            year = int(request.args.get("year", today.year))
            month = int(request.args.get("month", today.month))
            if not (1 <= month <= 12):
                raise ValueError
        except (TypeError, ValueError):
            year, month = today.year, today.month

        # Solo space: the couple has only one approved member (partner hasn't
        # joined/been approved yet). Never call the stats/qualitative functions
        # with a None partner — show a gentle empty state instead.
        if partner is None:
            return render_template(
                "insight.html",
                no_partner=True,
                stats=None,
                qualitative=None,
                has_data=False,
                year=year,
                month=month,
            )

        # Quantitative stats stay LIVE — a cheap DB query, never cached.
        stats = insights.compute_monthly_stats(u.couple_id, year, month, u, partner)
        # "has data" == at least one answer exists this month (derived from the
        # stats we already computed, so we avoid a second DB pass on the request).
        has_data = bool(
            stats["participation_a"]["days"] or stats["participation_b"]["days"]
        )

        # Qualitative comes ONLY from the cached MonthlyReport — the slow
        # `claude -p` call never runs on this request path.
        #
        # TRUE stale-while-revalidate for the DISPLAY: whether we show content
        # depends on whether the row HAS content (generated_at set), NOT on the
        # status. So while a background refresh runs (status=='generating') the
        # PREVIOUS cached content keeps showing — we never drop a populated
        # report back to the placeholder. The regen path only swaps the content
        # once the new report is ready (it never clears content when it marks
        # the row 'generating').
        report = MonthlyReport.query.filter_by(
            couple_id=u.couple_id, year=year, month=month
        ).first()
        has_content = report is not None and report.generated_at is not None
        qualitative = None
        # 새 구조화 리포트(report_json)가 있으면 그걸로 리치 카드를 렌더한다.
        # 없으면(과거 캐시된 리포트) 아래 레거시 qualitative로 폴백한다.
        report_data = report.report if has_content else None
        if has_content:
            qualitative = {
                "summary": report.summary or "",
                "themes": report.themes_list,
                "tone": report.tone or "",
                "divergent_question": report.divergent_question or "",
                "fun": report.fun or "",
            }

        # A background refresh is in flight for this month.
        refreshing = report is not None and report.status == "generating"
        # Placeholder ONLY when there's data but no content has ever been built
        # for this month (first-ever generation). If content exists, we show it.
        pending = has_data and not has_content

        # Trigger a background build when there's data but no cached content yet
        # (missing / failed-with-no-content). When content already exists we do
        # NOT kick from here — answer events keep it fresh — and the in-flight
        # guard in _kick_monthly_report would no-op anyway.
        if has_data and not has_content:
            if report is None or report.status in ("generating", "failed"):
                _kick_monthly_report(u.couple_id, year, month)

        # Auto-reload while anything is being (re)built so the display swaps to
        # the fresh content when the background thread finishes: either building
        # the first report (pending) or refreshing existing content (refreshing).
        auto_refresh = pending or (has_content and refreshing)

        # previous / next month links
        prev_m = (month - 1) or 12
        prev_y = year - 1 if month == 1 else year
        next_m = 1 if month == 12 else month + 1
        next_y = year + 1 if month == 12 else year
        return render_template(
            "insight.html",
            stats=stats,
            qualitative=qualitative,
            report=report_data,
            has_data=has_data,
            pending=pending,
            refreshing=refreshing,
            auto_refresh=auto_refresh,
            year=year,
            month=month,
            prev=(prev_y, prev_m),
            next=(next_y, next_m),
        )

    # ---- settings ----
    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        u = current_user()
        if request.method == "POST":
            # 네이버 업로더 유저스크립트 키 재발급(선택).
            if request.form.get("rotate_export_key"):
                u.export_key = None
                db.session.commit()
                _ensure_export_key(u)
                flash("네이버 업로더 키를 재발급했어. 유저스크립트에 다시 붙여넣어줘.", "ok")
                return redirect(url_for("settings"))
            # --- 네이버 API HUB 키(키워드 조사용) — 사용자별 -------------------
            # 값은 **받기만 한다.** 어떤 경로로도 다시 렌더하지 않고, flash·로그에도
            # 넣지 않는다(화면엔 설정됨/미설정만 보인다).
            if request.form.get("naver_keys_clear"):
                u.naver_api_key_id = None
                u.naver_api_key = None
                db.session.commit()
                flash("네이버 API 키를 지웠어. 키워드 조사는 꺼져.", "ok")
                return redirect(url_for("settings"))
            if request.form.get("naver_keys_test"):
                if not u.has_naver_api_keys:
                    flash("먼저 키를 저장해줘.", "error")
                    return redirect(url_for("settings"))
                cid, secret = u.naver_api_credentials
                ok, msg = naver_api.verify_credentials(cid, secret)
                flash(msg, "ok" if ok else "error")
                return redirect(url_for("settings"))
            if request.form.get("naver_keys_save"):
                key_id = (request.form.get("naver_api_key_id") or "").strip()[:200]
                secret = (request.form.get("naver_api_key") or "").strip()[:400]
                # 둘 중 하나만 비면 '그 칸은 그대로 두겠다'는 뜻으로 읽는다 — 폼이
                # 기존 값을 다시 안 그리므로, 하나만 바꾸려는 사람이 나머지를
                # 지우는 사고를 막는다.
                if key_id:
                    u.naver_api_key_id = key_id
                if secret:
                    u.naver_api_key = secret
                if not key_id and not secret:
                    flash("Client ID와 Secret을 입력해줘.", "error")
                    return redirect(url_for("settings"))
                db.session.commit()
                if u.has_naver_api_keys:
                    flash("네이버 API 키를 저장했어. '연결 확인'으로 점검해봐.", "ok")
                else:
                    flash("한 칸이 아직 비어 있어 — 키워드 조사는 두 값이 다 있어야 켜져.",
                          "error")
                return redirect(url_for("settings"))

            new_name = (request.form.get("app_name") or "").strip()
            new_display = (request.form.get("display_name") or "").strip()
            if new_name:
                Setting.set("app_name", new_name[:60])
            if new_display:
                u.display_name = new_display[:60]
            db.session.commit()
            flash("설정을 저장했어.", "ok")
            return redirect(url_for("settings"))
        # 네이버 업로더(유저스크립트)용 개인 키 — 최초 표시 시 생성. 템플릿은 me.export_key.
        _ensure_export_key(u)
        return render_template(
            "settings.html",
            current_app_name=Setting.get("app_name", DEFAULT_APP_NAME),
            # 값이 아니라 **불리언만** 넘긴다 — 템플릿이 키를 다시 그릴 방법 자체를
            # 안 갖게 한다.
            naver_keys_set=u.has_naver_api_keys,
        )

    # ---- in-app notifications ----
    @app.route("/notifications")
    @login_required
    def notifications():
        u = current_user()
        items = (
            Notification.query.filter_by(user_id=u.id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .all()
        )
        # Viewing the page marks everything read so the bell dot clears. Keep
        # the just-read ids so this render can still highlight them subtly.
        fresh_ids = {n.id for n in items if not n.is_read}
        if fresh_ids:
            for n in items:
                if n.id in fresh_ids:
                    n.is_read = True
            db.session.commit()
        return render_template("notifications.html", items=items, fresh_ids=fresh_ids)

    # ---- 데이트 뉴스 (서울 문화행사 피드) ----
    @app.route("/dates")
    @active_couple_required
    def dates():
        """진행 중인(만료되지 않은) 행사 목록 — 이 커플 AI 추천 점수순(높은 순).

        전체 정렬 목록(_ordered_date_items)의 첫 배치(BATCH)만 렌더하고, 나머지는
        스크롤에 맞춰 /dates/more가 배치로 붙인다(무한 스크롤·페이지네이션 UI 없음).
        'ready' 점수가 없는 진행 중 행사가 있으면 백그라운드 채점 스레드를 스폰한다
        (요청은 절대 블록 안 함)."""
        u = current_user()
        # 선택 쿼리 파라미터(칩/정렬). 첫 로드가 일관되도록 기본값을 명시한다.
        cat = request.args.get("cat") or "전체"
        sort = request.args.get("sort") or "score"
        direction = request.args.get("dir") or "desc"
        items = _ordered_date_items(
            u.couple_id, u.id, cat=cat, sort=sort, direction=direction
        )

        # 셀프힐: 'ready' 점수가 없는 진행 중 행사가 있으면 백그라운드 채점을 스폰.
        # (뷰를 반복 조회해도 _scoring 가드로 스레드가 쌓이지 않는다. 요청 블록 X.)
        needs_scoring = any(it["score_status"] != "ready" for it in items)
        if items and needs_scoring:
            _spawn_scoring_if_idle(current_app._get_current_object(), u.couple_id)

        # 셀프힐: 'pending' 추천인데 스레드가 사라졌으면(재시작) 되살린다 — 안 그러면
        # FAB 패널이 '뽑는 중'으로 영원히 폴링한다(초안 고착과 같은 구멍).
        resume_recommendation_if_orphaned(current_app._get_current_object(), u.couple_id)
        # ready 추천은 첫 로드에서 바로 그리고, pending이면 클라가 '뽑는 중' 상태·폴링을 재개한다.
        rec_payload = _date_recommendation_payload(u.couple_id)
        recommendation = rec_payload if rec_payload["status"] in ("ready", "pending") else None

        # 첫 배치만 그리고, 더 있으면 센티넬을 띄운다(클라가 이어서 당겨온다).
        first = items[:_DATES_BATCH]
        has_more = len(items) > _DATES_BATCH
        return render_template(
            "dates.html",
            items=first,
            has_more=has_more,
            recommendation=recommendation,
            cur_cat=cat,
            cur_sort=sort,
            cur_dir=direction,
        )

    @app.route("/dates/recommend", methods=["POST"])
    @active_couple_required
    def date_recommend():
        """'✨ 추천받기' — 이 커플 추천을 pending으로 세팅하고 백그라운드 워커를
        (가드로) 스폰한다. claude는 절대 여기(요청 경로)서 돌지 않는다 — 워커에서만.
        FAB이 fetch로 치므로 JSON을 돌려준다."""
        u = current_user()
        rec = DateRecommendation.query.filter_by(couple_id=u.couple_id).first()
        if rec is None:
            rec = DateRecommendation(couple_id=u.couple_id, status="pending")
            db.session.add(rec)
        else:
            rec.status = "pending"
            rec.updated_at = datetime.utcnow()
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            rec = DateRecommendation.query.filter_by(couple_id=u.couple_id).first()
            if rec is not None:
                rec.status = "pending"
                rec.updated_at = datetime.utcnow()
                db.session.commit()

        # 백그라운드 워커 스폰(요청은 절대 블록 안 함). 중복은 _recommending 가드가 떨군다.
        _spawn_recommend_if_idle(current_app._get_current_object(), u.couple_id)
        return jsonify({"ok": True, "status": "pending"})

    @app.route("/dates/recommend/status")
    @active_couple_required
    def date_recommend_status():
        """이 커플 최신 추천 상태 JSON — FAB 패널 폴링용. 순수 DB 읽기(claude 없음).

        {status, message, picks:[{event_id, title, why, image_url, category,
        source}]}. 존재하지 않거나 만료된 픽은 떨군다.

        폴링이 닿는 자리라 셀프힐도 여기서 한다 — 'pending'인데 만드는 스레드가
        사라졌으면(프로세스 재시작) 되살린다. 사용자는 패널을 열어 둔 채 기다리기만
        해도 복구된다(다시 '추천받기'를 누를 필요가 없다)."""
        u = current_user()
        resume_recommendation_if_orphaned(
            current_app._get_current_object(), u.couple_id
        )
        return jsonify(_date_recommendation_payload(u.couple_id))

    @app.route("/dates/more")
    @active_couple_required
    def dates_more():
        """무한스크롤 다음 배치 — offset부터 BATCH개 카드 HTML 조각.

        범위를 벗어나면 카드 없는 빈 조각을 돌려주고, 클라이언트는 빈 조각을
        받으면 로딩을 멈춘다. 초기 렌더와 같은 _date_cards.html을 쓴다."""
        u = current_user()
        try:
            offset = int(request.args.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0
        cat = request.args.get("cat") or "전체"
        sort = request.args.get("sort") or "score"
        direction = request.args.get("dir") or "desc"
        items = _ordered_date_items(
            u.couple_id, u.id, cat=cat, sort=sort, direction=direction
        )
        batch = items[offset:offset + _DATES_BATCH]
        return render_template("_date_cards.html", items=batch)

    @app.route("/dates/scores")
    @active_couple_required
    def dates_scores():
        """지정 행사 id들의 이 커플 채점 상태 JSON — 제자리 칩 갱신용.

        ids: 콤마 구분 정수(최대 60개). 각 id에 {status, score, reason}을 준다.
        EventScore 행이 없거나 존재하지 않는 id는 status 'none'으로 채운다."""
        u = current_user()
        raw = request.args.get("ids", "") or ""
        ids = []
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                ids.append(int(tok))
            except ValueError:
                continue
            if len(ids) >= 60:
                break

        out = {}
        if ids:
            scores = (
                EventScore.query.filter(
                    EventScore.couple_id == u.couple_id,
                    EventScore.event_id.in_(ids),
                ).all()
            )
            by_event = {s.event_id: s for s in scores}
            for eid in ids:
                s = by_event.get(eid)
                if s is None:
                    out[str(eid)] = {
                        "status": "none", "score": None, "reason": None
                    }
                else:
                    out[str(eid)] = {
                        "status": s.status,
                        "score": s.score,
                        "reason": s.reason,
                    }
        return jsonify(out)

    @app.route("/dates/<int:item_id>")
    @active_couple_required
    def date_detail(item_id):
        u = current_user()
        item = db.session.get(EventItem, item_id)
        if item is None:
            abort(404)
        sc = EventScore.query.filter_by(
            couple_id=u.couple_id, event_id=item.id
        ).first()
        score = sc.score if sc is not None else None
        reason = sc.reason if sc is not None else None
        score_status = sc.status if sc is not None else None
        pk = _pick_states(u.couple_id, u.id, [item.id]).get(item.id, {})
        return render_template(
            "date_detail.html",
            item=item,
            score=score,
            reason=reason,
            score_status=score_status,
            my_status=pk.get("my_status"),
            partner_status=pk.get("partner_status"),
            confirmed=pk.get("confirmed", False),
        )

    @app.route("/dates/<int:event_id>/ics")
    @active_couple_required
    def event_ics(event_id):
        """데이트 뉴스 행사 하나의 .ics를 내려준다 — 탭하면 기기 캘린더에 '추가'된다.
        행사는 전역 카탈로그(피드)라 로그인한 커플이면 모두 볼 수 있고, 없는 id면 404
        (date_detail와 같은 접근 규칙). 행사에는 시간 정보가 없어 항상 종일이며,
        DTEND는 배타적(end_date+1 또는 start+1)이다. 장소=LOCATION·링크=URL."""
        item = db.session.get(EventItem, event_id)
        if item is None:
            abort(404)
        # 시작일이 없으면 오늘로 폴백(방어). 종일: 배타적 DTEND = 종료일+1(또는 시작+1).
        start = item.start_date or date.today()
        dt_end = (item.end_date or start) + timedelta(days=1)
        desc_bits = []
        if item.place:
            desc_bits.append(item.place)
        if item.link:
            desc_bits.append(item.link)
        description = " · ".join(desc_bits) or None
        body = ics.build_ics(
            uid=f"event-{item.id}@ourday",
            summary=item.title,
            dt_start=start,
            dt_end=dt_end,
            all_day=True,
            description=description,
            location=item.place or None,
            url=item.link or None,
        )
        resp = app.response_class(body)
        resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
        # inline = iOS가 캘린더 앱으로 바로 넘김(다운로드 X)
        resp.headers["Content-Disposition"] = (
            f'inline; filename="ourday-date-{item.id}.ics"'
        )
        return resp

    # ---- 데이트 찜/방문함/관심없음 액션(AJAX JSON) ----
    def _pick_json(u, event_id):
        """공통 응답 상태 — 이 (커플,사용자,행사)의 최신 찜 상태 JSON dict."""
        st = _pick_states(u.couple_id, u.id, [event_id]).get(event_id, {})
        return {
            "ok": True,
            "my_status": st.get("my_status"),
            "confirmed": st.get("confirmed", False),
            "interested_count": st.get("interested_count", 0),
        }

    @app.route("/dates/<int:event_id>/pick", methods=["POST"])
    @active_couple_required
    def date_pick(event_id):
        """내 찜(interested) upsert + 상대 알림. 이번 찜으로 '확정'이 새로 성립하면
        (직전엔 미확정) 두 사람 모두에게 확정 알림을 *한 번만* 보낸다."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            return jsonify({"ok": False}), 404
        # 확정 전이(not-confirmed → confirmed) 감지를 위해 직전 상태를 먼저 읽는다.
        before = _pick_states(u.couple_id, u.id, [event_id]).get(event_id, {})
        was_confirmed = before.get("confirmed", False)

        _upsert_pick(u, event_id, "interested")

        after = _pick_states(u.couple_id, u.id, [event_id]).get(event_id, {})
        now_confirmed = after.get("confirmed", False)

        partner = u.partner
        link = url_for("date_detail", item_id=event_id)
        # 상대에게 찜 알림(행위자 본인에겐 절대 보내지 않음).
        _safe_notify(
            partner, "date_pick",
            _pick_msg("상대가 데이트를 찜했어! 💖 ", item.title), link,
        )
        # 새로 확정됐으면(전이 순간에만) 두 사람 모두에게 확정 알림 — 재찜엔 안 울림.
        if now_confirmed and not was_confirmed:
            msg = _pick_msg("데이트 확정! 💘 ", item.title, " 둘 다 찜했어")
            _safe_notify(u, "date_confirm", msg, link)
            _safe_notify(partner, "date_confirm", msg, link)

        return jsonify(_pick_json(u, event_id))

    @app.route("/dates/<int:event_id>/unpick", methods=["POST"])
    @active_couple_required
    def date_unpick(event_id):
        """내 찜 취소 — 내 EventPick 행을 삭제(토글 오프). 알림 없음."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            return jsonify({"ok": False}), 404
        EventPick.query.filter_by(event_id=event_id, user_id=u.id).delete()
        db.session.commit()
        return jsonify(_pick_json(u, event_id))

    @app.route("/dates/<int:event_id>/visited", methods=["POST"])
    @active_couple_required
    def date_visited(event_id):
        """내 상태를 '다녀옴'으로 — 내 피드에서 숨김. 알림 없음."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            return jsonify({"ok": False}), 404
        _upsert_pick(u, event_id, "visited")
        return jsonify(_pick_json(u, event_id))

    @app.route("/dates/<int:event_id>/dismiss", methods=["POST"])
    @active_couple_required
    def date_dismiss(event_id):
        """내 상태를 '관심없음'으로 — 내 피드에서 숨김. 알림 없음."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            return jsonify({"ok": False}), 404
        _upsert_pick(u, event_id, "dismissed")
        return jsonify(_pick_json(u, event_id))

    @app.route("/dates/<int:event_id>/went", methods=["POST"])
    @active_couple_required
    def date_went(event_id):
        """확정(둘 다 찜)된 데이트를 '다녀왔어'로 마감 → 추억 앨범으로 연결한다.

        확정 상태일 때만 동작한다: 커플 두 사람 모두의 EventPick.status를
        'visited'로 바꿔(활성 플랜/피드에서 빠짐) 추억 업로드 화면으로 보낸다 —
        행사 제목을 caption 프리필 힌트로 넘겨(url `?compose=<title>`) 그 날의
        사진이 데이트 이름으로 태깅되게 한다. 확정이 아니면 부드러운 안내 후
        되돌아간다(상태 변경 없음)."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            abort(404)
        st = _pick_states(u.couple_id, u.id, [event_id]).get(event_id, {})
        if not st.get("confirmed"):
            flash("아직 둘 다 찜한 확정 데이트가 아니야.", "error")
            return redirect(request.referrer or url_for("dates_wishlist"))
        # 커플 두 사람 모두의 이 행사 찜을 'visited'로 — 활성 플랜에서 빠진다.
        for member in u.couple.approved_members:
            _upsert_pick(member, event_id, "visited")
        # 추억 업로드로 이동 — 캡션 프리필 힌트(행사 제목)를 쿼리로 넘긴다.
        return redirect(url_for("memories", compose=item.title))

    @app.route("/dates/wishlist")
    @active_couple_required
    def dates_wishlist():
        """커플 공유 찜 목록 — interested가 1개 이상인(만료 안 된) 행사.
        확정(둘 다 찜)을 위에, 찜(한 명만)을 아래에 나눠 보여준다."""
        u = current_user()
        today = date.today()
        rows = (
            db.session.query(EventPick, EventItem)
            .join(EventItem, EventItem.id == EventPick.event_id)
            .filter(
                EventPick.couple_id == u.couple_id,
                EventPick.status == "interested",
                db.or_(EventItem.end_date.is_(None), EventItem.end_date >= today),
            )
            .all()
        )
        # 행사별로 누가 찜했는지 모은다.
        by_event = {}
        for p, ev in rows:
            entry = by_event.setdefault(ev.id, {"event": ev, "uids": set()})
            entry["uids"].add(p.user_id)

        confirmed_items, wish_items = [], []
        for entry in by_event.values():
            ev = entry["event"]
            uids = entry["uids"]
            me = u.id in uids
            partner = any(x != u.id for x in uids)
            confirmed = len(uids) >= 2
            if confirmed:
                who = "나 · 상대 둘 다 찜"
            elif me:
                who = "내가 찜"
            else:
                who = "상대가 찜"
            it = {
                "event": ev,
                "score_status": None,
                "my_status": "interested" if me else None,
                "confirmed": confirmed,
                "interested_count": len(uids),
                "picked_by_me": me,
                "picked_by_partner": partner,
                "wishlist_who": who,
            }
            (confirmed_items if confirmed else wish_items).append(it)

        # 마감 임박 순(마감 없는 건 뒤로), 다음 시작 순으로 정렬.
        _far = date.max

        def _wkey(it):
            ev = it["event"]
            return (ev.end_date or _far, ev.start_date or _far)

        confirmed_items.sort(key=_wkey)
        wish_items.sort(key=_wkey)
        return render_template(
            "dates_wishlist.html",
            confirmed_items=confirmed_items,
            wish_items=wish_items,
        )

    # ---- 데이트 후기 → 블로그 포스트 (P1: 데이터 + 작성 UX) ----
    def _couple_photos_for_picker(couple_id):
        """작성 폼의 사진 선택 그리드용 — 이 커플 사진을 최신순 dict 리스트로.

        _photo_grid.html와 같은 필드 모양(id·caption·tags·caption_status·
        original_name)을 주되, 여기선 썸네일 선택 UI만 쓴다. OneDrive 미연결이면
        빈 리스트(폼은 '앨범에 사진이 없어' 안내를 보인다). 순수 DB 읽기.
        """
        if not onedrive.onedrive_enabled():
            return []
        rows = (
            Photo.query.filter_by(couple_id=couple_id)
            .order_by(Photo.created_at.desc(), Photo.id.desc())
            .all()
        )
        return [
            {
                "id": p.id,
                "caption": p.caption,
                "tags": p.tags_list,
                "caption_status": p.caption_status,
                "original_name": p.original_name,
            }
            for p in rows
        ]

    def _parse_photo_ids(raw, couple_id):
        """폼 hidden input(photo_ids)을 이 커플의 Photo id 리스트로 파싱한다.

        JSON 배열 우선, 실패하면 CSV로 관대하게 파싱한다. **순서를 유지**하고,
        중복은 첫 등장만 남기며, 이 커플 소유가 아닌 id는 조용히 버린다(스코프 강제).
        """
        raw = (raw or "").strip()
        ids = []
        if raw:
            parsed = None
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                candidates = parsed
            else:
                # CSV 폴백(예: "3,1,2").
                candidates = [tok for tok in raw.replace("\n", ",").split(",")]
            for c in candidates:
                try:
                    ids.append(int(c))
                except (ValueError, TypeError):
                    continue
        if not ids:
            return []
        # 커플 소유 사진만 남긴다(순서 유지 + 중복 제거).
        owned = {
            pid
            for (pid,) in db.session.query(Photo.id)
            .filter(Photo.id.in_(ids), Photo.couple_id == couple_id)
            .all()
        }
        seen = set()
        out = []
        for i in ids:
            if i in owned and i not in seen:
                seen.add(i)
                out.append(i)
        return out

    def _parse_review_details(src):
        """폼에서 v2 '재료' 칸 8개를 파싱해 ``{필드: 값|''}`` dict로.

        전부 **선택 입력**이라 빈 칸은 ``''``로 둔다(저장 시 None = '모른다'). 길이는
        컬럼 정의에 맞춰 자른다 — 폼이 maxlength를 걸지만 서버에서도 강제한다.
        키 순서는 ``BlogReview.DETAIL_FIELDS``(= 글의 흐름 순서)를 따른다.
        """
        limits = {
            "visited_when": 100,
            "visit_reason": 300,
            "access_note": 300,
            "waiting": 200,
            "order_items": 2000,
            "highlight": 300,
            "downside": 300,
            "researched": 2000,
        }
        return {
            f: (src.get(f) or "").strip()[: limits[f]]
            for f in BlogReview.DETAIL_FIELDS
        }

    def _apply_review_details(review, details):
        """파싱된 재료 dict를 모델에 반영(빈 문자열은 NULL로 — '모른다'와 같은 뜻)."""
        for f in BlogReview.DETAIL_FIELDS:
            setattr(review, f, (details.get(f) or "").strip() or None)

    def _review_form_error(u, form, editing=None):
        """작성/수정 폼을 입력값을 유지한 채 다시 렌더(유효성 오류 경로)."""
        return render_template(
            "review_form.html",
            photos=_couple_photos_for_picker(u.couple_id),
            editing=editing,
            form=form,
        )

    @app.route("/reviews")
    @active_couple_required
    def reviews():
        """이 커플의 데이트 후기 목록(최신순). 순수 DB 읽기."""
        u = current_user()
        rows = (
            BlogReview.query.filter_by(couple_id=u.couple_id)
            .order_by(BlogReview.created_at.desc(), BlogReview.id.desc())
            .all()
        )
        return render_template("reviews.html", reviews=rows)

    @app.route("/reviews/new", methods=["GET", "POST"])
    @active_couple_required
    def review_new():
        """데이트 후기 작성 — GET은 폼, POST는 저장(status='draft'). AI 없음(P1)."""
        u = current_user()
        if request.method == "GET":
            return render_template(
                "review_form.html",
                photos=_couple_photos_for_picker(u.couple_id),
                editing=None,
                form=None,
            )

        topic = (request.form.get("topic") or "").strip()[:200]
        location = (request.form.get("location") or "").strip()[:300] or None
        prose = (request.form.get("prose") or "").strip()
        raw_ids = request.form.get("photo_ids") or ""
        photo_ids = _parse_photo_ids(raw_ids, u.couple_id)

        # 별점 0~10 정수 파싱(clamp).
        try:
            score = int((request.form.get("overall_score") or "").strip())
        except (ValueError, TypeError):
            score = None
        if score is not None:
            score = max(0, min(10, score))

        details = _parse_review_details(request.form)

        # 폼 상태(오류 시 재렌더용) — 파싱된 값들을 그대로 담는다.
        form = {
            "topic": topic,
            "location": location or "",
            "prose": prose,
            "overall_score": score if score is not None else 0,
            "photo_ids": photo_ids,
            **details,
        }
        if not topic:
            flash("주제/장소를 입력해줘.", "error")
            return _review_form_error(u, form)
        if not prose:
            flash("느낀점을 한두 줄이라도 남겨줘.", "error")
            return _review_form_error(u, form)
        if score is None:
            flash("별점을 매겨줘.", "error")
            return _review_form_error(u, form)

        review = BlogReview(
            couple_id=u.couple_id,
            created_by=u.id,
            topic=topic,
            location=location,
            prose=prose,
            overall_score=score,
            photo_ids=json.dumps(photo_ids) if photo_ids else None,
            status="pending",  # (P2) 바로 백그라운드 초안 생성으로.
        )
        _apply_review_details(review, details)
        db.session.add(review)
        db.session.commit()
        # (P2) 백그라운드로 네이버 블로그 초안 생성을 시작한다(요청 경로 아님).
        _spawn_generate_review(current_app._get_current_object(), review.id)
        flash("등록 완료! AI가 블로그 초안을 쓰는 중이야 ✍️", "success")
        return redirect(url_for("review_detail", rid=review.id))

    @app.route("/reviews/<int:rid>")
    @active_couple_required
    def review_detail(rid):
        """저장된 후기 상세 — 주제/위치 · 별점 · 산문 · 선택 사진(순서). P1은 AI
        출력이 없어 placeholder 카드를 보인다. cross-couple은 404."""
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        # 셀프힐 — 2026-10-07 사고(메모리 한도 초과 → 자동 재시작 → 데몬 스레드 사망 →
        # 행이 'pending' 영구 고착)의 복구 지점. 'pending'인데 만드는 스레드가 사라졌으면
        # 여기서 되살린다. /memories 가 멈춘 캡션을, /dates 가 미채점 행사를 되살리는
        # 것과 같은 자리·같은 패턴이다. 산출물은 건드리지 않는다(함수 docstring).
        app_obj = current_app._get_current_object()
        review_resumed = resume_review_if_orphaned(app_obj, review)
        # 'failed' 도 같은 자리에서 한 번 되살린다 — 같은 사고가 pending/failed 로
        # 갈리는 비대칭을 없앤다(resume_review_if_failed 머리말).
        review_resumed = resume_review_if_failed(app_obj, review) or review_resumed
        thumb_resumed = resume_thumbnail_if_orphaned(app_obj, review)
        # 네이버 복사본: 사용자가 편집한 게 있으면 그걸(이제 HTML), 없으면 생성
        # 초안에서 HTML을 조립. edited_text는 이제 HTML을 담는다. 뷰 시점에 이미지
        # 토큰을 신선하게 재발급해, 저장본이 옛(만료) 토큰을 얼렸어도 지금 복사한
        # 게 동작하게 한다(_review_copy_html은 이미 신선하나 멱등해 무해).
        copy_html = _refresh_blog_img_tokens(
            review.edited_text or _review_copy_html(review)
        )
        # 수동 크롭 조정 UI 재료 — image 블록마다(블록 인덱스 기준). 옛/새 스키마 모두
        # _ensure_blocks로 통일해 블록 인덱스가 crop-save와 일치하게 한다. 뷰포트에
        # 띄울 '원본 전체'는 크롭 없는 blog_img_url(다운스케일 풀이미지)을 쓴다.
        crop_sections = []
        # 정식(네이버 내부이미지) 삽입용 구조화 데이터 — cd-doc-data와 공개 pending API가
        # 같은 빌더(_build_export_payload)를 쓴다. image 블록엔 hq blog-img __src가 붙는다
        # (온페이지 붙여넣기 흐름은 iK=사진 id+크롭으로 매칭해 hq 여부 무관). 크롭 UI용
        # crop_sections는 그 블록들에서 파생한다(블록 인덱스가 crop-save와 일치).
        doc_data, _export_images = _build_export_payload(review)
        if doc_data:
            photos_ordered = review.photos_ordered
            blocks = doc_data["blocks"]
            for i, blk in enumerate(blocks):
                if not isinstance(blk, dict) or blk.get("type") != "image":
                    continue
                pi = blk.get("photo_index")
                if not (isinstance(pi, int) and 0 <= pi < len(photos_ordered)):
                    continue
                p = photos_ordered[pi]
                crop = blk.get("crop") or [0.0, 0.0, 1.0, 1.0]
                crop_sections.append(
                    {
                        "index": i,  # 블록 인덱스(crop-save가 target)
                        "heading": _crop_hint_for_image(blocks, i)[:40],
                        # 크롭 조정 UI 는 **화면에서 드래그**하는 미리보기다 —
                        # 원본(수 MB)을 부를 이유가 없다. 우리가 소유한 'view'
                        # 티어 자산(긴 변 1280)을 **고정 URL**로 받아 CSS 로 자른
                        # 모습을 보여 준다. 예전엔 같은 그림을 만료 24시간짜리
                        # 서명 URL(`/blog-img?e=&t=&v=r1280`)로 줬는데, 렌더마다
                        # 토큰이 달라져 **브라우저 캐시가 매번 미스**였고 매 요청이
                        # Graph 렌디션 왕복이었다. 픽셀은 같고 딜레이만 사라진다.
                        "img_url": _preview_url(p.id),
                        "crop": crop,
                    }
                )
        # 썸네일(Step 4) 재료. 서버는 스펙(template.css에서 읽은 수치)과 사진 URL만
        # 넘기고, 그림은 브라우저가 그린다.
        #
        # ⛔ **URL 이 두 개인 이유 — 화면과 결과물은 다른 그림을 필요로 한다.**
        #   preview_url : 화면의 DOM 미리보기(가로 300~400px 로 줄여 보여 준다).
        #                 우리가 소유한 'view' 자산 — 고정 URL · 만료 없음 · 재방문
        #                 네트워크 0. 예전엔 여기에도 **원본(수 MB)** 을 걸어 둬서,
        #                 썸네일 카드가 있는 후기는 상세 화면을 열 때마다 원본 한
        #                 장을 통째로 받았다(아무도 '내려받기'를 누르지 않아도).
        #   url         : 1080×1350 캔버스 래스터용 **원본 중계**(v=orig). 여기선
        #                 원본이 맞다 — 다운스케일본을 늘려 쓰면 흐려진다. 이 URL 은
        #                 '내려받기'를 누를 때 **그때 비로소** 로드된다.
        thumb_photos = []
        for i, p in enumerate(review.photos_ordered):
            thumb_photos.append(
                {
                    "index": i,
                    "url": publish_source_url(p),
                    "preview_url": _preview_url(p.id),
                    "name": (p.caption or p.original_name or f"사진 {i + 1}")[:40],
                }
            )
        # GIF(Step 5) 재료. 영상 목록은 **DB 읽기뿐**이다 — 여기서 OneDrive 를 부르지
        # 않는다(페이지 렌더가 네트워크를 기다리면 안 된다). 바이트가 어디서 올지는
        # 브라우저가 ``/videos/<id>/source`` 로 따로 물어본다(거기서 한 번만 실측).
        gif_videos = [
            {
                "id": v.id,
                "name": (v.original_name or v.filename or f"영상 {v.id}")[:60],
                "size": v.size_bytes,
                "stream_url": url_for("video_stream", video_id=v.id),
                "source_url": url_for("video_source", video_id=v.id),
                "delete_url": url_for("video_delete", video_id=v.id),
            }
            for v in (
                Video.query.filter_by(couple_id=u.couple_id)
                .order_by(Video.created_at.desc(), Video.id.desc())
                .all()
            )
        ]
        return render_template(
            "review_detail.html",
            review=review,
            photos=review.photos_ordered,
            copy_html=copy_html,
            crop_sections=crop_sections,
            doc_data=doc_data,
            export_images=_export_images,
            thumb=review.thumbnail,
            thumb_spec=thumbnail.spec(),
            thumb_photos=thumb_photos,
            gif=review.gif,
            gif_spec=gifmaker.spec(),
            gif_videos=gif_videos,
            onedrive_ready=onedrive.onedrive_enabled(),
            # 조사 메모 카드 재료. research는 dict(없으면 None)이고, 키 보유 여부는
            # **불리언만** 넘어간다(값은 템플릿에 절대 안 간다).
            research=review.research,
            naver_keys_set=u.has_naver_api_keys,
            # 'pending' 두 건(초안·썸네일)의 생성이 실제로 살아 있는지 — 화면이
            # '작성중'만 반복하지 않고 무슨 일이 났는지 말하게 하는 재료.
            review_state=review_pending_verdict(review),
            review_resumed=review_resumed,
            thumb_state=thumbnail_pending_verdict(review),
            thumb_resumed=thumb_resumed,
            # 다섯 상태로 펼친 '지금 무슨 일이 일어나고 있나' — 첫 렌더는 서버가
            # 그려 두고, 그 뒤 갱신은 /progress JSON 이 **같은 함수**로 만든다.
            progress=review_progress_view(app_obj, review, resumed=review_resumed),
            # 첫 화면 첫 사진 — <head> 에서 프리로드할 URL(없으면 None → 링크 생략).
            preview_preload=_first_img_src(copy_html),
        )

    @app.route("/reviews/<int:rid>/progress")
    @active_couple_required
    def review_progress(rid):
        """지금 그 초안이 **어디까지 왔나**를 작은 JSON 하나로 돌려준다.

        ⛔ 왜 페이지 새로고침이 아니라 이건가 — 예전엔 'pending' 동안 ``meta
        refresh`` 가 4초마다 **상세 페이지 전체**를 다시 그렸다. 그 페이지는
        사진·썸네일·GIF·복사본 HTML 까지 다 세우는 가장 무거운 화면이라, 기다리는
        동안 가장 비싼 요청을 가장 자주 보내고 있었던 셈이다. 여기서는 후기 행 +
        큐 행만 보고 수백 바이트를 돌려준다. **프리렌더와 '재진입 네트워크 0'은
        그대로다** — 이 경로는 그림을 하나도 건드리지 않는다.

        그리고 이 엔드포인트가 **셀프힐 자리**를 그대로 물려받는다(상세 화면이
        하던 ``resume_*``). 폴링이 페이지 새로고침을 대신하므로, 복구가 '사람이
        화면을 다시 열어야' 도는 일이 되면 안 된다 (CLAUDE.md: 판정·재개·탈출구).
        """
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        app_obj = current_app._get_current_object()
        resumed = resume_review_if_orphaned(app_obj, review)
        resumed = resume_review_if_failed(app_obj, review) or resumed
        thumb_resumed = resume_thumbnail_if_orphaned(app_obj, review)
        view = review_progress_view(app_obj, review, resumed=resumed)
        view["thumb_pending"] = (review.thumbnail or {}).get("status") == "pending"
        view["thumb_resumed"] = bool(thumb_resumed)
        return jsonify(view)

    # ---- 네이버 자동 export(v2) ----
    @app.route("/api/naver-export/mark/<int:rid>", methods=["POST"])
    @active_couple_required
    def naver_export_mark(rid):
        """앱에서 📤 누를 때 호출 — 이 후기를 '대기(pending)' 슬롯에 세운다.

        요청자 커플 소유 + ready 후기만. 사용자 export_key(없으면 생성)에 {rid,exp}를
        바인딩한다. 유저스크립트가 공개 pending API로 이 rid를 꺼내 자동 삽입한다.
        same-origin 인증 요청(로그인 필요). 클립보드 복사는 프런트에서 별도 폴백.
        """
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        if not (review.status == "ready" and review.ai):
            return jsonify(ok=False, error="not_ready"), 409
        key = _ensure_export_key(u)
        if not key:
            return jsonify(ok=False, error="no_key"), 500
        _naver_export_put(key, rid)
        return jsonify(ok=True)

    @app.route("/api/naver-export/pending", methods=["GET", "OPTIONS"])
    def naver_export_pending():
        """공개(로그인 불필요) — export_key가 인가. 대기 중 후기의 자동삽입 페이로드.

        네이버(blog.naver.com) 유저스크립트가 cross-origin으로 부른다 → **모든 응답 경로**에
        CORS ``*`` (+ OPTIONS preflight). 키로 사용자를 찾아 비만료 pending rid가 있으면
        payload(``pending:true``)를 내고 슬롯을 삭제(clear-on-serve). 없음/만료/잘못된 키는
        404가 아니라 **200 ``{"pending": false}``** 로 낸다 — 브라우저가 CORS로 막힌 404를
        읽지 못해 fetch가 NetworkError로 reject되던 문제를 피하고, "할 일 없음"을 깔끔히
        전달한다. 키 유무는 누설하지 않는다(잘못된 키·무대기 모두 pending:false). 쿠키 불필요.
        """
        def _cors(resp):
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Cache-Control"] = "no-store"
            return resp

        if request.method == "OPTIONS":
            resp = app.response_class("", status=204)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "*"
            resp.headers["Access-Control-Max-Age"] = "86400"
            return resp

        key = (request.args.get("k") or "").strip()
        if not key:  # 진짜 잘못된 요청만 400 — 단, ACAO는 반드시 붙인다.
            return _cors(jsonify(pending=False, error="missing_key")), 400

        try:
            rid = _naver_export_take(key)  # clear-on-serve
            if rid:
                user = User.query.filter_by(export_key=key).first()
                review = db.session.get(BlogReview, rid)
                if user is not None and review is not None and review.couple_id == user.couple_id:
                    doc_data, images = _build_export_payload(review)
                    if doc_data:
                        payload = dict(doc_data)
                        payload["pending"] = True
                        payload["rid"] = rid
                        payload["images"] = images or []
                        return _cors(jsonify(payload))
        except Exception:  # noqa: BLE001 — 어떤 실패든 CORS 붙은 응답으로(누설·NetworkError 방지)
            db.session.rollback()
            log.exception("naver-export pending 처리 실패")
        # 무대기 / 잘못된 키 / 만료 / 내부 실패 → 200 {"pending": false} (+ACAO)
        return _cors(jsonify(pending=False))

    @app.route("/reviews/<int:rid>/edit", methods=["GET", "POST"])
    @active_couple_required
    def review_edit(rid):
        """후기 수정 — 주제/위치/산문/별점/사진 선택+순서. cross-couple은 404."""
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)

        if request.method == "GET":
            form = {
                "topic": review.topic,
                "location": review.location or "",
                "prose": review.prose,
                "overall_score": review.overall_score,
                "photo_ids": review.photo_ids_list,
                # v2 재료 칸 — 옛 후기는 전부 빈 칸으로 열린다(컬럼이 NULL).
                **{f: (getattr(review, f, None) or "") for f in BlogReview.DETAIL_FIELDS},
            }
            return render_template(
                "review_form.html",
                photos=_couple_photos_for_picker(u.couple_id),
                editing=review,
                form=form,
            )

        topic = (request.form.get("topic") or "").strip()[:200]
        location = (request.form.get("location") or "").strip()[:300] or None
        prose = (request.form.get("prose") or "").strip()
        photo_ids = _parse_photo_ids(request.form.get("photo_ids") or "", u.couple_id)
        try:
            score = int((request.form.get("overall_score") or "").strip())
        except (ValueError, TypeError):
            score = None
        if score is not None:
            score = max(0, min(10, score))

        details = _parse_review_details(request.form)

        form = {
            "topic": topic,
            "location": location or "",
            "prose": prose,
            "overall_score": score if score is not None else 0,
            "photo_ids": photo_ids,
            **details,
        }
        if not topic:
            flash("주제/장소를 입력해줘.", "error")
            return _review_form_error(u, form, editing=review)
        if not prose:
            flash("느낀점을 한두 줄이라도 남겨줘.", "error")
            return _review_form_error(u, form, editing=review)
        if score is None:
            flash("별점을 매겨줘.", "error")
            return _review_form_error(u, form, editing=review)

        review.topic = topic
        review.location = location
        review.prose = prose
        review.overall_score = score
        review.photo_ids = json.dumps(photo_ids) if photo_ids else None
        _apply_review_details(review, details)
        # 입력이 바뀌었으니 초안을 새로 뽑는다 — pending으로 되돌리고 편집본은
        # 비워(새 초안이 복사본으로 보이게) 백그라운드 재생성을 시작한다.
        review.status = "pending"
        review.edited_text = None
        db.session.commit()
        _spawn_generate_review(current_app._get_current_object(), review.id)
        flash("후기를 수정했어. 바뀐 내용으로 초안을 다시 만드는 중이야 ✍️", "success")
        return redirect(url_for("review_detail", rid=review.id))

    @app.route("/reviews/<int:rid>/delete", methods=["POST"])
    @active_couple_required
    def review_delete(rid):
        """후기 삭제 → 목록. cross-couple은 404."""
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        db.session.delete(review)
        db.session.commit()
        flash("후기를 삭제했어.", "success")
        return redirect(url_for("reviews"))

    @app.route("/reviews/<int:rid>/regenerate", methods=["POST"])
    @active_couple_required
    def review_regenerate(rid):
        """초안 '다시 생성' — pending으로 되돌리고 편집본을 비운 뒤 백그라운드
        재생성을 스폰한다. 새 초안이 복사본으로 보이도록 edited_text를 지운다.
        cross-couple은 404."""
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        review.status = "pending"
        review.edited_text = None
        db.session.commit()
        _spawn_generate_review(current_app._get_current_object(), review.id)
        flash("초안을 다시 만드는 중이야 ✍️", "success")
        return redirect(url_for("review_detail", rid=review.id))

    @app.route("/reviews/<int:rid>/cancel", methods=["POST"])
    @active_couple_required
    def review_cancel(rid):
        """초안 생성 **중단** — 사람이 '✋ 그만할래'를 눌렀다. cross-couple은 404.

        왜 이게 있나 — 예전엔 claude 콜마다 120초 상한이 있었고, 그게 **일하고 있는
        claude 를 끊어** 후기를 ``failed`` 로 만들었다(본문 1콜 실측 73.7초, Render 는
        더 느리다). 상한을 걷어낸 대신, 멈추는 권한을 **사람에게** 준다.

        누르면 실제로 넷을 치운다 — 하나라도 빠지면 '멈췄다'는 말이 거짓이 된다:

          1. **claude 프로세스** — ``ai.request_cancel`` 이 등록된 프로세스를 그룹째
             죽인다. 그러면 일하던 쪽은 ``ai.Cancelled`` 로 접힌다.
          2. **큐 행** — ``aijobs.cancel`` 이 ``review:<id>`` 행을 지운다(아직 줄만
             서 있던 경우도 여기서 사라진다).
          3. **인프로세스 가드** — 안 풀면 나중에 ↻ 를 눌러도 스폰이 조용히 no-op 다.
          4. **후기 상태** — 'pending' 이었으면 'cancelled'. 이미 초안이 떠 있었다면
             (크롭만 남은 단계) 그 초안은 **그대로 둔다** — 쓸 수 있는 글을 사람이
             멈췄다고 버리지 않는다.
        """
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        key = f"review:{rid}"
        killed = ai.request_cancel(key)
        aijobs.cancel(key)
        with _generating_reviews_lock:
            _generating_reviews.discard(rid)
        had_draft = review.ai is not None
        if review.status == "pending":
            review.status = "cancelled"
            review.updated_at = datetime.utcnow()
            try:
                db.session.commit()
            except Exception:  # noqa: BLE001 — 중단이 페이지를 깨뜨리지 않게
                db.session.rollback()
                log.exception("후기 초안 중단 커밋 실패 (review=%s)", rid)
        log.info("후기 초안 생성을 사람이 중단했다 (review=%s, 죽인 claude=%s)",
                 rid, killed)
        flash(
            "여기까지만 하고 멈췄어. 지금까지 쓴 초안은 그대로 둘게 ✋"
            if had_draft else "초안 만들기를 멈췄어 ✋",
            "success",
        )
        return redirect(url_for("review_detail", rid=review.id))

    @app.route("/reviews/<int:rid>/save-text", methods=["POST"])
    @active_couple_required
    def review_save_text(rid):
        """편집한 네이버 복사본을 edited_text에 저장(영속). fetch면 JSON, 아니면
        상세로 리다이렉트. 빈 값이면 None으로 저장해 다음엔 생성 초안이 다시
        시드된다. cross-couple은 404."""
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        text = (request.form.get("text") or "").strip()
        review.edited_text = text or None
        db.session.commit()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(ok=True)
        flash("복사본을 저장했어.", "success")
        return redirect(url_for("review_detail", rid=review.id))

    @app.route("/reviews/<int:rid>/title", methods=["POST"])
    @active_couple_required
    def review_pick_title(rid):
        """제목 후보 중 하나를 최종 제목으로 고른다(ai_json.title 교체).

        v2는 AI가 '타깃 한 문장 → 후보 3개(서로 다른 구조) → 선정 + 이유'를 내므로
        사람이 화면에서 바꿔 고를 수 있어야 한다. 요청: ``index`` = title_candidates의
        0-based 인덱스. 후보가 없거나 범위 밖이면 400.

        초안(ai_json)만 고치고 **편집본(edited_text)은 비운다** — 편집본은 옛 제목이
        박힌 HTML이라 그대로 두면 화면 제목과 초안이 어긋난다(프런트가 편집본이 있을
        때만 confirm을 띄운다). cross-couple은 404.
        """
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        data = review.ai
        cands = (data or {}).get("title_candidates") or []
        try:
            idx = int((request.form.get("index") or "").strip())
        except (ValueError, TypeError):
            idx = -1
        if not isinstance(cands, list) or not (0 <= idx < len(cands)):
            abort(400)
        picked = cands[idx]
        new_title = ""
        if isinstance(picked, dict):
            new_title = (picked.get("title") or "").strip()
        elif isinstance(picked, str):
            new_title = picked.strip()
        if not new_title:
            abort(400)
        data["title"] = new_title
        review.ai_json = json.dumps(data, ensure_ascii=False)
        review.edited_text = None
        db.session.commit()
        flash("제목을 바꿨어. 미리보기도 새 제목으로 다시 만들었어 ✏️", "success")
        return redirect(url_for("review_detail", rid=review.id))

    @app.route("/reviews/<int:rid>/crop", methods=["POST"])
    @active_couple_required
    def review_crop_save(rid):
        """한 image 블록의 '수동 크롭'을 저장한다(사용자가 사진 위치를 직접 조정).

        요청: section(0-based '블록' 인덱스) + crop='x,y,w,h'(정규화). 검증: 4개 float,
        0..1·경계 안, 대상이 image 블록, 픽셀 비율 ≈ 4:3(사진 크기를 알 수 있을 때만
        엄격). 통과하면 해당 image 블록의 crop만 갱신해 ai_json에 다시 넣는다 —
        edited_text는 절대 건드리지 않는다. 옛 스키마 후기는 _ensure_blocks로 blocks로
        변환해 저장하므로 이후 blocks로 통일된다. AJAX면 JSON, 아니면 리다이렉트.
        cross-couple 404·잘못된 입력 400.
        """
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        def _fail(msg, code=400):
            if is_ajax:
                return jsonify(ok=False, error=msg), code
            flash(msg, "error")
            return redirect(url_for("review_detail", rid=rid))

        # 옛/새 스키마를 blocks로 통일(블록 인덱스가 UI와 일치).
        data = _ensure_blocks(review.ai)
        if not data or not isinstance(data.get("blocks"), list):
            return _fail("아직 초안이 없어.", 400)
        blocks = data["blocks"]

        try:
            idx = int(request.form.get("section", ""))
        except (ValueError, TypeError):
            return _fail("잘못된 블록이야.", 400)
        if idx < 0 or idx >= len(blocks):
            return _fail("잘못된 블록이야.", 400)
        if not isinstance(blocks[idx], dict) or blocks[idx].get("type") != "image":
            return _fail("이미지 블록이 아니야.", 400)

        parts = (request.form.get("crop") or "").split(",")
        if len(parts) != 4:
            return _fail("잘못된 크롭 값이야.", 400)
        try:
            x, y, w, h = (float(p) for p in parts)
        except ValueError:
            return _fail("잘못된 크롭 값이야.", 400)

        eps = 1e-6
        if not (
            all(0.0 - eps <= v <= 1.0 + eps for v in (x, y, w, h))
            and w > eps
            and h > eps
            and x + w <= 1.0 + 1e-3
            and y + h <= 1.0 + 1e-3
        ):
            return _fail("크롭이 이미지 밖으로 나갔어.", 400)

        # 픽셀 비율 검증(사진 크기를 알 수 있을 때만 — onedrive 조회 실패 시 관대).
        # ⛔ 크기 두 개를 알자고 3MB 원본을 받지 않는다 — 메타데이터면 충분하다
        #    (_crop_vision_dims: 렌디션 메타 → image 패싯, 둘 다 픽셀 0바이트).
        try:
            pi = blocks[idx].get("photo_index")
            photos_ordered = review.photos_ordered
            if isinstance(pi, int) and 0 <= pi < len(photos_ordered):
                dims = _crop_vision_dims(photos_ordered[pi])
            else:
                dims = None
        except Exception:  # noqa: BLE001 — 크기 조회 실패는 관대(경계 검증은 이미 통과)
            dims = None
        if dims:
            W, H = dims
            denom = h * H
            aspect = (w * W) / denom if denom else 0.0
            if abs(aspect - _BLOG_CROP_ASPECT) > 0.03 * _BLOG_CROP_ASPECT:
                return _fail("크롭 비율이 4:3이 아니야.", 400)

        # 경계로 한 번 더 클램프 후 저장(edited_text는 손대지 않음). 전체 blocks
        # 스키마를 다시 써 넣어 옛 스키마 후기도 첫 저장에 blocks로 넘어간다.
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.0, min(1.0 - x, w))
        h = max(0.0, min(1.0 - y, h))
        blocks[idx]["crop"] = [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]
        review.ai_json = json.dumps(data, ensure_ascii=False)
        db.session.commit()
        # 크롭이 바뀌면 그 블록의 std 캐시 키도 바뀐다(키에 크롭이 들어간다) —
        # 다음 방문이 기다리지 않게 **지금** 다시 구워 둔다(프리렌더, claude 無).
        _enqueue_ai(current_app._get_current_object(), "prerender",
                    f"prerender:{review.id}", {"review_id": review.id})
        if is_ajax:
            return jsonify(ok=True, crop=blocks[idx]["crop"])
        flash("사진 위치를 저장했어.", "success")
        return redirect(url_for("review_detail", rid=review.id))

    # ---- 썸네일 (Step 4) --------------------------------------------------
    # 서버는 픽셀을 만들지 않는다. 카피를 만들고(claude), 사람이 고르거나 줄인 문구를
    # 저장하고, **브라우저가 보고한 측정값을 다시 판정해** 넘침 여부를 기록한다.
    # PNG는 사용자의 브라우저가 Canvas로 그려 바로 내려받는다(thumbnail.py 머리말).
    def _review_or_404(rid):
        u = current_user()
        review = db.session.get(BlogReview, rid)
        if review is None or review.couple_id != u.couple_id:
            abort(404)
        return review

    @app.route("/reviews/<int:rid>/thumbnail", methods=["POST"])
    @active_couple_required
    def review_thumbnail_generate(rid):
        """썸네일 카피를 (재)생성한다 — 백그라운드 스레드. 초안이 없으면 409.

        요청 경로에서 claude를 돌리지 않는다(앱 절대 제약). 상태를 'pending'으로
        적고 바로 돌아오면, 상세 화면이 meta refresh로 결과를 받는다.
        """
        review = _review_or_404(rid)
        if not (review.status == "ready" and review.ai):
            flash("초안이 준비된 다음에 썸네일을 만들 수 있어.", "error")
            return redirect(url_for("review_detail", rid=review.id))
        state = review.thumbnail or {}
        state["status"] = "pending"
        state.pop("last_error", None)
        review.thumbnail_json = json.dumps(state, ensure_ascii=False)
        db.session.commit()
        _spawn_generate_thumbnail(app, review.id)
        flash("썸네일 문구를 만드는 중이야… 잠깐만 ✨", "success")
        return redirect(url_for("review_detail", rid=review.id))

    @app.route("/reviews/<int:rid>/thumbnail/state", methods=["POST"])
    @active_couple_required
    def review_thumbnail_state(rid):
        """사람이 고른 후보·줄인 문구·쓸 사진, 그리고 **브라우저 렌더 검증**을 저장.

        JSON 본문: ``{"picked": 0, "copy": {...}, "photo_index": 0,
        "render_check": {"fonts_ok": true, "boxes": {...}}}`` — 전부 선택이다.

        ``render_check``는 클라이언트가 보낸 **측정값만** 받고 ``ok``는 서버가 다시
        계산한다(``thumbnail.evaluate_render_check``) — 넘침 판정을 클라이언트의
        주장에 맡기지 않는다. 폰트 크기를 줄여 맞추는 길은 **스키마에 없다**:
        넘치면 ``ok=False``로 남고 문구를 줄여야 한다(gf-blog 규율).
        """
        review = _review_or_404(rid)
        state = review.thumbnail
        if not (state and state.get("candidates")):
            return jsonify(ok=False, error="no_thumbnail"), 409
        body = request.get_json(silent=True) or {}
        state = thumbnail.apply_pick(
            state,
            picked=body.get("picked"),
            copy=body.get("copy"),
            photo_index=body.get("photo_index"),
        )
        if "render_check" in body:
            checked = thumbnail.evaluate_render_check(body.get("render_check"))
            if checked is None:
                return jsonify(ok=False, error="bad_render_check"), 400
            checked["checked_at"] = datetime.utcnow().isoformat(timespec="seconds")
            state["render_check"] = checked
        state["status"] = "ready"
        review.thumbnail_json = json.dumps(state, ensure_ascii=False)
        db.session.commit()
        rc = state.get("render_check") or {}
        return jsonify(
            ok=True,
            render_ok=bool(rc.get("ok")),
            overflowing=rc.get("overflowing") or [],
        )

    # ---- 동영상 → GIF (Step 5) --------------------------------------------
    # 썸네일과 **같은 자리에 같은 방식으로** 얹는다: 서버는 픽셀을 만들지 않고,
    # 프리셋·한도를 단일 원천으로 내려보낸 뒤 **브라우저가 보고한 결과를 다시
    # 판정**해 기록한다. 한도를 넘으면 화질을 몰래 깎지 않고 내려받기를 막는다.
    #
    # ⛔ 이 경로에는 claude 호출이 **하나도 없다.** 초안 생성(~170초)과 완전히 별개다.
    @app.route("/reviews/<int:rid>/gif/state", methods=["POST"])
    @active_couple_required
    def review_gif_state(rid):
        """고른 영상·구간·프리셋과 **브라우저가 보고한 인코딩 결과/진단**을 저장.

        JSON 본문: ``{"settings": {...}, "result": {...}, "diag": {...}}`` — 전부 선택.

        ``result`` 는 측정값만 받고 ``ok`` 는 서버가 다시 계산한다
        (``gifmaker.evaluate_result``). 용량 판정을 클라이언트의 주장에 맡기지
        않는다 — 썸네일의 ``evaluate_render_check`` 와 같은 태도다.
        """
        review = _review_or_404(rid)
        body = request.get_json(silent=True) or {}
        state = review.gif or {}

        settings = None
        if "settings" in body:
            settings = gifmaker.normalize_settings(body.get("settings"))
            # 고른 영상이 이 커플 것인지 **여기서 다시** 확인한다(남의 영상 id 차단).
            vid = settings.get("video_id")
            if vid is not None:
                owned = db.session.get(Video, vid)
                if owned is None or owned.couple_id != review.couple_id:
                    return jsonify(ok=False, error="bad_video"), 400

        result = None
        if "result" in body:
            result = gifmaker.evaluate_result(body.get("result"))
            if result is None:
                return jsonify(ok=False, error="bad_result"), 400
            result["made_at"] = datetime.utcnow().isoformat(timespec="seconds")

        diag = None
        if "diag" in body:
            diag = gifmaker.evaluate_diag(body.get("diag"))
            if diag is None:
                return jsonify(ok=False, error="bad_diag"), 400

        state = gifmaker.apply_state(state, settings=settings, result=result,
                                     diag=diag)
        state["status"] = "ready"
        review.gif_json = json.dumps(state, ensure_ascii=False)
        db.session.commit()
        r = state.get("result") or {}
        return jsonify(ok=True, size_ok=bool(r.get("ok")),
                       over_by=r.get("over_by", 0))

    # ---- Web Push subscription management ----
    @app.route("/push/public-key")
    def push_public_key():
        """The VAPID public key the browser needs as applicationServerKey.
        Empty string when push is not configured (front-end hides the toggle)."""
        return app.response_class(
            VAPID_PUBLIC_KEY or "", mimetype="text/plain"
        )

    @app.route("/push/subscribe", methods=["POST"])
    @login_required
    def push_subscribe():
        u = current_user()
        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")
        keys = data.get("keys") or {}
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        if not endpoint or not p256dh or not auth:
            return jsonify({"error": "invalid subscription"}), 400
        # Upsert by endpoint: the same browser re-subscribing (or a device that
        # switched accounts) reuses the row instead of duplicating.
        sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
        if sub:
            sub.user_id = u.id
            sub.p256dh = p256dh
            sub.auth = auth
        else:
            db.session.add(
                PushSubscription(
                    user_id=u.id, endpoint=endpoint, p256dh=p256dh, auth=auth
                )
            )
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/push/unsubscribe", methods=["POST"])
    @login_required
    def push_unsubscribe():
        u = current_user()
        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")
        if endpoint:
            PushSubscription.query.filter_by(
                endpoint=endpoint, user_id=u.id
            ).delete()
        else:
            # No endpoint given → drop all of this user's subscriptions.
            PushSubscription.query.filter_by(user_id=u.id).delete()
        db.session.commit()
        return jsonify({"ok": True})

    # ---- daily reminder cron (called by GitHub Actions on a schedule) ----
    @app.route("/internal/cron/daily-reminder", methods=["POST"])
    def cron_daily_reminder():
        """Nudge every member who still hasn't answered today's question.

        Protected by CRON_SECRET (header ``X-Cron-Secret`` or ``?token=``),
        compared with a constant-time check. Creates an in-app Notification and
        a Web Push for each un-answered member. Safe when a couple has no
        question today (only couples WITH today's question are considered)."""
        provided = request.headers.get("X-Cron-Secret") or request.args.get("token") or ""
        if not CRON_SECRET or not hmac.compare_digest(provided, CRON_SECRET):
            abort(403)

        today = date.today()
        questions = DailyQuestion.query.filter_by(q_date=today).all()
        couples_with_question = 0
        reminders_sent = 0
        for q in questions:
            couples_with_question += 1
            couple = q.couple
            if couple is None:
                continue
            for member in couple.approved_members:
                if q.answer_by(member.id) is not None:
                    continue  # already answered — no nudge
                try:
                    notify(
                        member,
                        "reminder",
                        "오늘의 질문에 아직 답 안 했어! 답해줘 💌",
                        url_for("today"),
                    )
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception("reminder notify failed for user %s", member.id)
                    continue
                send_push(
                    member,
                    "오늘의 질문 💌",
                    "오늘의 질문에 아직 답 안 했어! 답해줘",
                    "/today",
                )
                reminders_sent += 1
        return jsonify(
            {
                "couples_with_question": couples_with_question,
                "reminders_sent": reminders_sent,
            }
        )

    @app.route("/internal/cron/refresh-events", methods=["POST"])
    def cron_refresh_events():
        """'데이트 뉴스' 피드를 밤마다 갱신 — 서울 문화행사 upsert + 만료 삭제.

        cron_daily_reminder와 동일하게 CRON_SECRET(헤더 ``X-Cron-Secret`` 또는
        ``?token=``)으로 상수시간 비교 보호. 키가 없으면 500이 아니라 no_key로
        우아하게 빠진다. 동시 중복 실행은 모듈 락으로 막는다(비차단)."""
        provided = request.headers.get("X-Cron-Secret") or request.args.get("token") or ""
        if not CRON_SECRET or not hmac.compare_digest(provided, CRON_SECRET):
            abort(403)

        # 인증키가 없으면 피드는 빈 상태 — 크래시 대신 no_key로 알린다.
        if not events.events_enabled():
            return jsonify({"ok": False, "reason": "no_key"})

        # 동시 실행 방지(락 못 잡으면 이미 도는 중 — 조용히 skip).
        if not _events_refresh_lock.acquire(blocking=False):
            return jsonify({"ok": False, "reason": "busy"})
        try:
            items = events.fetch_seoul_events()
            fetched = len(items)
            upserted = 0
            for it in items:
                try:
                    row = EventItem.query.filter_by(
                        source=it["source"], source_uid=it["source_uid"]
                    ).first()
                    if row is None:
                        row = EventItem(source=it["source"], source_uid=it["source_uid"])
                        db.session.add(row)
                    # 존재하면 필드 갱신, 없으면 새 행에 채움.
                    row.title = it["title"]
                    row.category = it.get("category")
                    row.description = it.get("description")
                    row.place = it.get("place")
                    row.district = it.get("district")
                    row.image_url = it.get("image_url")
                    row.link = it.get("link")
                    row.fee = it.get("fee")
                    row.is_free = it.get("is_free")
                    row.start_date = it.get("start_date")
                    row.end_date = it.get("end_date")
                    db.session.commit()
                    upserted += 1
                except Exception:  # noqa: BLE001 — 한 행 실패가 전체를 막지 않게
                    db.session.rollback()
                    log.exception("event upsert failed (uid=%s)", it.get("source_uid"))

            # 만료 행 삭제(end_date가 있고 오늘보다 과거).
            expired_deleted = 0
            try:
                today = date.today()
                expired_deleted = (
                    EventItem.query.filter(
                        EventItem.end_date.isnot(None),
                        EventItem.end_date < today,
                    ).delete(synchronize_session=False)
                )
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("event expiry delete failed")

            # 선채점(비차단): 갱신 직후 활성 커플들의 미채점 진행 행사를 백그라운드
            # 데몬 스레드로 미리 채점해 둔다. 요청은 이 스레드를 기다리지 않고 즉시
            # 응답한다(선채점은 자기 안에서 모든 예외를 삼킨다).
            try:
                threading.Thread(
                    target=prewarm_scores,
                    args=(app,),
                    daemon=True,
                ).start()
            except Exception:  # noqa: BLE001 — 선채점 스폰 실패가 응답을 막지 않게
                log.exception("failed to spawn prewarm thread")

            return jsonify(
                {
                    "ok": True,
                    "fetched": fetched,
                    "upserted": upserted,
                    "expired_deleted": expired_deleted,
                }
            )
        finally:
            _events_refresh_lock.release()

    @app.route("/internal/cron/refresh-popups", methods=["POST"])
    def cron_refresh_popups():
        """팝업 수집을 '백그라운드로' 시작 — claude 웹검색은 ~120s+라 동기로 돌리면
        gunicorn 워커 타임아웃에 죽는다. 그래서 데몬 스레드를 띄우고 즉시 응답한다.

        cron_refresh_events와 '똑같이' CRON_SECRET(헤더 ``X-Cron-Secret`` 또는
        ``?token=``)으로 상수시간 비교 보호. 모듈 락을 비차단으로 잡아 두 번째
        트리거가 이미 도는 중이면 busy를 돌려준다(락은 워커가 finally에서 푼다)."""
        provided = request.headers.get("X-Cron-Secret") or request.args.get("token") or ""
        if not CRON_SECRET or not hmac.compare_digest(provided, CRON_SECRET):
            abort(403)

        # 동시 실행 방지(락 못 잡으면 이미 도는 중 — busy). 잡았으면 워커가 finally
        # 에서 푼다. 스레드 스폰이 실패하면 여기서 되돌려 락을 즉시 푼다.
        if not _popups_refresh_lock.acquire(blocking=False):
            return jsonify({"ok": False, "reason": "busy"})
        try:
            threading.Thread(
                target=refresh_popups_worker,
                args=(app,),
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001 — 스폰 실패 시 락 되돌리고 알림
            _popups_refresh_lock.release()
            log.exception("failed to spawn popup refresh thread")
            return jsonify({"ok": False, "reason": "spawn_failed"})
        # 120s 페치를 기다리지 않고 즉시 응답(백그라운드에서 진행).
        return jsonify({"ok": True, "started": True})

    # ---- PWA plumbing ----
    @app.route("/manifest.json")
    def manifest():
        name = Setting.get("app_name", DEFAULT_APP_NAME)
        return jsonify(
            {
                "name": name,
                "short_name": name,
                "start_url": "/",
                "scope": "/",
                "display": "standalone",
                "background_color": "#fff1f6",
                "theme_color": "#ff6b9d",
                "lang": "ko",
                "icons": [
                    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {
                        "src": "/static/icons/maskable-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                        "purpose": "maskable",
                    },
                ],
            }
        )

    @app.route("/sw.js")
    def service_worker():
        # Served from root so the SW scope covers the whole app.
        resp = send_from_directory(app.static_folder, "sw.js")
        resp.headers["Content-Type"] = "application/javascript"
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/offline")
    def offline():
        return render_template("offline.html")

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    # ---- themed error handlers ----
    @app.errorhandler(404)
    @app.errorhandler(405)
    def _not_found(e):
        # Missing page or wrong method → same gentle "not ready yet" state.
        return (
            render_template(
                "error.html",
                heading="여기엔 아무것도 없어",
                message="아직 준비되지 않은 기능이에요",
            ),
            404,
        )

    @app.errorhandler(413)
    def _too_large(e):
        # 요청이 MAX_CONTENT_LENGTH를 넘었을 때. AJAX 업로더면 그 파일만 "too_large"로
        # 알려 나머지는 계속 올리게 하고, 일반 페이지 로드면 친절한 에러 화면을 보여준다.
        # 사진뿐 아니라 동영상(Step 5)도 이 핸들러를 타므로 문구는 '파일'로 둔다.
        if _wants_json():
            return jsonify(ok=False, error="too_large",
                           reason="파일 1개는 50MB 이하만 올릴 수 있어."), 413
        return (
            render_template(
                "error.html",
                heading="파일이 너무 커",
                message="파일 1개는 50MB 이하만 올릴 수 있어. 조금 작은 파일로 다시 시도해줘.",
            ),
            413,
        )

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def _server_error(e):
        # Let HTTP errors (404/405/etc.) keep their own handling/status.
        from werkzeug.exceptions import HTTPException

        if isinstance(e, HTTPException):
            return e
        # Surface the real root cause in the logs (Render), never to the user.
        app.logger.exception("unhandled exception rendering %s", request.path)
        # 오염된 트랜잭션(죽은 커넥션 등)을 먼저 비워, 에러 페이지 렌더가
        # 같은 죽은 커넥션에 다시 걸려 무너지지 않게 한다.
        try:
            db.session.rollback()
        except Exception:
            log.debug("error handler: rollback 실패", exc_info=True)
        try:
            # context_processor가 이제 DB 장애 내성이 있어 정상 렌더돼야 하지만,
            # 그래도 실패하면 아래 인라인 폴백으로 넘어간다.
            return (
                render_template(
                    "error.html",
                    heading="이런, 문제가 생겼어",
                    message="에러가 발생했습니다",
                ),
                500,
            )
        except Exception:
            # 최후의 안전망: 템플릿·DB 없이도 테마 화면을 반드시 보여준다.
            # (원시 흰색 Werkzeug 500 페이지는 절대 노출하지 않는다.)
            log.exception("error handler: error.html 렌더 실패 — 인라인 폴백 사용")
            html = (
                "<!doctype html>"
                "<html lang=\"ko\"><head><meta charset=\"UTF-8\"/>"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>"
                "<title>문제가 생겼어</title><style>"
                "html,body{margin:0;height:100%}"
                "body{background:#fff5f8;color:#5a4a52;"
                "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
                "display:flex;align-items:center;justify-content:center;text-align:center;padding:24px}"
                ".box{max-width:22rem}"
                "h1{font-size:1.4rem;margin:0 0 .6rem;color:#ff6b9d}"
                "p{font-size:1rem;line-height:1.6;margin:0;color:#8a7a82}"
                "</style></head><body><div class=\"box\">"
                "<h1>이런, 문제가 생겼어</h1>"
                "<p>잠시 후 다시 시도해줘</p>"
                "</div></body></html>"
            )
            return html, 500, {"Content-Type": "text/html; charset=utf-8"}


# --------------------------------------------------------------------------- #
# AI 작업 큐 배선 — 느린 claude 작업은 **DB 큐**를 거쳐 한 줄로 돈다
# --------------------------------------------------------------------------- #
# 규율의 정본은 ``aijobs.py`` 머리말. 여기서 하는 일은 셋뿐이다:
#   (1) kind → 기존 워커 함수를 잇는다 (워커 본문은 한 줄도 안 바뀐다),
#   (2) 스폰 지점이 ``_enqueue_ai`` 로 '스레드 띄우기' 대신 '줄 세우기'를 한다,
#   (3) 펌프를 띄운다 (프로세스당 하나 · gunicorn 워커 1개가 이 앱의 전제).
def _register_ai_handlers():
    aijobs.register(
        "monthly",
        lambda a, p: regenerate_monthly_report(
            a, p["couple_id"], p["year"], p["month"]
        ),
    )
    aijobs.register(
        "question_upgrade",
        lambda a, p: upgrade_daily_question(a, p["question_id"]),
    )
    aijobs.register("caption", lambda a, p: caption_photo(a, p["photo_id"]))
    aijobs.register("judge", lambda a, p: judge_case(a, p["case_id"]))
    aijobs.register(
        "score", lambda a, p: score_events_for_couple(a, p["couple_id"])
    )
    aijobs.register(
        "recommend", lambda a, p: recommend_dates_for_couple(a, p["couple_id"])
    )
    aijobs.register("review", lambda a, p: generate_review(a, p["review_id"]))
    aijobs.register(
        "thumbnail", lambda a, p: generate_thumbnail_copy(a, p["review_id"])
    )
    # claude 를 안 쓰는 유일한 잡 — 상세 화면 그림을 미리 구워 둔다(프리렌더).
    aijobs.register(
        "prerender", lambda a, p: prerender_review_images(a, p["review_id"])
    )


_register_ai_handlers()


def _pump_should_start():
    """펌프를 띄울 환경인가.

    ``AI_JOB_PUMP`` 로 강제할 수 있고, 기본(auto)은 **pytest 안에서는 안 띄운다** —
    테스트는 큐를 직접(``aijobs.run_one``) 돌려 결정적으로 검증한다.
    """
    flag = (os.environ.get("AI_JOB_PUMP") or "auto").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return False
    if flag in ("1", "true", "on", "yes"):
        return True
    return "pytest" not in sys.modules


app = create_app()

if _pump_should_start():
    # 부팅 즉시 띄운다 — 지난 프로세스가 남긴 'running' 행을 되돌려 **아무도 화면을
    # 열지 않아도** 이어서 한다. 2026-10-07 사고('작성중' 영구 고착)의 구조적 해소점.
    aijobs.start_pump(app)

if __name__ == "__main__":
    # Local dev only. Production uses gunicorn (see Dockerfile / fly.toml).
    app.run(host="127.0.0.1", port=5000, debug=True)
