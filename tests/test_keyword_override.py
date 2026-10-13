"""키워드를 **사람이 조율한다** — 그리고 사람 손이 닿은 것이 이긴다.

문제였던 것: 키워드는 AI 가 조사해서 고른 뒤 그대로 글이 됐고, 방향이 틀렸을 때
사람이 고칠 창구가 **없었다.** 창구를 만들어도 다음 생성에서 AI 가 덮어쓰면 그
기능은 있으나 마나다 — 그래서 여기서 못 박는 것은 창구가 아니라 **우선순위**다.

계약 다섯:

1. 사람이 고친 것은 조사 결과(``selection``)를 **덮어쓰지 않고 옆(``override``)에**
   남는다 — 조사 근거는 증거로 살아 있어야 하고, 다음 조사가 같은 칸을 다시 쓴다.
2. 프롬프트에 먹는 키워드는 **override > selection**.
3. 다음 생성에서 **AI 가 다시 덮지 못한다**(이게 이 기능의 전부다).
4. 적용은 **다음 생성부터** — 저장이 claude 를 부르지 않는다.
5. 사람이 메인을 바꿨으면 조사 쪽 '선정 이유·독자가 읽어야 할 것'은 따라가지
   않는다(그건 *다른 키워드*에 대한 설명이다).
"""
import json

import pytest

import ai
import app as app_module
import keyword_research
from models import BlogReview, db

_AI = {
    "title": "문래갈매기",
    "blocks": [{"type": "para", "text": "맛있었어요."}],
    "hashtags": ["#문래갈매기"],
}

_RESEARCH_OK = {
    "status": "ok",
    "selection": {
        "main": "문래 갈매기살 웨이팅",
        "supporting": ["문래갈매기 가격"],
        "main_reason": "후기에 앞 3팀·15분 기록이 있다",
        "angle": "주말 저녁 실제 대기",
        "limits": "롱테일 트렌드가 비어 정량 수요 미확인",
    },
    "candidates": [],
    "errors": [],
    "caveat": keyword_research.DATA_CAVEAT,
    "key_owner": "테스터",
}


def _ready(make_review, research=None):
    return make_review(
        ai_json=json.dumps(_AI, ensure_ascii=False),
        status="ready",
        research_json=json.dumps(research or _RESEARCH_OK, ensure_ascii=False),
    )


# --------------------------------------------------------------------------- #
# 1. 순수 함수 — 정규화 · 우선순위
# --------------------------------------------------------------------------- #
def test_first_line_is_main_and_the_rest_are_supporting():
    ov = keyword_research.normalize_override(
        ["문래 갈매기살 웨이팅", "문래 고기집", "문래 데이트"]
    )
    assert ov["main"] == "문래 갈매기살 웨이팅"
    assert ov["supporting"] == ["문래 고기집", "문래 데이트"]


def test_blanks_and_duplicates_and_overlong_entries_are_cleaned():
    ov = keyword_research.normalize_override(
        ["  문래  갈매기 ", "", "문래 갈매기", "   ", "가" * 200]
    )
    assert ov["main"] == "문래 갈매기"          # 안쪽 공백도 하나로 접는다
    assert len(ov["supporting"]) == 1
    assert len(ov["supporting"][0]) == keyword_research.MAX_KEYWORD_LEN


def test_an_empty_list_is_no_override_not_an_empty_one():
    assert keyword_research.normalize_override([]) is None
    assert keyword_research.normalize_override(["", "  "]) is None


def test_supporting_is_bounded():
    ov = keyword_research.normalize_override([f"키워드{i}" for i in range(40)])
    assert len(ov["supporting"]) == keyword_research.MAX_SUPPORTING


def test_override_sits_beside_the_research_not_on_top_of_it():
    merged = keyword_research.apply_override(
        _RESEARCH_OK, {"main": "사람 메인", "supporting": []}
    )
    assert merged["selection"]["main"] == "문래 갈매기살 웨이팅", "조사 근거는 남는다"
    assert merged["override"]["main"] == "사람 메인"
    assert "override" not in _RESEARCH_OK, "원본을 고치지 않는다"


