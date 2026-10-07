"""썸네일(노선 2 Step 4) — 값 보존 · 넘침 검증 · 생성 경로 격리 회귀 테스트.

여기서 지키려는 계약:
  * **디자인 수치는 gf-blog `썸네일/template.css` 그대로다.** `design/thumbnail-template.css`
    에서 파싱해 쓰고, 이 테스트가 그 수치를 하나하나 못박는다 — 누가 "조금만 키우자"로
    바꾸면 여기서 깨진다.
  * **넘침은 재서 판정한다.** 클라이언트가 보낸 ``ok`` 주장을 믿지 않고 서버가
    ``scrollWidth <= clientWidth``를 다시 계산한다(gf-blog render.py 의 assert 와 같은 식).
  * **넘치면 폰트를 줄이지 않는다 — 문구를 줄인다.** 스키마에도 코드에도 글자 크기를
    바꾸는 길이 없다는 것을 검증한다.
  * **썸네일은 초안 생성 경로에 끼지 않는다.** 이미 느린(Render 0.1 CPU · ~170초) 생성에
    claude 호출을 하나 더 얹지 않는다 — 사용자가 요청할 때만 돈다.
  * 네트워크·`claude` 를 타지 않는다(`_run_claude` 를 가짜로 끼운다).
"""
import json
import os
import re

import pytest

import ai
import app as app_module
import thumbnail
from models import BlogReview, Couple, Photo, User, db

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_READY_AI = {
    "title": "문래 저녁 고기집, 웨이팅 후 먹은 문래갈매기 갈매기살",
    "blocks": [
        {"type": "para", "text": "주말 저녁에 문래에 다녀왔어요."},
        {"type": "heading", "text": "웨이팅은 15분 정도"},
        {"type": "para", "text": "앞에 3팀이 있었어요."},
    ],
    "hashtags": ["#문래갈매기"],
}

_COPY_OK = {
    "candidates": [
        {"main_line1": "15분 기다린", "main_line2": "문래 갈매기살",
         "sub": "숯불에 구운 갈매기살", "badge": "주말 저녁 웨이팅", "reason": "근거"},
        {"main_line1": "회식하기 좋은", "main_line2": "문래 고기집",
         "sub": "좌식 자리와 넓은 테이블", "badge": "4인 이상", "reason": "근거2"},
        {"main_line1": "숯불향 진한", "main_line2": "문래갈매기",
         "sub": "된장찌개가 기본", "badge": "15분 대기", "reason": "근거3"},
    ],
    "picked": 0,
    "pick_reason": "짧고 지역·메뉴가 보인다",
}


@pytest.fixture()
def no_thumb_spawn(monkeypatch):
    """썸네일 백그라운드 스폰 차단 — claude 가 테스트에서 절대 돌지 않게."""
    calls = []
    monkeypatch.setattr(
        app_module, "_spawn_generate_thumbnail",
        lambda *a, **k: calls.append(a[-1] if a else None),
    )
    return calls


def _photo(couple_user, name="IMG_1.jpg"):
    p = Photo(
        couple_id=couple_user.couple_id,
        onedrive_item_id="item-" + name,
        filename=name,
        original_name=name,
        uploaded_by=couple_user.id,
        caption_status="pending",
    )
    db.session.add(p)
    db.session.commit()
    return p


# --------------------------------------------------------------------------- #
# 1. 값 보존 — gf-blog template.css 수치가 그대로 살아 있는가
# --------------------------------------------------------------------------- #
def test_template_file_is_present_and_utf8():
    path = os.path.join(_ROOT, "design", "thumbnail-template.css")
    with open(path, encoding="utf-8") as f:
        css = f.read()
    assert "1080px" in css and "1350px" in css
    assert "\r" not in css                      # LF (POLICY-ENCODING)


