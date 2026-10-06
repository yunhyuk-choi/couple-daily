"""키워드 조사 — 후기 재료 → 롱테일 후보 → 네이버 조사 → 메인/보조 선정 (노선 2 Step 3).

생성 파이프라인에서 **본문 생성 바로 앞**에 끼는 단계다::

    리뷰 저장 → [키워드 조사] → 본문 생성 → 제목 후보
                  ↑ 여기

하는 일(규율 정본은 ``C:\\temp\\gf-blog\\naver-blog-prompt.md`` [검색어와 글의 방향]):

  1. 후기·입력에서 **직접 답할 수 있는 질문**으로 롱테일 후보 3~5개 (claude)
  2. 후보별로 **블로그 검색**(경쟁 글 제목·요약·날짜) + **트렌드 52주·8주** 조회
  3. 메인 1개 + 보조 2~4개 선정, **선정·제외 이유와 한계를 기록** (claude)

⚠️ 읽는 법 — 이 모듈이 만드는 증거 블록과 메모에 그대로 박아 둔다:

  * **검색 결과 수(total)는 검색량이 아니다.** 그 말로 쓰인 글이 몇 편인지일 뿐이다.
  * **트렌드 상대지수(ratio)는 절대 검색 횟수가 아니다.** 같이 조회한 그룹 안에서의
    상대값이라 그룹이 바뀌면 숫자도 바뀐다.
  * **롱테일 트렌드가 빈 배열이면 "수요 없음"이 아니라 "정량 확인 실패"다.**
  * 긴 검색어라서 상위 노출이 쉽다는 주장은 근거가 없다 — 쓰지 않는다.

키가 없으면 **죽지 않고 건너뛴다** — ``research()``가 ``{"status": "skipped"}``를
돌려주고, 생성은 지금까지와 똑같이 진행된다(``ai.write_review``의 ``research`` 인자는
선택이고, 없으면 "조사 안 했다" 블록이 들어간다).

보안: 자격증명은 **인자로만** 흐르고 산출 dict·로그·프롬프트 어디에도 들어가지 않는다.
"""
import logging
from datetime import date

import ai
import naver_api

log = logging.getLogger(__name__)

# 후보 하나당 블로그 검색을 두 번(sim=연관순 / date=최신순) 친다. 경쟁의 '양'과
# '최근성'은 다른 질문이라 한 정렬로는 못 본다.
BLOG_DISPLAY = 20
# 증거 블록에 넣을 경쟁 글 수(제목·요약·날짜). 프롬프트가 터지지 않게 자른다.
EVIDENCE_TITLES = 6
EVIDENCE_DESC_CHARS = 120

# 조사 결과에 항상 붙는 한계 문구 — 모델이 안 적어도 사람이 보게 된다.
DATA_CAVEAT = (
    "검색 결과 수는 검색량이 아니고, 트렌드 상대지수는 절대 검색 횟수가 아니다. "
    "경쟁 글은 제목·요약·날짜만 확인했고 본문이나 실제 노출 순위는 보지 않았다."
)


def _trend_groups(candidates):
    """후보 → 트렌드 keywordGroups (그룹 5개 제한에 맞춰 앞에서 자른다)."""
    return [
        {"groupName": c["keyword"], "keywords": c.get("variants") or [c["keyword"]]}
        for c in candidates[: naver_api.MAX_GROUPS]
    ]


def collect_evidence(candidates, client_id, client_secret, today=None):
    """후보별 네이버 조사. ``(evidence: dict, errors: list[str])``.

    ``evidence[keyword] = {"total", "recent": [...], "related": [...],
    "trend": {"52w": {...}|None, "8w": {...}|None}}``.
    한 후보가 실패해도 나머지는 계속한다 — 조사는 best-effort다.
    """
    evidence = {}
    errors = []
    for c in candidates:
        kw = c["keyword"]
        row = {"total": None, "related": [], "recent": [], "trend": {}}
        for sort, slot in (("sim", "related"), ("date", "recent")):
            try:
                res = naver_api.search_blog(
                    client_id, client_secret, kw,
                    display=BLOG_DISPLAY, sort=sort,
                )
            except naver_api.NaverApiError as e:
                errors.append(f"'{kw}' 블로그 검색({sort}) 실패 — {e}")
                continue
            row["total"] = res["total"]
            row[slot] = res["items"][:EVIDENCE_TITLES]
        evidence[kw] = row

    # 트렌드는 그룹을 한 번에 보내므로 **구간당 1회**면 된다(호출을 아낀다).
    groups = _trend_groups(candidates)
    for name, (start, end) in naver_api.trend_windows(today or date.today()).items():
        try:
            parsed = naver_api.search_trend(
                client_id, client_secret, groups, start, end
            )
        except naver_api.NaverApiError as e:
            errors.append(f"트렌드({name}) 조회 실패 — {e}")
            parsed = {}
        for kw, row in evidence.items():
            row["trend"][name] = parsed.get(kw)
    return evidence, errors


def _trend_phrase(stat):
    """트렌드 한 구간 요약 문장. 빈 배열은 **'정량 확인 실패'**로 적는다."""
    if not stat or not stat.get("points"):
        return (
            "데이터 없음 (표본이 적어 네이버가 내주지 않음 — "
            "정량 수요 확인 실패이지 수요 없음이 아님)"
        )
    return (
        f"구간 {stat['points']}개 · 평균 {stat['avg']} · 최고 {stat['peak']} · "
        f"마지막 {stat['last']} (같이 조회한 후보들 안에서의 상대값)"
    )