def test_clearing_the_override_gives_the_research_back():
    merged = keyword_research.apply_override(_RESEARCH_OK, {"main": "사람 메인"})
    back = keyword_research.apply_override(merged, None)
    assert "override" not in back
    assert keyword_research.effective_keywords(back)["main"] == "문래 갈매기살 웨이팅"


def test_human_keywords_win_over_the_research():
    merged = keyword_research.apply_override(
        _RESEARCH_OK, {"main": "사람 메인", "supporting": ["사람 보조"]}
    )
    eff = keyword_research.effective_keywords(merged)
    assert eff == {"main": "사람 메인", "supporting": ["사람 보조"], "by_human": True}


def test_keywords_can_exist_without_any_research_at_all():
    """키가 없어 조사가 꺼져 있어도 사람이 직접 넣을 수 있어야 한다."""
    merged = keyword_research.apply_override(None, {"main": "직접 넣은 것"})
    eff = keyword_research.effective_keywords(merged)
    assert eff["main"] == "직접 넣은 것" and eff["by_human"] is True
    assert ai.format_keyword_block(merged) != ai.NO_KEYWORD_BLOCK


# --------------------------------------------------------------------------- #
# 2. 프롬프트 블록
# --------------------------------------------------------------------------- #
def test_prompt_block_carries_the_human_keyword_and_says_it_is_human():
    merged = keyword_research.apply_override(
        _RESEARCH_OK, {"main": "사람 메인", "supporting": ["사람 보조"]}
    )
    block = ai.format_keyword_block(merged)
    assert "사람 메인" in block and "사람 보조" in block
    assert "문래 갈매기살 웨이팅" not in block
    assert "사람이 직접" in block


def test_prompt_block_drops_reasons_that_belonged_to_the_old_main():
    """'선정 이유'는 *그 키워드*에 대한 설명이다 — 메인이 바뀌면 따라오면 안 된다."""
    merged = keyword_research.apply_override(_RESEARCH_OK, {"main": "전혀 다른 말"})
    block = ai.format_keyword_block(merged)
    assert "앞 3팀" not in block and "주말 저녁 실제 대기" not in block
    # 데이터 한계는 키워드가 아니라 조사 품질 이야기라 여전히 참이다.
    assert "롱테일 트렌드가 비어" in block


def test_prompt_block_keeps_the_reasons_when_the_human_only_edited_supporting():
    merged = keyword_research.apply_override(
        _RESEARCH_OK, {"main": "문래 갈매기살 웨이팅", "supporting": ["내가 넣은 보조"]}
    )
    block = ai.format_keyword_block(merged)
    assert "앞 3팀" in block and "내가 넣은 보조" in block


def test_write_review_actually_sees_the_human_keyword(monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "_run_claude", lambda p, **k: seen.append(p) or json.dumps(
        {"title": "t", "blocks": [{"type": "para", "text": "x" * 1400}],
         "hashtags": ["#a"]}))
    merged = keyword_research.apply_override(_RESEARCH_OK, {"main": "사람이 고른 말"})
    ai.write_review("문래갈매기", "", "맛있었다", 8, [], research=merged)
    assert "사람이 고른 말" in seen[0]


# --------------------------------------------------------------------------- #
# 3. 라우트 — 저장 · 추가 · 삭제 · 되돌리기
# --------------------------------------------------------------------------- #
def test_saving_keywords_stores_them_as_an_override(client, flask_app, make_review):
    review = _ready(make_review)
    resp = client.post(f"/reviews/{review.id}/keywords",
                       data={"keywords": "사람 메인\n보조 하나\n보조 둘"})
    assert resp.status_code == 302
    db.session.refresh(review)
    assert review.research["override"]["main"] == "사람 메인"
    assert review.research["override"]["supporting"] == ["보조 하나", "보조 둘"]
    assert review.research["selection"]["main"] == "문래 갈매기살 웨이팅"