def test_spec_preserves_every_template_number():
    """Figma 내보내기의 좌표·크기·폰트·색을 **하나도 바꾸지 않는다.**"""
    s = thumbnail.spec()
    assert s is not None
    assert s["canvas"] == {"width": 1080, "height": 1350}
    assert s["main"] == {
        "left": 90, "top": 935, "width": 900, "height": 300,
        "font_family": "Pretendard", "font_weight": 700,
        "font_size": 95, "line_height": 113, "color": "#FFFFFF",
    }
    assert s["sub"] == {
        "left": 90, "top": 1181, "width": 578, "height": 54,
        "font_family": "Pretendard", "font_weight": 500,
        "font_size": 45, "line_height": 54, "color": "#FFFFFF",
    }
    assert s["badge"]["left"] == 90 and s["badge"]["top"] == 846
    assert s["badge"]["width"] == 307 and s["badge"]["height"] == 70
    assert s["badge"]["background"] == "#D7FFFC"
    assert s["badge"]["radius"] == 999.0
    assert s["badge"]["padding"] == 10
    assert s["badge_label"]["width"] == 270 and s["badge_label"]["height"] == 43
    assert s["badge_label"]["font_weight"] == 600
    assert s["badge_label"]["font_size"] == 36
    assert s["badge_label"]["line_height"] == 43
    assert s["badge_label"]["color"] == "#000000"


def test_gradient_keeps_its_exact_colour_and_stop():
    g = thumbnail.spec()["gradient"]
    assert g["angle"] == 180.0
    assert g["stops"][0]["rgba"] == [217.0, 217.0, 217.0, 0.0]
    assert g["stops"][0]["at"] == 0.0
    # 81.73% 는 Figma 가 내보낸 값이다 — 반올림하지 않는다.
    assert g["stops"][1]["rgba"] == [17.0, 17.0, 17.0, 0.7]
    assert g["stops"][1]["at"] == pytest.approx(0.8173)


def test_raw_declarations_travel_to_the_browser_verbatim():
    """DOM 미리보기는 파싱한 숫자가 아니라 **원래 선언 문자열**을 그대로 붙인다."""
    css = thumbnail.spec()["css"]
    assert "font-size: 95px" in css["main"] and "line-height: 113px" in css["main"]
    assert "top: 1181px" in css["sub"]
    assert "border-radius: 999px" in css["badge"]
    assert "rgba(17, 17, 17, 0.7) 81.73%" in css["gradient"]
    # Figma 체커보드 플레이스홀더는 디자인이 아니라서 걷어낸다.
    assert "Checker.png" not in css["canvas"]


def test_broken_template_turns_the_feature_off_instead_of_crashing(monkeypatch):
    with pytest.raises(thumbnail.TemplateError):
        thumbnail.split_blocks("position: absolute; width: 10px;")
    monkeypatch.setattr(thumbnail, "TEMPLATE_PATH", os.path.join(_ROOT, "nope.css"))
    monkeypatch.setattr(thumbnail, "_SPEC_CACHE", {})
    assert thumbnail.spec() is None          # 앱은 죽지 않고 기능만 꺼진다


def test_budget_is_derived_from_the_boxes_not_hardcoded():
    s = thumbnail.spec()
    assert s["budget"]["main_line"] == s["main"]["width"] // s["main"]["font_size"]
    assert s["budget"]["sub"] == s["sub"]["width"] // s["sub"]["font_size"]
    assert s["budget"]["main_lines"] == 2     # 300 / 113


# --------------------------------------------------------------------------- #
# 2. 카피 정규화 (순수 함수)
# --------------------------------------------------------------------------- #
def test_normalize_copy_keeps_three_and_mirrors_the_pick():
    out = thumbnail.normalize_copy(_COPY_OK)
    assert len(out["candidates"]) == 3
    assert out["picked"] == 0
    assert out["copy"] == {f: _COPY_OK["candidates"][0][f]
                           for f in thumbnail.COPY_FIELDS}


