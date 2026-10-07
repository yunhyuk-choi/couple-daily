"""미리보기 자산 — **우리가 만들어 우리가 소유한다.**

왜 이게 있나 (2026-10-07, 3차 이미지 작업의 마지막 조각)
--------------------------------------------------------
갤러리 그리드·크롭 UI·라이트박스처럼 **사람이 보는 작은 그림**은 지금까지 매 요청
Graph 를 왕복해서 만들어졌다. 사진 한 장을 띄우려고:

    브라우저 → 우리 서버 → Graph 썸네일 메타(1왕복) → CDN 바이트(1왕복) → 브라우저

였고, 그 두 왕복은 **화면을 열 때마다** 다시 일어났다(프로세스 메모리 캐시는 재시작에
사라지고, Render 무료티어는 자주 잔다). 사용자의 지적이 정확하다 — 미리보기는
**DB 읽기와 리페인트 말고는 아무 시간도 들면 안 된다.**

### 설계 — 남의 URL 수명에 의존하지 않는다

"Graph 썸네일 URL 을 DB 에 넣어 두면 되나?"는 **틀린 질문**이다. 그건 남이 발급한
링크의 수명(문서화되지 않았고 언제든 바뀐다)에 우리 화면을 거는 짓이다. 답은:

    미리보기 자산을 **한 번 만들어 영구 보관**하고, **우리 고정 URL**로 내준다.

그러면 Graph 썸네일/렌디션 URL 이 1초를 살든 1년을 살든 **상관없어진다** — 그 질문
자체가 설계에서 사라진다. 구체적으로:

| 무엇 | 어떻게 |
|---|---|
| 만들기 | Graph 커스텀 렌디션(``c{N}x{N}``) **1회**. 서버는 픽셀을 디코드하지 않는다 |
| 보관 | **DB 행**(``photo_previews``) — 재시작·재배포에도 산다. 디스크는 배포 때 날아간다 |
| 내주기 | ``/memories/<id>/thumb`` · ``/memories/<id>/preview`` — 사진당 **고정** 주소 |
| 캐시 | ``Cache-Control: private, max-age=1년, immutable`` + ``ETag`` → 재방문 네트워크 0 |

### 왜 DB 인가 (디스크가 아니라)

``bytecache.DiskByteCache`` 는 **캐시**다 — 없어져도 되는 것(원본 바이트·가공본)을
담고, 없으면 다시 만들면 된다. 미리보기 자산은 그 반대다: **없으면 화면이 느려지는
것이 유일한 증상**이고, Render 의 ephemeral 디스크는 **재배포마다 비어 있다.** 매
배포 직후 모든 사용자가 모든 사진의 Graph 왕복을 다시 치르는 건 우리가 없애려던
바로 그 비용이다. 그래서 캐시가 아니라 **자산**으로 다룬다.

용량: 그리드 티어는 장당 수십 KB다(384px JPEG). 사진 1000장이면 수십 MB 수준이라
Postgres 한 칸에 충분히 들어간다. 큰 ``view`` 티어는 **쓰이는 사진만**(크롭 UI·
라이트박스가 실제로 연 사진) 게으르게 만든다 — 모든 사진에 미리 만들지 않는다.

규율
----
* **절대 raise 하지 않는다.** 못 만들면 ``None`` 이고 호출부가 옛 경로로 폴백한다.
* **서버는 픽셀을 디코드하지 않는다.** Pillow 를 쓰지 않는다(이 모듈은 PIL 을 import
  조차 하지 않는다 — 구조 테스트가 그걸 본다). 크기 조정은 Graph 가 한다.
* **원본 바이트를 받지 않는다.** ``get_photo_content`` 를 부르지 않는다.
* 생성은 사진·티어당 **한 번**이다. 같은 워커에서 동시 요청이 겹쳐도 한 번만 받는다.
"""
import hashlib
import logging
import os
import threading

import onedrive
from models import PhotoPreview, db

log = logging.getLogger(__name__)


def _env_int(name, default):
    try:
        v = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


