"""NAVER API HUB 클라이언트 — 블로그 검색 · 검색어 트렌드(얇은 래퍼).

호출 형태는 추측이 아니라 **실제로 같은 API를 쓰는 코드**(`gf-blog`의
`{맛집명}/research/collect.py`)에서 확인한 그대로다::

    base    https://naverapihub.apigw.ntruss.com
    headers X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY
    GET  /search/v1/blog          query, display, sort=sim|date, format=json
    POST /search-trend/v1/search  {startDate,endDate,timeUnit,keywordGroups}

⚠️ **지표를 오독하지 마라** — 이 모듈을 쓰는 모든 코드에 해당한다:

  * ``total``(블로그 검색 결과 수)은 **검색량이 아니다.** 그 키워드로 글이 몇 편
    있는지일 뿐이고, 사람들이 얼마나 찾는지와는 다른 수치다.
  * 트렌드의 ``ratio``는 **절대 검색 횟수가 아니다.** 요청한 키워드 그룹들 안에서
    조회 구간 최댓값을 100으로 둔 **상대값**이다. 그룹이 바뀌면 값도 바뀐다.
  * 롱테일 키워드의 ``data``가 **빈 배열이면 "수요 없음"이 아니라 "정량 확인 실패"**다
    (네이버가 표본이 적은 검색어를 내주지 않는다). 그걸 근거로 넓은 키워드로
    되돌리지 마라.
  * 긴 검색어라는 이유만으로 상위 노출이 쉽다는 주장도 근거가 없다.

보안: 자격증명은 **인자로만** 받고 모듈 전역·로그·예외 메시지 어디에도 남기지
않는다. ``NaverApiError``는 상태코드와 네이버가 준 짧은 메시지만 담는다 — 요청
헤더를 그대로 실어 나르지 않는다(스택트레이스에 키가 찍히는 사고를 원천 차단).
"""
import html
import json
import logging
import re
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

BASE = "https://naverapihub.apigw.ntruss.com"
BLOG_PATH = "/search/v1/blog"
TREND_PATH = "/search-trend/v1/search"

# 요청 경로(연결 확인)에서도 쓰이므로 넉넉하지 않게. 워커 경로도 같은 값을 쓴다.
TIMEOUT = 10

# 트렌드 API 제한 — 그룹 5개, 그룹당 키워드 20개.
MAX_GROUPS = 5
MAX_KEYWORDS_PER_GROUP = 20

_TAG_RE = re.compile(r"<[^>]+>")