def test_normalize_copy_caps_at_three_and_drops_half_written_candidates():
    data = {
        "candidates": [
            {"main_line1": "a", "main_line2": "b"},
            {"main_line1": "", "main_line2": "b"},        # 메인 1줄 없음 → 탈락
            {"main_line1": "c", "main_line2": ""},        # 메인 2줄 없음 → 탈락
            {"main_line1": "d", "main_line2": "e"},
            {"main_line1": "f", "main_line2": "g"},
            {"main_line1": "h", "main_line2": "i"},       # 4번째 생존자 → 잘린다
        ],
        "picked": 99,
    }
    out = thumbnail.normalize_copy(data)
    assert len(out["candidates"]) == 3
    assert out["picked"] == 0                 # 범위 밖은 0으로 되돌린다


def test_normalize_copy_flattens_newlines_and_caps_length():
    out = thumbnail.normalize_copy({
        "candidates": [{"main_line1": " 두 줄로\n  쓴  말 ", "main_line2": "가" * 80}],
    })
    c = out["candidates"][0]
    assert c["main_line1"] == "두 줄로 쓴 말"
    assert len(c["main_line2"]) == thumbnail.MAX_COPY_CHARS


def test_normalize_copy_refuses_design_fields():
    """모델이 '폰트를 줄여서 맞췄다'를 할 자리가 **스키마에 없다.**"""
    out = thumbnail.normalize_copy({
        "candidates": [{"main_line1": "a", "main_line2": "b",
                        "font_size": 60, "main_font_size": 48, "width": 1200}],
        "font_size": 42,
    })
    blob = json.dumps(out, ensure_ascii=False)
    assert "font_size" not in blob and "font-size" not in blob
    assert "1200" not in blob


def test_normalize_copy_rejects_junk():
    assert thumbnail.normalize_copy(None) is None
    assert thumbnail.normalize_copy({"candidates": []}) is None
    assert thumbnail.normalize_copy({"candidates": [{"sub": "서브만"}]}) is None


def test_apply_pick_resets_the_copy_when_the_candidate_changes():
    state = thumbnail.normalize_copy(_COPY_OK)
    state = thumbnail.apply_pick(state, picked=1)
    assert state["picked"] == 1
    assert state["copy"]["main_line1"] == "회식하기 좋은"


def test_editing_the_copy_invalidates_the_previous_render_check():
    state = thumbnail.normalize_copy(_COPY_OK)
    state["render_check"] = {"ok": True}
    state = thumbnail.apply_pick(state, copy={"sub": "더 짧게"})
    assert state["copy"]["sub"] == "더 짧게"
    # 문구가 바뀌면 직전 검증은 이 문구의 것이 아니다 — 다시 재야 한다.
    assert "render_check" not in state


def test_main_lines_cannot_be_emptied():
    state = thumbnail.normalize_copy(_COPY_OK)
    state = thumbnail.apply_pick(state, copy={"main_line1": "   "})
    assert state["copy"]["main_line1"] == "15분 기다린"


# --------------------------------------------------------------------------- #
# 3. 넘침 검증 — gf-blog 의 assert 와 같은 식, 단 **서버가 다시 판정**한다
# --------------------------------------------------------------------------- #
def _measure(main=(900, 900), sub=(400, 578), label=(240, 270), fonts_ok=True):
    def box(sw, cw):
        return {"scrollWidth": sw, "clientWidth": cw, "x": 90, "y": 935,
                "width": cw, "height": 300, "font": "700 95px Pretendard"}
    return {
        "fonts_ok": fonts_ok,
        "boxes": {"main": box(*main), "sub": box(*sub), "badge_label": box(*label)},
    }


def test_render_check_passes_when_nothing_overflows():
    out = thumbnail.evaluate_render_check(_measure())
    assert out["ok"] is True
    assert out["overflowing"] == []


def test_render_check_catches_overflow_per_box():
    out = thumbnail.evaluate_render_check(_measure(main=(1012, 900)))
    assert out["ok"] is False
    assert out["overflowing"] == ["main"]
    out = thumbnail.evaluate_render_check(_measure(label=(300, 270)))
    assert out["overflowing"] == ["badge_label"]


