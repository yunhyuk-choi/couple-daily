"""onedrive.py — Microsoft Graph OneDrive client for couple-daily photo storage.

Access uses a PUBLIC client (the Microsoft Graph PowerShell app id, which
supports the device-code flow) plus a ROTATING refresh token — NO Azure app
registration, NO client secret, NO credit card. The refresh token is obtained
once out-of-band via device-code flow and seeded through the
``ONEDRIVE_REFRESH_TOKEN`` env var; from then on it lives in the DB and rotates
on every refresh (Microsoft returns a NEW refresh_token each time, and we MUST
persist it or the next refresh fails).

The app touches ONLY a dedicated ``couple-daily`` folder at the drive root — the
couple's other OneDrive files are never listed or read.

This module is pure network I/O (a file PUT / GET), NOT an AI/agent call, so the
route may call it synchronously on the request path (see CLAUDE.md — the "never
run AI on the request path" rule is about the slow `claude` subprocess, not a
bounded HTTP upload). Every call carries a sane timeout so it can never hang.
"""
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime

import requests

import bytecache
from models import Setting, db

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
# Microsoft Graph PowerShell — a first-party PUBLIC client that permits the
# device-code flow (no secret). Overridable via env, defaults to the public id.
PUBLIC_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
SCOPE = "Files.ReadWrite offline_access"
FOLDER = "couple-daily"

CLIENT_ID = os.environ.get("ONEDRIVE_CLIENT_ID", PUBLIC_CLIENT_ID)
# Initial seed only — after the first refresh the DB copy is authoritative.
SEED_REFRESH_TOKEN = os.environ.get("ONEDRIVE_REFRESH_TOKEN")

_SETTING_KEY = "onedrive_refresh_token"

HTTP_TIMEOUT = 30  # seconds — file PUTs need headroom, but never hang forever

# ---- in-memory caches (single gunicorn worker on Render free tier) ----
_lock = threading.Lock()
_access_token = None
_access_expiry = 0.0  # epoch seconds; refresh a little before this
_reconnect_needed = False

# short-lived per-item IMAGE-BYTES cache for the backend image proxy. We cache
# the fetched BYTES (not a download URL): personal-OneDrive pre-auth download
# URLs expire very quickly, so a cached URL would 401 — but cached bytes are
# always a valid response. Kept brief; the browser also caches via Cache-Control.
#
# ⚠️ **이 캐시도 RAM 이 아니라 디스크에 산다**(app 의 가공완료 캐시와 같은 장치 —
# bytecache.DiskByteCache 머리말이 근거의 단일 원천). 옛 상한은 "memory can't grow
# unbounded" 라고 적힌 **개수 64개** 였는데 실제로는 아무것도 묶지 못했다 — 실측
# 아이폰 12MP 원본 한 장이 3.03MB 라 64개면 **197MB 상주**다. 거기에 `claude -p`
# 한 개가 실측 약 400MB 라, 이 캐시 하나가 512MB 티어를 넘기는 데 충분했다.
# 담는 양을 줄이는 대신 자리를 옮겼다: 예산 96MB(그런 원본 ~31장, 옛 64개와 같은
# 급의 용량)를 디스크에 두고 **상주 RAM 은 0** 이다.
_BYTES_CACHE_TTL = 1800  # seconds
_BYTES_CACHE_BUDGET = 96 * 1024 * 1024
_bytes_cache = bytecache.DiskByteCache(
    "onedrive-bytes", ttl=_BYTES_CACHE_TTL, budget=_BYTES_CACHE_BUDGET,
    directory=os.environ.get("ONEDRIVE_BYTES_CACHE_DIR"),
)
# 바이트는 디스크에, Content-Type 문자열만 여기에. 항목당 수십 바이트라 메모리
# 사고와 무관하다(상한은 폭주 방지용이고, 넘치면 통째로 비운다 — 재조회는 값싸다).
_BYTES_CTYPE_MAX = 4096
_bytes_ctype: dict[str, str] = {}

# per-(item,size) THUMBNAIL-BYTES cache for the gallery grid. Thumbnails are
# tiny (a few KB), so we can cache more of them and for longer than full bytes.
# Same rationale as _bytes_cache: cache the decoded bytes (the Graph thumbnail
# `url` is a short-lived CDN link that would 401 if reused).
# 여기는 개수 상한을 그대로 둔다 — 엔트리 크기가 균일하게 작아 개수가 곧 바이트다
# (실측: Graph 'medium' 썸네일 4.4KB · 192개가 가득 차도 **0.83MB**). 위 캐시와 달리
# 메모리 사고의 용의자가 아니다.
_THUMB_CACHE_TTL = 3600  # seconds
_THUMB_CACHE_MAX = 192
_thumb_cache: dict[tuple[str, str], tuple[bytes, str, float]] = {}

# 아이템 **메타데이터** 캐시 — 픽셀이 아니라 숫자 몇 개(이름·크기·가로세로·촬영일)만
# 담는다. 항목당 200바이트 남짓이라 1024개가 가득 차도 0.2MB 수준이고, 이 캐시의
# 목적은 메모리 절약이 아니라 **Graph 왕복 횟수를 지키는 것**이다(바이트 캐시를
# 디스크로 내보내고 프록시를 스트리밍으로 바꾸면서, 예전엔 바이트 캐시가 덤으로
# 막아 주던 '같은 사진 메타 재조회'가 노출됐다).
_META_CACHE_TTL = 900  # seconds
_META_CACHE_MAX = 1024
_meta_cache: dict[str, tuple[dict, float]] = {}