def format_evidence_block(candidates, evidence, errors=None):
    """조사 결과 → 선정 프롬프트에 넣을 텍스트 블록(순수 함수).

    맨 위에 **오독 금지 문구**를 박아, 모델이 블록만 보고도 숫자를 잘못 읽지 않게 한다.
    """
    out = [
        "[지표 읽는 법] 검색 결과 수(total)는 그 말로 쓰인 글 수이지 검색량이 아니다. "
        "트렌드 값은 아래 후보들 안에서의 상대값(최대 100)이지 검색 횟수가 아니다. "
        "트렌드 '데이터 없음'은 정량 확인 실패이지 수요 없음이 아니다.",
        "",
    ]
    for i, c in enumerate(candidates, 1):
        kw = c["keyword"]
        row = evidence.get(kw) or {}
        out.append(f"── 후보 {i}. {kw}")
        if c.get("question"):
            out.append(f"   묻는 것: {c['question']}")
        if c.get("answerable"):
            out.append(f"   후기가 답하는 대목: {c['answerable']}")
        total = row.get("total")
        out.append(
            f"   블로그 검색 결과 수: {total if total is not None else '조회 실패'}"
            " (검색량 아님)"
        )
        trend = row.get("trend") or {}
        out.append(f"   트렌드 52주: {_trend_phrase(trend.get('52w'))}")
        out.append(f"   트렌드 8주: {_trend_phrase(trend.get('8w'))}")
        for label, slot in (("연관순", "related"), ("최신순", "recent")):
            items = row.get(slot) or []
            if not items:
                continue
            out.append(f"   {label} 글:")
            for it in items:
                desc = (it.get("description") or "")[:EVIDENCE_DESC_CHARS]
                out.append(
                    f"     · ({it.get('postdate') or '날짜미상'}) {it.get('title')}"
                    + (f" — {desc}" if desc else "")
                )
        out.append("")
    if errors:
        out.append("[조사 실패 항목] " + " / ".join(errors[:5]))
    return "\n".join(out).strip()


def _public_evidence(candidates, evidence):
    """화면(조사 메모)에 보여줄 요약 — 원문 전체 대신 사람이 읽을 만큼만.

    DB에 들어가는 값이라 **자격증명은 물론 쓸데없는 원문도 담지 않는다.**
    """
    out = []
    for c in candidates:
        kw = c["keyword"]
        row = evidence.get(kw) or {}
        trend = row.get("trend") or {}
        out.append(
            {
                "keyword": kw,
                "question": c.get("question", ""),
                "answerable": c.get("answerable", ""),
                "total": row.get("total"),
                "trend_52w": _trend_phrase(trend.get("52w")),
                "trend_8w": _trend_phrase(trend.get("8w")),
                "trend_measured": bool(
                    (trend.get("52w") or {}).get("points")
                    or (trend.get("8w") or {}).get("points")
                ),
                "competitors": [
                    {
                        "title": it.get("title", ""),
                        "postdate": it.get("postdate", ""),
                        "link": it.get("link", ""),
                    }
                    for it in (row.get("related") or [])[:5]
                ],
            }
        )
    return out


def research(topic, location, prose, overall_score, details=None,
             client_id=None, client_secret=None, key_owner="", today=None):
    """키워드 조사 한 번. **절대 raise 하지 않고** 항상 dict를 돌려준다.

    반환 ``status``:
      * ``"skipped"`` — 키가 없다(기능 꺼짐). 생성은 그대로 진행된다.
      * ``"failed"``  — 키는 있는데 후보 생성/선정에 실패했다. 역시 생성은 진행된다.
      * ``"ok"``      — ``selection``(메인·보조·이유)과 ``candidates`` 증거가 있다.

    자격증명은 인자로만 쓰이고 **반환 dict에 절대 들어가지 않는다.**
    """
    if not (client_id or "").strip() or not (client_secret or "").strip():
        return {"status": "skipped", "reason": "no_credentials"}

    input_block = ai.build_review_input_block(
        topic, location, prose, overall_score, details
    )
    candidates = ai.suggest_keyword_candidates(input_block)
    if not candidates:
        return {"status": "failed", "reason": "no_candidates"}

    try:
        evidence, errors = collect_evidence(
            candidates, client_id, client_secret, today=today
        )
    except Exception:  # noqa: BLE001 — 조사 실패가 생성을 막지 않는다
        log.exception("keyword research: evidence collection blew up")
        evidence, errors = {}, ["조사 호출이 예외로 중단됐다"]

    selection = ai.select_keywords(
        input_block, format_evidence_block(candidates, evidence, errors), candidates
    )
    if not selection:
        return {
            "status": "failed",
            "reason": "no_selection",
            "candidates": _public_evidence(candidates, evidence),
            "errors": errors,
            "caveat": DATA_CAVEAT,
            "key_owner": key_owner,
        }
    return {
        "status": "ok",
        "selection": selection,
        "candidates": _public_evidence(candidates, evidence),
        "errors": errors,
        "caveat": DATA_CAVEAT,
        "key_owner": key_owner,
    }