# 티어 = '이 화면이 필요로 하는 긴 변'. 숫자는 화면이 정하지 코드가 정하지 않는다.
#
#   grid — 갤러리 그리드·사진 고르기·캘린더 썸네일. 폰 3열 그리드(≈125px 셀)에서
#          DPR 3 이면 375px 가 필요하다. 옛 Graph 'medium'(176px 박스, 실측 4.4KB)은
#          그 절반도 안 돼 눈에 띄게 뭉갰다. 384 는 그걸 메우면서도 장당 수십 KB다.
#   view — 크롭 UI 미리보기·라이트박스. 폰 전체화면(390×844 CSS · DPR 3 → 1170px)을
#          덮는 최소치가 1280 이다. 기존 상세 화면 렌디션(_RENDITION_PREVIEW_EDGE)과
#          같은 수치라 **보이는 픽셀이 달라지지 않는다.**
GRID_EDGE = _env_int("PREVIEW_GRID_EDGE", 384)
VIEW_EDGE = _env_int("PREVIEW_VIEW_EDGE", 1280)
TIERS = {"grid": GRID_EDGE, "view": VIEW_EDGE}

# 한 자산이 이보다 크면 DB 에 넣지 않는다(폭주 방지). 렌디션이 이 크기를 넘길 일은
# 없지만, 넘기면 그건 우리가 모르는 일이 일어난 것이므로 보관하지 않고 그냥 내준다.
MAX_BYTES = _env_int("PREVIEW_MAX_BYTES", 3 * 1024 * 1024)

# 미리보기 URL 의 캐시 버전. 티어 크기를 바꿨을 때 **이미 캐시된 브라우저**까지
# 새 자산을 받게 하는 단 하나의 손잡이다(URL 이 바뀌어야 immutable 이 정직해진다).
REV = (os.environ.get("PREVIEW_REV") or "1").strip()[:16] or "1"

# 같은 (사진, 티어)를 동시에 두 요청이 요구할 때 Graph 를 두 번 부르지 않기 위한
# 워커-내 문. 프로세스 밖까지 막지는 않는다(그래도 INSERT 경합은 아래가 흡수한다).
_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def etag_for(data) -> str:
    """바이트 → 약하지 않은 ETag 문자열(따옴표 포함). 내용이 같으면 항상 같다."""
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'


def _named_fallbacks(edge):
    """렌디션을 못 받을 때 쓸 Graph 기본 썸네일 이름 — 큰 것부터.

    Graph 가 커스텀 ``c{N}x{N}`` 를 거부하는 드라이브/아이템이 있을 수 있다(문서에
    한도가 없다 — 그래서 우리는 '주장하지 않고 폴백한다'). 기본 이름 셋은 언제나
    있다: large(800) · medium(176) · small(96).
    """
    names = [("large", 800), ("medium", 176), ("small", 96)]
    # 요구 크기보다 크거나 같은 것부터, 그다음 작은 것들.
    ge = [n for n, px in names if px >= edge]
    lt = [n for n, px in names if px < edge]
    return ge + lt


def _fetch(item_id, edge):
    """Graph 에서 미리보기 바이트를 **한 번** 받아 온다 → ``(data, ctype, w, h, src)``.

    ⛔ 원본을 받지 않는다. ⛔ Pillow 를 쓰지 않는다. 못 받으면 ``None``.
    """
    try:
        got = onedrive.get_rendition(item_id, edge)
    except onedrive.OneDriveError:
        log.warning("preview: 렌디션 조회 실패 (item=%s edge=%s)", item_id, edge,
                    exc_info=True)
        got = None
    except Exception:  # noqa: BLE001 — 미리보기 생성이 요청을 깨뜨릴 이유가 없다
        log.exception("preview: 렌디션 조회가 예외 (item=%s edge=%s)", item_id, edge)
        got = None
    if got:
        data, ctype, (w, h) = got
        return data, ctype or "image/jpeg", w, h, f"rendition:c{edge}"

    for name in _named_fallbacks(edge):
        try:
            data, ctype = onedrive.get_thumbnail(item_id, size=name)
        except onedrive.OneDriveError:
            log.warning("preview: 썸네일(%s) 실패 (item=%s)", name, item_id,
                        exc_info=True)
            continue
        except Exception:  # noqa: BLE001
            log.exception("preview: 썸네일(%s)이 예외 (item=%s)", name, item_id)
            continue
        if data:
            # 이름 있는 썸네일은 가로세로를 알려주지 않는다 — 모르는 건 비워 둔다
            # (템플릿이 width/height 를 생략한다. 지어내지 않는다).
            return data, ctype or "image/jpeg", None, None, f"thumbnail:{name}"
    return None


