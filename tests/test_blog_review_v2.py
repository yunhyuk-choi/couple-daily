"""blog-review v2 — 입력 폼 · 프롬프트 · 출력 스키마 · 복사본 회귀 테스트.

여기서 지키려는 계약:
  * 프롬프트는 파일(`prompts/blog-review.md`)에서 오고, 빈 재료 칸은 들어가지 않는다.
  * 출력 스키마는 para/heading/image/quote + 시스템이 붙이는 ratings 하나뿐이다
    (info 요약표·faq 고정 섹션·AI가 지어낸 항목별 별점은 생성되지 않는다).
  * **옛 ai_json이 깨지지 않는다** — info/faq/sections가 있어도 화면과 복사본이 산다.
  * 발행 본문에 표(<table>)가 없다.
"""
import json

import pytest

import ai
import app as app_module
from models import db


# --------------------------------------------------------------------------- #
# 1. 프롬프트 파일 분리
# --------------------------------------------------------------------------- #
def test_prompt_file_loads_and_strips_frontmatter():
    body = ai.load_prompt("blog-review")
    assert body, "prompts/blog-review.md 를 읽지 못했다"
    # 사람용 머리말(제목 + 인용 블록)은 모델에 가지 않는다.
    assert not body.startswith("#")
    assert "ai.write_review()" not in body
    # 치환 자리는 그대로 남아 있어야 한다.
    for ph in ("{{INPUT_BLOCK}}", "{{PHOTO_BLOCK}}", "{{PHOTO_RULE}}"):
        assert ph in body


def test_prompt_encodes_the_v2_rules():
    body = ai.load_prompt("blog-review")
    assert "표를 쓰지 마라" in body
    assert "볼드" in body
    assert "글자 수를 고정하지 마라" in body
    assert "것으로 안내돼 있어요" in body      # 경험/조사 어미 구분
    assert "900~1400" not in body             # v1의 고정 분량은 사라졌다
    # 요약표·고정 Q&A·AI 별점은 '만들지 마라'로 명시돼 있다.
    assert "만들지 마라" in body and "요약표" in body


def test_missing_prompt_file_degrades_to_none(monkeypatch):
    monkeypatch.setattr(ai, "_PROMPT_CACHE", {})
    monkeypatch.setattr(ai, "_PROMPT_DIR", "/definitely/not/here")
    monkeypatch.setattr(ai, "_run_claude", lambda *a, **k: pytest.fail("호출되면 안 됨"))
    assert ai.write_review("문래갈매기", "", "맛있었다", 8, []) is None