def test_deleting_a_badge_is_just_a_shorter_list(client, flask_app, make_review):
    """배지 삭제·수정·추가는 전부 '줄 목록을 다시 보낸다' 하나로 끝난다."""
    review = _ready(make_review)
    client.post(f"/reviews/{review.id}/keywords",
                data={"keywords": "메인\n보조 A\n보조 B"})
    client.post(f"/reviews/{review.id}/keywords", data={"keywords": "메인\n보조 B"})
    db.session.refresh(review)
    assert review.research["override"]["supporting"] == ["보조 B"]


def test_promoting_a_supporting_keyword_swaps_the_main(client, flask_app, make_review):
    review = _ready(make_review)
    client.post(f"/reviews/{review.id}/keywords", data={"keywords": "보조 B\n메인"})
    db.session.refresh(review)
    assert review.research["override"]["main"] == "보조 B"


def test_reset_puts_the_research_back(client, flask_app, make_review):
    review = _ready(make_review)
    client.post(f"/reviews/{review.id}/keywords", data={"keywords": "사람 메인"})
    client.post(f"/reviews/{review.id}/keywords", data={"reset": "1"})
    db.session.refresh(review)
    assert keyword_research.read_override(review.research) is None
    assert keyword_research.effective_keywords(review.research)["by_human"] is False


def test_saving_an_empty_list_clears_rather_than_storing_nothing(
    client, flask_app, make_review
):
    review = _ready(make_review)
    client.post(f"/reviews/{review.id}/keywords", data={"keywords": "사람 메인"})
    client.post(f"/reviews/{review.id}/keywords", data={"keywords": "\n   \n"})
    db.session.refresh(review)
    assert keyword_research.read_override(review.research) is None


def test_saving_keywords_never_touches_the_draft_or_calls_claude(
    client, flask_app, make_review, monkeypatch
):
    """⭐ 적용은 **다음 생성부터** — 저장이 몰래 claude 를 부르지 않는다."""
    review = _ready(make_review)
    monkeypatch.setattr(
        ai, "_run_claude",
        lambda *a, **k: pytest.fail("키워드 저장이 claude 를 부르면 안 된다"),
    )
    spawned = []
    monkeypatch.setattr(app_module, "_spawn_generate_review",
                        lambda *a, **k: spawned.append(a))
    client.post(f"/reviews/{review.id}/keywords", data={"keywords": "사람 메인"})
    db.session.refresh(review)
    assert spawned == [], "저장이 재생성을 걸면 안 된다"
    assert review.status == "ready" and review.ai["title"] == "문래갈매기"


