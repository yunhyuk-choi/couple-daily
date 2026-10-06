"""키워드 조사(노선 2 Step 3) — 파싱 · 선정 · 프롬프트 · 키 취급 회귀 테스트.

여기서 지키려는 계약:
  * **네트워크를 절대 타지 않는다.** 실제 응답은 `tests/fixtures/`에 저장해 두고
    파싱·선정 로직만 검증한다(픽스처에 자격증명이 없다는 것도 테스트한다).
  * 지표 오독 금지가 코드·프롬프트 양쪽에 박혀 있다 — 검색 결과 수 ≠ 검색량,
    상대지수 ≠ 절대 검색 횟수, **빈 트렌드 = 정량 확인 실패이지 수요 없음이 아니다.**
  * **키가 없으면 죽지 않는다** — 조사를 건너뛰고 지금까지와 똑같이 생성한다.
  * 자격증명은 화면·조사 메모·프롬프트·에러 메시지 어디에도 새지 않는다.
"""
import json
import os

import pytest

import ai
import app as app_module
import keyword_research
import naver_api
from models import db

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# 테스트 전용 더미 자격증명 — 실제 키가 아니다. '새면 안 되는 문자열'의 표식으로 쓴다.
FAKE_ID = "TEST-CLIENT-ID-DO-NOT-LEAK"
FAKE_SECRET = "TEST-CLIENT-SECRET-DO-NOT-LEAK"


