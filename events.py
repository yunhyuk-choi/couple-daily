"""'데이트 뉴스' 페처 — 서울 열린데이터광장 '문화행사 정보(culturalEventInfo)'.

네트워크 전용 모듈이다: 여기엔 AI/claude가 없다(P1). 밤마다 크론이 호출해
실제 전시·공연·행사를 긁어와 정규화된 dict 리스트로 돌려준다. 어떤 경우에도
예외를 밖으로 던지지 않는다 — 키가 없거나 API가 에러/쿼터를 뱉으면 지금까지
모은 것(또는 [])을 반환해 피드가 빈 상태로 우아하게 degrade하도록 한다.

응답 형태:
  {"culturalEventInfo": {"list_total_count": <int>,
                          "RESULT": {"CODE": "...", "MESSAGE": "..."},
                          "row": [ {<event>}, ... ]}}
인증/쿼터 에러 시 최상위 키가 다를 수 있어(예: {"RESULT": {"CODE": "INFO-100"}})
방어적으로 파고든다. 필드명도 라이브 스키마와 어긋날 수 있으니 매 fetch마다
첫 row 원본을 한 번 로그로 남겨(필드명 대조가 즉시 가능하도록) 처리한다.
"""
import hashlib
import logging
import os
from datetime import date

import requests

log = logging.getLogger(__name__)

# 무료 인증키. 없으면 페처는 조용히 []를 반환하고 기능은 빈 상태로만 보인다.
SEOUL_KEY = os.environ.get("SEOUL_OPENAPI_KEY")

_BASE = "http://openapi.seoul.go.kr:8088"
_SERVICE = "culturalEventInfo"
_BLOCK = 1000        # API 한 번당 최대 행 수
_MAX_ROWS_CAP = 2000  # 작업량 상한(과도한 페이지네이션 방지)
_TIMEOUT = 15        # 초
_DESC_MAXLEN = 2000  # description 합성 상한


def events_enabled() -> bool:
    """인증키가 있을 때만 기능이 실제로 동작한다."""
    return bool(SEOUL_KEY)


# 서울 CODENAME → 칩 필터용 굵은 버킷 매핑에 쓰는 '공연'류 집합.
_PERF_CODENAMES = {
    "연극", "뮤지컬/오페라", "클래식", "콘서트", "무용", "국악", "독주/독창회", "영화",
}


def event_category_bucket(category) -> str:
    """서울 CODENAME(행사 category)을 데이트 뉴스 칩 필터용 굵은 버킷으로 매핑.

    규칙(순서 중요):
      * '축제' 포함 → "축제"
      * 공연류 집합(_PERF_CODENAMES) → "공연"
      * "전시/미술" → "전시"
      * "교육/체험" → "체험"
      * '팝업' 포함 → "팝업"(현재 소스에 데이터 없음 — 빈 상태로 우아히 처리)
      * 그 외/없음 → "기타"(전체 필터에서만 노출)
    """
    cat = (category or "").strip()
    if not cat:
        return "기타"
    if "축제" in cat:
        return "축제"
    if cat in _PERF_CODENAMES:
        return "공연"
    if cat == "전시/미술":
        return "전시"
    if cat == "교육/체험":
        return "체험"
    if "팝업" in cat:
        return "팝업"
    return "기타"