def test_keywords_of_another_couple_are_404(client, flask_app, make_review):
    from models import Couple, User
    other = Couple(invite_code="OTHERKEY")
    db.session.add(other)
    db.session.flush()
    stranger = User(email="z@example.com", password_hash="x", display_name="남",
                    couple_id=other.id, status="approved")
    db.session.add(stranger)
    db.session.flush()
    theirs = BlogReview(couple_id=other.id, created_by=stranger.id,
                        topic="남의 후기", prose="x", overall_score=5)
    db.session.add(theirs)
    db.session.commit()
    resp = client.post(f"/reviews/{theirs.id}/keywords", data={"keywords": "뺏기"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 4. ⭐ 다음 생성에서 AI 가 덮지 못한다
# --------------------------------------------------------------------------- #
def test_regeneration_keeps_the_human_keywords_and_skips_the_research(
    flask_app, make_review, monkeypatch, couple_user
):
    """핵심 — 사람이 고친 키워드가 다음 생성에서 **살아남는다.**

    그리고 조사를 다시 돌리지 않는다: 조사가 내놓는 본체(메인·보조)는 어차피
    override 에 밀려 버려지므로, 그걸 받으려고 claude 를 두 번 더 부르는 것은
    낭비다(근거는 지난 메모에 남아 있다).
    """
    couple_user.naver_api_key_id = "ID"
    couple_user.naver_api_key = "SECRET"
    review = _ready(make_review)
    db.session.commit()

    client_side = keyword_research.apply_override(
        review.research, {"main": "사람이 고른 메인", "supporting": ["사람 보조"]}
    )
    review.research_json = json.dumps(client_side, ensure_ascii=False)
    db.session.commit()
    rid = review.id

    researched = []
    monkeypatch.setattr(
        keyword_research, "research",
        lambda *a, **k: researched.append(1) or {
            "status": "ok", "selection": {"main": "AI 가 다시 고른 말", "supporting": []},
        },
    )
    seen = {}
    monkeypatch.setattr(
        ai, "write_review",
        lambda *a, **k: seen.update(research=k.get("research")) or {
            "title": "새 초안", "blocks": [{"type": "para", "text": "본문"}],
            "hashtags": ["#a"],
        },
    )
    monkeypatch.setattr(app_module, "_attach_section_crops", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_notify_review_done", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_enqueue_ai", lambda *a, **k: True)

    app_module.generate_review(flask_app, rid)
    # 워커는 **자기 app_context** 안에서 돌았다 → Flask-SQLAlchemy 3.x 는 세션을
    # app context 에 묶으므로 이 테스트 세션의 객체는 낡아 있다. 다시 읽는다.
    db.session.expire_all()

    assert researched == [], "override 가 있으면 조사를 다시 돌리지 않는다"
    assert ai.format_keyword_block(seen["research"]).startswith(
        "메인 키워드: 사람이 고른 메인"
    )
    saved = db.session.get(BlogReview, rid)
    assert saved.research["override"]["main"] == "사람이 고른 메인"
    assert saved.ai["title"] == "새 초안"


def test_without_an_override_the_research_still_runs_and_wins(
    flask_app, make_review, monkeypatch, couple_user
):
    """되돌리기를 누르면 다음 생성부터 조사가 다시 돈다 — 영구 고정이 아니다."""
    couple_user.naver_api_key_id = "ID"
    couple_user.naver_api_key = "SECRET"
    review = _ready(make_review)
    db.session.commit()
    rid = review.id

    researched = []
    monkeypatch.setattr(
        keyword_research, "research",
        lambda *a, **k: researched.append(1) or {
            "status": "ok", "selection": {"main": "AI 가 새로 고른 말", "supporting": []},
        },
    )
    monkeypatch.setattr(ai, "write_review", lambda *a, **k: {
        "title": "새 초안", "blocks": [{"type": "para", "text": "본문"}],
        "hashtags": ["#a"]})
    monkeypatch.setattr(app_module, "_attach_section_crops", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_notify_review_done", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_enqueue_ai", lambda *a, **k: True)

    app_module.generate_review(flask_app, rid)
    db.session.expire_all()

    assert researched == [1]
    saved = db.session.get(BlogReview, rid)
    assert keyword_research.effective_keywords(saved.research) == {
        "main": "AI 가 새로 고른 말", "supporting": [], "by_human": False
    }


# --------------------------------------------------------------------------- #
# 5. 화면
# --------------------------------------------------------------------------- #
def test_detail_shows_the_keyword_editor_with_main_first(
    client, flask_app, make_review
):
    review = _ready(make_review)
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert 'name="keywords"' in html
    assert "문래 갈매기살 웨이팅\n문래갈매기 가격" in html   # 첫 줄이 메인
    assert "이 키워드로 다시 쓰기" in html


def test_detail_says_when_a_human_edited_the_keywords(client, flask_app, make_review):
    review = _ready(make_review)
    client.post(f"/reviews/{review.id}/keywords", data={"keywords": "사람 메인"})
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "사람이 고친 것" in html
    assert "조사 결과로 되돌리기" in html
    assert "사람 메인" in html


def test_the_editor_shows_up_even_when_research_was_skipped(
    client, flask_app, make_review
):
    review = _ready(make_review, research={"status": "skipped",
                                           "reason": "no_credentials"})
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert 'name="keywords"' in html, "키가 없어도 직접 넣을 창구는 있어야 한다"