# --------------------------------------------------------------------------- #
# 2. 재료 칸 → 프롬프트 조립
# --------------------------------------------------------------------------- #
def _capture_prompt(monkeypatch):
    """_run_claude를 가로채 프롬프트를 담아두고 최소 JSON을 돌려준다."""
    seen = {}

    def fake_run(prompt, *a, **k):
        seen["prompt"] = prompt
        return json.dumps(
            {
                "title": "문래 저녁 고기집, 웨이팅 후 먹은 문래갈매기 갈매기살",
                "title_candidates": [
                    {"title": "문래 저녁 고기집, 웨이팅 후 먹은 문래갈매기 갈매기살",
                     "structure": "쉼표 2절", "reason": "검색 단서 + 이 글만의 것"},
                    {"title": "[문래 맛집] 문래갈매기, 직접 구워주는 갈매기살 후기",
                     "structure": "대괄호 선행", "reason": "업종 라벨을 앞세움"},
                    {"title": "문래 고기집 추천 문래갈매기 갈매기살",
                     "structure": "구분자 없는 명사구", "reason": "짧게"},
                ],
                "title_pick_reason": "지역·업종이 앞에 오고 숫자 단서가 뒤에 있다.",
                "summary": "",
                "blocks": [{"type": "para", "text": "주말에 문래에 다녀왔어요."}],
                "hashtags": ["문래갈매기", "#문래맛집"],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(ai, "_run_claude", fake_run)
    return seen


def test_details_land_in_prompt_and_empty_ones_do_not(monkeypatch):
    seen = _capture_prompt(monkeypatch)
    ai.write_review(
        "문래갈매기", "서울 영등포구 문래동", "고기가 맛있었어요.", 8, [],
        details={
            "waiting": "앞에 3팀, 약 15분",
            "order_items": "갈매기살 2인분, 삿포로 생맥주",
            "researched": "평일 15시~새벽 3시",
            "highlight": "",            # 빈 칸은 들어가면 안 된다
        },
    )
    p = seen["prompt"]
    assert "앞에 3팀, 약 15분" in p
    assert "갈매기살 2인분, 삿포로 생맥주" in p
    assert "가장 기억에 남은 것" not in p          # 빈 칸은 라벨째 빠진다
    # 조사 정보는 '겪은 것'과 구분되게 표시된다.
    assert "검색으로 확인한 공개 정보" in p
    # 치환이 끝나 자리표시자가 남아 있지 않다.
    for ph in ("{{INPUT_BLOCK}}", "{{PHOTO_BLOCK}}", "{{PHOTO_RULE}}"):
        assert ph not in p


def test_write_review_returns_title_candidates(monkeypatch):
    _capture_prompt(monkeypatch)
    out = ai.write_review("문래갈매기", "", "맛있었어요.", 8, [])
    assert out["title"].startswith("문래 저녁 고기집")
    assert len(out["title_candidates"]) == 3
    assert out["title_pick_reason"]
    assert out["hashtags"][0] == "#문래갈매기"      # # 자동 부착


# --------------------------------------------------------------------------- #
# 3. 출력 스키마 정규화
# --------------------------------------------------------------------------- #
def test_ai_cannot_emit_info_faq_or_its_own_ratings():
    out = ai._normalize_review(
        {
            "title": "제목",
            "blocks": [
                {"type": "para", "text": "본문"},
                {"type": "info", "items": [{"label": "위치", "value": "성수"}]},
                {"type": "faq", "items": [{"q": "주차?", "a": "없음"}]},
                {"type": "ratings", "items": [{"aspect": "분위기", "score": 9}]},
            ],
        },
        photos=[],
        overall_score=7,
    )
    types = [b["type"] for b in out["blocks"]]
    assert "info" not in types
    assert "faq" not in types
    # 별점은 사용자 총점 하나만, 맨 끝에.
    assert types.count("ratings") == 1
    assert out["blocks"][-1] == {
        "type": "ratings",
        "items": [{"aspect": "전체 만족도", "score": 7}],
    }


def test_summary_is_optional_and_absorbed_as_first_para():
    empty = ai._normalize_review(
        {"title": "T", "summary": "", "blocks": [{"type": "para", "text": "본문"}]},
        photos=[], overall_score=5,
    )
    assert empty["blocks"][0]["text"] == "본문"

    filled = ai._normalize_review(
        {"title": "T", "summary": "웨이팅은 15분이었어요.",
         "blocks": [{"type": "para", "text": "본문"}]},
        photos=[], overall_score=5,
    )
    assert filled["blocks"][0] == {"type": "para", "text": "웨이팅은 15분이었어요."}


def test_selected_title_is_always_among_candidates():
    out = ai._normalize_review(
        {
            "title": "후보에 없던 제목",
            "title_candidates": [{"title": "후보 A"}, {"title": "후보 B"}],
            "blocks": [{"type": "para", "text": "본문"}],
        },
        photos=[], overall_score=5,
    )
    assert out["title_candidates"][0]["title"] == "후보에 없던 제목"
    assert [c["title"] for c in out["title_candidates"]][1:] == ["후보 A", "후보 B"]


def test_normalize_rejects_titleless_or_textless_output():
    assert ai._normalize_review(
        {"blocks": [{"type": "para", "text": "본문"}]}, [], 5
    ) is None
    assert ai._normalize_review(
        {"title": "T", "blocks": [{"type": "image", "photo_index": 0}]}, [], 5
    ) is None


# --------------------------------------------------------------------------- #
# 4. 복사본(copy_text / copy_html) — 새 스키마 + 옛 데이터 호환
# --------------------------------------------------------------------------- #
_NEW_AI = {
    "title": "문래 저녁 고기집, 웨이팅 후 먹은 문래갈매기 갈매기살",
    "title_candidates": [
        {"title": "문래 저녁 고기집, 웨이팅 후 먹은 문래갈매기 갈매기살",
         "structure": "쉼표 2절", "reason": "r1"},
        {"title": "[문래 맛집] 문래갈매기 갈매기살 후기",
         "structure": "대괄호 선행", "reason": "r2"},
    ],
    "blocks": [
        {"type": "para", "text": "주말에 문래에 다녀왔어요."},
        {"type": "heading", "text": "문래갈매기 웨이팅은 15분 정도였어요"},
        {"type": "para", "text": "앞에 3팀이 있었어요."},
        {"type": "ratings", "items": [{"aspect": "전체 만족도", "score": 8}]},
    ],
    "hashtags": ["#문래갈매기"],
}

# P1/P2 초기 스키마(summary/sections/info_block/faq/ratings) — 운영 DB에 남아 있다.
_OLD_AI = {
    "title": "옛 후기",
    "summary": "한줄평입니다.",
    "info_block": [{"label": "위치", "value": "성수동"}],
    "sections": [{"heading": "분위기", "text": "좋았어요.", "photo_index": 0}],
    "ratings": [{"aspect": "분위기", "score": 9}],
    "faq": [{"q": "주차 되나요?", "a": "어려워요."}],
    "hashtags": ["#성수"],
}


def test_copy_text_new_schema(flask_app, make_review):
    review = make_review(ai_json=json.dumps(_NEW_AI, ensure_ascii=False),
                         status="ready")
    text = app_module._review_copy_text(review)
    assert text.startswith(_NEW_AI["title"])
    assert "문래갈매기 웨이팅은 15분 정도였어요" in text
    assert "총점 ★★★★☆ (8/10)" in text
    assert "⭐ 별점" not in text        # v2: 머리글 없이 한 줄
    assert "#문래갈매기" in text


def test_copy_text_still_renders_old_schema(flask_app, make_review):
    review = make_review(ai_json=json.dumps(_OLD_AI, ensure_ascii=False),
                         status="ready")
    text = app_module._review_copy_text(review)
    assert "한줄평입니다." in text        # summary → para
    assert "분위기" in text               # section heading
    assert "▶ 핵심 정보" in text          # info_block 은 계속 그린다
    assert "❓ 자주 묻는 질문" in text     # faq 도


def test_copy_html_has_no_table_in_either_schema(flask_app, make_review):
    for payload in (_NEW_AI, _OLD_AI):
        review = make_review(ai_json=json.dumps(payload, ensure_ascii=False),
                             status="ready")
        html = app_module._review_copy_html(review)
        assert html
        assert "<table" not in html        # 표 금지(gf-blog 규율)
        assert "<b>" not in html           # 볼드 금지
        assert "총점 ★★★★☆ (8/10)" in html or "총점 " in html


def test_copy_html_closing_has_no_overclaim(flask_app, make_review):
    review = make_review(ai_json=json.dumps(_NEW_AI, ensure_ascii=False),
                         status="ready")
    html = app_module._review_copy_html(review)
    assert "추천드려요" not in html
    assert "변동될 수 있으니" in html


# --------------------------------------------------------------------------- #
# 5. 폼 유효성 · 저장 · 제목 선택 라우트
# --------------------------------------------------------------------------- #
def _form(**over):
    base = {
        "topic": "문래갈매기",
        "location": "서울 영등포구 문래동",
        "prose": "직원분이 직접 구워주셨어요.",
        "overall_score": "8",
        "photo_ids": "[]",
        "visited_when": "일요일 저녁",
        "waiting": "앞에 3팀, 약 15분",
        "order_items": "갈매기살 2인분, 삿포로 생맥주",
        "researched": "평일 15시~새벽 3시",
    }
    base.update(over)
    return base


def test_new_review_saves_detail_fields(client, flask_app):
    from models import BlogReview
    resp = client.post("/reviews/new", data=_form(), follow_redirects=False)
    assert resp.status_code == 302
    review = BlogReview.query.one()
    assert review.visited_when == "일요일 저녁"
    assert review.waiting == "앞에 3팀, 약 15분"
    assert review.researched == "평일 15시~새벽 3시"
    # 안 적은 칸은 NULL — '모른다'와 같은 뜻이다.
    assert review.highlight is None
    assert review.details["order_items"] == "갈매기살 2인분, 삿포로 생맥주"


def test_detail_fields_are_all_optional(client, flask_app):
    from models import BlogReview
    data = {k: v for k, v in _form().items()
            if k in ("topic", "prose", "overall_score", "photo_ids")}
    resp = client.post("/reviews/new", data=data)
    assert resp.status_code == 302
    review = BlogReview.query.one()
    assert review.details == {}


def test_required_fields_still_enforced(client, flask_app):
    from models import BlogReview
    assert client.post("/reviews/new", data=_form(topic="")).status_code == 200
    assert client.post("/reviews/new", data=_form(prose="")).status_code == 200
    assert client.post("/reviews/new", data=_form(overall_score="")).status_code == 200
    assert BlogReview.query.count() == 0


def test_detail_fields_are_length_capped(client, flask_app):
    from models import BlogReview
    client.post("/reviews/new", data=_form(visited_when="가" * 500))
    assert len(BlogReview.query.one().visited_when) == 100


def test_pick_title_swaps_title_and_resets_edited_text(client, flask_app, make_review):
    review = make_review(ai_json=json.dumps(_NEW_AI, ensure_ascii=False),
                         status="ready", edited_text="<p>손으로 고친 것</p>")
    resp = client.post(f"/reviews/{review.id}/title", data={"index": "1"})
    assert resp.status_code == 302
    db.session.refresh(review)
    assert review.ai["title"] == "[문래 맛집] 문래갈매기 갈매기살 후기"
    assert review.edited_text is None
    # 후보 목록 자체는 보존된다(다시 되돌릴 수 있어야 한다).
    assert len(review.ai["title_candidates"]) == 2


def test_pick_title_rejects_out_of_range(client, flask_app, make_review):
    review = make_review(ai_json=json.dumps(_NEW_AI, ensure_ascii=False),
                         status="ready")
    assert client.post(f"/reviews/{review.id}/title",
                       data={"index": "9"}).status_code == 400
    assert client.post(f"/reviews/{review.id}/title",
                       data={"index": "x"}).status_code == 400


# --------------------------------------------------------------------------- #
# 6. 화면이 뜨는가 (옛 후기 포함)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [_NEW_AI, _OLD_AI])
def test_detail_page_renders(client, flask_app, make_review, payload):
    review = make_review(ai_json=json.dumps(payload, ensure_ascii=False),
                         status="ready", waiting="앞에 3팀, 약 15분")
    resp = client.get(f"/reviews/{review.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "앞에 3팀, 약 15분" in body          # 재료 칸 표시
    assert payload["title"] in body


def test_detail_page_shows_title_candidates(client, flask_app, make_review):
    review = make_review(ai_json=json.dumps(_NEW_AI, ensure_ascii=False),
                         status="ready")
    body = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "제목 후보" in body
    assert "[문래 맛집] 문래갈매기 갈매기살 후기" in body


def test_detail_page_hides_picker_for_legacy_reviews(client, flask_app, make_review):
    review = make_review(ai_json=json.dumps(_OLD_AI, ensure_ascii=False),
                         status="ready")
    body = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "제목 후보" not in body


def test_form_page_renders_new_fields(client, flask_app):
    body = client.get("/reviews/new").get_data(as_text=True)
    for name in ("visited_when", "visit_reason", "access_note", "waiting",
                 "order_items", "highlight", "downside", "researched"):
        assert f'name="{name}"' in body
    assert "모르면 그냥 비워 둬" in body