def _row(photo_id, tier):
    """저장된 자산 행(없으면 None). DB 가 삐끗해도 raise 하지 않는다."""
    try:
        return (
            PhotoPreview.query.filter_by(photo_id=photo_id, tier=tier).first()
        )
    except Exception:  # noqa: BLE001
        log.exception("preview: 행 조회 실패 (photo=%s tier=%s)", photo_id, tier)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _persist(photo_id, tier, data, ctype, w, h, source):
    """자산을 영구 보관한다. 경합(동시 INSERT)은 재조회로 흡수. 절대 raise 안 함."""
    if len(data) > MAX_BYTES:
        log.warning("preview: 너무 커서 보관하지 않는다 (photo=%s tier=%s bytes=%s)",
                    photo_id, tier, len(data))
        return None
    row = PhotoPreview(
        photo_id=photo_id, tier=tier, data=data,
        content_type=ctype, width=w, height=h,
        byte_len=len(data), source=source, etag=etag_for(data),
    )
    try:
        db.session.add(row)
        db.session.commit()
        return row
    except Exception:  # noqa: BLE001 — 유니크 충돌(동시 생성) 포함
        db.session.rollback()
        existing = _row(photo_id, tier)
        if existing is not None:
            return existing
        log.exception("preview: 보관 실패 (photo=%s tier=%s)", photo_id, tier)
        return None


def ensure(photo, tier="grid"):
    """``(bytes, content_type, etag, width, height)`` 또는 ``None``.

    이미 보관된 자산이 있으면 **DB 읽기 한 번**이 전부다 — Graph 왕복 0.
    없으면 **그때 한 번만** 만들어 영구 보관하고 돌려준다. 절대 raise 하지 않는다.
    """
    if photo is None:
        return None
    edge = TIERS.get(tier)
    if not edge:
        return None
    row = _row(photo.id, tier)
    if row is not None and row.data:
        return (row.data, row.content_type or "image/jpeg",
                row.etag or etag_for(row.data), row.width, row.height)

    item_id = getattr(photo, "onedrive_item_id", None)
    if not item_id:
        return None

    with _lock_for((photo.id, tier)):
        # 문 앞에서 기다리는 동안 다른 스레드가 만들어 뒀을 수 있다.
        row = _row(photo.id, tier)
        if row is not None and row.data:
            return (row.data, row.content_type or "image/jpeg",
                    row.etag or etag_for(row.data), row.width, row.height)
        got = _fetch(item_id, edge)
        if not got:
            return None
        data, ctype, w, h, source = got
        row = _persist(photo.id, tier, data, ctype, w, h, source)
    if row is not None and row.data:
        return (row.data, row.content_type or "image/jpeg",
                row.etag or etag_for(row.data), row.width, row.height)
    # 보관만 실패한 경우 — 화면은 살려 둔다(다음 요청이 다시 시도한다).
    return data, ctype, etag_for(data), w, h


def stored(photo_id, tier="grid"):
    """보관된 자산의 **메타만**(바이트 없이) — ``(etag, w, h)`` 또는 None.

    목록 화면이 ``width``/``height`` 를 미리 박아 레이아웃 흔들림(리플로)을 없애는
    데 쓴다. 바이트를 읽지 않으므로 목록 렌더가 무거워지지 않는다.
    """
    try:
        row = (
            db.session.query(
                PhotoPreview.etag, PhotoPreview.width, PhotoPreview.height
            )
            .filter_by(photo_id=photo_id, tier=tier)
            .first()
        )
    except Exception:  # noqa: BLE001
        log.exception("preview: 메타 조회 실패 (photo=%s)", photo_id)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    return (row[0], row[1], row[2]) if row else None


def dims_map(photo_ids, tier="grid"):
    """``{photo_id: (w, h)}`` — 목록 한 번에. 없는 사진은 키가 없다."""
    ids = [int(i) for i in (photo_ids or [])]
    if not ids:
        return {}
    try:
        rows = (
            db.session.query(
                PhotoPreview.photo_id, PhotoPreview.width, PhotoPreview.height
            )
            .filter(PhotoPreview.tier == tier, PhotoPreview.photo_id.in_(ids))
            .all()
        )
    except Exception:  # noqa: BLE001
        log.exception("preview: 치수 일괄 조회 실패")
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {}
    return {r[0]: (r[1], r[2]) for r in rows if r[1] and r[2]}


def forget(photo_id):
    """사진이 지워질 때 그 미리보기 자산도 같이 지운다. 절대 raise 안 함."""
    try:
        PhotoPreview.query.filter_by(photo_id=photo_id).delete()
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("preview: 삭제 실패 (photo=%s)", photo_id)