def _first(row, *keys):
    """row에서 keys를 순서대로 찾아 비어있지 않은 첫 문자열 값을 반환(없으면 None)."""
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _parse_date(s):
    """'2026-08-01 00:00:00.0' / '2026-08-01' 류 문자열을 date로. 실패 시 None."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    # 날짜 부분만 취한다(공백·T 이전 10자리 YYYY-MM-DD).
    head = s.replace("T", " ").split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return date.fromisoformat(head) if fmt == "%Y-%m-%d" else \
                _from_fmt(head, fmt)
        except ValueError:
            continue
    return None


def _from_fmt(s, fmt):
    from datetime import datetime
    return datetime.strptime(s, fmt).date()


def _source_uid(title, start_date, place):
    """소스에 명시적 id가 없어 title|start_date|place의 sha1(hex)를 안정적 키로."""
    basis = f"{title or ''}|{start_date or ''}|{place or ''}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _build_description(row):
    """PROGRAM/ETC_DESC/USE_TRGT를 합쳐 적당히 잘라 description 재료로."""
    parts = []
    for k in ("PROGRAM", "ETC_DESC", "USE_TRGT"):
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s and s not in parts:
            parts.append(s)
    if not parts:
        return None
    desc = "\n\n".join(parts)
    return desc[:_DESC_MAXLEN].strip() or None


def _normalize(row):
    """API row 하나 → 정규화된 dict. 모든 필드는 없을 수 있다고 보고 방어적으로."""
    title = _first(row, "TITLE")
    if not title:
        return None  # 제목 없는 행은 버린다(모델 not-null)
    place = _first(row, "PLACE")
    start_date = _parse_date(_first(row, "STRTDATE", "STRT_DATE", "START_DATE"))
    end_date = _parse_date(_first(row, "END_DATE", "ENDDATE", "END_DATE "))

    is_free_raw = _first(row, "IS_FREE")
    is_free = None
    if is_free_raw is not None:
        is_free = ("무료" in is_free_raw) or (is_free_raw.upper() == "FREE")

    return {
        "source": "seoul",
        "source_uid": _source_uid(title, start_date, place),
        "title": title[:300],
        "category": (_first(row, "CODENAME") or None),
        "description": _build_description(row),
        "place": place[:200] if place else None,
        "district": (_first(row, "GUNAME") or None),
        "image_url": _first(row, "MAIN_IMG"),
        "link": _first(row, "ORG_LINK", "HMPG_ADDR"),
        "fee": _first(row, "USE_FEE"),
        "is_free": is_free,
        "start_date": start_date,
        "end_date": end_date,
    }


def _fetch_block(start, end):
    """[start, end] 구간 한 블록을 호출해 (list_total_count, rows) 반환.

    실패/에러 응답이면 (None, [])를 돌려 호출부가 판단하게 한다. 예외 없음."""
    url = f"{_BASE}/{SEOUL_KEY}/json/{_SERVICE}/{start}/{end}/"
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("seoul events fetch failed (%s-%s): %s", start, end, e)
        return None, []

    body = data.get(_SERVICE) if isinstance(data, dict) else None
    if not isinstance(body, dict):
        # 인증/쿼터 에러는 최상위 RESULT로 올 수 있다.
        result = data.get("RESULT") if isinstance(data, dict) else None
        if isinstance(result, dict):
            log.warning("seoul events API error: %s %s",
                        result.get("CODE"), result.get("MESSAGE"))
        else:
            log.warning("seoul events: unexpected payload shape")
        return None, []

    result = body.get("RESULT")
    if isinstance(result, dict):
        code = str(result.get("CODE", ""))
        # INFO-000 만 정상; 그 외(INFO-200 데이터없음 포함)는 데이터가 없다고 본다.
        if code and code != "INFO-000":
            log.warning("seoul events API result: %s %s",
                        code, result.get("MESSAGE"))
            if code != "INFO-200":
                return None, []

    total = body.get("list_total_count")
    try:
        total = int(total) if total is not None else None
    except (ValueError, TypeError):
        total = None

    rows = body.get("row")
    if not isinstance(rows, list):
        rows = []
    return total, rows


def fetch_seoul_events(max_rows=1000):
    """서울 문화행사를 긁어와 정규화된 dict 리스트로 반환. 절대 예외 없음.

    첫 블록의 list_total_count로 페이지네이션하며, 작업량 상한(_MAX_ROWS_CAP)까지만
    당긴다. STRTDATE/END_DATE는 date로 파싱하고 source_uid는 sha1로 만든다. 키가
    없거나 에러면 지금까지 모은 것(또는 [])을 돌려준다."""
    if not events_enabled():
        return []

    hard_cap = min(max(int(max_rows or 0), _BLOCK), _MAX_ROWS_CAP)
    out = []
    logged_first = False

    total, rows = _fetch_block(1, min(_BLOCK, hard_cap))
    if not rows:
        return out

    # 필드명 대조가 즉시 되도록 첫 row 원본을 한 번만 로그.
    if not logged_first and rows:
        log.info("seoul events raw first row: %s", rows[0])
        logged_first = True

    def _consume(rs):
        for r in rs:
            if not isinstance(r, dict):
                continue
            item = _normalize(r)
            if item:
                out.append(item)

    _consume(rows)

    # 페이지네이션: total과 상한 안에서 다음 블록들을 이어 받는다.
    target = hard_cap
    if isinstance(total, int) and total > 0:
        target = min(total, hard_cap)

    start = _BLOCK + 1
    while start <= target:
        end = min(start + _BLOCK - 1, target)
        _, more = _fetch_block(start, end)
        if not more:
            break
        _consume(more)
        start += _BLOCK

    return out
