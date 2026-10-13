"""시스템 크롬 정리 — 상투구·방문 날짜·이모지 디바이더를 본문에서 뺀 계약.

사용자 지시(2026-10-13):
  "내돈내산 후기 뭐 이딴거 붙는데 제발 그딴거 넣지좀 마. 제거하고 방문 날짜 이런것도
   별로 안중요하니까 제거하고 이모지로 넣어지는 디바이더들도 제거해. 디바이더 넣고
   싶으면 네이버 (기본) 디바이더 사용하도록해"

셋 다 **빌더가 박던 것**이지 모델이 쓴 것이 아니다(원인은 코드와 프롬프트 예시).
그래서 계약도 빌더 쪽에 건다 — 발행 HTML·네이티브 문서·내보내기 페이로드·프롬프트.

여기서 쓰는 '구분선'은 네이버가 기본으로 주는 것 하나뿐이다:
  · 네이티브  ``{'@ctype':'horizontalLine', layout:'line1'}``  (``hrComp``)
  · HTML     ``<hr style="border:0;border-top:1px solid #e5e5e5;...">``
둘 다 **이미 이 레포(와 자매 레포 naver-ent-autopost)가 쓰던 모양 그대로**다.
"""
import json
import re
from pathlib import Path

import app as app_module

USERSCRIPT = Path(__file__).resolve().parents[1] / "tools" / "naver-uploader.user.js"
PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "blog-review.md"

# 사용자가 "이딴거"라고 부른 상투적 자랑·면책 문구. (이 레포에는 협찬·대가성 후기가
# 없으므로 고지할 법적 사실 자체가 없다 — 즉 이건 고지가 아니라 장식이었다.)
CLICHES = ("내돈내산", "솔직후기", "솔직 후기", "광고 아님", "협찬 아님", "협찬받지")

_AI = {
    "title": "문래 저녁 고기집, 웨이팅 후 먹은 문래갈매기 갈매기살",
    "blocks": [
        {"type": "para", "text": "주말에 문래에 다녀왔어요."},
        {"type": "heading", "text": "문래갈매기 웨이팅은 15분 정도였어요"},
        {"type": "para", "text": "앞에 3팀이 있었어요."},
        {"type": "heading", "text": "다시 생각나는 곳"},
        {"type": "para", "text": "양념이 좋았어요."},
        {"type": "quote", "text": "한 줄 매듭이에요."},
    ],
    "hashtags": ["#문래갈매기"],
}


def _ready(make_review, payload=None):
    return make_review(
        ai_json=json.dumps(payload or _AI, ensure_ascii=False), status="ready"
    )


# --------------------------------------------------------------------------- #
# 1. 「내돈내산」류 상투구 — 어느 경로에도 남지 않는다
# --------------------------------------------------------------------------- #
def test_copy_html_has_no_cliche_badge(flask_app, make_review):
    html = app_module._review_copy_html(_ready(make_review))
    assert html
    for bad in CLICHES:
        assert bad not in html, f"발행 HTML 에 상투구가 남았다: {bad}"


def test_userscript_has_no_cliche_badge():
    src = USERSCRIPT.read_text(encoding="utf-8")
    for bad in CLICHES:
        assert bad not in src, f"유저스크립트에 상투구가 남았다: {bad}"


def test_prompt_bans_the_cliche_outright():
    src = PROMPT.read_text(encoding="utf-8")
    assert "내돈내산" in src, "프롬프트가 상투구를 **금지어로** 명시해야 한다"
    # 금지 맥락에서만 등장해야 한다 — 제목 구조 예시로 베껴 쓰게 두면 안 된다.
    assert "`상호 솔직후기 |" not in src