# Graph 커스텀 렌디션을 받아들이기 전에 **원본 비율과 대조**할 때 허용하는 오차.
# `c{W}x{H}` 는 문서상 '박스 안에 들어가도록 비율 유지'지만, 문서를 믿고 끝내지
# 않는다 — 돌아온 가로세로를 원본과 대조해 어긋나면 **쓰지 않는다**(잘린 그림을
# 조용히 내보내느니 원본 경로로 폴백한다).
_RENDITION_ASPECT_TOL = 0.02


class OneDriveError(RuntimeError):
    """Any OneDrive/Graph failure surfaced to the caller."""


def _stored_refresh_token():
    """The current refresh token: DB copy if present, else the env seed."""
    tok = Setting.get(_SETTING_KEY)
    if tok:
        return tok
    return SEED_REFRESH_TOKEN or None


def onedrive_enabled() -> bool:
    """True when a usable refresh token exists (DB copy or env seed).

    Cheap: one Setting read + an env check, NO network. Gates the nav entry and
    the /memories UI so the app never crashes when OneDrive is unconfigured.
    """
    try:
        return _stored_refresh_token() is not None
    except Exception:  # noqa: BLE001 — a gate check must never crash a page
        log.exception("onedrive_enabled check failed")
        return False


def reconnect_needed() -> bool:
    """True if the last refresh attempt found the token dead (invalid_grant).

    Distinct from ``onedrive_enabled``: a token can be configured yet expired/
    revoked, in which case the user must re-run the device-code flow.
    """
    return _reconnect_needed


def get_access_token() -> str:
    """Return a valid Graph access token, refreshing when needed.

    Reads the refresh token from the DB (fallback env seed), POSTs a refresh,
    PERSISTS the rotated refresh token back to the DB, and caches the access
    token in memory until ~10 min before expiry (≈50 min). Raises OneDriveError
    on failure and flips the reconnect-needed flag when the token itself is dead.
    """
    global _access_token, _access_expiry, _reconnect_needed
    with _lock:
        now = time.time()
        if _access_token and now < _access_expiry:
            return _access_token

        refresh_token = _stored_refresh_token()
        if not refresh_token:
            _reconnect_needed = True
            raise OneDriveError("no OneDrive refresh token configured")

        try:
            resp = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": refresh_token,
                    "scope": SCOPE,
                },
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            # Network hiccup — the token may still be good; don't flip reconnect.
            raise OneDriveError(f"token refresh request failed: {e}") from e

        if resp.status_code != 200:
            # 400 invalid_grant → the refresh token is dead; re-connect required.
            if resp.status_code == 400:
                _reconnect_needed = True
            log.error("OneDrive token refresh failed (status=%s)", resp.status_code)
            raise OneDriveError(
                f"token refresh rejected (status={resp.status_code})"
            )

        payload = resp.json()
        access = payload.get("access_token")
        new_refresh = payload.get("refresh_token")
        expires_in = int(payload.get("expires_in") or 3600)
        if not access:
            _reconnect_needed = True
            raise OneDriveError("token refresh returned no access_token")

        # Persist the ROTATED refresh token (Microsoft issues a new one each
        # time). If this ever fails to save, the NEXT refresh would reuse the old
        # token which Microsoft may reject — so persistence is load-bearing.
        if new_refresh and new_refresh != refresh_token:
            try:
                Setting.set(_SETTING_KEY, new_refresh)
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("failed to persist rotated OneDrive refresh token")

        _access_token = access
        # Cache until ~10 min before expiry (typ. 3600s → ≈50 min usable).
        _access_expiry = now + min(expires_in, 3600) - 600
        _reconnect_needed = False
        return access


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}


def _json_body(r, what):
    """``r.json()`` 을 **절대 raw 예외로 새지 않게** 읽는다 — 실패는 OneDriveError.

    ⛔ 2026-10-07 운영 500 의 정체가 이것이었다. Graph 가 200 으로 **JSON 이 아닌 것**
    (에러 페이지·빈 본문·잘린 JSON)을 돌려주면 ``requests`` 가
    ``JSONDecodeError`` 를 던진다. 그건 ``OneDriveError`` 가 아니라서 호출부의
    ``except onedrive.OneDriveError`` 폴백에 **안 걸리고** 그대로 500 이 됐다
    (``/blog-img`` 본문 이미지가 전부 엑박 — 설계돼 있던 원본 폴백에 영영 못 닿았다).

    이 모듈 밖으로 나가는 실패는 **언제나 OneDriveError 하나**여야 한다. 그래야
    호출부가 "그럼 다른 길로"를 할 수 있다. 응답 앞머리를 로그에 남겨 다음 사람이
    'Graph 가 뭘 줬는지'를 추측하지 않게 한다(민감값 없음 — 에러 본문이다).
    """
    try:
        return r.json() or {}
    except Exception as e:  # noqa: BLE001 — JSONDecodeError/ValueError 등 전부
        head = (r.text or "")[:200].replace("\n", " ")
        ctype = r.headers.get("Content-Type") if r.headers else None
        raise OneDriveError(
            f"{what}: non-JSON response (status={r.status_code} "
            f"ctype={ctype} head={head!r})"
        ) from e


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_unique_name(filename: str) -> str:
    """A collision-proof, sanitized OneDrive item name.

    Prefix a UTC timestamp + short uuid so names are globally unique, then strip
    the original name to a safe charset (OneDrive forbids \\ / : * ? " < > |).
    """
    base = os.path.basename(filename or "photo")
    base = _SAFE_NAME_RE.sub("_", base).strip("._") or "photo"
    base = base[:80]
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:8]}_{base}"