def test_render_check_ignores_the_clients_own_verdict():
    """클라이언트가 'ok' 라고 주장해도 **측정값이 넘치면 넘친 것**이다."""
    raw = _measure(sub=(700, 578))
    raw["ok"] = True
    raw["overflowing"] = []
    out = thumbnail.evaluate_render_check(raw)
    assert out["ok"] is False and out["overflowing"] == ["sub"]


def test_render_check_fails_when_the_font_did_not_load():
    out = thumbnail.evaluate_render_check(_measure(fonts_ok=False))
    assert out["ok"] is False and out["fonts_ok"] is False
    assert out["overflowing"] == []     # 넘치진 않았다 — 폰트가 문제다


def test_render_check_refuses_a_partial_measurement():
    raw = _measure()
    del raw["boxes"]["sub"]             # 재야 할 상자를 안 쟀다 = 검증이 아니다
    assert thumbnail.evaluate_render_check(raw) is None
    assert thumbnail.evaluate_render_check({"boxes": {}}) is None
    assert thumbnail.evaluate_render_check("nope") is None


def test_render_check_tolerates_subpixel_rounding():
    assert thumbnail.evaluate_render_check(_measure(main=(900.3, 900)))["ok"] is True
    assert thumbnail.evaluate_render_check(_measure(main=(901, 900)))["ok"] is False


def test_measured_boxes_are_the_three_gf_blog_asserts():
    assert set(thumbnail.MEASURED_BOXES) == {"main", "sub", "badge_label"}


# --------------------------------------------------------------------------- #
# 4. 프롬프트 · claude 래퍼
# --------------------------------------------------------------------------- #
def test_prompt_encodes_the_gf_blog_copy_discipline():
    p = ai.load_prompt("thumbnail-copy")
    assert p
    assert "~~한 000" in p and "~~할 때 가기 좋은 000" in p   # 형식 2번
    assert "후보 3개" in p                                     # 4번
    assert "역대급" in p and "무조건" in p                      # 5번 과장 금지
    assert "만들어내지 마라" in p                               # 1번 날조 금지
    # 8번 — 넘치면 글자 크기가 아니라 문구를 줄인다
    assert "문구를 줄인다" in p
    assert "{{MAIN_BUDGET}}" in p and "{{BADGE_BUDGET}}" in p


def test_suggest_thumbnail_copy_fills_every_slot(monkeypatch):
    seen = {}

    def fake(prompt, **kw):
        seen["prompt"] = prompt
        return json.dumps(_COPY_OK, ensure_ascii=False)

    monkeypatch.setattr(ai, "_run_claude", fake)
    out = ai.suggest_thumbnail_copy(
        "주제 / 장소: 문래갈매기", "문래 저녁 고기집", "본문 전체",
        research=None, budget=thumbnail.spec()["budget"],
    )
    assert out["candidates"][0]["main_line1"] == "15분 기다린"
    p = seen["prompt"]
    assert "{{" not in p                       # 치환 자리가 남으면 안 된다
    assert "문래 저녁 고기집" in p and "본문 전체" in p
    assert "한글 9자 안쪽" in p                 # 900 / 95
    assert "조사를 하지 않았다" in p            # research 없음 → NO_KEYWORD_BLOCK


