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
import re
from datetime import date

import requests

import ai  # 팝업 수집만 claude(웹검색)를 쓴다 — 서울 페처는 여전히 AI 없음(P1)

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


# ---------------------------------------------------------------------------
# 팝업스토어 수집 — claude 웹검색(--allowedTools WebSearch WebFetch)
# ---------------------------------------------------------------------------
# 서울 문화행사 API엔 '팝업스토어'가 없다. 그래서 claude에게 실제 웹 검색을
# 시켜 지금 서울에서 열려있거나 곧 여는 팝업을 찾아 EventItem(source='popup')로
# 흘려보낸다. AI 출처라 UI에서 라벨링한다. 네트워크+claude 조합이라 느리고
# (~120s+) 백그라운드 전용이다 — 절대 요청 경로에서 부르지 마.


def popups_enabled() -> bool:
    """팝업 수집은 별도 키가 필요 없다 — 이미 쓰는 claude 인증에만 의존한다."""
    return True


# 기간 문자열에서 날짜 토큰을 뽑기 위한 범위 구분자(다양한 대시·물결).
_PERIOD_SEPS = ("~", "〜", "～", "–", "—", " - ", " ~ ")


def _parse_loose_date(token, default_year):
    """'8/1', '2026.08.01', '2026-08-01', '8월 1일' 류 토큰 하나를 date로(관대).

    연도가 없으면 default_year를 가정한다. 실패 시 None."""
    if not token:
        return None
    s = str(token).strip()
    if not s:
        return None
    # 먼저 연도까지 있는 완전한 ISO/점/슬래시 형식은 기존 _parse_date로.
    full = _parse_date(s)
    if full is not None:
        return full
    # 'M월 D일' → 'M/D'
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
    else:
        # 'M/D' 또는 'M.D' (연도 없음) — 앞의 두 숫자를 월/일로.
        m = re.search(r"(\d{1,2})[./](\d{1,2})", s)
        if not m:
            return None
        mo, da = int(m.group(1)), int(m.group(2))
    try:
        return date(default_year, mo, da)
    except ValueError:
        return None


def _parse_period(period):
    """자유 텍스트 기간 문자열 → (start_date, end_date) 관대 파싱. 실패분은 None.

    '8/1~8/14', '2026.08.01 ~ 2026.08.31', '8월 1일 - 8월 14일' 등을 처리한다.
    범위 구분자로 좌/우를 나눠 왼쪽=시작, 오른쪽=종료로 본다. 연도가 없으면
    올해로 가정한다. 구분자가 없으면 하나의 날짜로 보고 시작만 채운다."""
    if not period:
        return None, None
    s = str(period).strip()
    if not s:
        return None, None
    year = date.today().year

    left, right = s, None
    for sep in _PERIOD_SEPS:
        if sep in s:
            parts = s.split(sep, 1)
            left, right = parts[0], parts[1]
            break
    else:
        # 공백 없는 순수 대시 범위('8/1-8/14')도 시도(ISO 'YYYY-MM-DD'는 위에서 통과).
        if _parse_date(s) is None and s.count("-") == 1:
            left, right = s.split("-", 1)

    start = _parse_loose_date(left, year)
    end = _parse_loose_date(right, year) if right else None
    return start, end