def _fixture(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 0. 픽스처 자체 — 키가 들어 있으면 안 된다
# --------------------------------------------------------------------------- #
def test_fixtures_carry_no_credentials():
    for name in os.listdir(_FIX):
        with open(os.path.join(_FIX, name), encoding="utf-8") as f:
            raw = f.read()
        assert "X-NCP-APIGW" not in raw, f"{name}에 요청 헤더가 섞여 있다"
        assert "api-key" not in raw.lower(), f"{name}에 키처럼 보이는 것이 있다"


# --------------------------------------------------------------------------- #
# 1. 응답 파싱 (순수 함수 — 저장한 실제 응답으로)
# --------------------------------------------------------------------------- #
def test_parse_blog_search_strips_markup_and_dates():
    parsed = naver_api.parse_blog_search(_fixture("naver_blog_search.json"))
    assert parsed["total"] > 0
    assert parsed["items"]
    for it in parsed["items"]:
        assert "<b>" not in it["title"] and "<b>" not in it["description"]
        assert "&amp;" not in it["title"]
        # postdate는 YYYY-MM-DD로 정규화된다(없으면 "").
        assert it["postdate"] == "" or (
            len(it["postdate"]) == 10 and it["postdate"][4] == "-"
        )


def test_parse_trend_longtail_empty_is_not_zero_demand():
    """롱테일 그룹의 data가 빈 배열이면 0이 아니라 **None**(= 정량 확인 실패)."""
    parsed = naver_api.parse_trend(_fixture("naver_trend_52w.json"))
    wide = parsed["문래 갈매기살"]
    longtail = parsed["문래 갈매기살 웨이팅"]
    assert wide["points"] > 0 and wide["peak"] == 100.0
    assert longtail["points"] == 0
    # 0으로 떨어뜨리면 '수요 없음'으로 읽힌다 — None이어야 한다.
    assert longtail["last"] is None and longtail["avg"] is None


def test_trend_phrase_says_measurement_failed_not_no_demand():
    phrase = keyword_research._trend_phrase(
        {"points": 0, "last": None, "peak": None, "avg": None}
    )
    assert "정량 수요 확인 실패" in phrase
    assert "수요 없음이 아님" in phrase


def test_trend_windows_are_52w_and_8w():
    import datetime

    w = naver_api.trend_windows(datetime.date(2026, 10, 6))
    assert set(w) == {"52w", "8w"}
    assert (w["52w"][1] - w["52w"][0]).days == 7 * 52 - 1
    assert (w["8w"][1] - w["8w"][0]).days == 7 * 8 - 1


# --------------------------------------------------------------------------- #
# 2. 증거 수집 · 증거 블록
# --------------------------------------------------------------------------- #
CANDIDATES = [
    {"keyword": "문래 갈매기살", "variants": ["문래 갈매기살", "문래갈매기"],
     "question": "문래에서 갈매기살 먹을 곳", "answerable": "시작 검색어"},
    {"keyword": "문래 갈매기살 웨이팅", "variants": ["문래 갈매기살 웨이팅"],
     "question": "주말 저녁에 얼마나 기다리나", "answerable": "앞에 3팀, 15분 대기 기록"},
]


@pytest.fixture()
def fake_naver(monkeypatch):
    """네이버 호출을 픽스처로 대체하고, 넘어온 자격증명을 기록한다."""
    seen = {"ids": set(), "secrets": set(), "queries": [], "windows": []}

    def fake_search_blog(cid, secret, query, display=20, sort="sim", timeout=None):
        seen["ids"].add(cid)
        seen["secrets"].add(secret)
        seen["queries"].append((query, sort))
        return naver_api.parse_blog_search(_fixture("naver_blog_search.json"))

    def fake_search_trend(cid, secret, groups, start, end, time_unit="week",
                          timeout=None):
        seen["ids"].add(cid)
        seen["secrets"].add(secret)
        seen["windows"].append((str(start), str(end)))
        name = ("naver_trend_52w.json" if len(seen["windows"]) == 1
                else "naver_trend_8w.json")
        return naver_api.parse_trend(_fixture(name))

    monkeypatch.setattr(naver_api, "search_blog", fake_search_blog)
    monkeypatch.setattr(naver_api, "search_trend", fake_search_trend)
    return seen


def test_collect_evidence_queries_both_sorts_and_two_windows(fake_naver):
    evidence, errors = keyword_research.collect_evidence(
        CANDIDATES, FAKE_ID, FAKE_SECRET
    )
    assert not errors
    # 후보마다 sim(연관순)·date(최신순) 두 번씩.
    assert sorted(fake_naver["queries"]) == sorted(
        [(c["keyword"], s) for c in CANDIDATES for s in ("sim", "date")]
    )
    # 트렌드는 구간당 1회(그룹을 한꺼번에 보낸다) — 52주 · 8주.
    assert len(fake_naver["windows"]) == 2
    assert set(evidence) == {c["keyword"] for c in CANDIDATES}
    assert evidence["문래 갈매기살 웨이팅"]["trend"]["52w"]["points"] == 0


def test_evidence_block_carries_the_misreading_bans(fake_naver):
    evidence, _ = keyword_research.collect_evidence(CANDIDATES, FAKE_ID, FAKE_SECRET)
    block = keyword_research.format_evidence_block(CANDIDATES, evidence)
    assert "검색량이 아니다" in block
    assert "검색 횟수가 아니다" in block
    assert "정량 확인 실패이지 수요 없음이 아니다" in block
    # 자격증명이 증거 블록에 섞여선 안 된다.
    assert FAKE_ID not in block and FAKE_SECRET not in block


# --------------------------------------------------------------------------- #
# 3. 후보 · 선정 정규화 (순수)
# --------------------------------------------------------------------------- #
def test_normalize_candidates_keeps_variants_and_caps_at_five():
    data = {
        "candidates": [
            {"keyword": f"키워드{i}", "variants": [f"키워드{i}", f"키워드{i}변형"],
             "question": "q", "answerable": "a"}
            for i in range(8)
        ]
    }
    out = ai.normalize_candidates(data)
    assert len(out) == 5
    assert out[0]["variants"][0] == "키워드0"


def test_normalize_candidates_injects_keyword_into_variants():
    out = ai.normalize_candidates({"candidates": [{"keyword": "문래 갈매기살"}]})
    assert out[0]["variants"] == ["문래 갈매기살"]


def test_selection_main_must_be_a_researched_candidate():
    """조사하지 않은 검색어를 메인으로 내면 첫 후보로 되돌린다."""
    sel = ai.normalize_selection({"main": "조사 안 한 말"}, CANDIDATES)
    assert sel["main"] == CANDIDATES[0]["keyword"]


def test_selection_caps_supporting_and_drops_main_from_it():
    sel = ai.normalize_selection(
        {
            "main": "문래 갈매기살 웨이팅",
            "supporting": ["문래 갈매기살 웨이팅", "a", "b", "c", "d", "e"],
            "excluded": [{"keyword": "문래 갈매기살", "reason": "넓다"}],
        },
        CANDIDATES,
    )
    assert sel["main"] == "문래 갈매기살 웨이팅"
    assert "문래 갈매기살 웨이팅" not in sel["supporting"]
    assert len(sel["supporting"]) == 4
    assert sel["excluded"][0]["keyword"] == "문래 갈매기살"


def test_selection_drops_fabricated_score_fields():
    """근거 없는 '경쟁 점수'는 스키마에 자리가 없어 통째로 버려진다."""
    sel = ai.normalize_selection(
        {"main": "문래 갈매기살", "competition_score": 7, "difficulty": "하"},
        CANDIDATES,
    )
    assert "competition_score" not in sel and "difficulty" not in sel


# --------------------------------------------------------------------------- #
# 4. 프롬프트 — 조사 결과가 본문 생성에 실린다 / 없으면 '조사 안 했다'
# --------------------------------------------------------------------------- #
def test_keyword_prompts_encode_the_discipline():
    cand = ai.load_prompt("keyword-candidates")
    sel = ai.load_prompt("keyword-select")
    assert cand and sel
    assert "시작 검색어" in cand            # 지역+상호는 시작일 뿐
    assert "검색 결과 수(total)는 검색량이 아니다" in sel
    assert "절대 검색 횟수가 아니다" in sel
    assert "정량 확인 실패" in sel
    assert "상위 노출이 쉽다거나" in sel     # 긴 검색어 = 쉽다 주장 금지


def test_blog_review_prompt_has_keyword_slot_and_does_not_force_longtail_title():
    body = ai.load_prompt("blog-review")
    assert "{{KEYWORD_BLOCK}}" in body
    assert "롱테일 검색어 전체를" in body and "그대로 넣으려고 애쓰지 마라" in body
    assert "반복 횟수를 세지 마라" in body


def _capture(monkeypatch, payload=None):
    """_run_claude를 가로채 프롬프트를 모으고 최소 JSON을 돌려준다."""
    seen = []

    def fake_run(prompt, *a, **k):
        seen.append(prompt)
        return json.dumps(payload or {
            "title": "문래 저녁 고기집, 웨이팅 후 먹은 갈매기살",
            "blocks": [{"type": "para", "text": "본문"}],
            "hashtags": ["#문래갈매기"],
        }, ensure_ascii=False)

    monkeypatch.setattr(ai, "_run_claude", fake_run)
    return seen


def test_write_review_without_research_says_research_was_not_done(monkeypatch):
    seen = _capture(monkeypatch)
    assert ai.write_review("문래갈매기", "", "맛있었다", 8, [])
    assert "키워드 조사를 하지 않았다" in seen[0]
    assert "검색량·경쟁·노출에 대한 언급을 아예 하지 마라" in seen[0]


def test_write_review_with_research_carries_the_main_keyword(monkeypatch):
    seen = _capture(monkeypatch)
    research = {
        "status": "ok",
        "selection": {
            "main": "문래 갈매기살 웨이팅",
            "supporting": ["문래갈매기 가격"],
            "main_reason": "후기에 앞 3팀·15분 기록이 있다",
            "angle": "주말 저녁 실제 대기",
            "limits": "롱테일 트렌드가 비어 정량 수요 미확인",
        },
    }
    assert ai.write_review("문래갈매기", "", "맛있었다", 8, [], research=research)
    assert "문래 갈매기살 웨이팅" in seen[0]
    assert "롱테일 트렌드가 비어 정량 수요 미확인" in seen[0]
    assert "키워드 조사를 하지 않았다" not in seen[0]


def test_format_keyword_block_degrades_for_skipped_and_failed():
    for research in (None, {}, {"status": "skipped"}, {"status": "failed"},
                     {"status": "ok", "selection": {}}):
        assert ai.format_keyword_block(research) == ai.NO_KEYWORD_BLOCK


# --------------------------------------------------------------------------- #
# 5. 키가 없을 때 — 죽지 않고 건너뛴다 (⭐ 이 동작을 못박는다)
# --------------------------------------------------------------------------- #
def test_research_skips_without_credentials_and_never_calls_claude(monkeypatch):
    monkeypatch.setattr(
        ai, "_run_claude",
        lambda *a, **k: pytest.fail("키가 없으면 claude를 부르면 안 된다"),
    )
    out = keyword_research.research("문래갈매기", "", "맛있었다", 8, {})
    assert out == {"status": "skipped", "reason": "no_credentials"}


def test_generation_still_works_when_research_is_skipped(monkeypatch):
    seen = _capture(monkeypatch)
    research = keyword_research.research("문래갈매기", "", "맛있었다", 8, {})
    result = ai.write_review("문래갈매기", "", "맛있었다", 8, [], research=research)
    assert result and result["title"]
    assert "키워드 조사를 하지 않았다" in seen[0]


def test_research_degrades_when_candidate_generation_fails(monkeypatch, fake_naver):
    monkeypatch.setattr(ai, "suggest_keyword_candidates", lambda *a, **k: None)
    out = keyword_research.research(
        "문래갈매기", "", "맛있었다", 8, {},
        client_id=FAKE_ID, client_secret=FAKE_SECRET,
    )
    assert out["status"] == "failed" and out["reason"] == "no_candidates"


# --------------------------------------------------------------------------- #
# 6. 자격증명이 새지 않는다
# --------------------------------------------------------------------------- #
def test_research_output_never_contains_credentials(monkeypatch, fake_naver):
    prompts = []

    def fake_run(prompt, *a, **k):
        prompts.append(prompt)
        if "variants" in prompt:          # 1단계(후보 생성) 프롬프트
            return json.dumps({"candidates": CANDIDATES}, ensure_ascii=False)
        return json.dumps(
            {"main": "문래 갈매기살 웨이팅", "supporting": ["문래갈매기 가격"],
             "main_reason": "r", "limits": "l"},
            ensure_ascii=False,
        )

    monkeypatch.setattr(ai, "_run_claude", fake_run)
    out = keyword_research.research(
        "문래갈매기", "문래동", "맛있었다", 8, {},
        client_id=FAKE_ID, client_secret=FAKE_SECRET, key_owner="테스터",
    )
    assert out["status"] == "ok"
    assert out["selection"]["main"] == "문래 갈매기살 웨이팅"
    blob = json.dumps(out, ensure_ascii=False)
    assert FAKE_ID not in blob and FAKE_SECRET not in blob
    for p in prompts:
        assert FAKE_ID not in p and FAKE_SECRET not in p
    # 조사한 키는 실제로 네이버 호출에 쓰였다(조용히 빈 값으로 돌지 않았다).
    assert fake_naver["ids"] == {FAKE_ID}


def test_api_errors_never_echo_credentials(monkeypatch):
    def boom(*a, **k):
        raise naver_api.NaverApiError("Authentication Failed", status=401)

    monkeypatch.setattr(naver_api, "search_blog", boom)
    ok, msg = naver_api.verify_credentials(FAKE_ID, FAKE_SECRET)
    assert not ok and "인증 실패" in msg
    assert FAKE_ID not in msg and FAKE_SECRET not in msg


def test_blank_credentials_are_refused_before_any_http(monkeypatch):
    # 네트워크 계층을 건드리면 즉시 실패하도록 바꿔 둔다.
    monkeypatch.setattr(naver_api, "requests", pytest.fail)
    with pytest.raises(naver_api.NaverApiError):
        naver_api._request("GET", "/x", "", "", params={})


# --------------------------------------------------------------------------- #
# 7. 설정 화면 — '설정됨/미설정'만 보이고 값은 절대 안 보인다
# --------------------------------------------------------------------------- #
def test_settings_shows_unset_then_set_without_echoing_the_value(client, couple_user):
    html = client.get("/settings").get_data(as_text=True)
    assert "네이버 키워드 조사 키" in html
    assert "미설정" in html

    res = client.post(
        "/settings",
        data={"naver_keys_save": "1", "naver_api_key_id": FAKE_ID,
              "naver_api_key": FAKE_SECRET},
        follow_redirects=True,
    )
    html = res.get_data(as_text=True)
    assert "설정됨" in html
    # ⭐ 값은 어떤 형태로도 다시 렌더되지 않는다.
    assert FAKE_ID not in html and FAKE_SECRET not in html
    assert db.session.get(type(couple_user), couple_user.id).has_naver_api_keys


def test_settings_partial_update_keeps_the_other_half(client, couple_user):
    client.post("/settings", data={"naver_keys_save": "1",
                                   "naver_api_key_id": FAKE_ID,
                                   "naver_api_key": FAKE_SECRET},
                follow_redirects=True)
    client.post("/settings", data={"naver_keys_save": "1",
                                   "naver_api_key_id": "NEW-ID"},
                follow_redirects=True)
    u = db.session.get(type(couple_user), couple_user.id)
    assert u.naver_api_key_id == "NEW-ID"
    assert u.naver_api_key == FAKE_SECRET  # 안 건드린 칸은 그대로


def test_settings_clear_turns_research_off(client, couple_user):
    client.post("/settings", data={"naver_keys_save": "1",
                                   "naver_api_key_id": FAKE_ID,
                                   "naver_api_key": FAKE_SECRET},
                follow_redirects=True)
    html = client.post("/settings", data={"naver_keys_clear": "1"},
                       follow_redirects=True).get_data(as_text=True)
    assert "미설정" in html
    assert not db.session.get(type(couple_user), couple_user.id).has_naver_api_keys


def test_settings_connection_check_uses_the_stored_keys(client, couple_user,
                                                        monkeypatch):
    client.post("/settings", data={"naver_keys_save": "1",
                                   "naver_api_key_id": FAKE_ID,
                                   "naver_api_key": FAKE_SECRET},
                follow_redirects=True)
    seen = {}

    def fake_verify(cid, secret, timeout=None):
        seen["cid"] = cid
        return True, "연결 확인 완료 ✓ 키워드 조사를 쓸 수 있어."

    monkeypatch.setattr(app_module.naver_api, "verify_credentials", fake_verify)
    html = client.post("/settings", data={"naver_keys_test": "1"},
                       follow_redirects=True).get_data(as_text=True)
    assert seen["cid"] == FAKE_ID
    assert "연결 확인 완료" in html
    assert FAKE_ID not in html


# --------------------------------------------------------------------------- #
# 8. 상세 화면 — 조사 메모 / 키 없을 때 안내
# --------------------------------------------------------------------------- #
_READY_AI = {
    "title": "문래 저녁 고기집, 웨이팅 후 먹은 갈매기살",
    "blocks": [{"type": "para", "text": "본문"}],
    "hashtags": ["#문래갈매기"],
}


def test_detail_renders_the_research_memo(client, flask_app, make_review):
    research = {
        "status": "ok",
        "selection": {
            "main": "문래 갈매기살 웨이팅",
            "supporting": ["문래갈매기 가격"],
            "main_reason": "앞 3팀·15분 기록이 있다",
            "excluded": [{"keyword": "문래 갈매기살", "reason": "시작 검색어라 넓다"}],
            "limits": "롱테일 트렌드가 비어 정량 수요 미확인",
            "competition": "같은 질문에 직접 답하는 글은 적다",
        },
        "candidates": [
            {"keyword": "문래 갈매기살 웨이팅", "question": "얼마나 기다리나",
             "answerable": "앞 3팀", "total": 652,
             "trend_52w": "데이터 없음 (정량 수요 확인 실패)", "trend_8w": "데이터 없음",
             "trend_measured": False,
             "competitors": [{"title": "경쟁 글", "postdate": "2026-09-01",
                              "link": "https://x"}]},
        ],
        "caveat": keyword_research.DATA_CAVEAT,
        "key_owner": "여자친구",
    }
    r = make_review(
        status="ready",
        ai_json=json.dumps(_READY_AI, ensure_ascii=False),
        research_json=json.dumps(research, ensure_ascii=False),
    )
    html = client.get(f"/reviews/{r.id}").get_data(as_text=True)
    assert "키워드 조사 메모" in html
    assert "문래 갈매기살 웨이팅" in html
    assert "앞 3팀·15분 기록이 있다" in html
    assert "시작 검색어라 넓다" in html          # 제외 이유
    assert "정량 수요 미확인" in html            # 데이터 한계
    assert "검색량이 아니" in html               # 오독 금지 문구
    assert "여자친구" in html                    # 누구 키로 조사했는지


def test_detail_tells_how_to_turn_research_on_when_no_keys(client, flask_app,
                                                           make_review):
    r = make_review(status="ready", ai_json=json.dumps(_READY_AI, ensure_ascii=False))
    html = client.get(f"/reviews/{r.id}").get_data(as_text=True)
    assert "키워드 조사가 꺼져 있어" in html
    assert "/settings" in html


def test_detail_hides_the_hint_once_keys_are_set(client, flask_app, make_review,
                                                 couple_user):
    couple_user.naver_api_key_id = FAKE_ID
    couple_user.naver_api_key = FAKE_SECRET
    db.session.commit()
    r = make_review(status="ready", ai_json=json.dumps(_READY_AI, ensure_ascii=False))
    html = client.get(f"/reviews/{r.id}").get_data(as_text=True)
    assert "키워드 조사가 꺼져 있어" not in html
    assert FAKE_ID not in html


# --------------------------------------------------------------------------- #
# 9. 워커 — 어느 키를 쓰는가
# --------------------------------------------------------------------------- #
def test_worker_picks_the_authors_keys_then_falls_back_to_partner(
    flask_app, couple_user, make_review
):
    from models import User

    partner = User(
        email="b@example.com", password_hash="x", display_name="여자친구",
        couple_id=couple_user.couple_id, status="approved",
        naver_api_key_id=FAKE_ID, naver_api_key=FAKE_SECRET,
    )
    db.session.add(partner)
    db.session.commit()

    r = make_review()
    # 작성자는 키가 없다 → 파트너 키로 폴백하고, 누구 키인지 이름이 따라온다.
    creds, owner = app_module._naver_credentials_for(r)
    assert creds == (FAKE_ID, FAKE_SECRET) and owner == "여자친구"

    # 작성자가 키를 넣으면 자기 키가 이긴다.
    couple_user.naver_api_key_id = "MY-ID"
    couple_user.naver_api_key = "MY-SECRET"
    db.session.commit()
    creds, owner = app_module._naver_credentials_for(r)
    assert creds == ("MY-ID", "MY-SECRET") and owner == couple_user.display_name


def test_worker_without_any_keys_reports_no_credentials(flask_app, make_review):
    r = make_review()
    assert app_module._naver_credentials_for(r) == ((None, None), "")