def test_suggest_thumbnail_copy_degrades_to_none(monkeypatch):
    monkeypatch.setattr(ai, "_run_claude",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ai.suggest_thumbnail_copy("재료", "제목", "본문") is None
    monkeypatch.setattr(ai, "_run_claude", lambda *a, **k: "설명만 하고 JSON 없음")
    assert ai.suggest_thumbnail_copy("재료", "제목", "본문") is None


# --------------------------------------------------------------------------- #
# 5. 생성 경로 격리 — 썸네일이 초안 생성 시간을 늘리지 않는다
# --------------------------------------------------------------------------- #
def test_draft_generation_never_calls_the_thumbnail(flask_app, make_review,
                                                    monkeypatch):
    """초안 생성(이미 ~170초)에 claude 호출을 **하나도 더 얹지 않는다.**"""
    called = []
    monkeypatch.setattr(ai, "suggest_thumbnail_copy",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr(ai, "write_review", lambda *a, **k: dict(_READY_AI))
    monkeypatch.setattr(app_module.keyword_research, "research",
                        lambda *a, **k: {"status": "skipped"})
    review = make_review(status="pending")
    rid = review.id
    app_module.generate_review(flask_app, rid)
    db.session.expire_all()          # 워커는 자기 app context·세션에서 커밋한다
    assert called == []
    assert db.session.get(BlogReview, rid).status == "ready"
    assert db.session.get(BlogReview, rid).thumbnail_json is None


def test_thumbnail_worker_stores_candidates(flask_app, make_review, monkeypatch):
    monkeypatch.setattr(ai, "suggest_thumbnail_copy",
                        lambda *a, **k: thumbnail.normalize_copy(_COPY_OK))
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready")
    rid = review.id
    app_module.generate_thumbnail_copy(flask_app, rid)
    db.session.expire_all()
    state = db.session.get(BlogReview, rid).thumbnail
    assert state["status"] == "ready"
    assert len(state["candidates"]) == 3
    assert state["photo_index"] == 0


def test_thumbnail_worker_keeps_the_previous_copy_when_it_fails(
        flask_app, make_review, monkeypatch):
    prior = thumbnail.normalize_copy(_COPY_OK)
    prior["status"] = "ready"
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready",
                         thumbnail_json=json.dumps(prior, ensure_ascii=False))
    rid = review.id
    monkeypatch.setattr(ai, "suggest_thumbnail_copy", lambda *a, **k: None)
    app_module.generate_thumbnail_copy(flask_app, rid)
    db.session.expire_all()
    state = db.session.get(BlogReview, rid).thumbnail
    assert state["status"] == "ready"
    assert state["candidates"][0]["main_line1"] == "15분 기다린"
    assert state["last_error"]


def test_thumbnail_worker_marks_failed_without_a_prior(flask_app, make_review,
                                                       monkeypatch):
    monkeypatch.setattr(ai, "suggest_thumbnail_copy", lambda *a, **k: None)
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready")
    rid = review.id
    app_module.generate_thumbnail_copy(flask_app, rid)
    db.session.expire_all()
    assert db.session.get(BlogReview, rid).thumbnail["status"] == "failed"


# --------------------------------------------------------------------------- #
# 6. 라우트
# --------------------------------------------------------------------------- #
def test_generate_needs_a_ready_draft(client, flask_app, make_review,
                                      no_thumb_spawn):
    review = make_review(status="pending")
    r = client.post(f"/reviews/{review.id}/thumbnail", follow_redirects=True)
    assert r.status_code == 200
    assert db.session.get(BlogReview, review.id).thumbnail_json is None
    assert no_thumb_spawn == []


def test_generate_spawns_in_the_background_not_in_the_request(
        client, flask_app, make_review, no_thumb_spawn, monkeypatch):
    monkeypatch.setattr(
        ai, "_run_claude",
        lambda *a, **k: pytest.fail("claude must never run in the request path"),
    )
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready")
    r = client.post(f"/reviews/{review.id}/thumbnail", follow_redirects=True)
    assert r.status_code == 200
    assert db.session.get(BlogReview, review.id).thumbnail["status"] == "pending"
    assert no_thumb_spawn == [review.id]


def test_state_endpoint_needs_candidates(client, flask_app, make_review):
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready")
    r = client.post(f"/reviews/{review.id}/thumbnail/state", json={"picked": 0})
    assert r.status_code == 409


def _ready_review(make_review):
    state = thumbnail.normalize_copy(_COPY_OK)
    state["status"] = "ready"
    return make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                       status="ready",
                       thumbnail_json=json.dumps(state, ensure_ascii=False))