# --------------------------------------------------------------------------- #
# 2. 방문 날짜 — 본문에 '정보'로 박지 않는다 (입력 칸 visited_when 은 그대로 둔다)
# --------------------------------------------------------------------------- #
def test_copy_html_has_no_visit_date_header(flask_app, make_review):
    html = app_module._review_copy_html(_ready(make_review))
    assert "방문 날짜" not in html
    assert not re.search(r"\d{4}\.\s*\d{2}", html), "YYYY. MM 방문월 스탬프가 남았다"


def test_copy_html_quote_footer_is_gone_entirely(flask_app, make_review):
    """인용구 꼬리표는 `YYYY. MM 방문 · 내돈내산` 뿐이었다 — 둘 다 빠지면 꼬리표가 없다."""
    html = app_module._review_copy_html(_ready(make_review))
    assert "<blockquote" in html          # 매듭 자체는 남는다
    assert "방문 ·" not in html
    assert "margin-top:8px" not in html   # 꼬리표 <span> 의 흔적


def test_export_payload_carries_no_visit_date(flask_app, make_review):
    doc, _images = app_module._build_export_payload(_ready(make_review))
    assert doc is not None
    assert "visitDate" not in doc
    assert "visitFoot" not in doc


def test_userscript_does_not_read_visit_date():
    src = USERSCRIPT.read_text(encoding="utf-8")
    assert "visitDate" not in src
    assert "visitFoot" not in src
    # 주석으로 '무엇을 뺐는지' 설명하는 건 괜찮다 — 막는 건 **문자열 리터럴로 찍는 것**.
    assert "tn('방문 날짜" not in src


def test_visited_when_is_still_an_input_and_still_reaches_the_prompt():
    """사용자가 적는 칸은 살아 있다 — 뺀 것은 *날짜 스탬프*지 '언제 갔는지'가 아니다."""
    import ai as ai_module

    assert ("visited_when", "언제 갔는지(요일·시간대)") in ai_module.REVIEW_DETAIL_FIELDS
    block = ai_module.build_review_input_block(
        "문래갈매기", "문래동", "맛있었어요", 8, {"visited_when": "일요일 저녁"}
    )
    assert "일요일 저녁" in block


# --------------------------------------------------------------------------- #
# 3. 디바이더 — 이모지 장식 대신 네이버 기본 구분선
# --------------------------------------------------------------------------- #
EMOJI_DIVIDER_BITS = ("· · ·", "☕️", "🍰", "📷", "🌿", "🍽️", "🤍", "✱")


def test_copy_html_has_no_emoji_divider(flask_app, make_review):
    html = app_module._review_copy_html(_ready(make_review))
    for bad in EMOJI_DIVIDER_BITS:
        assert bad not in html, f"이모지 디바이더 조각이 남았다: {bad}"


def test_copy_html_separates_headings_with_the_naver_default_rule(flask_app, make_review):
    """소제목 앞 장식은 네이버 기본 구분선 하나다 — 이 레포가 이미 쓰던 `<hr>` 모양."""
    html = app_module._review_copy_html(_ready(make_review))
    hr = '<hr style="border:0;border-top:1px solid #e5e5e5;margin:26px 0;">'
    assert hr in html
    lines = html.split("\n")
    h3_at = [i for i, ln in enumerate(lines) if ln.startswith("<h3")]
    assert len(h3_at) == 2, "소제목 두 개가 <h3> 로 나와야 한다"
    for i in h3_at:
        # 소제목 **바로 앞**이 구분선이어야 한다 — 사이에 장식 문단이 끼면 안 된다.
        assert lines[i - 1] == hr, f"소제목 앞 줄이 구분선이 아니다: {lines[i - 1]!r}"


def test_userscript_keeps_the_naver_default_horizontal_line():
    src = USERSCRIPT.read_text(encoding="utf-8")
    assert "'@ctype': 'horizontalLine', id: sid(), layout: 'line1'" in src
    for bad in EMOJI_DIVIDER_BITS:
        assert bad not in src, f"유저스크립트에 이모지 디바이더 조각이 남았다: {bad}"