def _ensure_folder():
    """Create the dedicated ``couple-daily`` folder at the drive root if missing.

    Graph does NOT auto-create parent folders for a path upload, so this runs
    before the first PUT. Idempotent; never touches any other folder.
    """
    r = requests.get(
        f"{GRAPH_BASE}/me/drive/root:/{FOLDER}",
        headers=_auth_headers(),
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code == 200:
        return
    if r.status_code != 404:
        raise OneDriveError(f"folder check failed (status={r.status_code})")
    r = requests.post(
        f"{GRAPH_BASE}/me/drive/root/children",
        headers={**_auth_headers(), "Content-Type": "application/json"},
        json={
            "name": FOLDER,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        },
        timeout=HTTP_TIMEOUT,
    )
    # 409 == someone/another request already created it — fine.
    if r.status_code not in (200, 201, 409):
        raise OneDriveError(f"folder create failed (status={r.status_code})")


def upload_photo(file_bytes: bytes, filename: str):
    """Upload bytes to ``couple-daily/<unique-name>``.

    Returns ``(item_id, stored_name)`` — the OneDrive drive-item id (the
    load-bearing handle for fetch/delete) and the sanitized unique name we
    actually stored it under (so the caller can record the true name, not a
    re-derived guess). Simple PUT upload (fine for the ≤50 MB photos the route
    caps at; Graph simple upload allows up to 250 MB). Ensures the dedicated
    folder exists first. Raises OneDriveError on
    any failure.
    """
    try:
        _ensure_folder()
        name = _safe_unique_name(filename)
        url = f"{GRAPH_BASE}/me/drive/root:/{FOLDER}/{name}:/content"
        r = requests.put(
            url,
            headers={**_auth_headers(), "Content-Type": "application/octet-stream"},
            data=file_bytes,
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code not in (200, 201):
            raise OneDriveError(f"upload failed (status={r.status_code})")
        item = r.json()
        item_id = item.get("id")
        if not item_id:
            raise OneDriveError("upload returned no item id")
        # Prefer the name Graph reports back; fall back to what we asked for.
        return item_id, item.get("name") or name
    except OneDriveError:
        raise
    except requests.RequestException as e:
        raise OneDriveError(f"upload request failed: {e}") from e


def upload_photo_stream(stream, size, filename):
    """파일류 객체를 ``couple-daily/<unique-name>`` 으로 **메모리에 담지 않고** 올린다.

    ``upload_photo(file_bytes, …)`` 의 스트리밍 판이다. 사진 업로드 경로가
    ``file.read()`` 로 최대 50MB 를 파이썬 힙에 올리던 것을 없애려고 만들었다
    (영상이 이미 ``upload_stream`` 으로 하던 것과 같은 사상).

    크기에 따라 두 길로 간다 — **왕복 횟수를 괜히 늘리지 않으려고** 가른다:

      * ``size <= _CHUNK``(3.2MiB, 사진 대부분) → **단순 PUT 한 번**. 본문으로
        스트림 객체를 그대로 넘기고 ``Content-Length`` 를 명시하면 requests 가
        조각내어 보낸다 — 바이트가 변수에 담기지 않는다.
      * 그보다 크면 → ``upload_stream``(업로드 세션 청크 PUT). 한 번에 메모리에
        있는 것은 청크 하나뿐이다.

    반환·예외는 ``upload_photo`` 와 같다 — ``(item_id, stored_name)`` / OneDriveError.
    """
    if size is None or size <= 0:
        raise OneDriveError("upload stream has no bytes")
    if size > _CHUNK:
        return upload_stream(stream, size, filename)
    try:
        _ensure_folder()
        name = _safe_unique_name(filename)
        url = f"{GRAPH_BASE}/me/drive/root:/{FOLDER}/{name}:/content"
        r = requests.put(
            url,
            headers={
                **_auth_headers(),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(int(size)),
            },
            data=stream,
            timeout=UPLOAD_TIMEOUT,
        )
        if r.status_code not in (200, 201):
            raise OneDriveError(f"upload failed (status={r.status_code})")
        item = r.json()
        item_id = item.get("id")
        if not item_id:
            raise OneDriveError("upload returned no item id")
        return item_id, item.get("name") or name
    except OneDriveError:
        raise
    except requests.RequestException as e:
        raise OneDriveError(f"upload request failed: {e}") from e


def get_download_url(item_id: str):
    """Return a short-lived direct download URL for an item.

    Graph exposes ``@microsoft.graph.downloadUrl`` — a pre-authenticated CDN
    link (needs no Authorization header, lives ~1h). Ideal for <img src>.
    """
    try:
        r = requests.get(
            f"{GRAPH_BASE}/me/drive/items/{item_id}",
            headers=_auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise OneDriveError(f"item fetch failed (status={r.status_code})")
        return r.json().get("@microsoft.graph.downloadUrl")
    except requests.RequestException as e:
        raise OneDriveError(f"download-url request failed: {e}") from e


def get_photo_content(item_id: str):
    """Fetch an item's raw image bytes SERVER-SIDE, always via a FRESH link.

    Uses ``GET /me/drive/items/<id>/content`` which 302-redirects to a fresh,
    short-lived pre-authenticated download URL; ``requests`` follows the redirect
    by default (and drops the Bearer header on the cross-host hop, which is
    correct — the redirect target is already pre-authenticated). This never
    reuses a stale URL, so it can't serve an expired 401 body — the fix for
    personal-OneDrive URLs that expire almost immediately.

    Returns ``(bytes, content_type)``, or ``(None, None)`` if the item is gone.
    """
    try:
        r = requests.get(
            f"{GRAPH_BASE}/me/drive/items/{item_id}/content",
            headers=_auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            raise OneDriveError(f"content fetch failed (status={r.status_code})")
        return r.content, r.headers.get("Content-Type")
    except requests.RequestException as e:
        raise OneDriveError(f"content request failed: {e}") from e


def get_photo_content_cached(item_id: str):
    """``get_photo_content`` with a brief BYTES cache (on **disk**, not in RAM).

    Avoids refetch storms (e.g. a reload before the browser cache warms) without
    ever serving an expired URL — the cached value is the decoded bytes, always
    valid. Returns ``(bytes, ctype)`` or ``(None, None)`` if the item is gone.

    바이트는 디스크에, Content-Type 문자열만 **아주 작은** RAM 사이드 테이블에
    둔다(항목당 수십 바이트). ctype 을 잃어도 호출부가 파일명으로 되돌릴 수 있게
    설계돼 있지만, 서버가 준 값을 그대로 쓰는 편이 정확하다.
    """
    hit = _bytes_cache.get(item_id)
    if hit is not None:
        return hit, _bytes_ctype.get(item_id) or None
    data, ctype = get_photo_content(item_id)
    if data:
        _bytes_cache.put(item_id, data)
        if ctype:
            if len(_bytes_ctype) >= _BYTES_CTYPE_MAX:
                _bytes_ctype.clear()  # 문자열 테이블이라 통째로 비워도 싸다
            _bytes_ctype[item_id] = ctype
    return data, ctype


def get_thumbnail(item_id: str, size: str = "medium"):
    """Fetch a SMALL server-rendered thumbnail's bytes for a drive item.

    Graph exposes ``GET /me/drive/items/<id>/thumbnails/0/<size>`` (size is one
    of ``small`` / ``medium`` / ``large``) which returns a thumbnail resource
    whose ``url`` is a short-lived, pre-authenticated CDN link. We fetch THAT
    small image server-side so the gallery grid never proxies the full-res
    original. Returns ``(bytes, content_type)``, or ``(None, None)`` when the
    item has no thumbnail yet / is gone — the route then falls back to the full
    image. A brief in-process bytes cache absorbs refetch storms (we cache the
    decoded bytes, never the expiring URL).
    """
    now = time.time()
    key = (item_id, size)
    hit = _thumb_cache.get(key)
    if hit and now < hit[2]:
        return hit[0], hit[1]
    try:
        r = requests.get(
            f"{GRAPH_BASE}/me/drive/items/{item_id}/thumbnails/0/{size}",
            headers=_auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
        # 404 == item gone OR no thumbnail available for it yet.
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            raise OneDriveError(f"thumbnail meta fetch failed (status={r.status_code})")
        url = r.json().get("url")
        if not url:
            return None, None
        # The thumbnail url is already pre-authenticated — fetch WITHOUT the
        # Bearer header (sending it to the CDN host would be wrong).
        ir = requests.get(url, timeout=HTTP_TIMEOUT)
        if ir.status_code != 200:
            raise OneDriveError(f"thumbnail fetch failed (status={ir.status_code})")
        data = ir.content
        ctype = ir.headers.get("Content-Type") or "image/jpeg"
    except requests.RequestException as e:
        raise OneDriveError(f"thumbnail request failed: {e}") from e

    if data:
        # Evict expired / oldest before inserting to bound memory.
        if len(_thumb_cache) >= _THUMB_CACHE_MAX:
            for k in [k for k, v in _thumb_cache.items() if v[2] <= now]:
                _thumb_cache.pop(k, None)
            if len(_thumb_cache) >= _THUMB_CACHE_MAX:
                oldest = min(_thumb_cache, key=lambda k: _thumb_cache[k][2])
                _thumb_cache.pop(oldest, None)
        _thumb_cache[key] = (data, ctype, now + _THUMB_CACHE_TTL)
    return data, ctype


# --------------------------------------------------------------------------- #
# 메타데이터 · 렌디션 — **서버가 원본 픽셀을 만지지 않기 위한 두 설비**
# --------------------------------------------------------------------------- #
# 이 앱의 이미지 경로는 두 가지만 있으면 원본 바이트를 안 열어도 된다:
#
#   (1) 사진의 **표시 기준 가로세로** — 크롭 사각형 계산은 정규화 좌표라
#       *비율만* 알면 된다. Graph 의 ``image`` 패싯이 숫자로 알려 준다.
#       예전엔 이걸 알려고 3MB 원본을 받아 Pillow 로 열었다.
#   (2) **작은 다운스케일본** — Graph 가 서버에서 렌더해 준다(우리 CPU·메모리 0).
#       미리보기·크롭 UI·비전 판단에는 원본 해상도가 필요 없다.
#
# 원본 바이트가 정말 필요한 곳은 **발행용 크롭 산출** 한 곳뿐인데, 그건 이제
# 브라우저가 Canvas 로 한다(app.py 의 blog_img 머리말 참고). 그래서 서버는
# 원본을 '흘려보내기만' 한다 — 디코드하지 않는다.

_META_SELECT = "id,name,size,file,image,photo,eTag"


def _put_meta(item_id, meta):
    if len(_meta_cache) >= _META_CACHE_MAX:
        now = time.time()
        for k in [k for k, v in _meta_cache.items() if v[1] <= now]:
            _meta_cache.pop(k, None)
        if len(_meta_cache) >= _META_CACHE_MAX:
            _meta_cache.clear()  # 숫자 몇 개짜리 테이블이라 통째로 비워도 싸다
    _meta_cache[item_id] = (meta, time.time() + _META_CACHE_TTL)


def get_item_meta(item_id: str, refresh: bool = False):
    """드라이브 아이템의 **메타데이터만** — 픽셀을 한 바이트도 받지 않는다.

    ``GET /me/drive/items/<id>?$select=...`` 는 1KB 남짓의 JSON 을 돌려준다.
    여기서 꺼내 쓰는 것:

      * ``width``/``height`` — Graph 의 ``image`` 패싯. **EXIF 방향이 적용된
        표시 기준**이 아니라 '저장된' 픽셀 크기일 수 있으므로, 방향이 중요한
        계산에는 렌디션의 가로세로(이미 똑바로 세워져 온다)를 우선한다.
      * ``taken_at`` — ``photo.takenDateTime``. Graph 가 EXIF 를 서버에서 읽어
        준다. 업로드 경로가 바이트를 안 읽고도 촬영일을 채울 수 있는 길.
      * ``size``/``ctype``/``etag`` — 스트리밍 프록시의 조건부 응답용.

    없으면 ``None``. 실패는 OneDriveError.
    """
    if not item_id:
        return None
    if not refresh:
        hit = _meta_cache.get(item_id)
        if hit and time.time() < hit[1]:
            return hit[0]
    try:
        r = requests.get(
            f"{GRAPH_BASE}/me/drive/items/{item_id}?$select={_META_SELECT}",
            headers=_auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise OneDriveError(f"item meta request failed: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise OneDriveError(f"item meta fetch failed (status={r.status_code})")
    body = _json_body(r, "item meta")
    image = body.get("image") or {}
    photo = body.get("photo") or {}
    file_facet = body.get("file") or {}
    taken = photo.get("takenDateTime")
    taken_dt = None
    if taken:
        try:
            taken_dt = datetime.fromisoformat(str(taken).replace("Z", "+00:00"))
            if taken_dt.tzinfo is not None:
                taken_dt = taken_dt.replace(tzinfo=None)  # 앱 전체가 naive UTC
        except (TypeError, ValueError):
            taken_dt = None
    meta = {
        "id": body.get("id") or item_id,
        "name": body.get("name") or "",
        "size": int(body.get("size") or 0),
        "ctype": file_facet.get("mimeType") or "",
        "width": int(image.get("width") or 0) or None,
        "height": int(image.get("height") or 0) or None,
        "taken_at": taken_dt,
        "etag": body.get("eTag") or "",
    }
    _put_meta(item_id, meta)
    return meta


def rendition_name(long_edge) -> str:
    """긴 변이 ``long_edge`` 를 넘지 않는 **비율 유지** 렌디션의 Graph 이름.

    Microsoft 문서: ``c{W}x{H}`` = "Generate a thumbnail that fits inside a
    WxH pixel box, maintaining aspect ratio". (``_crop`` 접미사는 **채우고
    잘라낸다** — 우리는 절대 쓰지 않는다. 자르는 위치를 우리가 정해야 하므로.)
    정사각 박스를 주면 가로·세로 어느 쪽이 길든 그 변이 상한이 된다.
    """
    n = max(1, int(long_edge))
    return f"c{n}x{n}"


# Graph 가 **이름으로** 내주는 기본 렌디션 — 큰 것부터. 커스텀 ``c{W}x{H}`` 를
# 받지 않는 드라이브에서 내려갈 사다리의 두 번째 칸이다(세 번째 칸은 원본).
_NAMED_RENDITIONS = ("large", "medium", "small")

# 이 드라이브가 커스텀 스펙을 받아 주나 — None=아직 모름 / True / False.
# ⛔ 왜 기억하나: 안 받는 드라이브에서는 **매 요청마다** 똑같이 실패하고 Graph 왕복
# 비용만 치른다(실측: 개인 OneDrive 가 ``c1707x1707`` 에 JSON 아닌 응답을 줬다).
# 한 번 알아내면 그 뒤로는 바로 named 로 간다. 프로세스 재시작이면 다시 모르는
# 상태로 돌아가는데, 그게 맞다 — 드라이브·서비스가 바뀌었을 수 있다.
_custom_renditions_ok = None
_CUSTOM_RENDITION_ENV = (os.environ.get("ONEDRIVE_CUSTOM_RENDITIONS") or "").strip()


def custom_renditions_supported():
    """지금까지 관찰한 '이 드라이브가 커스텀 렌디션을 받나' (모르면 None)."""
    if _CUSTOM_RENDITION_ENV in ("0", "off", "false", "no"):
        return False
    if _CUSTOM_RENDITION_ENV in ("1", "on", "true", "yes"):
        return True
    return _custom_renditions_ok


def _note_custom_rendition(ok):
    global _custom_renditions_ok
    if _custom_renditions_ok is ok:
        return
    _custom_renditions_ok = ok
    log.info("OneDrive 커스텀 렌디션 지원 = %s (관찰값)", ok)


def get_rendition_meta(item_id: str, spec: str):
    """렌디션 **메타데이터**(가로·세로·URL) — 바이트는 아직 안 받는다.

    ``{"width":…, "height":…, "url":…}`` 또는 지원 안 함/없음이면 ``None``.
    이 단계에서 크기를 먼저 알 수 있어, **원하는 해상도가 아니면 바이트를
    아예 안 받고** 다음 후보로 넘어갈 수 있다.

    ⛔ 실패는 **언제나 OneDriveError** 다. 예전엔 ``r.json()`` 이 그대로 터져
    ``JSONDecodeError`` 가 모듈 밖으로 샜고, 호출부의 폴백이 그걸 못 잡아 운영에서
    ``/blog-img`` 가 500 을 냈다(``_json_body`` 머리말).
    """
    try:
        r = requests.get(
            f"{GRAPH_BASE}/me/drive/items/{item_id}/thumbnails/0/{spec}",
            headers=_auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise OneDriveError(f"rendition meta request failed: {e}") from e
    # 404 = 아이템이 없거나 이 크기를 못 만든다. 400 = 이름을 거부했다.
    if r.status_code in (400, 404):
        return None
    if r.status_code != 200:
        raise OneDriveError(f"rendition meta failed (status={r.status_code})")
    body = _json_body(r, f"rendition meta ({spec})")
    url = body.get("url")
    try:
        w = int(body.get("width") or 0)
        h = int(body.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if not url or w <= 0 or h <= 0:
        return None
    return {"width": w, "height": h, "url": url}


def _rendition_meta_or_none(item_id, spec, custom):
    """``get_rendition_meta`` 를 **절대 raise 하지 않게** 감싼다.

    커스텀 스펙에서 실패하면 "이 드라이브는 커스텀을 안 받는다"로 기억해 다음
    요청부터 바로 named 사다리로 내려간다. 성공하면 반대로 기억한다.
    """
    try:
        meta = get_rendition_meta(item_id, spec)
    except Exception:  # noqa: BLE001 — 렌디션은 최적화지 전제가 아니다
        log.warning("렌디션 메타 조회 실패 (item=%s spec=%s)", item_id, spec,
                    exc_info=True)
        if custom:
            _note_custom_rendition(False)
        return None
    if custom:
        _note_custom_rendition(meta is not None)
    return meta


def get_rendition(item_id: str, long_edge, min_long_edge=None,
                  expect_aspect=None):
    """Graph 가 **서버에서 렌더한** 다운스케일본 — ``(bytes, ctype, (w, h))``.

    우리 CPU·메모리를 전혀 쓰지 않고 작은 JPEG 를 얻는 길이다. HEIC 원본도
    Graph 가 알아서 JPEG 로 내준다(브라우저가 못 여는 포맷 문제도 같이 풀린다).

    **주장하지 않고 검증한다** — 돌아온 가로세로를 보고 두 가지를 확인한다:

      * 비율이 원본과 같은가 (``expect_aspect``, 없으면 ``image`` 패싯에서).
        어긋나면 ``None`` — 문서를 잘못 읽었거나 서비스가 바뀌어도 **잘린
        그림을 조용히 내보내지 않는다.**
      * 긴 변이 ``min_long_edge`` 이상인가. Graph 는 "요청한 크기와 정확히
        같지 않을 수 있다"고 명시하고 **상한이 문서화돼 있지 않다** — 그래서
        한도는 추측하지 않고 *돌아온 값으로* 안다. 모자라면 ``None`` 을
        돌려주고, 호출부가 더 큰 후보나 원본으로 간다.

    못 쓰면 ``None``(예외 아님 — 폴백이 정상 경로다). **그 약속을 이제 정말 지킨다** —
    예전엔 바이트 받기가 ``OneDriveError`` 를 던졌고, 메타 파싱은 raw
    ``JSONDecodeError`` 까지 흘렸다(운영 500 의 원인).

    **사다리**: 커스텀 ``c{N}x{N}`` → named(large/medium/small) → ``None``(호출부가
    원본으로). 커스텀을 안 받는 드라이브로 판명되면 그 뒤로는 바로 named 부터 본다.
    """
    want = expect_aspect
    if want is None:
        try:
            im = get_item_meta(item_id)
        except OneDriveError:
            im = None
        if im and im.get("width") and im.get("height"):
            a = im["width"] / im["height"]
            # ``image`` 패싯은 EXIF 회전 전 값일 수 있다 — 세로/가로 어느 쪽이든
            # 같은 '비율 쌍'으로 보고 둘 다 허용한다(회전만 다른 것은 정상).
            want = (a, 1.0 / a) if a else None
        else:
            want = None
    elif not isinstance(want, tuple):
        want = (float(want), 1.0 / float(want))

    specs = []
    if custom_renditions_supported() is not False:
        specs.append((rendition_name(long_edge), True))
    specs.extend((name, False) for name in _NAMED_RENDITIONS)

    for spec, custom in specs:
        meta = _rendition_meta_or_none(item_id, spec, custom)
        if not meta:
            continue
        w, h = meta["width"], meta["height"]
        if want:
            got = w / h if h else 0
            if not got or not any(abs(got / cand - 1.0) <= _RENDITION_ASPECT_TOL
                                  for cand in want if cand):
                log.warning(
                    "렌디션 비율이 원본과 다르다 — 쓰지 않는다 "
                    "(item=%s spec=%s got=%sx%s)", item_id, spec, w, h,
                )
                continue
        if min_long_edge and max(w, h) < int(min_long_edge):
            # 요구 해상도에 못 미친다. named 는 이것보다 **더 작으므로** 더 볼 게
            # 없다 — 괜히 Graph 를 세 번 더 왕복하지 않고 바로 원본으로 넘긴다.
            return None
        try:
            # 렌디션 URL 은 이미 인증된 CDN 링크다 — Bearer 를 붙이지 않는다.
            ir = requests.get(meta["url"], timeout=HTTP_TIMEOUT)
            if ir.status_code != 200:
                raise OneDriveError(
                    f"rendition fetch failed (status={ir.status_code})"
                )
            return (ir.content,
                    (ir.headers.get("Content-Type") or "image/jpeg"), (w, h))
        except Exception:  # noqa: BLE001 — 바이트를 못 받으면 다음 후보/원본으로
            log.warning("렌디션 바이트 수신 실패 (item=%s spec=%s)", item_id, spec,
                        exc_info=True)
            continue
    return None


def probe_renditions(item_id: str, long_edges=None):
    """**실측 도구**: 이 드라이브가 어느 커스텀 렌디션 크기까지 실제로 내주는가.

    Microsoft 문서는 ``c{W}x{H}`` 의 **상한을 적어 두지 않았고**, "요청한 것보다
    크거나 작은 것이 올 수 있다"고만 말한다. 그러니 한도는 **재서** 안다.
    운영 환경에서 ``tools/probe_graph_renditions.py`` 로 한 번 돌리면 된다.

    ⚠️ 반환값에 **URL 을 절대 담지 않는다** — pre-auth CDN 링크는 그 자체가
    자격증명이다(``probe_direct_cors`` 와 같은 규율).

    반환: ``{"original": {w,h,size}, "renditions": [{spec, ok, width, height,
    bytes, aspect_ok}, …]}``
    """
    edges = list(long_edges or (800, 1280, 1600, 1920, 2048, 2560,
                                3200, 4096, 8192, 16384))
    try:
        meta = get_item_meta(item_id, refresh=True)
    except OneDriveError as err:
        # 원본 메타를 못 얻어도 **렌디션 표는 뽑는다** — 이 도구의 요점은 한도다.
        log.warning("probe: 원본 메타 조회 실패 — 비율 판정 없이 진행 (%s)", err)
        meta = None
    out = {
        "original": {
            "width": (meta or {}).get("width"),
            "height": (meta or {}).get("height"),
            "size": (meta or {}).get("size"),
        },
        "renditions": [],
    }
    oa = None
    if meta and meta.get("width") and meta.get("height"):
        oa = meta["width"] / meta["height"]
    for e in edges:
        spec = rendition_name(e)
        row = {"spec": spec, "ok": False, "width": None, "height": None,
               "bytes": None, "aspect_ok": None}
        try:
            rm = get_rendition_meta(item_id, spec)
        except OneDriveError as err:
            row["error"] = str(err)[:120]
            out["renditions"].append(row)
            continue
        if not rm:
            out["renditions"].append(row)
            continue
        row.update(ok=True, width=rm["width"], height=rm["height"])
        if oa:
            got = rm["width"] / rm["height"]
            row["aspect_ok"] = (abs(got / oa - 1) <= _RENDITION_ASPECT_TOL
                                or abs(got * oa - 1) <= _RENDITION_ASPECT_TOL)
        try:
            ir = requests.get(rm["url"], timeout=HTTP_TIMEOUT)
            row["bytes"] = len(ir.content) if ir.status_code == 200 else None
        except requests.RequestException:
            pass
        out["renditions"].append(row)
    return out


def delete_photo(item_id: str):
    """Delete an item from OneDrive. A 404 (already gone) counts as success."""
    _bytes_cache.delete(item_id)
    _bytes_ctype.pop(item_id, None)
    for k in [k for k in _thumb_cache if k[0] == item_id]:
        _thumb_cache.pop(k, None)
    try:
        r = requests.delete(
            f"{GRAPH_BASE}/me/drive/items/{item_id}",
            headers=_auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code not in (204, 404):
            raise OneDriveError(f"delete failed (status={r.status_code})")
    except requests.RequestException as e:
        raise OneDriveError(f"delete request failed: {e}") from e


def list_folder():
    """List items in the ``couple-daily`` folder (id, name, size).

    ONLY this folder — never the rest of the drive. Returns [] when the folder
    is empty or absent. Mainly used by tests/verification.
    """
    try:
        r = requests.get(
            f"{GRAPH_BASE}/me/drive/root:/{FOLDER}:/children?$select=id,name,size,file",
            headers=_auth_headers(),
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise OneDriveError(f"list failed (status={r.status_code})")
        return r.json().get("value", [])
    except requests.RequestException as e:
        raise OneDriveError(f"list request failed: {e}") from e


# --------------------------------------------------------------------------- #
# 동영상 — 사진과 같은 폴더에 보관하되 **바이트는 절대 통째로 메모리에 올리지 않는다**
# --------------------------------------------------------------------------- #
# 사진은 ≤50MB를 bytes 로 읽어 한 번에 PUT 해도 됐다. 동영상은 같은 상한이어도
# Render 무료티어(512MB · 0.1 CPU)에서 "업로드 바이트 + 요청 버퍼 + 파이썬 힙"이
# 겹치면 위험하다. 그래서 동영상은 **업로드 세션(청크 PUT)** 으로 올리고,
# 내려줄 때도 **스트리밍(Range 통과)** 으로 흘린다. 어느 경로에도 전체 바이트를
# 담는 변수가 없다.
#
# ⛔ 동영상은 ``_bytes_cache`` 에 넣지 않는다 — 수십 MB짜리를 캐시하면 그 캐시 하나가
#    워커를 죽인다. 사진 캐시의 상한(64개)은 수백 KB짜리를 전제로 잡힌 수치다.

# Graph 업로드 세션 규약: 마지막 청크를 제외한 모든 청크는 320 KiB의 배수여야 한다.
_CHUNK = 320 * 1024 * 10  # 3.2 MiB — 메모리 상한이자 PUT 한 번의 크기
UPLOAD_TIMEOUT = 120  # 청크 PUT 은 파일 PUT 보다 더 오래 걸릴 수 있다


def upload_stream(stream, size, filename):
    """파일류 객체를 ``couple-daily/<unique-name>`` 으로 **청크 업로드**한다.

    Graph 업로드 세션(``createUploadSession``)을 열고 ``Content-Range`` 를 붙여
    조각조각 PUT 한다. 한 번에 메모리에 올라오는 것은 ``_CHUNK`` 바이트뿐이라
    50MB짜리 영상을 올려도 워커 메모리는 3MB대에서 평평하다.

    ``size`` 는 **정확한 총 바이트** 여야 한다(Content-Range 의 분모). 반환은
    ``upload_photo`` 와 같은 ``(item_id, stored_name)`` 이고, 실패는 OneDriveError.
    """
    if size <= 0:
        raise OneDriveError("upload stream has no bytes")
    try:
        _ensure_folder()
        name = _safe_unique_name(filename or "video")
        r = requests.post(
            f"{GRAPH_BASE}/me/drive/root:/{FOLDER}/{name}:/createUploadSession",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"item": {"@microsoft.graph.conflictBehavior": "rename"}},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code not in (200, 201):
            raise OneDriveError(f"upload session failed (status={r.status_code})")
        upload_url = r.json().get("uploadUrl")
        if not upload_url:
            raise OneDriveError("upload session returned no uploadUrl")

        sent = 0
        item = None
        while sent < size:
            chunk = stream.read(min(_CHUNK, size - sent))
            if not chunk:
                break
            first, last = sent, sent + len(chunk) - 1
            # uploadUrl 은 이미 인증된 URL이다 — Bearer 를 붙이지 않는다(붙이면 거부).
            cr = requests.put(
                upload_url,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {first}-{last}/{size}",
                },
                data=chunk,
                timeout=UPLOAD_TIMEOUT,
            )
            if cr.status_code in (200, 201):
                item = cr.json()
            elif cr.status_code != 202:
                # 실패하면 세션을 지워 OneDrive 에 반쪽 파일을 남기지 않는다.
                try:
                    requests.delete(upload_url, timeout=HTTP_TIMEOUT)
                except requests.RequestException:
                    pass
                raise OneDriveError(f"chunk upload failed (status={cr.status_code})")
            sent += len(chunk)

        if sent != size:
            try:
                requests.delete(upload_url, timeout=HTTP_TIMEOUT)
            except requests.RequestException:
                pass
            raise OneDriveError(f"upload truncated ({sent}/{size} bytes)")
        if not item or not item.get("id"):
            raise OneDriveError("upload returned no item id")
        return item["id"], item.get("name") or name
    except OneDriveError:
        raise
    except requests.RequestException as e:
        raise OneDriveError(f"upload request failed: {e}") from e


def open_range(item_id, range_header=None):
    """아이템 바이트를 **스트리밍으로** 연다 (Range 통과).

    ``GET /me/drive/items/<id>/content`` 는 신선한 pre-auth CDN 링크로 302 하고,
    ``requests`` 가 그 리다이렉트를 따라간다(크로스 호스트 홉에서 Authorization 은
    떨어지지만 ``Range`` 는 유지된다 — 목적지는 이미 인증된 URL이다).

    반환: ``(status, headers, response)``. ``response`` 는 ``stream=True`` 상태라
    **호출자가 ``iter_content`` 로 흘리고 반드시 ``close()`` 해야 한다.** 아이템이
    없으면 ``(404, {}, None)``.

    ⚠️ 응답 바이트를 여기서 읽지 않는다 — 읽는 순간 50MB가 메모리에 올라온다.
    """
    headers = dict(_auth_headers())
    if range_header:
        headers["Range"] = range_header
    try:
        r = requests.get(
            f"{GRAPH_BASE}/me/drive/items/{item_id}/content",
            headers=headers,
            timeout=HTTP_TIMEOUT,
            stream=True,
        )
    except requests.RequestException as e:
        raise OneDriveError(f"range request failed: {e}") from e
    if r.status_code == 404:
        r.close()
        return 404, {}, None
    if r.status_code not in (200, 206):
        status = r.status_code
        r.close()
        raise OneDriveError(f"range fetch failed (status={status})")
    return r.status_code, r.headers, r


def probe_direct_cors(item_id, origin):
    """**실측**: 이 아이템의 OneDrive 직접 다운로드 URL을 브라우저가 Canvas 로
    읽을 수 있는가? — 즉 CDN 이 ``Access-Control-Allow-Origin`` 을 주는가.

    후보 (a)(Microsoft CDN 직접 스트리밍, 서버 비용 0)를 **주장이 아니라 측정으로**
    가르기 위한 함수다. 다운로드 URL에 ``Origin`` 헤더를 붙여 1바이트만 요청하고,
    돌아온 응답 헤더에서 ACAO 와 206(Range) 지원을 본다.

    ⚠️ 반환값에 **URL을 절대 담지 않는다** — pre-auth 다운로드 URL 자체가
    자격증명이다(쿼리에 토큰이 박혀 있다). 담는 것은 판정과 헤더 값뿐이다.

    반환: ``{"ok": bool, "acao": str|None, "range": bool, "status": int}`` 또는
    조회조차 못 했으면 ``None``.
    """
    try:
        url = get_download_url(item_id)
    except OneDriveError:
        return None
    if not url:
        return None
    try:
        r = requests.get(
            url,
            headers={"Origin": origin, "Range": "bytes=0-0"},
            timeout=HTTP_TIMEOUT,
            stream=True,
        )
    except requests.RequestException:
        return None
    try:
        acao = r.headers.get("Access-Control-Allow-Origin")
        return {
            "ok": bool(acao and acao in ("*", origin)),
            "acao": acao,
            "range": r.status_code == 206,
            "status": r.status_code,
        }
    finally:
        r.close()