def test_state_saves_the_pick_the_copy_and_the_verdict(client, flask_app,
                                                       make_review):
    review = _ready_review(make_review)
    r = client.post(
        f"/reviews/{review.id}/thumbnail/state",
        json={"picked": 2, "copy": {"sub": "짧게"}, "photo_index": 1,
              "render_check": _measure()},
    )
    assert r.status_code == 200 and r.get_json()["render_ok"] is True
    state = db.session.get(BlogReview, review.id).thumbnail
    assert state["picked"] == 2
    assert state["copy"]["main_line1"] == "숯불향 진한"   # 후보 교체가 먼저 반영
    assert state["copy"]["sub"] == "짧게"                 # 그 위에 사람 편집
    assert state["photo_index"] == 1
    assert state["render_check"]["ok"] is True
    assert state["render_check"]["checked_at"]


def test_state_records_overflow_and_reports_it(client, flask_app, make_review):
    review = _ready_review(make_review)
    r = client.post(f"/reviews/{review.id}/thumbnail/state",
                    json={"render_check": _measure(main=(1100, 900))})
    body = r.get_json()
    assert body["render_ok"] is False and body["overflowing"] == ["main"]
    rc = db.session.get(BlogReview, review.id).thumbnail["render_check"]
    assert rc["boxes"]["main"]["overflow"] is True
    assert rc["boxes"]["main"]["scrollWidth"] == 1100


def test_state_rejects_an_unverifiable_measurement(client, flask_app,
                                                   make_review):
    review = _ready_review(make_review)
    r = client.post(f"/reviews/{review.id}/thumbnail/state",
                    json={"render_check": {"fonts_ok": True, "boxes": {}}})
    assert r.status_code == 400


def test_thumbnail_routes_are_couple_scoped(client, flask_app, couple_user):
    other = Couple(invite_code="OTHERXX")
    db.session.add(other)
    db.session.flush()
    stranger = User(email="b@example.com", password_hash="x", display_name="남",
                    couple_id=other.id, status="approved")
    db.session.add(stranger)
    db.session.flush()
    review = BlogReview(couple_id=other.id, created_by=stranger.id, topic="남의 후기",
                        prose="x", overall_score=8, status="ready",
                        ai_json=json.dumps(_READY_AI, ensure_ascii=False))
    db.session.add(review)
    db.session.commit()
    assert client.post(f"/reviews/{review.id}/thumbnail").status_code == 404
    assert client.post(f"/reviews/{review.id}/thumbnail/state",
                       json={"picked": 0}).status_code == 404


# --------------------------------------------------------------------------- #
# 7. 화면
# --------------------------------------------------------------------------- #
def test_detail_offers_to_make_a_thumbnail(client, flask_app, make_review,
                                           couple_user):
    p = _photo(couple_user)
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready",
                         photo_ids=json.dumps([p.id]))
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "썸네일 문구 만들기" in html
    assert "1080×1350" in html


def test_detail_renders_the_preview_and_the_measuring_boxes(
        client, flask_app, make_review, couple_user):
    p = _photo(couple_user)
    state = thumbnail.normalize_copy(_COPY_OK)
    state["status"] = "ready"
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready", photo_ids=json.dumps([p.id]),
                         thumbnail_json=json.dumps(state, ensure_ascii=False))
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "rv-thumb-cover" in html and "rv-thumb-main" in html
    assert "15분 기다린" in html and "회식하기 좋은" in html
    assert "static/thumbnail.js" in html
    assert "Pretendard" in html               # 폰트 로딩 링크
    assert "PNG 내려받기" in html
    # 편집칸이 실제로 **채워져** 있어야 한다. (`thumb.copy` 로 쓰면 Jinja 가 dict 의
    # copy() 메서드를 집어 전부 빈 칸으로 렌더된다 — 실측된 버그의 회귀 가드.)
    rendered = dict(re.findall(
        r'data-field="(\w+)"[^>]*?value="([^"]*)"', html, re.S
    ))
    assert rendered == thumbnail.normalize_copy(_COPY_OK)["copy"]
    # 스펙이 통째로 브라우저에 간다(단일 원천).
    assert '"line_height": 113' in html or '"line_height":113' in html


