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
_BYTES_CACHE_TTL = 1800  # seconds
# ⚠️ 상한은 **개수가 아니라 바이트**다. 옛 상한(64개)은 "memory can't grow unbounded"
# 라고 적혀 있었지만 실제로는 아무것도 묶지 못했다 — 실측: 아이폰 12MP 원본 한 장이
# 3.03MB 라 64개면 **197MB** 가 상주한다. 512MB 티어에서 `claude -p` 가 ~265MB,
# 파이썬 워커가 ~100MB 를 쓰므로 이 캐시 하나로 한도를 넘긴다(2026-10-07 OOM 사고의
# 지분). 예산 16MB = 그런 원본 약 5장 — "refetch storm 을 흡수한다"는 이 캐시의
# 목적(짧은 TTL)에는 충분하고, 갤러리 그리드는 아래 썸네일 캐시가 따로 받친다.
_BYTES_CACHE_BUDGET = 16 * 1024 * 1024
_bytes_cache: dict[str, tuple[bytes, str, float]] = {}

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
    """``get_photo_content`` with a brief in-process BYTES cache.

    Avoids refetch storms (e.g. a reload before the browser cache warms) without
    ever serving an expired URL — the cached value is the decoded bytes, always
    valid. TTL is short and the cache is size-capped. Returns ``(bytes, ctype)``
    or ``(None, None)`` if the item is gone.
    """
    now = time.time()
    hit = _bytes_cache.get(item_id)
    if hit and now < hit[2]:
        return hit[0], hit[1]
    data, ctype = get_photo_content(item_id)
    if data:
        # 만료분을 버리고 **바이트 예산** 안으로 축출한 뒤 넣는다. 한 장이 예산보다
        # 크면 아예 캐시하지 않는다(그 한 장이 캐시를 비우고 눌러앉지 않게) — 그런
        # 사진은 매번 다시 받을 뿐 기능은 그대로다.
        size = len(data)
        if size <= _BYTES_CACHE_BUDGET:
            for k in [k for k, v in _bytes_cache.items() if v[2] <= now]:
                _bytes_cache.pop(k, None)
            total = sum(len(v[0]) for v in _bytes_cache.values()) + size
            while _bytes_cache and total > _BYTES_CACHE_BUDGET:
                oldest = min(_bytes_cache, key=lambda k: _bytes_cache[k][2])
                total -= len(_bytes_cache.pop(oldest)[0])
            _bytes_cache[item_id] = (data, ctype or "", now + _BYTES_CACHE_TTL)
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


def delete_photo(item_id: str):
    """Delete an item from OneDrive. A 404 (already gone) counts as success."""
    _bytes_cache.pop(item_id, None)
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
