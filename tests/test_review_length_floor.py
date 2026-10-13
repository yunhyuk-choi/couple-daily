"""본문 길이 — **상한이 아니라 하한.**

사용자 지적(2026-10-13):
    "게시글 길이가 너무 짧아. 지금 뭔가 글 하나에 글자수 상한이 걸려있는 것 같은데,
     상한을 걸게 아니라 하한을 걸어두고 그 이상을 쓰게 해야지."

하한 숫자의 근거는 **레퍼런스 실측**(`naver-ent-autopost/docs/REFERENCE-STYLE.md` §4,
같은 작성자 블로그 32편):

    A 코호트 7편(우리가 따라 쓰는 문체)  최소 1,289 · p25 1,504 · 중앙 1,570 · 최대 3,350
    2026 전체 23편                      최소   630 · p25 1,148 · 중앙 1,548 · 최대 3,350
    그 문서의 결론                       "1,200~2,000자를 기대 범위로"

→ 하한 **1,200자**. A 코호트 최소값(1,289)보다 낮으니 레퍼런스 글은 하나도 안 걸린다.
상한은 없다(3,350자가 실재한다).

세는 법은 레퍼런스 집계(`collect_reference_blog.py` 의
``n_chars = sum(len(p) for p in paragraphs)``)와 **같다**:
  * **소제목 제외** — para·quote 만 센다.
  * **강조 토큰 제외** — `**` `==` `*` 백틱은 발행본에서 서식이 되어 사라지는 표시다.
    세면 모델이 마크업을 뿌려 하한을 '채울' 수 있다.

⚠️ 하한은 "재료 안에서 충실히 쓰라"는 뜻이지 **살을 붙이라는 뜻이 아니다** — 되먹임은
   **한 번뿐**이고, 두 번째도 길어지지 않으면 첫 결과를 그대로 쓴다.
"""
import ai


def _blocks(para_text, heading_text="소제목이야", quote_text=""):
    blocks = [{"type": "heading", "text": heading_text},
              {"type": "para", "text": para_text}]
    if quote_text:
        blocks.append({"type": "quote", "text": quote_text})
    return {"title": "제목", "blocks": blocks, "hashtags": []}


# --------------------------------------------------------------------------- #
# 1. 세는 법 — 레퍼런스와 같은 정의
# --------------------------------------------------------------------------- #
def test_headings_do_not_count_toward_the_floor():
    """레퍼런스 집계가 소제목을 빼므로 우리도 뺀다 — 안 그러면 숫자가 비교 불가다."""
    out = ai.review_body_chars(_blocks("가" * 100, heading_text="나" * 50))
    assert out == 100


def test_quotes_count_but_images_and_ratings_do_not():
    data = {"blocks": [
        {"type": "para", "text": "가" * 40},
        {"type": "quote", "text": "나" * 10},
        {"type": "image", "photo_index": 0},
        {"type": "ratings", "items": [{"aspect": "전체 만족도", "score": 8}]},
    ]}
    assert ai.review_body_chars(data) == 50


def test_emphasis_tokens_are_stripped_before_counting():
    """발행본에서 사라지는 표시로 하한을 채울 수 없다."""
    plain = "웨이팅은 15분이었고 문래갈매기는 7번 출구에서 3분 거리예요"
    marked = "웨이팅은 **15분**이었고 *문래갈매기*는 `7번 출구`에서 3분 거리예요"
    assert ai.review_body_chars(_blocks(marked)) == len(plain)
    # ==형광펜== 도 마찬가지
    assert ai.review_body_chars(_blocks("==꼭 양념으로==")) == len("꼭 양념으로")


def test_the_strip_rule_has_one_definition():
    """app 쪽 벗기기가 ai 쪽과 갈라지면 '발행본에 남는 글자'의 정의가 갈라진다."""
    import app as app_module
    s = "**굵게** ==형광== *분홍* `파랑`"
    assert app_module._strip_emph(s) == ai.strip_emphasis(s) == "굵게 형광 분홍 파랑"


# --------------------------------------------------------------------------- #
# 2. 하한값 — 실측 근거와 맞물려 있다
# --------------------------------------------------------------------------- #
def test_the_floor_sits_below_the_reference_cohort_minimum():
    """A 코호트 최소 1,289자 · 기대 범위 하단 1,200자 — 둘 사이를 벗어나면 근거가 깨진다."""
    assert ai.REVIEW_MIN_BODY_CHARS == 1200
    assert ai.REVIEW_MIN_BODY_CHARS <= 1289, (
        "하한이 레퍼런스 A 코호트 최소값보다 높다 — 레퍼런스 글조차 못 통과한다"
    )


# --------------------------------------------------------------------------- #
# 3. 되먹임 — 짧으면 한 번 더 쓰게 한다. 딱 한 번.
# --------------------------------------------------------------------------- #
def _fake_runner(monkeypatch, answers):
    """``_run_claude`` 를 대본으로 바꾸고 받은 프롬프트를 기록한다."""
    seen = []

    def fake(prompt, timeout=None, allow_web=False):
        seen.append(prompt)
        return answers[min(len(seen) - 1, len(answers) - 1)]

    monkeypatch.setattr(ai, "_run_claude", fake)
    return seen