class NaverApiError(Exception):
    """네이버 API 호출 실패. ``status``는 HTTP 코드(네트워크 실패면 None).

    메시지에는 **자격증명이 절대 들어가지 않는다** — 상태코드와 네이버가 준 짧은
    설명만 담는다. 호출부가 이 예외를 로그에 찍어도 키가 새지 않는다.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _headers(client_id, client_secret):
    return {
        "X-NCP-APIGW-API-KEY-ID": (client_id or "").strip(),
        "X-NCP-APIGW-API-KEY": (client_secret or "").strip(),
    }


def _short_error(resp):
    """응답 본문에서 사람이 읽을 짧은 설명만 뽑는다(키는 응답에 없다)."""
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text or "")[:200]
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return (err.get("message") or err.get("details") or "")[:200]
    if isinstance(payload, dict):
        return (payload.get("errorMessage") or "")[:200] or json.dumps(
            payload, ensure_ascii=False
        )[:200]
    return ""


def _request(method, path, client_id, client_secret, params=None, body=None,
             timeout=TIMEOUT):
    """공통 호출. 2xx면 JSON dict, 아니면 NaverApiError."""
    if not (client_id or "").strip() or not (client_secret or "").strip():
        raise NaverApiError("자격증명이 비어 있다")
    url = BASE + path
    headers = _headers(client_id, client_secret)
    try:
        if method == "POST":
            headers["Content-Type"] = "application/json"
            resp = requests.post(
                url, headers=headers,
                data=json.dumps(body or {}, ensure_ascii=False).encode("utf-8"),
                timeout=timeout,
            )
        else:
            resp = requests.get(url, headers=headers, params=params or {},
                                timeout=timeout)
    except requests.RequestException as e:
        # 예외 객체에 요청 헤더가 달려 올 수 있으므로 타입 이름만 남긴다.
        raise NaverApiError(f"네트워크 오류({type(e).__name__})") from None
    if resp.status_code != 200:
        raise NaverApiError(
            _short_error(resp) or "요청이 거부됐다", status=resp.status_code
        )
    try:
        data = resp.json()
    except ValueError:
        raise NaverApiError("응답을 JSON으로 읽지 못했다", status=resp.status_code) from None
    if not isinstance(data, dict):
        raise NaverApiError("예상치 못한 응답 형식", status=resp.status_code)
    return data


# --------------------------------------------------------------------------- #
# 파싱 — 순수 함수(네트워크 없음). 저장한 픽스처로 그대로 테스트한다.
# --------------------------------------------------------------------------- #
def strip_tags(s):
    """검색 결과의 ``<b>`` 하이라이트와 HTML 엔티티를 걷어낸 평문."""
    if not isinstance(s, str):
        return ""
    return html.unescape(_TAG_RE.sub("", s)).strip()


def parse_blog_search(payload):
    """블로그 검색 응답 → ``{"total": int, "items": [...]}``.

    ``total``은 **그 검색어로 걸리는 글 수**이지 검색량이 아니다(모듈 머리말 참고).
    각 item은 ``{title, link, description, bloggername, postdate}``이고 제목·요약은
    태그를 걷어낸 평문, ``postdate``는 ``YYYY-MM-DD``(없으면 "").
    """
    if not isinstance(payload, dict):
        return {"total": 0, "items": []}
    try:
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    items = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        pd = (raw.get("postdate") or "").strip()
        if len(pd) == 8 and pd.isdigit():
            pd = f"{pd[0:4]}-{pd[4:6]}-{pd[6:8]}"
        else:
            pd = ""
        items.append(
            {
                "title": strip_tags(raw.get("title")),
                "link": (raw.get("link") or "").strip(),
                "description": strip_tags(raw.get("description")),
                "bloggername": strip_tags(raw.get("bloggername")),
                "postdate": pd,
            }
        )
    return {"total": total, "items": items}


def parse_trend(payload):
    """트렌드 응답 → ``{그룹명: {"points": n, "last": x, "peak": y, "avg": z}}``.

    ``ratio``는 **요청한 그룹들 안에서의 상대값**(최대 100)이라 절대 검색 횟수가
    아니다. ``points == 0``(빈 배열)은 **수요 없음이 아니라 정량 확인 실패**다 —
    0으로 떨어뜨리면 그 자체가 오독이 되므로 값은 None으로 둔다.
    """
    out = {}
    if not isinstance(payload, dict):
        return out
    for group in payload.get("results") or []:
        if not isinstance(group, dict):
            continue
        name = (group.get("title") or "").strip()
        if not name:
            continue
        ratios = []
        for point in group.get("data") or []:
            if not isinstance(point, dict):
                continue
            try:
                ratios.append(float(point.get("ratio")))
            except (TypeError, ValueError):
                continue
        if not ratios:
            out[name] = {"points": 0, "last": None, "peak": None, "avg": None}
            continue
        out[name] = {
            "points": len(ratios),
            "last": round(ratios[-1], 1),
            "peak": round(max(ratios), 1),
            "avg": round(sum(ratios) / len(ratios), 1),
        }
    return out


def trend_windows(today=None):
    """조회할 두 구간 — 52주(장기 추세) · 8주(최근 추세). ``{이름: (시작, 끝)}``.

    gf-blog가 쓰는 구간 그대로다. 한 구간만 보면 계절성과 최근 변화를 못 가른다.
    """
    end = today or date.today()
    return {
        "52w": (end - timedelta(weeks=52) + timedelta(days=1), end),
        "8w": (end - timedelta(weeks=8) + timedelta(days=1), end),
    }


# --------------------------------------------------------------------------- #
# 호출
# --------------------------------------------------------------------------- #
def search_blog(client_id, client_secret, query, display=20, sort="sim",
                timeout=TIMEOUT):
    """블로그 검색 1회 → ``parse_blog_search`` 결과."""
    query = (query or "").strip()
    if not query:
        return {"total": 0, "items": []}
    display = max(1, min(100, int(display or 20)))
    sort = sort if sort in ("sim", "date") else "sim"
    payload = _request(
        "GET", BLOG_PATH, client_id, client_secret,
        params={"query": query, "display": display, "sort": sort, "format": "json"},
        timeout=timeout,
    )
    return parse_blog_search(payload)


def search_trend(client_id, client_secret, keyword_groups, start_date, end_date,
                 time_unit="week", timeout=TIMEOUT):
    """검색어 트렌드 1회 → ``parse_trend`` 결과.

    ``keyword_groups``: ``[{"groupName": ..., "keywords": [...]}, ...]``
    (그룹 5개·그룹당 키워드 20개 제한에 맞춰 잘라 보낸다).
    """
    groups = []
    for g in (keyword_groups or [])[:MAX_GROUPS]:
        if not isinstance(g, dict):
            continue
        name = (g.get("groupName") or "").strip()
        words = [w.strip() for w in (g.get("keywords") or []) if str(w).strip()]
        if not name or not words:
            continue
        groups.append({"groupName": name, "keywords": words[:MAX_KEYWORDS_PER_GROUP]})
    if not groups:
        return {}
    payload = _request(
        "POST", TREND_PATH, client_id, client_secret,
        body={
            "startDate": str(start_date),
            "endDate": str(end_date),
            "timeUnit": time_unit,
            "keywordGroups": groups,
        },
        timeout=timeout,
    )
    return parse_trend(payload)


def verify_credentials(client_id, client_secret, timeout=TIMEOUT):
    """'연결 확인' — 가장 가벼운 호출 1회로 키가 살아 있는지만 본다.

    ``(ok: bool, message: str)``. 메시지는 **사람에게 보여줄 문구**이고 자격증명을
    담지 않는다. 쿼터를 거의 안 쓰도록 ``display=1``짜리 블로그 검색 한 번만 친다.
    """
    try:
        search_blog(client_id, client_secret, "테스트", display=1, timeout=timeout)
    except NaverApiError as e:
        if e.status in (401, 403):
            return False, "키가 거부됐어 (인증 실패) — Client ID/Secret을 다시 확인해줘."
        if e.status == 429:
            return False, "호출 한도를 넘었어 (429). 잠시 뒤 다시 확인해줘."
        if e.status:
            return False, f"네이버가 거부했어 ({e.status}). {e}"
        return False, f"연결하지 못했어 — {e}"
    return True, "연결 확인 완료 ✓ 키워드 조사를 쓸 수 있어."