def test_the_first_attached_photo_is_the_default_background(
        client, flask_app, make_review, couple_user):
    """배경은 **후기에 올린 첫 사진**이다(새 업로드 경로를 만들지 않는다)."""
    first, second = _photo(couple_user, "a.jpg"), _photo(couple_user, "b.jpg")
    state = thumbnail.normalize_copy(_COPY_OK)
    state["status"] = "ready"
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready",
                         photo_ids=json.dumps([first.id, second.id]),
                         thumbnail_json=json.dumps(state, ensure_ascii=False))
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    # 셀렉트의 0번(첫 사진)이 선택돼 있고, 앱이 이미 쓰는 서명 URL 을 그대로 쓴다.
    assert 'value="0"' in html and "selected" in html
    assert f"/blog-img/{first.id}?" in html
    # 1080×1350 캔버스에 쓰려면 원본 해상도여야 한다. 예전엔 `hq=1`(서버가 원본을
    # 디코드·재인코딩)이었고, 지금은 `v=orig`(서버가 **열지 않고 중계**)다 —
    # 해상도는 같고 재인코딩 한 세대가 빠졌다.
    assert "v=orig" in html


def test_a_photoless_review_falls_back_to_a_flat_background(
        client, flask_app, make_review):
    """사진이 없어도 썸네일은 나온다 — 단, **AI 로 그림을 지어내지는 않는다.**"""
    state = thumbnail.normalize_copy(_COPY_OK)
    state["status"] = "ready"
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready",
                         thumbnail_json=json.dumps(state, ensure_ascii=False))
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "rv-thumb-cover" in html                 # 카드는 그대로 뜬다
    assert "rv-thumb-photo-select" not in html      # 고를 사진이 없다
    assert "단색 배경으로 그려" in html
    assert "지어내지는 않아" in html


def test_renderer_uses_the_photo_as_the_background():
    """합성 경로가 실제로 **사진을 깔고** 그 위에 타이포를 올리는가."""
    js = _renderer()
    assert "drawCover" in js and "object-fit:cover" in js
    assert "fallbackBg" in js                       # 사진 없을 때만 단색
    # 가독성 처리 = template.css 의 어두운 오버레이 그라데이션(사진 위에 깔린다).
    assert "createLinearGradient" in js


def test_detail_polls_while_the_copy_is_being_written(client, flask_app,
                                                      make_review, couple_user):
    p = _photo(couple_user)
    review = make_review(ai_json=json.dumps(_READY_AI, ensure_ascii=False),
                         status="ready", photo_ids=json.dumps([p.id]),
                         thumbnail_json=json.dumps({"status": "pending"}))
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert 'http-equiv="refresh"' in html
    assert "썸네일 문구를 만드는 중" in html


# --------------------------------------------------------------------------- #
# 8. 브라우저 렌더러 — 규율이 코드에 박혀 있는가
# --------------------------------------------------------------------------- #
def _renderer():
    with open(os.path.join(_ROOT, "static", "thumbnail.js"), encoding="utf-8") as f:
        return f.read()


def test_renderer_keeps_the_gf_blog_assertions():
    js = _renderer()
    assert "scrollWidth" in js and "clientWidth" in js   # 가로 넘침 assert
    assert "document.fonts.check" in js                  # 폰트 로딩 확인
    assert "roundRect" in js                             # 배지 — 값대로 그린다


def test_renderer_never_shrinks_the_font_to_fit():
    """폰트를 줄여 우겨넣는 길이 **코드에 없어야** 한다 (gf-blog 8번)."""
    js = _renderer()
    # 스펙의 글자 크기에 **대입하는** 코드가 없어야 한다(읽어 쓰는 건 당연히 있다).
    for banned in ("fontSize =", "font_size =", "font_size:", "scaleFont",
                   "shrinkTo", "autoFit", "fitText"):
        assert banned not in js, f"폰트를 줄이는 코드가 생겼다: {banned}"
    assert "문구를 줄이거나" in js         # 사람에게 시키는 안내는 있어야 한다


def test_renderer_blocks_the_download_while_it_overflows():
    js = _renderer()
    assert "el.download.disabled = !v.ok" in js
    assert "if (!v.ok) return;" in js