def _answer(n_chars):
    import json
    return json.dumps({
        "title": "문래 저녁 고기집, 웨이팅 후 먹은 갈매기살",
        "blocks": [{"type": "para", "text": "가" * n_chars}],
        "hashtags": ["#문래맛집"],
    }, ensure_ascii=False)


def _write(**kw):
    kw.setdefault("topic", "문래갈매기")
    kw.setdefault("location", "문래동")
    kw.setdefault("prose", "고기가 맛있었어요.")
    kw.setdefault("overall_score", 8)
    kw.setdefault("photos", [])
    return ai.write_review(**kw)


def test_a_long_enough_draft_costs_exactly_one_claude_call(monkeypatch):
    seen = _fake_runner(monkeypatch, [_answer(1500)])
    out = _write()
    assert out and ai.review_body_chars(out) == 1500
    assert len(seen) == 1, "하한을 넘겼는데 괜히 한 번 더 불렀다"


def test_a_short_draft_is_sent_back_to_be_written_longer(monkeypatch):
    seen = _fake_runner(monkeypatch, [_answer(600), _answer(1400)])
    out = _write()
    assert len(seen) == 2, "하한에 못 미쳤는데 되먹임을 안 했다"
    assert ai.review_body_chars(out) == 1400, "더 긴 결과를 안 썼다"
    follow = seen[1]
    assert "600자" in follow and "1200자" in follow.replace(",", "")
    # 되먹임은 '늘려라'가 아니라 '재료 안에서 충실히 써라'다.
    assert "지어내서 늘리지 마라" in follow
    assert "늘리지 말고 그대로 내라" in follow


def test_the_feedback_happens_once_and_never_loops(monkeypatch):
    """두 번째도 짧으면 거기서 멈춘다 — 호출 2회, 그 이상은 없다."""
    seen = _fake_runner(monkeypatch, [_answer(500), _answer(700)])
    out = _write()
    assert len(seen) == 2
    assert ai.review_body_chars(out) == 700


def test_a_feedback_that_came_back_shorter_is_discarded(monkeypatch):
    """재료가 그만큼이었다는 뜻이다 — 더 짧아진 글로 바꾸지 않는다."""
    seen = _fake_runner(monkeypatch, [_answer(900), _answer(300)])
    out = _write()
    assert len(seen) == 2
    assert ai.review_body_chars(out) == 900


def test_a_broken_feedback_answer_keeps_the_first_draft(monkeypatch):
    """되먹임 실패는 '생성 실패'가 아니다 — 첫 초안을 그대로 낸다."""
    seen = _fake_runner(monkeypatch, [_answer(800), "JSON 이 아니다"])
    out = _write()
    assert len(seen) == 2
    assert out and ai.review_body_chars(out) == 800


def test_a_cancel_during_the_feedback_still_cancels(monkeypatch):
    """사람이 멈춘 것은 삼키지 않는다 — 되먹임 안에서도 마찬가지다."""
    import pytest
    calls = []

    def fake(prompt, timeout=None, allow_web=False):
        calls.append(prompt)
        if len(calls) == 1:
            return _answer(400)
        raise ai.Cancelled("사람이 멈춤")

    monkeypatch.setattr(ai, "_run_claude", fake)
    with pytest.raises(ai.Cancelled):
        _write()


# --------------------------------------------------------------------------- #
# 4. 프롬프트 — 상한 문구가 남아 있으면 안 된다
# --------------------------------------------------------------------------- #
def test_the_prompt_asks_for_a_floor_not_a_cap():
    with open("prompts/blog-review.md", encoding="utf-8") as fh:
        src = fh.read()
    assert "글자 수를 고정하지 마라" not in src, (
        "상한(=고정 금지) 문구가 남아 있다 — 모델에게 짧게 써도 된다는 허가다"
    )
    assert "최소\n   1,200자" in src or "최소 1,200자" in src
    assert "짧으면 짧은 대로 둔다" not in src
    # 지어내기 금지는 하한보다 **세다**.
    assert "지어내면 그건 실패다" in src


# --------------------------------------------------------------------------- #
# 5. 블록 개수 — 긴 글의 끝을 조용히 자르지 않는다
# --------------------------------------------------------------------------- #
def test_the_block_cap_sits_well_above_the_longest_reference_post():
    """레퍼런스 최대 글 = 단락 41 + 소제목 5 + 이미지 24 = 70블록(익선동 청수당).

    옛 값 60 은 **그보다 낮아서** 사진 많은 긴 글의 끝을 말없이 잘랐다.
    """
    assert ai.MAX_REVIEW_BLOCKS >= 70 * 2


def test_a_long_photo_heavy_post_is_not_truncated():
    photos = [{"index": i, "caption": f"사진 {i}"} for i in range(24)]
    blocks = []
    for i in range(24):
        blocks.append({"type": "para", "text": "본문 단락이야. " * 5})
        blocks.append({"type": "para", "text": "두 번째 단락이야. " * 5})
        blocks.append({"type": "image", "photo_index": i})
    data = {"title": "익선동 청수당 카페 후기", "blocks": blocks, "hashtags": []}
    out = ai._normalize_review(data, photos, 8)
    # ratings 블록 하나가 끝에 붙는다.
    assert len(out["blocks"]) == len(blocks) + 1, "긴 글의 끝이 잘렸다"
    used = {b["photo_index"] for b in out["blocks"] if b["type"] == "image"}
    assert used == set(range(24)), "사진이 잘려 나갔다"
