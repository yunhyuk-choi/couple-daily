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
