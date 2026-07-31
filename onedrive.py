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
_BYTES_CACHE_TTL = 60  # seconds
_BYTES_CACHE_MAX = 24  # cap entries so memory can't grow unbounded
_bytes_cache: dict[str, tuple[bytes, str, float]] = {}

# per-(item,size) THUMBNAIL-BYTES cache for the gallery grid. Thumbnails are
# tiny (a few KB), so we can cache more of them and for longer than full bytes.
# Same rationale as _bytes_cache: cache the decoded bytes (the Graph thumbnail
# `url` is a short-lived CDN link that would 401 if reused).
_THUMB_CACHE_TTL = 600  # seconds
_THUMB_CACHE_MAX = 64
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
    if data is not None:
        # Evict expired / oldest entries before inserting to bound memory.
        if len(_bytes_cache) >= _BYTES_CACHE_MAX:
            for k in [k for k, v in _bytes_cache.items() if v[2] <= now]:
                _bytes_cache.pop(k, None)
            if len(_bytes_cache) >= _BYTES_CACHE_MAX:
                oldest = min(_bytes_cache, key=lambda k: _bytes_cache[k][2])
                _bytes_cache.pop(oldest, None)
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