def _normalize_popup(entry):
    """claude가 준 팝업 dict 하나 → 서울 페처와 같은 모양의 EventItem용 dict.

    제목(name) 없으면 None. category는 '팝업'으로 고정해 event_category_bucket이
    '팝업'을 돌려주게 한다. 날짜는 관대하게: end_date 필드를 먼저 파싱하고, 없으면
    period 문자열의 오른쪽(종료)에서, start_date는 period 왼쪽(시작)에서 best-effort로
    뽑는다. 별도 desc가 없으면 period 텍스트를 설명으로 쓴다."""
    if not isinstance(entry, dict):
        return None
    title = (entry.get("name") or "").strip()
    if not title:
        return None
    place = (entry.get("place") or "").strip() or None
    link = (entry.get("link") or "").strip() or None
    desc = (entry.get("desc") or "").strip() or None
    period = (entry.get("period") or "").strip() or None

    # period에서 best-effort 시작/종료를 뽑고, 명시 end_date가 있으면 그걸 우선.
    p_start, p_end = _parse_period(period)
    end_date = _parse_date(entry.get("end_date")) or p_end
    start_date = _parse_date(entry.get("start_date")) or p_start
    # 별도 설명이 없으면 기간 텍스트라도 설명으로 남겨 카드가 비지 않게.
    description = desc or period

    # 소스에 id가 없어 title|place의 sha1을 안정적 dedupe 키로.
    source_uid = hashlib.sha1(f"{title}|{place or ''}".encode("utf-8")).hexdigest()
    return {
        "source": "popup",
        "source_uid": source_uid,
        "title": title[:300],
        "category": "팝업",  # event_category_bucket이 '팝업' 버킷을 돌려줌
        "description": description,
        "place": place[:200] if place else None,
        "district": None,
        "image_url": None,
        "link": link,
        "fee": None,
        "is_free": None,
        "start_date": start_date,
        "end_date": end_date,
    }


def fetch_popups(max_items=5):
    """claude 웹검색으로 서울의 실제 팝업스토어를 찾아 정규화 dict 리스트로 반환.

    백그라운드 전용(네트워크+claude). 프롬프트는 '가볍게' 유지한다 — 정확한 ISO
    날짜 검증을 강요하면 웹검색이 너무 오래 걸려(240s 타임아웃) 실측했다. 그래서
    기간은 자유 텍스트(period)로 받고 종료일만 '이미 명백하면' ISO로 받는다. 어떤
    실패/타임아웃/파싱 에러에도 예외를 밖으로 던지지 않는다 — []를 반환해 피드가
    우아하게 degrade하도록 한다."""
    try:
        n = int(max_items or 0)
    except (TypeError, ValueError):
        n = 5
    if n <= 0:
        n = 5

    today = date.today().isoformat()
    prompt = (
        "너는 서울의 '팝업스토어' 정보를 모으는 도우미야. 반드시 웹 검색을 사용해서 "
        f"(오늘 날짜: {today}) 지금 서울에서 실제로 '열려 있거나 곧 여는' 팝업스토어를 "
        f"최대 {n}개만 찾아줘.\n\n"
        "규칙(아주 중요):\n"
        "- 반드시 실제로 웹을 검색해서 확인된 것만 담아. 절대 지어내지 마.\n"
        "- 이미 종료된(기간이 지난) 팝업은 제외해.\n"
        "- 전시·공연·페스티벌 말고 '팝업스토어'만. 확실하지 않으면 그냥 빼.\n"
        "- 각 항목의 링크(link)는 실제 예약·안내 페이지나 출처 URL로.\n"
        "- period는 검색 결과에 보이는 기간을 '그대로' 자유롭게 적어(예: '8/1~8/14', "
        "'8월 중순까지'). 날짜를 정확히 맞추려고 추가 검색으로 시간 쓰지 마.\n"
        "- end_date는 검색 결과에서 종료일이 '이미 명백할 때만' YYYY-MM-DD로 적고, "
        "아니면 빈 문자열로 둬(확인하려 추가 검색하지 마).\n\n"
        "출력은 아래 JSON 객체 하나만, 다른 텍스트/설명/코드펜스 없이:\n"
        '{"popups":[{"name":"","place":"","period":"","link":"","end_date":""}]}'
    )

    try:
        raw = ai._run_claude(prompt, timeout=ai.POPUP_TIMEOUT, allow_web=True)
    except Exception as e:  # noqa: BLE001 — claude 실패는 팝업 없음으로 degrade
        log.warning("popup fetch: claude run failed: %s", e)
        return []

    # 디버깅용으로 원본 출력을 한 번 남긴다(필드/스키마 대조).
    log.info("popup fetch raw output: %s", (raw or "")[:2000])

    try:
        data = ai._extract_json(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("popup fetch: JSON parse failed: %s", e)
        return []

    raw_popups = data.get("popups") if isinstance(data, dict) else None
    if not isinstance(raw_popups, list):
        log.warning("popup fetch: no popups array in output")
        return []

    out = []
    for entry in raw_popups[:n]:
        item = _normalize_popup(entry)
        if item:
            out.append(item)
    return out
